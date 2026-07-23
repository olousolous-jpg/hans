"""HANS_MATRIX_V1 — most na Matrix (matrix.org) s E2E šifrováním.

Proč: Telegram bot komunikace NENÍ end-to-end (servery vidí obsah v plaintextu,
včetně snímků z /hlidej). Matrix E2E se děje na KLIENTOVI → homeserver dostane
jen ciphertext. Cíl: data uložená u poskytovatele nečitelná.

Návrh zrcadlí `hans_telegram.TelegramBridge`:
  - OUTBOUND: send / send_proactive / send_photo / send_video
  - INBOUND: sync callback → handler.send_chat_message(person, text, channel="matrix")
  - quiet hours (v noci nepípat), multiuser mapování (user_id → osoba/role)
tak, aby oba mosty šly později pod společnou abstrakci (Notifier).

matrix-nio je async; Hans je vláknový/synchronní → asyncio smyčka běží ve
VLASTNÍM vlákně a synchronní metody na ni plánují korutiny (run_coroutine_threadsafe).

⚠️ E2E stav (olm sessions, device keys) MUSÍ persistovat (store_path) — jinak se
při každém startu vytvoří nové zařízení a šifrování se rozsype. Login token se
ukládá taky, ať se nepřihlašujeme znovu (nový device = E2E churn).

Závislosti: python3-matrix-nio, python3-olm (apt, NE pip — PEP668).
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import sqlite3
import threading
import time

_log = logging.getLogger("hans.matrix")

try:
    from nio import (AsyncClient, AsyncClientConfig, InviteMemberEvent,
                     LoginResponse, RoomMessageText, UploadResponse)
    _NIO_OK = True
except Exception as _e:  # pragma: no cover
    AsyncClient = None
    _NIO_OK = False
    _log.warning("matrix-nio nedostupné: %s", _e)


class MatrixBridge:
    def __init__(self, config: dict, chat_handler=None):
        cfg = (config.get("matrix", {}) or {})
        self.config = config or {}
        self._handler = chat_handler
        self.homeserver = str(cfg.get("homeserver", "https://matrix.org") or
                              "https://matrix.org")
        self.user_id = str(cfg.get("user_id", "") or "")      # @hansbot:matrix.org
        self.password = str(cfg.get("password", "") or "")    # jen 1. přihlášení
        self.room_id = str(cfg.get("room_id", "") or "")      # primární místnost
        self.as_person = str(cfg.get("as_person", "") or "uživatel")
        self.device_name = str(cfg.get("device_name", "hans") or "hans")
        self.store_path = str(cfg.get("store_path", "data/matrix_store") or
                              "data/matrix_store")
        # multiuser: user_id → {person, role}. role 'full' | 'chat' (jako Telegram).
        # Primární user_id (room owner) se bere z místnosti; povolené protějšky
        # se konfigurují v matrix.users (koho bot poslouchá).
        self._users: dict = {}
        for u in (cfg.get("users", []) or []):
            uid = str((u or {}).get("user_id", "") or "")
            if not uid:
                continue
            self._users[uid] = {
                "person": str((u or {}).get("as_person", "") or
                              self.as_person).lower(),
                "role": str((u or {}).get("role", "full") or "full"),
            }
        self._announce = bool(cfg.get("announce_online", False))
        self.enabled = (bool(cfg.get("enabled", False)) and _NIO_OK and
                        bool(self.user_id) and
                        bool(self.password or self._creds_exist()))

        # quiet hours (TELEGRAM_QUIET_HOURS_V1 paralela)
        self._quiet_start = int(cfg.get("quiet_start_hour", 22))
        self._quiet_end = int(cfg.get("quiet_end_hour", 9))
        self._deferred: list = []

        self._client = None
        self._loop = None
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()   # set až po prvním úspěšném sync (E2E keys)
        self._started_ts = 0.0            # ignoruj zprávy starší než start
        self._pending_brain_notify = False  # HANS_TELEGRAM_BRAIN_NOTIFY_V1 parita
        self._cmd_state: dict = {}          # per-most stav příkazů (pcoff/paint)
        # proaktivní doručování (HANS_MATRIX_PROACTIVE_V1) — stav/throttly
        self._q_store = None
        self._last_q_push = 0.0
        self._last_cal_check = 0.0
        self._last_art_check = 0.0
        self._last_art_id = None
        self._proactive_running = False

        if cfg.get("enabled", False) and not self.enabled:
            if not _NIO_OK:
                _log.warning("matrix: matrix-nio nedostupné → vypnuto")
            elif not self.user_id:
                _log.warning("matrix: enabled, ale chybí user_id → vypnuto")
            elif not (self.password or self._creds_exist()):
                _log.warning("matrix: chybí password i uložený token → vypnuto")

    # ── credentials persistence ─────────────────────────────────────────────
    def _creds_file(self) -> str:
        return os.path.join(self.store_path, "credentials.json")

    def _creds_exist(self) -> bool:
        try:
            return os.path.getsize(self._creds_file()) > 0
        except OSError:
            return False

    def _load_creds(self):
        try:
            with open(self._creds_file(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def _save_creds(self, user_id: str, device_id: str, access_token: str):
        try:
            os.makedirs(self.store_path, exist_ok=True)
            with open(self._creds_file(), "w", encoding="utf-8") as f:
                json.dump({"user_id": user_id, "device_id": device_id,
                           "access_token": access_token}, f)
            os.chmod(self._creds_file(), 0o600)  # token = tajné
        except Exception as e:
            _log.warning("matrix: uložení creds selhalo: %s", e)

    # ── helpers ─────────────────────────────────────────────────────────────
    def _person_for(self, uid: str) -> str:
        u = self._users.get(str(uid))
        return u["person"] if u else self.as_person

    def _is_full(self, uid: str) -> bool:
        u = self._users.get(str(uid))
        return (u.get("role", "full") == "full") if u else True

    def _in_quiet_hours(self) -> bool:
        h = time.localtime().tm_hour
        if self._quiet_start == self._quiet_end:
            return False
        if self._quiet_start < self._quiet_end:
            return self._quiet_start <= h < self._quiet_end
        return h >= self._quiet_start or h < self._quiet_end

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self):
        if not self.enabled or self._thread:
            return
        self._stop.clear()
        self._started_ts = time.time()
        self._thread = threading.Thread(target=self._run, name="matrix",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._loop and self._client:
            try:
                asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
            except Exception:
                pass

    def _run(self):
        """Vlastní asyncio smyčka ve vlákně."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as e:
            _log.warning("matrix smyčka spadla: %s", e)
        finally:
            try:
                self._loop.close()
            except Exception:
                pass

    async def _main(self):
        os.makedirs(self.store_path, exist_ok=True)
        conf = AsyncClientConfig(store_sync_tokens=True,
                                 encryption_enabled=True)
        creds = self._load_creds()
        device_id = (creds or {}).get("device_id") or None
        self._client = AsyncClient(self.homeserver, self.user_id,
                                   device_id=device_id,
                                   store_path=self.store_path, config=conf)

        # login: obnovit token, jinak heslem
        if creds and creds.get("access_token"):
            self._client.restore_login(
                user_id=creds["user_id"], device_id=creds["device_id"],
                access_token=creds["access_token"])
            _log.info("matrix: obnovena relace (device %s)", creds["device_id"])
        else:
            resp = await self._client.login(self.password,
                                            device_name=self.device_name)
            if not isinstance(resp, LoginResponse):
                _log.warning("matrix login selhal: %s", resp)
                return
            self._save_creds(resp.user_id, resp.device_id, resp.access_token)
            _log.info("matrix: přihlášeno, nový device %s", resp.device_id)

        # store se načte automaticky (store_path); nahraj klíče, pokud třeba
        if self._client.should_upload_keys:
            await self._client.keys_upload()

        # zobrazované jméno = persona (typing pak čte „Hans píše…", ne login bota)
        try:
            _pname = str(((self.config.get("persona", {}) or {}).get("name"))
                         or "Hans")
            await self._client.set_displayname(_pname)
        except Exception as e:
            _log.debug("matrix set_displayname: %s", e)

        # registrace callbacků PŘED úvodním sync
        self._client.add_event_callback(self._on_message, RoomMessageText)
        # auto-přijetí pozvánky (jen od povolených user_id, nebo když seznam
        # prázdný = důvěřuj — jednouživatelský setup). Bez toho by se bot
        # musel do místnosti připojit ručně.
        self._client.add_event_callback(self._on_invite, InviteMemberEvent)

        # úvodní sync — načte device listy + stav místností (nutné pro E2E send)
        await self._client.sync(timeout=30000, full_state=True)
        self._ready.set()
        _log.info("matrix: E2E připraveno, naslouchám")

        if self._announce and self.room_id:
            await self._a_send("Hans je k dispozici (Matrix, šifrovaně).",
                               self.room_id)

        # hlavní smyčka
        while not self._stop.is_set():
            try:
                self._flush_deferred()
                # proaktivní doručování běží v EXECUTORU (self.send blokuje na
                # run_coroutine_threadsafe().result → z loop vlákna deadlock).
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._proactive_tick)
                except Exception:
                    pass
                await self._client.sync(timeout=30000)
            except Exception as e:
                _log.debug("matrix sync: %s", e)
                await asyncio.sleep(5)

    # ── INBOUND ─────────────────────────────────────────────────────────────
    async def _on_invite(self, room, event):
        """Auto-join na pozvánku (jen když zve povolený user_id, nebo bez
        whitelistu). state_key == náš user_id = pozvánka pro nás."""
        try:
            if getattr(event, "state_key", "") != self._client.user_id:
                return
            inviter = getattr(event, "sender", "")
            if self._users and inviter not in self._users:
                _log.warning("matrix: pozvánka od neznámého %s → ignoruji",
                             inviter)
                return
            await self._client.join(room.room_id)
            _log.info("matrix: připojen do místnosti %s (pozval %s)",
                      room.room_id, inviter)
        except Exception as e:
            _log.warning("matrix on_invite: %s", e)

    async def _on_message(self, room, event):
        try:
            # ignoruj vlastní zprávy a historii před startem
            if event.sender == self._client.user_id:
                return
            if getattr(event, "server_timestamp", 0) / 1000.0 < self._started_ts:
                return
            uid = event.sender
            text = (event.body or "").strip()
            if not text:
                return
            if self._users and uid not in self._users:
                _log.warning("matrix: zpráva od neznámého %s — ignoruji", uid)
                return
            person = self._person_for(uid)
            _log.info("matrix ← %s: %.60s", person, text)

            # HANS_BRIDGE_COMMANDS_V1 — příkazy/intenty (jen role 'full'), stejné
            # co Telegram. Běží v EXECUTORU: ctx.send volá self.send, které blokuje
            # na run_coroutine_threadsafe().result() → z loop vlákna by deadlocklo.
            rid = room.room_id
            if self._is_full(uid):
                try:
                    from scripts import bridge_commands as _bc
                    ctx = _bc.BridgeCtx(
                        send=lambda t, _r=rid: self.send(t, _r),
                        send_photo=lambda p, c="", _r=rid: self.send_photo(p, c, _r),
                        person=person, is_full=True, handler=self._handler,
                        config=self.config, state=self._cmd_state)
                    loop = asyncio.get_event_loop()
                    handled = await loop.run_in_executor(None, _bc.handle, text, ctx)
                    if handled:
                        return
                except Exception as e:
                    _log.warning("matrix bridge_commands: %s", e)

            self._pending_brain_notify = True  # brain-up notify (parita s TG)
            reply = await self._reply_with_typing(rid, person, text)
            if reply:
                await self._a_send(reply, room.room_id)
            else:
                await self._a_send(
                    "Můj mozek (počítač s jazykovým centrem) teď spí, takže "
                    "vám nedokážu pořádně odpovědět. Dám vědět, jakmile budu "
                    "opět online.", room.room_id)
        except Exception as e:
            _log.warning("matrix on_message: %s", e)

    def _safe_handle(self, person: str, text: str):
        """Blokující inference mozku — běží v executoru, ať nezamrzne smyčka."""
        try:
            if self._handler is not None and hasattr(self._handler,
                                                     "send_chat_message"):
                return self._handler.send_chat_message(person, text,
                                                       channel="matrix")
        except Exception as e:
            _log.warning("matrix → chat selhal: %s", e)
        return None

    async def _reply_with_typing(self, room_id: str, person: str, text: str):
        """HANS_MATRIX_TYPING_V1 — pošli typing indikátor („Hans píše…"), pusť
        inference v thread poolu (neblokuje event loop → sync běží dál) a typing
        obnovuj, dokud mozek nedomyslí. Server typing timeout je 20 s → obnova á
        15 s pokryje i dlouhou inferenci. Na konci typing vypni."""
        loop = asyncio.get_event_loop()
        fut = loop.run_in_executor(None, self._safe_handle, person, text)
        try:
            while True:
                try:
                    await self._client.room_typing(room_id, True, timeout=20000)
                except Exception:
                    pass
                done, _ = await asyncio.wait({fut}, timeout=15)
                if fut in done:
                    break
        finally:
            try:
                await self._client.room_typing(room_id, False)
            except Exception:
                pass
        try:
            return fut.result()
        except Exception:
            return None

    # ── OUTBOUND (async jádro) ──────────────────────────────────────────────
    async def _a_send(self, text: str, room_id: str) -> bool:
        try:
            await self._client.room_send(
                room_id=room_id, message_type="m.room.message",
                content={"msgtype": "m.text", "body": text[:16000]},
                ignore_unverified_devices=True)
            return True
        except Exception as e:
            _log.warning("matrix room_send selhal: %s", e)
            return False

    async def _a_send_file(self, path: str, caption: str, room_id: str,
                           msgtype: str) -> bool:
        try:
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            name = os.path.basename(path)
            # Načti CELÝ soubor do paměti PŘED uploadem — event loop je za běhu
            # zaneprázdněný syncem; čtení souboru po kouskách během awaitů by se
            # mohlo prokládat. Data v paměti + data_provider callable = jeden
            # čistý zdroj, retry-safe. (guard videa ~0,4 MB, obrazy ~1,5 MB.)
            with open(path, "rb") as f:
                data = f.read()
            size = len(data)
            info = {"mimetype": mime, "size": size}
            if msgtype == "m.image":
                try:
                    import io as _io
                    from PIL import Image as _Image
                    with _Image.open(_io.BytesIO(data)) as _im:
                        info["w"], info["h"] = _im.size
                except Exception:
                    pass
            resp, keys = await self._client.upload(
                lambda *_a: data, content_type=mime, filename=name,
                encrypt=True, filesize=size)
            if not isinstance(resp, UploadResponse):
                _log.warning("matrix upload selhal: %s", resp)
                return False
            # E2E místnost → šifrovací info v 'file' (ne 'url'); body = název
            # souboru (technický), čitelný popisek jde SAMOSTATNOU zprávou.
            keys["url"] = resp.content_uri
            content = {"msgtype": msgtype, "body": name, "info": info,
                       "file": keys}
            await self._client.room_send(
                room_id=room_id, message_type="m.room.message",
                content=content, ignore_unverified_devices=True)
            if caption:
                await self._a_send(caption, room_id)
            return True
        except Exception as e:
            _log.warning("matrix send_file selhal: %s", e)
            return False

    # ── OUTBOUND (synchronní API — zrcadlí TelegramBridge) ──────────────────
    def _dispatch(self, coro, timeout: float = 30.0) -> bool:
        if not self._loop:
            return False
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return bool(fut.result(timeout=timeout))
        except Exception as e:
            _log.warning("matrix dispatch: %s", e)
            return False

    def send(self, text: str, room_id: str = None) -> bool:
        if not self.enabled or not (text or "").strip():
            return False
        rid = room_id or self.room_id
        if not rid:
            _log.warning("matrix send: chybí room_id")
            return False
        return self._dispatch(self._a_send(text, rid))

    def send_proactive(self, text: str, room_id: str = None) -> bool:
        """V tichém okně odloží, jinak pošle."""
        if not self.enabled or not (text or "").strip():
            return False
        rid = room_id or self.room_id
        if self._in_quiet_hours():
            self._deferred.append((text, rid))
            _log.info("matrix: proaktivní zpráva ODLOŽENA do %d:00", self._quiet_end)
            return True
        return self.send(text, room_id=rid)

    def _flush_deferred(self):
        if not self._deferred or self._in_quiet_hours():
            return
        pending = self._deferred
        self._deferred = []
        for txt, rid in pending:
            try:
                asyncio.run_coroutine_threadsafe(self._a_send(txt, rid),
                                                 self._loop)
            except Exception:
                pass
        _log.info("matrix: doručeno %d odložených zpráv", len(pending))

    def send_photo(self, file_path: str, caption: str = "",
                   room_id: str = None) -> bool:
        if not self.enabled or not os.path.exists(file_path):
            return False
        rid = room_id or self.room_id
        if not rid:
            return False
        return self._dispatch(
            self._a_send_file(file_path, caption, rid, "m.image"), timeout=60)

    def send_video(self, file_path: str, caption: str = "",
                   room_id: str = None) -> bool:
        if not self.enabled or not os.path.exists(file_path):
            return False
        rid = room_id or self.room_id
        if not rid:
            return False
        return self._dispatch(
            self._a_send_file(file_path, caption, rid, "m.video"), timeout=120)

    # ── PROAKTIVNÍ DORUČOVÁNÍ (HANS_MATRIX_PROACTIVE_V1) ─────────────────────
    # Běží v EXECUTORU ze sync smyčky (worker vlákno → self.send/send_photo přes
    # run_coroutine_threadsafe fungují). Port z hans_telegram poll loopu,
    # zjednodušený na JEDNU místnost (self.room_id, majitel = self.as_person).
    def _questions(self):
        if self._q_store is None:
            try:
                from scripts.hans_questions import HansQuestionsStore
                dp = ((self.config.get("hans_idle", {}) or {}).get("diary_db")
                      or self.config.get("diary_db") or "data/hans_diary.db")
                self._q_store = HansQuestionsStore(dp, self.config)
            except Exception as e:
                _log.warning("matrix HansQuestionsStore init: %s", e)
        return self._q_store

    def _proactive_tick(self):
        if self._proactive_running or not self.room_id:
            return
        self._proactive_running = True
        try:
            self._maybe_deliver_requested_art()
            self._maybe_calendar_reminders()
            self._maybe_push_questions()
        except Exception as e:
            _log.debug("matrix proactive: %s", e)
        finally:
            self._proactive_running = False

    def _maybe_deliver_requested_art(self):
        """Doruč obraz jen když si o něj uživatel řekl přes Matrix („namaluj…").
        Autonomní malování (sny/den) se NEposílá. Doručí HNED po domalování
        (poll á 15 s); `art_wait_minutes` (default 20) je jen deadline pro vzdání,
        když render nikdy nedoběhne."""
        paint = self._cmd_state.get("paint") or {}
        if not paint.get("pending"):
            return
        now = time.time()
        if now - self._last_art_check < 15:
            return
        self._last_art_check = now
        # render může trvat i přes 6 min (SDXL na PC + fronta) → konfigurovatelné
        # okno, default 20 min. Kratší okno = časté „vypršel limit", i když se
        # obraz nakonec dokreslí (viditelný přes /obraz).
        wait_s = float((self.config.get("matrix", {}) or {}).get(
            "art_wait_minutes", 20)) * 60
        if now - paint["pending"] > wait_s:  # render se nepovedl / trvá moc dlouho
            self._cmd_state.pop("paint", None)
            self.send("Obraz se mi teď nepodařilo vytvořit, pane. "
                      "Zkuste to prosím znovu.")
            return
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path(),
                                  uri=True, timeout=3.0)
            if self._last_art_id is None:
                row = con.execute("SELECT MAX(rowid) FROM diary WHERE "
                                  "event_type='artwork'").fetchone()
                self._last_art_id = (row[0] if row and row[0] else 0)
            rows = con.execute(
                "SELECT rowid, title, COALESCE(note,''), COALESCE(data,'') "
                "FROM diary WHERE event_type='artwork' AND rowid > ? "
                "ORDER BY rowid ASC LIMIT 3", (self._last_art_id,)).fetchall()
            con.close()
        except Exception as e:
            _log.debug("matrix art deliver query: %s", e)
            return
        if not rows:
            return
        rid_, title, note, data = rows[-1]
        self._last_art_id = rid_
        path = ""
        try:
            path = (json.loads(data) or {}).get("path", "") if data else ""
        except Exception:
            path = ""
        cap = f"🎨 Hotovo — {title}"
        try:
            from scripts.hans_art import origin_line as _origin
            _o = _origin(title, data)
        except Exception:
            _o = ""
        if _o:
            cap += f"\n{_o}"
        if note:
            cap += f"\n\n{note[:200]}"
        self._cmd_state.pop("paint", None)
        if path and os.path.exists(path):
            self.send_photo(path, cap)
        else:
            self.send(cap)
        _log.info("matrix: vyžádaný obraz → %.40s", title)

    def _diary_path(self) -> str:
        return ((self.config.get("hans_idle", {}) or {}).get("diary_db")
                or self.config.get("diary_db") or "data/hans_diary.db")

    def _maybe_calendar_reminders(self):
        """HANS_CALENDAR_V1 — připomeň blížící se události majiteli místnosti
        (self.as_person). Throttle 5 min; mark_reminded brání opakování."""
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
        if now - self._last_cal_check < 300:
            return
        self._last_cal_check = now
        try:
            store = CalendarStore(self.config, self._diary_path())
            due = store.due_reminders(
                lead_hours=float(cc.get("reminder_lead_hours", 2)))
            owner_name = (self.as_person or "").lower()
            for ev in (due or []):
                owner = (ev.get("person") or "").lower()
                if owner and owner != owner_name:
                    continue  # cizí kalendář → neposílej do téhle místnosti
                text = store.reminder_text(ev)
                if self.send_proactive(text):
                    store.mark_reminded(owner or owner_name, ev["uid"],
                                        ev["start_ts"])
                    _log.info("matrix: připomínka → %.50s", text)
        except Exception as e:
            _log.debug("matrix calendar reminders: %s", e)

    def _maybe_push_questions(self):
        """Hans pošle svou otázku majiteli místnosti. Reuse „telegram" fáze
        otázek (= push-na-telefon kanál, teď obsluhovaný Matrixem). Throttle
        matrix.question_interval_h (default 6 h)."""
        interval = float((self.config.get("matrix", {}) or {}).get(
            "question_interval_h", 6)) * 3600
        now = time.time()
        if now - self._last_q_push < interval:
            return
        qs = self._questions()
        if qs is None:
            return
        self._last_q_push = now  # i prázdný pokus = počkej interval
        try:
            q = qs.next_for_channel(self.as_person, "telegram",
                                    only_undelivered=True)
        except Exception as e:
            _log.debug("matrix next_for_channel: %s", e)
            return
        if not q or not (q.question or "").strip():
            return
        if self.send_proactive(q.question):
            try:
                qs.mark_channel_delivered(q.id)
            except Exception as e:
                _log.debug("matrix mark_channel_delivered: %s", e)
            _log.info("matrix: otázka → %s: %.60s", self.as_person, q.question)


