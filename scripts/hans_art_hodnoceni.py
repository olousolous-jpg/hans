"""Funkce přesunuté z `scripts/hans_art.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_ha.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time

def _lekce_z_vytky_premalovani(db_path: str, okno_s: float = 900.0) -> bool:
    """Je poslední lekce odvozená z výtky, po které se právě přemalovává?
    (žádost o opakování v posledních 15 min a nejnovější lekce není starší
    než ona). Chyba → False."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        fb = con.execute(
            "SELECT ts FROM diary WHERE event_type='art_feedback' AND ts > ? "
            "AND data LIKE '%\"matrix_opakuj\"%' ORDER BY ts DESC LIMIT 1",
            (time.time() - okno_s,)).fetchone()
        le = con.execute("SELECT ts FROM diary WHERE event_type='art_lesson' "
                         "AND note IS NOT NULL AND note!='' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        return bool(fb and le and le[0] >= fb[0] - 5)
    except Exception:
        return False


def _lekce_splnena(klicova: str, lekce: str, popis: str) -> tuple:
    """(splněno: bool|None, nalezeno[], chybí[]) — porovná kmeny klíčových slov
    (jinak slov lekce) s popisem obrazu. None = není z čeho soudit."""
    zdroj = klicova or lekce or ""
    slova = [w for w in re.findall(r"[a-z]{5,}", zdroj.lower()) if w not in _ha._KW_STOP]
    kmeny = list(dict.fromkeys(w[:6] for w in slova))
    if not kmeny or not popis:
        return None, [], []
    low = popis.lower()
    nal = [k for k in kmeny if k in low]
    chybi = [k for k in kmeny if k not in low]
    return (len(nal) / len(kmeny) >= 0.34), nal, chybi


def feedback_rating(text: str):
    """+1 / -1 / None z textu nebo klíče reakce."""
    low = (text or "").lower()
    if any(x in low for x in _ha._PALEC_DOLU):
        return -1
    if any(x in low for x in _ha._PALEC_NAHORU):
        return 1
    return None


def record_art_feedback(db_path: str, artwork_rowid, title: str, rating=None,
                        comment: str = "", person: str = "", via: str = "") -> bool:
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "art_feedback", title or "",
             ("%s %s" % ({1: "👍", -1: "👎"}.get(rating, ""), comment or "")).strip(),
             json.dumps({"artwork_rowid": artwork_rowid, "rating": rating,
                         "comment": comment or "", "person": person, "via": via},
                        ensure_ascii=False)))
        db.commit()
        db.close()
        _ha._log.info("art: HANS_ART_FEEDBACK_V1 %s k „%s“: %s %.80s", person,
                  title, {1: "👍", -1: "👎"}.get(rating, "·"), comment or "")
        return True
    except Exception as e:
        _ha._log.warning("art: zápis zpětné vazby selhal: %s", e)
        return False


def namet_ztratil_jmeno(puvodni: str, novy: str) -> bool:
    fp, fn = _ha._fb_fold(puvodni), _ha._fb_fold(novy)
    for m in list(_ha._NAMET_JMENO.finditer(puvodni or ""))[1:] or []:
        if _ha._fb_fold(m.group(0))[:4] not in fn:
            return True
    return bool(set(_ha._NAMET_OBECNE.findall(fn)) - set(_ha._NAMET_OBECNE.findall(fp)))


def je_zadost_o_opakovani(text: str) -> bool:
    return bool(_ha._OPAKUJ.search(text or ""))


def _fb_fold(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", (text or "").lower())
                   if unicodedata.category(c) != "Mn")


