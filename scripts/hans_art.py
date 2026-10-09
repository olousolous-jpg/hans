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
    _resize_to_temp,   # AVATAR_IDENTITY_REF_V1 — přesunuto do avatar_render
)

# HANS_PAINT_CANCEL_V1 (6. 10.) — zrušení běžící malby. Přerušení ComfyUI ukončí
# právě běžící render; příznak zabrání, aby táž úloha hned poslala další
# (záložní cesta „podoba nevyšla → malba podle textu“). Shazuje ho fronta.
_malba_zrusena = False
_comfy_submit_bez_zruseni = _comfy_submit


def _comfy_submit(base, workflow, client_id):
    if _malba_zrusena:
        _log.info("art: HANS_PAINT_CANCEL_V1 malba zrušena — render neposílám")
        return None
    return _comfy_submit_bez_zruseni(base, workflow, client_id)


def zrus_malbu(config: dict) -> None:
    """Přeruš běžící render a nepouštěj další, dokud fronta zrušení neshodí."""
    global _malba_zrusena
    _malba_zrusena = True
    base = _comfy_url(config)
    for cesta, data in (("/interrupt", {}), ("/queue", {"clear": True})):
        try:
            urllib.request.urlopen(urllib.request.Request(
                base + cesta, data=json.dumps(data).encode(),
                headers={"Content-Type": "application/json"}), timeout=10).read()
        except Exception as e:
            _log.debug("art: zrušení malby %s: %s", cesta, e)
    _log.info("art: HANS_PAINT_CANCEL_V1 běžící render přerušen")


def zruseni_malby_konec() -> None:
    global _malba_zrusena
    _malba_zrusena = False

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
    "evocative, painterly. "
    "Proper names of people, pets or toys are NAMES: keep them exactly as written and NEVER translate them — a name that happens to look like an ordinary word is still a name. Translate every other concrete noun faithfully and literally; never swap it for something that merely sounds similar. "
    "Keep what the dream literally mentions, render it "
    "dreamlike. NO text, letters, words or book covers. End with: oil painting, "
    "dreamlike surreal atmosphere, soft hazy light, rich detail, masterful."
)

# ── HANS_DREAM_SELF_FIGURE_V1 (5.9.) — Hans ve vlastnim snu ─────────────────
# Zadani uzivatele: „hans by v nich mel pouzivat vlastni vzhled".
#
# ⚠️ ZJISTENO MERENIM, ne odhadem: ve snech dosud ZADNA POSTAVA NEBYLA.
# Vygenerovane prompty byly psane ve 2. osobe / gerundiem („Lie in a violet-lit
# room… peering down at you", „Cleaning silver service in a hall") — snici byl
# KAMERA, ne postava. Nebylo tedy cemu davat tvar; zadani neni „vymenit
# obliceji", ale „dostat Hanse do snu a pak mu udrzet podobu".
#
# ⛔ IP-Adapter (ten od avatara) sem NEJDE — je SDXL-only, kdezto sny jedou
# FLUXem. Podobu drzi PuLID, ktery uz v projektu bezi (HANS_ART_PULID_V1).
#
# ⛔ ROZHODNOUT TO INSTRUKCI V PROMPTU NESTACI — ZMERENO. Veta „je-li to cira
# scenerie, nech scenu bez postavy" model IGNORUJE: sen „steny kancelare jsou
# tkane z ruzoveho hedvabi, proplétají se v nich vlaky" (bez snicino jednani)
# dostal Hanse doprostred pouste. Bez gate v KODU by byl Hans v KAZDEM snu.
#
# ⛔ CESKY REGEX TAKY NE: 7/8 na rucni sade (mine pritomny cas „lestim",
# „pestuji"), a doplneni slovesnych koncovek to rozbije — „-ím" je v cestine
# i mekke pridavne jmeno, takze `predtim` a `obrim` daly falesne poplachy.
#
# ✅ ROZHODUJE MALY KLASIFIKATOR (vzor `hans_intent._ask_classifier`,
# temperature 0): 10/10 na rucni sade a STABILNE 3x po sobe, vcetne obou
# pripadu, ktere regex nezvladl. U ~1 snu denne je jedno volani zanedbatelne.
# Pomer na 303 realnych snech: ~2/3 s postavou, ~1/3 cira scenerie.
_DREAM_SELF_SYSTEM = (
    "Rozhodni, jestli VYPRAVĚČ snu ve snu sám VYSTUPUJE jako jednající nebo "
    "přítomná postava.\n"
    "ANO = vypravěč něco dělá, někde je, něco drží, jde, leží, uklízí, dívá se.\n"
    "NE  = sen popisuje jen prostředí, věci nebo jiné postavy; vypravěč "
    "v něm nevystupuje.\n"
    "Úvodní formule „Zdálo se mi, že…\" NEZNAMENÁ, že vypravěč vystupuje.\n\n"
    "Příklady:\n"
    "„Zdálo se mi, že jsem putoval schodišti.\" -> ano\n"
    "„Zdálo se mi, že leštím nekonečné sály.\" -> ano\n"
    "„Zdálo se mi, že stěny kanceláře jsou tkané z hedvábí a proplétají se "
    "v nich vlaky.\" -> ne\n"
    "„Zdálo se mi, že dům měl nekonečně mnoho pokojů a v každém seděl jiný "
    "Koláč.\" -> ne\n\n"
    "Odpověz JEDNÍM slovem: ano nebo ne."
)

