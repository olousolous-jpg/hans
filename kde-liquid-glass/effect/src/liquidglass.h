/*
    Liquid Glass – efekt pro KWin 6.7.

    Based on KWin's built-in blur effect (src/plugins/blur, v6.7.5):
    SPDX-FileCopyrightText: 2010 Fredrik Höglund <fredrik@kde.org>
    SPDX-FileCopyrightText: 2018 Alex Nemeth <alex.nemeth329@gmail.com>

    SPDX-License-Identifier: GPL-2.0-or-later
*/

#pragma once

#include "effect/effect.h"
#include "opengl/glutils.h"
#include "scene/borderradius.h"
#include "scene/item.h"

#include <QElapsedTimer>
#include <QList>
#include <QStringList>
#include <QTimer>

#include <unordered_map>

namespace KWin
{

class BackgroundEffectItem;
class BorderMoveFilter;
class Window;

struct GlassRenderData
{
    /// Temporary render targets needed for the Dual Kawase algorithm, the first texture
    /// contains not blurred background behind the window, it's cached.
    std::vector<std::unique_ptr<GLTexture>> textures;
    std::vector<std::unique_ptr<GLFramebuffer>> framebuffers;
};

struct GlassWindowData
{
    /// The region that should be blurred behind the window
    std::optional<RegionF> content;

    /// The region that should be blurred behind the frame
    std::optional<RegionF> frame;

    /// Glass rim around the window frame (normal windows and dialogs)
    bool ring = false;

    /// Liquid motion: lagging offset in logical px, decays over time
    QPointF motion;

    /// Ripples under the glass while the window moves: strength 0..1 (fades slowly) and direction
    qreal wave = 0;
    QPointF waveDir = QPointF(1, 0);

    std::unordered_map<RenderView *, GlassRenderData> render;

    std::unique_ptr<BackgroundEffectItem> blurItem;
};

class LiquidGlassEffect : public KWin::Effect
{
    Q_OBJECT

public:
    LiquidGlassEffect();
    ~LiquidGlassEffect() override;

    static bool supported();
    static bool enabledByDefault();

    void reconfigure(ReconfigureFlags flags) override;
    void prePaintScreen(ScreenPrePaintData &data) override;
    void drawWindow(const RenderTarget &renderTarget, const RenderViewport &viewport, EffectWindow *w, int mask, const Region &deviceRegion, WindowPaintData &data) override;

    bool provides(Feature feature) override;
    bool isActive() const override;

    int requestedEffectChainPosition() const override
    {
        return 20;
    }

    bool eventFilter(QObject *watched, QEvent *event) override;

    bool blocksDirectScanout() const override;

public Q_SLOTS:
    void slotWindowAdded(KWin::EffectWindow *w);
    void slotWindowDeleted(KWin::EffectWindow *w);
    void slotViewRemoved(KWin::RenderView *view);
#if KWIN_BUILD_X11
    void slotPropertyNotify(KWin::EffectWindow *w, long atom);
#endif
    void setupDecorationConnections(KWin::EffectWindow *w);

private:
    void initBlurStrengthValues();
    RegionF blurRegion(EffectWindow *w) const;
    RegionF decorationBlurRegion(const EffectWindow *w) const;
    bool decorationSupportsBlurBehind(const EffectWindow *w) const;
    bool shouldBlur(const EffectWindow *w, int mask, const WindowPaintData &data) const;
    void updateBlurRegion(EffectWindow *w);
    void blur(const RenderTarget &renderTarget, const RenderViewport &viewport, EffectWindow *w, int mask, const Region &deviceRegion, WindowPaintData &data);
    GLTexture *ensureNoiseTexture();

