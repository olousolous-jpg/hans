/*
    Liquid Glass – effect settings in System Settings (Desktop Effects).

    Widgets named kcfg_<Key> are managed by KConfigDialogManager automatically
    (load, save, defaults) according to liquidglass.kcfg. App translucency is
    written separately into the Kvantum theme LiquidGlass.

    SPDX-License-Identifier: GPL-2.0-or-later
*/

#include "liquidglass_config.h"

// KConfigSkeleton
#include "liquidglassconfig.h"

#include <KPluginFactory>

#include <QCheckBox>
#include <QComboBox>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QScrollArea>
#include <QTabWidget>
#include <QWheelEvent>
#include <QSlider>
#include <QStandardPaths>
#include <QApplication>
#include <QVBoxLayout>

K_PLUGIN_CLASS(KWin::LiquidGlassEffectConfig)

namespace KWin
{

namespace
{

// The mouse wheel over a slider without focus should scroll the page, not
// change the value. The wheel changes the value only after clicking the slider.
class WheelGuard : public QObject
{
public:
    using QObject::QObject;
    bool eventFilter(QObject *watched, QEvent *event) override
    {
        if (event->type() == QEvent::Wheel) {
            if (auto *w = qobject_cast<QWidget *>(watched); w && !w->hasFocus()) {
                event->ignore(); // propagates to the parent (scroll area)
                return true;
            }
        }
        return false;
    }
};

QWidget *slider(const QString &key, int min, int max, const QString &suffix, QSlider **out = nullptr)
{
    auto *box = new QWidget;
    auto *row = new QHBoxLayout(box);
    row->setContentsMargins(0, 0, 0, 0);
    auto *s = new QSlider(Qt::Horizontal);
    if (!key.isEmpty()) {
        s->setObjectName(QStringLiteral("kcfg_") + key);
    }
    s->setRange(min, max);
    s->setPageStep(std::max(1, (max - min) / 10));
    s->setFocusPolicy(Qt::StrongFocus);
    static WheelGuard *guard = new WheelGuard(qApp);
    s->installEventFilter(guard);
    auto *value = new QLabel;
    value->setMinimumWidth(value->fontMetrics().horizontalAdvance(QStringLiteral("100 px")));
    value->setAlignment(Qt::AlignRight | Qt::AlignVCenter);
    const auto update = [value, suffix](int v) {
        value->setText(QString::number(v) + suffix);
    };
    QObject::connect(s, &QSlider::valueChanged, value, update);
    update(s->value());
    row->addWidget(s, 1);
    row->addWidget(value);
    if (out) {
        *out = s;
    }
    return box;
}

QCheckBox *check(const QString &key, const QString &text)
{
    auto *c = new QCheckBox(text);
    c->setObjectName(QStringLiteral("kcfg_") + key);
    return c;
}

QLineEdit *line(const QString &key, const QString &placeholder)
{
    auto *e = new QLineEdit;
    e->setObjectName(QStringLiteral("kcfg_") + key);
    e->setPlaceholderText(placeholder);
    return e;
}

QLabel *hint(const QString &text)
{
    auto *l = new QLabel(text);
    l->setWordWrap(true);
    QFont f = l->font();
    f.setPointSizeF(f.pointSizeF() * 0.9);
    l->setFont(f);
    l->setEnabled(false); // grey text
    return l;
}

// The Kvantum theme is edited line by line, the rest of the file stays as it is.
int readKvantum(const QString &path, const QString &key, int fallback)
{
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return fallback;
    }
    const QString text = QString::fromUtf8(f.readAll());
    const QRegularExpression re(QStringLiteral("^%1\\s*=\\s*(\\d+)").arg(key), QRegularExpression::MultilineOption);
    const auto m = re.match(text);
    return m.hasMatch() ? m.captured(1).toInt() : fallback;
}

void writeKvantum(const QString &path, const QString &key, int value)
{
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return;
    }
    QString text = QString::fromUtf8(f.readAll());
    f.close();
    const QString entry = QStringLiteral("%1=%2").arg(key).arg(value);
    const QRegularExpression re(QStringLiteral("^%1\\s*=.*$").arg(key), QRegularExpression::MultilineOption);
    if (text.contains(re)) {
        text.replace(re, entry);
    } else {
        const QString section = QStringLiteral("[%General]");
        const int at = text.indexOf(section);
        if (at >= 0) {
            text.insert(at + section.size(), QLatin1Char('\n') + entry);
        } else {
            text.prepend(section + QLatin1Char('\n') + entry + QLatin1Char('\n'));
        }
    }
    if (f.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        f.write(text.toUtf8());
    }
}