# ── izolovaný test harness ──────────────────────────────────────────────────
# python3 -m scripts.hans_matrix           → přihlásí bota, echo příchozích zpráv
# python3 -m scripts.hans_matrix "ahoj"    → pošle text do room_id z configu
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            _cfg = json.load(f)
    except Exception as e:
        print(f"config.json nelze načíst: {e}")
        sys.exit(1)

    class _Echo:
        def send_chat_message(self, person, text, channel=None):
            print(f"[HANDLER] {person} přes {channel}: {text}")
            return f"Slyším tě, {person}: „{text}“ (echo test)"

    br = MatrixBridge(_cfg, _Echo())
    if not br.enabled:
        print("MatrixBridge NENÍ enabled — zkontroluj config.matrix "
              "(enabled/user_id/password).")
        sys.exit(1)
    br.start()
    print("Čekám na E2E ready...")
    if not br._ready.wait(timeout=60):
        print("Timeout — E2E se nerozjelo do 60 s.")
        sys.exit(1)
    print("READY. Bot naslouchá.")

    if len(sys.argv) > 1:
        ok = br.send(" ".join(sys.argv[1:]))
        print(f"send → {ok}")
        time.sleep(2)
    else:
        print("Napiš botovi z Elementu — objeví se echo. Ctrl-C ukončí.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    br.stop()
