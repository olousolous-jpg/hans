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
    _comfy_url, _comfy_workflow, _comfy_submit, _comfy_wait,
    _first_image, _comfy_fetch_image,
    _ollama_loaded, _ollama_unload, _comfy_free, _ollama_warm,
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
def _scene_prompt(config: dict, title: str, reflection: str, db_path: str = "",
                  system: str = None, source_intro: str = None) -> str:
    """LLM (levný, keep_alive=0) → anglický SDXL scene prompt. Fallback šablona.
    HANS_ART_LESSON_V1: když db_path, vloží do promptu ponaučení z minulých obrazů.
    HANS_DREAMS_V1: system+source_intro lze přepsat (snová varianta místo knižní)."""
    fallback = (f"a quiet evocative scene inspired by '{title}', "
                "symbolic objects, soft window light, oil painting, "
                "atmospheric lighting, rich detail, masterful")
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return fallback
    acfg = _acfg(config)
    model = str(acfg.get("prompt_model", "qwen2.5:7b"))
    user = (source_intro if source_intro is not None
            else f"Book: {title}\n\nReader's reflection (Czech):\n{reflection}\n\n")
    lessons = _recent_lessons(db_path)
    if lessons:
        user += ("LEARNED GUIDANCE from your past paintings (respect these — they "
                 "make the next piece better):\n- " + "\n- ".join(lessons) + "\n\n")
        _log.info("art: scene prompt zohledňuje %d ponaučení", len(lessons))
    user += "Write the SDXL prompt."
    try:
        raw = ollama_generate(
            model, user, system=(system or _PROMPT_SYSTEM), config=config,
            timeout=int(acfg.get("llm_timeout", 90)), keep_alive=0)
    except Exception as e:
        _log.warning("art: scene prompt LLM failed: %s", e)
        return fallback
    if not raw or not raw.strip():
        return fallback
    p = raw.strip().strip('"').replace("\n", " ")
    return p[:600]


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
    "You are looking at a finished oil painting. In 2-3 sentences describe what is "
    "depicted (subject, setting, dominant colors, mood) and give a BALANCED, honest "
    "assessment of the craft: say what works, AND point out a genuine weakness if "
    "one is actually visible (e.g. a slightly awkward figure, hand or face, a muddy "
    "area). Do not invent or exaggerate flaws, but do not gloss over a real one "
    "either. Be accurate — neither flattering nor fault-hunting."
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
    past = _past_verdicts(db_path)
    past_block = ""
    if past:
        past_block = ("Tvé dřívější obrazy a jak ses k nim vyjádřil:\n"
                      + "\n".join('- „%s": %s' % (t, n) for t, n in past) + "\n\n")
    system = (core + "\n\n" if core else "") + (
        "Právě jsi dokončil olejomalbu inspirovanou " + source_label + ". Níže máš NEZÁVISLÝ "
        "popis toho, co je na plátně vidět. Napiš 2-3 věty v první osobě: co jsi "
        "namaloval a jak hodnotíš výsledek. Buď UPŘÍMNÝ a VYVÁŽENÝ — pochval, co se "
        "povedlo, a věcně přiznej skutečný nedostatek, pokud tam OPRAVDU je (např. "
        "lehce nepovedená postava, ruka či obličej), ale nepřeháněj drobnosti ani si "
        "nevymýšlej vady. Cíl je trefit se do reality: zdařilý obraz oceníš, slabší "
        "místo pojmenuješ bez dramatizování. Smíš se ohlédnout za dřívějšími obrazy. "
        "Žádné uvozovky, žádný nadpis.")
    user = (past_block
            + "Kniha: %s\n" % title
            + "Tvá reflexe knihy: %s\n\n" % (reflection or "")[:400]
            + "Co je na obrazu skutečně vidět (nezávislý popis):\n%s\n\n" % vision_desc
            + "Napiš svůj verdikt.")
    try:
        out = ollama_generate(model, user, system=system, config=config,
                              timeout=int(acfg.get("verdict_timeout", 120)))
    except Exception as e:
        _log.warning("art: verdict LLM failed: %s", e)
        return fallback
    out = (out or "").strip().strip('"')
    if out:
        _log.info("art: Hansův verdikt: %.120s", out)
        return out[:500]
    return fallback


# ── HANS_ART_LESSON_V1 — smyčka verdikt → ponaučení → příští render ──────────
# (a) Z vize+verdiktu odvodí krátké anglické PONAUČENÍ ('art_lesson' v deníku);
# _scene_prompt ho příště vloží qwen do promptu + _lesson_negatives dolní negativ.
_LESSON_SYSTEM = (
    "You are an art director. You receive an INDEPENDENT description of a "
    "rendered image and the painter's own verdict. Output ONE short line of "
    "reusable guidance IN ENGLISH for the painter's NEXT image. If the piece "
    "worked well (it usually does), REINFORCE what to keep doing — the styles, "
    "subjects or moods that succeeded. Suggest AVOIDING something ONLY when the "
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
    user = ("Independent description of the rendered image:\n%s\n\n"
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
def _render_image(config: dict, title: str, reflection: str, db_path: str = "",
                  scene_system: str = None, scene_intro: str = None):
    """Vyrenderuje 1 obraz přes ComfyUI/SDXL. Vrací (rel_path, prompt, vision_desc)
    nebo None. VRAM orchestrace uvnitř (unload LLM → render → _comfy_free →
    llava vize → warm hans-czech). vision_desc = llava popis renderu pro hodnocení
    (HANS_ART_VERDICT_V1), '' když vize selže. db_path → ponaučení z minulých
    obrazů (HANS_ART_LESSON_V1) ovlivní scénu i negativní prompt. Nikdy nehází."""
    ckpt = _ckpt(config)
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
                           system=scene_system, source_intro=scene_intro)
    acfg = _acfg(config)
    w = int(acfg.get("width", 1024)); h = int(acfg.get("height", 768))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2**31)   # náhodný seed = každý obraz jiný
    client_id = uuid.uuid4().hex

    os.makedirs(ART_DIR, exist_ok=True)
    fname = f"{int(time.time())}_{_slug(title)}.png"
    dest = os.path.join(ART_DIR, fname)

    loaded = _ollama_loaded(config)
    _ollama_unload(config, loaded)
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        wf = _comfy_workflow(ckpt, prompt, seed, w, h, steps, cfg_s)
        # HANS_ART_LESSON_V1 — dolň negativní prompt podle ponaučení z minulých obrazů
        extra_neg = _lesson_negatives(_recent_lessons(db_path))
        if extra_neg and isinstance(wf.get("7"), dict):
            wf["7"]["inputs"]["text"] = wf["7"]["inputs"]["text"] + ", " + extra_neg
            _log.info("art: negativ dolněn ponaučením: %s", extra_neg)
        _log.info("art: render start (%s, %dx%d, %d steps, timeout %ds) — prompt: %.120s",
                  ckpt, w, h, steps, rtimeout, prompt)
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
                        scene_system=_DREAM_SCENE_SYSTEM, scene_intro=scene_intro)
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
                        scene_system=_DAY_SCENE_SYSTEM, scene_intro=scene_intro)
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
