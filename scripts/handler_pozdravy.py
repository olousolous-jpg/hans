"""Metody třídy `OpenWebUIDirectHandler` přesunuté z `scripts/openwebui_direct_handler.py` (ROZDELENI_METOD_V1).

Text metod je beze změny; jména původního modulu se čtou přes `_h.` až při
volání. `OpenWebUIDirectHandler` má třídu `PozdravyMixin` mezi předky, takže volání přes `self` platí dál.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from datetime import datetime, date
import json
import os
import threading


class PozdravyMixin:
    """Část třídy `OpenWebUIDirectHandler` — viz hlavička modulu."""

    def _generate_greeting_prompt(self, name: str, duvod=None) -> tuple:
        self._greeting_thread_surfaced = False  # GREETING_THREAD_POPUP_V1
        hour = datetime.now().hour
        if 5  <= hour < 12: tod = "ráno"
        elif 12 <= hour < 17: tod = "odpoledne"
        elif 17 <= hour < 22: tod = "večer"
        else:                  tod = "v noci"

        greeting_cfg  = self.config.get("greeting", {})
        system = self._build_system(name, for_greeting=True) + (
            " Pozdrav stručně a důstojně: nanejvýš dvě krátké věty,"
            " žádná dlouhá souvětí.")  # GREETING_BREVITY_V1
        if duvod:  # HANS_GESTURE_WAVE_GREET_V1
            system += (" Zdravíš proto, že ti %s. Odpověz JEN krátkým"
                       " pozdravem, jedinou větou." % duvod)
        # Přidej náladu do tónu pozdravu
        _hi2 = getattr(self, '_hans_idle', None)
        if _hi2 and hasattr(_hi2, '_mood'):
            _mp = _hi2._mood.get_prompt_addition()
            if _mp:
                system += " " + _mp

        # Sestav co Hans skutečně dělal — rotuje, neopakuje se
        _hi = getattr(self, '_hans_idle', None)
        _activity_hint = ""

        # Sbírej kandidáty ze všech zdrojů
        _candidates: list[str] = []

        if _hi:
            # Vnitřní myšlenky
            if hasattr(_hi, '_introspection'):
                _candidates.extend(_hi._introspection._recent_thoughts[:4])

            # Co četl
            if hasattr(_hi, '_curiosity') and _hi._curiosity._recent:
                for _r in _hi._curiosity._recent[:4]:
                    _candidates.append(
                        f"četl jsem o tématu '{_r.title}': {_r.summary[:80]}")

            # Filmy z deníku
            try:
                rows = _hi._db.execute(
                    "SELECT title FROM diary WHERE event_type='movie_browsed' "
                    "ORDER BY ts DESC LIMIT 5"
                ).fetchall()
                for (t,) in rows:
                    _candidates.append(f"přemýšlel jsem o filmu '{t}'")
            except Exception:
                pass

        # Majordomus aktivity — věrohodné věci které Hans dělá
        import random as _rnd
        _butler = [
            "přeleštil jsem stříbro — odraz svíček je nyní uspokojivý",
            "zkontroloval jsem zásoby čaje a doplnil anglický breakfast",
            "seřadil jsem knihy v knihovně podle roku vydání",
            "přeložil jsem přikrývky v ložnici podle pravidel správné domácnosti",
            "zkontroloval jsem okenní závěsy — prach se hromadí nenápadně",
            "naostřil jsem nože v kuchyni — tupý nůž je nehodný domácnosti",
            "zapsal jsem poznámky o stavu domácnosti do zásobní knihy",
            "přelil jsem květiny — mírně, jak se sluší",
            "zkontroloval jsem hodiny v každé místnosti — musí jít shodně",
            "upravil jsem polohu obrazů — symetrie je základem důstojnosti",
            "vyčistil jsem příborník a seřadil příbory podle protokolu",
            "prověřil jsem stav svíček — vždy musí být připraveny",
            "zkontroloval jsem zásoby whisky a zaznamenal stav do knihy",
            "přemýšlel jsem o správném pořadí chodu při příští večeři",
            "zkontroloval jsem teploměr — správná teplota místnosti je 18 stupňů",
        ]
        # Přidej majordomus aktivity jako menšinové kandidáty (1 z 3)
        # aby převažovaly skutečné zážitky ale butler věci se občas objevily
        if _candidates:
            _candidates.extend(_rnd.sample(_butler, min(2, len(_butler))))
        else:
            _candidates = _butler[:]

        # Vyber kandidáta který ještě nebyl použit
        _unused = [c for c in _candidates if c not in self._used_hints]
        if not _unused:
            # Všechno bylo použito — resetuj paměť a začni znovu
            self._used_hints.clear()
            _unused = _candidates

        if _unused:
            _activity_hint = _rnd.choice(_unused)
            # Zapamatuj si co bylo řečeno (max 10 položek)
            self._used_hints.append(_activity_hint)
            if len(self._used_hints) > 10:
                self._used_hints.pop(0)

        # GREETING_WEATHER_OPTIN_V1 — kdo dostává počasí v pozdravu (dle configu).
        # NE natvrdo šablona: jde normální greeting cestou (aktivita/nitky);
        # počasí se přidá až dole a JEN když je reálně zjištěné.
        _special = self.config.get("greeting", {}).get("special_greetings", {})
        _wants_weather = name.lower() in [k.lower() for k in _special]

        user_template = greeting_cfg.get("user_prompt",
                        "Pozdrav hosta jménem {name} jednou větou. Je {tod}.")
        user = user_template.format(name=name, tod=tod)

        # GREETING_LEAD_PRIORITY_V1 — pozdrav vede JEDINOU proaktivní věcí, ať
        # se do dvouvětého pozdravu nemíchá víc nesouvisejících háčků. Pořadí:
        # výpadek > rozjetá nitka > ranní zdraví > co Hans dělal.
        _lead = False

        # 0) HANS_GREET_REASON_LEAD_V1 (6.9.) — POZDRAV NA VYZADANI (dnes
        # zamavani gestem) vede pred vsemi ostatnimi duvody.
        # Zmereno 6.9.: `duvod` pridany jen do `system` NEZABRAL — vetev (4)
        # nize rika v `user` doslova "zmin, cemu ses venoval behem jejich
        # nepritomnosti", a model poslechne tu konkretnejsi a blizsi
        # instrukci. Vsech 8 pozdravu na mavnuti zacinalo "Behem Vasi
        # nepritomnosti...". Prompt debt: veta v promptu prohrava se
        # soupericí vetou, proto to musi byt VETEV, ne dodatek.
        if duvod:
            # Pokyn uzivatele 6.9.: na mavnuti JEN KRATKY POZDRAV. Delsi
            # uvitani (cemu se venoval, nitky, pocasi) zustava u rozpoznani
            # tvare — tam ma smysl, protoze clovek prave prisel.
            user = (
                f"Pozdrav {name} JEDNOU krátkou větou — právě {duvod}. "
                f"Je {tod}. Nic víc nepřidávej: žádnou zmínku o jeho "
                f"nepřítomnosti, o tom čemu ses věnoval, ani o počasí."
            )
            _lead = True

        # 1) HANS_DOWNTIME_V1 — byl jsem dlouho mimo provoz: přiznám a zeptám se.
        try:
            _dt_g = getattr(_hi, '_downtime', None) if _hi else None
            if _dt_g and not _dt_g.get('answered'):
                user = (
                    f"Pozdrav {name} krátce a důstojně. Je {tod}. Pak v jedné větě "
                    f"přiznej, žes byl delší dobu mimo provoz, a vlídně se zeptej, "
                    f"co se mezitím dělo. (Fakt: {_dt_g.get('sentence','')}) "
                    f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                    f"Jméno použij jen jednou na začátku."
                )
                _dt_g['surfaced'] = True  # příští zpráva osoby = vyprávění
                _lead = True
        except Exception:
            pass

        # 2) HANS_THREADS_SURFACING_V1 — navnáž na rozjetou nitku; příchod osoby
        # = nejpřirozenější moment „jak to dopadlo".
        if not _lead:
            try:
                _tstore = self._thread_store()
                _thr = _tstore.surface_for(name) if _tstore is not None else None
                if _thr is not None:
                    _fu = _thr.follow_up or f"zeptej se, jak to dopadlo s: {_thr.topic}"
                    user = (
                        f"Pozdrav {name} krátce a důstojně. Je {tod}. Pak naváž na to, "
                        f"co {name} dříve zmínil/a, a přirozeně se zeptej: {_fu} "
                        f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                        f"Jméno použij jen jednou na začátku."
                    )
                    _tstore.mark_surfaced(_thr.id)
                    self._greeting_thread_surfaced = True  # GREETING_THREAD_POPUP_V1
                    _lead = True
            except Exception as _tiche:
                from scripts.logger import tichy_zapis as _tz  # HANS_SILENT_WRITE_LOG_V1
                _tz('openwebui_direct_handler:_generate_greeting_prompt', _tiche)

        # 3) HANS_MORNING_HEALTH_V1 — ráno po chybné noci: krátká upřímná zmínka.
        if not _lead:
            try:
                _mh_g = getattr(_hi, '_morning_health', None) if _hi else None
                from datetime import datetime as _dt_mh
                if _mh_g and _mh_g.get('date') == _dt_mh.now().strftime('%Y-%m-%d'):
                    user = (
                        f"Pozdrav {name} krátce a důstojně. Je {tod}. Pak v jedné větě "
                        f"upřímně zmiň, žes ráno nebyl ve své kůži kvůli nočním "
                        f"potížím v záznamech ({_mh_g.get('summary','')}). "
                        f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                        f"Jméno použij jen jednou na začátku."
                    )
                    _lead = True
            except Exception:
                pass

        # 4) Co Hans dělal (activity hint) — výchozí, jen když nic výš nevedlo.
        if not _lead and _activity_hint:
            user = (  # GREETING_BREVITY_V1
                f"Pozdrav {name} krátce a důstojně. Je {tod}. "
                f"Pak v JEDNÉ stručné větě nenásilně zmiň, čemu ses během "
                f"jejich nepřítomnosti věnoval: {_activity_hint}. "
                f"Celkem nanejvýš dvě krátké věty, žádné dlouhé souvětí. "
                f"Jméno použij jen jednou na začátku."
            )

        # GREETING_WEATHER_OPTIN_V1 — počasí JEN když reálně zjištěné; přesná
        # citace (neodhaduj) → konec konfabulace „82 °C". Jinak nezmiňuj.
        if _wants_weather and not duvod:   # HANS_GREET_REASON_LEAD_V1
            _wx = getattr(self, "_weather", None)
            _tomorrow = ((_wx.get_tomorrow_string() if _wx else "") or "").strip()
            if _tomorrow:
                user += (f" Na závěr nahlas PŘESNĚ tuto předpověď na zítřek, "
                         f"slovo od slova; neuváděj jiná čísla ani neodhaduj: "
                         f"„{_tomorrow}\"")

        return system, user

    def _send_greeting_async(self, name: str, confidence: float, duvod=None):
        # HANS_GESTURE_WAVE_GREET_V1 — `duvod` rekne, PROC se zdravi (dnes
        # zamavani gestem). Bez nej by pozdrav na mavnuti znel stejne jako
        # pozdrav pri rozpoznani tvare a neslo by je od sebe odlisit.
        try:
            prompt = self._generate_greeting_prompt(name, duvod=duvod)
            first  = [True]

            def _on_sentence(sentence: str):
                tts = self.tts_speaker
                if tts and tts.enabled:
                    tts.speak(sentence, priority=first[0])
                first[0] = False

            response = self._stream_message(prompt, name=name,
                                             internal=True,  # G3D: uvítačka není faktický dotaz
                                            on_sentence=_on_sentence)
            if response:
                self.conv_store.add_greeting(name, response)
                self._log_interaction(name, str(prompt), response)
                _mem = getattr(self, 'memory', None)  # T5_DIALOG_RECALL_V1
                if _mem is not None:
                    try: _mem.bump_dialog(name)
                    except Exception as _be: print(f"[Chat] bump_dialog failed: {_be}")

            # HANS_QUESTION_POPUP_V1 — po pozdravu zkus položit čekající otázku
            # přes popup okno (vysloví + zobrazí + čeká na odpověď).
            try:
                _opened = self.ask_question_via_popup(name)
            except Exception as _qpe:
                _opened = False
                print(f"[Chat] greeting popup-question failed: {_qpe}")
            # GREETING_THREAD_POPUP_V1 — pozdrav navnázal na rozjetou nitku
            # (vyslovil follow-up) → otevři okno naseedované pozdravem, ať má
            # uživatel kam odpovědět. Jen když popup-otázka neběžela (ne 2 okna).
            try:
                if (not _opened) and getattr(self, '_greeting_thread_surfaced', False) and response:
                    from scripts.popup_chat_window import SimplePopupChat
                    SimplePopupChat(self, name, 1.0, already_greeted=True,
                                    initial_question=response)
            except Exception as _tpe:
                print(f"[Chat] greeting thread-popup failed: {_tpe}")
        except Exception as e:
            print(f"[Chat] Greeting error for {name}: {e}")

    def handle_face_recognition(self, name: str, confidence: float):
        # HANS_EYE_BLINK_V1 — vrací True, když se PRÁVĚ spustil pozdrav (aby
        # volající mohl mrknout očima). Jinak False (idempotentní — už pozdraveno).
        if not self.enabled or not name:
            return False
        should_greet = self.should_greet_person(name)
        greeted = False
        if should_greet and self.greeting_enabled:
            self.mark_person_greeted(name)
            threading.Thread(target=self._send_greeting_async,
                             args=(name, confidence), daemon=True).start()
            greeted = True
        if self.popup_enabled and self.popup_manager:
            self.popup_manager.handle_face_detection(name, confidence,
                                                      not should_greet)
        return greeted

    def _load_daily_greetings(self) -> set:
        try:
            os.makedirs("data", exist_ok=True)
            if os.path.exists(self.greeting_persistence_file):
                with open(self.greeting_persistence_file) as f:
                    data = json.load(f)
                today = date.today().isoformat()
                if today in data:
                    cleaned = {d: v for d, v in data.items() if d >= today}
                    with open(self.greeting_persistence_file, "w") as f:
                        json.dump(cleaned, f)
                    return set(data[today])
        except Exception as e:
            print(f"[Chat] Load daily greetings error: {e}")
        return set()

    def _save_daily_greetings(self):
        try:
            today = date.today().isoformat()
            data  = {}
            if os.path.exists(self.greeting_persistence_file):
                with open(self.greeting_persistence_file) as f:
                    data = json.load(f)
            data[today] = list(self.daily_greeted)
            with open(self.greeting_persistence_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[Chat] Save daily greetings error: {e}")

    def should_greet_person(self, name: str) -> bool:
        if not self.greeting_enabled:
            return False
        if self.greeting_mode == "once_per_session":
            return name not in self.session_greeted
        elif self.greeting_mode == "once_per_day":
            return name not in self.daily_greeted
        return True

    def mark_person_greeted(self, name: str):
        if self.greeting_mode == "once_per_session":
            self.session_greeted.add(name)
        elif self.greeting_mode == "once_per_day":
            self.daily_greeted.add(name)
            self._save_daily_greetings()

    def reset_session_greetings(self):
        self.session_greeted.clear()

    def reset_daily_greetings(self):
        self.daily_greeted.clear()
        self._save_daily_greetings()

    @staticmethod
    def _collapse_repeated_greetings(text: str) -> str:
        """hans-czech občas na vágní zprávu degeneruje do opakovaných pozdravů
        („Dobrý večer, Stando. …" 3×). Když odpověď obsahuje věcný odstavec,
        zahoď krátké odstavce-pozdravy (filler); samé pozdravy → nech první."""
        import re as _re
        if not text:
            return text
        paras = [p.strip() for p in _re.split(r"\n\s*\n", text) if p.strip()]
        if len(paras) <= 1:
            return text
        # HANS_GREETING_OUTPUT_TRIM_V2 — vzor je ted modulovy (sdileny
        # s vystupnim orezem), aby nevznikla druha kopie.
        _greet = _h._POZDRAV_UZIVATEL_RE
        is_filler = lambda p: bool(_greet.match(p)) and len(p) < 90
        substantive = [p for p in paras if not is_filler(p)]
        kept = substantive if substantive else paras[:1]
        return "\n\n".join(kept)

# ROZDELENI_METOD_V1 — až na konci, viz hlavička
from scripts import openwebui_direct_handler as _h  # noqa: E402