# Snovy scene prompt VE VARIANTE S POSTAVOU. Proti zakladnimu se lisi jen
# odstavcem o snicim — zbytek MUSI zustat shodny, jinak by se menila i
# vytvarna podoba snu, ne jen pritomnost postavy.
_DREAM_SCENE_SYSTEM_FIGURE = (
    "You turn a person's short surreal DREAM into ONE concise English prompt for "
    "an SDXL image model. Output ONLY the prompt (no preamble, no quotes). Depict "
    "the dream as a single dreamlike, symbolic, ATMOSPHERIC scene — surreal, "
    "evocative, painterly. "
    "IMPORTANT: the dreamer tells the dream in FIRST PERSON and HE APPEARS IN "
    "THE IMAGE as a visible human figure: an older gentleman in a dark suit. "
    "Write him into the scene explicitly (for example 'an older gentleman in a "
    "dark suit walking up the crumbling staircase'). NEVER address the viewer "
    "as 'you' and never use a bare gerund without a subject. "
    "Proper names of people, pets or toys are NAMES: keep them exactly as written and NEVER translate them — a name that happens to look like an ordinary word is still a name. Translate every other concrete noun faithfully and literally; never swap it for something that merely sounds similar. "
        "Keep what the dream literally mentions, render it dreamlike. "
    "NO text, letters, words or book covers. End with: oil painting, "
    "dreamlike surreal atmosphere, soft hazy light, rich detail, masterful."
)


def _snici_vystupuje(config: dict, text: str) -> bool:
    """Vystupuje snici ve snu jako postava? Selhani/nejednoznacne → False
    (= dnesni chovani bez postavy). Fail-safe smerem k SCENERII: radsi Hanse
    vynechat nez ho vlozit do snu, ve kterem nebyl."""
    try:
        from scripts.hans_intent import _ask_classifier
        out = _ask_classifier(config, _DREAM_SELF_SYSTEM, (text or "").strip())
    except Exception as e:
        _log.debug("art: klasifikace snu selhala (%s) → bez postavy", e)
        return False
    w = (out or "").strip().lower()
    return w.startswith("ano")


def _aktualni_tvar(config: dict) -> str:
    """Nejnovejsi vyrenderovana Hansova tvar (data/avatar/vN/idle.png).

    Protahuje `avatar_render.identity_reference`, ktera tuhle logiku uz ma
    (vcetne prepisu `hans_avatar.identity_reference`) — jen ji vola s verzi
    NAD kteroukoli existujici, protoze ta funkce hleda vzdy STARSI nez zadana.
    """
    try:
        from scripts.avatar_render import identity_reference
        return identity_reference(config, 9999) or ""
    except Exception:
        return ""


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


