#!/bin/bash
# installer/test_install.sh — bezpečná zkouška instalátoru, i na Pi, kde Hans běží.
#
#   bash installer/test_install.sh               # interaktivní průchod, falešný LLM
#   bash installer/test_install.sh --yes         # rychlý průchod s výchozími odpověďmi
#   bash installer/test_install.sh --live-llm    # generovat skutečnou Ollamou na PC
#   bash installer/test_install.sh --keep        # nechat výsledky v data/installer/dryrun/
#   bash installer/test_install.sh --no-unit     # přeskočit automatické testy
#
# Co dělá:
#   1. syntaxe skriptů + automatické testy (installer/tests)
#   2. otisk souborů, na které instalace sahá (config, služba, .venv, stav)
#   3. celý install.sh v režimu --dry-run --fresh (jako na novém zařízení)
#      — ve výchozím stavu s FALEŠNÝM jazykovým modelem: PC se nezatěžuje a
#        texty persony jsou ukázkové (zahradnice), ať zadáš cokoli
#   4. kontrola výsledného configu z dry-run (jde načíst, nic citlivého ve
#      veřejné části, persona se sestaví) a že se na zařízení NIC nezměnilo
set -uo pipefail

INSTALLER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$INSTALLER_DIR")"
DRY="$ROOT/data/installer/dryrun"
# shellcheck source=lib/common.sh
source "$INSTALLER_DIR/lib/common.sh"

YES=0; LIVE=0; KEEP=0; UNIT=1
while [ $# -gt 0 ]; do
    case "$1" in
        --yes|-y) YES=1 ;;
        --live-llm) LIVE=1 ;;
        --keep) KEEP=1 ;;
        --no-unit) UNIT=0 ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Neznámý přepínač: $1 (viz --help)" ;;
    esac
    shift
done

FAILED=0
fail() { err "$*"; FAILED=1; }
PY=python3

# ── 1. syntaxe a automatické testy ──────────────────────────────────────────
header "1/4 Syntaxe a automatické testy"
for f in install.sh test_install.sh pc/install_pc.sh lib/common.sh; do
    bash -n "$INSTALLER_DIR/$f" && ok "bash -n $f" || fail "syntaktická chyba v $f"
