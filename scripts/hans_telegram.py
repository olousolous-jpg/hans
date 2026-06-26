"""scripts/hans_telegram.py

HANS_TELEGRAM_V1 — most Hans ↔ Telegram: push na telefon + OBOUSMĚRNÝ chat.

- OUTBOUND: send(text) → zpráva na telefon (alerty, digest, proaktivní vzkaz).
- INBOUND: long-poll getUpdates → text routuje do Hansova chatu (paměť+osobnost)
  přes chat_handler.send_chat_message → odpověď pošle zpět. Vzdálený chat odkudkoliv.

Bezpečnost: odpovídá JEN na povolený chat_id (config). Bot token je SECRET → žije
jen v config.json (gitignored). Vše gatované telegram.enabled (default false) — bez
tokenu se nespustí. Nikdy nevyhazuje výjimku nahoru (nesmí shodit Hanse).

Pozn. soukromí: Telegram = cloud, obsah opouští dům. Posílej jen to, co chceš ven.

Setup (uživatel):
  1) @BotFather → /newbot → získej BOT TOKEN.
  2) napiš svému botovi libovolnou zprávu.
  3) chat_id zjistíš z getUpdates (helper: python3 -m scripts.hans_telegram <token>).
  4) config.json telegram: {enabled:true, bot_token, chat_id, as_person}.

Config:
  telegram.{enabled, bot_token, chat_id, as_person, announce_online}
"""
from __future__ import annotations
import json
import logging
import os
import re
import sqlite3
import threading
import time

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

_log = logging.getLogger("hans_telegram")
_API = "https://api.telegram.org/bot%s/%s"