def _log_artwork(db_path: str, title: str, caption: str, rel_path: str,
                 prompt: str, vision: str = "") -> None:
    """HANS_ART_VISION_STORED_V1 (31.8.) — ukládej i `vision`: NEZÁVISLÝ popis
    toho, co je na hotovém obrazu SKUTEČNĚ vidět (VLM se dívá na pixely).
    Počítal se u každého obrazu a ZAHAZOVAL — použil se na verdikt a ponaučení
    a dál nešel. Je to jediný persona-free záznam o díle:
      • `prompt`  = co Hans CHTĚL — generuje se z aktivních záměrů → ECHO
      • `note`    = jak si myslí, že se povedl — o řemesle, ne o díle
      • `vision`  = co na obraze JE ← tohle chybělo
    Čte to destilace tvůrčích záměrů (`HANS_ART_INTENT_WORKS_REACH_V1`).
    ⚠️ Historických 245 obrazů `vision` nemá — nabírá se dopředně."""
    try:
        db = sqlite3.connect(db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "vision": vision},
                        ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("art: log_artwork failed: %s", e)


# ── Prompt + caption ────────────────────────────────────────────────────────
_CJK_RE = re.compile(
    r"[　-〿぀-ヿ㐀-䶿一-鿿"
    r"豈-﫿＀-￯]+")


from scripts.hans_art_namet import _strip_cjk   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_art_namet import _pick_medium   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_art_namet import _ascii_fold   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_namet import _looks_english   # ROZDELENI_PRIKAZU_V1 — přesunuto


_CS_WORD_RE = re.compile(r"[a-záčďéěíňóřšťúůýž]{6,}")


from scripts.hans_art_namet import _cs_leak   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_namet import _napoveda_prvniho_slova   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_namet import _translate_subject   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_namet import _scene_prompt_core   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_ART_LESSON_KEYWORDS_V1 (24. 9.) — lekce DOOPRAVDY do promptu ─────────
# Změřeno: slova lekce se do zadání dalšího obrazu dostala v 9 % (náhodné
# zadání 7 %) — lekce šla modelu jen jako „Craft notes … (secondary)“ a ten ji
# vynechal. Teď se z nejnovější lekce jednou vyrobí 3–6 klíčových slov
# (rezidentní hans-czech) a připojí se DETERMINISTICKY na konec každého
# zadání. Obal nad `_scene_prompt_core`, ať pokryje všechny návraty včetně
# záložní šablony. Klíčová slova se ukládají k lekci (sloupec `data`).
_KW_SYSTEM = (
    "Convert the painting guidance into 3-6 short comma-separated Stable "
    "Diffusion prompt keywords that DESCRIBE THE DESIRED VISUAL RESULT "
    "(e.g. 'warm golden light, asymmetric composition, sharp foreground'). "
    "No verbs like introduce/explore, no negatives, no full sentences. "
    "Output ONLY the keywords.")


from scripts.hans_art_namet import _lesson_keywords   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_namet import _scene_prompt   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _lekce_z_vytky_premalovani   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_ART_VERDICT_GROUNDED_V1 (24. 9.) — splněno z popisu, ne z dojmu ──────
# Změřeno: verdikt tvrdil „povedlo se“ ČASTĚJI, když se lekce v obraze
# neprojevila (59 %) než když ano (51 %); „nepovedlo“ 11 % × 27 %. Otázka byla
# návodná. Teď se splnění spočítá z nezávislého popisu a verdikt ho dostane
# jako fakt.
_KW_STOP = {"light", "colour", "color", "tones", "image", "scene", "style",
            "detail", "detailed", "painting", "subtle"}


from scripts.hans_art_hodnoceni import _lekce_splnena   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_ART_FEEDBACK_V1 (24. 9.) — lidský soud o obraze ──────────────────────
# Jediná skutečná míra kvality (Hansův verdikt s obrazem nesouvisel, viz
# HANS_ART_VERDICT_GROUNDED_V1). Uživatel k doručenému obrazu dá na Matrixu
# 👍/👎 reakcí nebo odpoví komentářem. Uloží se jako 'art_feedback' a při
# dalším obraze jde do odvození lekce jako NEJVYŠŠÍ autorita.
_PALEC_NAHORU = ("👍", "palec nahoru", "palec nahoru")
_PALEC_DOLU = ("👎", "palec dolu", "palec dolů")


