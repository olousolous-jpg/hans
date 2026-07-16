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
        # HANS_QUESTIONS_ROUTING_V1 — persons_placeholder: uživatelé bez chat_id
        # (Hans o nich ví, ale nemají kam pushnout otázku → escalate přeskočí
        # Telegram fázi a jde rovnou na web).
        self._persons_placeholder: set = set()
        for u in (cfg.get("users", []) or []):
            _cid = str((u or {}).get("chat_id", "") or "")
            _person = str((u or {}).get("as_person", "") or self.as_person).lower()
            if not _cid:
                # osoba je registrovaná, ale Telegram jí nedoručí
                if _person:
                    self._persons_placeholder.add(_person)
                continue
            # dict indexovaný chat_id — pokud legacy má stejný cid, přepíšeme
            # s user-level nastavením (typicky push_questions=true pro hlavního uživatele)
            self._users[_cid] = {
                "person": _person or self.as_person,
                "role": str((u or {}).get("role", "full") or "full"),
                "push_questions": bool((u or {}).get("push_questions", False)),
            }
        # persons_with_telegram = mohou dostat push (mají chat_id AND
        # push_questions=True). anyone otázky se rovnou berou z prvního push
        # uživatele.
        self._persons_with_telegram: set = set()
        for _cid, _u in self._users.items():
            if _cid and _u.get("push_questions"):
                self._persons_with_telegram.add((_u.get("person") or "").lower())
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

    # HANS_QUESTIONS_ROUTING_V1 — dostupnost kanálu pro osobu
    def _person_has_channel(self, person: str, channel: str) -> bool:
        """Callback pro escalate_channels — má osoba dostupný tento kanál?
        Telegram: existuje entry v _users s push_questions=True A neprázdný
        chat_id pro tuto osobu (nebo 'anyone' pokud aspoň jeden takový je).
        Web/popup: vždy True (dashboard a kamera jsou pro každou osobu)."""
        person = (person or "").lower()
        if channel == "telegram":
            if not self._persons_with_telegram:
                return False
            if person == "anyone":
                return True
            return person in self._persons_with_telegram
        if channel in ("web", "popup"):
            return True
        return False

    def _escalate_questions(self):
        """HANS_QUESTIONS_ROUTING_V1 — projde otázky a přesune vyprchané fáze
        na další kanál. Volá se v poll loopu (dostatečně často, escalate je
        levné). Nepushuje — jen mění channel/channel_since."""
        qs = self._questions()
        if qs is None:
            return
        try:
            _q_cfg = (self.config.get("hans_questions", {}) or {})
            order = _q_cfg.get("channel_order") or ["telegram", "web", "popup"]
            stage_h = float(_q_cfg.get("channel_stage_hours", 12.0))
            stats = qs.escalate_channels(order, stage_h, self._person_has_channel)
            _moved = {k: v for k, v in stats.items() if v > 0}
            if _moved:
                _log.info("questions escalate: %s", _moved)
        except Exception as e:
            _log.debug("escalate_questions: %s", e)

    def _maybe_push_questions(self):
        """HANS_QUESTIONS_ROUTING_V1 — Hans pošle otázky ve fázi Telegram
        uživatelům s push_questions=True. Throttle per uživatel; tiché okno
        řeší send_proactive. Bere jen otázky s channel='telegram' A
        asked_voice_at IS NULL (neposláno ještě v této fázi)."""
        interval = float((self.config.get("telegram", {}) or {}).get(
            "question_interval_h", 6)) * 3600
        now = time.time()
        qs = None
        for cid, u in self._users.items():
            if not u.get("push_questions"):
                continue
            if not cid:
                continue
            if now - self._last_q_push.get(cid, 0) < interval:
                continue
            if qs is None:
                qs = self._questions()
                if qs is None:
                    return
            self._last_q_push[cid] = now  # i prázdný pokus = počkej interval
            try:
                q = qs.next_for_channel(u["person"], "telegram",
                                        only_undelivered=True)
            except Exception as e:
                _log.debug("next_for_channel(%s): %s", u["person"], e)
                continue
            if not q or not (q.question or "").strip():
                continue
            if self.send_proactive(q.question, chat_id=cid):
                try:
                    qs.mark_channel_delivered(q.id)
                except Exception as e:
                    _log.debug("mark_channel_delivered: %s", e)
                _log.info("telegram: otázka → %s: %.60s", u["person"], q.question)

    def _maybe_calendar_reminders(self):
        """HANS_CALENDAR_V1 — připomeň blížící se události z Proton kalendáře.
        SOUKROMÍ: každá událost jde JEN majiteli kalendáře (event['person']),
        NE ostatním Telegram uživatelům. Throttle 5 min; mark_reminded brání
        opakování; send_proactive respektuje tiché okno."""
        cc = (self.config.get("calendar", {}) or {})
        if not cc.get("enabled"):
            return
        try:
            from scripts.hans_calendar import is_enabled, CalendarStore
        except Exception:
            return
        if not is_enabled(self.config):
            return
        now = time.time()
        if now - getattr(self, "_last_cal_check", 0) < 300:
            return
        self._last_cal_check = now
        try:
            store = CalendarStore(self.config, self._diary_path())
            due = store.due_reminders(
                lead_hours=float(cc.get("reminder_lead_hours", 2)))
            if not due:
                return
            # osoba → její chat_id (jen jí posílej její kalendář)
            person_cid = {}
            for cid, u in self._users.items():
                p = (u.get("person") or "").lower()
                if cid and p and p not in person_cid:
                    person_cid[p] = cid
            for ev in due:
                owner = (ev.get("person") or "").lower()
                cid = person_cid.get(owner)
                if not cid:
                    continue  # majitel nemá Telegram → nikam neposílej
                text = store.reminder_text(ev)
                if self.send_proactive(text, chat_id=cid):
                    store.mark_reminded(owner, ev["uid"], ev["start_ts"])
                    _log.info("telegram: připomínka → %s: %.50s", owner, text)
        except Exception as e:
            _log.debug("calendar reminders: %s", e)

    def _maybe_deliver_requested_art(self):
        """HANS_ART_REQUEST_V1 — doruč obraz JEN uživateli, který si o něj
        v Telegramu řekl („namaluj…"). Autonomní malování Hanse (sny, den…)
        se NEposílá. Když je pending požadavek a objeví se nový obraz, pošli
        ho žadateli a pending zruš (čeká max 6 min na render)."""
        pend = getattr(self, "_pending_paint", None)
        if not pend:
            return
        now = time.time()
        if now - getattr(self, "_last_art_check", 0) < 15:
            return
        self._last_art_check = now
        # vyprš staré požadavky (render se nepovedl / trvá moc dlouho)
        for cid in [c for c, ts in pend.items() if now - ts > 360]:
            pend.pop(cid, None)
            try:
                self.send(u"Obraz se mi teď nepodařilo vytvořit, pane. "
                          u"Zkuste to prosím znovu.", chat_id=cid)
            except Exception:
                pass
        if not pend:
            return
        try:
            import json as _json
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path(),
                                  uri=True, timeout=3.0)
            if getattr(self, "_last_art_id", None) is None:
                row = con.execute("SELECT MAX(rowid) FROM diary WHERE "
                                  "event_type='artwork'").fetchone()
                self._last_art_id = (row[0] if row and row[0] else 0)
            rows = con.execute(
                "SELECT rowid, title, COALESCE(note,''), COALESCE(data,'') "
                "FROM diary WHERE event_type='artwork' AND rowid > ? "
                "ORDER BY rowid ASC LIMIT 3", (self._last_art_id,)).fetchall()
            con.close()
        except Exception as e:
            _log.debug("art deliver query: %s", e)
            return
        if not rows:
            return
        # nejnovější nový obraz → pošli VŠEM čekajícím žadatelům, pak vyčisti
        rid, title, note, data = rows[-1]
        self._last_art_id = rid
        path = ""
        try:
            path = (_json.loads(data) or {}).get("path", "") if data else ""
        except Exception:
            path = ""
        cap = f"🎨 Hotovo — {title}"
        # HANS_ART_ORIGIN_V1 — nejdřív Z ČEHO jsem vycházel, teprve pak kritika.
        try:
            from scripts.hans_art import origin_line as _origin
            _o = _origin(title, data)
        except Exception:
            _o = ""
        if _o:
            cap += f"\n{_o}"
        if note:
            cap += f"\n\n{note[:200]}"
        waiting = list(pend.keys())
        for cid in waiting:
            if path and os.path.exists(path):
                self.send_photo(path, caption=cap, chat_id=cid)
            else:
                self.send(cap, chat_id=cid)
            pend.pop(cid, None)
        _log.info("telegram: vyžádaný obraz → %d žadatelům: %.40s",
                  len(waiting), title)

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
                self._escalate_questions()  # HANS_QUESTIONS_ROUTING_V1
                self._maybe_push_questions()  # TELEGRAM_QUESTIONS_V1
                self._maybe_calendar_reminders()  # HANS_CALENDAR_V1
                self._maybe_deliver_requested_art()  # HANS_ART_REQUEST_V1
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
            if _intent == "paint":
                # HANS_ART_REQUEST_V1 — označ, že tento uživatel čeká na obraz;
                # NEvracej se → padne do chatu (namaluj příkaz → render na pozadí),
                # výsledek pak doručí _maybe_deliver_requested_art.
                if not hasattr(self, "_pending_paint"):
                    self._pending_paint = {}
                self._pending_paint[cid] = time.time()
            elif _intent == "artwork":
                self._cmd_artwork(cid, self._pick_mode(text)); return
            if _intent == "diary":
                self._cmd_diary(cid); return
            if _intent == "status":
                self._cmd_status(cid); return
            if _intent == "musing":
                self._cmd_musing(cid); return
            if _intent == "wol":
                self._cmd_wol(cid); return
            if _intent == "pcoff":
                self._cmd_pcoff(cid); return
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

    def send_video(self, file_path: str, caption: str = "",
                   chat_id: str = None) -> bool:
        """HANS_GUARD_RECORD_V1 — pošle video (sendVideo). Nikdy nevyhodí.

        Proč video a ne odkaz: Pi je v LAN, odkaz je z dovolené nedostupný.
        Telegram bot smí do 50 MB; minuta 640×360 @10fps ≈ jednotky MB.
        """
        if not self.enabled or not file_path or not os.path.exists(file_path):
            return False
        cid = chat_id or self.chat_id
        if not cid:
            return False
        try:
            size_mb = os.path.getsize(file_path) / (1024 * 1024)
            if size_mb > 49:
                _log.warning("telegram video %.1f MB > limit → neposílám", size_mb)
                return False
            with open(file_path, "rb") as f:
                r = requests.post(
                    _API % (self.token, "sendVideo"),
                    data={"chat_id": cid, "caption": (caption or "")[:1000]},
                    files={"video": f}, timeout=120)
            ok = bool(r.ok and (r.json() or {}).get("ok"))
            if not ok:
                _log.warning("telegram sendVideo selhal: %s", r.text[:200])
            return ok
        except Exception as e:
            _log.warning("telegram sendVideo: %s", e)
            return False

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
        if cmd in ("vypnipc", "shutdown", "pcoff", "vypnout"):
            self._cmd_pcoff(cid); return True
        if cmd in ("herni", "herní", "hra", "game", "hrani", "hraní"):
            self._cmd_game(cid, text); return True
        # HANS_TELEGRAM_INSPECT_V1 — read-only inspekční příkazy z registru
        # chat_commands (ukaž, co Hans napsal/vytvořil). Whitelist = jen bezpečné
        # „ukaž" příkazy; NIKDY ne destruktivní (/zapomen reset, /sleep, /enroll…).
        if self._route_inspect_command(text, cmd, cid):
            return True
        if cmd in ("help", "pomoc", "napoveda", "nápověda", "start"):
            self.send("Většinu věcí mi můžete říct i normální řečí — sám poznám, "
                      "co chcete, a nabídnu to (s potvrzením). Např.:\n"
                      "  „pusť Smrtonosnou past\" · „přidej na nákup mléko\" ·\n"
                      "  „nastuduj něco o hradech\" · „jaké je počasí\" ·\n"
                      "  „kdo je doma\" · „co hraje\" · „běž spát\"\n\n"
                      "Příkazy:\n"
                      "/kalendar — nadcházející události (z vašeho Proton kalendáře)\n"
                      "/seznam — můj seznam poznámek/úkolů (/seznam hotovo N, /seznam smaz N)\n"
                      "/obraz — pošlu poslední obraz (napiš „náhodný obraz\" pro náhodný)\n"
                      "/denik — výpis z mého deníku\n"
                      "/uvaha — má poslední úvaha\n"
                      "/stav — stav systému (teplota, RAM, disk, mozek)\n"
                      "/studium — můj studijní program (co studuji)\n"
                      "/dilo — mé autorské dílo na pokračování\n"
                      "/napad — mé vlastní postřehy (synteze)\n"
                      "/kritika — co u sebe chci zlepšit (sebekritika)\n"
                      "/wol — probudím počítač (PC) přes síť\n"
                      "/vypnipc — vypnu počítač (PC)\n"
                      "/herni — herní mód: uvolním grafiku pro hru (/herni vyp = zpět)\n"
                      "/hlidej — hlídám dům: při pohybu nebo změně světla pošlu "
                      "snímek (/hlidej stop = konec, /hlidej stav = jak jsem na tom)\n"
                      "/help — tato nápověda", chat_id=cid)
            return True
        return False

    # HANS_TELEGRAM_INSPECT_V1 — whitelist cmd_id z chat_commands registru,
    # které smí projít přes Telegram (vše read-only „ukaž co Hans vytvořil";
    # sub-příkazy jako „teď" spustí práci na pozadí, stejně jako lokálně).
    _INSPECT_CMDS = frozenset({"studium", "dilo", "napad", "kritika",
                               "nitky", "zajmy", "seznam", "kalendar",
                               "zdravi", "nastroj", "brief", "vytvor",
                               "prohloubit"})

    # HANS_GUARD_TELEGRAM_V1 — příkazy, které MĚNÍ chování domu (ne read-only).
    # Smí je jen role 'full'; hlavní use-case = zapnout/vypnout hlídání z dovolené.
    _MUTATING_CMDS = frozenset({"hlidej"})

    def _route_inspect_command(self, text: str, cmd: str, cid: str) -> bool:
        """Zkusí obsloužit inspekční slash příkaz přes chat_commands. True když ano."""
        try:
            from scripts.chat_commands import parse_command, dispatch
        except Exception:
            return False
        resolved = parse_command(text)  # (cmd_id, args) | None
        if not resolved:
            return False
        _cid_ = resolved[0]
        if _cid_ not in self._INSPECT_CMDS and _cid_ not in self._MUTATING_CMDS:
            return False
        # HANS_GUARD_TELEGRAM_V1 — mutující příkaz smí jen role 'full'.
        if _cid_ in self._MUTATING_CMDS and not self._is_full(cid):
            self.send("Tenhle příkaz vám bohužel provést nemohu, pane.",
                      chat_id=cid)
            return True
        if self._handler is None:
            self.send("Tenhle příkaz teď neumím obsloužit (chybí spojení s mozkem).",
                      chat_id=cid)
            return True
        try:
            out = dispatch(resolved, self._handler, self._person_for(cid))
        except Exception as e:
            _log.warning("telegram inspect dispatch selhal: %s", e)
            out = None
        self.send(out or "Zatím k tomu nic nemám, pane.", chat_id=cid)
        return True

    def _cmd_game(self, cid: str, text: str = ""):
        """OLLAMA_GAME_MODE_V1 — herní mód přes Telegram. /herni [zap|vyp|stav]."""
        try:
            from scripts.ollama_client import set_game_mode, game_mode_on
        except Exception as e:
            self.send("Herní mód není dostupný: %s" % e, chat_id=cid); return
        parts = (text or "").lower().split()
        arg = parts[1] if len(parts) > 1 else ""
        if arg in ("stav", "status"):
            self.send("Herní mód je ZAPNUTÝ (grafika volná pro hru)."
                      if game_mode_on() else "Herní mód je vypnutý.", chat_id=cid)
            return
        if arg in ("zap", "on", "1", "ano", "start"):
            target = True
        elif arg in ("vyp", "off", "0", "ne", "stop", "konec"):
            target = False
        else:
            target = not game_mode_on()   # toggle
        res = set_game_mode(target, config=self.config)
        if isinstance(res, dict) and "error" in res:
            self.send("Herní mód selhal: %s" % res["error"], chat_id=cid); return
        if target:
            self.send("Herní mód ZAPNUT — uvolnil jsem %d model(ů) z grafické "
                      "paměti, grafika je volná pro hru. Až dohraješ, pošli "
                      "/herni vyp." % (res.get("unloaded", 0) if isinstance(res, dict) else 0),
                      chat_id=cid)
        else:
            self.send("Herní mód VYPNUT — mozek je zase k dispozici.", chat_id=cid)

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
            # HANS_ART_ORIGIN_V1 — popisek = původ díla + kritika, ne jen kritika.
            cap = (note or "Můj obraz").strip()
            try:
                from scripts.hans_art import origin_line as _origin
                con2 = sqlite3.connect("file:%s?mode=ro" % self._diary_path(),
                                       uri=True, timeout=3.0)
                _t = con2.execute(
                    "SELECT title FROM diary WHERE event_type='artwork' "
                    "AND data = ? ORDER BY ts DESC LIMIT 1", (data,)).fetchone()
                con2.close()
                _o = _origin(_t[0] if _t else "", data)
            except Exception:
                _o = ""
            if _o:
                cap = f"{_o}\n\n{cap}"
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
        # HANS_ART_REQUEST_V1 — „namaluj/nakresli [téma]" = maluj NOVÝ obraz
        # (a pošli výsledek), NE poslat poslední. Rozlišit od „pošli obrázek".
        if re.search(r"namaluj|nakresli|namalovat|nakreslit|vytvoř\w*\s+obr", t):
            return "paint"
        if re.search(r"obraz|obrázek|obrazek|namaloval|malb", t) and re.search(self._ASK, t):
            return "artwork"
        if re.search(r"den[ií]k|deníč|zápis", t) and re.search(self._ASK, t):
            return "diary"
        if re.search(r"\bstav\b|teplot|kolik.*ram|\bcpu\b|vyt[íi]ž|jak.*ti to běž", t):
            return "status"
        if re.search(r"úvah|uvah|myšlenk|myslenk", t) and re.search(self._ASK, t):
            return "musing"
        if re.search(r"(vypni|vypnout|zhasni).*(pc|počítač|pocitac|mozek)|"
                     r"\bshutdown\b", t):
            return "pcoff"
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

    def _cmd_pcoff(self, cid: str):
        """HANS_PC_SHUTDOWN_CMD_V1 — protějšek /wol: vypni PC na povel."""
        try:
            from scripts.chat_commands import _cmd_vypnipc
        except Exception:
            self.send("Na počítač teď nedosáhnu.", chat_id=cid)
            return
        self.send("Posílám počítači povel k vypnutí. Ověřím, zda zhasl…",
                  chat_id=cid)

        def _work():
            try:
                msg = _cmd_vypnipc(self._handler, None, "")
            except Exception as e:
                msg = "Vypnutí se nezdařilo: %s" % e
            self.send(msg, chat_id=cid)

        threading.Thread(target=_work, daemon=True).start()

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
            from scripts.ollama_client import game_mode_on
            lines.append("Herní mód: " + ("ZAPNUT (grafika uvolněna pro hru)"
                                           if game_mode_on() else "vypnut"))
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