struct Preset
{
    QString name;
    QList<QPair<QString, QVariant>> values;
};

QList<Preset> presets()
{
    using V = QVariant;
    return {
        {QStringLiteral("iPhone (clear glass, strong lens)"),
         {{QStringLiteral("BlurStrength"), V(4)}, {QStringLiteral("RingWidth"), V(10)},
          {QStringLiteral("RingClarity"), V(90)}, {QStringLiteral("Refraction"), V(28)},
          {QStringLiteral("EdgeWidth"), V(30)}, {QStringLiteral("ChromaticAberration"), V(25)},
          {QStringLiteral("Specular"), V(80)}, {QStringLiteral("TintStrength"), V(4)},
          {QStringLiteral("WaveStrength"), V(60)}}},
        {QStringLiteral("Subtle"),
         {{QStringLiteral("Refraction"), V(10)}, {QStringLiteral("EdgeWidth"), V(18)},
          {QStringLiteral("ChromaticAberration"), V(15)}, {QStringLiteral("Specular"), V(35)},
          {QStringLiteral("MotionStrength"), V(30)}}},
        {QStringLiteral("Strong"),
         {{QStringLiteral("Refraction"), V(32)}, {QStringLiteral("EdgeWidth"), V(36)},
          {QStringLiteral("ChromaticAberration"), V(60)}, {QStringLiteral("Specular"), V(75)},
          {QStringLiteral("MotionStrength"), V(80)}, {QStringLiteral("RingWidth"), V(8)}}},
        {QStringLiteral("Dark (smoky) glass"),
         {{QStringLiteral("DarkTint"), V(true)}, {QStringLiteral("TintStrength"), V(22)}}},
        {QStringLiteral("Light (frosted) glass"),
         {{QStringLiteral("DarkTint"), V(false)}, {QStringLiteral("TintStrength"), V(8)}}},
    };
}

} // namespace