def je_slovni_hodnoceni(text: str, minut_od_doruceni: float,
                        blizko_min: float = 10.0) -> bool:
    """Je zpráva slovním hodnocením naposledy doručeného obrazu? Slovo o obraze
    kdykoli v okně, nebo hodnotící věta hned po doručení. Příkaz, otázka
    a nová žádost o malbu hodnocením nejsou."""
    f = _ha._fb_fold(text)
    if not f.strip() or _ha._FB_NE.search(f):
        return False
    if _ha._FB_SLOVO.search(f):
        return True
    return (0 <= minut_od_doruceni <= blizko_min and len(f.split()) >= 3
            and bool(_ha._FB_SOUD.search(f)))


def opraveny_namet(config: dict, puvodni: str, pripominka: str) -> str:
    """Původní námět + připomínka → opravený námět (rezidentní hans-czech).
    Změřeno 6/6 („zralok zapasi s ponorkou“ + „mel jsem na mysli okusovat…“
    → „Zralok okusuje ponorku.“). Chyba → původní námět."""
    try:
        from scripts.ollama_client import ollama_generate
        acf = _ha._acfg(config)
        out = ollama_generate(
            str(acf.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")),
            "Původní zadání: %s\nPřipomínka: %s\nOpravené zadání:" % (puvodni, pripominka),
            system=_ha._OPRAVA_SYS, config=config, timeout=60,
            options={"temperature": 0.2, "num_predict": 60})
        v = ((out or "").strip().splitlines() or [""])[0].strip().strip('"„“').rstrip(".")
        if v and _ha.namet_ztratil_jmeno(puvodni, v):
            _ha._log.info("art: HANS_ART_REPAINT_KEEP_NAMES_V1 opravený námět „%s“ "
                      "ztratil jméno → původní zadání", v[:80])
            return puvodni
        return v[:160] or puvodni
    except Exception as e:
        _ha._log.warning("art: opravený námět selhal: %s", e)
        return puvodni


def rederive_lesson_with_feedback(config: dict, db_path: str, artwork_rowid) -> str:
    """HANS_ART_FEEDBACK_V2 — po lidském hodnocení odvoď lekci k TOMU obrazu
    znovu, hned. Nová lekce je nejnovější → příští obraz ji dostane do zadání
    (klíčová slova se dopočítají lazy). Dřív se hodnocení projevilo až
    u přespříštího obrazu. Vrací novou lekci nebo ''."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute("SELECT title, note, data FROM diary WHERE rowid=? "
                          "AND event_type='artwork'", (artwork_rowid,)).fetchone()
        fbs = con.execute("SELECT data FROM diary WHERE event_type='art_feedback' "
                          "ORDER BY ts DESC").fetchall()
        con.close()
    except Exception as e:
        _ha._log.warning("art: rederive — čtení selhalo: %s", e)
        return ""
    if not row:
        return ""
    title, verdict, data = row
    _zadani = ""
    try:
        _dj = json.loads(data or "{}") or {}
        vision = _dj.get("vision", "")
        if _dj.get("source") == "subject":        # HANS_ART_SUBJECT_CHECK_V1
            _sc = _dj.get("subject_check") or {}
            _zadani = (title or "") + ((" (English: %s)" % _sc["en"]) if _sc.get("en") else "")
            if _sc.get("match") == "no" and _sc.get("instead"):
                _zadani += " — an independent check found the image shows instead: %s" % _sc["instead"]
    except Exception:
        vision = ""
    rating, koment = None, []
    for (d,) in fbs:
        try:
            j = json.loads(d or "{}")
        except Exception:
            continue
        if j.get("artwork_rowid") != artwork_rowid:
            continue
        if rating is None and j.get("rating") is not None:
            rating = j["rating"]
        if j.get("comment"):
            koment.append(j["comment"])
    if rating is None and not koment:
        return ""
    lesson = _ha._derive_art_lesson(config, db_path, title, vision, verdict or "",
                                store=True, feedback_this=(rating, " / ".join(koment)),
                                zadani=_zadani)
    # HANS_ART_LESSON_PENDING_V1 — model nedostupný (herní mód, PC spí) → dohnat
    if vision:
        _ha._lekce_ceka_zmen(artwork_rowid, pridat=not lesson)
    _ha._log.info("art: HANS_ART_FEEDBACK_V2 lekce k „%s“ po hodnocení: %.120s", title, lesson)
    return lesson


def _lekce_ceka_nacti() -> dict:
    try:
        with open(_ha._LEKCE_CEKA, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _lekce_ceka_zmen(artwork_rowid, pridat: bool) -> None:
    try:
        d = _ha._lekce_ceka_nacti()
        k = str(artwork_rowid)
        if pridat and k not in d:
            d[k] = time.time()
            _ha._log.info("art: HANS_ART_LESSON_PENDING_V1 lekce k obrazu %s odložena "
                      "(model nedostupný), čeká %d", k, len(d))
        elif not pridat and k in d:
            d.pop(k)
        else:
            return
        if d:
            with open(_ha._LEKCE_CEKA, "w", encoding="utf-8") as f:
                json.dump(d, f)
        elif os.path.exists(_ha._LEKCE_CEKA):
            os.remove(_ha._LEKCE_CEKA)
    except Exception as e:
        _ha._log.debug("art: fronta lekcí: %s", e)


def recent_art_feedback(db_path: str, days: int = 21, limit: int = 3) -> list:
    """[(titul, rating, komentář)] nejnovější první, sloučené po obraze."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        rows = con.execute(
            "SELECT title, data FROM diary WHERE event_type='art_feedback' "
            "AND ts>=? ORDER BY ts DESC", (time.time() - days * 86400,)).fetchall()
        con.close()
    except Exception:
        return []
    po = {}
    for title, data in rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            continue
        k = d.get("artwork_rowid") or title
        r = po.setdefault(k, [title, None, []])
        if r[1] is None and d.get("rating") is not None:
            r[1] = d.get("rating")
        if d.get("comment"):
            r[2].append(d["comment"])
    out = [(t, r, " / ".join(c)) for t, r, c in po.values()]
    return out[:limit]