    // Liquid Glass
    bool wantsRing(const EffectWindow *w) const;
    bool forcesGlass(const EffectWindow *w) const;
    BorderRadius shapeRadius(const EffectWindow *w, bool ring) const;
    int sampleMargin() const;
    RectF glassBounds(EffectWindow *w) const;
    void updateItemGeometry(EffectWindow *w);
    void slotFrameGeometryChanged(KWin::EffectWindow *w, const KWin::RectF &oldGeometry);
    void slotMouseChanged(const QPointF &pos, const QPointF &oldpos);
    void repaintGlass(EffectWindow *w);

private:
    struct
    {
        std::unique_ptr<GLShader> shader;
        int mvpMatrixLocation;
        int colorMatrixLocation;
        int offsetLocation;
        int halfpixelLocation;
        int boxLocation;
        int cornerRadiusLocation;
        int opacityLocation;
        int clipEnabledLocation;
        int texSizeLocation;
        int edgeWidthLocation;
        int refractionLocation;
        int chromaLocation;
        int specularLocation;
        int tintLocation;
        int lightPosLocation;
        int lightOnLocation;
        int motionLocation;
        int lightAngleLocation;
        int sharpTexLocation;
        int frameBoxLocation;
        int frameRadiusLocation;
        int hasRingLocation;
        int ringClarityLocation;
        int timeLocation;
        int waveAmpLocation;
        int waveDirLocation;
    } m_glassPass;

    struct
    {
        std::unique_ptr<GLShader> shader;
        int mvpMatrixLocation;
        int offsetLocation;
        int halfpixelLocation;
    } m_downsamplePass;

    struct
    {
        std::unique_ptr<GLShader> shader;
        int mvpMatrixLocation;
        int offsetLocation;
        int halfpixelLocation;
    } m_upsamplePass;

    struct
    {
        std::unique_ptr<GLShader> shader;
        int mvpMatrixLocation;
        int noiseTextureSizeLocation;

        std::unique_ptr<GLTexture> noiseTexture;
        qreal noiseTextureScale = 1.0;
        int noiseTextureStength = 0;
    } m_noisePass;

    bool m_valid = false;
#if KWIN_BUILD_X11
    long net_wm_blur_region = 0;
#endif
    RenderView *m_currentView = nullptr;

    QMatrix4x4 m_colorMatrix;
    size_t m_iterationCount; // number of times the texture will be downsized to half size
    int m_offset;
    int m_expandSize;
    int m_noiseStrength;

    // Liquid Glass settings (logical px or 0..1)
    int m_edgeWidth = 26;
    int m_refraction = 20;
    float m_chroma = 0.35f;
    float m_specular = 0.55f;
    float m_tintStrength = 0.08f;
    bool m_darkTint = false;
    int m_cornerRadius = 10;
    int m_ringWidth = 6;
    QStringList m_ringExclude;
    QStringList m_forceGlass;
    bool m_mouseLight = true;
    bool m_liquidMotion = true;
    float m_motionStrength = 0.5f;
    bool m_idleShimmer = false;
    float m_ringClarity = 0.7f;
    float m_waveStrength = 0.6f;

    // dragging a window border (not a corner) moves the window instead of resizing it
    bool m_borderMoves = true;
    std::unique_ptr<BorderMoveFilter> m_borderFilter;

public:
    bool borderMoves() const
    {
        return m_borderMoves;
    }
    /// the window whose glass rim (drawn outside the window) is under pos
    Window *ringWindowAt(const QPointF &pos) const;

private:

    QElapsedTimer m_clock;
    qint64 m_lastFrameMs = 0;
    QTimer m_shimmerTimer;

    struct OffsetStruct
    {
        float minOffset;
        float maxOffset;
        int expandSize;
    };

    QList<OffsetStruct> blurOffsets;

    struct BlurValuesStruct
    {
        int iteration;
        float offset;
    };

    QList<BlurValuesStruct> blurStrengthValues;

    QMap<EffectWindow *, QMetaObject::Connection> windowBlurChangedConnections;
    std::unordered_map<EffectWindow *, GlassWindowData> m_windows;
};

inline bool LiquidGlassEffect::provides(Effect::Feature feature)
{
    if (feature == Blur) {
        return true;
    }
    return KWin::Effect::provides(feature);
}

} // namespace KWin
