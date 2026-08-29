"""
HANS_ART_V1 — Hans ve volné chvíli (v noci) namaluje obraz k dočtené knize.

Když Hans dočte knihu a sepíše completion reflexi, v noci z ní vytvoří jeden
obraz (SDXL přes ComfyUI) jako vizuální „ohlédnutí" za knihou. Obraz + popisek
se uloží a objeví se na dashboardu.

Reuse:
  - ComfyUI/SDXL klient + VRAM orchestrace z avatar_render (unload Ollama →
    render → _comfy_free → rewarm hans-czech).
  - Zdroj tématu: hans_library (dočtená kniha) + book_completion_reflection (deník).

Deferral-safe ([[ollama-deferred-processing]]): obraz se označí `artwork_done=1`
AŽ po úspěšném renderu. ComfyUI/Ollama dole v noci → retry příští noc.
Spouští se v nočním ticku hans_routine za večerní reflexí. 1 obraz / dočtenou knihu.
"""

import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
import uuid
from typing import Optional

from scripts.avatar_render import (
    _comfy_url, _comfy_workflow, _comfy_workflow_flux, _comfy_workflow_flux_pulid,
    _comfy_submit, _comfy_wait,
    _first_image, _comfy_fetch_image,
    _ollama_loaded, _ollama_unload, _comfy_free, _ollama_warm,
    _comfy_upload_image, _comfy_workflow_img2img, _comfy_workflow_ipadapter,
    _NEG_BASE,
)

_log = logging.getLogger("hans_art")
ART_DIR = os.path.join("data", "hans_art")

_PROMPT_SYSTEM = (
    "You turn a book and a reader's reflection into ONE concise English prompt "
    "for an SDXL image model. Output ONLY the prompt (no preamble, no quotes). "
    "Describe a single evocative SCENE or symbolic still life inspired by the "
    "book's mood and the reflection — atmosphere, setting, light, key objects. "
    "NO text, letters, words or book covers in the image. Painterly, fine-art "
    "feel. End with: oil painting, atmospheric lighting, rich detail, masterful."
)

# HANS_DREAMS_V1 — sebeřízená tvorba: Hans z vlastního popudu namaluje svůj SEN.
_DREAM_SCENE_SYSTEM = (
    "You turn a person's short surreal DREAM into ONE concise English prompt for "
    "an SDXL image model. Output ONLY the prompt (no preamble, no quotes). Depict "
    "the dream as a single dreamlike, symbolic, ATMOSPHERIC scene — surreal, "
    "evocative, painterly. Keep what the dream literally mentions, render it "
    "dreamlike. NO text, letters, words or book covers. End with: oil painting, "
    "dreamlike surreal atmosphere, soft hazy light, rich detail, masterful."
)

# HANS_DAY_PAINTING_V1 — Hans namaluje obraz vystihující svůj DEN a NÁLADU.
_DAY_SCENE_SYSTEM = (
    "You turn a person's DAY and their MOOD into ONE concise English SDXL prompt for "
    "a SYMBOLIC, ATMOSPHERIC still life or quiet scene. The MOOD MUST DOMINATE the "
    "image — let it drive the LIGHTING, COLOR PALETTE and overall feeling (a worried "
    "day = cold muted colors, heavy shadows, restless dim light; a content day = warm "
    "golden serene light). The day's moments are only secondary symbolic motifs "
    "(objects, setting), NOT the focus, NO people. Make the emotional tone "
    "unmistakable, even if somber. NO text, letters, words or book covers. End with: "
    "oil painting, expressive mood, rich detail, masterful."
)

# HANS_DAY_MOOD_VISUAL_V1 — nálada → konkrétní vizuální atmosféra (ať „kousne" do SDXL,
# jinak SDXL stočí vše do hezkého klidu). Klíče = hans_mood.MOODS.
_MOOD_VISUAL = {
    "content":     "warm serene atmosphere, soft golden light, harmonious gentle colors, quiet contentment",
    "curious":     "bright inviting light, intriguing details, fresh vivid colors, a sense of wonder",
    "lonely":      "empty quiet space, cool muted tones, long soft shadows, a single faint light, deep solitude",
    "melancholic": "muted desaturated palette, grey-blue tones, fading wistful light, a pensive somber mood",
    "engaged":     "lively warm light, rich saturated colors, dynamic focused composition, vitality",
    "worried":     "tense uneasy atmosphere, heavy dark shadows, cold muted colors, restless dim light, disquiet",
}


def _acfg(config: dict) -> dict:
    return config.get("hans_art", {}) or {}


def _ckpt(config: dict) -> str:
    # vlastní model nebo sdílený s avatarem (SDXL checkpoint)
    return (_acfg(config).get("image_model")
            or (config.get("hans_avatar", {}) or {}).get("image_model", ""))


# ── DB ────────────────────────────────────────────────────────────────────────
def _ensure_schema(db_path: str) -> None:
    """Idempotentní sloupec hans_library.artwork_done. BEZ backfillu —
    existující dočtené knihy zůstanou eligible (dostanou obraz)."""
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        cols = [r[1] for r in db.execute("PRAGMA table_info(hans_library)")]
        if "artwork_done" not in cols:
            db.execute("ALTER TABLE hans_library ADD COLUMN artwork_done INTEGER DEFAULT 0")
            db.commit()
        db.close()
    except Exception as e:
        _log.debug("art: ensure_schema failed: %s", e)


def _pending_book(db_path: str) -> Optional[dict]:
    """Nejstarší dočtená kniha, která má completion reflexi a ještě nemá obraz."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT book_id, book_title FROM hans_library "
            "WHERE status='finished' AND COALESCE(completion_reflected,0)=1 "
            "AND COALESCE(artwork_done,0)=0 ORDER BY finished_at LIMIT 1"
        ).fetchone()
        con.close()
        if row:
            return {"book_id": row[0], "title": row[1] or "kniha"}
    except Exception as e:
        _log.debug("art: pending_book failed: %s", e)
    return None


def _source_text(db_path: str, title: str) -> str:
    """Completion reflexe (note→data) k titulu; fallback spojené per-kapitola reflexe."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT COALESCE(NULLIF(note,''), data) FROM diary "
            "WHERE event_type='book_completion_reflection' AND title LIKE ? "
            "AND COALESCE(NULLIF(note,''), data) IS NOT NULL "
            "ORDER BY ts DESC LIMIT 1", (title + "%",)).fetchone()
        if not row:
            rows = con.execute(
                "SELECT data FROM diary WHERE event_type='book_reflection' "
                "AND title LIKE ? AND data IS NOT NULL AND data!='' "
                "ORDER BY ts DESC LIMIT 5", (title + "%",)).fetchall()
            con.close()
            return "\n".join(r[0].strip() for r in rows if r and r[0])[:1500]
        con.close()
        return (row[0] or "").strip()[:1500]
    except Exception as e:
        _log.debug("art: source_text failed: %s", e)
        return ""


def _mark_done(db_path: str, book_id: str) -> None:
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        db.execute("UPDATE hans_library SET artwork_done=1 WHERE book_id=?", (book_id,))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: mark_done failed: %s", e)


def origin_line(title: str, data) -> str:
    """HANS_ART_ORIGIN_V1 — z čeho Hans u obrazu vycházel (sen/film/kniha/námět).

    Vrací JEDNU českou větu do popisku obrazu. Nikdy nevyhazuje výjimku;
    když zdroj není znám, vrátí prázdný řetězec (radši nic než domyšlené).
    """
    import json as _json
    d = {}
    try:
        d = _json.loads(data) if isinstance(data, str) else (data or {})
    except Exception:
        d = {}
    if not isinstance(d, dict):
        d = {}
    src = (d.get("source") or "").strip()
    t = (title or "").strip()

    def _short(s, n=180):
        s = " ".join(str(s or "").split())
        return s if len(s) <= n else s[:n].rstrip() + "…"

    if src == "dream":
        dream = _short(d.get("dream"))
        return f"Vycházel jsem ze svého snu: „{dream}“" if dream else "Vycházel jsem ze svého snu."
    if src == "day":
        mood = (d.get("mood") or "").strip()
        return (f"Vycházel jsem z dnešního dne — nálada: {mood}." if mood
                else "Vycházel jsem z dnešního dne.")
    if src == "home":
        return "Maloval jsem svůj domov — pohled z místa, kde stojím."
    if src == "self":
        return "Autoportrét — maloval jsem sebe podle svého avatara."
    if src == "person":
        return f"Portrét: {t}." if t else "Portrét."
    if src == "subject":
        return f"Námět, který jste mi zadal: {t}." if t else ""
    if src in ("book", "") and t:
        return f"Vycházel jsem z četby: {t}."
    return f"Námět: {t}." if t else ""


def _log_artwork(db_path: str, title: str, caption: str, rel_path: str, prompt: str) -> None:
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log_artwork failed: %s", e)


# ── Prompt + caption ────────────────────────────────────────────────────────
_CJK_RE = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿"
    r"豈-﫿＀-￯]+")


def _strip_cjk(text: str) -> str:
    """Odstraň CJK znaky (qwen2.5 občas ujede do čínštiny/japonštiny) a sjednoť
    mezery/interpunkci, ať zbyde čistý anglický SDXL prompt."""
    t = _CJK_RE.sub(" ", text or "")
    t = re.sub(r"\s*[-–—,]\s*(?=[,\.])", " ", t)   # osamělé spojky po stripu
    t = re.sub(r"\s+", " ", t).strip(" ,-–—")
    return t


# HANS_ART_MEDIUM_VARIETY_V2 — rotující výtvarné médium/paleta proti jednotnému
# „oil painting" nádechu (obrazy vypadaly jeden jak druhý). Nudge pro LLM +
# fallback ocas. NEplatí pro home/mockup/portrét/explicitní styl (mají svůj look).
_MEDIA = [
    "oil painting, rich impasto texture, painterly brushwork",
    "watercolor, soft luminous washes, delicate, airy",
    "gouache, matte opaque color, bold flat shapes",
    "ink illustration, fine cross-hatching, expressive linework",
    "impressionist, loose visible brushstrokes, broken vibrant color",
    "expressionist, bold emotional strokes, intense palette",
    "charcoal drawing, soft tonal shading, textured paper, monochrome",
    "soft pastel, chalky texture, muted harmonious palette",
    "digital painting, crisp clean rendering, vivid color",
    "acrylic, flat vivid color, confident strokes",
    "tempera, fine layered detail, luminous color",
    "art nouveau linework, decorative organic curves, elegant palette",
]
_last_medium = {"v": ""}


def _pick_medium() -> str:
    import random
    pool = [m for m in _MEDIA if m != _last_medium["v"]] or _MEDIA
    m = random.choice(pool)
    _last_medium["v"] = m
    return m


_CZ_COMMON = {
    "domov", "dum", "byt", "pokoj", "kuchyne", "loznice", "obyvak", "okno",
    "dvere", "zahrada", "dvur", "ulice", "mesto", "vesnice", "namesti", "hrad",
    "zamek", "kostel", "kaple", "most", "reka", "potok", "jezero", "rybnik",
    "les", "louka", "pole", "hora", "kopec", "strom", "kvetina", "kytka", "pes",
    "kocka", "kun", "ptak", "auto", "vlak", "lod", "mesic", "slunce", "hvezda",
    "obloha", "mrak", "voda", "ohen", "snih", "dest", "cesta", "park", "kavarna",
    "hospoda", "stul", "zidle", "postel", "obraz", "socha", "pohled", "misto",
    "muj", "moje", "svuj", "nas", "noc", "den", "rano", "vecer", "svetlo",
    "stin", "krajina", "scena", "zima", "jaro", "leto", "podzim", "kraj",
    "sen", "muz", "zena", "dite", "clovek", "tvar", "kvet", "vez",
}


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
    toks = re.findall(r"[a-z]+", _ascii_fold(s).lower())
    return not any(t in _CZ_COMMON for t in toks)


_CS_WORD_RE = re.compile(r"[a-záčďéěíňóřšťúůýž]{6,}")


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
    for w in _CS_WORD_RE.findall((subject_cs or "").lower()):
        stem = w[:-2]
        if len(stem) >= 5 and stem in low:
            return w
    return ""


def _translate_subject(config: dict, subject_cs: str) -> str:
    """Český námět → anglicky, vyhrazeným krátkým dotazem (ne uvnitř psaní
    scény). Změřeno 5/5 správně vč. „vodníka" → water sprite a zachovaného
    „Karlštejn Castle". POUŽÍVÁ SE JEN JAKO NÁPOVĚDA při úniku — překládat
    rovnou celý námět je horší: „souboj kočky se psem" → „cat fight" (ztratí
    psa), protože překlad zkracuje."""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return ""
    out = ollama_generate(
        str(_acfg(config).get("prompt_model", "qwen2.5:7b")),
        "Czech: %s\nEnglish:" % subject_cs,
        system=("Translate the Czech noun phrase into ENGLISH. Output ONLY the "
                "English words, 1-6 words, nothing else. Never transliterate — "
                "if it is a creature or thing, use its real English name."),
        config=config, timeout=60, keep_alive=0,
        options={"temperature": 0.0, "num_predict": 24})
    return (out or "").strip().strip('."\'').splitlines()[0][:60] if out else ""


