# shellcheck shell=bash
# Společné funkce instalátoru. Načítá se přes `source` z install.sh a pc/install_pc.sh.
#
# Proměnné, které volající může nastavit:
#   DRY_RUN=1    — nic nespouštět, jen vypsat, co by se udělalo
#   ASSUME_YES=1 — na otázky ano/ne odpovídat výchozí hodnotou (neinteraktivně)
#   LOG_FILE     — kam se zrcadlí výstup spouštěných příkazů

if [ -t 1 ]; then
    C_RED=$'\e[31m'; C_GRN=$'\e[32m'; C_YEL=$'\e[33m'; C_BLU=$'\e[34m'
    C_BLD=$'\e[1m'; C_DIM=$'\e[2m'; C_RST=$'\e[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_BLD=""; C_DIM=""; C_RST=""
fi

DRY_RUN="${DRY_RUN:-0}"
ASSUME_YES="${ASSUME_YES:-0}"
LOG_FILE="${LOG_FILE:-/dev/null}"

say()   { printf '%s\n' "$*"; }
info()  { printf '%s  %s%s\n' "$C_BLU" "$*" "$C_RST"; }
ok()    { printf '%s  ✓ %s%s\n' "$C_GRN" "$*" "$C_RST"; }
warn()  { printf '%s  ⚠ %s%s\n' "$C_YEL" "$*" "$C_RST" >&2; }
err()   { printf '%s  ✗ %s%s\n' "$C_RED" "$*" "$C_RST" >&2; }
die()   { err "$*"; exit 1; }

header() {
    printf '\n%s━━ %s %s%s\n' "$C_BLD" "$1" "$(printf '━%.0s' $(seq 1 $((60 - ${#1}))))" "$C_RST"
}

# run CMD… — spustí příkaz (v dry-run jen vypíše). Výstup jde i do LOG_FILE.
run() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '%s  [dry-run] %s%s\n' "$C_DIM" "$*" "$C_RST"
        return 0
    fi
    printf '%s  $ %s%s\n' "$C_DIM" "$*" "$C_RST"
    printf '\n$ %s\n' "$*" >>"$LOG_FILE"
    "$@" 2>&1 | tee -a "$LOG_FILE"
    return "${PIPESTATUS[0]}"
}

# run_sh "řetězec" — totéž pro příkaz s rourou/přesměrováním.
run_sh() {
    if [ "$DRY_RUN" = "1" ]; then
        printf '%s  [dry-run] %s%s\n' "$C_DIM" "$1" "$C_RST"
        return 0
    fi
    printf '%s  $ %s%s\n' "$C_DIM" "$1" "$C_RST"
    printf '\n$ %s\n' "$1" >>"$LOG_FILE"
    bash -c "$1" 2>&1 | tee -a "$LOG_FILE"
    return "${PIPESTATUS[0]}"
}

# confirm "Otázka" [a|n] — vrací 0 pro ano. Výchozí odpověď druhým argumentem.
confirm() {
    local q="$1" def="${2:-a}" hint ans
    if [ "$def" = "a" ]; then hint="[A/n]"; else hint="[a/N]"; fi
    if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        [ "$def" = "a" ]; return
    fi
    read -r -p "  $q $hint: " ans || ans=""
    ans="${ans,,}"
    [ -z "$ans" ] && ans="$def"
    case "$ans" in a|ano|y|yes) return 0 ;; *) return 1 ;; esac
}

# ask "Otázka" "výchozí" — vypíše odpověď na stdout.
ask() {
    local q="$1" def="${2:-}" ans
    if [ "$ASSUME_YES" = "1" ] || [ ! -t 0 ]; then
        printf '%s' "$def"; return
    fi
    if [ -n "$def" ]; then
        read -r -p "  $q [$def]: " ans || ans=""
    else
        read -r -p "  $q: " ans || ans=""
    fi
    printf '%s' "${ans:-$def}"
}

have() { command -v "$1" >/dev/null 2>&1; }

# http_ok URL — 0, když URL odpoví (jakýmkoli HTTP kódem < 500) do 4 s.
http_ok() {
    local code
    code="$(curl -s -o /dev/null -m 4 -w '%{http_code}' "$1" 2>/dev/null || true)"
    [ -n "$code" ] && [ "$code" != "000" ] && [ "$code" -lt 500 ]
}

# ── Stav instalace (aby šel instalátor pouštět opakovaně) ─────────────────────
# Soubor KEY=VALUE; kroky se značí done_<krok>=1.
STATE_FILE="${STATE_FILE:-}"

state_get() {
    [ -n "$STATE_FILE" ] && [ -f "$STATE_FILE" ] || return 0
    grep -E "^$1=" "$STATE_FILE" | tail -n1 | cut -d= -f2-
}

state_set() {
    [ -n "$STATE_FILE" ] || return 0
    [ "$DRY_RUN" = "1" ] && return 0
    mkdir -p "$(dirname "$STATE_FILE")"
    touch "$STATE_FILE"
    grep -vE "^$1=" "$STATE_FILE" >"$STATE_FILE.tmp" || true
    printf '%s=%s\n' "$1" "$2" >>"$STATE_FILE.tmp"
    mv "$STATE_FILE.tmp" "$STATE_FILE"
}