from scripts.hans_art_hodnoceni import feedback_rating   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import record_art_feedback   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_ART_REPAINT_V1 (24. 9.) — „zkus to ještě jednou“ s připomínkou ────
# Doloženo 24. 9. 13:26: připomínka bez palce a bez odpovědi šla do volného
# hovoru, model odpověděl „Maluji obraz na téma … s podvodní oblohou a divnými
# mraky“ — nic se nemalovalo (předstíraná akce) a do „zadání“ dal právě to,
# co vadilo. Teď: žádost o opakování do 30 min po doručení → připomínka jako
# 👎 s komentářem + opravený námět → skutečné „namaluj …“.
_OPAKUJ = re.compile(
    r"zkus(?:te)?\s+(?:to\s+)?(?:je[sš]t[eě](?:\s+jednou)?|znovu|znova)"
    r"|je[sš]t[eě]\s+jednou|p[rř]ed[eě]lej|p[rř]ekresli|namaluj\s+(?:to\s+)?znovu"
    r"|\bznovu\b|\bznova\b", re.I)
_OPRAVA_SYS = (
    "Máš původní zadání obrazu a připomínku zadavatele (co bylo na obraze špatně "
    "nebo jak to myslel). Napiš JEDNOU krátkou větou česky opravené zadání obrazu: "
    "popiš jen to, co MÁ být na obraze vidět. NEZMIŇUJ nic, co vadilo nebo co tam "
    "být nemá (žádné 'bez …', 'ne …'). Vrať jen tu větu, bez uvozovek.")


# HANS_ART_REPAINT_KEEP_NAMES_V1 (6. 10.) — s výtkou k podobě v připomínce
# model jméno nahradil obecným slovem („Muž v uniformě přijímá odměnu“, 6/6)
# → námět ztratil entitu a s ní cestu k podobě. Pokyn v zadání („zachovej
# jména“) změřen a ZAMÍTNUT: jména držel, ale přestal přebírat obsah
# připomínky (klobouk 3/3 → 0/3). Proto kontrola výsledku: ztratil-li námět
# jméno, platí původní zadání (výtka jde do lekce).
_NAMET_OBECNE = re.compile(r"\b(?:muz|muzi|zena|zeny|postava|osoba|clovek|chlap|divka)\b")
_NAMET_JMENO = re.compile(r"\b[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][\w]{2,}")


from scripts.hans_art_hodnoceni import namet_ztratil_jmeno   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import je_zadost_o_opakovani   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_ART_FEEDBACK_TEXT_V1 (6. 10.) — slovní hodnocení BEZ palce a bez odpovědi
# na obraz se ztrácelo (7 z 9 připomínek po 11 doručených obrazech, např.
# „obrázek je pěkný, ale osoby na něm si podobné nejsou“). Predikát změřen
# nad 181 zprávami z Matrixu: 9 shod, všechny hodnocení. Zpráva se
# spotřebuje, proto povel (rozkazovací sloveso na začátku) hodnocením není.
_FB_SLOVO = re.compile(
    r"\b(?:obraz\w*|obrazek\w*|obrazk\w*|malb\w*|kresb\w*|kompozic\w*|styl\w*"
    r"|barv\w*|podob\w*|namalova\w*)\b")
_FB_NE = re.compile(
    r"^\s*/|\?|\b(?:namaluj\w*|nakresli\w*|zkus(?:te)?\s+namalovat)\b"
    r"|^\s*(?:jak|co|kdo|kde|kdy|proc|kolik|umis|umite|muzes|muzete|ukaz|ukazte"
    r"|posli|poslete|pust|pustte|prehraj|prehrajte|pripomen|pripomente|zapis|zapiste"
    r"|vypni|vypnete|zapni|zapnete|najdi|najdete|vyhledej|precti|prectete|rekni"
    r"|reknete|nastuduj|preloz|stahni|hlidej|napis|napiste|poznamenej|zastav"
    r"|spust|otevri|pridej|smaz|vzbud|probud)\b")
