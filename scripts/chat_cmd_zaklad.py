"""Obsluhy příkazů přesunuté z `scripts/chat_commands.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_cc.` až při
volání. Registrace příkazů (`register(...)`) zůstává v původním modulu.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import re
import time

def _cmd_ooda(handler, name, args) -> str:  # OODA_CMD_V1
    """/ooda — diagnostika OODA. Zavolá _decide_activity (zaloguje skóre),
    akci NEVYKONÁ, vrátí název vybrané aktivity. Skóre: grep z logu.
    Cesta k idle objektu kopíruje _cmd_denik (handler._hans_idle)."""
    _hi = getattr(handler, "_hans_idle", None)
    if _hi is None:
        return "OODA: idle objekt (handler._hans_idle) není dostupný."
    if not hasattr(_hi, "_decide_activity"):
        return "OODA: _decide_activity na idle objektu chybí (patch aplikován?)."
    try:  # OODA_CMD_SCORE_V1
        chosen_fn = _hi._decide_activity(dry_run=True)  # OODA_DRYRUN_V1
        fn_name = getattr(chosen_fn, "__name__", str(chosen_fn))
        label = fn_name.replace("_activity_", "")
        score = getattr(_hi, "_last_ooda_score", None)
        if score:
            return (
                "OODA skóre: %s\n"
                "(akce NEvykonána — jen diagnostika)"
            ) % score
        # Fallback: atribut chybí (starý běh / první patch nenasazen)
        return (
            "OODA by teď vybralo: %s\n"
            "(akce NEvykonána). Skóre v logu:\n"
            "  grep 'OODA skóre:' data/system.log | tail -1"
        ) % label
    except Exception as _e:
        return "OODA diagnostika selhala: %s" % _e


def _cmd_zapis(handler, name, args) -> str:
    txt = _cc._note_text(args or "")
    if not txt:
        return "Co si mám poznamenat, pane?"
    # týž kód jako agentní akce `add_note` (včetně větve „má to čas → je to
    # připomínka", HANS_REMINDER_ADD_V1) — jedna pravda o zápisu
    from scripts.hans_agent import _run_add_note
    return _run_add_note(handler, {"text": txt})


def _cmd_seznam(handler, name, args) -> str:  # HANS_AGENT_V1 — poznámky/seznam
    """/seznam — výpis poznámek; /seznam hotovo N; /seznam smaz N."""
    import sqlite3 as _sql
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
    a = (args or "").strip().lower()
    try:
        db = _sql.connect(dbp, timeout=5.0)
        db.execute("CREATE TABLE IF NOT EXISTS hans_notes ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, "
                   "done INTEGER NOT NULL DEFAULT 0)")
        m = re.match(r"(hotovo|smaz|smaž)\s+(\d+)", a)
        if m:
            nid = int(m.group(2))
            if m.group(1).startswith("hotov"):
                db.execute("UPDATE hans_notes SET done=1 WHERE id=?", (nid,))
                db.commit(); db.close()
                return f"✓ Položka {nid} označena jako hotová."
            db.execute("DELETE FROM hans_notes WHERE id=?", (nid,))
            db.commit(); db.close()
            return f"✓ Položka {nid} smazána."
        rows = db.execute("SELECT id, text, done FROM hans_notes "
                          "ORDER BY done ASC, id ASC LIMIT 40").fetchall()
        db.close()
        if not rows:
            return "Seznam je prázdný, pane."
        lines = ["📝 Seznam:"]
        for i, t, d in rows:
            lines.append(f"  {'✓' if d else '•'} [{i}] {t}")
        return "\n".join(lines)
    except Exception as e:
        return f"/seznam: chyba ({e})"


def _cmd_dialog(handler, name, args) -> str:
    """Spustí Hans-Koláč dialog flag souborem (totéž co web admin tlačítko)."""
    from pathlib import Path as _P
    try:
        flag = _P("data/.trigger_dialog")
        flag.parent.mkdir(exist_ok=True)
        flag.touch()
        from scripts.hans_kolac import kolac_name as _kn  # KOLAC_NAME_CONFIGURABLE_V1
        _k = _kn(getattr(handler, "config", {}) or {})
        return f"Zavolám {_k}. Dialog se spustí za chvíli."
    except Exception as e:
        return f"Nepodařilo se mi zavolat společníka: {e}"


def _cmd_info(handler, name, args) -> str:
    """/stav — HANS_STATUS_UNIFIED_V1: tentýž text jako z mostu (Matrix).
    Dvě samostatné implementace se ukázaly jako matoucí (5.8.): most chytá
    „stav" dřív, takže z telefonu se tahle verze nikdy neukázala."""
    from scripts.hans_status import status_text
    return status_text(getattr(handler, "config", {}) or {}, handler, name or "")


