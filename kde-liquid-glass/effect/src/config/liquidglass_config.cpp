/*
    Liquid Glass – nastavení efektu v Nastavení systému (Efekty plochy).

    Prvky s názvem kcfg_<Klíč> spravuje KConfigDialogManager automaticky
    (načtení, uložení, výchozí hodnoty) podle liquidglass.kcfg. Průhlednost
    aplikací se zapisuje zvlášť do motivu Kvantum LiquidGlass.

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
#include <QSlider>
#include <QStandardPaths>
#include <QVBoxLayout>

K_PLUGIN_CLASS(KWin::LiquidGlassEffectConfig)

namespace KWin
{

namespace
{

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
    l->setEnabled(false); // šedý text
    return l;
}

// Motiv Kvantum se upravuje po řádcích, zbytek souboru zůstane, jak je.
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
        {QStringLiteral("iPhone (čiré sklo, silná čočka)"),
         {{QStringLiteral("BlurStrength"), V(4)}, {QStringLiteral("RingWidth"), V(10)},
          {QStringLiteral("RingClarity"), V(90)}, {QStringLiteral("Refraction"), V(28)},
          {QStringLiteral("EdgeWidth"), V(30)}, {QStringLiteral("ChromaticAberration"), V(25)},
          {QStringLiteral("Specular"), V(80)}, {QStringLiteral("TintStrength"), V(4)},
          {QStringLiteral("WaveStrength"), V(60)}}},
        {QStringLiteral("Jemné"),
         {{QStringLiteral("Refraction"), V(10)}, {QStringLiteral("EdgeWidth"), V(18)},
          {QStringLiteral("ChromaticAberration"), V(15)}, {QStringLiteral("Specular"), V(35)},
          {QStringLiteral("MotionStrength"), V(30)}}},
        {QStringLiteral("Silné"),
         {{QStringLiteral("Refraction"), V(32)}, {QStringLiteral("EdgeWidth"), V(36)},
          {QStringLiteral("ChromaticAberration"), V(60)}, {QStringLiteral("Specular"), V(75)},
          {QStringLiteral("MotionStrength"), V(80)}, {QStringLiteral("RingWidth"), V(8)}}},
        {QStringLiteral("Tmavé (kouřové) sklo"),
         {{QStringLiteral("DarkTint"), V(true)}, {QStringLiteral("TintStrength"), V(22)}}},
        {QStringLiteral("Světlé (mléčné) sklo"),
         {{QStringLiteral("DarkTint"), V(false)}, {QStringLiteral("TintStrength"), V(8)}}},
    };
}

} // namespace

LiquidGlassEffectConfig::LiquidGlassEffectConfig(QObject *parent, const KPluginMetaData &data)
    : KCModule(parent, data)
{
    LiquidGlassConfig::instance(QStringLiteral("kwinrc"));

    // obsah je delší, proto v posuvné oblasti
    auto *outer = new QVBoxLayout(widget());
    outer->setContentsMargins(0, 0, 0, 0);
    auto *scroll = new QScrollArea;
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    auto *page = new QWidget;
    auto *col = new QVBoxLayout(page);
    scroll->setWidget(page);
    outer->addWidget(scroll);
    widget()->setMinimumSize(560, 720);

    const auto group = [col](const QString &title) {
        auto *g = new QGroupBox(title);
        auto *form = new QFormLayout(g);
        form->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
        col->addWidget(g);
        return form;
    };
    const QString px = QStringLiteral(" px");
    const QString pct = QStringLiteral(" %");

    // ── předvolby
    {
        auto *form = group(QStringLiteral("Předvolba"));
        m_preset = new QComboBox;
        m_preset->addItem(QStringLiteral("— vyber předvolbu —"));
        for (const Preset &p : presets()) {
            m_preset->addItem(p.name);
        }
        m_preset->addItem(QStringLiteral("Výchozí hodnoty"));
        connect(m_preset, &QComboBox::activated, this, &LiquidGlassEffectConfig::applyPreset);
        form->addRow(QStringLiteral("Použít:"), m_preset);
        form->addRow(hint(QStringLiteral("Předvolba jen nastaví hodnoty níže; uloží se tlačítkem Použít.")));
    }

    // ── sklo
    {
        auto *form = group(QStringLiteral("Sklo"));
        form->addRow(QStringLiteral("Rozmazání:"), slider(QStringLiteral("BlurStrength"), 1, 15, QString()));
        form->addRow(QStringLiteral("Sytost barev:"), slider(QStringLiteral("Saturation"), 50, 250, pct));
        form->addRow(QStringLiteral("Lom na hraně:"), slider(QStringLiteral("Refraction"), 0, 64, px));
        form->addRow(QStringLiteral("Šířka čočky u hrany:"), slider(QStringLiteral("EdgeWidth"), 4, 80, px));
        form->addRow(QStringLiteral("Barevný rozptyl:"), slider(QStringLiteral("ChromaticAberration"), 0, 100, pct));
        form->addRow(QStringLiteral("Lesk:"), slider(QStringLiteral("Specular"), 0, 100, pct));
        form->addRow(QStringLiteral("Tón skla:"), slider(QStringLiteral("TintStrength"), 0, 100, pct));
        form->addRow(QString(), check(QStringLiteral("DarkTint"), QStringLiteral("Tmavé (kouřové) sklo místo mléčného")));
        form->addRow(QStringLiteral("Šum proti pruhům:"), slider(QStringLiteral("NoiseStrength"), 0, 14, QString()));
        form->addRow(QStringLiteral("Zaoblení (výchozí):"), slider(QStringLiteral("CornerRadius"), 0, 40, px));
    }

    // ── rámeček kolem oken
    {
        auto *form = group(QStringLiteral("Skleněný rámeček kolem oken"));
        form->addRow(QStringLiteral("Šířka:"), slider(QStringLiteral("RingWidth"), 0, 40, px));
        form->addRow(hint(QStringLiteral("0 = bez rámečku. Rámeček je i kolem neprůhledných aplikací (Firefox, GTK).")));
        form->addRow(QStringLiteral("Čirost:"), slider(QStringLiteral("RingClarity"), 0, 100, pct));
        form->addRow(QStringLiteral("Bez rámečku:"), line(QStringLiteral("RingExcludeClasses"), QStringLiteral("např. steam, firefox")));
    }

    // ── animace
    {
        auto *form = group(QStringLiteral("Animace"));
        form->addRow(QString(), check(QStringLiteral("MouseLight"), QStringLiteral("Lesk na hraně sleduje kurzor")));
        form->addRow(QString(), check(QStringLiteral("LiquidMotion"), QStringLiteral("Setrvačnost skla při přesouvání okna")));
        form->addRow(QStringLiteral("Síla setrvačnosti:"), slider(QStringLiteral("MotionStrength"), 0, 100, pct));
        form->addRow(QStringLiteral("Vlnění při přesouvání:"), slider(QStringLiteral("WaveStrength"), 0, 100, pct));
        form->addRow(QString(), check(QStringLiteral("IdleShimmer"), QStringLiteral("Světlo pomalu „dýchá“ (stále překresluje)")));
    }

    // ── ostatní
    {
        auto *form = group(QStringLiteral("Aplikace"));
        form->addRow(QStringLiteral("Sklo přes celé okno:"), line(QStringLiteral("ForceGlassClasses"), QStringLiteral("třídy oken oddělené čárkou")));
        form->addRow(hint(QStringLiteral("Smysl má jen u aplikací s průhledným pozadím.")));
    }

    // ── Kvantum
    {
        auto *form = group(QStringLiteral("Průhlednost aplikací (Kvantum)"));
        form->addRow(QStringLiteral("Okna:"), slider(QString(), 0, 90, pct, &m_windowOpacity));
        form->addRow(QStringLiteral("Nabídky:"), slider(QString(), 0, 90, pct, &m_menuOpacity));
        form->addRow(hint(QStringLiteral("Projeví se v nově spuštěných aplikacích; nabídka na ploše po restartu Plasmy nebo novém přihlášení.")));
        const bool haveTheme = QFileInfo::exists(kvantumThemeFile());
        for (QSlider *s : {m_windowOpacity, m_menuOpacity}) {
            s->setEnabled(haveTheme);
            connect(s, &QSlider::valueChanged, this, [this]() {
                setNeedsSave(true);
            });
        }
        if (!haveTheme) {
            form->addRow(hint(QStringLiteral("Motiv Kvantum LiquidGlass není nainstalovaný (install.sh --only kvantum).")));
        }
    }

    col->addStretch(1);

    addConfig(LiquidGlassConfig::self(), page);
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
