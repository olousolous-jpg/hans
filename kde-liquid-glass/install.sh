#!/usr/bin/env bash
# Liquid Glass pro KDE Plasma 6 na Arch Linuxu.
#
# Nainstaluje potřebné balíčky, přeloží efekt pro KWin (sklo s lomem světla,
# leskem a animací), zapne ho místo vestavěného rozostření a nastaví
# průhledný vzhled aplikací (Kvantum) a panelu.
#
#   ./install.sh                 vše
#   ./install.sh --dry-run       jen vypíše, co by se dělo
#   ./install.sh --only effect   jen balíčky + efekt (bez Kvantum a panelu)
#   ./install.sh --rebuild       po aktualizaci KWinu: znovu přeložit efekt
#   ./install.sh --uninstall     vrátit vše zpět
#   ./install.sh --backup        jen zálohovat současný vzhled
#   ./install.sh --restore [SOUBOR]  obnovit vzhled ze zálohy (bez SOUBORU poslední)
#   ./install.sh --list-backups  vypsat zálohy
#   ./install.sh --dark          tmavé sklo a tmavý motiv aplikací
#   ./install.sh --yes           bez dotazů
#
# Spouští se jako běžný uživatel (ne root); sudo si řekne samo.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/liquid-glass"
STATE_FILE="$STATE_DIR/state.env"
MANIFEST="$STATE_DIR/install_manifest.txt"
BUILD_DIR="${LG_BUILD_DIR:-$HOME/.cache/liquid-glass-build}"
BIN_DIR="$HOME/.local/bin"
KVANTUM_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Kvantum"
THEME=LiquidGlass
BACKUP_DIR="${LG_BACKUP_DIR:-$HOME/liquid-glass-zalohy}"
RESTORE_FILE=""

# Co patří ke vzhledu plochy (cesty relativně k $HOME). Zálohují se jen ty, které existují.
BACKUP_PATHS=(
    .config/kwinrc .config/kdeglobals .config/plasmarc .config/plasmashellrc
    .config/plasma-org.kde.plasma.desktop-appletsrc .config/kdedefaults
    .config/breezerc .config/klassyrc .config/ksplashrc .config/kscreenlockerrc
    .config/kcminputrc .config/Trolltech.conf .config/Kvantum
    .config/gtk-3.0/settings.ini .config/gtk-4.0/settings.ini .config/xsettingsd
    .config/konsolerc .local/share/konsole
    .local/share/plasma .local/share/color-schemes .local/share/aurorae .local/share/icons
)

DRY=0; YES=0; DARK=0; MODE=install; ONLY=""

PACKAGES=(
    base-devel cmake extra-cmake-modules git python
    kwin qt6-base qt6-declarative qt6-tools
    kconfig kcoreaddons kwindowsystem kdecoration
    libepoxy libdrm wayland libxcb vulkan-headers vulkan-icd-loader mesa
    kvantum
)

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_b=$'\e[1m'; c_0=$'\e[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✔%s %s\n' "$c_ok" "$c_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_warn" "$c_0" "$*"; }
die()  { printf '%s✘ %s%s\n' "$c_err" "$*" "$c_0" >&2; exit 1; }
head_() { printf '\n%s== %s ==%s\n' "$c_b" "$*" "$c_0"; }

# spustí příkaz, nebo ho v --dry-run jen vypíše
run() {
    if (( DRY )); then printf '  %s[dry-run]%s %s\n' "$c_dim" "$c_0" "$*"; return 0; fi
    "$@"
}

ask() {  # ask "Otázka?" -> 0 = ano
    (( YES )) && return 0
    local a; read -r -p "  $1 [A/n] " a || true
    [[ -z "$a" || "$a" =~ ^[aAyY] ]]
}

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0; }

while (( $# )); do
    case "$1" in
        --dry-run) DRY=1 ;;
        --yes|-y) YES=1 ;;
        --dark) DARK=1 ;;
        --uninstall) MODE=uninstall ;;
        --rebuild) MODE=rebuild ;;
        --backup) MODE=backup ;;
        --list-backups) MODE=list ;;
        --restore)
            MODE=restore
            if [[ -n "${2:-}" && "${2:-}" != --* ]]; then RESTORE_FILE="$2"; shift; fi ;;
        --only) ONLY="${2:-}"; shift ;;
        -h|--help) usage ;;
        *) die "Neznámý přepínač: $1 (viz --help)" ;;
    esac
    shift