class TelegramBridge:
    def __init__(self, config: dict, chat_handler=None):
        cfg = (config.get("telegram", {}) or {})
        self.config = config or {}
        self._handler = chat_handler
        self.token = str(cfg.get("bot_token", "") or "")
        self.chat_id = str(cfg.get("chat_id", "") or "")
        self.as_person = str(cfg.get("as_person", "") or "uživatel")
        # TELEGRAM_MULTIUSER_V1 — víc povolených uživatelů.
        # _users[cid] = {person, role, push_questions}
        #   role: 'full' (chat + obrázky/příkazy) | 'chat' (jen chat + otázky)
        #   push_questions: Hans jí sám posílá své otázky přes Telegram
        # Zpětně kompat: legacy chat_id/as_person = primární full uživatel.
        self._users: dict = {}
        if self.chat_id:
            self._users[self.chat_id] = {"person": self.as_person,
                                         "role": "full", "push_questions": False}
        for u in (cfg.get("users", []) or []):
            _cid = str((u or {}).get("chat_id", "") or "")
            if not _cid:
                continue
            self._users[_cid] = {
                "person": str((u or {}).get("as_person", "") or self.as_person),
                "role": str((u or {}).get("role", "full") or "full"),
                "push_questions": bool((u or {}).get("push_questions", False)),
            }
        self._q_store = None  # TELEGRAM_QUESTIONS_V1 (lazy)
        self._last_q_push: dict = {}  # cid -> ts (throttle)
        self._announce = bool(cfg.get("announce_online", False))
        self.enabled = (bool(cfg.get("enabled", False))
                        and bool(self.token) and requests is not None)
        self._offset = None
        self._pending_brain_notify = False  # HANS_TELEGRAM_BRAIN_NOTIFY_V1
        self._stop = threading.Event()
        self._thread = None
        # TELEGRAM_QUIET_HOURS_V1 — v noci NEpípat telefon: proaktivní (Hansem
        # iniciované) zprávy se v tichém okně pozdrží a doručí až po quiet_end.
        # Odpovědi na zprávy uživatele jdou VŽDY hned (i v noci).
        self._quiet_start = int(cfg.get("quiet_start_hour", 22))  # včetně
        self._quiet_end = int(cfg.get("quiet_end_hour", 9))       # do (po 9:00 OK)
        self._deferred: list = []  # in-memory fronta odložených proaktivních zpráv
        if cfg.get("enabled", False) and not self.enabled:
            if requests is None:
                _log.warning("telegram: requests není dostupné → vypnuto")
            elif not self.token:
                _log.warning("telegram: enabled, ale chybí bot_token → vypnuto")

    # ── OUTBOUND ────────────────────────────────────────────────────────────
    def _person_for(self, cid: str) -> str:
        """TELEGRAM_MULTIUSER_V1 — osoba podle chat_id (pro správné připsání)."""
        u = self._users.get(str(cid))
        return u["person"] if u else self.as_person

    def _is_full(self, cid: str) -> bool:
        """True když uživatel smí obrázky/příkazy (role 'full'). 'chat' = jen
        chat + otázky."""
        u = self._users.get(str(cid))
        return (u.get("role", "full") == "full") if u else True

    def send(self, text: str, chat_id: str = None) -> bool:
        """Pošle zprávu na telefon. Vrací True/False. Nikdy nevyhodí."""
        if not self.enabled or not (text or "").strip():
            return False
        cid = chat_id or self.chat_id
        if not cid:
            _log.warning("telegram send: chybí chat_id")
            return False
        try:
            r = requests.post(_API % (self.token, "sendMessage"),
                              json={"chat_id": cid, "text": text[:4000]},
                              timeout=15)
            if not r.ok:
                _log.warning("telegram send HTTP %s: %s", r.status_code,
                             (r.text or "")[:120])
            return bool(r.ok)
        except Exception as e:
            _log.warning("telegram send selhal: %s", e)
            return False

    # ── PROAKTIVNÍ (quiet-hours, TELEGRAM_QUIET_HOURS_V1) ───────────────────
    def _in_quiet_hours(self) -> bool:
        """True v tichém okně (přes půlnoc). Default 22:00–09:00."""
        h = time.localtime().tm_hour
        if self._quiet_start == self._quiet_end:
            return False
        if self._quiet_start < self._quiet_end:
            return self._quiet_start <= h < self._quiet_end
        return h >= self._quiet_start or h < self._quiet_end  # přes půlnoc

    def send_proactive(self, text: str, chat_id: str = None) -> bool:
        """Hansem iniciovaná zpráva (Severka, brain-notify, announce, otázky).
        V tichém okně se NEpošle hned, ale odloží a doručí po quiet_end (9:00).
        Odpovědi na uživatele tudy NEchodí — ty jdou přes send() vždy hned.
        chat_id=None → primární uživatel; jinak konkrétní (otázky pro vedlejšího uživatele)."""
        if not self.enabled or not (text or "").strip():
            return False
        cid = chat_id or self.chat_id
        if self._in_quiet_hours():
            self._deferred.append((text, cid))
            _log.info("telegram: proaktivní zpráva ODLOŽENA do %d:00 "
                      "(tiché okno, nepípat v noci)", self._quiet_end)
            return True
        return self.send(text, chat_id=cid)

    def _flush_deferred(self):
        """Doruč odložené proaktivní zprávy, jakmile skončí tiché okno."""
        if not self._deferred or self._in_quiet_hours():
            return
        pending = self._deferred
        self._deferred = []
        for item in pending:
            txt, cid = item if isinstance(item, tuple) else (item, None)
            self.send(txt, chat_id=cid)
        _log.info("telegram: doručeno %d odložených proaktivních zpráv", len(pending))

    # ── OTÁZKY PRO UŽIVATELE (TELEGRAM_QUESTIONS_V1) ────────────────────────
    def _questions(self):
        if self._q_store is None:
            try:
                from scripts.hans_questions import HansQuestionsStore
                self._q_store = HansQuestionsStore(self._diary_path(), self.config)
            except Exception as e:
                _log.warning("HansQuestionsStore init: %s", e)
        return self._q_store

    def _maybe_push_questions(self):
        """Hans pošle své čekající otázky uživatelům s push_questions=true
        (např. vedlejší uživatel). Throttle per uživatel; tiché okno řeší send_proactive.
        mark_asked_voice = SDÍLENÉ značení → nezeptá se 2× (ani přes popup)."""
        interval = float((self.config.get("telegram", {}) or {}).get(
            "question_interval_h", 6)) * 3600
        now = time.time()
        qs = None
        for cid, u in self._users.items():
            if not u.get("push_questions"):
                continue
            if now - self._last_q_push.get(cid, 0) < interval:
                continue
            if qs is None:
                qs = self._questions()
                if qs is None:
                    return
            self._last_q_push[cid] = now  # i prázdný pokus = počkej interval
            try:
                q = qs.next_for_person(u["person"])
            except Exception as e:
                _log.debug("next_for_person(%s): %s", u["person"], e)
                continue
            if not q or not (q.question or "").strip():
                continue
            if self.send_proactive(q.question, chat_id=cid):
                try:
                    qs.mark_asked_voice(q.id)
                except Exception as e:
                    _log.debug("mark_asked_voice: %s", e)
                _log.info("telegram: otázka → %s: %.60s", u["person"], q.question)

    # ── INBOUND (long-poll) ─────────────────────────────────────────────────
    def start(self):
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._poll_loop, daemon=True,
                                        name="HansTelegram")
        self._thread.start()
        _log.info("TelegramBridge spuštěn (long-poll, as_person=%s)", self.as_person)

    def stop(self):
        self._stop.set()

    def _poll_loop(self):
        if self._announce:
            self.send_proactive("Hans je k dispozici. Můžete mi napsat.")
        while not self._stop.is_set():
            try:
                self._flush_deferred()  # TELEGRAM_QUIET_HOURS_V1 — doruč po 9:00
                self._maybe_push_questions()  # TELEGRAM_QUESTIONS_V1
                params = {"timeout": 30}
                if self._offset is not None:
                    params["offset"] = self._offset
                r = requests.get(_API % (self.token, "getUpdates"),
                                 params=params, timeout=40)
                if not r.ok:
                    time.sleep(5)
                    continue
                for upd in (r.json().get("result", []) or []):
                    try:
                        self._offset = int(upd["update_id"]) + 1
                    except Exception:
                        pass
                    try:
                        self._handle_update(upd)
                    except Exception as e:
                        _log.warning("telegram handle_update: %s", e)
            except Exception as e:
                _log.debug("telegram poll: %s", e)
                time.sleep(5)

    def _handle_update(self, upd: dict):
        msg = upd.get("message") or upd.get("edited_message")
        if not isinstance(msg, dict):
            return
        cid = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not text:
            return
        # bezpečnost: jen povolené chat_id (TELEGRAM_MULTIUSER_V1)
        if self._users and cid not in self._users:
            _log.warning("telegram: zpráva z neznámého chat_id %s — ignoruji", cid)
            return
        _person = self._person_for(cid)
        _log.info("telegram ← %s: %.60s", _person, text)
        # TELEGRAM_MULTIUSER_V1 — obrázky/příkazy jen pro 'full' uživatele.
        # 'chat' role (např. vedlejší uživatel) → vše padá rovnou do chatu (žádné obrázky).
        if self._is_full(cid):
            # HANS_TELEGRAM_CONTENT_V1 — Telegram příkazy (obraz/deník/úvaha)
            if self._handle_command(text, cid):
                return
            # HANS_TELEGRAM_NL_INTENT_V1 — žádost o obsah v přirozené řeči
            _intent = self._detect_intent(text)
            if _intent == "artwork":
                self._cmd_artwork(cid, self._pick_mode(text)); return
            if _intent == "diary":
                self._cmd_diary(cid); return
            if _intent == "status":
                self._cmd_status(cid); return
            if _intent == "musing":
                self._cmd_musing(cid); return
            if _intent == "wol":
                self._cmd_wol(cid); return
        # běžná zpráva → chat (mozek); až teď nastav pending pro brain-notify
        self._pending_brain_notify = True  # HANS_TELEGRAM_BRAIN_NOTIFY_V1
        reply = None
        try:
            if self._handler is not None and hasattr(self._handler,
                                                     "send_chat_message"):
                reply = self._handler.send_chat_message(_person, text)
        except Exception as e:
            _log.warning("telegram → chat selhal: %s", e)
            reply = None
        if reply:
            self.send(reply, chat_id=cid)
        else:
            # HANS_TELEGRAM_BRAIN_NOTIFY_V1 — bez mozku (PC spí) upřímně + slib notify
            self.send("Můj mozek (počítač s jazykovým centrem) teď spí, takže vám "
                      "nedokážu pořádně odpovědět. Dám vědět, jakmile budu opět "
                      "online.", chat_id=cid)


    # ── OBSAH NA POŽÁDÁNÍ (HANS_TELEGRAM_CONTENT_V1) ────────────────────────
    def _diary_path(self) -> str:
        return ((self.config.get("hans_idle", {}) or {}).get("diary_db")
                or self.config.get("diary_db") or "data/hans_diary.db")

    def send_photo(self, file_path: str, caption: str = "",
                   chat_id: str = None) -> bool:
        """Pošle obrázek na telefon (sendPhoto, multipart). Nikdy nevyhodí."""
        if not self.enabled or not file_path or not os.path.exists(file_path):
            return False
        cid = chat_id or self.chat_id  # TELEGRAM_MULTIUSER_V1 — komu poslat
        if not cid:
            return False
        try:
            with open(file_path, "rb") as f:
                r = requests.post(
                    _API % (self.token, "sendPhoto"),
                    data={"chat_id": cid, "caption": (caption or "")[:1000]},
                    files={"photo": f}, timeout=40)
            if not r.ok:
                _log.warning("telegram sendPhoto HTTP %s", r.status_code)
            return bool(r.ok)
        except Exception as e:
            _log.warning("telegram send_photo selhal: %s", e)
            return False

    def _handle_command(self, text: str, cid: str) -> bool:
        """Telegram-specifické příkazy (pošli OBSAH). Vrátí True když obslouženo."""
        cmd = (text or "").lower().lstrip("/").split()[0] if text else ""
        if cmd in ("obraz", "foto", "malba", "obrázek", "obrazek"):
            self._cmd_artwork(cid, self._pick_mode(text)); return True
        if cmd in ("denik", "deník", "diary"):
            self._cmd_diary(cid); return True
        if cmd in ("uvaha", "úvaha", "myslenka", "myšlenka"):
            self._cmd_musing(cid); return True
        if cmd in ("stav", "status"):
            self._cmd_status(cid); return True
        if cmd in ("wol", "probudit", "wakeup", "probud", "probuď"):
            self._cmd_wol(cid); return True
        if cmd in ("help", "pomoc", "napoveda", "nápověda", "start"):
            self.send("Můžete mi normálně psát (povídáme si), nebo použít příkazy:\n"
                      "/obraz — pošlu poslední obraz (napiš „náhodný obraz\" pro náhodný)\n"
                      "/denik — výpis z mého deníku\n"
                      "/uvaha — má poslední úvaha\n"
                      "/stav — stav systému (teplota, RAM, disk, mozek)\n"
                      "/wol — probudím počítač (PC) přes síť\n"
                      "/help — tato nápověda", chat_id=cid)
            return True
        return False

    def _pick_mode(self, text: str) -> str:
        """náhodný vs poslední obraz dle formulace."""
        t = (text or "").lower()
        if re.search(r"náhod|nahod|nějak|nejak|random|jin[ýé]|jakýkoliv|jakykoliv", t):
            return "random"
        return "latest"

    def _cmd_artwork(self, cid: str, pick: str = "latest"):
        row = None
        order = "RANDOM()" if pick == "random" else "ts DESC"
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path(),
                                  uri=True, timeout=3.0)
            row = con.execute(
                "SELECT note, data FROM diary WHERE event_type='artwork' "
                "ORDER BY " + order + " LIMIT 1").fetchone()
            con.close()
        except Exception as e:
            _log.warning("telegram artwork query: %s", e)
        if not row:
            self.send("Zatím jsem nic nenamaloval.", chat_id=cid)
            return
        note, data = row
        path = ""
        try:
            path = ((json.loads(data or "{}") or {}).get("path", "") or "")
        except Exception:
            path = ""
        if path and os.path.exists(path):
            cap = (note or "Můj obraz").strip()
            if not self.send_photo(path, caption=cap, chat_id=cid):
                self.send("Obraz se mi nepodařilo odeslat.", chat_id=cid)
        else:
            self.send("Obraz teď nemůžu najít.", chat_id=cid)

    def _cmd_diary(self, cid: str, limit: int = 10):
        _TYPES = ("musing", "book_reflection", "book_completion_reflection",
                  "narrative_chapter", "work_completion_reflection",
                  "creation_reflection", "movie_opinion", "web_read",
                  "lesson_learned", "reading_takeaway", "spontaneous",
                  "introspection")
        lines = []
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path(),
                                  uri=True, timeout=3.0)
            q = ("SELECT datetime(ts,'unixepoch','localtime'), COALESCE(title,''), "
                 "COALESCE(note,'') FROM diary WHERE event_type IN (%s) "
                 "ORDER BY ts DESC LIMIT ?" % ",".join("?" * len(_TYPES)))
            rows = con.execute(q, _TYPES + (int(limit),)).fetchall()
            con.close()
            for t, ti, no in rows:
                body = (no or ti).strip().replace("\n", " ")
                if body:
                    day = (t or "")[5:16]  # MM-DD HH:MM
                    lines.append("• %s — %s" % (day, body[:160]))
        except Exception as e:
            _log.warning("telegram diary query: %s", e)
        txt = "Z mého deníku:\n" + "\n".join(lines) if lines else "Deník je zatím prázdný."
        self.send(txt[:4000], chat_id=cid)

    # HANS_TELEGRAM_NL_INTENT_V1 — rozpoznej žádost o obsah v přirozené řeči
    # (ne jen /příkaz). Vyžaduje PODSTATNÉ JMÉNO + SLOVESO žádosti, ať casual
    # zmínka („ten obraz je hezký") netriggeruje.
    _ASK = r"(pošl|posl|ukaž|ukaz|zobraz|poslat|uvid|vidět|videt|mrkn|dej|chci|" \
           r"můžeš|muzes|máš.*\bobraz|nějak)"

    def _detect_intent(self, text: str):
        t = (text or "").lower()
        if not t:
            return None
        if re.search(r"obraz|obrázek|obrazek|namaloval|malb|namaluj", t) and re.search(self._ASK, t):
            return "artwork"
        if re.search(r"den[ií]k|deníč|zápis", t) and re.search(self._ASK, t):
            return "diary"
        if re.search(r"\bstav\b|teplot|kolik.*ram|\bcpu\b|vyt[íi]ž|jak.*ti to běž", t):
            return "status"
        if re.search(r"úvah|uvah|myšlenk|myslenk", t) and re.search(self._ASK, t):
            return "musing"
        if re.search(r"probu[ďd]|\bwol\b|(zapni|nastartuj|nahoď|nahod).*(pc|počítač|pocitac|mozek)", t):
            return "wol"
        return None

    @staticmethod
    def _send_magic(mac: str):
        """WOL magic packet (UDP broadcast :9) — stejně jako hans_routine."""
        import socket
        m = mac.replace(":", "").replace("-", "").lower()
        if len(m) != 12 or not all(c in "0123456789abcdef" for c in m):
            raise ValueError("neplatná MAC")
        packet = bytes.fromhex("FF" * 6 + m * 16)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.sendto(packet, ("255.255.255.255", 9))
        finally:
            s.close()

    def _wol_verify(self, ip: str, cid: str):
        import subprocess
        for _ in range(8):
            time.sleep(15)
            try:
                ok = subprocess.run(["ping", "-c", "1", "-W", "2", ip],
                                    capture_output=True, timeout=5).returncode == 0
            except Exception:
                ok = False
            if ok:
                self.send("Počítač je online. Mozek se za chvíli probudí.", chat_id=cid)
                return
        self.send("Počítač se zatím neprobudil (možná je úplně vypnutý nebo "
                  "WOL v BIOSu vypnutý).", chat_id=cid)

    def _cmd_wol(self, cid: str):
        """HANS_TELEGRAM_WOL_V1 — manuální probuzení PC přes síť (na povel)."""
        mac = str(self.config.get("wol_pc_mac", "") or "")
        if not mac:
            self.send("Nemám nastavenou MAC adresu PC (wol_pc_mac).", chat_id=cid)
            return
        try:
            self._send_magic(mac)
            _log.info("telegram /wol → magic packet to %s", mac)
        except Exception as e:
            self.send("Probuzení se nezdařilo: %s" % e, chat_id=cid)
            return
        self.send("Posílám počítači probouzecí signál. Za chvíli ověřím, "
                  "zda naběhl…", chat_id=cid)
        ip = str(self.config.get("wol_pc_ip", "") or "")
        if ip:
            threading.Thread(target=self._wol_verify, args=(ip, cid),
                             daemon=True).start()

    def _cmd_status(self, cid: str):
        """HANS_TELEGRAM_CONTENT_V1 — stav systému (teplota, RAM, CPU, disk, mozek)."""
        lines = ["Stav systému:"]
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                lines.append("Teplota CPU: %.0f °C" % (int(f.read().strip()) / 1000.0))
        except Exception:
            pass
        try:
            import psutil
            cpu = psutil.cpu_percent(interval=0.5)
            vm = psutil.virtual_memory()
            du = psutil.disk_usage("/")
            lines.append("Zátěž CPU: %.0f %%" % cpu)
            lines.append("RAM: %.1f / %.1f GB (volné %.1f GB)" % (
                (vm.total - vm.available) / 1e9, vm.total / 1e9, vm.available / 1e9))
            lines.append("Disk: volných %.0f GB z %.0f GB" % (
                du.free / 1e9, du.total / 1e9))
        except Exception:
            pass
        try:
            la = os.getloadavg()
            lines.append("Load: %.2f %.2f %.2f" % la)
        except Exception:
            pass
        try:
            with open("/proc/uptime") as f:
                up = float(f.read().split()[0])
            lines.append("Běžím: %dd %dh %dm" % (
                int(up // 86400), int((up % 86400) // 3600), int((up % 3600) // 60)))
        except Exception:
            pass
        try:
            url = ((self.config.get("openwebui_chat", {}) or {}).get("base_url", "")
                   or "").rstrip("/")
            if url:
                r = requests.get(url + "/api/tags", timeout=4)
                lines.append("Mozek (LLM): " + ("online" if r.ok else "nedostupný"))
        except Exception:
            lines.append("Mozek (LLM): spí / nedostupný")
        self.send("\n".join(lines), chat_id=cid)

    def _cmd_musing(self, cid: str):
        row = None
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path(),
                                  uri=True, timeout=3.0)
            row = con.execute(
                "SELECT COALESCE(note,'') FROM diary WHERE event_type='musing' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            con.close()
        except Exception as e:
            _log.warning("telegram musing query: %s", e)
        txt = (row[0].strip() if row and row[0] else "")
        self.send(txt[:4000] if txt else "Zatím jsem si nic nezapsal.", chat_id=cid)


# ── CLI helper: zjisti chat_id (po napsání zprávy botovi) ───────────────────
if __name__ == "__main__":
    import sys
    if requests is None:
        print("requests není dostupné"); sys.exit(1)
    if len(sys.argv) < 2:
        print("Použití: python3 -m scripts.hans_telegram <BOT_TOKEN>")
        print("(nejdřív napiš svému botovi v Telegramu libovolnou zprávu)")
        sys.exit(1)
    tok = sys.argv[1].strip()
    try:
        r = requests.get(_API % (tok, "getUpdates"), timeout=20)
        data = r.json()
    except Exception as e:
        print("chyba:", e); sys.exit(1)
    res = data.get("result", []) or []
    if not res:
        print("Žádné zprávy. Napiš botovi v Telegramu a spusť znovu.")
        sys.exit(0)
    seen = {}
    for upd in res:
        m = upd.get("message") or upd.get("edited_message") or {}
        ch = m.get("chat") or {}
        if ch.get("id"):
            seen[str(ch["id"])] = ch.get("first_name") or ch.get("title") or "?"
    print("Nalezené chat_id:")
    for cid, name in seen.items():
        print("  chat_id =", cid, " (", name, ")")
