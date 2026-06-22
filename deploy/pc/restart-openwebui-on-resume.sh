#!/bin/sh
# /usr/lib/systemd/system-sleep/restart-openwebui   (na PC, ne na Pi!)
#
# Po probuzení PC ze spánku restartuje kontejner OpenWebUI.
#
# Proč: po suspend/resume drží OpenWebUI mrtvá DB spojení → backend proces
# žije (/health, /api/version, /api/config i proxy na Ollamu odpovídají),
# ALE endpointy nad jeho databází (/api/v1/auths = login/session, root UI)
# visí na timeout → stránka se v prohlížeči nenačte. Ollama jako nativní
# služba se zotaví sama; OpenWebUI v Dockeru ne. Restart kontejneru to spraví.
#
# systemd předává: $1 = pre|post, $2 = suspend|hibernate|...
# Restartujeme jen při probuzení (post).

[ "$1" = "post" ] || exit 0

DOCKER=$(command -v docker) || exit 0

# počkej na docker daemon (po resume může chvíli najíždět síť/storage)
i=0
while [ $i -lt 6 ]; do
    "$DOCKER" info >/dev/null 2>&1 && break
    sleep 2
    i=$((i + 1))
done

# najdi kontejner OpenWebUI: nejdřív podle image, fallback podle jména
CONTAINER=$("$DOCKER" ps --filter "ancestor=ghcr.io/open-webui/open-webui:main" --format '{{.Names}}' | head -n1)
[ -z "$CONTAINER" ] && CONTAINER=$("$DOCKER" ps --format '{{.Names}}' | grep -i 'open.\?webui' | head -n1)
[ -z "$CONTAINER" ] && { logger -t restart-openwebui "kontejner OpenWebUI nenalezen"; exit 0; }

"$DOCKER" restart "$CONTAINER" >/dev/null 2>&1
logger -t restart-openwebui "OpenWebUI ($CONTAINER) restartovan po resume"
exit 0
