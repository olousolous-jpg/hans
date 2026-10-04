/*
    Liquid Glass – efekt pro KWin 6.7, upravená kopie vestavěného blur efektu
    (kwin v6.7.5, src/plugins/blur/blur.cpp). Změny: onscreen průchod se
    sklem (lom, rozptyl, lesk), skleněný rámeček kolem oken, animace.

    SPDX-FileCopyrightText: 2010 Fredrik Höglund <fredrik@kde.org>
    SPDX-FileCopyrightText: 2011 Philipp Knechtges <philipp-dev@knechtges.com>
    SPDX-FileCopyrightText: 2018 Alex Nemeth <alex.nemeth329@gmail.com>

    SPDX-License-Identifier: GPL-2.0-or-later
*/

#include "liquidglass.h"
// KConfigSkeleton
#include "liquidglassconfig.h"

#include "core/rendertarget.h"
#include "input.h"
#include "input_event.h"
#include "options.h"
#include "pointer_input.h"
#include "core/renderviewport.h"
#include "effect/effecthandler.h"
#include "opengl/glplatform.h"
#include "scene/backgroundeffectitem.h"
#include "scene/decorationitem.h"
#include "scene/scene.h"
#include "scene/surfaceitem.h"
#include "scene/windowitem.h"
#include "wayland/backgroundeffect_v1.h"
#include "wayland/display.h"
#include "wayland/surface.h"
#include "wayland_server.h"
#include "window.h"

#if KWIN_BUILD_X11
#include "utils/xcbutils.h"
#include "x11window.h"
#endif

#include <QGuiApplication>
#include <QMatrix4x4>
#include <QScreen>
#include <QTime>
#include <QTimer>
#include <QWindow>
#include <cmath> // for ceil()
#include <cstdlib>

#include <KConfigGroup>
#include <KSharedConfig>

#include <KDecoration3/Decoration>

Q_LOGGING_CATEGORY(KWIN_LIQUIDGLASS, "kwin_effect_liquidglass", QtWarningMsg)

static void ensureResources()
{
    // Must initialize resources manually because the effect is a static lib.
    Q_INIT_RESOURCE(liquidglass);
}

namespace KWin
{

static const QByteArray s_blurAtomName = QByteArrayLiteral("_KDE_NET_WM_BLUR_BEHIND_REGION");

static QMatrix4x4 colorTransformMatrix(qreal saturation, qreal contrast)
{
    QMatrix4x4 saturationMatrix;
    QMatrix4x4 contrastMatrix;

    if (!qFuzzyCompare(saturation, 1.0)) {
        const qreal rval = (1.0 - saturation) * 0.2126;
        const qreal gval = (1.0 - saturation) * 0.7152;
        const qreal bval = (1.0 - saturation) * 0.0722;

        saturationMatrix = QMatrix4x4(rval + saturation, rval, rval, 0.0,
                                      gval, gval + saturation, gval, 0.0,
                                      bval, bval, bval + saturation, 0.0,
                                      0.0, 0.0, 0.0, 1.0);
    }

    if (!qFuzzyCompare(contrast, 1.0)) {
        const float transl = (1.0 - contrast) / 2.0;

        contrastMatrix = QMatrix4x4(contrast, 0.0, 0.0, 0.0,
                                    0.0, contrast, 0.0, 0.0,
                                    0.0, 0.0, contrast, 0.0,
                                    transl, transl, transl, 1.0);
    }

    return contrastMatrix * saturationMatrix;
}

/**
 * Stisk levého tlačítka na okraji dekorace (levý, pravý, horní, dolní okraj,
 * ne roh) spustí přesun okna jako při tažení za titulek. Filtr stojí těsně
 * před filtrem dekorací KWinu, takže dekorace stisk vůbec nedostane a
 * nezačne roztahovat. Rohy, modifikátory (Meta+tažení) a ostatní tlačítka
 * zůstávají beze změny.
 */
class BorderMoveFilter : public InputEventFilter
{
public:
    explicit BorderMoveFilter(LiquidGlassEffect *effect)
        : InputEventFilter(InputFilterOrder::Decoration)
        , m_effect(effect)
    {
    }

