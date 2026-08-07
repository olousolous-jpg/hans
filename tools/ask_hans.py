#!/usr/bin/env python3
"""Pošli Hansovi dotaz přes web chat most a počkej na odpověď.

Používá `POST /api/chat/send` + `GET /api/chat/poll` (WEBADMIN_V2), tedy
SKUTEČNOU chatovou cestu — parse_command → LLM router → agent → grounding
→ persona. Jiný kanál než Matrix, takže testy nezaplevelí místnost.
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:7860"   # web_admin na Pi


def _post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=20) as r:
        return json.loads(r.read().decode())


def ask(message, person="Uživatel", timeout=180):
    rid = _post("/api/chat/send", {"person": person, "message": message})["id"]
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(2)
        try:
            resp = _get("/api/chat/poll?id=" + rid)
        except Exception:
            continue
        # API vrací {"ready": true, "response": "..."} — ne "text".
        if resp and resp.get("ready") and resp.get("response"):
            return resp["response"], time.time() - t0
    return None, time.time() - t0


if __name__ == "__main__":
    for msg in sys.argv[1:]:
        txt, dt = ask(msg)
        print("=" * 70)
        print("TY:   %s" % msg)
        print("HANS: %s" % (txt or "(bez odpovědi po %.0fs)" % dt))
        print("      [%.1fs]" % dt)