def _scene_prompt(config: dict, title: str, reflection: str, db_path: str = "",
                  system: str = None, source_intro: str = None,
                  en_fallback: str = None, prev: dict = None,
                  cs_subject: str = "") -> Optional[str]:
    """LLM (levný, keep_alive=0) → anglický SDXL scene prompt. Fallback šablona.
    HANS_ART_LESSON_V1: když db_path, vloží do promptu ponaučení z minulých obrazů.
    HANS_DREAMS_V1: system+source_intro lze přepsat (snová varianta místo knižní)."""
    # HANS_ART_MEDIUM_VARIETY_V2 — vyber médium pro tuhle malbu (ne u fixních looků)
    _novary = {globals().get(n) for n in (
        "_HOME_SCENE_SYSTEM", "_STYLE_SCENE_SYSTEM", "_MOCKUP_SCENE_SYSTEM")}
    medium = _pick_medium() if (system not in _novary) else ""
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
        _en_fb = _en_name(_fsubj,
                          lang=(config.get("curiosity", {}) or {}).get("wiki_lang", "cs"))
        if _en_fb:
            _fsubj = _en_fb
    except Exception:
        pass
    fallback = ", ".join(x for x in (_fsubj, _fstyle, _tail) if x)
    # HANS_ART_SAFE_FALLBACK_V1 — fallback (LLM dole) smí do FLUXu jen s
    # anglickým námětem; český → en_fallback od volajícího, jinak None = odlož.
    def _fb():
        # HANS_ART_CS_LEAK_V1 (5.8.) — `_looks_english` je DĚRAVÝ: pozná jen
        # diakritiku a seznam běžných českých slov, takže „zenskeho kentaura"
        # (uživatel psal bez háčků, ani jedno slovo v seznamu není) projde jako
        # angličtina a syrová čeština doteče do FLUXu. Doloženo 5.8.: LLM na
        # scénu vypršel, fallback poslal do modelu „zenskeho kentaura, gouache".
        # Rozšiřovat seznam slov je nekonečná práce → radši se ZEPTEJ na
        # překlad (krátký dotaz projde i tam, kde dlouhý na scénu vypršel).
        if cs_subject:
            _en = _translate_subject(config, cs_subject)
            if _en and _looks_english(_en) and not _cs_leak(cs_subject, _en):
                _log.info('art: fallback — český námět „%s" přeložen na „%s"',
                          cs_subject, _en)
                return ", ".join(x for x in (_en, _fstyle, _tail) if x)
        # Překlad nevyšel (mozek dole / zahlcený): NESMÍME spadnout zpátky na
        # `_looks_english`, ta český námět bez diakritiky propustí. Když v
        # námětu čeština prokazatelně zůstala, radši ODLOŽ render — tohle je
        # celý smysl HANS_ART_SAFE_FALLBACK_V1 (radši žádný obraz než garbage).
        if cs_subject and _cs_leak(cs_subject, fallback):
            if en_fallback:
                return ", ".join(x for x in (en_fallback, _tail) if x)
            _log.warning('art: námět „%s" se nepodařilo dostat do angličtiny '
                         '— render odložen (retry příště)', cs_subject)
            return None
        if _looks_english(_fsubj):
            return fallback
        if en_fallback:
            return ", ".join(x for x in (en_fallback, _tail) if x)
        return None
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return _fb()
    acfg = _acfg(config)
    model = str(acfg.get("prompt_model", "qwen2.5:7b"))
    user = (source_intro if source_intro is not None
            else f"Book: {title}\n\nReader's reflection (Czech):\n{reflection}\n\n")
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
        _log.info("art: scene prompt nese %d trvalých záměrů", len(_intents))
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
        _log.info('art: navazuji na předchozí dílo „%s"', prev.get("title") or "?")
    lessons = _recent_lessons(db_path, 2 if _intents else 3)
    if lessons:
        user += ("Craft notes from recent pieces (secondary to the intent above):"
                 "\n- " + "\n- ".join(lessons) + "\n\n")
        _log.info("art: scene prompt zohledňuje %d ponaučení", len(lessons))
    if medium:
        user += ("MEDIUM: render this as %s (or another fine-art medium if it "
                 "truly suits the subject better). END the SDXL prompt with the "
                 "chosen medium's keywords — this OVERRIDES any default medium.\n"
                 % medium)
        _log.info("art: médium malby: %s", medium.split(",")[0])
    user += "Write the SDXL prompt. Reply in ENGLISH ONLY — no Chinese, Japanese or other non-English words."
    try:
        raw = ollama_generate(
            model, user, system=(system or _PROMPT_SYSTEM), config=config,
            timeout=int(acfg.get("llm_timeout", 90)), keep_alive=0)
    except Exception as e:
        _log.warning("art: scene prompt LLM failed: %s", e)
        return _fb()
    if not raw or not raw.strip():
        return _fb()
    p = raw.strip().strip('"').replace("\n", " ")
    # qwen2.5 občas ujede do CJK (čínština/japonština) → SDXL to nepochopí a
    # stočí styl jinam. Odstraň CJK; když po očištění zbyde málo, použij fallback.
    p2 = _strip_cjk(p)
    if len(p2) < 0.6 * len(p):
        _log.warning("art: scene prompt ujel do CJK (%d→%d zn) — fallback", len(p), len(p2))
        return _fb()
    # HANS_ART_CS_LEAK_V1 — zůstalo v „anglickém" promptu české slovo? Pak ho
    # model transliteroval místo přeložil a SDXL/FLUX ho nezná (kentaur→kůň).
    # Opravujeme JEN při úniku: doplníme anglickou nápovědu do závorky (systém
    # ji už umí použít, viz „If the description gives an English name in
    # parentheses") a scénu napíšeme znovu — celý námět překládat dopředu
    # NELZE, překlad zkracuje („souboj kočky se psem" → „cat fight").
    leak = _cs_leak(cs_subject, p2) if cs_subject else ""
    if leak:
        en = _translate_subject(config, cs_subject)
        if en and not _cs_leak(cs_subject, en):
            _log.info('art: český únik „%s" v promptu → nápověda „%s", píšu scénu znovu',
                      leak, en)
            hint = "%s (%s)" % (cs_subject, en)
            user2 = user.replace(cs_subject, hint, 1) if cs_subject in user else (
                user + "\nENGLISH NAME OF THE SUBJECT: %s\n" % en)
            try:
                raw2 = ollama_generate(
                    model, user2, system=(system or _PROMPT_SYSTEM), config=config,
                    timeout=int(acfg.get("llm_timeout", 90)), keep_alive=0)
            except Exception as _e2:
                raw2 = ""
                _log.warning("art: přepis scény po úniku selhal: %s", _e2)
            p3 = _strip_cjk((raw2 or "").strip().strip('"').replace("\n", " "))
            if p3 and not _cs_leak(cs_subject, p3):
                return p3[:600]
            _log.warning('art: české slovo „%s" v promptu zůstalo i po přepisu', leak)
        else:
            _log.warning('art: český únik „%s", ale překlad nevyšel', leak)
    return p2[:600]


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


# ── HANS_ART_VERDICT_V1 — hodnocení obrazu (vize + persona + vyvíjející se vkus) ──
_VISION_PROMPT = (
    "You are looking at a finished painting. In 2-3 sentences describe ONLY what is "
    "ACTUALLY visible (subject, setting, dominant colors, mood) and give a BALANCED, "
    "honest assessment of the craft: say what works, AND point out a genuine weakness "
    "if one is actually visible (e.g. a muddy area, weak composition, flat lighting). "
    "CRITICAL: describe only what is truly there — if the image has NO people or "
    "figures, do NOT mention any figures, faces, hands or posture at all, and do NOT "
    "treat the absence of people as a weakness or suggest adding them (architecture, "
    "landscapes and still lifes are meant to be unpopulated). Do not invent or "
    "exaggerate flaws, but do not gloss over a real one either. Be accurate — neither "
    "flattering nor fault-hunting."
)


def _describe_render(config: dict, dest_path: str) -> str:
    """B (vize): llava popíše SKUTEČNÝ vyrenderovaný obraz. keep_alive=0 (VRAM
    on-demand). Běží PO _comfy_free, PŘED warmem hans-czech. '' při selhání."""
    try:
        import base64
        with open(dest_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        _log.debug("art: read render for vision failed: %s", e)
        return ""
    acfg = _acfg(config)
    model = str(acfg.get("vision_model")
                or (config.get("room_observer", {}) or {}).get("model")
                or "qwen2.5vl:7b")
    try:
        from scripts.ollama_client import ollama_generate
        desc = ollama_generate(
            model, _VISION_PROMPT, images=[b64], config=config,
            timeout=int(acfg.get("vision_timeout", 90)), keep_alive=0)
    except Exception as e:
        _log.warning("art: vision describe failed: %s", e)
        return ""
    desc = (desc or "").strip()
    if desc:
        _log.info("art: vize obrazu: %.120s", desc)
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
        _log.debug("art: past_verdicts failed: %s", e)
        return []


def _evaluate_artwork(config: dict, db_path: str, title: str,
                      reflection: str, vision_desc: str,
                      source_label: str = "knihou") -> str:
    """Hansovo hodnocení (hans-czech persona): co namaloval + jestli se mu obraz
    povedl/líbí — reaguje na SKUTEČNOU kvalitu (llava popis) a svůj vyvíjející se
    vkus (minulé verdikty). Vrací český text = caption. Fallback _caption.
    HANS_DREAMS_V1: source_label = čím se obraz inspiroval (knihou / svým snem)."""
    fallback = _caption(reflection, title)
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
    acfg = _acfg(config)
    model = str(acfg.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest"))
    # HANS_ART_PROGRESS_V1 — uzavřít smyčku verdikt→ponaučení→verdikt:
    # předchozí ponaučení (= záměr, který vedl TENHLE obraz) předáme do verdiktu,
    # ať Hans posoudí, jestli se ho povedlo naplnit (narativ pokroku). Minulé
    # verdikty NEechujeme syrově (to plodilo šablonu "světlo+barvy / kompozice") —
    # předáme je jen jako anti-repetiční pokyn "všimni si něčeho jiného".
    prior_lessons = _recent_lessons(db_path, 1)
    prior_lesson = prior_lessons[0] if prior_lessons else ""
    past = _past_verdicts(db_path, limit=2)
    progress_block = ""
    if prior_lesson:
        progress_block = (
            "Před tímto obrazem sis předsevzal zlepšit toto:\n„%s\"\n"
            "V první větě upřímně posuď, jestli se to tentokrát povedlo (klidně "
            "i jen částečně) — tím uvidíš svůj vlastní vývoj.\n\n" % prior_lesson)
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
        _log.warning("art: verdict LLM failed: %s", e)
        return fallback
    out = (out or "").strip().strip('"')
    if out:
        _log.info("art: Hansův verdikt: %.120s", out)
        # ořez na CELOU větu (ne uprostřed) — hard cap až kdyby to ujelo
        cap = int(acfg.get("verdict_max_chars", 900))
        if len(out) > cap:
            cut = max(out.rfind(". ", 0, cap), out.rfind("! ", 0, cap),
                      out.rfind("? ", 0, cap))
            out = out[:cut + 1] if cut > cap // 2 else out[:cap]
        return out
    return fallback


# ── HANS_ART_LESSON_V1 — smyčka verdikt → ponaučení → příští render ──────────
# (a) Z vize+verdiktu odvodí krátké anglické PONAUČENÍ ('art_lesson' v deníku);
# _scene_prompt ho příště vloží qwen do promptu + _lesson_negatives dolní negativ.
_LESSON_SYSTEM = (
    "You are an art director. You receive an INDEPENDENT description of a "
    "rendered image, the painter's own verdict, and the painter's RECENT "
    "guidance lines. Output ONE short line of reusable guidance IN ENGLISH for "
    "the painter's NEXT image. Crucially: do NOT repeat earlier guidance — if a "
    "previous aim was clearly met, acknowledge it briefly and move ON to a FRESH "
    "aspect to grow (vary across composition, light, colour, texture, mood, "
    "perspective, narrative). If the piece worked well (it usually does), "
    "REINFORCE what to keep doing. Suggest AVOIDING something ONLY when the "
    "verdict named a genuine, clear problem; never assume anatomy is flawed by "
    "default. Max 22 words. Output ONLY the guidance line — no preamble, no quotes."
)


def _derive_art_lesson(config: dict, db_path: str, title: str,
                       vision_desc: str, verdict: str, store: bool = True) -> str:
    """Odvodí ponaučení pro příští render z vize + verdiktu. Běží na hans-czech
    (warm, žádný extra model do VRAM). Uloží do deníku 'art_lesson' (když store).
    Vrací ponaučení nebo ''. Nikdy nehází."""
    if not vision_desc:
        return ""
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return ""
    acfg = _acfg(config)
    model = str(acfg.get("verdict_model")
                or (config.get("models", {}) or {}).get("dialog", "hans-czech:latest"))
    recent = _recent_lessons(db_path, 3)
    recent_block = ""
    if recent:
        recent_block = ("Painter's recent guidance lines (do NOT repeat these — "
                        "build on them or move to a new aspect):\n"
                        + "\n".join("- %s" % r for r in recent) + "\n\n")
    user = (recent_block
            + "Independent description of the rendered image:\n%s\n\n"
            "Painter's verdict:\n%s\n\nWrite the ONE-line guidance."
            % (vision_desc, verdict))
    try:
        raw = ollama_generate(model, user, system=_LESSON_SYSTEM, config=config,
                              timeout=int(acfg.get("lesson_timeout", 90)))
    except Exception as e:
        _log.warning("art: lesson LLM failed: %s", e)
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
            _log.info("art: ponaučení uloženo: %.120s", lesson)
        except Exception as e:
            _log.warning("art: ulož lesson failed: %s", e)
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


_NEG_HANDS = "deformed hands, extra fingers, fused fingers, mutated hands"


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
    parts = [_NEG_BASE, _NEG_HANDS]
    try:
        extra = _lesson_negatives(_recent_lessons(db_path))
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


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (title or "kniha").lower()).strip("_")
    return s[:40] or "kniha"


# ── Render core (sdílené noční i ruční cestou) ──────────────────────────────
def _comfy_ready(config: dict) -> bool:
    """HANS_COMFY_WEDGE_V1 (4.8.) — brána PŘED renderem: odpovídá ComfyUI?

    Doloženo 4.8.: ComfyUI zatuhl (TCP port přijímal spojení, HTTP mlčelo) →
    render čekal celý `render_timeout` (900 s) a teprve pak spadl na horší
    fallback. Zvyšování timeoutu tenhle případ neřeší — server nebyl pomalý,
    ale zaseklý. Rychlá sonda to pozná za sekundy; health vrstva mezitím
    ComfyUI restartuje (`heal_comfyui`), takže příští pokus projde."""
    try:
        from scripts.hans_health import comfy_alive
        if comfy_alive(config, timeout=float(
                _acfg(config).get("comfy_probe_timeout", 8))):
            return True
    except Exception:
        return True          # sonda nedostupná → nezdržuj, zkus render
    _log.warning("art: ComfyUI neodpovídá — render odložen "
                 "(nečekám %ss naprázdno; health ho zkusí restartovat)",
                 _acfg(config).get("render_timeout", 900))
    return False


def _last_in_series(db_path: str, series: str) -> Optional[dict]:
    """HANS_ART_CONTINUITY_V1 (5.8.) — poslední Hansovo dílo TÉŽE série
    (day/dream/home/book) i s jeho vlastním verdiktem. Slouží k tomu, aby další
    obraz na předchozí NAVAZOVAL, ne aby začínal od nuly.
    Záměrně jen v rámci série: řetězit „pes" do „mého dne" by dalo nesmysl."""
    if not db_path or not series:
        return None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT title, note, data FROM diary WHERE event_type='artwork' "
            "AND data LIKE ? ORDER BY ts DESC LIMIT 1",
            ('%"source": "' + series + '"%',)).fetchone()
        con.close()
    except Exception:
        return None
    if not row:
        return None
    title, note, data = row
    try:
        prompt = (json.loads(data or "{}") or {}).get("prompt") or ""
    except Exception:
        prompt = ""
    if not prompt:
        return None
    return {"title": title or "", "verdict": (note or "")[:300], "prompt": prompt[:400]}