    bool pointerButton(PointerButtonEvent *event) override
    {
        if (!m_effect->borderMoves() || event->button != Qt::LeftButton
            || event->state != PointerButtonState::Pressed
            || event->modifiersRelevantForShortcuts != Qt::NoModifier) {
            return false;
        }
        // okno pod kurzorem; kurzor musí být na jeho dekoraci, ne v obsahu
        Window *window = input()->pointer()->hover();
        if (!window || !window->decoration() || !window->isMovable()
            || window->clientGeometry().contains(event->position)) {
            return false;
        }
        switch (window->decoration()->sectionUnderMouse()) {
        case Qt::LeftSection:
        case Qt::RightSection:
        case Qt::TopSection:
        case Qt::BottomSection:
            break;
        default:
            return false; // titulek, tlačítka a rohy řeší dekorace jako obvykle
        }
        window->performMousePressCommand(Options::MouseActivateRaiseAndMove, event->position);
        return true;
    }

private:
    LiquidGlassEffect *const m_effect;
};

LiquidGlassEffect::LiquidGlassEffect()
{
    LiquidGlassConfig::instance(effects->config());
    ensureResources();

    m_glassPass.shader = ShaderManager::instance()->generateShaderFromFile(ShaderTrait::MapTexture,
                                                                           QStringLiteral(":/effects/liquidglass/shaders/glass.vert"),
                                                                           QStringLiteral(":/effects/liquidglass/shaders/glass.frag"));
    if (!m_glassPass.shader) {
        qCWarning(KWIN_LIQUIDGLASS) << "Failed to load glass pass shader";
        return;
    } else {
        auto *shader = m_glassPass.shader.get();
        m_glassPass.mvpMatrixLocation = shader->uniformLocation("modelViewProjectionMatrix");
        m_glassPass.colorMatrixLocation = shader->uniformLocation("colorMatrix");
        m_glassPass.offsetLocation = shader->uniformLocation("offset");
        m_glassPass.halfpixelLocation = shader->uniformLocation("halfpixel");
        m_glassPass.boxLocation = shader->uniformLocation("box");
        m_glassPass.cornerRadiusLocation = shader->uniformLocation("cornerRadius");
        m_glassPass.opacityLocation = shader->uniformLocation("opacity");
        m_glassPass.clipEnabledLocation = shader->uniformLocation("clipEnabled");
        m_glassPass.texSizeLocation = shader->uniformLocation("texSize");
        m_glassPass.edgeWidthLocation = shader->uniformLocation("edgeWidth");
        m_glassPass.refractionLocation = shader->uniformLocation("refraction");
        m_glassPass.chromaLocation = shader->uniformLocation("chroma");
        m_glassPass.specularLocation = shader->uniformLocation("specular");
        m_glassPass.tintLocation = shader->uniformLocation("tint");
        m_glassPass.lightPosLocation = shader->uniformLocation("lightPos");
        m_glassPass.lightOnLocation = shader->uniformLocation("lightOn");
        m_glassPass.motionLocation = shader->uniformLocation("motion");
        m_glassPass.lightAngleLocation = shader->uniformLocation("lightAngle");
        m_glassPass.sharpTexLocation = shader->uniformLocation("sharpTex");
        m_glassPass.frameBoxLocation = shader->uniformLocation("frameBox");
        m_glassPass.frameRadiusLocation = shader->uniformLocation("frameRadius");
        m_glassPass.hasRingLocation = shader->uniformLocation("hasRing");
        m_glassPass.ringClarityLocation = shader->uniformLocation("ringClarity");
        m_glassPass.timeLocation = shader->uniformLocation("time");
        m_glassPass.waveAmpLocation = shader->uniformLocation("waveAmp");
        m_glassPass.waveDirLocation = shader->uniformLocation("waveDir");
    }

    m_downsamplePass.shader = ShaderManager::instance()->generateShaderFromFile(ShaderTrait::MapTexture,
                                                                                QStringLiteral(":/effects/liquidglass/shaders/vertex.vert"),
                                                                                QStringLiteral(":/effects/liquidglass/shaders/downsample.frag"));
    if (!m_downsamplePass.shader) {
        qCWarning(KWIN_LIQUIDGLASS) << "Failed to load downsampling pass shader";
        return;
    } else {
        m_downsamplePass.mvpMatrixLocation = m_downsamplePass.shader->uniformLocation("modelViewProjectionMatrix");
        m_downsamplePass.offsetLocation = m_downsamplePass.shader->uniformLocation("offset");
        m_downsamplePass.halfpixelLocation = m_downsamplePass.shader->uniformLocation("halfpixel");
    }

    m_upsamplePass.shader = ShaderManager::instance()->generateShaderFromFile(ShaderTrait::MapTexture,
                                                                              QStringLiteral(":/effects/liquidglass/shaders/vertex.vert"),
                                                                              QStringLiteral(":/effects/liquidglass/shaders/upsample.frag"));
    if (!m_upsamplePass.shader) {
        qCWarning(KWIN_LIQUIDGLASS) << "Failed to load upsampling pass shader";
        return;
    } else {
        m_upsamplePass.mvpMatrixLocation = m_upsamplePass.shader->uniformLocation("modelViewProjectionMatrix");
        m_upsamplePass.offsetLocation = m_upsamplePass.shader->uniformLocation("offset");
        m_upsamplePass.halfpixelLocation = m_upsamplePass.shader->uniformLocation("halfpixel");
    }

    m_noisePass.shader = ShaderManager::instance()->generateShaderFromFile(ShaderTrait::MapTexture,
                                                                           QStringLiteral(":/effects/liquidglass/shaders/vertex.vert"),
                                                                           QStringLiteral(":/effects/liquidglass/shaders/noise.frag"));
    if (!m_noisePass.shader) {
        qCWarning(KWIN_LIQUIDGLASS) << "Failed to load noise pass shader";
        return;
    } else {
        m_noisePass.mvpMatrixLocation = m_noisePass.shader->uniformLocation("modelViewProjectionMatrix");
        m_noisePass.noiseTextureSizeLocation = m_noisePass.shader->uniformLocation("noiseTextureSize");
    }

    initBlurStrengthValues();
    reconfigure(ReconfigureAll);

#if KWIN_BUILD_X11
    if (effects->xcbConnection()) {
        net_wm_blur_region = effects->announceSupportProperty(s_blurAtomName, this);
    }
#endif

    waylandServer()->backgroundEffectManager()->addBlurCapability();

    connect(effects, &EffectsHandler::windowAdded, this, &LiquidGlassEffect::slotWindowAdded);
    connect(effects, &EffectsHandler::windowDeleted, this, &LiquidGlassEffect::slotWindowDeleted);
    connect(effects, &EffectsHandler::viewRemoved, this, &LiquidGlassEffect::slotViewRemoved);
#if KWIN_BUILD_X11
    connect(effects, &EffectsHandler::propertyNotify, this, &LiquidGlassEffect::slotPropertyNotify);
    connect(effects, &EffectsHandler::xcbConnectionChanged, this, [this]() {
        net_wm_blur_region = effects->announceSupportProperty(s_blurAtomName, this);
    });
#endif

    // přesun okna tažením za okraj (jen Wayland, na X11 input() chybí)
    if (input()) {
        m_borderFilter = std::make_unique<BorderMoveFilter>(this);
        input()->installInputEventFilter(m_borderFilter.get());
    }

    // Liquid Glass: lesk sleduje kurzor, pomalé „dýchání“ světla
    connect(effects, &EffectsHandler::mouseChanged, this,
            [this](const QPointF &pos, const QPointF &oldpos, Qt::MouseButtons, Qt::MouseButtons, Qt::KeyboardModifiers, Qt::KeyboardModifiers) {
                slotMouseChanged(pos, oldpos);
            });
    m_clock.start();
    m_shimmerTimer.setInterval(33);
    connect(&m_shimmerTimer, &QTimer::timeout, this, [this]() {
        for (auto &[window, data] : m_windows) {
            repaintGlass(window);
        }
    });
    if (m_idleShimmer) {
        m_shimmerTimer.start();
    }

    // Fetch the blur regions for all windows
    const auto stackingOrder = effects->stackingOrder();
    for (EffectWindow *window : stackingOrder) {
        slotWindowAdded(window);
    }

    m_valid = true;
}

LiquidGlassEffect::~LiquidGlassEffect()
{
    if (m_borderFilter && input()) {
        input()->uninstallInputEventFilter(m_borderFilter.get());
    }
    waylandServer()->backgroundEffectManager()->removeBlurCapability();
}


void LiquidGlassEffect::initBlurStrengthValues()
{
    // This function creates an array of blur strength values that are evenly distributed

    // The range of the slider on the blur settings UI
    int numOfBlurSteps = 15;
    int remainingSteps = numOfBlurSteps;

    /*
     * Explanation for these numbers:
     *
     * The texture blur amount depends on the downsampling iterations and the offset value.
     * By changing the offset we can alter the blur amount without relying on further downsampling.
     * But there is a minimum and maximum value of offset per downsample iteration before we
     * get artifacts.
     *
     * The minOffset variable is the minimum offset value for an iteration before we
     * get blocky artifacts because of the downsampling.
     *
     * The maxOffset value is the maximum offset value for an iteration before we
     * get diagonal line artifacts because of the nature of the dual kawase blur algorithm.
     *
     * The expandSize value is the minimum value for an iteration before we reach the end
     * of a texture in the shader and sample outside of the area that was copied into the
     * texture from the screen.
     */

    // {minOffset, maxOffset, expandSize}
    blurOffsets.append({1.0, 2.0, 10}); // Down sample size / 2
    blurOffsets.append({2.0, 3.0, 20}); // Down sample size / 4
    blurOffsets.append({2.0, 5.0, 50}); // Down sample size / 8
    blurOffsets.append({3.0, 8.0, 150}); // Down sample size / 16
    // blurOffsets.append({5.0, 10.0, 400}); // Down sample size / 32
    // blurOffsets.append({7.0, ?.0});       // Down sample size / 64

    float offsetSum = 0;

    for (int i = 0; i < blurOffsets.size(); i++) {
        offsetSum += blurOffsets[i].maxOffset - blurOffsets[i].minOffset;
    }

    for (int i = 0; i < blurOffsets.size(); i++) {
        int iterationNumber = std::ceil((blurOffsets[i].maxOffset - blurOffsets[i].minOffset) / offsetSum * numOfBlurSteps);
        remainingSteps -= iterationNumber;

        if (remainingSteps < 0) {
            iterationNumber += remainingSteps;
        }

        float offsetDifference = blurOffsets[i].maxOffset - blurOffsets[i].minOffset;

        for (int j = 1; j <= iterationNumber; j++) {
            // {iteration, offset}
            blurStrengthValues.append({i + 1, blurOffsets[i].minOffset + (offsetDifference / iterationNumber) * j});
        }
    }
}

void LiquidGlassEffect::reconfigure(ReconfigureFlags flags)
{
    Q_UNUSED(flags)
    LiquidGlassConfig::self()->read();

    int blurStrength = LiquidGlassConfig::blurStrength() - 1;
    m_iterationCount = blurStrengthValues[blurStrength].iteration;
    m_offset = blurStrengthValues[blurStrength].offset;
    m_expandSize = blurOffsets[m_iterationCount - 1].expandSize;
    m_noiseStrength = LiquidGlassConfig::noiseStrength();
    m_colorMatrix = colorTransformMatrix(LiquidGlassConfig::saturation() / 100.0, 1.0);

    const auto classList = [](const QString &value) {
        QStringList out;
        for (const QString &part : value.split(QLatin1Char(','), Qt::SkipEmptyParts)) {
            const QString t = part.trimmed().toLower();
            if (!t.isEmpty()) {
                out << t;
            }
        }
        return out;
    };
    m_edgeWidth = std::max(1, LiquidGlassConfig::edgeWidth());
    m_refraction = std::clamp(LiquidGlassConfig::refraction(), 0, 64);
    m_chroma = LiquidGlassConfig::chromaticAberration() / 100.0f;
    m_specular = LiquidGlassConfig::specular() / 100.0f;
    m_tintStrength = LiquidGlassConfig::tintStrength() / 100.0f;
    m_darkTint = LiquidGlassConfig::darkTint();
    m_cornerRadius = std::max(0, LiquidGlassConfig::cornerRadius());
    m_ringWidth = std::clamp(LiquidGlassConfig::ringWidth(), 0, 40);
    m_ringExclude = classList(LiquidGlassConfig::ringExcludeClasses());
    m_forceGlass = classList(LiquidGlassConfig::forceGlassClasses());
    m_mouseLight = LiquidGlassConfig::mouseLight();
    m_liquidMotion = LiquidGlassConfig::liquidMotion();
    m_motionStrength = LiquidGlassConfig::motionStrength() / 100.0f;
    m_idleShimmer = LiquidGlassConfig::idleShimmer();
    m_ringClarity = LiquidGlassConfig::ringClarity() / 100.0f;
    m_waveStrength = LiquidGlassConfig::waveStrength() / 100.0f;
    m_borderMoves = LiquidGlassConfig::borderMoves();
    if (m_idleShimmer && m_valid) {
        m_shimmerTimer.start();
    } else {
        m_shimmerTimer.stop();
    }

    if (m_valid) {
        // nastavení rámečku a vynuceného skla mění tvar skla u všech oken
        const auto stackingOrder = effects->stackingOrder();
        for (EffectWindow *window : stackingOrder) {
            updateBlurRegion(window);
        }
    }
    for (auto &[window, data] : m_windows) {
        data.blurItem->setPixelsToExpandRepaintsBelowOpaqueRegions(m_expandSize + sampleMargin());
        updateItemGeometry(window);
    }

    // Update all windows for the blur to take effect
    effects->addRepaintFull();
}

void LiquidGlassEffect::updateBlurRegion(EffectWindow *w)
{
    std::optional<RegionF> content;
    std::optional<RegionF> frame;

#if KWIN_BUILD_X11
    if (net_wm_blur_region != XCB_ATOM_NONE) {
        if (const auto x11Window = qobject_cast<X11Window *>(w->window())) {
            Xcb::Property wmBlurRegionProperty(false, x11Window->window(), net_wm_blur_region, XCB_ATOM_CARDINAL, 0, 32768);
            if (const auto cardinals = wmBlurRegionProperty.array<uint32_t>()) {
                if (cardinals->size() == 0 || cardinals->size() == 1) {
                    // It means blur background behind whole window.
                    content = RegionF();
                } else if (cardinals->size() % 4 == 0) {
                    RegionF region;
                    for (uint i = 0; i < cardinals->size();) {
                        const int x = (*cardinals)[i++];
                        const int y = (*cardinals)[i++];
                        const int w = (*cardinals)[i++];
                        const int h = (*cardinals)[i++];
                        region += Xcb::fromXNative(Rect(x, y, w, h));
                    }
                    content = region;
                }
            }
        }
    }
#endif

    if (SurfaceInterface *surface = w->surface()) {
        if (!surface->blurRegion().isEmpty()) {
            content = surface->blurRegion();
        }
    }

    if (auto internal = w->internalWindow()) {
        const auto property = internal->property("kwin_blur");
        if (property.isValid()) {
            content = property.value<RegionF>();
        }
    }

    if (w->decorationHasAlpha() && decorationSupportsBlurBehind(w)) {
        frame = decorationBlurRegion(w);
    }

    // sklo přes celé okno pro vybrané aplikace (prázdný region = celé okno)
    if (!content.has_value() && forcesGlass(w)) {
        content = RegionF();
    }

    const bool ring = wantsRing(w);

    if (content.has_value() || frame.has_value() || ring) {
        GlassWindowData &data = m_windows[w];
        data.content = content;
        data.frame = frame;
        data.ring = ring;
        if (!data.blurItem) {
            data.blurItem = std::make_unique<BackgroundEffectItem>(w->windowItem());
        }
        data.blurItem->setPixelsToExpandRepaintsBelowOpaqueRegions(m_expandSize + sampleMargin());
        data.blurItem->setEffectBoundingRect(blurRegion(w).boundingRect());
        updateItemGeometry(w);
    } else {
        if (auto it = m_windows.find(w); it != m_windows.end()) {
            effects->makeOpenGLContextCurrent();
            m_windows.erase(it);
        }
    }
}

void LiquidGlassEffect::slotWindowAdded(EffectWindow *w)
{
    SurfaceInterface *surf = w->surface();

    if (surf) {
        windowBlurChangedConnections[w] = connect(surf, &SurfaceInterface::blurChanged, this, [this, w]() {
            if (w) {
                updateBlurRegion(w);
            }
        });
    }
    if (auto internal = w->internalWindow()) {
        internal->installEventFilter(this);
    }

    setupDecorationConnections(w);
    connect(w, &EffectWindow::windowDecorationChanged, this, [this, w]() {
        setupDecorationConnections(w);
        updateBlurRegion(w);
    });
    connect(w, &EffectWindow::windowMaximizedStateChanged, this, [this, w]() {
        updateBlurRegion(w);
    });
    connect(w, &EffectWindow::windowFullScreenChanged, this, [this, w]() {
        updateBlurRegion(w);
    });
    connect(w, &EffectWindow::windowFrameGeometryChanged, this, &LiquidGlassEffect::slotFrameGeometryChanged);
    if (WindowItem *item = w->windowItem()) {
        // BackgroundEffectItem si při změně velikosti okna ořízne geometrii
        // na rám okna; rámeček skla a okraj pro lom leží mimo, tak ji vrátíme.
        connect(item->windowContainer(), &Item::boundingRectChanged, this, [this, w]() {
            updateItemGeometry(w);
        });
    }

    updateBlurRegion(w);
}

void LiquidGlassEffect::slotWindowDeleted(EffectWindow *w)
{
    if (auto it = m_windows.find(w); it != m_windows.end()) {
        effects->makeOpenGLContextCurrent();
        m_windows.erase(it);
    }
    if (auto it = windowBlurChangedConnections.find(w); it != windowBlurChangedConnections.end()) {
        disconnect(*it);
        windowBlurChangedConnections.erase(it);
    }
}

void LiquidGlassEffect::slotViewRemoved(KWin::RenderView *view)
{
    for (auto &[window, data] : m_windows) {
        if (auto it = data.render.find(view); it != data.render.end()) {
            effects->makeOpenGLContextCurrent();
            data.render.erase(it);
        }
    }
}

#if KWIN_BUILD_X11
void LiquidGlassEffect::slotPropertyNotify(EffectWindow *w, long atom)
{
    if (w && atom == net_wm_blur_region && net_wm_blur_region != XCB_ATOM_NONE) {
        updateBlurRegion(w);
    }
}
#endif

void LiquidGlassEffect::setupDecorationConnections(EffectWindow *w)
{
    if (!w->decoration()) {
        return;
    }

    connect(w->decoration(), &KDecoration3::Decoration::blurRegionChanged, this, [this, w]() {
        updateBlurRegion(w);
    });
}

bool LiquidGlassEffect::eventFilter(QObject *watched, QEvent *event)
{
    auto internal = qobject_cast<QWindow *>(watched);
    if (internal && event->type() == QEvent::DynamicPropertyChange) {
        QDynamicPropertyChangeEvent *pe = static_cast<QDynamicPropertyChangeEvent *>(event);
        if (pe->propertyName() == "kwin_blur") {
            if (auto w = effects->findWindow(internal)) {
                updateBlurRegion(w);
            }
        }
    }
    return false;
}

bool LiquidGlassEffect::enabledByDefault()
{
    const auto context = effects->openglContext();
    if (!context || context->isSoftwareRenderer()) {
        return false;
    }
    GLPlatform *gl = context->glPlatform();

    if (gl->isIntel() && gl->chipClass() < SandyBridge) {
        return false;
    }
    if (gl->isPanfrost() && gl->chipClass() <= MaliT8XX) {
        return false;
    }
    // The blur effect works, but is painfully slow (FPS < 5) on Mali and VideoCore
    if (gl->isLima() || gl->isVideoCore4() || gl->isVideoCore3D()) {
        return false;
    }
    return true;
}

bool LiquidGlassEffect::supported()
{
    return effects->isOpenGLCompositing();
}

bool LiquidGlassEffect::decorationSupportsBlurBehind(const EffectWindow *w) const
{
    return w->decoration() && !w->decoration()->blurRegion().isNull();
}

RegionF LiquidGlassEffect::decorationBlurRegion(const EffectWindow *w) const
{
    if (!decorationSupportsBlurBehind(w)) {
        return RegionF();
    }

    RegionF decorationRegion = RegionF(w->decoration()->rect()) - w->contentsRect();
    //! we return only blurred regions that belong to decoration region
    return decorationRegion.intersected(RegionF(w->decoration()->blurRegion()));
}

RegionF LiquidGlassEffect::blurRegion(EffectWindow *w) const
{
    RegionF region;

    if (auto it = m_windows.find(w); it != m_windows.end()) {
        const std::optional<RegionF> &content = it->second.content;
        const std::optional<RegionF> &frame = it->second.frame;
        if (content.has_value()) {
            if (content->isEmpty()) {
                // An empty region means that the blur effect should be enabled
                // for the whole window.
                region = w->contentsRect();
            } else {
                region = content->translated(w->contentsRect().topLeft()) & w->contentsRect();
            }
            if (frame.has_value()) {
                region += frame.value();
            }
        } else if (frame.has_value()) {
            region = frame.value();
        }

        if (it->second.ring) {
            // skleněný rámeček kolem okna; díra uprostřed je zmenšená o
            // zaoblení, aby sklo bylo i pod zaoblenými rohy okna
            const RectF frameRect(0, 0, w->width(), w->height());
            const QVector4D r = shapeRadius(w, false).toVector();
            const qreal inner = std::max({r.x(), r.y(), r.z(), r.w()});
            const qreal ring = m_ringWidth;
            RegionF ringRegion(frameRect.grownBy(QMarginsF(ring, ring, ring, ring)));
            if (frameRect.width() > 2 * inner && frameRect.height() > 2 * inner) {
                ringRegion -= RegionF(frameRect.grownBy(QMarginsF(-inner, -inner, -inner, -inner)));
            }
            region += ringRegion;
        }
    }

    return region;
}

bool LiquidGlassEffect::wantsRing(const EffectWindow *w) const
{
    if (m_ringWidth <= 0) {
        return false;
    }
    if (!(w->isNormalWindow() || w->isDialog()) || w->isDesktop() || w->isDock()) {
        return false;
    }
    if (w->isFullScreen()) {
        return false;
    }
    if (const Window *window = w->window(); window && window->maximizeMode() == MaximizeFull) {
        return false;
    }
    if (!m_ringExclude.isEmpty()) {
        const QString cls = w->windowClass().toLower();
        for (const QString &needle : m_ringExclude) {
            if (cls.contains(needle)) {
                return false;
            }
        }
    }
    return true;
}

bool LiquidGlassEffect::forcesGlass(const EffectWindow *w) const
{
    if (m_forceGlass.isEmpty() || w->isDesktop()) {
        return false;
    }
    const QString cls = w->windowClass().toLower();
    for (const QString &needle : m_forceGlass) {
        if (cls.contains(needle)) {
            return true;
        }
    }
    return false;
}

BorderRadius LiquidGlassEffect::shapeRadius(const EffectWindow *w, bool ring) const
{
    BorderRadius radius = w->window() ? w->window()->borderRadius() : BorderRadius();
    if (radius.isNull()) {
        radius = BorderRadius(m_cornerRadius);
    }
    if (ring) {
        const qreal grow = m_ringWidth;
        radius = BorderRadius(radius.topLeft() + grow, radius.topRight() + grow,
                              radius.bottomRight() + grow, radius.bottomLeft() + grow);
    }
    return radius;
}

int LiquidGlassEffect::sampleMargin() const
{
    // lom a setrvačnost čtou pozadí až za hranou skla
    return m_refraction + (m_liquidMotion ? 24 : 0) + 4;
}

RectF LiquidGlassEffect::glassBounds(EffectWindow *w) const
{
    return blurRegion(w).boundingRect();
}

void LiquidGlassEffect::updateItemGeometry(EffectWindow *w)
{
    auto it = m_windows.find(w);
    if (it == m_windows.end() || !it->second.blurItem) {
        return;
    }
    const RectF bounds = glassBounds(w);
    if (bounds.isEmpty()) {
        return;
    }
    const qreal m = sampleMargin();
    it->second.blurItem->setGeometry(bounds.grownBy(QMarginsF(m, m, m, m)));
}

void LiquidGlassEffect::repaintGlass(EffectWindow *w)
{
    const RectF bounds = glassBounds(w);
    if (!bounds.isEmpty()) {
        effects->addRepaint(bounds.translated(w->pos()));
    }
}

void LiquidGlassEffect::slotFrameGeometryChanged(EffectWindow *w, const RectF &oldGeometry)
{
    auto it = m_windows.find(w);
    if (it == m_windows.end()) {
        return;
    }
    const RectF now = w->frameGeometry();
    if (now.size() != oldGeometry.size()) {
        it->second.blurItem->setEffectBoundingRect(blurRegion(w).boundingRect());
        updateItemGeometry(w);
    }
    const QPointF delta = now.topLeft() - oldGeometry.topLeft();
    if (m_waveStrength > 0 && !delta.isNull() && now.size() == oldGeometry.size()) {
        // vlnění: každý posun přidá energii, směr se plynule natáčí za pohybem
        const qreal len = std::hypot(delta.x(), delta.y());
        GlassWindowData &glass = it->second;
        glass.wave = std::min<qreal>(1.0, glass.wave + len * 0.04);
        const QPointF dir = glass.waveDir * 0.7 + (delta / len) * 0.3;
        const qreal dl = std::hypot(dir.x(), dir.y());
        if (dl > 1e-3) {
            glass.waveDir = dir / dl;
        }
        repaintGlass(w);
    }
    if (m_liquidMotion && m_motionStrength > 0) {
        if (!delta.isNull() && now.size() == oldGeometry.size()) {
            QPointF m = it->second.motion * 0.6 + delta * (0.9 * m_motionStrength);
            const qreal limit = 24.0 * m_motionStrength;
            const qreal len = std::hypot(m.x(), m.y());
            if (len > limit && len > 0) {
                m *= limit / len;
            }
            it->second.motion = m;
            repaintGlass(w);
        }
    }
}

void LiquidGlassEffect::slotMouseChanged(const QPointF &pos, const QPointF &oldpos)
{
    if (!m_mouseLight || m_specular <= 0 || (pos - oldpos).manhattanLength() < 2) {
        return;
    }
    // Lesk od kurzoru je vidět jen u hrany (útlum na ~300 px), takže se
    // překresluje, jen když je kurzor v pásu kolem hrany, ne nad celým oknem.
    const qreal reach = 260;
    const auto nearEdge = [reach](const RectF &bounds, const QPointF &p) {
        const RectF outer = bounds.grownBy(QMarginsF(reach, reach, reach, reach));
        if (!outer.contains(p)) {
            return false;
        }
        if (bounds.width() <= 2 * reach || bounds.height() <= 2 * reach) {
            return true;
        }
        return !bounds.grownBy(QMarginsF(-reach, -reach, -reach, -reach)).contains(p);
    };
    for (auto &[window, data] : m_windows) {
        const RectF bounds = glassBounds(window).translated(window->pos());
        if (nearEdge(bounds, pos) || nearEdge(bounds, oldpos)) {
            effects->addRepaint(bounds);
        }
    }
}

void LiquidGlassEffect::prePaintScreen(ScreenPrePaintData &data)
{
    m_currentView = data.view;

    // dozvuk setrvačnosti: posun skla se po zastavení okna plynule vrátí
    const qint64 now = m_clock.elapsed();
    const qreal dt = std::clamp<qint64>(now - m_lastFrameMs, 0, 100);
    m_lastFrameMs = now;
    if (dt > 0) {
        const qreal decay = std::exp(-dt / 90.0);
        const qreal waveDecay = std::exp(-dt / 450.0);
        for (auto &[window, glass] : m_windows) {
            if (glass.motion.isNull() && glass.wave <= 0) {
                continue;
            }
            glass.motion *= decay;
            if (std::hypot(glass.motion.x(), glass.motion.y()) < 0.15) {
                glass.motion = QPointF();
            }
            glass.wave *= waveDecay;
            if (glass.wave < 0.01) {
                glass.wave = 0;
            }
            repaintGlass(window);
        }
    }

    effects->prePaintScreen(data);
}

bool LiquidGlassEffect::shouldBlur(const EffectWindow *w, int mask, const WindowPaintData &data) const
{
    if (effects->activeFullScreenEffect() && !w->data(WindowForceBlurRole).toBool()) {
        return false;
    }

    if (w->isDesktop()) {
        return false;
    }

    bool scaled = !qFuzzyCompare(data.xScale(), 1.0) && !qFuzzyCompare(data.yScale(), 1.0);
    bool translated = data.xTranslation() || data.yTranslation();

    if ((scaled || (translated || (mask & PAINT_WINDOW_TRANSFORMED))) && !w->data(WindowForceBlurRole).toBool()) {
        return false;
    }

    return true;
}

void LiquidGlassEffect::drawWindow(const RenderTarget &renderTarget, const RenderViewport &viewport, EffectWindow *w, int mask, const Region &deviceRegion, WindowPaintData &data)
{
    blur(renderTarget, viewport, w, mask, deviceRegion, data);

    // Draw the window over the blurred area
    effects->drawWindow(renderTarget, viewport, w, mask, deviceRegion, data);
}

GLTexture *LiquidGlassEffect::ensureNoiseTexture()
{
    if (m_noiseStrength == 0) {
        return nullptr;
    }

    const qreal scale = std::max(1.0, QGuiApplication::primaryScreen()->logicalDotsPerInch() / 96.0);
    if (!m_noisePass.noiseTexture || m_noisePass.noiseTextureScale != scale || m_noisePass.noiseTextureStength != m_noiseStrength) {
        // Init randomness based on time
        std::srand((uint)QTime::currentTime().msec());

        QImage noiseImage(QSize(256, 256), QImage::Format_Grayscale8);

        for (int y = 0; y < noiseImage.height(); y++) {
            uint8_t *noiseImageLine = (uint8_t *)noiseImage.scanLine(y);

            for (int x = 0; x < noiseImage.width(); x++) {
                noiseImageLine[x] = std::rand() % m_noiseStrength;
            }
        }

        noiseImage = noiseImage.scaled(noiseImage.size() * scale);

        m_noisePass.noiseTexture = GLTexture::upload(noiseImage);
        if (!m_noisePass.noiseTexture) {
            return nullptr;
        }
        m_noisePass.noiseTexture->setFilter(GL_NEAREST);
        m_noisePass.noiseTexture->setWrapMode(GL_REPEAT);
        m_noisePass.noiseTextureScale = scale;
        m_noisePass.noiseTextureStength = m_noiseStrength;
    }

    return m_noisePass.noiseTexture.get();
}

void LiquidGlassEffect::blur(const RenderTarget &renderTarget, const RenderViewport &viewport, EffectWindow *w, int mask, const Region &deviceRegion, WindowPaintData &data)
{
    auto it = m_windows.find(w);
    if (it == m_windows.end()) {
        return;
    }

    GlassWindowData &blurInfo = it->second;
    GlassRenderData &renderInfo = blurInfo.render[m_currentView];
    if (!shouldBlur(w, mask, data)) {
        return;
    }

    // Compute the effective blur shape. Note that if the window is transformed, so will be the blur shape.
    RegionF blurShape = blurRegion(w);
    if (data.xScale() != 1 || data.yScale() != 1) {
        blurShape.scale(data.xScale(), data.yScale());
    }
    if (data.xTranslation() || data.yTranslation()) {
        blurShape.translate(data.xTranslation(), data.yTranslation());
    }

    blurShape.translate(w->pos());

    // Pozadí se zachytává s okrajem navíc: lom a setrvačnost čtou i to, co je
    // za hranou skla. Oříznuto na právě kreslený výstup.
    const RectF shapeBounds = blurShape.boundingRect();
    const qreal margin = sampleMargin();
    const Rect backgroundRect = shapeBounds.grownBy(QMarginsF(margin, margin, margin, margin)).rounded()
        & viewport.renderRect().rounded();
    if (backgroundRect.isEmpty()) {
        return;
    }
    const Rect scaledBackgroundRect = backgroundRect.scaled(viewport.scale()).rounded();
    const Rect deviceBackgroundRect = viewport.mapToDeviceCoordinates(backgroundRect).rounded();
    const auto opacity = w->opacity() * data.opacity();

    // Get the effective shape that will be actually blurred. It's possible that all of it will be clipped.
    QList<RectF> effectiveShape;
    effectiveShape.reserve(blurShape.rects().size());
    if (deviceRegion != Region::infinite()) {
        for (const Rect &clipRect : deviceRegion.rects()) {
            const RectF deviceClipRect = clipRect.translated(-deviceBackgroundRect.topLeft());
            for (const RectF &shapeRect : blurShape.rects()) {
                const RectF deviceShapeRect = shapeRect.translated(-backgroundRect.topLeft()).scaled(viewport.scale()).rounded();
                if (const RectF intersected = deviceClipRect.intersected(deviceShapeRect); !intersected.isEmpty()) {
                    effectiveShape.append(intersected);
                }
            }
        }
    } else {
        for (const RectF &rect : blurShape.rects()) {
            effectiveShape.append(rect.translated(-backgroundRect.topLeft()).scaled(viewport.scale()).rounded());
        }
    }
    if (effectiveShape.isEmpty()) {
        return;
    }

    // Maybe reallocate offscreen render targets. Keep in mind that the first one contains
    // original background behind the window, it's not blurred.
    GLenum textureFormat = GL_RGBA8;
    if (renderTarget.texture()) {
        textureFormat = renderTarget.texture()->internalFormat();
    }

    if (renderInfo.framebuffers.size() != (m_iterationCount + 1) || renderInfo.textures[0]->size() != backgroundRect.size() || renderInfo.textures[0]->internalFormat() != textureFormat) {
        renderInfo.framebuffers.clear();
        renderInfo.textures.clear();

        glClearColor(0, 0, 0, 0);
        for (size_t i = 0; i <= m_iterationCount; ++i) {
            auto texture = GLTexture::allocate(textureFormat, backgroundRect.size() / (1 << i));
            if (!texture) {
                qCWarning(KWIN_LIQUIDGLASS) << "Failed to allocate an offscreen texture";
                return;
            }
            texture->setFilter(GL_LINEAR);
            texture->setWrapMode(GL_CLAMP_TO_EDGE);

            auto framebuffer = std::make_unique<GLFramebuffer>(texture.get());
            if (!framebuffer->valid()) {
                qCWarning(KWIN_LIQUIDGLASS) << "Failed to create an offscreen framebuffer";
                return;
            }
            EglContext::currentContext()->pushFramebuffer(framebuffer.get());
            glClear(GL_COLOR_BUFFER_BIT);
            EglContext::currentContext()->popFramebuffer();
            renderInfo.textures.push_back(std::move(texture));
            renderInfo.framebuffers.push_back(std::move(framebuffer));
        }
    }

    // Fetch the pixels behind the shape that is going to be blurred.
    const Region dirtyRegion = viewport.mapFromDeviceCoordinatesContained(deviceRegion) & backgroundRect;
    for (const Rect &dirtyRect : dirtyRegion.rects()) {
        renderInfo.framebuffers[0]->blitFromRenderTarget(renderTarget, viewport, dirtyRect, dirtyRect.translated(-backgroundRect.topLeft()));
    }

    // Upload the geometry: the first 6 vertices are used when downsampling and upsampling offscreen,
    // the remaining vertices are used when rendering on the screen.
    GLVertexBuffer *vbo = GLVertexBuffer::streamingBuffer();
    vbo->reset();
    vbo->setAttribLayout(std::span(GLVertexBuffer::GLVertex2DLayout), sizeof(GLVertex2D));

    const int vertexCount = effectiveShape.size() * 6;
    if (auto result = vbo->map<GLVertex2D>(6 + vertexCount)) {
        auto map = *result;

        size_t vboIndex = 0;

        // The geometry that will be blurred offscreen, in logical pixels.
        {
            const RectF localRect = RectF(0, 0, backgroundRect.width(), backgroundRect.height());

            const float x0 = localRect.left();
            const float y0 = localRect.top();
            const float x1 = localRect.right();
            const float y1 = localRect.bottom();

            const float u0 = x0 / backgroundRect.width();
            const float v0 = 1.0f - y0 / backgroundRect.height();
            const float u1 = x1 / backgroundRect.width();
            const float v1 = 1.0f - y1 / backgroundRect.height();

            // first triangle
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x0, y0),
                .texcoord = QVector2D(u0, v0),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x1, y1),
                .texcoord = QVector2D(u1, v1),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x0, y1),
                .texcoord = QVector2D(u0, v1),
            };

            // second triangle
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x0, y0),
                .texcoord = QVector2D(u0, v0),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x1, y0),
                .texcoord = QVector2D(u1, v0),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x1, y1),
                .texcoord = QVector2D(u1, v1),
            };
        }

        // The geometry that will be painted on screen, in device pixels.
        for (const RectF &rect : effectiveShape) {
            const float x0 = rect.left();
            const float y0 = rect.top();
            const float x1 = rect.right();
            const float y1 = rect.bottom();

            const float u0 = x0 / scaledBackgroundRect.width();
            const float v0 = 1.0f - y0 / scaledBackgroundRect.height();
            const float u1 = x1 / scaledBackgroundRect.width();
            const float v1 = 1.0f - y1 / scaledBackgroundRect.height();

            // first triangle
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x0, y0),
                .texcoord = QVector2D(u0, v0),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x1, y1),
                .texcoord = QVector2D(u1, v1),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x0, y1),
                .texcoord = QVector2D(u0, v1),
            };

            // second triangle
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x0, y0),
                .texcoord = QVector2D(u0, v0),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x1, y0),
                .texcoord = QVector2D(u1, v0),
            };
            map[vboIndex++] = GLVertex2D{
                .position = QVector2D(x1, y1),
                .texcoord = QVector2D(u1, v1),
            };
        }

        vbo->unmap();
    } else {
        qCWarning(KWIN_LIQUIDGLASS) << "Failed to map vertex buffer";
        return;
    }

    vbo->bindArrays();

    // The downsample pass of the dual Kawase algorithm: the background will be scaled down 50% every iteration.
    {
        ShaderManager::instance()->pushShader(m_downsamplePass.shader.get());

        QMatrix4x4 projectionMatrix;
        projectionMatrix.ortho(QRectF(0.0, 0.0, backgroundRect.width(), backgroundRect.height()));

        m_downsamplePass.shader->setUniform(m_downsamplePass.mvpMatrixLocation, projectionMatrix);
        m_downsamplePass.shader->setUniform(m_downsamplePass.offsetLocation, float(m_offset));

        for (size_t i = 1; i < renderInfo.framebuffers.size(); ++i) {
            const auto &read = renderInfo.framebuffers[i - 1];
            const auto &draw = renderInfo.framebuffers[i];

            const QVector2D halfpixel(0.5 / read->colorAttachment()->width(),
                                      0.5 / read->colorAttachment()->height());
            m_downsamplePass.shader->setUniform(m_downsamplePass.halfpixelLocation, halfpixel);

            read->colorAttachment()->bind();

            GLFramebuffer::pushFramebuffer(draw.get());
            vbo->draw(GL_TRIANGLES, 0, 6);
        }

        ShaderManager::instance()->popShader();
    }

    // The upsample pass of the dual Kawase algorithm: the background will be scaled up 200% every iteration.
    {
        ShaderManager::instance()->pushShader(m_upsamplePass.shader.get());

        QMatrix4x4 projectionMatrix;
        projectionMatrix.ortho(QRectF(0.0, 0.0, backgroundRect.width(), backgroundRect.height()));

        m_upsamplePass.shader->setUniform(m_upsamplePass.mvpMatrixLocation, projectionMatrix);
        m_upsamplePass.shader->setUniform(m_upsamplePass.offsetLocation, float(m_offset));

        for (size_t i = renderInfo.framebuffers.size() - 1; i > 1; --i) {
            GLFramebuffer::popFramebuffer();
            const auto &read = renderInfo.framebuffers[i];

            const QVector2D halfpixel(0.5 / read->colorAttachment()->width(),
                                      0.5 / read->colorAttachment()->height());
            m_upsamplePass.shader->setUniform(m_upsamplePass.halfpixelLocation, halfpixel);

            read->colorAttachment()->bind();

            vbo->draw(GL_TRIANGLES, 0, 6);
        }

        ShaderManager::instance()->popShader();
    }

    const float modulation = opacity * opacity;

    {
        ShaderManager::instance()->pushShader(m_glassPass.shader.get());

        QMatrix4x4 projectionMatrix = viewport.projectionMatrix();
        projectionMatrix.translate(scaledBackgroundRect.x(), scaledBackgroundRect.y());

        GLFramebuffer::popFramebuffer();
        const auto &read = renderInfo.framebuffers[1];

        const QVector2D halfpixel(0.5 / read->colorAttachment()->width(),
                                  0.5 / read->colorAttachment()->height());

        const qreal scale = viewport.scale();
        const RectF nativeBox = shapeBounds
                                    .scaled(scale)
                                    .rounded()
                                    .translated(-scaledBackgroundRect.topLeft());
        const BorderRadius windowRadius = w->window()->borderRadius();
        const bool ring = blurInfo.ring;
        const bool clip = ring || !windowRadius.isNull();
        const BorderRadius nativeCornerRadius = shapeRadius(w, ring).scaled(scale).rounded();

        const QPointF cursor = effects->cursorPos() * scale - QPointF(scaledBackgroundRect.topLeft());
        const QPointF motion = blurInfo.motion * scale;
        // pevné světlo shora zleva, při „dýchání“ pomalu krouží
        const float lightAngle = float(M_PI * 1.25 + (m_idleShimmer ? std::sin(m_clock.elapsed() / 1000.0 * 0.6) * 0.9 : 0.0));
        const QVector4D tint = m_darkTint ? QVector4D(0.05, 0.06, 0.08, m_tintStrength)
                                          : QVector4D(1.0, 1.0, 1.0, m_tintStrength);

        auto *shader = m_glassPass.shader.get();
        shader->setUniform(m_glassPass.mvpMatrixLocation, projectionMatrix);
        shader->setUniform(m_glassPass.colorMatrixLocation, m_colorMatrix);
        shader->setUniform(m_glassPass.halfpixelLocation, halfpixel);
        shader->setUniform(m_glassPass.offsetLocation, float(m_offset));
        shader->setUniform(m_glassPass.boxLocation, QVector4D(nativeBox.horizontalCenter(), nativeBox.verticalCenter(), nativeBox.width() * 0.5, nativeBox.height() * 0.5));
        shader->setUniform(m_glassPass.cornerRadiusLocation, nativeCornerRadius.toVector());
        shader->setUniform(m_glassPass.opacityLocation, modulation);
        shader->setUniform(m_glassPass.clipEnabledLocation, clip ? 1.0f : 0.0f);
        shader->setUniform(m_glassPass.texSizeLocation, QVector2D(scaledBackgroundRect.width(), scaledBackgroundRect.height()));
        shader->setUniform(m_glassPass.edgeWidthLocation, float(m_edgeWidth * scale));
        shader->setUniform(m_glassPass.refractionLocation, float(m_refraction * scale));
        shader->setUniform(m_glassPass.chromaLocation, m_chroma);
        shader->setUniform(m_glassPass.specularLocation, m_specular);
        shader->setUniform(m_glassPass.tintLocation, tint);
        shader->setUniform(m_glassPass.lightPosLocation, QVector2D(cursor));
        shader->setUniform(m_glassPass.lightOnLocation, m_mouseLight ? 1.0f : 0.0f);
        shader->setUniform(m_glassPass.motionLocation, QVector2D(motion));
        shader->setUniform(m_glassPass.lightAngleLocation, lightAngle);

        // rámeček: tvar okna (díra v rámečku) a ostré pozadí pro čiré sklo
        const RectF frameBox = w->frameGeometry()
                                   .scaled(scale)
                                   .rounded()
                                   .translated(-scaledBackgroundRect.topLeft());
        shader->setUniform(m_glassPass.frameBoxLocation, QVector4D(frameBox.horizontalCenter(), frameBox.verticalCenter(), frameBox.width() * 0.5, frameBox.height() * 0.5));
        shader->setUniform(m_glassPass.frameRadiusLocation, shapeRadius(w, false).scaled(scale).rounded().toVector());
        shader->setUniform(m_glassPass.hasRingLocation, ring ? 1.0f : 0.0f);
        shader->setUniform(m_glassPass.ringClarityLocation, m_ringClarity);
        shader->setUniform(m_glassPass.timeLocation, float(m_clock.elapsed() / 1000.0));
        shader->setUniform(m_glassPass.waveAmpLocation, float(m_waveStrength * 9.0 * blurInfo.wave * scale));
        shader->setUniform(m_glassPass.waveDirLocation, QVector2D(blurInfo.waveDir));
        shader->setUniform(m_glassPass.sharpTexLocation, 1);

        glActiveTexture(GL_TEXTURE1);
        renderInfo.textures[0]->bind();
        glActiveTexture(GL_TEXTURE0);
        read->colorAttachment()->bind();

        glEnable(GL_BLEND);
        glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);

        vbo->draw(GL_TRIANGLES, 6, vertexCount);

        glDisable(GL_BLEND);

        glActiveTexture(GL_TEXTURE1);
        renderInfo.textures[0]->unbind();
        glActiveTexture(GL_TEXTURE0);

        ShaderManager::instance()->popShader();
    }

    if (m_noiseStrength > 0) {
        // Apply an additive noise onto the blurred image. The noise is useful to mask banding
        // artifacts, which often happens due to the smooth color transitions in the blurred image.

        glEnable(GL_BLEND);
        if (opacity < 1.0) {
            glBlendFunc(GL_CONSTANT_ALPHA, GL_ONE);
        } else {
            glBlendFunc(GL_ONE, GL_ONE);
        }

        if (GLTexture *noiseTexture = ensureNoiseTexture()) {
            ShaderManager::instance()->pushShader(m_noisePass.shader.get());

            QMatrix4x4 projectionMatrix = viewport.projectionMatrix();
            projectionMatrix.translate(scaledBackgroundRect.x(), scaledBackgroundRect.y());

            m_noisePass.shader->setUniform(m_noisePass.mvpMatrixLocation, projectionMatrix);
            m_noisePass.shader->setUniform(m_noisePass.noiseTextureSizeLocation, QVector2D(noiseTexture->width(), noiseTexture->height()));

            noiseTexture->bind();

            vbo->draw(GL_TRIANGLES, 6, vertexCount);

            ShaderManager::instance()->popShader();
        }

        glDisable(GL_BLEND);
    }

    vbo->unbindArrays();
}

bool LiquidGlassEffect::isActive() const
{
    return m_valid && !effects->isScreenLocked();
}

bool LiquidGlassEffect::blocksDirectScanout() const
{
    return false;
}

} // namespace KWin

#include "moc_liquidglass.cpp"