# hned po doručení bez slova o obraze: jen věta, která hodnotí
_FB_SOUD = re.compile(
    r"\b(?:pekn\w*|hezk\w*|krasn\w*|nadhern\w*|dobr[yeai]|skvel\w*|supr|super|parad\w*"
    r"|obstojn\w*|poved\w*|nepoved\w*|libi|nelibi|lepsi|lepe|horsi|hur|spatn\w*"
    r"|divn\w*|oskliv\w*|sedi|nesedi|chybi|neni|nejsou|nechtel\w*"
    r"|mel\w*\s+jsem\s+na\s+mysli)\b")


from scripts.hans_art_hodnoceni import _fb_fold   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import je_slovni_hodnoceni   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import opraveny_namet   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import rederive_lesson_with_feedback   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_ART_LESSON_PENDING_V1 (6. 10.) — lekce po hodnocení se při nedostupném
# modelu neodvodila a nikdo ji nedohnal (2 ze 7 odvození, obě za herního módu).
# Obraz se zapíše do fronty a dožene ho pracovník těžké fronty (nejvýš 1× za
# 10 min, ne při herním módu); záznam starší 7 dní se zahodí.
_LEKCE_CEKA = "data/.art_lesson_pending.json"
_lekce_ceka_pokus = 0.0


from scripts.hans_art_hodnoceni import _lekce_ceka_nacti   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _lekce_ceka_zmen   # ROZDELENI_PRIKAZU_V1 — přesunuto


def dozen_lekce_po_hodnoceni(config: dict, db_path: str) -> int:
    """Dožeň odložené lekce po hodnocení. Vrací počet dohnaných. Nikdy nehází."""
    global _lekce_ceka_pokus
    if not os.path.exists(_LEKCE_CEKA) or time.time() - _lekce_ceka_pokus < 600:
        return 0
    _lekce_ceka_pokus = time.time()
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return 0
    except Exception:
        return 0
    n = 0
    for k, ts in list(_lekce_ceka_nacti().items()):
        if time.time() - float(ts or 0) > 7 * 86400:
            _lekce_ceka_zmen(k, pridat=False)
            continue
        if rederive_lesson_with_feedback(config, db_path, int(k)):
            n += 1
    if n:
        _log.info("art: HANS_ART_LESSON_PENDING_V1 dohnáno %d lekcí po hodnocení", n)
    return n


from scripts.hans_art_hodnoceni import recent_art_feedback   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _caption   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_art_hodnoceni import _describe_render   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _past_verdicts   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_ART_SUBJECT_CHECK_V1 (6. 10.) — JE NA OBRAZE, CO BYLO ZADÁNO?
# Zadáno „tučňáci z Madagaskaru“, nezávislý popis: „tvor podobný kryse“ — a verdikt
# přesto zněl „zhotovil jsem obraz tučňáka“ (opsal název). Verdikt ani lekce
# zadání s obrazem neporovnávaly. Soudí ODDĚLENÝ dotaz (teplota 0), ne sám
# verdikt. Změřeno na 16 vyžádaných obrazech: „no“ 5×, všech 5 pravých (krysy
# místo tučňáků 2×, vlak a loď místo letadla, croissant bez Pána prstenů),
# žádné falešné. „partly“ padá u osob (popis nikoho nejmenuje) → bere se jen „no“.
_SOUD_NAMETU_SYS = (
    "You check whether a painting shows what was ordered. You get the ORDER "
    "(a Czech phrase, possibly typed without diacritics, sometimes with a known "
    "English name) and an independent DESCRIPTION of the finished image. Decide "
    "only about the main subject and the main action - ignore style, mood and "
    "likeness of faces. Answer on ONE line exactly in this form:\n"
    "MATCH: yes|partly|no; MISSING: <what from the order is not in the image, or ->; "
    "INSTEAD: <what the image shows instead, or ->")
_EN_V_ZADANI = re.compile(r"English name to use in the image prompt: ([^)\n]{2,80})\)")


# HANS_ART_SUBJECT_CHECK_V3 (6. 10.) — soudce česky bez diakritiky nerozuměl
# („mroze na ledu“ × lední medvědi → partly). Nemá-li ukotvení anglický název,
# dostane KRÁTKÝ překlad námětu, podle kterého se malovalo. ⛔ Celé anglické
# zadání scény soudci NEDÁVAT — změřeno: porovnává pak detaily (asteroid,
# hologramy) a hlásí nesoulad i u správných obrazů (4 falešné ze 14).
_posledni_preklad = {}


