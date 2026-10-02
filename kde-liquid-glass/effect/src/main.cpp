/*
    SPDX-FileCopyrightText: 2021 Vlad Zahorodnii <vlad.zahorodnii@kde.org>

    SPDX-License-Identifier: GPL-2.0-or-later
*/

#include "liquidglass.h"

namespace KWin
{

KWIN_EFFECT_FACTORY_SUPPORTED_ENABLED(LiquidGlassEffect,
                                      "metadata.json",
                                      return LiquidGlassEffect::supported();
                                      ,
                                      return LiquidGlassEffect::enabledByDefault();)

} // namespace KWin

#include "main.moc"
