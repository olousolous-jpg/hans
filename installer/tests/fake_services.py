"""Falešná Ollama + OpenWebUI pro testy průvodce (jen standardní knihovna).

Spuštění samostatně:  python3 installer/tests/fake_services.py 11999
Zaznamenává přijaté požadavky do FakeState.log (pro asserty v testech).
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PERSONA = {
    "core": "Jsi {name}, zahradnice, která pečuje o zahradu i domácnost lidí, se kterými žije. "
            "Jsi trpělivá, věcná a laskavá. Mluvíš krátce a k věci.",
    "style_rules": "Mluvíš krátkými větami a vyhýbáš se cizím slovům.",
    "interests_seed": "Zajímá ji botanika, počasí, kompostování a staré odrůdy jablek.",
    "identity_who": "Jsem digitální zahradnice. Mám ráda rostliny a klid. Pozoruji domácnost a pomáhám.",
    "identity_household": "V domácnosti žije Petr a Jana. Rád s nimi mluvím o zahradě a o počasí.",
    "identity_companion": "Koláč je plyšový medvídek-detektiv. Občas mi oponuje a baví mě to.",
    "identity_life": "Žiji na Raspberry Pi 5 s akcelerátorem Hailo. Myslím na počítači v síti.",
}
COMPANION = {"name": "Šiška", "personality": "Veverka, která všechno zpochybňuje.",
             "interests": "Zajímají ji ořechy a zásoby.",
             "doctrine": "Jsi Šiška — veverka skeptička. Oponuješ s humorem."}
FORMS = {
    "Petr": {"gen": "Petra", "dat": "Petrovi", "acc": "Petra", "loc": "Petrovi", "voc": "Petře"},
    "Jana": {"gen": "Jany", "dat": "Janě", "acc": "Janu", "loc": "Janě", "voc": "Jano"},
}


class FakeState:
    def __init__(self):
        self.models = ["bge-m3:latest", "jobautomation/OpenEuroLLM-Czech:latest", "llava:7b"]
        self.collections: dict[str, str] = {}
        self.files: dict[str, str] = {}
        self.log: list[tuple[str, str, object]] = []
        self.chat_override = None      # callable(body) -> str, pro testy chyb


def make_handler(st: FakeState):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            ctype = self.headers.get("Content-Type", "")
            if "json" in ctype and raw:
                return json.loads(raw)
            return raw

        def _send(self, obj, code=200):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            st.log.append(("GET", self.path, None))
            if self.path == "/api/version":
                return self._send({"version": "0.9.0"})
            if self.path == "/api/tags":
                return self._send({"models": [{"name": m} for m in st.models]})
            if self.path.startswith("/api/v1/knowledge"):
                return self._send({"items": [{"name": k, "id": v} for k, v in st.collections.items()]})
            if self.path == "/":
                return self._send({"ok": True})
            self._send({"detail": "not found"}, 404)

        def do_POST(self):
            body = self._body()
            st.log.append(("POST", self.path, body))
            p = self.path
            if p == "/api/chat":
                if st.chat_override:
                    return self._send({"message": {"content": st.chat_override(body)}})
                prompt = body["messages"][-1]["content"]
                if "Vyskloňuj" in prompt:
                    name = prompt.split("„")[1].split("“")[0]
                    out = FORMS.get(name, {k: name for k in ("gen", "dat", "acc", "loc", "voc")})
                elif "SPOLEČNÍKA" in prompt:
                    out = COMPANION
                elif "Vytvoř identitu" in prompt:
                    out = PERSONA
                else:
                    out = {"pozdrav": "Dobrý den"}
                return self._send({"message": {"content": json.dumps(out, ensure_ascii=False)}})
            if p == "/api/pull":
                name = body.get("model") or body.get("name")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.end_headers()
                for ev in ({"status": "pulling manifest"},
                           {"status": "downloading", "total": 100, "completed": 50},
                           {"status": "success"}):
                    self.wfile.write((json.dumps(ev) + "\n").encode())
                if name not in st.models:
                    st.models.append(name if ":" in name.split("/")[-1] else name + ":latest")
                return
            if p == "/api/create":
                name = body.get("model") or body.get("name")
                st.models.append(name)
                return self._send({"status": "success"})
            if p == "/api/v1/knowledge/create":
                kid = str(uuid.uuid4())
                st.collections[body["name"]] = kid
                return self._send({"id": kid, "name": body["name"]})
            if p == "/api/v1/files/":
                fid = str(uuid.uuid4())
                st.files[fid] = "uploaded"
                return self._send({"id": fid})
            if p.endswith("/file/add") or p.endswith("/file/remove"):
                return self._send({"ok": True})
            self._send({"detail": "not found"}, 404)

    return H


def start(port: int = 0):
    st = FakeState()
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(st))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, st


if __name__ == "__main__":
    srv, _ = start(int(sys.argv[1]) if len(sys.argv) > 1 else 11999)
    print("fake services on :%d" % srv.server_address[1], flush=True)
    threading.Event().wait()