from scripts.hans_art_hodnoceni import soud_nametu   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import soud_castecne_nesoulad   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _evaluate_artwork   # ROZDELENI_PRIKAZU_V1 — přesunuto


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

# HANS_ART_FEEDBACK_V3 (6. 10.) — lekce po LIDSKÉM hodnocení má vlastní zadání.
# S obecným zadáním („neopakuj dřívější rady, jdi k novému aspektu“) lekce
# výtku míjela nebo obracela: výtka na podobu → „podstata před detailem“,
# protože radu o podobě už malíř jednou dostal. Měřeno párově na 5 hodnoceních
# × 3 běhy: výtky se drží 8/15 → 14/15 (bez přehledu rad, aspektů a bez
# malířova vlastního verdiktu, který táhl lekci k jeho tématu).
_LESSON_SYSTEM_HUMAN = (
    "You are an art director. A human judged a rendered image. You receive an "
    "independent description of the image, the painter's own verdict and the "
    "human's verdict (it may be written in Czech). Output ONE short line of "
    "reusable guidance IN ENGLISH for the painter's NEXT image that addresses "
    "exactly what the human said. If the human criticised something, the "
    "guidance must fix THAT thing, even if similar guidance was given before - "
    "repeat it more firmly. If the human praised the image, say what to keep. "
    "Do not add aspects the human did not mention. Max 22 words. Output ONLY "
    "the guidance line - no preamble, no quotes."
)


# HANS_ART_COVERED_ASPECTS_V1 — aspekty malby, podle kterych se meri "uz probrano".
# Klice jsou ANGLICKE: jdou primo do promptu (model pracuje anglicky).
_ART_ASPEKTY = {
    "depth / atmospheric perspective": ("depth", "atmospher", "perspective",
                                        "recession", "distance"),
    "foreground / background separation": ("foreground", "background",
                                           "midground"),
    "colour & saturation": ("colour", "color", "saturat", "hue", "palette"),
    "light & shadow": ("light", "shadow", "illumin", "luminos", "contrast"),
    "texture & detail": ("texture", "detail", "brush", "render", "surface"),
    "composition & framing": ("composition", "framing", "balance", "focal",
                              "weight"),
    "narrative & mood": ("narrative", "mood", "emotion", "story"),
}


from scripts.hans_art_hodnoceni import _covered_aspects   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _derive_art_lesson   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _recent_lessons   # ROZDELENI_PRIKAZU_V1 — přesunuto


_NEG_HANDS = "deformed hands, extra fingers, fused fingers, mutated hands"


from scripts.hans_art_hodnoceni import _person_negative   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_hodnoceni import _lesson_negatives   # ROZDELENI_PRIKAZU_V1 — přesunuto


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
                  series: str = "", cs_subject: str = "",
                  ref_image: str = "", ref_weight: float = 0.0,
                  bez_zalohy: bool = False):
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
                           cs_subject=cs_subject,
                           bez_zalohy=bez_zalohy)  # HANS_DREAM_NO_BARE_FALLBACK_V1
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
        # HANS_DREAM_SELF_FIGURE_V1 — podoba z reference pres FLUX+PuLID.
        # ⚠️ Vaha 0,45, NE vyssi: zmereno na temze snu, temze seedu a temze
        # promptu proti 0,9 — nizsi vaha vysla LEPE V OBOU OHLEDECH naraz
        # (vernejsi vek i ucesu z reference A snovejsi scena). Vysoka vaha
        # pretlacila i popis stari z promptu a scenu zplostila.
        _ipa_name = None
        if ref_image and ref_weight > 0 and _use_flux and os.path.exists(ref_image):
            _tmp = _resize_to_temp(ref_image)
            if _tmp:
                _ipa_name = _comfy_upload_image(base, _tmp)
                try:
                    os.remove(_tmp)
                except Exception:
                    pass
            if not _ipa_name:
                _log.warning("art: referenci %s se nepodarilo nahrat → "
                             "render bez podoby", ref_image)
        if _use_flux and _ipa_name:
            wf = _comfy_workflow_flux_pulid(
                ckpt, prompt, seed, w, h,
                int(acfg.get("flux_steps", 20)),
                float(acfg.get("flux_guidance", 3.5)),
                _ipa_name, float(ref_weight))
            _log.info("art: FLUX+PuLID — podoba z %s (w=%.2f)", ref_image, ref_weight)
        elif _use_flux:
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
    _log_artwork(diary_db_path, title, caption, rel_path, prompt, vision_desc)
    _log.info('art: ruční obraz pro „%s" → %s', title, rel_path)
    return rel_path, caption