def _caption(reflection: str, title: str) -> str:
    """Krátký český popisek = první věta reflexe, fallback název knihy.
    Slouží jako FALLBACK, když Hansovo hodnocení (HANS_ART_VERDICT_V1) selže."""
    r = (reflection or "").strip()
    if not r:
        return f'Inspirováno knihou „{title}".'
    m = re.split(r"(?<=[.!?])\s", r, maxsplit=1)
    first = m[0].strip()
    return (first[:160] + ("…" if len(first) > 160 else "")) if first else \
        f'Inspirováno knihou „{title}".'


def _describe_render(config: dict, dest_path: str) -> str:
    """B (vize): llava popíše SKUTEČNÝ vyrenderovaný obraz. keep_alive=0 (VRAM
    on-demand). Běží PO _comfy_free, PŘED warmem hans-czech. '' při selhání."""
    try:
        import base64
        with open(dest_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        _ha._log.debug("art: read render for vision failed: %s", e)
        return ""
    acfg = _ha._acfg(config)
    model = str(acfg.get("vision_model")
                or (config.get("room_observer", {}) or {}).get("model")
                or "qwen2.5vl:7b")
    try:
        from scripts.ollama_client import ollama_generate
        desc = ollama_generate(
            model, _ha._VISION_PROMPT, images=[b64], config=config,
            timeout=int(acfg.get("vision_timeout", 90)), keep_alive=0)
    except Exception as e:
        _ha._log.warning("art: vision describe failed: %s", e)
        return ""
    desc = (desc or "").strip()
    if desc:
        _ha._log.info("art: vize obrazu: %.120s", desc)
    return desc


def _past_verdicts(db_path: str, limit: int = 5) -> list:
    """C (vyvíjející se vkus): Hansovy minulé verdikty o vlastních obrazech."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        rows = con.execute(
            "SELECT title, note FROM diary WHERE event_type='artwork' "
            "AND note IS NOT NULL AND note!='' ORDER BY ts DESC LIMIT ?",
            (limit,)).fetchall()
        con.close()
        return [(r[0] or "kniha", r[1]) for r in rows if r and r[1]]
    except Exception as e:
        _ha._log.debug("art: past_verdicts failed: %s", e)
        return []


def soud_nametu(config: dict, subject: str, grounded: str, vision_desc: str) -> dict:
    """{'match': 'yes'|'partly'|'no', 'missing': str, 'instead': str, 'en': str}
    nebo {} (nejde posoudit / model nedostupný). Nikdy nehází."""
    if not (subject and vision_desc):
        return {}
    try:
        from scripts.ollama_client import ollama_generate
        acf = _ha._acfg(config)
        m = _ha._EN_V_ZADANI.search(grounded or "")
        en = m.group(1).strip() if m else ""
        en = en or _ha._posledni_preklad.get(subject, "")     # V3
        out = ollama_generate(
            str(acf.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")),
            "ORDER: %s%s\n\nDESCRIPTION:\n%s" % (
                subject, (" (known English name: %s)" % en) if en else "",
                vision_desc[:1500]),
            system=_ha._SOUD_NAMETU_SYS, config=config, timeout=90,
            options={"temperature": 0.0, "num_predict": 90})
        o = (out or "").strip().replace("\n", " ")
        mm = re.search(r"MATCH:\s*(yes|partly|no)", o, re.I)
        if not mm:
            return {}
        cist = lambda x: re.sub(r"^[\s<\-\u2013>]+|[\s<>\-\u2013;.]+$", "", x or "")[:120]
        mi = re.search(r"MISSING:\s*(.*?)(?:;?\s*(?:->)?\s*INSTEAD:|$)", o, re.I)
        ins = re.search(r"INSTEAD:\s*(.*)$", o, re.I)
        r = {"match": mm.group(1).lower(), "missing": cist(mi.group(1) if mi else ""),
             "instead": cist(ins.group(1) if ins else ""), "en": en,
             # V2: námět s osobou/postavou (ukotvení nese vzhled postavy / podobu)
             "osoba": bool(re.search(r"Vzhled postavy|podob[au] osoby", grounded or ""))}
        _ha._log.info("art: HANS_ART_SUBJECT_CHECK_V1 „%s“ → %s (chybí: %s; místo toho: %s)",
                  subject[:50], r["match"], r["missing"] or "-", r["instead"] or "-")
        return r
    except Exception as e:
        _ha._log.debug("art: soud námětu selhal: %s", e)
        return {}


def soud_castecne_nesoulad(soud: dict) -> bool:
    """`partly` + pojmenované chybějící + místo toho něco jiného, a chybějící
    není vlastní jméno ani jde o námět s osobou/postavou (`osoba` v soudu)."""
    s = soud or {}
    if s.get("match") != "partly" or not s.get("missing") or not s.get("instead"):
        return False
    if s.get("osoba"):
        return False
    return not re.search(r"\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ]\w+", s["missing"])


def _evaluate_artwork(config: dict, db_path: str, title: str,
                      reflection: str, vision_desc: str,
                      source_label: str = "knihou",
                      soud: dict = None) -> str:
    """Hansovo hodnocení (hans-czech persona): co namaloval + jestli se mu obraz
    povedl/líbí — reaguje na SKUTEČNOU kvalitu (llava popis) a svůj vyvíjející se
    vkus (minulé verdikty). Vrací český text = caption. Fallback _caption.
    HANS_DREAMS_V1: source_label = čím se obraz inspiroval (knihou / svým snem)."""
    fallback = _ha._caption(reflection, title)
    if not vision_desc:
        return fallback
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return fallback
    try:
        from scripts.hans_persona import persona_core
        core = persona_core(config, with_address=False)
    except Exception:
        core = ""
    acfg = _ha._acfg(config)
    model = str(acfg.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest"))
    # HANS_ART_PROGRESS_V1 — uzavřít smyčku verdikt→ponaučení→verdikt:
    # předchozí ponaučení (= záměr, který vedl TENHLE obraz) předáme do verdiktu,
    # ať Hans posoudí, jestli se ho povedlo naplnit (narativ pokroku). Minulé
    # verdikty NEechujeme syrově (to plodilo šablonu "světlo+barvy / kompozice") —
    # předáme je jen jako anti-repetiční pokyn "všimni si něčeho jiného".
    prior_lessons = _ha._recent_lessons(db_path, 1)
    prior_lesson = prior_lessons[0] if prior_lessons else ""
    past = _ha._past_verdicts(db_path, limit=2)
    progress_block = ""
    if prior_lesson:
        # HANS_ART_VERDICT_GROUNDED_V1 — splnění spočtené z popisu jako FAKT
        _pl, _pkw = _ha._lesson_keywords(config, db_path)
        _ok, _nal, _chybi = _ha._lekce_splnena(_pkw, prior_lesson, vision_desc)
        if _ok is True:
            _fakt = ("Podle nezávislého popisu se to na obraze PROJEVILO "
                     "(v popisu je: %s)." % ", ".join(_nal))
        elif _ok is False:
            _fakt = ("Podle nezávislého popisu se to na obraze NEPROJEVILO "
                     "(v popisu chybí: %s). Nepiš, že se to povedlo." % ", ".join(_chybi[:5]))
        else:
            _fakt = "Z popisu se to posoudit nedá — nevyjadřuj se k tomu."
        progress_block = (
            "Před tímto obrazem sis předsevzal zlepšit toto:\n„%s\"\n%s\n"
            "V první větě to věcně řekni, bez přikrašlování.\n\n"
            % (prior_lesson, _fakt))
        _ha._log.info("art: HANS_ART_VERDICT_GROUNDED_V1 lekce splněna=%s", _ok)
    antirepeat_block = ""
    if past:
        antirepeat_block = (
            "Takto ses vyjádřil k posledním obrazům — NEopakuj stejnou chválu "
            "ani stejnou výtku, všimni si pokaždé NĚČEHO JINÉHO:\n"
            + "\n".join('- %s' % n for _t, n in past) + "\n\n")
    past_block = progress_block + antirepeat_block
    system = (core + "\n\n" if core else "") + (
        "Právě jsi dokončil obraz inspirovaný " + source_label + ". Níže máš NEZÁVISLÝ "
        "popis toho, co je na plátně vidět. Napiš 2-3 věty v první osobě: co jsi "
        "namaloval a jak hodnotíš výsledek. Buď UPŘÍMNÝ: když se obraz prostě "
        "povedl, klidně ho oceň bez výhrad — výtku přidej JEN když je v popisu "
        "vidět opravdový nedostatek. A pokaždé si všímej JINÉHO aspektu "
        "(kompozice, světlo, barvy, detail, nálada, perspektiva, textura), ne "
        "pořád téhož. DŮLEŽITÉ: hodnoť POUZE to, co je v nezávislém popisu — pokud "
        "na obraze nejsou lidé/postavy, VŮBEC nepiš o postavách, jejich držení těla "
        "ani póze (nevymýšlej si je) a NEpovažuj nepřítomnost lidí za nedostatek "
        "(architektura, krajina a zátiší mají být bez lidí). Nevymýšlej si vady ani nepřeháněj drobnosti. "
        "Pokud sis z minula něco předsevzal a týká se to tohoto obrazu, navaž na to "
        "a řekni, jestli ses posunul. Žádné uvozovky, žádný nadpis.")
    user = (past_block
            + "Kniha: %s\n" % title
            + "Tvá reflexe knihy: %s\n\n" % (reflection or "")[:400]
            + "Co je na obrazu skutečně vidět (nezávislý popis):\n%s\n\n" % vision_desc
            + "Napiš svůj verdikt.")
    try:
        # HANS_ART_VERDICT_LEN_V1 — bez num_predict se verdikt sekal na výchozím
        # limitu Ollamy (~128 tok); dej mu prostor na celou kritiku.
        out = ollama_generate(model, user, system=system, config=config,
                              timeout=int(acfg.get("verdict_timeout", 120)),
                              options={"num_predict":
                                       int(acfg.get("verdict_num_predict", 320))})
    except Exception as e:
        _ha._log.warning("art: verdict LLM failed: %s", e)
        return fallback
    out = (out or "").strip().strip('"')
    # HANS_ART_SUBJECT_CHECK_V1 — nezávislý soud se k verdiktu PŘIPÍŠE kódem.
    # Jako pokyn v zadání ho model respektoval jen 1× ze 3 (měřeno 6. 10.).
    if out and (soud or {}).get("match") == "no":
        out = ("Zadaný námět („%s“) se mi nezdařil — podle nezávislého popisu "
               "na obraze není. %s" % (title, out))
    # HANS_ART_SUBJECT_CHECK_V2 (6. 10.) — „partly“ s tím, co chybí, je u námětu
    # BEZ osoby taky nesoulad („chybí žlutí mimoni, místo toho opice“). U osob
    # a postav se nebere: popis obrazu nikoho nejmenuje, „chybí <Jméno>“ je
    # tam pokaždé.
    elif out and _ha.soud_castecne_nesoulad(soud):
        out = ("Zadání („%s“) jsem splnil jen zčásti — podle nezávislého popisu "
               "na obraze chybí: %s. %s" % (title, soud["missing"], out))
    if out:
        _ha._log.info("art: Hansův verdikt: %.120s", out)
        # ořez na CELOU větu (ne uprostřed) — hard cap až kdyby to ujelo
        cap = int(acfg.get("verdict_max_chars", 900))
        if len(out) > cap:
            cut = max(out.rfind(". ", 0, cap), out.rfind("! ", 0, cap),
                      out.rfind("? ", 0, cap))
            out = out[:cut + 1] if cut > cap // 2 else out[:cap]
        return out
    return fallback


def _covered_aspects(db_path: str, days: int = 30, min_n: int = 4) -> list:
    """HANS_ART_COVERED_ASPECTS_V1 — které aspekty už Hans řeší dokola.

    Vrací [(aspekt, kolikrát)] sestupně, jen ty nad `min_n`. Slouží k tomu,
    aby art director VĚDĚL, co už je vytěžené, a šel jinam — `_recent_lessons`
    mu ukáže jen 3 poslední texty, což je ~37 h z osmi měsíců malování.
    Fail-safe: chyba → prázdný seznam (radši bez přehledu než bez ponaučení).
    """
    if not db_path:
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        rows = con.execute(
            "SELECT note FROM diary WHERE event_type='art_lesson' "
            "AND note IS NOT NULL AND note!='' AND ts>=?",
            (time.time() - days * 86400,)).fetchall()
        con.close()
    except Exception:
        return []
    poc = {}
    for (note,) in rows:
        low = (note or "").lower()
        for aspekt, slova in _ha._ART_ASPEKTY.items():
            if any(s in low for s in slova):
                poc[aspekt] = poc.get(aspekt, 0) + 1
    return sorted([(a, n) for a, n in poc.items() if n >= min_n],
                  key=lambda x: -x[1])


def _derive_art_lesson(config: dict, db_path: str, title: str,
                       vision_desc: str, verdict: str, store: bool = True,
                       feedback_this=None, zadani: str = "") -> str:
    """Odvodí ponaučení pro příští render z vize + verdiktu. Běží na hans-czech
    (warm, žádný extra model do VRAM). Uloží do deníku 'art_lesson' (když store).
    Vrací ponaučení nebo ''. Nikdy nehází."""
    if not vision_desc:
        return ""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return ""
    acfg = _ha._acfg(config)
    model = str(acfg.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest"))
    # HANS_ART_FEEDBACK_V3 — u lidského verdiktu o tomhle obraze bez přehledů
    recent = [] if feedback_this else _ha._recent_lessons(db_path, 3)
    recent_block = ""
    if recent:
        recent_block = ("Painter's recent guidance lines (do NOT repeat these — "
                        "build on them or move to a new aspect):\n"
                        + "\n".join("- %s" % r for r in recent) + "\n\n")
    # HANS_ART_COVERED_ASPECTS_V1 — 3 posledni texty pokryvaji ~37 h; bez tohohle
    # prehledu se model po ctvrtem obraze vrati k tomuze aspektu.
    _covered = [] if feedback_this else _ha._covered_aspects(
        db_path, days=int(acfg.get("covered_days", 30)),
        min_n=int(acfg.get("covered_min", 4)))
    if _covered:
        # ⚠️ Nestaci rict "tohle uz mas" — zmereno 10. 9., ze nad prahem je
        # VSECH SEDM aspektu (10-22x za 30 dni), takze "jdi jinam" nema kam.
        # Proto se davaji OBA konce: nejvytezenejsi (vyhni se) i nejmene
        # probrany (tam je prostor) — pozitivni smer misto samotneho zakazu.
        _dny = int(acfg.get("covered_days", 30))
        _nej = _covered[:3]
        _mez = [x for x in _covered[-2:] if x not in _nej]
        recent_block += (
            "Aspects the painter has worked on MOST in the last %d days "
            "(well covered — avoid unless the verdict names a real problem):\n"
            % _dny
            + "\n".join("- %s (%dx)" % (a, n) for a, n in _nej) + "\n")
        if _mez:
            recent_block += (
                "LEAST explored lately — prefer one of these:\n"
                + "\n".join("- %s (only %dx)" % (a, n) for a, n in _mez)
                + "\n")
        recent_block += "\n"
        _ha._log.info("art: lesson zná vytěžené aspekty (nej: %s, mezera: %s)",
                  _nej[0][0] if _nej else "?",
                  _mez[0][0] if _mez else "—")
    # HANS_ART_FEEDBACK_V1 — lidský soud má přednost před vlastním verdiktem
    _fb = [] if feedback_this else _ha.recent_art_feedback(db_path)
    if _fb:
        recent_block += (
            "HUMAN FEEDBACK on recent paintings (the real judge — it OUTRANKS the "
            "painter's own verdict; build the guidance on it first):\n"
            + "\n".join("- %s: %s%s" % (
                t_, {1: "liked it", -1: "did NOT like it"}.get(r_, "commented"),
                (" — \"%s\"" % c_) if c_ else "") for t_, r_, c_ in _fb) + "\n\n")
        _ha._log.info("art: lesson zná %d lidských hodnocení", len(_fb))
    # HANS_ART_FEEDBACK_V2 — lidský soud o TOMHLE obraze je verdikt, ne vodítko.
    # Doloženo 24. 9.: 👍 „není mu co vytknout“ a lekce přesto opravovala
    # měřítko — výtku si vzala z Hansova vlastního verdiktu.
    if feedback_this:
        _r, _c = feedback_this
        if _r == 1:
            _pokyn = ("The human LIKED this image%s. Do NOT fix anything the human "
                      "did not criticise and ignore the painter's own doubts. Output "
                      "guidance that KEEPS what worked here — name it concretely "
                      "from the description." % ((" and said: \"%s\"" % _c) if _c else ""))
        elif _r == -1:
            _pokyn = ("The human DID NOT LIKE this image%s. Build the guidance on "
                      "fixing exactly that; the painter's own praise does not count."
                      % ((" and said: \"%s\"" % _c) if _c else ""))
        else:
            _pokyn = ("The human commented on this image: \"%s\". Build the "
                      "guidance on that comment first." % _c)
        # HANS_ART_SUBJECT_CHECK_V1 — bez zadání model výtku „není to tučňák“
        # (psanou bez diakritiky) nepřečetl a radil „zvětši tvora“.
        if zadani:
            recent_block += "THE ORDER for this image was: %s\n\n" % zadani
        recent_block += "HUMAN VERDICT ON THIS VERY IMAGE (final word):\n%s\n\n" % _pokyn
    user = (recent_block
            + "Independent description of the rendered image:\n%s\n\n"
            "Painter's verdict:\n%s\n\nWrite the ONE-line guidance."
            % (vision_desc, "(withheld)" if feedback_this else verdict))
    try:
        raw = ollama_generate(model, user,
                              system=(_ha._LESSON_SYSTEM_HUMAN if feedback_this
                                      else _ha._LESSON_SYSTEM), config=config,
                              timeout=int(acfg.get("lesson_timeout", 90)))
    except Exception as e:
        _ha._log.warning("art: lesson LLM failed: %s", e)
        return ""
    lesson = (raw or "").strip().strip('"').replace("\n", " ")[:200]
    if not lesson:
        return ""
    if store:
        try:
            db = sqlite3.connect(db_path, timeout=5.0)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "art_lesson", title, lesson))
            db.commit()
            db.close()
            _ha._log.info("art: ponaučení uloženo: %.120s", lesson)
        except Exception as e:
            _ha._log.warning("art: ulož lesson failed: %s", e)
    return lesson


def _recent_lessons(db_path: str, limit: int = 3) -> list:
    """Posledních N ponaučení (nejnovější první), deduped na text."""
    if not db_path:
        return []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        rows = con.execute(
            "SELECT note FROM diary WHERE event_type='art_lesson' "
            "AND note IS NOT NULL AND note!='' ORDER BY ts DESC LIMIT ?",
            (limit * 3,)).fetchall()
        con.close()
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        t = (r[0] or "").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= limit:
            break
    return out


def _person_negative(db_path: str) -> str:
    """HANS_PERSON_NEG_V1 (5.8.) — negativ pro portrét CIZÍ osoby.

    Dvě opravy naráz:
      (1) `_NEG_BASE` místo `_NEG` → bez avatarového anti-driftu (jinak měl
          Dalí v negativu „moustache" a Jack Black „beard").
      (2) cesta podoby osoby si stavěla workflow sama a `_lesson_negatives`
          NIKDY nevolala (ty jsou jen v `_render_image`) → Hans se na ní
          nemohl poučit, ani kdyby si ponaučení o rukách zapsal. Teď se
          napojují, plus ruce natvrdo: portrét je má skoro vždy v záběru
          a jsou to nejslabší místo modelu.

    Ruce se NEŘEŠÍ oříznutím kompozice (zvažováno, uživatel zamítl 5.8.):
    radši znetvořené ruce v obraze než ohýbat kompozici, aby nebyly vidět."""
    parts = [_ha._NEG_BASE, _ha._NEG_HANDS]
    try:
        extra = _ha._lesson_negatives(_ha._recent_lessons(db_path))
    except Exception:
        extra = ""
    if extra:
        parts.append(extra)
    return ", ".join(dict.fromkeys(", ".join(parts).split(", ")))


def _lesson_negatives(lessons: list) -> str:
    """Deterministicky odvodí extra negativní termy z ponaučení (keyword trigger)."""
    blob = " ".join(lessons).lower()
    neg = []
    if any(k in blob for k in ("hand", "finger", "ruce", "ruka", "prst")):
        neg += ["deformed hands", "extra fingers", "mutated hands"]
    if any(k in blob for k in ("face", "facial", "obličej", "tvář", "anatom")):
        neg += ["malformed face", "distorted facial features"]
    if any(k in blob for k in ("figure", "body", "person", "postav", "figur", "limb")):
        neg += ["awkward pose", "elongated limbs"]
    return ", ".join(dict.fromkeys(neg))  # dedup, zachovej pořadí

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_art as _ha  # noqa: E402