done

want() { [[ -z "$ONLY" || ",$ONLY," == *",$1,"* ]]; }

kwrite() { run kwriteconfig6 "$@"; }
kread()  { kreadconfig6 "$@" 2>/dev/null || true; }

save_state() {  # uloží původní hodnotu jen poprvé
    local key="$1" value="$2"
    (( DRY )) && return 0
    mkdir -p "$STATE_DIR"; touch "$STATE_FILE"
    grep -q "^$key=" "$STATE_FILE" || printf '%s=%q\n' "$key" "$value" >> "$STATE_FILE"
}

kwin_dbus() { qdbus6 org.kde.KWin "$@" 2>/dev/null || true; }

# ── kontroly ────────────────────────────────────────────────────────────────
preflight() {
    head_ "Kontrola systému"
    [[ $EUID -ne 0 ]] || die "Spusť jako běžný uživatel, ne jako root (sudo si skript řekne sám)."
    command -v pacman >/dev/null || die "Tohle není Arch Linux (chybí pacman)."
    ok "Arch Linux"
    if command -v kwin_wayland >/dev/null; then
        local v; v="$(kwin_wayland --version 2>/dev/null | awk '{print $2}')"
        ok "KWin ${v:-?}"
        case "$v" in
            6.7.*|6.8.*|6.9.*) ;;
            "") warn "Verzi KWinu se nepodařilo zjistit." ;;
            *) warn "Efekt je psaný pro KWin 6.7. Na verzi $v se nemusí přeložit (API KWinu se mezi verzemi mění)." ;;
        esac
    else
        warn "KWin zatím není nainstalovaný, doinstaluje se."
    fi
    if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]]; then
        warn "Nejsi v relaci Wayland (${XDG_SESSION_TYPE:-?}). Efekt je laděný pro Wayland, na X11 bude slabší."
    else
        ok "relace Wayland"
    fi
}

# ── balíčky ─────────────────────────────────────────────────────────────────
step_packages() {
    head_ "Balíčky"
    local missing=()
    for p in "${PACKAGES[@]}"; do
        pacman -Qq "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if (( ${#missing[@]} == 0 )); then ok "vše nainstalováno"; return; fi
    say "  chybí: ${missing[*]}"
    run sudo pacman -S --needed --noconfirm "${missing[@]}"
    ok "balíčky nainstalovány"
}

# ── efekt ───────────────────────────────────────────────────────────────────
step_effect() {
    head_ "Efekt Liquid Glass pro KWin"
    run rm -rf "$BUILD_DIR"
    run cmake -S "$HERE/effect" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
    run cmake --build "$BUILD_DIR" -j"$(nproc)"
    run sudo cmake --install "$BUILD_DIR"
    if (( ! DRY )); then
        mkdir -p "$STATE_DIR"
        cp "$BUILD_DIR/install_manifest.txt" "$MANIFEST"
        ok "nainstalováno: $(grep -m1 'liquidglass.so' "$MANIFEST" || echo '?')"
    fi

    save_state blurEnabled "$(kread --file kwinrc --group Plugins --key blurEnabled)"
    kwrite --file kwinrc --group Plugins --key blurEnabled false
    kwrite --file kwinrc --group Plugins --key liquidglassEnabled true

    # výchozí hodnoty zapsat jen při první instalaci (uživatelské nastavení nepřepisovat)
    if [[ -z "$(kread --file kwinrc --group Effect-liquidglass --key RingWidth)" ]]; then
        kwrite --file kwinrc --group Effect-liquidglass --key RingWidth 6
    fi
    (( DARK )) && kwrite --file kwinrc --group Effect-liquidglass --key DarkTint true

    run install -Dm755 "$HERE/liquid-glass" "$BIN_DIR/liquid-glass"
    ok "nástroj pro nastavení: $BIN_DIR/liquid-glass"

    install_pacman_hook

    if (( ! DRY )) && pgrep -x kwin_wayland >/dev/null; then
        kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect blur >/dev/null
        kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect liquidglass >/dev/null
        kwin_dbus /Effects org.kde.kwin.Effects.loadEffect liquidglass >/dev/null
        if [[ "$(kwin_dbus /Effects org.kde.kwin.Effects.isEffectLoaded liquidglass)" == "true" ]]; then
            ok "efekt běží"
        else
            warn "Efekt se zatím nenačetl. Odhlas se a přihlas; pokud ani pak, viz README (Řešení potíží)."
        fi
    fi
}