# ── HANS_CAPABILITY_AWARENESS_V1 — malování na LIBOVOLNÉ téma (na požádání) ──
# HANS_ART_CS_CASE_V1 (6.9.) — namet od uzivatele prichazi ve 4. PADU a casto
# BEZ DIAKRITIKY („namaluj mi kocku"). qwen2.5:7b to cetl jako jine slovo a
# maloval neco jineho: „kocku" -> hraci KOSTKA (doloženo živě 6. 9., obraz
# zlute kostky misto kocky), „sovu" -> jestrab, „vlci maky" -> vyjici vlci,
# „most v zimni mlze" -> postava v mlze (most cetl jako anglicke „most").
#
# ZMERENO (8 nametu x 3 behy, temperature 0.6):
#   dnesni stav                     13/24
#   namet rucne do 1. padu          21/24  <- vinu nese PAD, ne diakritika
#   (samotna diakritika jen         15/24)
#   tato klauzule                   24/24
# Na vlastnich jmenech a fransizach neskodi: 21/24 -> 23/24.
#
# ⛔ Cesta pres normalizacni LLM krok (namet -> 1. pad) je ZMERENA A ZAMITNUTA:
#   qwen2.5:7b 3/48 (plodi „koralku cukru", michá cyrilici),
#   hans-czech 0/48 (jen opisuje few-shot vzory).
# ⛔ Anglicka napoveda z `_translate_subject` take NE: 15/24 -> 17/24 a u
#   „kocku" udela TYZ omyl („cube"), u „vlci maky" vrati „wolves'bane".
# Model tedy PREKLADAT UMI — jen spatne cte vstup. Proto klauzule, ne kod.
_CS_CASE_HINT = (
    "The Czech phrase may be INFLECTED (an oblique case) and may be written "
    "WITHOUT diacritics \u2014 e.g. 'kocku' means 'ko\u010dka' (a cat), "
    "'sovu' means 'sova' (an owl). Before writing the prompt, work out the "
    "dictionary (nominative) form of every Czech noun and depict THAT. Never "
    "guess an English word by how the Czech letters look. If a Czech word "
    "looks like an English word (e.g. 'most' = bridge, 'pes' = dog), it is "
    "still CZECH \u2014 translate it."
)

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
    "detailed, artistic, high quality. " + _CS_CASE_HINT
)


_HONORIFIC = re.compile(
    r"^\s*(pan[íaou]?|pán[aeu]?|slečn[aou]|sir|mr|mrs|ms|dr)\s+", re.IGNORECASE)


from scripts.hans_art_ukotveni import _en_name   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _ground_via_wikipedia   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _looks_like_scene   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_ART_CHAR_APPEARANCE_V1 — detekce fiktivní postavy + extrakce vzhledu z Wiki
_IS_CHARACTER = re.compile(
    r"poh[áa]dkov[áéíou]+\s+postav|fiktivn[íi]|je\s+postav|postav[au]\s+z\b"
    r"|ve[čc]ern[íi][čc]|kreslen|animovan|hrdin[au]|loupe[žz]n", re.I)
_APPEAR_KW = re.compile(
    r"vous|klobouk|[čc]epic|nos[íi]\b|o[šs]acen|oble[čc]|obuv|botk|vlas|sukn"
    r"|halenk|[šs]aty|kab[áa]t|pl[áa][šs][ťt]|br[ýy]l|vzhled|vypad|m[áa]\s+na\s+sob",
    re.I)