def _render_image(config: dict, title: str, reflection: str, db_path: str = "",
                  en_fallback: str = None,
                  scene_system: str = None, scene_intro: str = None,
                  series: str = "", cs_subject: str = ""):
    """Vyrenderuje 1 obraz přes ComfyUI/SDXL. Vrací (rel_path, prompt, vision_desc)
    nebo None. VRAM orchestrace uvnitř (unload LLM → render → _comfy_free →
    llava vize → warm hans-czech). vision_desc = llava popis renderu pro hodnocení
    (HANS_ART_VERDICT_V1), '' když vize selže. db_path → ponaučení z minulých
    obrazů (HANS_ART_LESSON_V1) ovlivní scénu i negativní prompt. Nikdy nehází."""
    # OLLAMA_GAME_MODE_V1 — herní mód: ComfyUI render (SDXL ~7 GB do VRAM) je
    # přímé HTTP mimo Ollama gate → gatuj ho tady, ať se za hry VRAM nezabere.
    # None = deferral-safe (volající to bere jako „render odložen", retry později).
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _log.info("art: herní mód — render odložen (VRAM volná pro hru)")
            return None
    except Exception:
        pass
    # HANS_ART_FLUX_V1 — obecné malování přes FLUX.1-dev (celé postavy/zvířata),
    # když `art.use_flux`. paint_self (autoportrét) zůstává na SDXL+IP-Adapteru
    # (podobu drží IP-Adapter, ten je SDXL-only). Default False = beze změny SDXL.
    _use_flux = bool(_acfg(config).get("use_flux", False))
    ckpt = (_acfg(config).get("flux_ckpt", "flux1-dev-fp8.safetensors")
            if _use_flux else _ckpt(config))
    if not ckpt:
        _log.warning("art: image_model nenastaven (hans_art/hans_avatar) — skip")
        return None
    base = _comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception as e:
        _log.warning("art: ComfyUI nedostupný (%s) — render odložen", e)
        return None

    prompt = _scene_prompt(config, title, reflection, db_path,
                           system=scene_system, source_intro=scene_intro,
                           en_fallback=en_fallback,
                           prev=_last_in_series(db_path, series) if series else None,
                           cs_subject=cs_subject)
    # HANS_ART_SAFE_FALLBACK_V1 — _scene_prompt vrátí None, když LLM selhal a
    # není bezpečný anglický námět → ODLOŽ render (radši žádný obraz než garbage
    # z nepřeloženého českého námětu, doloženo 30.7. „domov" → muž na ulici).
    if not prompt:
        _log.warning("art: scene prompt nešel bezpečně sestavit (LLM dole, "
                     "český námět) — render odložen, retry příště")
        return None
    acfg = _acfg(config)
    w = int(acfg.get("width", 1024)); h = int(acfg.get("height", 768))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2**31)   # náhodný seed = každý obraz jiný
    client_id = uuid.uuid4().hex

    os.makedirs(ART_DIR, exist_ok=True)
    fname = f"{int(time.time())}_{_slug(title)}.png"
    dest = os.path.join(ART_DIR, fname)

    if not _comfy_ready(config):
        return None
    loaded = _ollama_loaded(config)
    _ollama_unload(config, loaded)
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        if _use_flux:
            wf = _comfy_workflow_flux(ckpt, prompt, seed, w, h,
                                      int(acfg.get("flux_steps", 20)),
                                      float(acfg.get("flux_guidance", 3.5)))
        else:
            # HANS_NEG_SPLIT_V1 — `_NEG_BASE` bez avatarového anti-driftu:
            # tudy jde VŠECHNA obecná malba (den, sen, kniha, námět na
            # požádání), takže „moustache/beard/glasses" v negativu srážel
            # každou vousatou postavu (doloženo Gandalf 25.7.). Anti-drift
            # zůstává jen na Hansově vlastní tváři (paint_self/avatar_render).
            wf = _comfy_workflow(ckpt, prompt, seed, w, h, steps, cfg_s,
                                 negative=_NEG_BASE)
        # HANS_ART_LESSON_V1 — dolň negativní prompt podle ponaučení z minulých obrazů
        extra_neg = _lesson_negatives(_recent_lessons(db_path))
        if extra_neg and isinstance(wf.get("7"), dict):
            wf["7"]["inputs"]["text"] = wf["7"]["inputs"]["text"] + ", " + extra_neg
            _log.info("art: negativ dolněn ponaučením: %s", extra_neg)
        _log.info("art: render start (%s, %dx%d, %d steps, timeout %ds) — prompt: %.120s",
                  ckpt, w, h,
                  int(acfg.get("flux_steps", 20)) if _use_flux else steps,
                  rtimeout, prompt)
        pid = _comfy_submit(base, wf, client_id)
        if not pid:
            _log.warning("art: ComfyUI submit selhal (pid None)")
        else:
            hist = _comfy_wait(base, pid, timeout=rtimeout)
            img = _first_image(hist) if hist else None
            if not hist:
                _log.warning("art: render vypršel (timeout %ds) — ComfyUI nejspíš "
                             "studený checkpoint (RX6800/ROCm). Zvyš render_timeout.", rtimeout)
            elif not img:
                _log.warning("art: render doběhl, ale v history není obrázek")
            elif _comfy_fetch_image(base, img, dest):
                ok = True
            else:
                _log.warning("art: fetch obrázku z ComfyUI selhal")
    except Exception as e:
        _log.warning("art: render selhal: %s", e)
    finally:
        _comfy_free(config)
        # HANS_ART_VERDICT_V1 — vize PO uvolnění ComfyUI, PŘED warmem hans-czech
        # (VRAM volná pro llava; keep_alive=0 ji po popisu zase pustí).
        if ok:
            vision_desc = _describe_render(config, dest)
        _ollama_warm(config, config.get("models", {}).get("dialog", "hans-czech:latest"))

    if ok:
        return os.path.join("data", "hans_art", fname), prompt, vision_desc
    return None


def comfy_available(config: dict) -> bool:
    """Rychlá kontrola, jestli ComfyUI na PC běží (pro /art feedback)."""
    try:
        urllib.request.urlopen(f"{_comfy_url(config)}/system_stats", timeout=8).read()
        return True
    except Exception:
        return False


def _current_book_title(db_path: str) -> str:
    """Aktuálně čtená (přednost) nebo poslední dočtená kniha."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT book_title FROM hans_library WHERE status IN ('reading','finished') "
            "ORDER BY (status='reading') DESC, started_at DESC LIMIT 1").fetchone()
        con.close()
        return (row[0] if row else "") or "kniha"
    except Exception:
        return "kniha"


def book_is_read(db_path: str, title: str) -> bool:
    """HANS_ART_UNREAD_WISHLIST_V1: zná Hans tuhle knihu? (čte/dočetl ji, nebo k ní
    má reflexi). Když ne, /art ji nemá malovat naslepo — přidá ji na seznam čtení."""
    t = (title or "").strip()
    if not t:
        return True  # prázdno = aktuální kniha (vždy známá)
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT 1 FROM hans_library WHERE book_title LIKE ? "
            "AND status IN ('reading','finished') LIMIT 1", (t,)).fetchone()
        if not row:  # fallback: má k ní vůbec nějakou reflexi?
            row = con.execute(
                "SELECT 1 FROM diary WHERE event_type IN "
                "('book_completion_reflection','book_reflection','book_read') "
                "AND title LIKE ? LIMIT 1", (t + "%",)).fetchone()
        con.close()
        return bool(row)
    except Exception as e:
        _log.debug("art: book_is_read failed: %s", e)
        return True  # fail-open: radši namaluj než zablokuj


def add_to_wishlist(db_path: str, title: str, url: str = "",
                    author: str = "", lang: str = "",
                    book_id: str = "") -> str:
    """Přidá nečtenou knihu na seznam k přečtení (hans_library status='wishlist',
    nízká priorita). Idempotentní (LIKE na titul). Vrací 'added'|'exists'|'error'.

    HANS_BOOK_MENTIONS_V1: volitelné url/author/lang (z Gutendexu) — když jsou,
    reader umí knihu z wishlistu stáhnout a přečíst. book_id přebíjí default slug
    (pro Gutenberg id, ať souhlasí s katalogem)."""
    t = (title or "").strip()
    if not t:
        return "error"
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        ex = db.execute("SELECT status FROM hans_library WHERE book_title LIKE ? LIMIT 1",
                        (t,)).fetchone()
        if ex:
            db.close()
            return "exists"
        bid = (book_id or "").strip() or ("wish_" + _slug(t))
        db.execute(
            "INSERT INTO hans_library (book_id, book_title, author, total_chapters, "
            "current_chapter, started_at, status, url, source_lang) "
            "VALUES (?,?,?,0,0,?,'wishlist',?,?)",
            (bid, t, author, time.time(), url, lang))
        db.commit()
        db.close()
        _log.info('art: kniha „%s" přidána na seznam k přečtení (wishlist%s)',
                  t, ", s URL" if url else "")
        return "added"
    except Exception as e:
        _log.warning("art: add_to_wishlist failed: %s", e)
        return "error"


def render_now(config: dict, diary_db_path: str, title: str = "") -> Optional[tuple]:
    """Na počkání (/art) — vyrenderuje obraz pro zadanou nebo aktuálně čtenou
    knihu, zaloguje do galerie (deník 'artwork'), ale NEznačí artwork_done
    (= ruční vzorek, noční logika dočtených knih běží dál). Vrací (rel_path, caption)."""
    title = (title or "").strip() or _current_book_title(diary_db_path)
    reflection = _source_text(diary_db_path, title)
    res = _render_image(config, title, reflection, diary_db_path)
    if not res:
        return None
    rel_path, prompt, vision_desc = res
    caption = _evaluate_artwork(config, diary_db_path, title, reflection, vision_desc)
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    _log_artwork(diary_db_path, title, caption, rel_path, prompt)
    _log.info('art: ruční obraz pro „%s" → %s', title, rel_path)
    return rel_path, caption


# ── HANS_CAPABILITY_AWARENESS_V1 — malování na LIBOVOLNÉ téma (na požádání) ──
_SUBJECT_SCENE_SYSTEM = (
    "You turn a short Czech description of a SUBJECT or theme into ONE concise "
    "English SDXL image prompt — an evocative, artistic INTERPRETATION (an "
    "impression), not a literal diagram or text. Output ONLY the prompt (no "
    "preamble, no quotes). Choose fitting composition, colors and mood for the "
    "subject. If the description gives an English/franchise name in parentheses, "
    "USE THAT English name in the prompt — SDXL does not understand Czech names. "
    "If the subject is a FICTIONAL CHARACTER or franchise, depict the CHARACTER(S) "
    "themselves (their look), not a movie poster or cinema scene. "
    "Reply in ENGLISH ONLY (no Chinese/Japanese). NO text, letters or "
    "words in the image, NO watermark. End with: digital painting, atmospheric, "
    "detailed, artistic, high quality."
)


_HONORIFIC = re.compile(
    r"^\s*(pan[íaou]?|pán[aeu]?|slečn[aou]|sir|mr|mrs|ms|dr)\s+", re.IGNORECASE)


def _en_name(name: str, lang: str = "cs") -> str:
    """HANS_ART_EN_TITLE_V1 — ANGLICKÝ/franšízový název přes Wiki langlink
    („Mimoni"→„Minions", „Cardiffský hrad"→„Cardiff Castle"). SDXL nerozumí
    českým jménům → u fikce i reálií pak namaluje ROZPOZNATELNOU věc, ne
    generickou scénu. '' když langlink není nebo je stejný."""
    n = (name or "").strip()
    if not n or lang == "en":
        return ""
    try:
        from scripts.hans_study import _en_title
        en = (_en_title(n, lang=lang) or "").strip()
        # rozcestník = nejednoznačné → k ničemu
        if not en or re.search(r"\(disambiguation\)|rozcestník|may refer to",
                               en, re.I):
            return ""
        # strhni Wiki disambiguační příponu „(film)"/„(character)" — není součást jména
        en = re.sub(r"\s*\([^)]*\)\s*$", "", en).strip()
        if en and en.lower() != n.lower():
            return en
    except Exception as e:
        _log.debug("art: _en_name(%s) selhal: %s", n, e)
    return ""


def _ground_via_wikipedia(config: dict, db_path: str, subject: str) -> str:
    """HANS_ART_SUBJECT_GROUNDING_V2 — když námět není v Hansově čtení (C1 miss),
    dohledej ho na Wikipedii a ROVNOU ulož do entity store (příště už zná; pomůže
    i chatu). Vrací „Titul: definiční věta" nebo '' (nenalezeno/nevhodné)."""
    low = (subject or "").strip().lower()
    # jen konkrétní pojmenované věci — ne konverzační seed / dlouhé fráze /
    # POPISNÉ SCÉNY (4+ slov = „klidná krajina s řekou za soumraku" NENÍ entita,
    # jinak se resolvne na náhodného malíře krajin a pollutuje store).
    if (not subject or len(subject) > 60 or len(subject.split()) > 3
            or low.startswith(("náš rozhovor", "dojem", "nas rozhovor"))):
        return ""
    try:
        from scripts.web_reader import WebReader
        from scripts.hans_entities import EntityStore, _first_sentence
        lang = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
        art = WebReader(config).wikipedia_article(subject, lang=lang,
                                                  max_chars=1500)
        if not art or not (art.get("text") or "").strip():
            return ""
        # ulož do entity store (čistý glos ze zdroje = anti-konfab)
        try:
            EntityStore(config, db_path).capture_from_reading(
                art["title"], art["text"], url=art.get("url", ""),
                lang=art.get("lang", lang))
        except Exception:
            pass
        gloss = _first_sentence(art["text"])
        # rozcestník („Bauhaus má více významů") = k ničemu → ber jako miss
        if re.search(r"(m[aá] více význam|může (být|označovat|odkazovat)|"
                     r"rozcestník|may refer to|several meanings)", gloss, re.I):
            _log.debug("art: '%s' → rozcestník, přeskakuji", subject)
            return ""
        if gloss.strip():
            name = art["title"]
            _en = _en_name(art.get("page_title", name),   # HANS_ART_EN_TITLE_V1
                           lang=art.get("lang", lang))
            if _en:
                name = "%s (English name to use in the image prompt: %s)" % (name, _en)
            _log.info("art: namet '%s' dohledan na Wikipedii → '%s'%s "
                      "(ulozeno do entity store)", subject, art["title"],
                      f" [EN: {_en}]" if _en else "")
            return "%s: %s" % (name, gloss.strip())
    except Exception as e:
        _log.debug("art: wiki grounding selhal: %s", e)
    return ""


# HANS_ART_SCENE_NO_GROUND_V1 — pozná KOMPOZIČNÍ scénu (víc prvků, „v pozadí",
# koordinace „a"), kterou NELZE scvrknout na jednu entitu. Doložený případ:
# „alej sakur a v pozadí japonskou svatyni" → loose match překlepu „svatini" na
# entitu „Svatba" přebil celý prompt → svatba místo sakur. Scéna jde RAW do
# scene-prompt LLM (ten víceprvkový popis zvládne). Diakritika volitelná.
def _looks_like_scene(s: str) -> bool:
    sl = " " + (s or "").lower() + " "
    if "pozad" in sl or "popred" in sl or "popřed" in sl:  # v pozadí / v popředí
        return True
    content = [w for w in sl.split() if len(w) >= 4]
    if " a " in sl and len(content) >= 3:                   # koordinace ≥3 prvků
        return True
    return False