install_pacman_hook() {
    # Po aktualizaci KWinu přeložený efekt nesedí na novou verzi a KWin ho
    # nenačte (nic se nerozbije, jen zmizí sklo). Hook připomene přeložení.
    local hook=/etc/pacman.d/hooks/liquid-glass-kwin.hook
    local tmp; tmp="$(mktemp)"
    cat > "$tmp" <<EOF
[Trigger]
Operation = Upgrade
Type = Package
Target = kwin

[Action]
Description = Liquid Glass: KWin byl aktualizován, spusť '$HERE/install.sh --rebuild'
When = PostTransaction
Exec = /usr/bin/true
EOF
    run sudo install -Dm644 "$tmp" "$hook"
    rm -f "$tmp"
    ok "připomínka po aktualizaci KWinu: $hook"
}

# ── Kvantum (průhledné aplikace) ────────────────────────────────────────────
step_kvantum() {
    head_ "Vzhled aplikací (Kvantum)"
    local base="" candidates=(KvMojaveLight KvFlatLight KvRoughGlass KvFlat KvArc)
    (( DARK )) && candidates=(KvMojave KvRoughGlass KvFlat KvArcDark)
    for b in "${candidates[@]}"; do
        [[ -f "/usr/share/Kvantum/$b/$b.kvconfig" ]] && { base="$b"; break; }
    done
    [[ -n "$base" ]] || { warn "Nenalezen žádný základní motiv Kvantum, krok přeskočen."; return; }
    ok "základ motivu: $base"

    local dir="$KVANTUM_DIR/$THEME"
    run mkdir -p "$dir"
    run cp "/usr/share/Kvantum/$base/$base.svg" "$dir/$THEME.svg"
    if (( DRY )); then
        say "  ${c_dim}[dry-run]${c_0} $dir/$THEME.kvconfig = $base.kvconfig + průhlednost a rozostření"
    else
        python3 "$HERE/kvantum/make_theme.py" "/usr/share/Kvantum/$base/$base.kvconfig" "$HERE/kvantum/overrides.ini" "$dir/$THEME.kvconfig"
    fi
    run mkdir -p "$KVANTUM_DIR"
    save_state kvantumTheme "$(kread --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme)"
    kwrite --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme "$THEME"

    save_state widgetStyle "$(kread --file kdeglobals --group KDE --key widgetStyle)"
    kwrite --file kdeglobals --group KDE --key widgetStyle kvantum
    ok "styl aplikací: Kvantum / $THEME (projeví se v nově spuštěných aplikacích)"
}

# ── panel ───────────────────────────────────────────────────────────────────
step_panel() {
    head_ "Panel Plasmy"
    if ! pgrep -x plasmashell >/dev/null; then warn "plasmashell neběží, krok přeskočen."; return; fi
    ask "Nastavit panely jako plovoucí a průhledné (restartuje panel)?" || { warn "přeskočeno"; return; }
    local ids
    ids="$(qdbus6 org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript \
        'print(panels().map(function(p){ return p.id; }).join(" "))' 2>/dev/null || true)"
    [[ -n "$ids" ]] || { warn "Panely se nepodařilo zjistit."; return; }
    for id in $ids; do
        save_state "panelOpacity_$id" "$(kread --file plasmashellrc --group PlasmaViews --group "Panel $id" --key panelOpacity)"
        save_state "floating_$id" "$(kread --file plasmashellrc --group PlasmaViews --group "Panel $id" --key floating)"
        kwrite --file plasmashellrc --group PlasmaViews --group "Panel $id" --key panelOpacity 2
        kwrite --file plasmashellrc --group PlasmaViews --group "Panel $id" --key floating 1
    done
    run systemctl --user restart plasma-plasmashell.service
    ok "panely: plovoucí, průhledné"
}

