"""
CHAT_DEFERRED_V1 — zpráva, na kterou Hans neměl mozek, se nesmí ztratit.

DOLOŽENÝ PŘÍPAD (13.8. 12:01): uživatel poslal z telefonu
„pripomen v 18:00 „PC watchdog (skript leží na ploše)"" — jenže na PC běžela hra,
takže mozek byl pauznutý (`GAME_MODE_CHAT_GATE_V1`) a `_stream_message` zprávu
ZAHODIL (`return None`). Hans slušně odpověděl „můj mozek spí", ale OBSAH
požadavku zmizel: žádná připomínka nevznikla. Uživatel to sám odhadl —
*„jelikož PC běží, asi se to ztratí. Může to jít na zpracování, až bude mozek
dostupný?"*

ŘEŠENÍ: zpráva se uloží do fronty a přehraje se, jakmile je mozek zase k
dispozici. Týž princip, jaký v systému platí pro všechno ostatní závislé na
Ollamě ([[ollama-deferred-processing]]): výpadek LLM nesmí ztratit data.

ROZHODNUTÍ, KTERÁ STOJÍ ZA VYSVĚTLENÍ:
  • **Fronta je SOUBOR, ne paměť.** Restart Hanse je častý a herní mód trvá
    hodiny; v paměti by požadavek nepřežil. Týž důvod jako u
    `pc_deferred_shutdown` a stavu hlídacího režimu.
  • **Frontuje se AŽ selhaná odpověď, ne každá zpráva.** Deterministické
    příkazy (`/stav`, `/zdravi`) v herním módu fungují a obslouží se dřív
    (`bridge_commands.handle`) — do fronty se dostane jen to, co reálně
    potřebovalo mozek a nedostalo ho.
  • **Strop stáří.** Přehrát dotaz „co teď běží v TV?" po pěti hodinách je
    horší než ho zahodit — odpověď by mířila do minulosti. Starší než
    `max_age_h` se proto neprovede, jen se ohlásí, že vypršel.
  • **Odpověď se posílá zpět tam, odkud přišla** (kanál v záznamu), a značí
    se jako opožděná, aby uživatel poznal, na co Hans odpovídá.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("chat_deferred")

QUEUE = "data/chat_deferred.jsonl"
MAX_ITEMS = 20          # pojistka proti zaplavení (hra může trvat hodiny)
DEFAULT_MAX_AGE_H = 6.0


def enqueue(person: str, text: str, channel: str = "matrix",
            path: str = QUEUE) -> bool:
    """Ulož zprávu, na kterou nebyl mozek. True = uloženo."""
    text = (text or "").strip()
    if not text:
        return False
    try:
        rows = _load(path)
        if len(rows) >= MAX_ITEMS:
            log.warning("fronta plná (%d) — zpráva se neuloží: %.40s",
                        len(rows), text)
            return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "person": person or "",
                                "text": text, "channel": channel or ""},
                               ensure_ascii=False) + "\n")
        log.info("odloženo do fronty (%s): %.60s", person or "?", text)
        return True
    except Exception as e:
        log.warning("nelze uložit odloženou zprávu: %s", e)
        return False


def _load(path: str = QUEUE) -> list:
    out = []
    try:
        if not os.path.isfile(path):
            return out
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def pending(path: str = QUEUE) -> list:
    return _load(path)


def _clear(path: str = QUEUE) -> None:
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except Exception as e:
        log.warning("nelze smazat frontu: %s", e)


def drain(handler, send, config: dict | None = None,
          path: str = QUEUE) -> int:
    """Přehraj odložené zprávy. Volat, až je mozek dostupný.

    `handler` musí umět `send_chat_message(person, text, channel=...)`,
    `send` je funkce pro odeslání odpovědi (text) -> None.
    Vrací počet zpracovaných. Fronta se maže AŽ po průchodu (když odpověď
    selže, zpráva se do fronty vrátí — stejný princip jako notify_queue).
    """
    rows = _load(path)
    if not rows:
        return 0
    cfg = ((config or {}).get("chat_deferred", {}) or {})
    max_age = float(cfg.get("max_age_h", DEFAULT_MAX_AGE_H)) * 3600.0
    now = time.time()
    keep, done = [], 0
    for r in rows:
        text = (r.get("text") or "").strip()
        person = r.get("person") or ""
        age = now - float(r.get("ts") or 0)
        if not text:
            continue
        when = time.strftime("%H:%M", time.localtime(float(r.get("ts") or now)))
        if age > max_age:
            # Nepřehrávat — odpověď by mířila do minulosti. Ale ŘÍCT to.
            try:
                send("Nestihl jsem vyřídit vaši zprávu z %s („%s“) — mozek byl "
                     "dlouho nedostupný a bál bych se odpovídat na tak starý "
                     "dotaz. Pošlete ji prosím znovu, bude-li stále platit."
                     % (when, text[:80]))
            except Exception as e:
                log.warning("hlášení vypršelé zprávy selhalo: %s", e)
            log.info("odložená zpráva vypršela (%.1f h): %.60s",
                     age / 3600.0, text)
            continue
        try:
            reply = handler.send_chat_message(person, text,
                                              channel=r.get("channel") or None)
        except TypeError:
            try:
                reply = handler.send_chat_message(person, text)
            except Exception as e:
                log.warning("přehrání odložené zprávy selhalo: %s", e)
                keep.append(r)
                continue
        except Exception as e:
            log.warning("přehrání odložené zprávy selhalo: %s", e)
            keep.append(r)
            continue
        if not reply:
            keep.append(r)      # mozek zase nedostupný → zkusí se příště
            continue
        try:
            send("K vaší zprávě z %s se vracím, pane:\n\n%s" % (when, reply))
            done += 1
        except Exception as e:
            log.warning("odeslání odložené odpovědi selhalo: %s", e)
            keep.append(r)
    # přepiš frontu tím, co zbylo
    try:
        if keep:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for r in keep:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(tmp, path)
        else:
            _clear(path)
    except Exception as e:
        log.warning("nelze přepsat frontu: %s", e)
    if done:
        log.info("CHAT_DEFERRED_V1: vyřízeno %d odložených zpráv", done)
    return done