# HANS_ART_CHAR_APPEARANCE_V1 — detekce fiktivní postavy + extrakce vzhledu z Wiki
_IS_CHARACTER = re.compile(
    r"poh[áa]dkov[áéíou]+\s+postav|fiktivn[íi]|je\s+postav|postav[au]\s+z\b"
    r"|ve[čc]ern[íi][čc]|kreslen|animovan|hrdin[au]|loupe[žz]n", re.I)
_APPEAR_KW = re.compile(
    r"vous|klobouk|[čc]epic|nos[íi]\b|o[šs]acen|oble[čc]|obuv|botk|vlas|sukn"
    r"|halenk|[šs]aty|kab[áa]t|pl[áa][šs][ťt]|br[ýy]l|vzhled|vypad|m[áa]\s+na\s+sob",
    re.I)


def _wiki_character_appearance(config: dict, name: str) -> str:
    """Vytáhne z Wiki článku VĚTY o vzhledu postavy (oblečení, vlasy, rysy).
    Fiktivní postavy bez vlastního článku (Manka→Rumcajs) sdílí pasáž, která
    obvykle popisuje víc postav — vezmi ji, scene-LLM zdůrazní zadanou."""
    try:
        from scripts.web_reader import WebReader
        a = WebReader(config).wikipedia_article(name)
        if not a or not a.get("text"):
            return ""
        sents = re.split(r"(?<=[.!?])\s+", a["text"])
        # appearance věty PŘEDNOST (vzhled), doplň větami se jménem subjektu
        # (role/kontext — pomůže sub-postavám bez vlastního vzhledu: Cipísek).
        appear = [s.strip() for s in sents if _APPEAR_KW.search(s)]
        named = [s.strip() for s in sents
                 if re.search(re.escape(name), s, re.I) and s.strip() not in appear]
        out = appear[:4] + named[:2]
        return " ".join(out)[:700]
    except Exception as e:
        _log.debug("art: _wiki_character_appearance: %s", e)
        return ""


# HANS_ART_PLACE_APPEARANCE_V1 (29.8.) — PODOBA MÍSTA z Wiki článku.
# Táž třída chyby jako u fiktivních postav (HANS_ART_CHAR_APPEARANCE_V1),
# jen jiná větev: `gloss` je PRVNÍ VĚTA článku, tedy ZAŘAZENÍ, ne PODOBA.
# Doloženo 27.8. (deník artwork id 132043): „Hrad Trosky" → gloss „zřícenina
# hradu na vrcholu stejnojmenného vrchu" → prompt „Ruin of Trosky Castle
# stands atop a hill" → obraz obecné zříceniny na kopci. Charakteristická
# dvojice věží na sopouších (Panna a Baba) přitom V ČLÁNKU JE, jen o pár
# odstavců níž — grounding se cestou neztrácel, on nikdy nevznikl.
# Popisné sekce v pořadí PRIORITY (ne v pořadí výskytu v článku — u Trosek
# stojí „Přírodní poměry" před „Stavební podobou", ale silueta je v druhé).
_PLACE_SEC_PRIO = [
    r"Stavebn[íi]\s+podoba", r"Fyzick[ýy]\s+popis", r"Popis", r"Architektura",
    r"Podoba", r"Vzhled", r"P[řr][íi]rodn[íi]\s+pom[ěe]ry", r"Charakteristika",
    r"Geografie", r"Poloha",
]
_PLACE_HEAD = re.compile(r"\n?(=+)\s*[^=\n]{2,60}\s*=+\n?")
_PLACE_VIS = re.compile(
    r"v[ěe][žz]|skal|[čc]edi[čc]|sopou|vulk[áa]n|nefelinit|vrchol|dominant"
    r"|hradb|pal[áa]c|kupol|ark[áa]d|p[ůu]dorys|st[řr]ech|fas[áa]d|okn|klenb"
    r"|n[áa]dvo|tyč[íi]|vyp[íi]n[áa]|kamen|cihl|z[ďd]|brán|most|jezer|vodop[áa]d"
    r"|poho[řr]|[úu]dol|les|[řr]ek|tvo[řr][íi]\s|rozkl[áa]d|obklop|elips|kruh"
    r"|patr|sloup|oblouk|mramor", re.I)
# Věty o DĚJINÁCH se do obrazového promptu nehodí (majitelé, přestavby,
# letopočty). Bez tohoto filtru vytáhl prototyp Bezdězu „přestavbu" místo
# okrouhlé Čertovy věže.
# HANS_ART_PLACE_NO_PEOPLE_V1 — popisná sekce nemusí popisovat PODOBU.
# Doloženo při stavbě: česká sekce „Fyzický popis" u Kolosea je o kapacitě
# a o tom, kde seděli senátoři → bez tohoto filtru by patch u Kolosea
# ZHORŠIL dnešní stav (dnes tam jde aspoň jen holá glosa).
_PLACE_LIDE = re.compile(
    r"sen[áa]tor|ob[čc]an|[šs]lecht|div[áa]k|obyvatel|n[áa]v[šs]t[ěe]vn"
    r"|jezdc|patricij|posazen|sedadl|sez(en|ení)|kapacit|pojmout", re.I)
_PLACE_HIST = re.compile(
    r"roku?\s+\d|\d{3,4}|stolet[íi]|p[řr]estav|zbo[řr]|majitel|rod[uů]\b"
    r"|kr[áa]l|c[íi]sa[řr]", re.I)


def _wiki_place_appearance(config: dict, name: str, max_chars: int = 700) -> str:
    """Vytáhne z Wiki článku věty o PODOBĚ místa (silueta, hmota, materiál).

    Tři věci, které se při stavbě ukázaly jako nutné (změřeno na 8 místech):
    (1) `max_chars=40000` — výchozích 12 000 znaků článek uřízne JEŠTĚ PŘED
        popisnou sekcí (ta stojí až za historií); u Kosti i Kolosea se do
        výřezu nevešla vůbec.
    (2) konec sekce = nadpis STEJNÉ nebo VYŠŠÍ úrovně. „Fyzický popis" má
        hned pod sebou podsekci → naivní „do dalšího ==" vrátilo 2 znaky.
    (3) ŽÁDNÝ fallback na klíčová slova mimo popisnou sekci. Vyzkoušeno
        a zahozeno: tahal do obrazového promptu majitele hradu („Zajícové
        z Hazmburka") a koloniální dějiny Zambie. Radši nic než historie —
        beze změny se chová jako dosud.
    """
    try:
        from scripts.web_reader import WebReader
        a = WebReader(config).wikipedia_article(name, max_chars=40000)
        txt = (a or {}).get("text") or ""
        if not txt:
            return ""
        out = _place_appearance_from_text(txt, max_chars)
        if out:
            _log.info("art: podoba místa '%s' — %d zn.", name, len(out))
        return out
    except Exception as e:
        _log.debug("art: _wiki_place_appearance: %s", e)
        return ""


def _place_appearance_from_text(txt: str, max_chars: int = 700) -> str:
    """HANS_ART_PLACE_PURE_V1 — čistá půlka `_wiki_place_appearance` (bez sítě),
    aby šla tvrdit v regresní sadě. Tři pravidla, která tu drží, se dají snadno
    „zjednodušit" a rozbít TIŠE, proto mají každé svůj případ v regresi."""
    if not txt:
        return ""
    try:
        for pat in _PLACE_SEC_PRIO:
            m = re.search(r"(=+)\s*(" + pat + r"[^=\n]{0,30}?)\s*=+", txt, re.I)
            if not m:
                continue
            uroven = len(m.group(1))
            konec = len(txt)
            for h in _PLACE_HEAD.finditer(txt, m.end()):
                if len(h.group(1)) <= uroven:
                    konec = h.start()
                    break
            body = _PLACE_HEAD.sub(" ", txt[m.end():konec]).strip()
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
            vis = [s for s in sents
                   if _PLACE_VIS.search(s) and not _PLACE_HIST.search(s)
                   and not _PLACE_LIDE.search(s)]   # HANS_ART_PLACE_NO_PEOPLE_V1
            out, n = [], 0
            for s in vis:
                if n + len(s) > max_chars:
                    break
                out.append(s)
                n += len(s) + 1
                if len(out) >= 4:
                    break
            if out:
                _log.info("art: podoba místa ze sekce '%s' (%d vět)",
                          m.group(2).strip(), len(out))
                return " ".join(out)
        return ""
    except Exception as e:
        _log.debug("art: _place_appearance_from_text: %s", e)
        return ""


def _ground_subject(config: dict, db_path: str, subject: str) -> str:
    """HANS_ART_SUBJECT_GROUNDING_V1/V2 — zjisti, KOHO/CO malovat.
    Kaskáda: (1) C1 entity store (Hansovo čtení) → (2) Wikipedia fallback
    (a ulož do store). SDXL tak dostane informovaný popis místo holého jména
    („pan Sorge" → „Erich Robert Sorge: německý církevní hudebník a skladatel").
    Bez shody vrací syrový námět."""
    s = (subject or "").strip()
    if not s:
        return s
    # HANS_ART_SCENE_NO_GROUND_V1 — víceprvkovou scénu NEscvrkávej na entitu
    if _looks_like_scene(s):
        return s
    s_clean = _HONORIFIC.sub("", s).strip() or s
    try:
        from scripts.hans_entities import EntityStore
        es = EntityStore(config, db_path)
        # HANS_ART_COMMON_NOUN_V1 (5.8.) — OBECNÉ podstatné jméno se NEUKOTVUJE
        # na pojmenovanou entitu. Entity store je od TOHO, aby poznal JMÉNA
        # („pan Sorge", „Bud Spencer"); u obecného slova je shoda skoro vždy
        # náhoda. Doloženo 5.8.: „namaluj kočku" → entita „Kockums" (švédská
        # loděnice) → prompt „Saab Kockums shipyard in Malmö" → obraz PŘÍSTAVU
        # S JEŘÁBY. Prahem to opravit NELZE: „kocku" je regulérní prefix
        # „kockums", takže žádné pravidlo o délce prefixu to od českého
        # skloňování („hrad"→„hradu") neodliší. Rozlišovač, který k dispozici
        # JE: uživatel píše jména s velkým písmenem, obecná slova malým.
        # Bez velkého písmene se grounding přeskočí ÚPLNĚ (i Wikipedia — ta
        # dělá tutéž chybu) a FLUX beztak ví, jak vypadá kočka.
        # Souvisí HANS_ENTITY_SURNAME_PERSON_ONLY_V1 (táž třída, jiná větev).
        if not _is_proper_name(s_clean, config):   # HANS_ART_NAME_CLASSIFY_V1
            # Obecné slovo → NEGROUNDOVAT VŮBEC (ani Wikipedií: „kočku" tam
            # padne na „Kockums", „psa" na „Psaní levou rukou" — týž prefix
            # problém). Syrový námět převede do angličtiny až scene-prompt,
            # což je přesně jeho práce, a FLUX ví, jak vypadá kočka.
            _log.info("art: '%s' je obecné slovo — grounding přeskočen", s_clean)
            return s
        ent = es.resolve(s, loose=True) or es.resolve(s_clean, loose=True)
        gloss = (ent.get("gloss") if ent else "") or ""
        if gloss.strip():
            name = ent.get("name", s_clean)
            _wl = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
            _en = _en_name(name, lang=_wl)          # HANS_ART_EN_TITLE_V1
            if _en:
                name = "%s (English name to use in the image prompt: %s)" % (name, _en)
            _log.info("art: namet '%s' ukotven na entitu '%s'%s", s,
                      ent.get("name", s_clean), f" [EN: {_en}]" if _en else "")
            _base = "%s: %s" % (name, gloss.strip())
            # HANS_ART_CHAR_APPEARANCE_V1 — fiktivní postava (Rumcajs…), kterou
            # FLUX nezná: gloss je definice bez vzhledu → vytáhni z Wiki článku
            # VZHLED (červený klobouk, vousy…), ať má FLUX co malovat.
            if _IS_CHARACTER.search(gloss):
                _app = _wiki_character_appearance(config, ent.get("name", s_clean))
                if _app:
                    _log.info("art: postava '%s' — vzhled z Wiki (%d zn)",
                              s_clean, len(_app))
                    _base = ("Vzhled postavy %s (ZDŮRAZNI ho v obraze): %s\n%s"
                             % (name, _app, _base))
            # HANS_ART_PLACE_APPEARANCE_V1 — místo (hrad, zřícenina, jezero):
            # gloss řekne, CO to je, ne JAK to vypadá. Brána je `etype` z entity
            # storu, ne regex nad prózou — užší a nelže.
            elif (ent.get("etype") or "") == "místo":
                _app = _wiki_place_appearance(config, ent.get("name", s_clean))
                if _app:
                    _log.info("art: místo '%s' — podoba z Wiki (%d zn)",
                              s_clean, len(_app))
                    _base = ("Podoba místa %s (ZDŮRAZNI ji v obraze): %s\n%s"
                             % (name, _app, _base))
            return _base
        # C1 miss → Wikipedia fallback (dohledej + ulož do store)
        enriched = _ground_via_wikipedia(config, db_path, s_clean)
        if enriched:
            return enriched
    except Exception as e:
        _log.debug("art: grounding námětu selhal: %s", e)
    return s


def tv_paint_subject(config: dict, db_path: str, now_playing: dict) -> tuple:
    """HANS_ART_TV_GROUNDING_V1 — nejlepší malovací NÁMĚT z běžícího pořadu.
    Dřív se malovalo jen z názvu → u pořadu bez popisku vznikla odpojená scéna
    („Vyprávěj, přijímačky" → černobílá cesta lesem). Kaskáda:
      (1) PLOT z Kodi (přímý popis děje) → nejlepší,
      (2) plot chybí → DOHLEDEJ popis na Wikipedii (i EN přes langlink),
      (3) ani to → jen název (a přiznat to volajícímu).
    Vrací (subject_pro_malbu, source_label). source_label = odkud popis je."""
    np = now_playing or {}
    title = (np.get("title") or np.get("label") or "").strip()
    show  = (np.get("showtitle") or "").strip()
    plot  = (np.get("plot") or np.get("plotoutline") or "").strip()
    # zobrazovaný název: „Seriál – epizoda" u dílu, jinak název
    disp = ("%s – %s" % (show, title)) if (show and title and
            show.lower() != title.lower()) else (title or show)
    if plot and len(plot) >= 40:
        return ("%s: %s" % (disp, plot[:400]), "z popisu pořadu")
    # plot chybí → internet (Wikipedia); zkus seriál i epizodní název
    for q in (show, title):
        q = (q or "").strip()
        if not q:
            continue
        g = _ground_via_wikipedia(config, db_path, q)
        if g:
            _log.info("art TV: popisek chyběl → dohledáno na Wikipedii pro '%s'", q)
            return (g, "z internetu (popisek u pořadu chyběl)")
    return (disp or title or show or "televizní obrazovka", "jen podle názvu")