LiquidGlassEffectConfig::LiquidGlassEffectConfig(QObject *parent, const KPluginMetaData &data)
    : KCModule(parent, data)
{
    LiquidGlassConfig::instance(QStringLiteral("kwinrc"));

    // preset on top, tabs below; each tab fits without scrolling
    auto *outer = new QVBoxLayout(widget());
    outer->setContentsMargins(0, 0, 0, 0);
    auto *top = new QWidget;
    auto *topLayout = new QVBoxLayout(top);
    topLayout->setContentsMargins(0, 0, 0, 0);
    outer->addWidget(top);
    auto *tabs = new QTabWidget;
    outer->addWidget(tabs, 1);
    widget()->setMinimumWidth(520);

    QVBoxLayout *col = topLayout;
    const auto newTab = [tabs, &col](const QString &title) {
        auto *scroll = new QScrollArea;
        scroll->setWidgetResizable(true);
        scroll->setFrameShape(QFrame::NoFrame);
        auto *page = new QWidget;
        col = new QVBoxLayout(page);
        scroll->setWidget(page);
        tabs->addTab(scroll, title);
    };
    const auto group = [&col](const QString &title) {
        auto *g = new QGroupBox(title);
        auto *form = new QFormLayout(g);
        form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
        col->addWidget(g);
        return form;
    };
    const auto finishTab = [&col]() {
        col->addStretch(1);
    };
    const QString px = QStringLiteral(" px");
    const QString pct = QStringLiteral(" %");

    // ── presets
    {
        auto *form = group(QStringLiteral("Preset"));
        m_preset = new QComboBox;
        m_preset->addItem(QStringLiteral("— choose a preset —"));
        for (const Preset &p : presets()) {
            m_preset->addItem(p.name);
        }
        m_preset->addItem(QStringLiteral("Default values"));
        connect(m_preset, &QComboBox::activated, this, &LiquidGlassEffectConfig::applyPreset);
        form->addRow(QStringLiteral("Load:"), m_preset);
        form->addRow(hint(QStringLiteral("A preset only sets the values below; they are saved with the Apply button.")));
    }

    // ── glass
    newTab(QStringLiteral("Glass"));
    {
        auto *form = group(QStringLiteral("Glass"));
        form->addRow(QStringLiteral("Blur:"), slider(QStringLiteral("BlurStrength"), 1, 15, QString()));
        form->addRow(QStringLiteral("Saturation:"), slider(QStringLiteral("Saturation"), 50, 250, pct));
        form->addRow(QStringLiteral("Edge refraction:"), slider(QStringLiteral("Refraction"), 0, 64, px));
        form->addRow(QStringLiteral("Lens width at the edge:"), slider(QStringLiteral("EdgeWidth"), 4, 80, px));
        form->addRow(QStringLiteral("Chromatic dispersion:"), slider(QStringLiteral("ChromaticAberration"), 0, 100, pct));
        form->addRow(QStringLiteral("Specular:"), slider(QStringLiteral("Specular"), 0, 100, pct));
        form->addRow(QStringLiteral("Glass tint:"), slider(QStringLiteral("TintStrength"), 0, 100, pct));
        form->addRow(QString(), check(QStringLiteral("DarkTint"), QStringLiteral("Dark (smoky) glass instead of frosted")));
        form->addRow(QStringLiteral("Anti-banding noise:"), slider(QStringLiteral("NoiseStrength"), 0, 14, QString()));
        form->addRow(QStringLiteral("Corner radius (fallback):"), slider(QStringLiteral("CornerRadius"), 0, 40, px));
    }

    finishTab();

    // ── rim around windows
    newTab(QStringLiteral("Rim"));
    {
        auto *form = group(QStringLiteral("Glass rim around windows"));
        form->addRow(QStringLiteral("Width:"), slider(QStringLiteral("RingWidth"), 0, 40, px));
        form->addRow(hint(QStringLiteral("0 = no rim. The rim is also drawn around opaque apps (Firefox, GTK).")));
        form->addRow(QStringLiteral("Clarity:"), slider(QStringLiteral("RingClarity"), 0, 100, pct));
        form->addRow(QString(), check(QStringLiteral("BorderMoves"), QStringLiteral("Dragging a window border moves the window (corners still resize)")));
        form->addRow(QStringLiteral("No rim for:"), line(QStringLiteral("RingExcludeClasses"), QStringLiteral("e.g. steam, firefox")));
    }

    finishTab();

    // ── animation
    newTab(QStringLiteral("Animation"));
    {
        auto *form = group(QStringLiteral("Animation"));
        form->addRow(QString(), check(QStringLiteral("MouseLight"), QStringLiteral("Edge highlight follows the cursor")));
        form->addRow(QString(), check(QStringLiteral("LiquidMotion"), QStringLiteral("Glass inertia while moving a window")));
        form->addRow(QStringLiteral("Inertia strength:"), slider(QStringLiteral("MotionStrength"), 0, 100, pct));
        form->addRow(QStringLiteral("Ripples while moving:"), slider(QStringLiteral("WaveStrength"), 0, 100, pct));
        form->addRow(QString(), check(QStringLiteral("IdleShimmer"), QStringLiteral("Light slowly \"breathes\" (repaints continuously)")));
    }

    finishTab();

    // ── other
    newTab(QStringLiteral("Apps"));
    {
        auto *form = group(QStringLiteral("Apps"));
        form->addRow(QStringLiteral("Glass behind whole window:"), line(QStringLiteral("ForceGlassClasses"), QStringLiteral("comma-separated window classes")));
        form->addRow(hint(QStringLiteral("Only useful for apps with a translucent background.")));
    }

    // ── Kvantum
    {
        auto *form = group(QStringLiteral("App translucency (Kvantum)"));
        form->addRow(QStringLiteral("Windows:"), slider(QString(), 0, 90, pct, &m_windowOpacity));
        form->addRow(QStringLiteral("Menus:"), slider(QString(), 0, 90, pct, &m_menuOpacity));
        form->addRow(hint(QStringLiteral("Applies to newly started apps; the desktop menu after restarting Plasma or logging in again.")));
        const bool haveTheme = QFileInfo::exists(kvantumThemeFile());
        for (QSlider *s : {m_windowOpacity, m_menuOpacity}) {
            s->setEnabled(haveTheme);
            connect(s, &QSlider::valueChanged, this, [this]() {
                setNeedsSave(true);
            });
        }
        if (!haveTheme) {
            form->addRow(hint(QStringLiteral("The Kvantum theme LiquidGlass is not installed (install.sh --only kvantum).")));
        }
    }

    finishTab();

    addConfig(LiquidGlassConfig::self(), widget());
}

