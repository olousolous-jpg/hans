"""Funkce přesunuté z `scripts/hans_art.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_ha.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Optional
import json
import re
import sqlite3
import time

def _strip_cjk(text: str) -> str:
    """Odstraň CJK znaky (qwen2.5 občas ujede do čínštiny/japonštiny) a sjednoť
    mezery/interpunkci, ať zbyde čistý anglický SDXL prompt."""
    t = _ha._CJK_RE.sub(" ", text or "")
    t = re.sub(r"\s*[-–—,]\s*(?=[,\.])", " ", t)   # osamělé spojky po stripu
    t = re.sub(r"\s+", " ", t).strip(" ,-–—")
    return t


def _pick_medium() -> str:
    import random
    pool = [m for m in _ha._MEDIA if m != _ha._last_medium["v"]] or _ha._MEDIA
    m = random.choice(pool)
    _ha._last_medium["v"] = m
    return m


def _ascii_fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def _looks_english(s: str) -> bool:
    """HANS_ART_SAFE_FALLBACK_V1 — je NÁMĚT bezpečně anglicky pro FLUX? False při
    české diakustice (ě š č ř ž ů ň ď ť) NEBO běžném českém slově (i bez
    diakritiky: „domov"/„les"). Konzervativní: při pochybnosti False (radši
    odlož než malovat garbage)."""
    if not s:
        return False
    if re.search(r"[ěščřžůňďťýáíéóúĚŠČŘŽŮŇĎŤÝÁÍÉÓÚ]", s):
        return False
    toks = re.findall(r"[a-z]+", _ha._ascii_fold(s).lower())
    return not any(t in _ha._CZ_COMMON for t in toks)


def _cs_leak(subject_cs: str, prompt_en: str) -> str:
    """HANS_ART_CS_LEAK_V1 (5.8.) — zůstalo v anglickém promptu ČESKÉ slovo?

    Doloženo: „namaluj zenskeho kentaura" → prompt „A female **kentaur** …"
    (3 pokusy ze 3). FLUX slovo nezná, chytne se zbytku („equine forms")
    a namaluje KONĚ. Anglicky je to `centaur` — model transliteroval místo
    aby přeložil.

    Heuristika: vezmi z českého námětu slova délky ≥6, ustřihni 2 znaky
    koncovky (skloňování) a hledej kmen v anglickém promptu. „kentaura" →
    „kentaur" → nalezeno = únik. Falešný poplach je levný (jeden překlad
    navíc), takže se hraje na jistotu."""
    low = (prompt_en or "").lower()
    for w in _ha._CS_WORD_RE.findall((subject_cs or "").lower()):
        stem = w[:-2]
        if len(stem) >= 5 and stem in low:
            return w
    return ""


# HANS_ART_SUBJECT_EN_V3 (6. 10.) — PRVNÍ SLOVO námětu bez diakritiky, které
# překlad ztratil („tucnaky“ → peanuts, „mroze“ → hares, „smouly“ → owls).
# Brána = zpětný překlad: nevrátí-li se kmen prvního slova, dohledá se to
# slovo na cs.wikipedii a anglický název hesla jde překladači jako slovník.
# Měřeno na 34 námětech: 24 → 30 správně, 35 dotazů na Wikipedii; varianta
# se všemi slovy měla stejný zisk, ale šum („vyznamenani = Order“) a 53 dotazů.
# Do paměti entit se NIC neukládá (obecné slovo by ji zaneslo).
def _napoveda_prvniho_slova(config: dict, subject_cs: str, en0: str) -> tuple:
    """(slovo, anglický název) nebo ('', ''). Nikdy nehází."""
    try:
        prvni = re.findall(r"\w{4,}", subject_cs or "")[:1]
        if not prvni or not en0:
            return "", ""
        w = prvni[0]
        from scripts.ollama_client import ollama_generate
        _acf = _ha._acfg(config)
        zp = ollama_generate(
            str(_acf.get("subject_translate_model") or _acf.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")),
            "English: %s\nCzech:" % en0,
            system=("Translate the English phrase into CZECH. Output ONLY the "
                    "Czech words, nothing else."),
            config=config, timeout=60,
            options={"temperature": 0.0, "num_predict": 30})
        k = _ha._fb_fold(w)[:4]
        if any(x[:4] == k for x in re.findall(r"\w{3,}", _ha._fb_fold(zp or ""))):
            return "", ""                       # slovo se vrátilo → překlad sedí
        from scripts.web_reader import WebReader
        for tit, _sc in WebReader(config).wikipedia_search_candidates(w, lang="cs", limit=3):
            if _ha._fb_fold(tit).split()[0][:4] == k and len(tit.split()) <= 2:
                time.sleep(1.5)                 # kvóta Wikipedie je sdílená se studiem
                en = _ha._en_name(tit, lang="cs") or ""
                if en:
                    return w, en
        return "", ""
    except Exception as e:
        _ha._log.debug("art: nápověda prvního slova selhala: %s", e)
        return "", ""


def _translate_subject(config: dict, subject_cs: str, en_nazev: str = "",
                       slovnik: str = "") -> str:
    """Český námět → anglicky, vyhrazeným krátkým dotazem (ne uvnitř psaní
    scény). Změřeno 5/5 správně vč. „vodníka" → water sprite a zachovaného
    „Karlštejn Castle". POUŽÍVÁ SE JEN JAKO NÁPOVĚDA při úniku — překládat
    rovnou celý námět je horší: „souboj kočky se psem" → „cat fight" (ztratí
    psa), protože překlad zkracuje."""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return ""
    # HANS_ART_SUBJECT_EN_V1 (24. 9.) — překládá ČESKÝ model (hans-czech,
    # rezidentní → 0 VRAM, BEZ keep_alive=0, to by ho vyhodilo z paměti).
    # Změřeno na 10 námětech: qwen2.5:7b 6/10 („zraloka“ → ripe banana,
    # „žraloka“ → crocodile, „kentaura“ → knight with horse head, „kočky se
    # psem“ → cat fight), hans-czech 10/10 vč. „cat versus dog fight“.
    # Zkracování z docstringu výš byla vada qwen, ne překladu jako takového.
    _acf = _ha._acfg(config)
    out = ollama_generate(
        str(_acf.get("subject_translate_model")
            or _acf.get("verdict_model")
            or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")),
        # HANS_ART_SUBJECT_EN_V2 (6. 10.) — námět bez diakritiky překládá model
        # špatně („tucnaky z madagaskaru“ → Madagascar jumping rats; i když
        # Wikipedie mezitím dohledala „The Penguins of Madagascar“, do zadání šlo
        # obojí a vyšla krysa). Zná-li ukotvení anglický název, dostane ho
        # i překladač: 9/9 správně včetně děje („…on the beach“).
        ("Czech: %s\nKnown English name: %s\nEnglish:" % (subject_cs, en_nazev))
        if en_nazev else
        ("Czech: %s\nDictionary: %s\nEnglish:" % (subject_cs, slovnik))   # V3
        if slovnik else ("Czech: %s\nEnglish:" % subject_cs),
        system=("Translate the Czech noun phrase into ENGLISH. Output ONLY the "
                "English words, 1-6 words, nothing else. Never transliterate — "
                "if it is a creature or thing, use its real English name."
                + (" If a known English name of something in the phrase is "
                   "given, use that name exactly." if en_nazev else "")
                + (" If a dictionary of some words is given, use it."
                   if (slovnik and not en_nazev) else "")),
        config=config, timeout=60,
        options={"temperature": 0.0, "num_predict": 24})
    return (out or "").strip().strip('."\'').splitlines()[0][:60] if out else ""


def _scene_prompt_core(config: dict, title: str, reflection: str, db_path: str = "",
                  system: str = None, source_intro: str = None,
                  en_fallback: str = None, prev: dict = None,
                  cs_subject: str = "", bez_zalohy: bool = False) -> Optional[str]:
    """LLM (levný, keep_alive=0) → anglický SDXL scene prompt. Fallback šablona.
    HANS_ART_LESSON_V1: když db_path, vloží do promptu ponaučení z minulých obrazů.
    HANS_DREAMS_V1: system+source_intro lze přepsat (snová varianta místo knižní)."""
    # HANS_ART_MEDIUM_VARIETY_V2 — vyber médium pro tuhle malbu (ne u fixních looků)
    _novary = {globals().get(n) for n in (
        "_HOME_SCENE_SYSTEM", "_STYLE_SCENE_SYSTEM", "_MOCKUP_SCENE_SYSTEM")}
    medium = _ha._pick_medium() if (system not in _novary) else ""
    _tail = medium if medium else "oil painting, atmospheric lighting, rich detail, masterful"
    # HANS_ART_FALLBACK_NEUTRAL_V1 — když scene-prompt LLM selže, sestav
    # SCENE-NEUTRÁLNÍ fallback řízený NÁMĚTEM (dřív „soft window light" =
    # interiérový bias → vždy tichý pokoj u okna; a syrový český {title} vč.
    # „(styl: X)" i instrukčních sloves leakoval do SDXL). LLM tu není (proto
    # fallback) → český námět nepřeložím, ale aspoň bez interiéru a instrukcí.
    _fsubj = title
    # HANS_ART_FALLBACK_GROUNDED_V1 (20.7.) — když LLM scene-prompter selže,
    # fallback dřív vzal syrový český `title` (např. „Arnolda Rimmera" v gen.).
    # SDXL neví, kdo to je → hádá pohlaví ze zvuku (Rimmer → žena, doloženo).
    # `reflection` je grounded text („Arnold Rimmer: Arnold Jidáš Rimmer,…") —
    # resolvený kanonický titul (Wiki lookup v `_ground_subject`). Vezmi z něj
    # canonical name pro fallback, ať SDXL dostane rozpoznatelný pojem.
    if reflection and ":" in reflection:
        _canon = reflection.split(":", 1)[0].strip()
        # grounded formát bývá „Name (English name to use in the image prompt: XXX)"
        _mp = re.search(r"English name to use in the image prompt:\s*([^)]+)",
                        _canon, re.I)
        if _mp:
            _canon = _mp.group(1).strip()
        else:
            _canon = re.sub(r"\s*\([^)]*\)\s*$", "", _canon).strip()
        if (_canon and _canon.lower() != _fsubj.lower()
                and 1 <= len(_canon.split()) <= 6):
            _fsubj = _canon
    _fstyle = ""
    _sm = re.search(r"\(styl:\s*([^)]+)\)", _fsubj)
    if _sm:                       # odděl „(styl: art nouveau)" → stylové klíč. slovo
        _fstyle = _sm.group(1).strip()
        _fsubj = (_fsubj[:_sm.start()] + _fsubj[_sm.end():]).strip()
    _fsubj = re.sub(               # strhni řetěz instrukčních sloves (nejsou námět)
        r"(?i)^\s*(?:(?:zkus|znovu|jinak|namaluj|namalovat|nakresli|nakreslit|"
        r"vytvo[řr](?:it)?|nam[aá]luj|p[řr]ekresli|p[řr]emaluj|oprav)\s+"
        r"(?:obr[aá]zek\s+|obraz\s+|jak\s+by\s+)?)+", "", _fsubj)
    _fsubj = _fsubj.strip(" \"'„“”.:!?-") or "an evocative scene"
    # HANS_ART_EN_TITLE_V1 — i ve fallbacku dej SDXL ROZPOZNATELNÝ anglický
    # název (Mimoni→Minions), když existuje langlink. Český námět SDXL nechápe.
    try:
        _en_fb = _ha._en_name(_fsubj,
                          lang=(config.get("curiosity", {}) or {}).get("wiki_lang", "cs"))
        if _en_fb:
            _fsubj = _en_fb
    except Exception:
        pass
    fallback = ", ".join(x for x in (_fsubj, _fstyle, _tail) if x)
    # HANS_ART_SAFE_FALLBACK_V1 — fallback (LLM dole) smí do FLUXu jen s
    # anglickým námětem; český → en_fallback od volajícího, jinak None = odlož.
    def _fb():
        # HANS_DREAM_NO_BARE_FALLBACK_V1 (14. 9.) — u SNU zadny zalozni prompt
        # neexistuje. Titul „Sen" se prelozil na „Dream", `_looks_english` ho
        # pustil a FLUX dostal jen „Dream, <medium>" — namaloval obecny sen
        # (14. 9.: zena v posteli misto zahrady s hodinami). Zmereno v galerii:
        # 10 ze 74 snovych obrazu takhle vzniklo bez obsahu snu. Radsi odlozit.
        if bez_zalohy:
            _ha._log.warning("art: scena se nesestavila a zalozni prompt tu nema "
                         "smysl — render odlozen, retry priste")
            return None
        # HANS_ART_CS_LEAK_V1 (5.8.) — `_looks_english` je DĚRAVÝ: pozná jen
        # diakritiku a seznam běžných českých slov, takže „zenskeho kentaura"
        # (uživatel psal bez háčků, ani jedno slovo v seznamu není) projde jako
        # angličtina a syrová čeština doteče do FLUXu. Doloženo 5.8.: LLM na
        # scénu vypršel, fallback poslal do modelu „zenskeho kentaura, gouache".
        # Rozšiřovat seznam slov je nekonečná práce → radši se ZEPTEJ na
        # překlad (krátký dotaz projde i tam, kde dlouhý na scénu vypršel).
        if cs_subject:
            _en = _ha._translate_subject(config, cs_subject)
            if _en and _ha._looks_english(_en) and not _ha._cs_leak(cs_subject, _en):
                _ha._log.info('art: fallback — český námět „%s" přeložen na „%s"',
                          cs_subject, _en)
                return ", ".join(x for x in (_en, _fstyle, _tail) if x)
        # Překlad nevyšel (mozek dole / zahlcený): NESMÍME spadnout zpátky na
        # `_looks_english`, ta český námět bez diakritiky propustí. Když v
        # námětu čeština prokazatelně zůstala, radši ODLOŽ render — tohle je
        # celý smysl HANS_ART_SAFE_FALLBACK_V1 (radši žádný obraz než garbage).
        if cs_subject and _ha._cs_leak(cs_subject, fallback):
            if en_fallback:
                return ", ".join(x for x in (en_fallback, _tail) if x)
            _ha._log.warning('art: námět „%s" se nepodařilo dostat do angličtiny '
                         '— render odložen (retry příště)', cs_subject)
            return None
        if _ha._looks_english(_fsubj):
            return fallback
        if en_fallback:
            return ", ".join(x for x in (en_fallback, _tail) if x)
        return None
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return _fb()
    acfg = _ha._acfg(config)
    model = str(acfg.get("prompt_model", "qwen2.5:7b"))
    user = (source_intro if source_intro is not None
            else f"Book: {title}\n\nReader's reflection (Czech):\n{reflection}\n\n")
    # HANS_ART_SUBJECT_EN_V1 — anglický název námětu VŽDY, ne až po úniku.
    # Doloženo 22. 9.: „ponorku v tlame zraloka“ → slon, „zraloka“ → žena
    # v knihovně, „žraloka“ → příšera v lese; v promptu žádné české slovo
    # nezůstalo, takže _cs_leak (níž) se nespustil — slovo se PŘELOŽILO ŠPATNĚ.
    # Vždy (1 dotaz na rezidentní model, ~1 s): `_looks_english` pustí češtinu
    # bez háčků („zraloka“) a `_cs_leak` nerozliší jazyk. U anglického námětu
    # vyjde překlad stejně a nápověda se nepřidá.
    if cs_subject:
        _zn = re.search(r"English name to use in the image prompt: ([^)\n]{2,80})\)", user)
        _en0 = _ha._translate_subject(config, cs_subject, _zn.group(1).strip() if _zn else "")
        if not _zn and _en0:                    # HANS_ART_SUBJECT_EN_V3
            _w1, _en1 = _ha._napoveda_prvniho_slova(config, cs_subject, _en0)
            if _en1:
                _en0b = _ha._translate_subject(config, cs_subject,
                                           slovnik="%s = %s" % (_w1, _en1))
                _ha._log.info('art: HANS_ART_SUBJECT_EN_V3 „%s“ = %s (Wikipedie) → '
                          'překlad „%s“ místo „%s“', _w1, _en1, _en0b, _en0)
                _en0 = _en0b or _en0
        if _en0:                                # HANS_ART_SUBJECT_CHECK_V3 — pro soud námětu
            _ha._posledni_preklad[cs_subject] = _en0
            while len(_ha._posledni_preklad) > 8:
                _ha._posledni_preklad.pop(next(iter(_ha._posledni_preklad)))
        if _en0 and _en0.lower() != cs_subject.lower() and not _ha._cs_leak(cs_subject, _en0):
            if cs_subject in user:
                user = user.replace(cs_subject, "%s (%s)" % (cs_subject, _en0), 1)
            else:
                user += "ENGLISH NAME OF THE SUBJECT: %s\n\n" % _en0
            _ha._log.info('art: HANS_ART_SUBJECT_EN_V1 námět „%s“ → „%s“', cs_subject, _en0)
    # HANS_ART_INTENT_V1 (5.8.) — TRVALÉ ZÁMĚRY mají přednost před posledními
    # ponaučeními. Ponaučení jsou reakce na JEDEN obraz a jsou zaměnitelná
    # (115 unikátních textů, ale pořád „introduce subtle X to enhance visual
    # depth") → tři náhodná z posledních dnů nedávají směr. Záměr říká, co
    # Hans dlouhodobě sleduje; destiluje ho týdenní reflexe z reálných děl.
    # Ponaučení zůstávají jako DOPLNĚK (konkrétní řemeslo), ne jako hlavní
    # vodítko — a když ještě žádný záměr není, chová se to jako dřív.
    _intents = []
    try:
        from scripts.hans_art_intent import active_intentions
        _intents = active_intentions(db_path)
    except Exception:
        _intents = []
    if _intents:
        user += ("YOUR STANDING ARTISTIC INTENT (what you pursue across works — "
                 "let it shape this piece):\n- " + "\n- ".join(_intents) + "\n\n")
        _ha._log.info("art: scene prompt nese %d trvalých záměrů", len(_intents))
    # HANS_ART_CONTINUITY_V1 — předchozí dílo téže série: nová práce na něj
    # NAVAZUJE. „Nes jedno dál, jedno vědomě změň" je záměrně asymetrické —
    # čistá variace by dala 20× tentýž obraz, čistá novota zas žádnou linku.
    if prev and prev.get("prompt"):
        user += ("PREVIOUS WORK IN THIS SERIES (%s) — you painted it and judged it:\n"
                 "  scene: %s\n  your own verdict: %s\n"
                 "This new piece CONTINUES that work: carry ONE element forward "
                 "(a motif, the light, the palette) and deliberately CHANGE one "
                 "thing so it moves on. Do not repeat the same scene.\n\n"
                 % (prev.get("title") or "", prev["prompt"], prev.get("verdict") or "—"))
        _ha._log.info('art: navazuji na předchozí dílo „%s"', prev.get("title") or "?")
    lessons = _ha._recent_lessons(db_path, 2 if _intents else 3)
    if lessons:
        user += ("Craft notes from recent pieces (secondary to the intent above):"
                 "\n- " + "\n- ".join(lessons) + "\n\n")
        _ha._log.info("art: scene prompt zohledňuje %d ponaučení", len(lessons))
    if medium:
        user += ("MEDIUM: render this as %s (or another fine-art medium if it "
                 "truly suits the subject better). END the SDXL prompt with the "
                 "chosen medium's keywords — this OVERRIDES any default medium.\n"
                 % medium)
        _ha._log.info("art: médium malby: %s", medium.split(",")[0])
    user += "Write the SDXL prompt. Reply in ENGLISH ONLY — no Chinese, Japanese or other non-English words."
    try:
        raw = ollama_generate(
            model, user, system=(system or _ha._PROMPT_SYSTEM), config=config,
            timeout=int(acfg.get("llm_timeout", 90)), keep_alive=0)
    except Exception as e:
        _ha._log.warning("art: scene prompt LLM failed: %s", e)
        return _fb()
    if not raw or not raw.strip():
        return _fb()
    p = raw.strip().strip('"').replace("\n", " ")
    # qwen2.5 občas ujede do CJK (čínština/japonština) → SDXL to nepochopí a
    # stočí styl jinam. Odstraň CJK; když po očištění zbyde málo, použij fallback.
    p2 = _ha._strip_cjk(p)
    if len(p2) < 0.6 * len(p):
        _ha._log.warning("art: scene prompt ujel do CJK (%d→%d zn) — fallback", len(p), len(p2))
        return _fb()
    # HANS_ART_CS_LEAK_V1 — zůstalo v „anglickém" promptu české slovo? Pak ho
    # model transliteroval místo přeložil a SDXL/FLUX ho nezná (kentaur→kůň).
    # Opravujeme JEN při úniku: doplníme anglickou nápovědu do závorky (systém
    # ji už umí použít, viz „If the description gives an English name in
    # parentheses") a scénu napíšeme znovu — celý námět překládat dopředu
    # NELZE, překlad zkracuje („souboj kočky se psem" → „cat fight").
    leak = _ha._cs_leak(cs_subject, p2) if cs_subject else ""
    if leak:
        en = _ha._translate_subject(config, cs_subject)
        if en and not _ha._cs_leak(cs_subject, en):
            _ha._log.info('art: český únik „%s" v promptu → nápověda „%s", píšu scénu znovu',
                      leak, en)
            hint = "%s (%s)" % (cs_subject, en)
            user2 = user.replace(cs_subject, hint, 1) if cs_subject in user else (
                user + "\nENGLISH NAME OF THE SUBJECT: %s\n" % en)
            try:
                raw2 = ollama_generate(
                    model, user2, system=(system or _ha._PROMPT_SYSTEM), config=config,
                    timeout=int(acfg.get("llm_timeout", 90)), keep_alive=0)
            except Exception as _e2:
                raw2 = ""
                _ha._log.warning("art: přepis scény po úniku selhal: %s", _e2)
            p3 = _ha._strip_cjk((raw2 or "").strip().strip('"').replace("\n", " "))
            if p3 and not _ha._cs_leak(cs_subject, p3):
                return p3[:600]
            _ha._log.warning('art: české slovo „%s" v promptu zůstalo i po přepisu', leak)
        else:
            _ha._log.warning('art: český únik „%s", ale překlad nevyšel', leak)
    return p2[:600]


def _lesson_keywords(config: dict, db_path: str, lesson_id=None) -> tuple:
    """(lekce, klíčová slova) nejnovější lekce (nebo `lesson_id`); lazy
    doplní a uloží klíčová slova, když chybí. Chyba → ('', '')."""
    if not db_path:
        return "", ""
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        if lesson_id is None:
            row = con.execute(
                "SELECT id, note, data FROM diary WHERE event_type='art_lesson' "
                "AND note IS NOT NULL AND note!='' ORDER BY ts DESC LIMIT 1").fetchone()
        else:
            row = con.execute("SELECT id, note, data FROM diary WHERE id=?",
                              (lesson_id,)).fetchone()
        if not row:
            con.close()
            return "", ""
        lid, note, data = row
        try:
            kw = (json.loads(data or "{}") or {}).get("keywords", "")
        except Exception:
            kw = ""
        if not kw:
            from scripts.ollama_client import ollama_generate
            acfg = _ha._acfg(config)
            model = str(acfg.get("verdict_model")
                        or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest"))
            raw = ollama_generate(model, note, system=_ha._KW_SYSTEM, config=config,
                                  timeout=60, options={"temperature": 0,
                                                       "num_predict": 40})
            kw = _ha._strip_cjk((raw or "").strip().strip('"').replace("\n", " "))
            kw = ", ".join(x.strip(" .") for x in kw.split(",") if x.strip(" ."))[:160]
            if kw:
                con.execute("UPDATE diary SET data=? WHERE id=?",
                            (json.dumps({"keywords": kw}, ensure_ascii=False), lid))
                con.commit()
        con.close()
        return note or "", kw or ""
    except Exception as e:
        _ha._log.warning("art: klíčová slova lekce selhala: %s", e)
        return "", ""


def _scene_prompt(config: dict, title: str, reflection: str, db_path: str = "",
                  *args, **kwargs) -> Optional[str]:
    p = _ha._scene_prompt_core(config, title, reflection, db_path, *args, **kwargs)
    if not p or not db_path:
        return p
    # HANS_ART_REPAINT_NO_KW_V1 (6. 10.) — u PŘEMALOVÁNÍ nese výtku opravený
    # námět. Klíčová slova lekce z právě vytknutého obrazu jdou na KONEC promptu
    # a námět přebila: námět „skupina žlutých postaviček…“ + lekce (výtku
    # přečetla obráceně) „realistic primate fur, diverse monkey faces“ → opice.
    if _ha._lekce_z_vytky_premalovani(db_path):
        _ha._log.info("art: HANS_ART_REPAINT_NO_KW_V1 přemalování — klíčová slova "
                  "poslední lekce vynechána (výtku nese opravený námět)")
        return p
    _l, kw = _ha._lesson_keywords(config, db_path)
    if kw:
        p = p.rstrip(" ,.;") + ", " + kw
        _ha._log.info("art: HANS_ART_LESSON_KEYWORDS_V1 → do promptu: %s", kw)
    return p

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_art as _ha  # noqa: E402
