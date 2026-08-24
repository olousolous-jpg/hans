"""scripts/hans_lessons.py

HANS_CORRECTION_LEARNING_V1 (#4, KONZERVATIVNÍ) — Hans se učí z korekcí.

Když ho uživatel v rozhovoru OPRAVÍ / vyvrátí mu tvrzení / dá najevo, že se spletl,
Hans si z toho v noci vezme PONAUČENÍ (lekci) a:
  - uloží ho do deníku jako 'lesson_learned' (LOG + noční reflexe „kde jsem se mýlil"),
  - příště je má v kontextu chatu (aby chybu neopakoval),
  - ráno z toho pocítí jemnou pokoru (mírná nálada).

KONZERVATIVNÍ rozsah: NEMĚNÍ automaticky paměť, fakta ani postoje. Jen se z toho
poučí (vědomě) — případnou korekci paměti/postoje řeší člověk.

Anti-konfabulace: jen REÁLNÉ opravy z přepisu, ne pouhý jiný názor/vkus.

API:
  extract_corrections(config, diary_db_path, window_hours=26) -> int   # noční
  recent_lessons(diary_db_path, hours=48, limit=5) -> list[str]         # chat ctx
  scan_overnight_lessons(diary_db_path, since) -> int                   # mood nudge
"""
from __future__ import annotations
import json
import logging
import re
import sqlite3
import time

_log = logging.getLogger("hans_lessons")

# HANS_CORRECTION_NO_CLAIM_V1 (24.8.) — `claim` je NEPOVINNÝ.
# Dřív prompt vyžadoval Hansovo chybné tvrzení DOSLOVNĚ v přepisu, jenže okno
# extrakce je 26 h (`window_hours`). Doloženo na Gutštejnovi: tvrzení 27.7.
# 12:20:39, oprava uživatele 28.7. 11:22:24 = o 23 h později → claim ~8 h ZA
# oknem → model korektně vrátil prázdné pole → 28. ani 29.7. nevznikla ani jedna
# lekce (strop max_per_night to nebyl). Takhle propadla KAŽDÁ oprava starší než
# ~den — a to je ten běžný případ, protože chyby si člověk všimne později.
# ⚠️ Okno se ZÁMĚRNĚ nerozšiřuje (dražší přepis, víc tokenů, a stejně by to
# selhalo u korekce po týdnu). Anti-konfabulační laťka zůstává, jen míří tam,
# kam patří: na DOSLOVNÁ SLOVA OSOBY, ne na přítomnost Hansova tvrzení.
_SYSTEM = (
    "Jsi pozorný analytik. Dostaneš PŘEPIS dnešních rozhovorů jedné osoby s postavou "
    "jménem {persona_name} (řádky „osoba:…\" a „{persona_name}:…\"). Najdi momenty, "
    "kdy osoba {persona_name} OPRAVILA — vyvrátila mu tvrzení, upozornila, že se spletl, "
    "že něco řekl nepřesně nebo si vymyslel. Pro každý takový moment vrať objekt s klíči:\n"
    "  claim     = co {persona_name} řekl špatně (jeho chybné tvrzení). NEPOVINNÉ — "
    "když jeho původní tvrzení v TOMHLE přepisu není, nech prázdný řetězec.\n"
    "  correction= jak ho osoba opravila / jak to ve skutečnosti je,\n"
    "  lesson    = krátké ponaučení v 1. osobě, co si z toho {persona_name} bere "
    "(např. „Nemám si domýšlet preference lidí, když je neznám.\").\n"
    "OPRAVA S ODSTUPEM — DŮLEŽITÉ: osoba často opravuje omyl až se zpožděním, klidně "
    "o několik dní, takže původní chybné tvrzení v přepisu vůbec být NEMUSÍ. Takovou "
    "opravu zahrň TAKÉ, s prázdným claim. Poznáš ji podle toho, že osoba něco výslovně "
    "popírá nebo uvádí na pravou míru — „X není Y\", „to je špatně\", „mýlíš se\", "
    "„ve skutečnosti je to jinak\". Nedomýšlej si, co {persona_name} řekl předtím; "
    "když jeho tvrzení neznáš, prostě nech claim prázdný.\n"
    "PŘÍSNĚ ANTI-KONFABULACE: zahrň JEN opravy, jejichž znění je DOSLOVNĚ v přepisu "
    "ve slovech té osoby. NEZAHRNUJ pouhý jiný názor či vkus, běžnou otázku, ani "
    "nesouhlas v preferenci (to není oprava). Když žádná oprava není, vrať prázdné pole. "
    "Vrať VÝHRADNĚ JSON pole objektů, nic víc."
)