def _style_from_study(config: dict, style: str) -> str:
    """HANS_ART_STYLE_V4 — popis stylu z HANSOVÝCH studijních poznámek (RAG
    hans_cetba/hans_identita — Hans studoval Design). Osobnější než Wikipedie.
    Vrátí chunk (≤300 zn) jen když se styl v textu opravdu vyskytuje, jinak ''."""
    try:
        from scripts.hans_knowledge import HansKnowledge
        from scripts.hans_entities import _norm
        kn = HansKnowledge(config)
        key = _norm(style)
        first = (key.split() or [""])[0]
        for col in ("hans_cetba", "hans_identita"):
            try:
                res = kn.query(col, style, 3, 0.8)
            except Exception:
                continue
            for ch in (getattr(res, "chunks", None) or []):
                t = (ch.get("text") or "").strip()
                if t and first and first[:6] in _norm(t):
                    _log.info("art: styl '%s' ukotven z Hansova studia (%s)",
                              style, col)
                    return t[:300]
    except Exception as e:
        _log.debug("art: style-from-study selhal: %s", e)
    return ""


def _ground_style(config: dict, db_path: str, style: str) -> str:
    """HANS_ART_STYLE_V4 — ukotvi umělecký styl (umělec/směr). Kaskáda:
    (1) C1 entity store → (2) Hansovy studijní poznámky (RAG) → (3) Wikipedia
    (uloží do store). Vrací krátký český popis stylu, fallback syrový styl."""
    s = (style or "").strip()
    if not s:
        return s
    try:
        from scripts.hans_entities import EntityStore
        ent = EntityStore(config, db_path).resolve(s, loose=True)
        if ent and (ent.get("gloss") or "").strip():
            return "%s: %s" % (ent["name"], ent["gloss"].strip())
    except Exception:
        pass
    note = _style_from_study(config, s)
    if note:
        return note
    wiki = _ground_via_wikipedia(config, db_path, s)
    if wiki:
        return wiki
    # rozcestník / nenalezeno → zkus stylové upřesnění (Bauhaus → škola/směr)
    for hint in (" (výtvarná škola)", " umělecký směr", " umělecký sloh",
                 " (umění)"):
        wiki = _ground_via_wikipedia(config, db_path, s + hint)
        if wiki:
            return wiki
    return s


_STYLE_SCENE_SYSTEM = (
    "You turn a short Czech description of a SUBJECT and an ART STYLE into ONE "
    "concise English SDXL image prompt — an evocative artistic INTERPRETATION of "
    "the subject RENDERED IN THAT STYLE. Output ONLY the prompt (no preamble, no "
    "quotes). Reply in ENGLISH ONLY (no Chinese/Japanese). NO text, letters or "
    "words in the image, NO watermark. END with strong English keywords of the "
    "requested ART STYLE (movement/artist name + its visual traits), NOT a generic "
    "tail."
)


# ── HANS_ART_PERSON_LIKENESS_V3 — podoba osoby (img2img z reálné fotky) ──────
def _download_ref_image(url: str) -> Optional[str]:
    """Stáhni obrázek na /tmp. Vrací cestu nebo None."""
    try:
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            ext = ".jpg"
        path = os.path.join("/tmp", "hans_ref_%d%s" % (int(time.time()), ext))
        req = urllib.request.Request(
            url, headers={"User-Agent": "HansBot/1.0 (home assistant)"})
        with urllib.request.urlopen(req, timeout=25) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception as e:
        _log.debug("art: download ref selhal: %s", e)
        return None


def _resolve_entity(config: dict, db_path: str, subject: str):
    """Entity dict pro námět (name/etype/gloss/source/source_title) z C1 store;
    když chybí, dohledá na Wikipedii (uloží) a resolvuje znovu. None = nic."""
    s = (subject or "").strip()
    if not s:
        return None
    s_clean = _HONORIFIC.sub("", s).strip() or s
    try:
        from scripts.hans_entities import EntityStore
        es = EntityStore(config, db_path)
        ent = es.resolve(s, loose=True) or es.resolve(s_clean, loose=True)
        if ent:
            return ent
        if _ground_via_wikipedia(config, db_path, s_clean):
            return es.resolve(s, loose=True) or es.resolve(s_clean, loose=True)
    except Exception as e:
        _log.debug("art: resolve entity selhal: %s", e)
    return None


def _fetch_person_ref(config: dict, ent: dict) -> Optional[str]:
    """Stáhni portrét osoby z Wikipedie (dle source URL entity), zmenši pro SDXL.
    Vrací lokální cestu nebo None (osoba bez obrázku → fallback na text)."""
    title = ent.get("source_title") or ent.get("name") or ""
    if not title:
        return None
    src = ent.get("source") or ""
    m = re.search(r"https?://([a-z]{2})\.wikipedia", src)
    lang = m.group(1) if m else (config.get("curiosity", {}) or {}).get(
        "wiki_lang", "cs")
    try:
        from scripts.web_reader import WebReader
        url = WebReader(config).wikipedia_image(title, lang=lang)
        if not url:
            return None
        raw = _download_ref_image(url)
        if not raw:
            return None
        tmp = _resize_to_temp(raw)
        try:
            os.remove(raw)
        except Exception:
            pass
        return tmp
    except Exception as e:
        _log.debug("art: fetch person ref selhal: %s", e)
        return None


_PERSON_FILLER = {
    "na", "ve", "in", "the", "of", "and", "pan", "pani", "paní", "herce",
    "herec", "hereckou", "portret", "portrét", "obraz", "podobizna", "jako",
    "se", "si", "je", "byl", "byla", "sir", "lord", "mistr", "svaty", "svatý",
}


def _subject_beyond_name(subject: str, person_name: str) -> bool:
    """HANS_ART_PORTRAIT_IMG2IMG_V1 — nese námět kromě JMÉNA ještě něco (akci,
    místo, rekvizitu)? „Bud Spencer" → False (portrét), „Radecký na motorce"
    → True (scéna). Rozhoduje o img2img × PuLID, viz `paint_person_from_photo`.

    Porovnává se na složeninách bez diakritiky, prefixově (skloňování
    „Radeckého") a s podobnostním prahem na PŘEKLEPY („TerRence" vs uložené
    „Terence" — jinak by překlep vypadal jako obsah navíc a portrét by se
    poslal na scénu)."""
    def _fold(s):
        import unicodedata
        s = unicodedata.normalize("NFKD", (s or "").lower())
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^\w\s]", " ", s).split()

    from difflib import SequenceMatcher as _SM
    name_toks = _fold(person_name)
    rest = []
    for t in _fold(subject):
        if len(t) < 3 or t in _PERSON_FILLER:
            continue
        if t in name_toks:
            continue
        if any(t[:4] == n[:4] and min(len(t), len(n)) >= 4 for n in name_toks):
            continue
        if any(_SM(None, t, n).ratio() >= 0.8 for n in name_toks):
            continue
        rest.append(t)
    return bool(rest)


