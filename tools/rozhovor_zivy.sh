#!/bin/bash
# rozhovor_zivy.sh <osoba> <soubor_s_vetami>
#
# Navazujici testovaci rozhovor v BEZICIM Hansovi (ne ve vlastnim procesu).
# Doplnuje scripts/test_rozhovor.py, ktery bezi v procesu vlastnim — rozdil je
# podstatny u vseho, co se pta na stav procesu (napr. probe_matrix hlasi ve
# skriptu "most v tomto procesu nebezi").
#
# Most jde pres soubor data/.web_chat_req.json do Hansova procesu, takze se
# testuje TAZ instance, kterou ma uzivatel. Odpoved je v poli `response`.
#
# ⚠️ PO TESTU UKLIDIT VE TRECH ULOZISTICH (denik + hans_convindex.forget,
#    data/conversations/<jmeno>.json, RAG hans_knowledge.delete). Filtruj
#    podle CASU vlastniho behu A ZAROVEN podle shody s tim, cos sam poslal —
#    samotny cas chyta i Hansovu vlastni proaktivni cinnost, samotny text
#    chyta i vlastni testy uzivatele. Obojí dolozeno 4.9.
#
# Pouziti:
#   date +%s > /tmp/ts.txt          # poznamenej start kvuli uklidu
#   tools/rozhovor_zivy.sh <jmeno-z-known_persons> vety.txt
set -u
OSOBA="${1:?jmeno osoby — klic z config.known_persons}"
SOUBOR="${2:?soubor s vetami, jedna na radek}"
HOST="${HANS_HOST:-http://127.0.0.1:7860}"
N=0
while IFS= read -r v; do
  [ -z "$v" ] && continue
  N=$((N+1))
  id=$(curl -s -m 15 -X POST "$HOST/api/chat/send" \
       -H 'Content-Type: application/json' \
       -d "$(python3 -c 'import json,sys;print(json.dumps({"person":sys.argv[1],"message":sys.argv[2]}))' "$OSOBA" "$v")" \
       | python3 -c 'import json,sys;print(json.load(sys.stdin).get("id",""))')
  printf '\n[%02d] %s: %s\n' "$N" "$OSOBA" "$v"
  if [ -z "$id" ]; then echo "     !! nedostal jsem id"; continue; fi
  t=""
  for i in $(seq 1 90); do
    t=$(curl -s -m 15 "$HOST/api/chat/poll?id=$id" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except: d={}
print(d.get("response") or "")')
    [ -n "$t" ] && { printf '     HANS: %s\n' "$t"; break; }
    sleep 2
  done
  [ -z "$t" ] && echo "     !! timeout"
  sleep 3
done < "$SOUBOR"