def _extract_json_array(raw: str):
    """Robustní extrakce JSON pole (toleruje ```json fences)."""
    if not raw:
        return []
    s = raw.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    a = s.find("[")
    b = s.rfind("]")
    if a != -1 and b != -1 and b > a:
        try:
            out = json.loads(s[a:b + 1])
            if isinstance(out, list):
                return out
        except Exception:
            pass
    # HANS_CORRECTION_TRUNCATION_V1 — pojistka: když se pole nedoparsuje
    # (uříznuté generování → chybí `]`), vytáhni aspoň KOMPLETNÍ objekty.
    # Dřív se celá dávka zahodila kvůli jedné nedopsané položce.
    return _salvage_objects(s[a:] if a != -1 else s)


def _salvage_objects(s: str) -> list:
    """Kompletní `{...}` objekty z rozbitého/useknutého JSON pole."""
    out, depth, start = [], 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict):
                            out.append(obj)
                    except Exception:
                        pass
                    start = None
    return out


def extract_corrections(config: dict, diary_db_path: str,
                        window_hours: float = 26.0) -> int:
    """Noční krok: z denních dialogů vytáhne momenty, kdy byl Hans opraven, a uloží
    'lesson_learned' do deníku. NEMĚNÍ paměť/postoje. Vrací počet lekcí. LLM offline
    / žádné dialogy → 0 (tichý skip)."""
    cfg = (config.get("corrections", {}) or {})
    if not cfg.get("enabled", True):
        return 0
    window_hours = float(cfg.get("window_hours", window_hours))
    since = time.time() - window_hours * 3600.0
    try:
        from scripts.hans_threads import _gather_dialogs
    except Exception as e:
        _log.warning("extract_corrections: _gather_dialogs nedostupný: %s", e)
        return 0
    dialogs = _gather_dialogs(diary_db_path, since)
    if not dialogs:
        _log.info("extract_corrections: žádné dialogy v okně, skip")
        return 0
    er = config.get("evening_reflection", {}) or {}
    model = str(cfg.get("model", er.get("model",
                "jobautomation/OpenEuroLLM-Czech:latest")))
    timeout = int(cfg.get("llm_timeout", 300))
    max_per_night = int(cfg.get("max_per_night", 3))
    _num_predict = int(cfg.get("num_predict", 800))   # HANS_CORRECTION_TRUNCATION_V1
    # HANS_CORRECTION_NUMCTX_V1 (24.8.) — SKUTEČNÁ příčina uřezaného JSON.
    # ZMĚŘENO na reálném volání: `done_reason=length`, `prompt_eval=1906`,
    # `eval_count=142` při `num_ctx` defaultu 2048 → prompt sežral okno a na
    # výstup zbylo 142 tokenů. `num_predict` je proti tomu bezmocný.
    # ⚠️ Perverzní důsledek, který to mělo celou dobu: čím DELŠÍ den (delší
    # přepis), tím MÍŇ místa na odpověď → tím MÉNĚ zachycených korekcí.
    # Přepis 4000 zn ≈ 1400 tok + system ≈ 500 → 8192 dá pohodlnou rezervu.
    _num_ctx = int(cfg.get("num_ctx", 8192))
    try:
        from scripts.ollama_client import ollama_generate
    except Exception as e:
        _log.warning("extract_corrections: ollama_client nedostupný: %s", e)
        return 0
    try:
        from scripts.hans_persona import persona_name as _pn
        system = _SYSTEM.format(persona_name=_pn(config))
    except Exception:
        system = _SYSTEM.format(persona_name="Hans")

    written = 0
    for person, notes in dialogs.items():
        if written >= max_per_night:
            break
        # HANS_CORRECTION_TRUNCATION_V1 (24.8.) — brát KONEC přepisu, ne
        # začátek. `_gather_dialogs` vrací zprávy chronologicky, takže `[:4000]`
        # si nechal NEJSTARŠÍ a zahodil nejnovější — tedy přesně ty korekce,
        # kvůli kterým se to pouští. ZMĚŘENO 24.8.: přepis 5219 zn, oprava
        # „Gutštejn" na pozici 3536 (prošla), „Karlštejn" 4640 a „Kinský" 4936
        # (obě uříznuty). Korekce přichází na konci hovoru, ne na začátku.
        transcript = "\n\n".join(notes)[-4000:]
        if not transcript.strip():
            continue
        try:
            # HANS_CORRECTION_TRUNCATION_V1 — `num_predict` se NIKDY nenastavil,
            # takže platil default modelu (~128 tok). ZMĚŘENO: výstup uříznut na
            # 401 zn uprostřed pole, bez `]` → `_extract_json_array` vrátil []
            # → z noci 0 lekcí, i když model korekce NAŠEL. Tichá ztráta:
            # extrakce takhle přicházela o všechno, co se nevešlo do jedné
            # krátké položky.
            raw = ollama_generate(model=model, prompt=transcript, system=system,
                                  config=config, timeout=timeout, keep_alive=0,
                                  options={"temperature": 0.1,
                                           "num_predict": _num_predict,
                                           "num_ctx": _num_ctx})
        except Exception as e:
            _log.warning("extract_corrections LLM (%s): %s", person, e)
            continue
        items = _extract_json_array(raw)
        for it in items:
            if written >= max_per_night:
                break
            if not isinstance(it, dict):
                continue
            lesson = str(it.get("lesson", "") or "").strip()
            if len(lesson) < 8:
                continue
            claim = str(it.get("claim", "") or "").strip()
            correction = str(it.get("correction", "") or "").strip()
            try:
                db = sqlite3.connect(diary_db_path, timeout=5.0)
                db.execute(
                    "INSERT INTO diary (ts, event_type, title, note, data) "
                    "VALUES (?,?,?,?,?)",
                    (time.time(), "lesson_learned", person, lesson,
                     json.dumps({"claim": claim, "correction": correction},
                                ensure_ascii=False)))
                db.commit()
                db.close()
                written += 1
                _log.info("lesson_learned [%s]: %.70s", person, lesson)
            except Exception as e:
                _log.warning("extract_corrections diary write failed: %s", e)
    _log.info("extract_corrections: uloženo %d lekcí", written)
    return written