from scripts.hans_art_ukotveni import _wiki_character_appearance   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_art_ukotveni import _wiki_place_appearance   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _place_appearance_from_text   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _ground_subject   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import tv_paint_subject   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _style_from_study   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _ground_style   # ROZDELENI_PRIKAZU_V1 — přesunuto


_STYLE_SCENE_SYSTEM = (
    "You turn a short Czech description of a SUBJECT and an ART STYLE into ONE "
    "concise English SDXL image prompt — an evocative artistic INTERPRETATION of "
    "the subject RENDERED IN THAT STYLE. Output ONLY the prompt (no preamble, no "
    "quotes). Reply in ENGLISH ONLY (no Chinese/Japanese). NO text, letters or "
    "words in the image, NO watermark. END with strong English keywords of the "
    "requested ART STYLE (movement/artist name + its visual traits), NOT a generic "
    "tail. " + _CS_CASE_HINT
)


from scripts.hans_art_ukotveni import _download_ref_image   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _resolve_entity   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _fetch_person_ref   # ROZDELENI_PRIKAZU_V1 — přesunuto


_PERSON_FILLER = {
    "na", "ve", "in", "the", "of", "and", "pan", "pani", "paní", "herce",
    "herec", "hereckou", "portret", "portrét", "obraz", "podobizna", "jako",
    "se", "si", "je", "byl", "byla", "sir", "lord", "mistr", "svaty", "svatý",
}


from scripts.hans_art_ukotveni import _subject_beyond_name   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _ref_ma_tvar   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_malby import paint_person_from_photo   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_malby import paint_place_from_photo   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_malby import paint_self   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_art_ukotveni import _is_proper_name   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _name_shaped   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_ukotveni import _wiki_capture_person   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_malby import paint_subject   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_art_domov import _home_source   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _home_paint_source   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import paint_home   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import render_home_now   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── img2img: přemaluj REÁLNOU fotku Hansova pohledu do uměleckého stylu ──────
# Nejvěrnější varianta — kompozice/rozložení zůstane z fotky, SDXL jen přidá styl.
_HOME_STYLE_PROMPT = (
    "a cozy living room interior, the same scene and layout, warm artistic oil "
    "painting, painterly brushwork, soft natural daylight, warm inviting "
    "atmosphere, rich texture, fine art, masterful"
)
_PHOTO_EXT = (".jpg", ".jpeg", ".png", ".webp")


from scripts.hans_art_domov import _pick_home_photo   # ROZDELENI_PRIKAZU_V1 — přesunuto


# AVATAR_IDENTITY_REF_V1 — `_resize_to_temp` se přesunula do avatar_render
# (potřebují ji oba) a importuje se nahoře. Zde záměrně už není.


from scripts.hans_art_domov import paint_home_from_photo   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import render_home_photo_now   # ROZDELENI_PRIKAZU_V1 — přesunuto


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
    _log_artwork(diary_db_path, title, caption, rel_path, prompt, vision_desc)
    _mark_done(diary_db_path, book["book_id"])
    _log.info('art: obraz hotov pro „%s" → %s', title, rel_path)
    return True


from scripts.hans_art_domov import _jmena_ve_snu   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _last_dream_painting_ts   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _sablony_snu   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _recent_unpainted_dream   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import paint_dream   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_DAY_PAINTING_V1 — Hans namaluje svůj den / náladu ───────────────────
_DAY_EVENT_TYPES = ("reading_takeaway", "movie_opinion", "introspection",
                    "web_read", "room_description", "case_opened", "case_closed",
                    "book_reflection", "dialog_reflection", "spontaneous")


from scripts.hans_art_domov import _day_mood   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _day_fragments   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _last_day_painting_ts   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import _last_home_painting_ts   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_art_domov import paint_day   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── Ruční test: python3 -m scripts.hans_art [název knihy] ────────────────────
# Vyrenderuje vzorek pro aktuálně čtenou (nebo zadanou) knihu BEZ značení done
# a BEZ zápisu do deníku → lze pouštět opakovaně a vidět, co vzniká.
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    try:                                   # HANS_MAIN_CONFIG_IO_V1
        from scripts.config_io import load as _cfg_load
    except ImportError:                    # spusteno jako skript, root chybi
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
        from scripts.config_io import load as _cfg_load
    cfg = _cfg_load()
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