QString LiquidGlassEffectConfig::kvantumThemeFile() const
{
    return QStandardPaths::writableLocation(QStandardPaths::GenericConfigLocation)
        + QStringLiteral("/Kvantum/LiquidGlass/LiquidGlass.kvconfig");
}

void LiquidGlassEffectConfig::applyPreset(int index)
{
    const QList<Preset> list = presets();
    if (index <= 0) {
        return;
    }
    if (index == list.size() + 1) {
        KCModule::defaults();
    } else {
        for (const auto &[key, value] : list.at(index - 1).values) {
            const QString name = QStringLiteral("kcfg_") + key;
            if (auto *s = widget()->findChild<QSlider *>(name)) {
                s->setValue(value.toInt());
            } else if (auto *c = widget()->findChild<QCheckBox *>(name)) {
                c->setChecked(value.toBool());
            }
        }
    }
    m_preset->setCurrentIndex(0);
}

void LiquidGlassEffectConfig::load()
{
    KCModule::load();
    const QString path = kvantumThemeFile();
    if (QFileInfo::exists(path)) {
        m_windowOpacity->setValue(readKvantum(path, QStringLiteral("reduce_window_opacity"), 18));
        m_menuOpacity->setValue(readKvantum(path, QStringLiteral("reduce_menu_opacity"), 22));
    }
    setNeedsSave(false);
}

void LiquidGlassEffectConfig::save()
{
    KCModule::save();

    const QString path = kvantumThemeFile();
    if (QFileInfo::exists(path)) {
        writeKvantum(path, QStringLiteral("reduce_window_opacity"), m_windowOpacity->value());
        writeKvantum(path, QStringLiteral("reduce_menu_opacity"), m_menuOpacity->value());
    }

    QDBusMessage msg = QDBusMessage::createMethodCall(QStringLiteral("org.kde.KWin"),
                                                      QStringLiteral("/Effects"),
                                                      QStringLiteral("org.kde.kwin.Effects"),
                                                      QStringLiteral("reconfigureEffect"));
    msg << QStringLiteral("liquidglass");
    QDBusConnection::sessionBus().send(msg);
}

void LiquidGlassEffectConfig::defaults()
{
    KCModule::defaults();
    m_windowOpacity->setValue(18);
    m_menuOpacity->setValue(22);
}

} // namespace KWin

#include "liquidglass_config.moc"
#include "moc_liquidglass_config.cpp"