def _cmd_help(handler, name, args) -> str:
    """Seznam commands."""
    lines = ["Dostupné příkazy:"]
    for c in _cc.list_commands():
        lines.append(f"  /{c['slash']} — {c['help']}")
    return "\n".join(lines)


def _cmd_zaptej(handler, name, args) -> str:
    """Vyvolá curiosity — Hans vygeneruje otázku a hledá odpověď.
    # CMD_ZAPTEJ_PATCH
    args = volitelný kontext (pokud prázdné, vezme z místnosti)."""
    _hi = getattr(handler, "_hans_idle", None)
    _cur = getattr(_hi, "_curiosity", None) if _hi else None
    if not _cur:
        return "Curiosity modul není dostupný."

    # Kontext: explicitně args nebo "rozhovor s {name}" jako placeholder
    context = args.strip() if args else f"rozhovor s osobou {name or 'host'}"

    import threading
    def _run():
        try:
            # source_type = jiný než observation/room → půjde do Wikipedie
            _cur.trigger_question(context, source_type="manual")
        except Exception as e:
            print(f"[Chat] zaptej failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return "Položím si otázku a hledám odpověď, pane."


def _cmd_enroll(handler, name, args) -> str:
    """Spustí video enroll. # ENROLL_MULTI_DEFAULT
    Bez sekund (jen jméno) → multi-phase (3 vzdálenosti, ~2 min).
    Se sekundami → single phase (jen aktuální vzdálenost).
    """
    from pathlib import Path as _P
    parts = (args or "").strip().split()
    target_name = parts[0] if parts else (name or "")
    if not target_name:
        return "Použití: /enroll <jméno> [sekundy]   (bez sekund = multi-phase)"
    try:
        flag = _P("data/.video_enroll")
        flag.parent.mkdir(exist_ok=True)
        if len(parts) < 2:
            # Multi-phase mode (3 vzdálenosti, ~2 min)
            flag.write_text(f"multi:{target_name.lower()}|0")
            return (f"Spouštím multi-phase enrollment pro '{target_name}'. "
                    f"Budu vás vést — postavte se prosím asi metr od kamery.")
        try:
            secs = int(parts[1])
        except ValueError:
            secs = 30
        secs = max(5, min(secs, 120))
        flag.write_text(f"{target_name.lower()}|{secs}")
        return (f"Spouštím video enroll pro '{target_name}' na {secs}s. "
                f"Otáčejte hlavou pomalu.")
    except Exception as e:
        return f"Nepodařilo se spustit enroll: {e}"


def _cmd_verify(handler, name, args) -> str:
    """G5C: ověř faktická tvrzení proti Wikipedii. JEN diagnostika.
    /verify <text>  → ověří text;  /verify → poslední Hansovu odpověď.
    """
    # 1) Zdroj textu: arg má přednost, jinak poslední Hansova odpověď
    text = (args or "").strip()
    if not text:
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — poslední odpověď JEN v tomto kanálu
            _ch = _cc._current_channel()
            hist = ((handler.conv_store.get_history_scoped(name, _ch)
                     if _ch else handler.conv_store.get_history(name)) or [])
            for msg in reversed(hist):
                if msg.get("role") == "assistant" and msg.get("content"):
                    text = msg["content"].strip()
                    break
        except Exception as e:
            _cc._log.error("G5C: čtení historie selhalo: %s", e)
    if not text:
        return "Nemám co ověřovat, pane. Zadejte /verify <text> nebo se nejdřív na něco zeptejte."

    _cc._log.info("G5C: verify začíná, text=%r", text[:120])

    # 2) Extrakce tvrzení (LLM)
    # G5J_VERIFY_UNIFY_V1 (a) — ENTITA = předmět tvrzení (dílo/událost/místo),
    # NE osoba, co o nich něco tvrdí. Sjednoceno s hans_routine extraktorem.
    extract_sys = (
        "Jsi extraktor faktických tvrzení. Z textu vypiš ověřitelná "
        "faktická tvrzení o světě (osoby, díla, události, místa, data). "
        "ENTITA je PŘEDMĚT tvrzení s vlastním heslem na Wikipedii — "
        "u díla samotné dílo, NE jeho autor. Tvrzení 'R.U.R. napsal "
        "Čapek' → ENTITA je 'R.U.R.' (dílo), NE 'Čapek'. Tvrzení "
        "'Wells napsal Válku světů' → ENTITA 'Válka světů'. "
        "Ignoruj dojmy, zdvořilosti, názory. Každé tvrzení na samostatný "
        "řádek ve tvaru 'ENTITA | tvrzení'. Max 3. Pokud žádné, napiš PRÁZDNÉ."
    )
    extract = _cc._g5c_llm(handler, extract_sys, "Text:" + _cc.NL_RUNTIME + text, num_predict=200)
    if not extract or "PRÁZDNÉ" in extract.upper():
        return "Pane, v textu nenacházím konkrétní faktické tvrzení k ověření."

    # 3) Wikipedia raw_text pro každé tvrzení
    try:
        from scripts.web_reader import WebReader
        cfg = getattr(handler, "config", {}) or {}
        wr = WebReader(cfg)
    except Exception as e:
        _cc._log.error("G5C: WebReader init selhal: %s", e)
        return "Pane, ověření selhalo (čtečka webu nedostupná): " + str(e)

    lines = [l.strip() for l in extract.splitlines() if l.strip()][:3]
    results = []
    for line in lines:
        entity = line.split("|", 1)[0].strip() if "|" in line else line
        claim = line.split("|", 1)[1].strip() if "|" in line else line
        if not entity:
            continue
        # G5J_VERIFY_UNIFY_V1 (b) — plný článek dle PŘEDMĚTU (kopie G5F z routine):
        # _wikipedia_search → fetch_url (2500 zn.), fallback REST summary.
        from urllib.parse import quote as _q
        wiki = ""
        try:
            _title = wr._wikipedia_search(entity)
            if _title:
                _url = 'https://cs.wikipedia.org/wiki/' + _q(_title.replace(' ', '_'))
                _full = wr.fetch_url(_url, topic='verify')
                if _full and getattr(_full, 'raw_text', ''):
                    wiki = _full.raw_text[:2500]
                    _cc._log.info('G5C: [%s] plný článek %r (%d zn.)',
                              entity, _title, len(_full.raw_text))
            if not wiki:
                _rr = wr.wikipedia(entity)
                if _rr and getattr(_rr, 'raw_text', ''):
                    wiki = _rr.raw_text[:1200]
                    _cc._log.info('G5C: [%s] fallback summary', entity)
        except Exception as e:
            _cc._log.warning("G5C: zdroj pro %r selhal: %s", entity, e)
        if not wiki:
            results.append("• " + entity + ": Wikipedie nenašla (nelze ověřit)")
            continue
        cmp_sys = (
            "Jsi ověřovatel faktů. Porovnej TVRZENÍ s textem z Wikipedie. "
            "Odpověz stručně jedním z: SHODA / ROZPOR / NELZE OVĚŘIT, "
            "a krátce proč (max 1 věta). Buď přísný na fakta."
        )
        cmp_user = "TVRZENÍ: " + claim + _cc.NL_RUNTIME + _cc.NL_RUNTIME + "WIKIPEDIE:" + _cc.NL_RUNTIME + wiki
        verdict = _cc._g5c_llm(handler, cmp_sys, cmp_user, num_predict=120)
        _cc._log.info("G5C: [%s] verdikt=%s", entity, verdict[:120])
        results.append("• " + entity + ": " + verdict)

    if not results:
        return "Pane, nepodařilo se extrahovat ověřitelné entity."

    body = _cc.NL_RUNTIME.join(results)
    return ("Ověření proti Wikipedii, pane:" + _cc.NL_RUNTIME + _cc.NL_RUNTIME + body
            + _cc.NL_RUNTIME + _cc.NL_RUNTIME + "(Pozn.: jen diagnostika, nic se neukládá.)")


def _cmd_schopnosti(handler, name, args) -> str:
    # HANS_CAP_SUMMARY_V1 — plný výčet se slash-příkazy zahltí nováčka. Proto:
    # jen EXPLICITNÍ slash /schopnosti (origin "slash") → plný report; přirozený
    # dotaz „co umíš/dokážeš?" (origin "nl"/"llm"/neznámý) → vřelé shrnutí. Původ
    # rozhoduje, protože slash i LLM-route vracejí OBĚ prázdné args (nerozliší se).
    try:
        from scripts.hans_capabilities import (capabilities_report,
                                               capabilities_summary)
        if _cc._route_origin() == "slash":
            return capabilities_report()
        return capabilities_summary()
    except Exception as e:
        return "Přehled schopností nedostupný: %s" % e


def _cmd_pripady(handler, name, args) -> str:
    """Běžící případ, poslední tři uzavřené a souhrn — z `kolac_cases`."""
    import json
    import sqlite3
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
    try:
        from scripts.hans_kolac import kolac_name as _kn
        k = _kn(cfg)
    except Exception:
        k = "Koláč"
    try:
        db = sqlite3.connect(f"file:{dbp}?mode=ro", uri=True)
        aktivni = db.execute(
            "SELECT title, phase, opened_at, clues FROM kolac_cases "
            "WHERE phase != 'closed' ORDER BY opened_at DESC LIMIT 1").fetchone()
        uzavrene = db.execute(
            "SELECT title, opened_at, closed_at FROM kolac_cases "
            "WHERE phase = 'closed' AND closed_at IS NOT NULL "
            "ORDER BY closed_at DESC").fetchall()
        # Skutečný závěr píše jen hook `case_resolution` (resolution v tabulce
        # je generická věta) — a jen u části případů.
        zavery = {}
        for t, note, data in db.execute(
                "SELECT title, note, data FROM diary "
                "WHERE event_type = 'case_resolution' ORDER BY id"):
            txt = (note or data or "").strip()
            if t and txt:
                zavery[t] = txt
        db.close()
    except Exception as e:
        _cc._log.warning("pripady: čtení selhalo: %s", e)
        return "Záznamy o případech se mi teď nepodařilo otevřít."

    radky = []
    if aktivni:
        title, phase, opened, clues = aktivni
        try:
            stopy = json.loads(clues or "[]")
        except Exception:
            stopy = []
        den = max(1, int((time.time() - opened) / 86400) + 1)
        r = (f"Právě s panem {k}em vyšetřujeme „{title}“ — od {_cc._datum_cz(opened)} "
             f"({den}. den), fáze: {_cc._FAZE_CZ.get(phase, phase)}, "
             f"záznamů ve spisu {len(stopy)}.")
        podnet = (stopy[0].get("text") if stopy and isinstance(stopy[0], dict)
                  else "") or ""
        if podnet:
            # Šablona stopy je ve 3. osobě („<Jméno> četl o X a …“) → 1. osoba.
            podnet = re.sub(r"^\w+\s+((pře)?četl)\b", r"jsem \1", podnet, 1)
            r += f" Začalo to tím, že {podnet}"
            if not r.endswith((".", "!", "?")):
                r += "."
        radky.append(r)
    else:
        radky.append(f"Teď s panem {k}em žádný případ nevyšetřujeme.")

    if uzavrene:
        posl = []
        for title, o, c in uzavrene[:3]:
            p = f"„{title}“ ({_cc._datum_cz(o)}–{_cc._datum_cz(c)}, {_cc._dny_cz((c - o) / 86400)})"
            z = zavery.get(title)
            if z:
                z = re.split(r"(?<=[.!?])\s", z, 1)[0][:200]
                p += f" — můj závěr: {z}"
            posl.append(p)
        radky.append("Naposledy uzavřené: " + "; ".join(p.rstrip(".") for p in posl) + ".")
        delky = sorted((c - o) / 86400 for _, o, c in uzavrene)
        n = len(delky)
        med = (delky[n // 2] if n % 2 else (delky[n // 2 - 1] + delky[n // 2]) / 2)
        radky.append(
            f"Celkem jsme uzavřeli {n} případů; typicky trvají {_cc._dny_cz(med)} "
            f"(nejkratší {_cc._dny_cz(delky[0])}, nejdelší {_cc._dny_cz(delky[-1])}).")
    return "\n".join(radky)

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import chat_commands as _cc  # noqa: E402