def paint_person_from_photo(config: dict, diary_db_path: str, subject: str,
                            ref_path: str, style: str = "",
                            person_name: str = "") -> Optional[tuple]:
    """HANS_ART_PERSON_LIKENESS_V3 — přemaluj REÁLNÝ portrét osoby do Hansova
    stylu (img2img, denoise ~0.5 → drží podobu). Vrací (rel_path, caption) nebo
    None (→ volající spadne na text-grounded malbu). Nikdy nehází."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    ckpt = _ckpt(config)
    if not ckpt or not ref_path or not os.path.exists(ref_path):
        return None
    base = _comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception:
        return None
    img_name = _comfy_upload_image(base, ref_path)
    try:
        os.remove(ref_path)
    except Exception:
        pass
    if not img_name:
        return None

    nm = person_name or subject
    acfg = _acfg(config)
    pcfg = (acfg.get("person_likeness", {}) or {})
    seed = uuid.uuid4().int % (2**31)
    client_id = uuid.uuid4().hex
    # HANS_ART_PULID_V1 — když je FLUX+PuLID zapnutý, zachovej PODOBU z ref fota a
    # slož NOVOU scénu z celého námětu (osoba NA MOTORCE ap.). Jinak legacy
    # img2img (drží kompozici → jen portrét, akce/scéna se ztratí).
    #
    # HANS_ART_PORTRAIT_IMG2IMG_V1 (4.8.) — obě cesty mají OPAČNÝ kompromis a
    # dosud rozhodoval jen config, takže PuLID přebil i prosté portréty:
    #   • img2img  = překreslí SKUTEČNOU fotku → drží podobu, ale drží i
    #     kompozici, takže scéna/akce se ztratí,
    #   • PuLID    = složí novou scénu, podobu drží jen „na první pohled".
    # Rozhoduje tedy ZADÁNÍ: nese-li námět kromě jména i akci/místo („Radecký
    # na motorce"), je potřeba scéna → PuLID; holé jméno („Bud Spencer") =
    # portrét → img2img. Denoise 0.70 (laděno naživo 4.8. na Bud Spencerovi:
    # 0.50 = přemalovaná fotka, 0.75 už posouvá rysy, 0.70 drží podobu
    # a přitom je to malba). `denoise` čte JEN img2img větev — PuLID jede na
    # `pulid_weight`, tahle hodnota mu nesahá do scén.
    _wants_scene = _subject_beyond_name(subject, nm)
    _use_pulid = (bool(pcfg.get("use_pulid", False))
                  and bool(acfg.get("use_flux", False))
                  and (_wants_scene or not pcfg.get("portrait_img2img", True)))
    _log.info("art: podoba '%s' → %s (%s)", nm,
              "FLUX+PuLID (scéna)" if _use_pulid else "img2img překreslení fotky",
              "námět nese akci/místo" if _wants_scene else "holé jméno = portrét")
    if _use_pulid:
        _flux_ckpt = acfg.get("flux_ckpt", "flux1-dev-fp8.safetensors")
        _intro = ("Subject to depict (described in Czech): %s\n\n"
                  "Write ONE vivid English image prompt of THIS scene. Name the "
                  "person and describe era-appropriate clothing, the action and "
                  "the setting. The exact face is supplied separately, so focus on "
                  "scene, pose, attire and atmosphere.\n\n" % subject)
        prompt = _scene_prompt(config, subject, "", diary_db_path,
                               system=_SUBJECT_SCENE_SYSTEM, source_intro=_intro)
        w = int(acfg.get("width", 896)); h = int(acfg.get("height", 1152))
    else:
        style_kw = (", in the style of %s" % style) if style else ""
        prompt = ("expressive painterly portrait of %s%s, oil painting, artistic "
                  "brushwork, rich detail, atmospheric lighting, masterful" %
                  (nm, style_kw))
    dn = float(pcfg.get("denoise", 0.5))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    os.makedirs(ART_DIR, exist_ok=True)
    fname = "%d_%s_podoba.png" % (int(time.time()), _slug(subject))
    dest = os.path.join(ART_DIR, fname)

    if not _comfy_ready(config):
        return None
    loaded = _ollama_loaded(config)
    _ollama_unload(config, loaded)
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        if _use_pulid:
            wf = _comfy_workflow_flux_pulid(
                _flux_ckpt, prompt, seed, w, h,
                int(acfg.get("flux_steps", 20)),
                float(acfg.get("flux_guidance", 3.5)), img_name,
                float(pcfg.get("pulid_weight", 0.9)))
            _log.info("art: podoba osoby FLUX+PuLID start (%s) — %.90s", nm, prompt)
        else:
            _pneg = _person_negative(diary_db_path)
            wf = _comfy_workflow_img2img(ckpt, prompt, seed, img_name, dn, steps,
                                         cfg_s, negative=_pneg)
            _log.info("art: podoba osoby img2img start (%s, denoise %.2f) — neg: %.90s",
                      nm, dn, _pneg)
        pid = _comfy_submit(base, wf, client_id)
        if pid:
            hist = _comfy_wait(base, pid, timeout=rtimeout)
            img = _first_image(hist) if hist else None
            if img and _comfy_fetch_image(base, img, dest):
                ok = True
    except Exception as e:
        _log.warning("art: podoba img2img selhal: %s", e)
    finally:
        _comfy_free(config)
        if ok:
            vision_desc = _describe_render(config, dest)
        _ollama_warm(config,
                     config.get("models", {}).get("dialog", "hans-czech:latest"))
    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    title = subject[:80] if not style else ("%s (styl: %s)" % (subject, style))[:80]
    caption = _evaluate_artwork(config, diary_db_path, title, subject,
                                vision_desc,
                                source_label="podle skutečné podoby osoby")
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "person",
                         "denoise": dn, "painted_ts": time.time()},
                        ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log person artwork failed: %s", e)
    _log.info("art: Hans namaloval podobu osoby '%s' → %s", nm, rel_path)
    return rel_path, caption


def paint_place_from_photo(config: dict, diary_db_path: str, subject: str,
                           ref_path: str, style: str = "",
                           place_name: str = "",
                           grounded: str = "") -> Optional[tuple]:
    """HANS_ART_PLACE_LIKENESS_V1 (29.8.) — přemaluj REÁLNOU FOTKU místa do
    Hansova stylu (img2img). Vrací (rel_path, caption) nebo None (→ volající
    spadne na text-grounded malbu). Nikdy nehází.

    PROČ VŮBEC: text sám nestačil. I když `HANS_ART_PLACE_APPEARANCE_V1` dostal
    do promptu „the two spires and palaces", FLUX to přečetl jako ŠPIČATÉ
    STŘECHY a namaloval pohádkový zámek (29.8., 299 s). Fotka tutéž informaci
    nese jednoznačně.

    ⛔ IP-ADAPTER JE VYZKOUŠENÝ A ZAMÍTNUTÝ (29.8., měřeno na Troskách):
    weight 0.75 dal fotorealistický letecký pohled s JEDNOU věží — druhou
    ztratil a přiopsal z fotky i nesouvisející chalupu. Je to přesně týž
    kompromis, jaký má u osob PuLID (`HANS_ART_PORTRAIT_IMG2IMG_V1`): volná
    kompozice, podoba jen „na první pohled". U holého názvu místa chceme
    OPAK — věrnost. Proto img2img.

    ⚠️ Cena, kterou to má: img2img zdědí ZÁBĚR fotky. Wikipedia má u Trosek
    i u Kosti letecký snímek → Hans maluje z ptačí perspektivy. U osob je to
    táž vlastnost a je přijatá.
    """
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    ckpt = _ckpt(config)
    if not ckpt or not ref_path or not os.path.exists(ref_path):
        return None
    base = _comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception:
        return None
    img_name = _comfy_upload_image(base, ref_path)
    try:
        os.remove(ref_path)
    except Exception:
        pass
    if not img_name:
        return None

    nm = place_name or subject
    acfg = _acfg(config)
    lcfg = (acfg.get("place_likeness", {}) or {})
    # anglický název do promptu (HANS_ART_EN_TITLE_V1) — SDXL na český nereaguje
    _wl = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
    nm_en = _en_name(nm, lang=_wl) or nm
    style_kw = (", in the style of %s" % style) if style else ""
    # HANS_ART_PLACE_PROMPT_V1 (29.8.) — ⚠️ U MÍSTA NESTAČÍ HOLÁ ŠABLONA.
    # Doloženo živým během: prompt „expressive painterly view of Hrad Trosky"
    # + fotka Trosek při denoise 0.60 dal VESNICI S KOSTELEM. Fotka strukturu
    # NEUŘÍDÍ sama — prompt ji přebije. (U osob šablona stačí, protože portrét
    # žádný obsah nepotřebuje: obličej je obličej. Místo potřebuje vědět, ŽE
    # jsou to dvě věže na skalách — jinak si model dosadí, co je běžnější.)
    # Proto se prompt staví z groundingu (`HANS_ART_PLACE_APPEARANCE_V1`) touž
    # cestou jako u text-grounded malby.
    prompt = ""
    if grounded:
        _intro = ("Place to depict (described in Czech):\n%s\n\n"
                  "A real photograph of this place is supplied separately and "
                  "will be repainted. Write ONE English SDXL prompt that names "
                  "the place and describes its PHYSICAL FORM — rock, towers, "
                  "ruins, materials, surroundings. Do not invent buildings that "
                  "are not described.\n\n" % grounded)
        try:
            prompt = _scene_prompt(config, subject, grounded, diary_db_path,
                                   system=_SUBJECT_SCENE_SYSTEM,
                                   source_intro=_intro,
                                   cs_subject=subject) or ""
        except Exception as _spe:
            _log.debug("art: scene prompt místa selhal: %s", _spe)
    if not prompt:
        prompt = ("expressive painterly view of %s%s, oil painting, artistic "
                  "brushwork, rich detail, atmospheric lighting, masterful"
                  % (nm_en, style_kw))
    elif style_kw:
        prompt += style_kw
    dn = float(lcfg.get("denoise", 0.60))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2**31)
    client_id = uuid.uuid4().hex
    os.makedirs(ART_DIR, exist_ok=True)
    fname = "%d_%s_podoba.png" % (int(time.time()), _slug(subject))
    dest = os.path.join(ART_DIR, fname)

    if not _comfy_ready(config):
        return None
    _ollama_unload(config, _ollama_loaded(config))
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        wf = _comfy_workflow_img2img(ckpt, prompt, seed, img_name, dn, steps,
                                     cfg_s, negative=_NEG_BASE)
        _log.info("art: podoba místa img2img start (%s, denoise %.2f) — %.90s",
                  nm_en, dn, prompt)
        pid = _comfy_submit(base, wf, client_id)
        if pid:
            hist = _comfy_wait(base, pid, timeout=rtimeout)
            img = _first_image(hist) if hist else None
            if img and _comfy_fetch_image(base, img, dest):
                ok = True
    except Exception as e:
        _log.warning("art: podoba místa img2img selhala: %s", e)
    finally:
        _comfy_free(config)
        if ok:
            vision_desc = _describe_render(config, dest)
        _ollama_warm(config,
                     config.get("models", {}).get("dialog", "hans-czech:latest"))
    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    title = subject[:80] if not style else ("%s (styl: %s)" % (subject, style))[:80]
    caption = _evaluate_artwork(config, diary_db_path, title, subject, vision_desc,
                                source_label="podle skutečné podoby místa")
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "place",
                         "denoise": dn, "painted_ts": time.time()},
                        ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log place artwork failed: %s", e)
    _log.info("art: Hans namaloval podobu místa '%s' → %s", nm, rel_path)
    return rel_path, caption


def paint_self(config: dict, diary_db_path: str, full_figure: bool = True,
               style: str = ""):
    """HANS_ART_SELF_V1 — Hans namaluje SÁM SEBE z vlastního avatar descriptoru
    (jeho vzhled: butler, frak…), volitelně jako CELOU POSTAVU. Řeší, že „namaluj
    sebe" nemá znamenat namalovat uživatele. Vrací (rel_path, caption) nebo None.
    Nikdy nehází. VRAM: unload LLM → render → warm."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    try:
        from scripts.avatar_descriptor import latest_descriptor
        from scripts.avatar_render import build_prompt
    except Exception as e:
        _log.warning("art: paint_self import: %s", e)
        return None
    desc = latest_descriptor(diary_db_path) or {
        "role": "english butler", "attire": "black tailcoat, white gloves, "
        "high collar", "age_look": "late 50s", "build": "tall, slim",
        "demeanor": "formal and reserved", "setting": "wood-panelled study"}
    framing = ("full body shot, full-length portrait, standing upright, the "
               "complete figure from head to shoes, entire body and legs "
               "visible, shoes visible, wide framing, distant full-length view"
               if full_figure else "portrait, upper body")
    prompt = build_prompt(desc, "dignified, poised, looking at viewer", framing)
    if style:
        prompt = prompt + ", in the style of " + str(style)
    ckpt = _ckpt(config)
    if not ckpt:
        _log.warning("art: paint_self — image_model nenastaven")
        return None
    acfg = _acfg(config)
    scfg = (acfg.get("self_portrait", {}) or {})
    base = _comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception:
        return None
    steps = int(acfg.get("steps", 28))
    cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2 ** 31)
    client_id = uuid.uuid4().hex
    os.makedirs(ART_DIR, exist_ok=True)
    fname = f"{int(time.time())}_hans_self.png"
    dest = os.path.join(ART_DIR, fname)

    # HANS_ART_SELF_V1 — TEMPLATE = Hansův vytvořený avatar (img2img) → DRŽÍ jeho
    # vzhled. Bez avatara fallback na txt2img z descriptoru.
    avatar_path = ((config.get("hans_avatar", {}) or {}).get("face_image")
                   or "data/avatar/v1/idle.png")
    img_name = None
    if os.path.exists(avatar_path):
        tmp = _resize_to_temp(avatar_path)
        if tmp:
            img_name = _comfy_upload_image(base, tmp)
            try:
                os.remove(tmp)
            except Exception:
                pass
    # rozměry: celá postava = na výšku (IP-Adapter dá novou kompozici z portrétní
    # reference, na rozdíl od img2img, který drží kompozici vstupu = portrét)
    w, h = (832, 1216) if full_figure else (1024, 1024)
    ipa_model = scfg.get("ipadapter_file",
                         "ip-adapter-plus_sdxl_vit-h.safetensors")
    ipa_clip = scfg.get("clip_vision",
                        "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors")
    ipa_weight = float(scfg.get("ipadapter_weight", 0.75))

    if not _comfy_ready(config):
        return None
    _ollama_unload(config, _ollama_loaded(config))
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    try:
        if img_name:
            # IP-ADAPTER — NOVÁ kompozice (celá postava) + PODOBA avatara
            wf = _comfy_workflow_ipadapter(ckpt, prompt, seed, w, h, steps,
                                           cfg_s, img_name, ipa_model, ipa_clip,
                                           ipa_weight)
            _log.info("art: paint_self IP-ADAPTER z avatara (w=%.2f, %dx%d) — %.80s",
                      ipa_weight, w, h, prompt)
        else:
            wf = _comfy_workflow(ckpt, prompt, seed, w, h, steps, cfg_s)
            _log.info("art: paint_self txt2img (bez avatara) — %.80s", prompt)
        pid = _comfy_submit(base, wf, client_id)
        hist = _comfy_wait(base, pid, timeout=rtimeout) if pid else None
        img = _first_image(hist) if hist else None
        if img and _comfy_fetch_image(base, img, dest):
            ok = True
    except Exception as e:
        _log.warning("art: paint_self render selhal: %s", e)
    finally:
        _comfy_free(config)
        _ollama_warm(config, config.get("models", {}).get("dialog",
                                                          "hans-czech:latest"))
    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    caption = "Má vlastní podoba" + (" — celá postava" if full_figure else "")
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute("INSERT INTO diary (ts, event_type, title, note, data) "
                   "VALUES (?,?,?,?,?)",
                   (time.time(), "artwork", "Autoportrét", caption,
                    json.dumps({"path": rel_path, "prompt": prompt,
                                "source": "self"}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.debug("art: paint_self log: %s", e)
    return (rel_path, caption)


# HANS_ART_NAME_CLASSIFY_V1 (5.8.) — „je to JMÉNO, nebo obecné slovo?"
# `HANS_ART_COMMON_NOUN_V1` to rozhodoval podle VELKÉHO PÍSMENE, což je slabé:
# „namaluj bud spencer" malým písmenem přišlo o grounding a FLUX maloval jen
# podle jména. Rozhoduje teď model (hans-czech, viz HANS_INTENT_PC_V1) —
# změřeno 19/19 při 1,2 s, včetně sporných („kočka na zdi", „západ slunce"
# → obecné; „říp", „pán prstenů" → jméno).
# Fallback zůstává velké písmeno: model dole → přesně původní chování.
_NAME_CLS_SYSTEM = (
    "Uživatel chce namalovat obraz a řekl NÁMĚT. Rozhodni, jestli je námět "
    "VLASTNÍ JMÉNO (konkrétní osoba, postava, dílo, značka nebo zeměpisné "
    "jméno — něco, co má encyklopedické heslo), nebo OBECNÉ SLOVO (druh věci, "
    "zvíře, rostlina, běžný předmět nebo krajina).\n\n"
    "Příklady:\n„bud spencer\" -> jmeno\n„rumcajs\" -> jmeno\n"
    "„matka tereza\" -> jmeno\n„říp\" -> jmeno\n„pán prstenů\" -> jmeno\n"
    "„kočka\" -> obecne\n„pes\" -> obecne\n„les\" -> obecne\n"
    "„starý dům\" -> obecne\n\n"
    "Odpověz JEDNÍM slovem: jmeno nebo obecne."
)

_name_cls_cache: dict = {}


def _is_proper_name(subject: str, config: dict) -> bool:
    """Je námět vlastní jméno (→ má smysl groundovat)?"""
    s = (subject or "").strip()
    if not s:
        return False
    _cap = any(w[:1].isupper() for w in s.split() if w)   # fallback heuristika
    if s in _name_cls_cache:
        return _name_cls_cache[s]
    try:
        from scripts.hans_intent import _ask_classifier
        out = _ask_classifier(config, _NAME_CLS_SYSTEM, s)
    except Exception:
        out = None
    if out is None:
        return _cap
    low = (out or "").strip().lower()
    if low.startswith("jmeno") or low.startswith("jméno"):
        res = True
    elif low.startswith("obecne") or low.startswith("obecné"):
        res = False
    else:
        return _cap                       # nejednoznačné → heuristika
    if len(_name_cls_cache) < 256:
        _name_cls_cache[s] = res
    return res


def _name_shaped(subject: str) -> bool:
    """HANS_ART_PERSON_WIKI_LOOKUP_V1 — vypadá námět jako JMÉNO (a stojí tedy za
    to zkusit, jestli to není osoba)? Krátký a s velkým písmenem. Drží Wikipedia
    dotaz od běžných námětů („kočka na zdi"), ať se nedělá zbytečný request
    navíc — `_ground_subject` si stejně sáhne na Wikipedii sám."""
    s = (subject or "").strip()
    if not s:
        return False
    toks = s.split()
    if not (1 <= len(toks) <= 4):
        return False
    # aspoň jeden token začíná velkým písmenem (a nejde o celou větu)
    return any(t[:1].isupper() for t in toks)


def _wiki_capture_person(config: dict, db_path: str, subject: str):
    """Dohledej námět na Wikipedii a ulož jako entitu (etype určí `_classify`).
    Vrací NÁZEV nalezeného článku (nebo None) — ne jen True: Wikipedia opraví
    i překlep („TerRence Hill" → „Terence Hill") a resolvovat se pak musí podle
    OPRAVENÉHO titulu, jinak by překlep entitu minul (token-prefix match potřebuje
    shodné první 4 znaky, a „terr" ≠ „tere"). Sdílí mechaniku s `_ground_subject`;
    tady jde o to, aby cesta k PODOBĚ měla stejné vstupy jako grounding."""
    try:
        from scripts.web_reader import WebReader
        from scripts.hans_entities import EntityStore
        lang = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
        art = WebReader(config).wikipedia_article(subject, lang=lang,
                                                  max_chars=1500)
        if not art or not (art.get("text") or "").strip():
            return None
        EntityStore(config, db_path).capture_from_reading(
            art["title"], art["text"], url=art.get("url", ""),
            lang=art.get("lang", lang))
        _log.info("art: osobu '%s' jsem neznal → dohledal na Wikipedii '%s'",
                  subject, art["title"])
        return art["title"]
    except Exception as e:
        _log.debug("art: wiki lookup osoby selhal: %s", e)
        return None


def paint_subject(config: dict, diary_db_path: str, subject: str,
                  style: str = ""):
    """Hans namaluje obraz na LIBOVOLNÉ téma / dojem (např. z rozhovoru).
    style: volitelný umělecký styl („Salvador Dalí", „Bauhaus", „gotika") —
    grounduje se a vloží do promptu místo pevného ocasu (HANS_ART_STYLE_V4).
    Loguje do galerie jako source='subject'. Vrací (rel_path, caption) nebo None.
    Nikdy nehází. VRAM orchestrace uvnitř _render_image."""
    subject = (subject or "").strip()
    if not subject:
        return None
    style = (style or "").strip()
    title = subject[:80]
    if style:
        title = ("%s (styl: %s)" % (subject, style))[:80]
    # HANS_ART_PERSON_LIKENESS_V3 — je-li námět OSOBA, nejdřív zkus img2img
    # z reálného portrétu (drží podobu). Miss / bez fotky → text-grounded malba.
    if (_acfg(config).get("person_likeness", {}) or {}).get("enabled", True):
        try:
            # HANS_ART_PULID_V1 — nejdřív hledej ZNÁMOU OSOBU v námětu (i když jsou
            # tam další entity, např. 'Harley-Davidson', které by jinak přebily);
            # osoba má přednost → PuLID zachová podobu + složí scénu z celého námětu.
            _ent = None
            try:
                from scripts.hans_entities import EntityStore
                _ent = EntityStore(config, diary_db_path).resolve(
                    subject, loose=True, etype="osoba")
            except Exception:
                _ent = None
            # HANS_ART_COMMON_NOUN_V1 — `_resolve_entity` při chybějící entitě
            # SÁM dohledá na Wikipedii a ULOŽÍ ji. U obecného slova tím do
            # paměti propašuje nesmysl („kočku" → „Kockums", švédská loděnice),
            # i když `_ground_subject` níž grounding správně přeskočí. Cesta
            # k podobě OSOBY beztak dává smysl jen u jmen → stejná brána.
            if not _ent and _name_shaped(subject) and _is_proper_name(subject, config):
                _ent = _resolve_entity(config, diary_db_path, subject)
            # HANS_ART_PERSON_WIKI_LOOKUP_V1 (4.8.) — osobu, kterou Hans JEŠTĚ
            # NEZNÁ, dohledej na Wikipedii a teprve pak rozhodni o podobě.
            # Bez tohohle měla cesta k podobě přísnější vstup než samotný
            # grounding (ten Wikipedii umí) → u neznámé/překlepnuté osoby se
            # PuLID vůbec nespustil a FLUX maloval jen podle jména.
            # Doloženo 4.8.: „Bud Spencer" (v paměti nebyl) i „TerRence Hill"
            # (překlep) skončily jako generický muž, ačkoli cs.wikipedia má
            # u obou článek s portrétem.
            if ((not _ent or _ent.get("etype") != "osoba")
                    and _name_shaped(subject)
                    and _is_proper_name(subject, config)):
                _wt = _wiki_capture_person(config, diary_db_path, subject)
                if _wt:
                    try:
                        from scripts.hans_entities import EntityStore as _ES2
                        _es2 = _ES2(config, diary_db_path)
                        # resolvuj podle OPRAVENÉHO titulu (překlep by minul),
                        # fallback na původní námět
                        _ent2 = (_es2.resolve(_wt, loose=True, etype="osoba")
                                 or _es2.resolve(subject, loose=True, etype="osoba"))
                        if _ent2:
                            _ent = _ent2
                    except Exception:
                        pass
            # HANS_ENTITY_POSTAVA_V1 (20.7.) — ZKUŠENO+ZAMÍTNUTO: rozšíření gate
            # na etype=='postava' (fiktivní postavy → img2img z Wiki obrázku)
            # dopadlo špatně — Wiki obrázek postavy je FOTKA HERCE (Rimmer→Chris
            # Barrie), ne postavy → podoba nesedí; Kryten (těžká maska) render
            # nedoběhl. Text-grounded je pro fikci lepší (aspoň doběhne).
            # Ponecháno jen `etype=='osoba'` (reálné osoby: Matka Tereza, kde
            # Wiki obrázek = ta osoba). etype='postava' klasifikace zůstává
            # (neškodná metadata), jen NEROUTUJE na img2img.
            if _ent and _ent.get("etype") == "osoba":
                _ref = _fetch_person_ref(config, _ent)
                if _ref:
                    _r = paint_person_from_photo(
                        config, diary_db_path, subject, _ref, style,
                        person_name=_ent.get("name", subject))
                    if _r:
                        return _r
                    _log.info("art: podoba osoby nevyšla → text-grounded malba")
        except Exception as _pe:
            _log.debug("art: person-likeness cesta selhala: %s", _pe)
    # HANS_ART_SUBJECT_GROUNDING_V1 — ukotvi námět (kdo/co to je) PŘED renderem
    grounded = _ground_subject(config, diary_db_path, subject)
    # HANS_ART_PLACE_LIKENESS_V1 — je-li námět MÍSTO, přemaluj jeho skutečnou
    # fotku (analogie person_likeness). Běží AŽ ZA groundingem schválně: ten
    # entitu vyhledá a uloží, takže tady stačí LEVNÉ čtení ze storu bez sítě
    # — a u obecného slova („kočku") se grounding přeskočí, takže se sem
    # nedostane ani tahle cesta (HANS_ART_COMMON_NOUN_V1 platí i pro místa).
    if (_acfg(config).get("place_likeness", {}) or {}).get("enabled", True):
        try:
            from scripts.hans_entities import EntityStore
            _pent = EntityStore(config, diary_db_path).resolve(
                subject, loose=True, etype="místo")
            if _pent:
                _pnm = _pent.get("name", subject)
                # Holý název = chceme VĚRNOST → fotka. Námět se scénou
                # („Trosky v bouři") by se překreslením fotky ztratil, proto
                # jde dál na text-grounded cestu. Táž úvaha jako
                # HANS_ART_PORTRAIT_IMG2IMG_V1 u osob, tatáž funkce.
                if _subject_beyond_name(subject, _pnm):
                    _log.info("art: místo '%s' nese scénu → text-grounded malba",
                              _pnm)
                else:
                    _pref = _fetch_person_ref(config, _pent)   # funkce je generická
                    if _pref:
                        _r = paint_place_from_photo(
                            config, diary_db_path, subject, _pref, style,
                            place_name=_pnm, grounded=grounded)
                        if _r:
                            return _r
                        _log.info("art: podoba místa nevyšla → text-grounded malba")
                    else:
                        _log.info("art: místo '%s' nemá na Wikipedii fotku "
                                  "→ text-grounded malba", _pnm)
        except Exception as _ple:
            _log.debug("art: place-likeness cesta selhala: %s", _ple)
    # scene_intro musí NÉST námět — dřív bylo "" → LLM dostal prázdný prompt
    # a maloval naslepo (např. „pan Sorge" → generický stařec).
    if style:
        # HANS_ART_STYLE_V4 — ukotvi i STYL (stejná kaskáda: entity/RAG/Wiki)
        style_desc = _ground_style(config, diary_db_path, style)
        scene_intro = (
            "Subject/theme to depict (described in Czech):\n%s\n\n"
            "Render it IN THIS ART STYLE (described in Czech):\n%s\n\n"
            "Create ONE English SDXL prompt of the subject rendered in that "
            "style; weave the style into composition, forms and palette, and "
            "end with strong English style keywords.\n\n" % (grounded, style_desc))
        _scene_sys = _STYLE_SCENE_SYSTEM
    else:
        scene_intro = (
            "Subject/theme to depict (described in Czech):\n%s\n\n"
            "Create ONE evocative artistic English SDXL prompt of THIS subject "
            "(fitting the person/thing described).\n\n" % grounded)
        _scene_sys = _SUBJECT_SCENE_SYSTEM
    res = _render_image(config, title, grounded, diary_db_path,
                        scene_system=_scene_sys, scene_intro=scene_intro,
                        cs_subject=subject)
    if not res:
        _log.warning('art: obraz na téma „%s" se nevyrenderoval', title)
        # HANS_ART_PROMISE_KEPT_V1 (6.8.) — uživateli už bylo řečeno „maluji"
        # (chat odpovídá HNED, render běží na pozadí). Když se render odloží,
        # nesmí to skončit jen řádkem v logu — jinak Hans slíbil obraz, který
        # nikdy nepřijde. Doloženo 15:06: Ollama timeout → render odložen,
        # uživatel čekal a `/stav` mezitím hlásil, že se nemaluje.
        # Fronta doručí zprávu Hansovým mostem (`HANS_NOTIFY_QUEUE_V1`).
        try:
            import json as _js
            import time as _tm
            with open("data/notify_queue.jsonl", "a", encoding="utf-8") as _q:
                _q.write(_js.dumps({
                    "text": ("Omlouvám se, pane — obraz na téma „%s\" se mi "
                             "teď nepodařilo namalovat (nedostal jsem se ke "
                             "svému mozku). Zkusím to znovu; kdyby to "
                             "spěchalo, řekněte a pustím se do toho hned."
                             % title),
                    # HANS_NOTIFY_DIRECT_V1 — uživatel na obraz ČEKÁ; tiché hodiny
                    # tuhle omluvu jednou odložily do 9:00 a slib zase visel.
                    "direct": True,
                    "ts": _tm.time()}, ensure_ascii=False) + "\n")
        except Exception as _ne:
            _log.debug("art: notifikace o odloženém renderu: %s", _ne)
        return None
    rel_path, prompt, vision_desc = res
    caption = _evaluate_artwork(config, diary_db_path, title, subject, vision_desc,
                                source_label="tím, oč jsem byl požádán")
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "subject",
                         "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log subject artwork failed: %s", e)
    _log.info('art: Hans namaloval na téma „%s" → %s', title, rel_path)
    return rel_path, caption


# ── HANS_PLACE_PAINT_V1 — Hans namaluje, jak si představuje svůj domov ───────
# Věrný režim: JEDNA realistická scéna OBÝVÁKU (kde Hans je) z konkrétních popisů
# fotek — ne celý byt (jeden obraz = jedna scéna), ne abstraktní shrnutí, fotostyl.
_HOME_SCENE_SYSTEM = (
    "You turn detailed descriptions of someone's real LIVING ROOM (from photos) "
    "into ONE concise English prompt for an SDXL image model. Output ONLY the "
    "prompt (no preamble, no quotes). Compose ONE coherent, REALISTIC wide interior "
    "view of the MAIN LIVING ROOM from the person's own vantage point. If an explicit "
    "LEFT / RIGHT / BACK layout is given, FOLLOW IT PRECISELY as the camera viewpoint "
    "(place each item on the correct side). FAITHFULLY reproduce the SPECIFIC "
    "furniture, COLORS and layout described — exact furniture colors, the wall color, "
    "the windows with their light, and the described furniture and arrangement. Do "
    "NOT invent extra rooms or a different style; do NOT depict adjacent rooms — "
    "only this one main living room. Realistic, photographic, true to the "
    "description. NO people, NO text, letters or words. End with: realistic interior "
    "photograph, wide angle, natural daylight, true to life, detailed, sharp focus."
)


def _home_source(db_path: str) -> str:
    """Zdrojový text pro CHAT/prozaické použití: preferuj syntetizovaný 'home_model',
    fallback = spojené pohledy z fotek + fakta. '' když nic."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content FROM place_facts WHERE category='home_model' "
            "ORDER BY updated_ts DESC LIMIT 1").fetchone()
        if row and (row["content"] or "").strip():
            conn.close()
            return row["content"].strip()
        rows = conn.execute(
            "SELECT category, content FROM place_facts "
            "WHERE category != 'home_model' ORDER BY category, id").fetchall()
        conn.close()
        parts = [r["content"].strip() for r in rows if (r["content"] or "").strip()]
        return "\n".join(parts)
    except Exception as e:
        _log.warning("art: _home_source selhal: %s", e)
        return ""


def _home_paint_source(db_path: str) -> str:
    """Zdroj pro VĚRNÝ render. PRIORITA = autoritativní fakta od uživatele
    (room/layout/window/door/neighbor/note — přesné rozložení a barvy); fotky
    (mental_map) doplní vizuální detail. Fallback home_model."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        urows = conn.execute(
            "SELECT content FROM place_facts WHERE category IN "
            "('room','layout','window','door','neighbor','note') ORDER BY category, id"
        ).fetchall()
        mrows = conn.execute(
            "SELECT content FROM place_facts WHERE category='mental_map' ORDER BY id"
        ).fetchall()
        conn.close()
        user = [(r["content"] or "").strip() for r in urows if (r["content"] or "").strip()]
        views = [(r["content"] or "").strip() for r in mrows if (r["content"] or "").strip()]
        parts = []
        if user:
            parts.append("AUTHORITATIVE layout of my living room, from my vantage "
                         "point — follow this precisely (Czech):\n- " + "\n- ".join(user))
        if views:
            parts.append("Extra visual detail from photos (colors, objects):\n- "
                         + "\n- ".join(views))
        if parts:
            return "\n\n".join(parts)
    except Exception as e:
        _log.warning("art: _home_paint_source selhal: %s", e)
    return _home_source(db_path)


def paint_home(config: dict, diary_db_path: str) -> Optional[tuple]:
    """Hans namaluje svůj obývák VĚRNĚ podle fotek (konkrétní popisy z mental_map,
    jedna realistická scéna). Loguje do galerie jako 'home'.
    Vrací (rel_path, caption) nebo None. Nikdy nehází."""
    text = _home_paint_source(diary_db_path)
    if not text:
        _log.warning("art: žádný model domova (place_facts prázdné) — skip")
        return None
    title = "Můj domov"
    scene_intro = "%s\n\n" % text
    res = _render_image(config, title, text, diary_db_path,
                        en_fallback="a quiet home interior seen from within a "
                        "room, view from where one stands, soft natural light",
                        scene_system=_HOME_SCENE_SYSTEM, scene_intro=scene_intro)
    if not res:
        _log.warning("art: domov se nevyrenderoval — retry příště")
        return None
    rel_path, prompt, vision_desc = res
    caption = _evaluate_artwork(config, diary_db_path, title, text, vision_desc,
                                source_label="svým domovem, jak si ho představuje")
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "home",
                         "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log home artwork failed: %s", e)
    _log.info("art: Hans namaloval svůj domov → %s", rel_path)
    return rel_path, caption


