# Liquid Glass pro KDE Plasma 6 (Arch Linux)

Vzhled „tekutého skla“ jako v novém macOS/iOS: pozadí za průhlednými okny,
panelem a nabídkami je rozmazané, **na hranách se láme jako přes čočku**, hrana
má **lesk**, který **sleduje kurzor**, a při posouvání okna se sklo
**setrvačností „přelije“**. Kolem běžných (neprůhledných) oken je navíc tenký
**skleněný rámeček**, takže sklo je vidět u všech aplikací, i u GTK.

![náhled](docs/nahled.png)

*Náhled je vykreslený tím samým shaderem nad zkušebním obrázkem
(`tools/preview.py`), ne snímek skutečné plochy.* Vlevo nahoře je průhledné okno,
vpravo nahoře neprůhledné okno se skleněným rámečkem, vlevo dole lesk od kurzoru
a vpravo dole setrvačnost při posunu okna.

## Co je uvnitř

| Část | Co dělá |
|---|---|
| `effect/` | efekt pro KWin v C++ a GLSL, upravená kopie vestavěného rozostření z KWinu 6.7.5 |
| `kvantum/` | motiv Kvantum `LiquidGlass`: průhledná okna a nabídky Qt/KDE aplikací (základ KvMojaveLight, resp. KvMojave) |
| `install.sh` | instalace balíčků, překlad, zapnutí, vzhled, odinstalace |
| `liquid-glass` | nástroj pro nastavení z příkazové řádky |
| `tools/preview.py` | náhled shaderu bez KWinu (ladění vzhledu) |

## Instalace

```bash
./install.sh --dry-run     # nejdřív se podívat, co se bude dít
./install.sh               # instalace (sudo si řekne samo)
./install.sh --dark        # tmavá varianta
```

Skript udělá tohle:
0. **Zazálohuje současný vzhled** do `~/liquid-glass-zalohy/` (viz níže).
1. Doinstaluje balíčky: `base-devel cmake extra-cmake-modules kwin qt6-base kconfig kvantum …`.
2. Přeloží efekt a nainstaluje ho do `/usr/lib/qt6/plugins/kwin/effects/plugins/`.
3. Vypne vestavěný efekt *Rozostření* a zapne *Tekuté sklo*.
4. Vytvoří motiv Kvantum `LiquidGlass` a nastaví ho jako styl aplikací.
5. Po dotazu nastaví panely jako plovoucí a průhledné (restartuje panel).
6. Přidá pacman hook, který po aktualizaci KWinu připomene přeložení.

Původní hodnoty si uloží do `~/.local/share/liquid-glass/state.env` a
`./install.sh --uninstall` je vrátí.

Části jdou spustit i zvlášť: `./install.sh --only effect`, `--only kvantum`,
`--only panel`.

### Záloha a obnova vzhledu

Před každou instalací se uloží záloha
`~/liquid-glass-zalohy/zaloha-DATUM-pred-instalaci.tar.gz`. Obsahuje nastavení
vzhledu: `kwinrc`, `kdeglobals`, `plasmarc`, `plasmashellrc`, rozložení plochy
a panelů, Kvantum, dekorace Breeze/Klassy, profily Konsole, nastavení GTK a
uživatelské motivy, barvy a ikony v `~/.local/share`. V souboru
`LIQUID-GLASS-ZALOHA.txt` uvnitř je zapsáno, jaký motiv byl nastavený.

```bash
./install.sh --backup                 # zálohovat kdykoli ručně
./install.sh --list-backups           # vypsat zálohy
./install.sh --restore                # obnovit z poslední zálohy
./install.sh --restore ~/liquid-glass-zalohy/zaloha-….tar.gz
```

Obnova před přepsáním zazálohuje i současný stav (`…-pred-obnovou`), takže se
dá vrátit. Obnovují se jen soubory ze seznamu vzhledu, nic jiného. Po obnově se
odhlas a přihlas.

### Po aktualizaci KWinu

KWin načte jen efekt přeložený přesně pro jeho verzi. Po aktualizaci Plasmy tedy
sklo zmizí, ale nic se nerozbije. Vrátí ho:

```bash
./install.sh --rebuild
```

Pak se odhlas a přihlas.

## Nastavení

```bash
liquid-glass show                  # aktuální hodnoty
liquid-glass preset iphone         # iphone | jemne | vychozi | silne | bez-ramecku | tmave | svetle
liquid-glass set Refraction 28     # jedna hodnota, projeví se hned
liquid-glass off / on              # vrátit vestavěné rozostření / zapnout sklo
```