def recent_lessons(diary_db_path: str, hours: float = 48.0,
                   limit: int = 5) -> list:
    """READ-ONLY: posledních `limit` lekcí (note) za okno. '' / chyba → []."""
    since = time.time() - hours * 3600.0
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='lesson_learned' "
            "AND ts > ? ORDER BY ts DESC LIMIT ?", (since, int(limit))).fetchall()
        return [r[0].strip() for r in rows if r and r[0] and r[0].strip()]
    except Exception as e:
        _log.debug("recent_lessons failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_TOPIC_STOP_EXTRA = {
    "jake", "jaky", "jaka", "jake", "ktery", "ktera", "ktere", "kteri",
    "cem", "cim", "proc", "kde", "kdy", "kdo", "jak", "muzes", "muzete",
    "rici", "rekni", "povez", "vis", "znas", "prosim", "mohl", "mohla",
    "bys", "byste", "nachazeji", "nachazi", "vsechno", "vsechny", "nejake",
}


def lessons_for_topic(diary_db_path: str, text: str, limit: int = 3,
                      scan: int = 500) -> list:
    """HANS_LESSON_BY_TOPIC_V1 — lekce k TÉMATU dotazu, BEZ časového okna.

    Proč to existuje: `recent_lessons` je čistě časové (48-72 h), takže oprava
    zmizí z kontextu za pár dní, zatímco omyl leží v RAGu napořád. Ta asymetrie
    znamená, že omyl časem VŽDY vyhraje — doloženo Gutštejnem (opraven 28.7.,
    znovu tvrzen 7.8., 21.8. i 24.8.). Proto se lekce hledá podle TÉMATU
    a nikdy neexpiruje.

    Matchuje se proti KONKRÉTNÍM polím (`entity` / `claim` / `correction`),
    NE proti obecné próze ponaučení — „Musím si ověřovat informace." by jinak
    sedělo na cokoliv a zaplavilo kontext. České skloňování řeší `_tok_match`
    ze sdíleného entity store (prefixová shoda), takže „hrady"/„hradu" trefí
    „hrad". READ-ONLY, chyba → [].
    """
    if not (text or "").strip():
        return []
    try:
        from scripts.hans_entities import _tokens, _tok_match
    except Exception as e:
        _log.debug("lessons_for_topic: entity primitiva nedostupná: %s", e)
        return []
    try:
        from scripts.hans_recall import _STOPWORDS as _SW
    except Exception:
        _SW = set()
    q = [t for t in _tokens(text)
         if len(t) >= 4 and t not in _SW and t not in _TOPIC_STOP_EXTRA]
    if not q:
        return []
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT note, data FROM diary WHERE event_type='lesson_learned' "
            "ORDER BY ts DESC LIMIT ?", (int(scan),)).fetchall()
    except Exception as e:
        _log.debug("lessons_for_topic failed: %s", e)
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    # HANS_LESSON_BY_TOPIC_DF_V1 — rozlišující token poznáme podle toho, že je
    # v korpusu lekcí VZÁCNÝ, ne podle velkého písmene. Kapitálky byly špatný
    # proxy v OBOU směrech: pouštěly „Česko"/„Hans" a zahazovaly „fotbale"
    # (malé písmeno) i „2026" (číslice) — „kdo pořádá MS 2026?" pak nenašlo
    # vlastní lekci o MS. Změřeno na 41 reálných lekcích: šum má vysoké df
    # (hans 14, standa 10, nemam 10, pane 6, zaznam 5), obsah má df ≤ 3
    # (fotbale 1, francii 1, jana 1, kost 1, gotika 2, hrad 3).
    docs = []
    for note, data in rows:
        note = (note or "").strip()
        if not note:
            continue
        try:
            d = json.loads(data or "{}")
        except Exception:
            d = {}
        hay = " ".join(str(d.get(k, "") or "")
                       for k in ("entity", "claim", "correction")).strip()
        if not hay:
            continue        # bez konkrétních polí není co vázat na téma
        docs.append((note, d, set(t for t in _tokens(hay) if len(t) >= 4)))
    if not docs:
        return []
    df = {}
    for _n, _d, _s in docs:
        for t in _s:
            df[t] = df.get(t, 0) + 1
    max_df = max(3, len(docs) // 10)
    # HANS_LESSON_BY_TOPIC_RANK_V1 — řadit podle SPECIFIČNOSTI trefy, ne podle
    # stáří. Bez toho uřízl limit tu pravou lekci: „kdo pořádá MS 2026?" trefilo
    # `fotbale` (df 1) i `2026` (df 3), jenže tři novější lekce se shodou na
    # `2026` se dostaly před ni. Vyhrává nejvzácnější trefený token, při shodě
    # novější lekce.
    scored = []
    for _i, (note, d, htok) in enumerate(docs):
        htok = [t for t in htok if df.get(t, 0) <= max_df]
        if not htok:
            continue
        # HANS_LESSON_BY_TOPIC_EXACT_V1 — PŘESNÁ shoda tokenu má přednost před
        # skloňovanou. `_tok_match` je prefixové, takže občas spáruje nesouvisející
        # slova („porada"~„pořádku", obojí prefix „porad") a protože takový token
        # bývá vzácný, prolezl by nahoru. Tier 0 = přesná shoda, tier 1 = shoda
        # přes skloňování (ta je pořád potřeba: „hrady"→„hrad", „Francie"→„Francii").
        best = None
        for a in q:
            for b in htok:
                if a == b:
                    _key = (0, df.get(b, 99))
                elif _tok_match(a, b):
                    _key = (1, df.get(b, 99))
                else:
                    continue
                if best is None or _key < best:
                    best = _key
        if best is None:
            continue
        _corr = str(d.get("correction", "") or "").strip()
        scored.append((best, _i, _corr if len(_corr) >= 8 else note, note))
    scored.sort(key=lambda r: (r[0], r[1]))   # (přesnost, vzácnost), pak novost
    out, seen = [], set()
    for _best, _i, _item, note in scored:
        if _item in seen or note in seen:
            continue
        seen.add(_item)
        seen.add(note)
        out.append(_item)
        if len(out) >= int(limit):
            break
    if out:
        _log.debug("lessons_for_topic: %d lekcí k tématu %r", len(out), text[:60])
    return out


def scan_overnight_lessons(diary_db_path: str, since: float) -> int:
    """READ-ONLY: počet lekcí uložených od `since` (pro ranní mood nudge)."""
    conn = None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        n = conn.execute(
            "SELECT COUNT(*) FROM diary WHERE event_type='lesson_learned' "
            "AND ts > ?", (since,)).fetchone()[0]
        return int(n or 0)
    except Exception as e:
        _log.debug("scan_overnight_lessons failed: %s", e)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    import tempfile, os
    print("=== hans_lessons smoke (temp DB) ===")
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, event_type TEXT, "
               "title TEXT, note TEXT, data TEXT)")
    db.execute("INSERT INTO diary(ts,event_type,title,note) VALUES (?,?,?,?)",
               (time.time(), "lesson_learned", "standa",
                "Nemám si domýšlet, co lidé mají rádi, když to nevím."))
    db.commit(); db.close()
    print("recent_lessons:", recent_lessons(p))
    print("scan_overnight_lessons:", scan_overnight_lessons(p, time.time() - 3600))
    os.unlink(p)