def render_home_now(config: dict, diary_db_path: str) -> Optional[tuple]:
    """Tenký veřejný wrapper pro chat (/misto obraz) — IMAGINOVANÝ z textu."""
    return paint_home(config, diary_db_path)


# ── img2img: přemaluj REÁLNOU fotku Hansova pohledu do uměleckého stylu ──────
# Nejvěrnější varianta — kompozice/rozložení zůstane z fotky, SDXL jen přidá styl.
_HOME_STYLE_PROMPT = (
    "a cozy living room interior, the same scene and layout, warm artistic oil "
    "painting, painterly brushwork, soft natural daylight, warm inviting "
    "atmosphere, rich texture, fine art, masterful"
)
_PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _pick_home_photo(config: dict) -> str:
    """Vyber reprezentativní fotku obýváku z drop-folderu — preferuj 'hansuv pohled'
    (široký záběr z Hansova místa), jinak první."""
    pd = (config.get("place", {}) or {}).get("photo_dir") or os.path.join("data", "room_photos")
    if not os.path.isdir(pd):
        return ""
    files = [f for f in sorted(os.listdir(pd)) if f.lower().endswith(_PHOTO_EXT)]
    for key in ("hansuv pohled", "pohled"):
        for f in files:
            if key in f.lower():
                return os.path.join(pd, f)
    return os.path.join(pd, files[0]) if files else ""


def _resize_to_temp(path: str, max_side: int = 1024) -> Optional[str]:
    """Zmenši fotku na ~max_side (SDXL nativní) a ulož do /tmp PNG pro upload."""
    try:
        import cv2
        img = cv2.imread(path)
        if img is None:
            return None
        h, w = img.shape[:2]
        s = max_side / float(max(h, w))
        if s < 1.0:
            img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
        out = os.path.join("/tmp", "hans_home_%d.png" % (int(time.time())))
        cv2.imwrite(out, img)
        return out
    except Exception as e:
        _log.warning("art: resize fotky selhal: %s", e)
        return None


