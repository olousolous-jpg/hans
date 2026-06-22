#!/bin/sh
# install-resume-hook.sh  —  SPUSTIT NA PC (ne na Pi):  sudo sh install-resume-hook.sh
#
# Nainstaluje systemd sleep-hook, který po každém probuzení PC ze spánku
# restartuje kontejner OpenWebUI. Řeší, že OpenWebUI po suspend/resume drží
# mrtvá DB spojení → backend žije (/health OK), ale login/root UI visí na
# timeout a stránka se nenačte. Po instalaci proběhne i ostrý test.

set -e

HOOK=/usr/lib/systemd/system-sleep/restart-openwebui

if [ "$(id -u)" != "0" ]; then
    echo "Spusť přes sudo:  sudo sh install-resume-hook.sh"
    exit 1
fi

mkdir -p /usr/lib/systemd/system-sleep

cat > "$HOOK" <<'EOF'
#!/bin/sh
# Restart OpenWebUI po probuzení PC ze spánku.
# systemd předává: $1 = pre|post, $2 = suspend|hibernate|...
[ "$1" = "post" ] || exit 0

DOCKER=$(command -v docker) || exit 0

# počkej na docker daemon (po resume může chvíli najíždět síť/storage)
i=0
while [ $i -lt 6 ]; do
    "$DOCKER" info >/dev/null 2>&1 && break
    sleep 2
    i=$((i + 1))
done

# najdi kontejner OpenWebUI: podle image, fallback podle jména
CONTAINER=$("$DOCKER" ps --filter "ancestor=ghcr.io/open-webui/open-webui:main" --format '{{.Names}}' | head -n1)
[ -z "$CONTAINER" ] && CONTAINER=$("$DOCKER" ps --format '{{.Names}}' | grep -i 'open.\?webui' | head -n1)
[ -z "$CONTAINER" ] && { logger -t restart-openwebui "kontejner OpenWebUI nenalezen"; exit 0; }

"$DOCKER" restart "$CONTAINER" >/dev/null 2>&1
logger -t restart-openwebui "OpenWebUI ($CONTAINER) restartovan po resume"
exit 0
EOF

chmod +x "$HOOK"

echo "== Hook nainstalován =="
ls -l "$HOOK"

echo
echo "== Ostrý test (simuluji probuzení) =="
"$HOOK" post suspend

echo
echo "== Stav kontejneru (čekám 3s) =="
sleep 3
docker ps --format '{{.Names}}\t{{.Status}}' | grep -i 'open.\?webui' || echo "(kontejner OpenWebUI nenalezen v docker ps)"

echo
echo "== Log hooku =="
journalctl -t restart-openwebui --no-pager 2>/dev/null | tail -3 || echo "(journalctl nedostupný)"

echo
echo "Hotovo. Pokud STATUS výše ukazuje 'Up X seconds', hook funguje."
echo "Při příštím probuzení PC se OpenWebUI restartuje automaticky."