| Klíč | Výchozí | Význam |
|---|---|---|
| `BlurStrength` | 10 | síla rozmazání (1–15) |
| `Saturation` | 160 | sytost barev za sklem (%) |
| `NoiseStrength` | 3 | jemný šum proti pruhování |
| `Refraction` | 20 | o kolik px se pozadí na hraně „ohne“ |
| `EdgeWidth` | 26 | šířka pásu u hrany, kde se světlo láme (px) |
| `ChromaticAberration` | 35 | barevný rozptyl na hraně (0–100) |
| `Specular` | 55 | síla lesku (0–100) |
| `TintStrength` / `DarkTint` | 8 / false | mléčný (světlý) nebo kouřový (tmavý) tón |
| `RingWidth` | 6 | skleněný rámeček kolem běžných oken (px, 0 = vypnuto) |
| `RingClarity` | 70 | jak čiré je sklo rámečku (0 = mléčné, 100 = skoro ostré pozadí) |
| `WaveStrength` | 60 | vlnění pozadí pod sklem při přesouvání okna (0 = vypnuto) |
| `RingExcludeClasses` | – | okna bez rámečku, např. `steam,firefox` |
| `ForceGlassClasses` | – | sklo přes celé okno pro dané aplikace (smysl má jen u průhledných) |
| `CornerRadius` | 10 | zaoblení, když ho okno samo neudává |
| `MouseLight` | true | lesk sleduje kurzor |
| `LiquidMotion` / `MotionStrength` | true / 50 | setrvačnost skla při posunu okna |
| `IdleShimmer` | false | světlo pomalu „dýchá“ (stále překresluje, víc odběru) |

Hodnoty jsou v `~/.config/kwinrc` ve skupině `[Effect-liquidglass]`.

### Tipy

- **Konsole:** v profilu *Vzhled → Průhlednost* nastav 15–25 % a zapni
  *Rozostřit pozadí*. Konsole si pak sklo vyžádá sama.
- **Firefox, Chrome, GTK aplikace** mají vlastní neprůhledné pozadí, uvnitř okna
  sklo nebude. Kolem nich je ale skleněný rámeček.
- **Čitelnost:** pokud je text na skle hůř čitelný, zvyš `BlurStrength` nebo
  `TintStrength`, případně v `~/.config/Kvantum/LiquidGlass/LiquidGlass.kvconfig`
  sniž `reduce_window_opacity`.
- **Stíny:** rámeček leží pod stínem okna. Pro výraznější sklo zmenši stín
  v *Nastavení → Vzhled → Dekorace oken → Breeze → Stíny*.

## Řešení potíží

| Potíž | Co zkusit |
|---|---|
| `liquid-glass status` hlásí, že efekt neběží | odhlásit a přihlásit; po aktualizaci KWinu `./install.sh --rebuild` |
| překlad selže (chyba v `liquidglass.cpp`) | KWin je jiné verze, než pro jakou je efekt psaný (6.7); viz níže |
| sklo „bliká“ nebo je cítit zpomalení | `liquid-glass preset bez-ramecku`, `IdleShimmer false`, nižší `BlurStrength` |
| nic nepomáhá | `liquid-glass off` vrátí vestavěné rozostření |

Logy efektu se zobrazí příkazem:
```bash
journalctl --user -b | grep -i liquidglass
```

## Jak to funguje

Efekt vychází z vestavěného rozostření KWinu (algoritmus dual Kawase). Mění se
poslední průchod, který kreslí na obrazovku (`effect/src/shaders/glass.frag`):

- **Lom:** vzdálenost od zaoblené hrany se počítá vzdálenostním polem
  (`sdfRoundedBox`). V pásu u hrany se pozadí čte posunuté ve směru normály
  hrany, takže obsah zpoza okraje se „ohne“ do skla.
- **Barevný rozptyl:** červený a modrý kanál se čtou s mírně jiným posunem.
- **Lesk:** tenká světlá linka na hraně s pevným světlem shora zleva, k tomu
  odlesk, který sílí u kurzoru.
- **Setrvačnost:** při posunu okna se ukládá vektor pohybu, který posune čtení
  pozadí proti směru pohybu a pak plynule odezní (asi 90 ms).
- **Rámeček kolem okna:** k oblasti skla se přidá pruh šířky `RingWidth` kolem
  rámu okna. Pozadí se zachytává s okrajem navíc, aby lom měl co číst.

Shader se dá ladit bez KWinu:

```bash
pip install moderngl pillow numpy
python3 tools/preview.py --refraction 28 --chroma 0.6
```

## Stav a omezení

- Efekt je přeložený a ověřený proti hlavičkám **KWinu 6.7.5** (stejná verze
  jako na Archu k 2. 10. 2026). Shader se překládá a vykresluje (Mesa,
  GLSL 1.40). Instalace a odinstalace jsou vyzkoušené naostro v kontejneru, jen
  pacman byl nahrazený atrapou.
- **Na skutečné ploše Plasmy to vyzkoušené není.** V kontejneru není grafický
  čip, takže KWin nešel spustit s OpenGL. Je možné, že bude potřeba doladit
  výchozí hodnoty nebo opravit drobnosti v kreslení rámečku.
- Plasma 6.8 a novější: API KWinu se mezi verzemi mění. Pokud se efekt
  nepřeloží, je potřeba ho upravit podle `src/plugins/blur` z dané verze KWinu
  (změny bývají malé).
- Nastavení panelu (`panelOpacity`, `floating` v `plasmashellrc`) je dělané
  podle současné Plasmy. Kdyby nezabralo, nastav panel ručně v režimu úprav.
- Na X11 efekt funguje, ale je laděný pro Wayland.

## Licence

GPL-2.0-or-later. Efekt je odvozený z KWinu (© autoři KWinu, viz hlavičky
souborů).