done
for f in "$INSTALLER_DIR"/wizard.py "$INSTALLER_DIR"/hans_setup/*.py; do
    "$PY" -m py_compile "$f" 2>/dev/null || fail "Python chyba v ${f#"$ROOT"/}"
done
ok "Python soubory se přeloží"
if [ "$UNIT" = "1" ]; then
    # testy pracují na dočasné kopii repozitáře, skutečný config nepoužívají
    if (cd "$ROOT" && "$PY" -m unittest discover -s installer/tests 2>&1 | tail -n 4); then
        ok "automatické testy prošly"
    else
        fail "automatické testy selhaly (podrobně: python3 -m unittest discover -s installer/tests -v)"
    fi
fi

# ── 2. otisk stavu zařízení ─────────────────────────────────────────────────
header "2/4 Otisk stavu před zkouškou"
WATCH=("$ROOT/config.json" "$ROOT/config.private.json" "$ROOT/data/installer/state"
       "$ROOT/data/installer/answers.json" "$HOME/.config/systemd/user/hans.service"
       "/etc/sudoers.d/hans-hailortd" "$ROOT/resources/arcface_mobilefacenet.hef")
fingerprint() {
    local f
    for f in "${WATCH[@]}"; do
        if [ -e "$f" ]; then printf '%s %s\n' "$(sha256sum "$f" | cut -c1-16)" "$f"
        else printf 'neexistuje %s\n' "$f"; fi
    done
    printf 'venv:%s\n' "$([ -d "$ROOT/.venv" ] && echo ano || echo ne)"
    printf 'backups:%s\n' "$(ls -d "$ROOT"/data/installer/backup-* 2>/dev/null | wc -l)"
}
BEFORE="$(fingerprint)"
info "sledováno ${#WATCH[@]} souborů + .venv + zálohy configu"
rm -rf "$DRY"

# ── 3. instalace naostro v dry-run ──────────────────────────────────────────
header "3/4 install.sh --dry-run --fresh"
FAKE_PID=""
cleanup() {
    [ -n "$FAKE_PID" ] && kill "$FAKE_PID" 2>/dev/null
    [ "$KEEP" = "1" ] || rm -rf "$DRY"
}
trap cleanup EXIT

if [ "$LIVE" = "1" ]; then
    warn "--live-llm: generování OPRAVDU poběží na Ollamě na PC (načte model do VRAM)."
else
    PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
    "$PY" "$INSTALLER_DIR/tests/fake_services.py" "$PORT" >/dev/null 2>&1 &
    FAKE_PID=$!
    for _ in $(seq 1 20); do http_ok "http://127.0.0.1:$PORT/api/version" && break; sleep 0.2; done
    http_ok "http://127.0.0.1:$PORT/api/version" || die "falešný LLM server se nespustil"
    export HANS_GEN_URL="http://127.0.0.1:$PORT"
    export HANS_GEN_MODEL="jobautomation/OpenEuroLLM-Czech:latest"
    info "falešný LLM na :$PORT — texty persony budou ukázkové, PC se nezatěžuje"
fi

args=(--dry-run --fresh)
[ "$YES" = "1" ] && args+=(--yes)
bash "$INSTALLER_DIR/install.sh" "${args[@]}" || fail "install.sh --dry-run skončil chybou"

# ── 4. kontrola výsledku ────────────────────────────────────────────────────
header "4/4 Kontrola"
if [ -f "$DRY/config.json" ]; then
    if (cd "$ROOT" && "$PY" - "$DRY" <<'EOF'
import json, sys
from pathlib import Path
sys.path.insert(0, ".")
from scripts import config_io
from scripts.hans_persona import persona_core
d = Path(sys.argv[1])
cfg = config_io.load(root=d, hlasit=False)
pub = json.loads((d / "config.json").read_text(encoding="utf-8"))
priv = json.loads((d / "config.private.json").read_text(encoding="utf-8")) \
    if (d / "config.private.json").exists() else {}
bad = config_io.zkontroluj_verejny(pub, priv)
print("  persona       :", cfg.get("persona", {}).get("name"))
print("  domácnost     :", ", ".join(cfg.get("known_persons", {})) or "-")
print("  chat model    :", cfg.get("models", {}).get("dialog"))
print("  Ollama (PC)   :", cfg.get("openwebui_chat", {}).get("base_url", "-"))
print("  začátek promptu:", persona_core(cfg)[:110] + "…")
if bad:
    print("  ✗ ve veřejné části je citlivá hodnota:", "; ".join(bad[:5]))
    sys.exit(1)
print("  ✓ config jde načíst a veřejná část je čistá")
EOF
    ); then ok "výsledný config z dry-run je v pořádku"
    else fail "výsledný config z dry-run neprošel kontrolou"; fi
else
    warn "dry-run nezapsal config (přeskočené kroky network/persona?) — kontrola configu vynechána"
fi

AFTER="$(fingerprint)"
if [ "$BEFORE" = "$AFTER" ]; then
    ok "na zařízení se nic nezměnilo (config, služba, .venv, stav instalace)"
else
    fail "NĚCO SE ZMĚNILO — tohle by se v dry-run stát nemělo:"
    diff <(echo "$BEFORE") <(echo "$AFTER") | sed 's/^/    /'
fi

header "Výsledek"
if [ "$KEEP" = "1" ] && [ -d "$DRY" ]; then
    info "výsledky dry-run: ${DRY#"$ROOT"/}/ (config.json, config.private.json, answers.json)"
    info "pozor: config.private.json a answers.json obsahují, co jsi zadal (jména, klíče)"
fi
if [ "$FAILED" = "0" ]; then ok "ZKOUŠKA PROŠLA"; else err "ZKOUŠKA NEPROŠLA — viz výše"; fi
exit "$FAILED"