def paint_home_from_photo(config: dict, diary_db_path: str,
                          photo_path: str = "", denoise: float = None) -> Optional[tuple]:
    """Přemaluj REÁLNOU fotku Hansova pohledu na pokoj do uměleckého stylu (img2img,
    nízký denoise → kompozice zůstane z fotky). Nejvěrnější varianta. Loguje do
    galerie jako 'home_photo'. Vrací (rel_path, caption) nebo None. Nikdy nehází."""
    try:  # OLLAMA_GAME_MODE_V1 — nezabírat VRAM za hry
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _log.info("art: herní mód — img2img render odložen")
            return None
    except Exception:
        pass
    ckpt = _ckpt(config)
    if not ckpt:
        _log.warning("art: image_model nenastaven — skip")
        return None
    base = _comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception as e:
        _log.warning("art: ComfyUI nedostupný (%s) — odloženo", e)
        return None
    photo = photo_path or _pick_home_photo(config)
    if not photo or not os.path.exists(photo):
        _log.warning("art: žádná fotka pokoje v drop-folderu — skip")
        return None
    tmp = _resize_to_temp(photo)
    if not tmp:
        return None
    img_name = _comfy_upload_image(base, tmp)
    try:
        os.remove(tmp)
    except Exception:
        pass
    if not img_name:
        _log.warning("art: upload fotky do ComfyUI selhal")
        return None

    acfg = _acfg(config)
    pcfg = (config.get("place", {}) or {}).get("paint", {}) or {}
    dn = float(denoise if denoise is not None else pcfg.get("denoise", 0.5))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2**31)
    client_id = uuid.uuid4().hex
    os.makedirs(ART_DIR, exist_ok=True)
    fname = "%d_muj_domov_foto.png" % int(time.time())
    dest = os.path.join(ART_DIR, fname)

    if not _comfy_ready(config):
        return None
    loaded = _ollama_loaded(config)
    _ollama_unload(config, loaded)
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        wf = _comfy_workflow_img2img(ckpt, _HOME_STYLE_PROMPT, seed, img_name,
                                     dn, steps, cfg_s)
        _log.info("art: home img2img start (denoise %.2f, %d steps) z %s",
                  dn, steps, os.path.basename(photo))
        pid = _comfy_submit(base, wf, client_id)
        if pid:
            hist = _comfy_wait(base, pid, timeout=rtimeout)
            img = _first_image(hist) if hist else None
            if img and _comfy_fetch_image(base, img, dest):
                ok = True
            elif not hist:
                _log.warning("art: home img2img vypršel (timeout %ds)", rtimeout)
            else:
                _log.warning("art: home img2img — bez obrázku")
        else:
            _log.warning("art: home img2img submit selhal")
    except Exception as e:
        _log.warning("art: home img2img selhal: %s", e)
    finally:
        _comfy_free(config)
        if ok:
            vision_desc = _describe_render(config, dest)
        _ollama_warm(config, config.get("models", {}).get("dialog", "hans-czech:latest"))

    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    caption = _evaluate_artwork(config, diary_db_path, "Můj domov",
                                "Přemaloval jsem svůj pokoj z vlastního pohledu.",
                                vision_desc,
                                source_label="svým pokojem, jak ho vidí a přemaloval")
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", "Můj domov", caption,
             json.dumps({"path": rel_path, "prompt": _HOME_STYLE_PROMPT,
                         "source": "home_photo", "denoise": dn,
                         "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log home_photo artwork failed: %s", e)
    _log.info("art: Hans přemaloval svůj pokoj z fotky → %s", rel_path)
    return rel_path, caption


def render_home_photo_now(config: dict, diary_db_path: str) -> Optional[tuple]:
    """Veřejný wrapper pro chat — VĚRNÝ přemalovaný pokoj z reálné fotky (img2img)."""
    return paint_home_from_photo(config, diary_db_path)


# ── Hlavní entry (noční) ────────────────────────────────────────────────────
def generate_pending_artwork(config: dict, diary_db_path: str) -> bool:
    """Vyrenderuje obraz pro 1 dočtenou knihu bez obrazu. Vrací True při úspěchu.
    Deferral-safe — nikdy nehází, při nedostupnosti vrátí False (retry příště)."""
    if not _acfg(config).get("enabled", True):
        return False
    _ensure_schema(diary_db_path)
    book = _pending_book(diary_db_path)
    if not book:
        return False  # nic k namalování

    title = book["title"]
    reflection = _source_text(diary_db_path, title)
    res = _render_image(config, title, reflection, diary_db_path)
    if not res:
        _log.warning('art: obraz pro „%s" se nevyrenderoval — retry příště', title)
        return False
    rel_path, prompt, vision_desc = res
    caption = _evaluate_artwork(config, diary_db_path, title, reflection, vision_desc)
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    _log_artwork(diary_db_path, title, caption, rel_path, prompt)
    _mark_done(diary_db_path, book["book_id"])
    _log.info('art: obraz hotov pro „%s" → %s', title, rel_path)
    return True


# ── HANS_DREAMS_V1 — Hans z vlastního popudu namaluje svůj sen ───────────────
def _last_dream_painting_ts(db_path: str) -> float:
    """Kdy Hans naposledy namaloval sen (throttle). 0.0 = nikdy."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        rows = con.execute(
            "SELECT data FROM diary WHERE event_type='artwork' "
            "AND data LIKE '%\"source\": \"dream\"%' ORDER BY ts DESC LIMIT 1").fetchall()
        con.close()
        for (d,) in rows:
            try:
                return float(json.loads(d).get("painted_ts", 0)) or 0.0
            except Exception:
                pass
    except Exception as e:
        _log.debug("art: last_dream_painting_ts failed: %s", e)
    return 0.0


def _recent_unpainted_dream(db_path: str, days: int = 4) -> Optional[dict]:
    """Nejnovější sen (deník event_type='dream') za posledních `days` dní, který
    Hans ještě nenamaloval (jeho ts není v žádném artwork.data.dream_ts)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        painted = set()
        for (d,) in con.execute(
                "SELECT data FROM diary WHERE event_type='artwork' "
                "AND data LIKE '%\"dream_ts\"%'").fetchall():
            try:
                dt = json.loads(d).get("dream_ts")
                if dt:
                    painted.add(int(dt))
            except Exception:
                pass
        cutoff = time.time() - days * 86400
        rows = con.execute(
            "SELECT ts, COALESCE(NULLIF(note,''), data) FROM diary "
            "WHERE event_type='dream' AND ts>=? "
            "AND COALESCE(NULLIF(note,''), data) IS NOT NULL "
            "ORDER BY ts DESC LIMIT 12", (cutoff,)).fetchall()
        con.close()
        for ts, text in rows:
            if int(ts) not in painted and text and len(text.strip()) > 15:
                return {"ts": float(ts), "text": text.strip()}
    except Exception as e:
        _log.debug("art: recent_unpainted_dream failed: %s", e)
    return None


def paint_dream(config: dict, diary_db_path: str) -> bool:
    """Sebeřízená tvorba: Hans namaluje obraz ke svému nedávnému snu (groundovaný
    v jeho dni). Throttle (min_interval_days) → zřídka. Vrací True při úspěchu.
    Deferral-safe — nikdy nehází, retry příště. Loguje do galerie jako 'dream'."""
    acfg = _acfg(config)
    dcfg = acfg.get("dreams", {}) or {}
    if not dcfg.get("enabled", True):
        return False
    # HANS_DREAMS_PER_DREAM_V1 — maluj KAŽDÝ nový sen (idempotence dle dream_ts
    # zajistí 1×/sen); krátký odstup jen utlumí duplicitní sny z téže noci.
    interval_h = float(dcfg.get("min_interval_hours", 8))
    last = _last_dream_painting_ts(diary_db_path)
    if last and (time.time() - last) < interval_h * 3600:
        return False  # krátký odstup proti duplicitám téže noci
    dream = _recent_unpainted_dream(diary_db_path, int(dcfg.get("max_age_days", 4)))
    if not dream:
        return False  # nic čerstvého k namalování

    text = dream["text"]
    title = "Sen"
    scene_intro = "A dream (described in Czech):\n%s\n\n" % text
    res = _render_image(config, title, text, diary_db_path,
                        en_fallback="a surreal, dreamlike scene, soft and atmospheric",
                        scene_system=_DREAM_SCENE_SYSTEM, scene_intro=scene_intro,
                        series="dream")
    if not res:
        _log.warning("art: sen se nevyrenderoval — retry příště")
        return False
    rel_path, prompt, vision_desc = res
    caption = _evaluate_artwork(config, diary_db_path, title, text, vision_desc,
                                source_label="svým snem z minulé noci")
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    # zápis do galerie s označením 'dream' + odkaz na zdrojový sen (idempotence)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "dream",
                         "dream_ts": int(dream["ts"]), "painted_ts": time.time(),
                         "dream": text[:300]}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log dream artwork failed: %s", e)
    _log.info('art: Hans namaloval svůj sen → %s', rel_path)
    return True


# ── HANS_DAY_PAINTING_V1 — Hans namaluje svůj den / náladu ───────────────────
_DAY_EVENT_TYPES = ("reading_takeaway", "movie_opinion", "introspection",
                    "web_read", "room_description", "case_opened", "case_closed",
                    "book_reflection", "dialog_reflection", "spontaneous")


def _day_mood(config: dict) -> str:
    """Převažující nálada DNE (vážená dobou strávenou v každé náladě), ne okamžitá —
    nálada poskakuje, tak bereme, kde Hans strávil nejvíc času. Z system.logu.
    Vrací dominantní náladu (+ druhou, když je den proměnlivý), '' když nic."""
    import re
    from datetime import datetime
    path = (config.get("logging", {}) or {}).get("file", "data/system.log")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400000))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    seq = []  # (epoch, new_mood) dnešní přechody
    for ln in tail.splitlines():
        if today not in ln or "hans_mood: Mood:" not in ln:
            continue
        m = re.search(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Mood: \S+ → (\S+)", ln)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                seq.append((ts, m.group(2)))
            except Exception:
                pass
    if not seq:
        return ""
    seq.sort()
    now = time.time()
    dur = {}
    for i, (ts, mood) in enumerate(seq):
        end = seq[i + 1][0] if i + 1 < len(seq) else now
        dur[mood] = dur.get(mood, 0.0) + max(0.0, end - ts)
    ranked = sorted(dur.items(), key=lambda x: -x[1])
    dom = ranked[0][0]
    # proměnlivý den: druhá nálada má aspoň 60 % času té první → zmiň obě
    if len(ranked) > 1 and ranked[1][1] >= 0.6 * ranked[0][1]:
        return "%s (s přechody do %s)" % (dom, ranked[1][0])
    return dom


def _day_fragments(db_path: str) -> str:
    """Salientní dnešní zážitky z deníku (bez perceptuálního šumu) pro grounding."""
    bits = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        ph = ",".join("?" * len(_DAY_EVENT_TYPES))
        rows = con.execute(
            "SELECT title, COALESCE(NULLIF(note,''), data) FROM diary "
            "WHERE date(ts,'unixepoch','localtime')=date('now','localtime') "
            "AND event_type IN (%s) "
            "AND COALESCE(NULLIF(note,''), data)<>'' "
            # HANS_SPONTANEOUS_TEMPLATE_MARK_V1/V2 (27.8.; V2 kotví na začátek
            # pole — `%"template"%` kdekoli by tiše zahodilo článek,
            # který o šablonách jen píše) — obraz dne se nesmí
            # opírat o šablonu. ⚠️ `%%` je nutné: celý řetězec jde přes `% ph`.
            "AND COALESCE(data,'') NOT LIKE '{\"template\":%%' "
            "ORDER BY COALESCE(importance,0) DESC, RANDOM() LIMIT 6" % ph,
            _DAY_EVENT_TYPES).fetchall()
        con.close()
        for t, n in rows:
            frag = (t or "").strip()
            if n:
                frag = (frag + ": " + n.strip()) if frag else n.strip()
            if frag:
                bits.append("- " + frag[:120])
    except Exception as e:
        _log.debug("art: day_fragments failed: %s", e)
    return "\n".join(bits[:6])


def _last_day_painting_ts(db_path: str) -> float:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT data FROM diary WHERE event_type='artwork' "
            "AND data LIKE '%\"source\": \"day\"%' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return float(json.loads(row[0]).get("painted_ts", 0)) or 0.0
    except Exception as e:
        _log.debug("art: last_day_painting_ts failed: %s", e)
    return 0.0


def _last_home_painting_ts(db_path: str) -> float:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT data FROM diary WHERE event_type='artwork' "
            "AND data LIKE '%\"source\": \"home%' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return float(json.loads(row[0]).get("painted_ts", 0)) or 0.0
    except Exception as e:
        _log.debug("art: last_home_painting_ts failed: %s", e)
    return 0.0


def paint_day(config: dict, diary_db_path: str) -> bool:
    """Sebeřízená tvorba: Hans namaluje obraz vystihující svůj DEN a NÁLADU
    (symbolická atmosférická scéna). Deferral-safe. Galerie 'day'."""
    acfg = _acfg(config)
    pcfg = acfg.get("day_painting", {}) or {}
    if not pcfg.get("enabled", True):
        return False
    frags = _day_fragments(diary_db_path)
    if not frags or len(frags) < 30:
        return False  # málo materiálu na den
    mood = _day_mood(config)
    title = "Můj den"
    # HANS_DAY_MOOD_VISUAL_V1 — náladu rozepiš na vizuální atmosféru a dej ji NAHORU,
    # ať dominuje (jinak ji den/SDXL přebijou do hezkého klidu).
    base = mood.split(" (")[0] if mood else ""
    vis = _MOOD_VISUAL.get(base, "")
    mood_block = ("DOMINANT MOOD: %s — %s\n\n" % (mood, vis)) if vis else (
        ("Mood: %s\n\n" % mood) if mood else "")
    source = mood_block + "Today's moments (secondary motifs):\n" + frags
    scene_intro = "A person's day and mood:\n%s\n\n" % source
    res = _render_image(config, title, source, diary_db_path,
                        en_fallback="a quiet everyday scene from domestic life",
                        scene_system=_DAY_SCENE_SYSTEM, scene_intro=scene_intro,
                        series="day")
    if not res:
        _log.warning("art: obraz dne se nevyrenderoval — retry příště")
        return False
    rel_path, prompt, vision_desc = res
    caption = _evaluate_artwork(config, diary_db_path, title, source, vision_desc,
                                source_label="svým dnešním dnem")
    _derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "day",
                         "mood": mood, "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log day artwork failed: %s", e)
    _log.info('art: Hans namaloval svůj den → %s', rel_path)
    return True


# ── Ruční test: python3 -m scripts.hans_art [název knihy] ────────────────────
# Vyrenderuje vzorek pro aktuálně čtenou (nebo zadanou) knihu BEZ značení done
# a BEZ zápisu do deníku → lze pouštět opakovaně a vidět, co vzniká.
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cfg = json.load(open("config.json"))
    DB = "data/hans_diary.db"
    _title = " ".join(sys.argv[1:]).strip()
    if not _title:
        try:
            con = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
            row = con.execute(
                "SELECT book_title FROM hans_library WHERE status IN ('reading','finished') "
                "ORDER BY (status='reading') DESC, started_at DESC LIMIT 1").fetchone()
            con.close()
            _title = (row[0] if row else "") or "kniha"
        except Exception:
            _title = "kniha"
    print(f"[test] renderuji vzorek pro knihu: {_title!r}")
    _refl = _source_text(DB, _title)
    print(f"[test] zdroj reflexe: {len(_refl)} zn")
    _res = _render_image(cfg, _title, _refl, DB)
    if _res:
        _path, _prompt, _vis = _res
        print(f"[test] HOTOVO → {_path}\n[test] prompt: {_prompt}")
        print(f"[test] vize (llava): {_vis or '(nedostupná)'}")
        _verdict = _evaluate_artwork(cfg, DB, _title, _refl, _vis)
        print(f"[test] Hansův verdikt: {_verdict}")
        _lesson = _derive_art_lesson(cfg, DB, _title, _vis, _verdict, store=False)
        print(f"[test] Ponaučení pro příště (neuloženo): {_lesson or '(žádné)'}")
    else:
        print("[test] render se nezdařil (ComfyUI dole / image_model? viz log)")