# ── záloha vzhledu ──────────────────────────────────────────────────────────
step_backup() {  # step_backup [popis]
    local label="${1:-pred-instalaci}"
    head_ "Záloha současného vzhledu"
    local items=() p
    for p in "${BACKUP_PATHS[@]}"; do
        [[ -e "$HOME/$p" ]] && items+=("$p")
    done
    if (( ${#items[@]} == 0 )); then warn "Není co zálohovat."; return; fi
    local file
    file="$BACKUP_DIR/zaloha-$(date +%Y%m%d-%H%M%S)-$label.tar.gz"
    if (( DRY )); then
        say "  ${c_dim}[dry-run]${c_0} zálohoval bych do $file:"
        printf '      %s\n' "${items[@]}"
        return
    fi
    mkdir -p "$BACKUP_DIR"
    # popis zálohy: co bylo nastavené (pro člověka, při obnově se nepoužívá)
    local info; info="$(mktemp -d)"
    {
        echo "Záloha vzhledu KDE: $(date '+%d. %m. %Y %H:%M')"
        echo "KWin: $(kwin_wayland --version 2>/dev/null || echo '?')"
        echo "Globální motiv: $(kread --file kdeglobals --group KDE --key LookAndFeelPackage)"
        echo "Barvy: $(kread --file kdeglobals --group General --key ColorScheme)"
        echo "Styl aplikací: $(kread --file kdeglobals --group KDE --key widgetStyle)"
        echo "Ikony: $(kread --file kdeglobals --group Icons --key Theme)"
        echo "Motiv Plasmy: $(kread --file plasmarc --group Theme --key name)"
        echo "Dekorace oken: $(kread --file kwinrc --group org.kde.kdecoration2 --key theme)"
        echo "Kvantum: $(kread --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme)"
        echo
        echo "Obnova: ./install.sh --restore \"$file\""
    } > "$info/LIQUID-GLASS-ZALOHA.txt"
    tar -czf "$file" -C "$HOME" "${items[@]}" -C "$info" LIQUID-GLASS-ZALOHA.txt 2>/dev/null \
        || tar -czf "$file" -C "$HOME" "${items[@]}" -C "$info" LIQUID-GLASS-ZALOHA.txt
    rm -rf "$info"
    ok "uloženo: $file ($(du -h "$file" | cut -f1))"
    say "  ${c_dim}obnova: ./install.sh --restore${c_0}"
}

list_backups() {
    head_ "Zálohy v $BACKUP_DIR"
    local f found=0
    for f in "$BACKUP_DIR"/zaloha-*.tar.gz; do
        [[ -e "$f" ]] || continue
        found=1
        printf '  %s  (%s)\n' "$(basename "$f")" "$(du -h "$f" | cut -f1)"
    done
    (( found )) || say "  žádné"
}

restore_backup() {
    local file="$RESTORE_FILE"
    if [[ -z "$file" ]]; then
        file="$(ls -1t "$BACKUP_DIR"/zaloha-*.tar.gz 2>/dev/null | grep -v -- '-pred-obnovou' | head -n1 || true)"
    fi
    [[ -n "$file" && -f "$file" ]] || die "Záloha nenalezena (viz ./install.sh --list-backups)."
    head_ "Obnova vzhledu ze zálohy"
    tar -xzf "$file" -O LIQUID-GLASS-ZALOHA.txt 2>/dev/null | sed -n '1,9p' | sed 's/^/  /' || true
    ask "Obnovit vzhled z $(basename "$file")? Současné nastavení se předtím taky zazálohuje." \
        || { warn "zrušeno"; return; }
    step_backup pred-obnovou
    # z obnovované zálohy vzít jen cesty ze seznamu (nic mimo vzhled)
    local members=() m p
    while IFS= read -r m; do
        m="${m#./}"; m="${m%/}"
        for p in "${BACKUP_PATHS[@]}"; do
            if [[ "$m" == "$p" ]]; then members+=("$m"); break; fi
        done
    done < <(tar -tzf "$file")
    (( ${#members[@]} )) || die "V záloze nejsou žádné soubory vzhledu."
    # adresáře nahradit celé, aby v nich nezůstalo nic z novějšího vzhledu
    for m in "${members[@]}"; do
        [[ -d "$HOME/$m" ]] && run rm -rf "$HOME/${m:?}"
    done
    run tar -xzf "$file" -C "$HOME" "${members[@]}"
    ok "obnoveno: ${#members[@]} položek"
    if (( ! DRY )); then
        kwin_dbus /KWin reconfigure >/dev/null
        if [[ "$(kread --file kwinrc --group Plugins --key liquidglassEnabled)" != "true" ]]; then
            kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect liquidglass >/dev/null
            [[ "$(kread --file kwinrc --group Plugins --key blurEnabled)" != "false" ]] \
                && kwin_dbus /Effects org.kde.kwin.Effects.loadEffect blur >/dev/null
        fi
        pgrep -x plasmashell >/dev/null && run systemctl --user restart plasma-plasmashell.service
    fi
    say "  Pro úplné projevení (styl aplikací, dekorace oken) se odhlas a přihlas."
    [[ -f "$MANIFEST" ]] && say "  ${c_dim}Efekt zůstal nainstalovaný, jen je vypnutý; úplně odstranit: ./install.sh --uninstall${c_0}"
    return 0
}

# ── odinstalace ─────────────────────────────────────────────────────────────
restore() {  # restore KEY file group... key
    local key="$1"; shift
    local val=""
    [[ -f "$STATE_FILE" ]] || return 0
    grep -q "^$key=" "$STATE_FILE" || return 0
    val="$(bash -c "source '$STATE_FILE'; printf '%s' \"\${$key}\"")"
    if [[ -z "$val" ]]; then
        kwrite "$@" --delete
    else
        kwrite "$@" "$val"
    fi
}

uninstall() {
    head_ "Odinstalace Liquid Glass"
    kwrite --file kwinrc --group Plugins --key liquidglassEnabled false
    restore blurEnabled --file kwinrc --group Plugins --key blurEnabled
    if pgrep -x kwin_wayland >/dev/null && (( ! DRY )); then
        kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect liquidglass >/dev/null
        kwin_dbus /Effects org.kde.kwin.Effects.loadEffect blur >/dev/null
    fi
    if [[ -f "$MANIFEST" ]]; then
        while read -r f || [[ -n "$f" ]]; do [[ -n "$f" ]] && run sudo rm -f "$f"; done < "$MANIFEST"
        ok "soubory efektu odstraněny"
    fi
    restore widgetStyle --file kdeglobals --group KDE --key widgetStyle
    restore kvantumTheme --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme
    run rm -rf "$KVANTUM_DIR/$THEME"
    if [[ -f "$STATE_FILE" ]]; then
        local restart_panel=0
        for key in $(grep -o '^panelOpacity_[^=]*\|^floating_[^=]*' "$STATE_FILE"); do
            local id="${key#*_}" name="${key%%_*}"
            restore "$key" --file plasmashellrc --group PlasmaViews --group "Panel $id" --key "$name"
            restart_panel=1
        done
        (( restart_panel )) && pgrep -x plasmashell >/dev/null && run systemctl --user restart plasma-plasmashell.service
    fi
    run sudo rm -f /etc/pacman.d/hooks/liquid-glass-kwin.hook
    run rm -f "$BIN_DIR/liquid-glass"
    run rm -rf "$BUILD_DIR"
    run rm -rf "$STATE_DIR"
    ok "hotovo; nastavení efektu v kwinrc [Effect-liquidglass] zůstalo pro případ návratu"
}

# ── hlavní ──────────────────────────────────────────────────────────────────
(( DRY )) && say "${c_warn}Režim dry-run: nic se neinstaluje ani nemění.${c_0}"
case "$MODE" in
    uninstall) uninstall ;;
    backup)    step_backup rucni ;;
    list)      list_backups ;;
    restore)   restore_backup ;;
    rebuild)   preflight; step_packages; step_effect ;;
    install)
        preflight
        step_backup pred-instalaci
        want effect && step_packages
        want effect && step_effect
        want kvantum && step_kvantum
        want panel && step_panel
        head_ "Hotovo"
        say "  Nastavení:  liquid-glass show | liquid-glass preset silne | liquid-glass --help"
        say "  Aplikace s průhledným pozadím spusť znovu (nebo se odhlas a přihlas)."
        ;;
esac
