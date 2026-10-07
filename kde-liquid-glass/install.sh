#!/usr/bin/env bash
# Liquid Glass for KDE Plasma 6 on Arch Linux.
#
# Installs the required packages, builds the KWin effect (glass with light
# refraction, specular highlights and animation), enables it in place of the
# built-in Blur effect and sets up translucent apps (Kvantum) and panels.
#
#   ./install.sh                 everything
#   ./install.sh --dry-run       only print what would be done
#   ./install.sh --only effect   only packages + effect (no Kvantum, no panel)
#   ./install.sh --rebuild       after a KWin update: rebuild the effect
#   ./install.sh --uninstall     revert everything
#   ./install.sh --backup        only back up the current look
#   ./install.sh --restore [FILE]  restore the look from a backup (latest if no FILE)
#   ./install.sh --list-backups  list backups
#   ./install.sh --dark          dark glass and a dark app theme
#   ./install.sh --yes           no questions
#
# Run as a normal user (not root); sudo is requested when needed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/liquid-glass"
STATE_FILE="$STATE_DIR/state.env"
MANIFEST="$STATE_DIR/install_manifest.txt"
BUILD_DIR="${LG_BUILD_DIR:-$HOME/.cache/liquid-glass-build}"
BIN_DIR="$HOME/.local/bin"
KVANTUM_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Kvantum"
THEME=LiquidGlass
BACKUP_DIR="${LG_BACKUP_DIR:-$HOME/liquid-glass-backups}"
LEGACY_BACKUP_DIR="$HOME/liquid-glass-zalohy"   # backups from the Czech version
RESTORE_FILE=""

# What belongs to the desktop look (paths relative to $HOME). Only existing ones are backed up.
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
    kconfig kcoreaddons kwindowsystem kdecoration kcmutils
    libepoxy libdrm wayland libxcb vulkan-headers vulkan-icd-loader mesa
    kvantum
)

c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_err=$'\e[31m'; c_dim=$'\e[2m'; c_b=$'\e[1m'; c_0=$'\e[0m'
say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✔%s %s\n' "$c_ok" "$c_0" "$*"; }
warn() { printf '  %s!%s %s\n' "$c_warn" "$c_0" "$*"; }
die()  { printf '%s✘ %s%s\n' "$c_err" "$*" "$c_0" >&2; exit 1; }
head_() { printf '\n%s== %s ==%s\n' "$c_b" "$*" "$c_0"; }

# run a command, or only print it in --dry-run
run() {
    if (( DRY )); then printf '  %s[dry-run]%s %s\n' "$c_dim" "$c_0" "$*"; return 0; fi
    "$@"
}

ask() {  # ask "Question?" -> 0 = yes
    (( YES )) && return 0
    local a; read -r -p "  $1 [Y/n] " a || true
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
        *) die "Unknown option: $1 (see --help)" ;;
    esac
    shift
done

want() { [[ -z "$ONLY" || ",$ONLY," == *",$1,"* ]]; }

kwrite() { run kwriteconfig6 "$@"; }
kread()  { kreadconfig6 "$@" 2>/dev/null || true; }

save_state() {  # store the original value only the first time
    local key="$1" value="$2"
    (( DRY )) && return 0
    mkdir -p "$STATE_DIR"; touch "$STATE_FILE"
    grep -q "^$key=" "$STATE_FILE" || printf '%s=%q\n' "$key" "$value" >> "$STATE_FILE"
}

kwin_dbus() { qdbus6 org.kde.KWin "$@" 2>/dev/null || true; }

# ── checks ──────────────────────────────────────────────────────────────────
preflight() {
    head_ "System check"
    [[ $EUID -ne 0 ]] || die "Run as a normal user, not as root (the script asks for sudo itself)."
    command -v pacman >/dev/null || die "This is not Arch Linux (pacman is missing)."
    ok "Arch Linux"
    if command -v kwin_wayland >/dev/null; then
        local v; v="$(kwin_wayland --version 2>/dev/null | awk '{print $2}')"
        ok "KWin ${v:-?}"
        case "$v" in
            6.7.*|6.8.*|6.9.*) ;;
            "") warn "Could not determine the KWin version." ;;
            *) warn "The effect is written for KWin 6.7. It may not build on $v (the KWin API changes between versions)." ;;
        esac
    else
        warn "KWin is not installed yet, it will be installed."
    fi
    if [[ "${XDG_SESSION_TYPE:-}" != "wayland" ]]; then
        warn "Not a Wayland session (${XDG_SESSION_TYPE:-?}). The effect is tuned for Wayland; on X11 it is weaker."
    else
        ok "Wayland session"
    fi
}

