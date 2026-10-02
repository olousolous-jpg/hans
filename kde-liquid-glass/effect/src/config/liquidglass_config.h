/*
    Liquid Glass – nastavení efektu v Nastavení systému (Efekty plochy).

    SPDX-License-Identifier: GPL-2.0-or-later
*/

#pragma once

#include <KCModule>

class QComboBox;
class QSlider;

namespace KWin
{

class LiquidGlassEffectConfig : public KCModule
{
    Q_OBJECT

public:
    explicit LiquidGlassEffectConfig(QObject *parent, const KPluginMetaData &data);

    void load() override;
    void save() override;
    void defaults() override;

private:
    void applyPreset(int index);
    QString kvantumThemeFile() const;

    QComboBox *m_preset = nullptr;
    // průhlednost aplikací (motiv Kvantum LiquidGlass), mimo kwinrc
    QSlider *m_windowOpacity = nullptr;
    QSlider *m_menuOpacity = nullptr;
};

} // namespace KWin