# ── packages ────────────────────────────────────────────────────────────────
step_packages() {
    head_ "Packages"
    local missing=()
    for p in "${PACKAGES[@]}"; do
        pacman -Qq "$p" >/dev/null 2>&1 || missing+=("$p")
    done
    if (( ${#missing[@]} == 0 )); then ok "everything installed"; return; fi
    say "  missing: ${missing[*]}"
    run sudo pacman -S --needed --noconfirm "${missing[@]}"
    ok "packages installed"
}

# ── effect ──────────────────────────────────────────────────────────────────
step_effect() {
    head_ "Liquid Glass effect for KWin"
    run rm -rf "$BUILD_DIR"
    run cmake -S "$HERE/effect" -B "$BUILD_DIR" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
    run cmake --build "$BUILD_DIR" -j"$(nproc)"
    run sudo cmake --install "$BUILD_DIR"
    if (( ! DRY )); then
        mkdir -p "$STATE_DIR"
        cp "$BUILD_DIR/install_manifest.txt" "$MANIFEST"
        ok "installed: $(grep -m1 'liquidglass.so' "$MANIFEST" || echo '?')"
    fi

    save_state blurEnabled "$(kread --file kwinrc --group Plugins --key blurEnabled)"
    kwrite --file kwinrc --group Plugins --key blurEnabled false
    kwrite --file kwinrc --group Plugins --key liquidglassEnabled true

    # write defaults only on the first install (never overwrite user settings)
    if [[ -z "$(kread --file kwinrc --group Effect-liquidglass --key RingWidth)" ]]; then
        kwrite --file kwinrc --group Effect-liquidglass --key RingWidth 6
    fi
    (( DARK )) && kwrite --file kwinrc --group Effect-liquidglass --key DarkTint true

    run install -Dm755 "$HERE/liquid-glass" "$BIN_DIR/liquid-glass"
    ok "settings tool: $BIN_DIR/liquid-glass"
    ensure_path

    install_pacman_hook

    if (( ! DRY )) && pgrep -x kwin_wayland >/dev/null; then
        kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect blur >/dev/null
        kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect liquidglass >/dev/null
        kwin_dbus /Effects org.kde.kwin.Effects.loadEffect liquidglass >/dev/null
        if [[ "$(kwin_dbus /Effects org.kde.kwin.Effects.isEffectLoaded liquidglass)" == "true" ]]; then
            ok "effect is running"
        else
            warn "The effect has not loaded yet. Log out and back in; if it still does not load, see README (Troubleshooting)."
        fi
    fi
}

ensure_path() {
    # Arch does not have ~/.local/bin in PATH; add it to ~/.bashrc (and fish/zsh if present)
    [[ ":$PATH:" == *":$BIN_DIR:"* ]] && return 0
    local line='export PATH="$HOME/.local/bin:$PATH"'
    local rc
    for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
        [[ -f "$rc" || "$rc" == "$HOME/.bashrc" ]] || continue
        if ! grep -qs '\.local/bin' "$rc"; then
            if (( DRY )); then
                say "  ${c_dim}[dry-run]${c_0} would add to $rc: $line"
            else
                printf '\n# liquid-glass and other user commands\n%s\n' "$line" >> "$rc"
            fi
        fi
    done
    if command -v fish >/dev/null && [[ -d "$HOME/.config/fish" ]]; then
        run fish -c "fish_add_path -U $BIN_DIR"
    fi
    warn "$BIN_DIR was not in PATH; added for new terminals. In this terminal run: source ~/.bashrc"
}

install_pacman_hook() {
    # After a KWin update the built effect no longer matches the new version and
    # KWin will not load it (nothing breaks, the glass just disappears). The hook
    # reminds you to rebuild.
    local hook=/etc/pacman.d/hooks/liquid-glass-kwin.hook
    local tmp; tmp="$(mktemp)"
    cat > "$tmp" <<EOF
[Trigger]
Operation = Upgrade
Type = Package
Target = kwin

[Action]
Description = Liquid Glass: KWin was updated, run '$HERE/install.sh --rebuild'
When = PostTransaction
Exec = /usr/bin/true
EOF
    run sudo install -Dm644 "$tmp" "$hook"
    rm -f "$tmp"
    ok "reminder after KWin updates: $hook"
}

# ── Kvantum (translucent apps) ──────────────────────────────────────────────
step_kvantum() {
    head_ "App style (Kvantum)"
    local base="" candidates=(KvMojaveLight KvFlatLight KvRoughGlass KvFlat KvArc)
    (( DARK )) && candidates=(KvMojave KvRoughGlass KvFlat KvArcDark)
    for b in "${candidates[@]}"; do
        [[ -f "/usr/share/Kvantum/$b/$b.kvconfig" ]] && { base="$b"; break; }
    done
    [[ -n "$base" ]] || { warn "No base Kvantum theme found, step skipped."; return; }
    ok "base theme: $base"

    local dir="$KVANTUM_DIR/$THEME"
    run mkdir -p "$dir"
    run cp "/usr/share/Kvantum/$base/$base.svg" "$dir/$THEME.svg"
    if (( DRY )); then
        say "  ${c_dim}[dry-run]${c_0} $dir/$THEME.kvconfig = $base.kvconfig + translucency and blur"
    else
        python3 "$HERE/kvantum/make_theme.py" "/usr/share/Kvantum/$base/$base.kvconfig" "$HERE/kvantum/overrides.ini" "$dir/$THEME.kvconfig"
    fi
    run mkdir -p "$KVANTUM_DIR"
    save_state kvantumTheme "$(kread --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme)"
    kwrite --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme "$THEME"

    save_state widgetStyle "$(kread --file kdeglobals --group KDE --key widgetStyle)"
    kwrite --file kdeglobals --group KDE --key widgetStyle kvantum
    ok "app style: Kvantum / $THEME (applies to newly started apps)"
}

# ── panel ───────────────────────────────────────────────────────────────────
step_panel() {
    head_ "Plasma panel"
    if ! pgrep -x plasmashell >/dev/null; then warn "plasmashell is not running, step skipped."; return; fi
    ask "Make panels floating and translucent (restarts the panel)?" || { warn "skipped"; return; }
    local ids
    ids="$(qdbus6 org.kde.plasmashell /PlasmaShell org.kde.PlasmaShell.evaluateScript \
        'print(panels().map(function(p){ return p.id; }).join(" "))' 2>/dev/null || true)"
    [[ -n "$ids" ]] || { warn "Could not detect the panels."; return; }
    for id in $ids; do
        save_state "panelOpacity_$id" "$(kread --file plasmashellrc --group PlasmaViews --group "Panel $id" --key panelOpacity)"
        save_state "floating_$id" "$(kread --file plasmashellrc --group PlasmaViews --group "Panel $id" --key floating)"
        kwrite --file plasmashellrc --group PlasmaViews --group "Panel $id" --key panelOpacity 2
        kwrite --file plasmashellrc --group PlasmaViews --group "Panel $id" --key floating 1
    done
    run systemctl --user restart plasma-plasmashell.service
    ok "panels: floating, translucent"
}

# ── look backup ─────────────────────────────────────────────────────────────
step_backup() {  # step_backup [label]
    local label="${1:-before-install}"
    head_ "Backup of the current look"
    local items=() p
    for p in "${BACKUP_PATHS[@]}"; do
        [[ -e "$HOME/$p" ]] && items+=("$p")
    done
    if (( ${#items[@]} == 0 )); then warn "Nothing to back up."; return; fi
    local file
    file="$BACKUP_DIR/backup-$(date +%Y%m%d-%H%M%S)-$label.tar.gz"
    if (( DRY )); then
        say "  ${c_dim}[dry-run]${c_0} would back up to $file:"
        printf '      %s\n' "${items[@]}"
        return
    fi
    mkdir -p "$BACKUP_DIR"
    # description of the backup: what was set (for humans, not used by restore)
    local info; info="$(mktemp -d)"
    {
        echo "KDE look backup: $(date '+%Y-%m-%d %H:%M')"
        echo "KWin: $(kwin_wayland --version 2>/dev/null || echo '?')"
        echo "Global theme: $(kread --file kdeglobals --group KDE --key LookAndFeelPackage)"
        echo "Colors: $(kread --file kdeglobals --group General --key ColorScheme)"
        echo "App style: $(kread --file kdeglobals --group KDE --key widgetStyle)"
        echo "Icons: $(kread --file kdeglobals --group Icons --key Theme)"
        echo "Plasma style: $(kread --file plasmarc --group Theme --key name)"
        echo "Window decoration: $(kread --file kwinrc --group org.kde.kdecoration2 --key theme)"
        echo "Kvantum: $(kread --file "$KVANTUM_DIR/kvantum.kvconfig" --group General --key theme)"
        echo
        echo "Restore: ./install.sh --restore \"$file\""
    } > "$info/LIQUID-GLASS-BACKUP.txt"
    tar -czf "$file" -C "$HOME" "${items[@]}" -C "$info" LIQUID-GLASS-BACKUP.txt 2>/dev/null \
        || tar -czf "$file" -C "$HOME" "${items[@]}" -C "$info" LIQUID-GLASS-BACKUP.txt
    rm -rf "$info"
    ok "saved: $file ($(du -h "$file" | cut -f1))"
    say "  ${c_dim}restore: ./install.sh --restore${c_0}"
}

# all backups, newest first (including ones made by the Czech version)
all_backups() {
    ls -1t "$BACKUP_DIR"/backup-*.tar.gz "$LEGACY_BACKUP_DIR"/zaloha-*.tar.gz 2>/dev/null || true
}

list_backups() {
    head_ "Backups"
    local f found=0
    while IFS= read -r f; do
        [[ -n "$f" ]] || continue
        found=1
        printf '  %s  (%s)\n' "$f" "$(du -h "$f" | cut -f1)"
    done < <(all_backups)
    (( found )) || say "  none"
}

restore_backup() {
    local file="$RESTORE_FILE"
    if [[ -z "$file" ]]; then
        file="$(all_backups | grep -v -- '-before-restore\|-pred-obnovou' | head -n1 || true)"
    fi
    [[ -n "$file" && -f "$file" ]] || die "Backup not found (see ./install.sh --list-backups)."
    head_ "Restoring the look from a backup"
    { tar -xzf "$file" -O LIQUID-GLASS-BACKUP.txt 2>/dev/null || tar -xzf "$file" -O LIQUID-GLASS-ZALOHA.txt 2>/dev/null; } \
        | sed -n '1,9p' | sed 's/^/  /' || true
    ask "Restore the look from $(basename "$file")? The current settings are backed up first." \
        || { warn "cancelled"; return; }
    step_backup before-restore
    # take only paths from the list out of the backup (nothing outside the look)
    local members=() m p
    while IFS= read -r m; do
        m="${m#./}"; m="${m%/}"
        for p in "${BACKUP_PATHS[@]}"; do
            if [[ "$m" == "$p" ]]; then members+=("$m"); break; fi
        done
    done < <(tar -tzf "$file")
    (( ${#members[@]} )) || die "The backup contains no look files."
    # replace whole directories so nothing from the newer look stays behind
    for m in "${members[@]}"; do
        [[ -d "$HOME/$m" ]] && run rm -rf "$HOME/${m:?}"
    done
    run tar -xzf "$file" -C "$HOME" "${members[@]}"
    ok "restored: ${#members[@]} items"
    if (( ! DRY )); then
        kwin_dbus /KWin reconfigure >/dev/null
        if [[ "$(kread --file kwinrc --group Plugins --key liquidglassEnabled)" != "true" ]]; then
            kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect liquidglass >/dev/null
            [[ "$(kread --file kwinrc --group Plugins --key blurEnabled)" != "false" ]] \
                && kwin_dbus /Effects org.kde.kwin.Effects.loadEffect blur >/dev/null
        fi
        pgrep -x plasmashell >/dev/null && run systemctl --user restart plasma-plasmashell.service
    fi
    say "  Log out and back in for everything (app style, window decorations) to apply."
    [[ -f "$MANIFEST" ]] && say "  ${c_dim}The effect is still installed, just disabled; to remove it: ./install.sh --uninstall${c_0}"
    return 0
}

# ── uninstall ───────────────────────────────────────────────────────────────
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
    head_ "Uninstalling Liquid Glass"
    kwrite --file kwinrc --group Plugins --key liquidglassEnabled false
    restore blurEnabled --file kwinrc --group Plugins --key blurEnabled
    if pgrep -x kwin_wayland >/dev/null && (( ! DRY )); then
        kwin_dbus /Effects org.kde.kwin.Effects.unloadEffect liquidglass >/dev/null
        kwin_dbus /Effects org.kde.kwin.Effects.loadEffect blur >/dev/null
    fi
    if [[ -f "$MANIFEST" ]]; then
        while read -r f || [[ -n "$f" ]]; do [[ -n "$f" ]] && run sudo rm -f "$f"; done < "$MANIFEST"
        ok "effect files removed"
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
    ok "done; the effect settings in kwinrc [Effect-liquidglass] are kept in case you come back"
}

# ── main ────────────────────────────────────────────────────────────────────
(( DRY )) && say "${c_warn}Dry-run mode: nothing is installed or changed.${c_0}"
case "$MODE" in
    uninstall) uninstall ;;
    backup)    step_backup manual ;;
    list)      list_backups ;;
    restore)   restore_backup ;;
    rebuild)   preflight; step_packages; step_effect ;;
    install)
        preflight
        step_backup before-install
        want effect && step_packages
        want effect && step_effect
        want kvantum && step_kvantum
        want panel && step_panel
        head_ "Done"
        say "  Settings:  System Settings → Desktop Effects → Liquid Glass, or liquid-glass --help"
        say "  Restart apps with translucent backgrounds (or log out and back in)."
        ;;
esac
