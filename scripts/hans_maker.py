"""HANS_MAKER_V1 — exekuce: brief → nástroj → reálný artefakt (uzavření smyčky).

Poslední článek oblouku studium → dílo:
  study_note → hans_brief (destilát „nejlepší prompt z naučeného") → TENTO modul
  (nástroj brief vykoná) → artefakt na disku.

Dělba práce (rozhodnutí uživatele): Hans (ze studia) říká CO aplikovat = brief;
NÁSTROJ (coder/obrazový model) ví JAK = tady. Persona se nepoužívá — čistě
technická exekuce briefu.

Cíle:
  - coder → coder-schopný model (qwen2.5-coder z toolscoutu, jinak qwen2.5:14b)
    → samostatný index.html s vloženým CSS → data/works/artifacts/<slug>/.
  - image → ComfyUI (reuse hans_art) → obraz aplikující nastudovanou estetiku.

VRAM: coder model jede `num_gpu:0` (RAM/CPU) — NEsahá na rezidentní hans-czech
ani na hru (vzor reasoning tier). Pomalejší, ale one-shot a bezpečné.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_maker")

_ROOT = Path(__file__).resolve().parent.parent
_ART_DIR = _ROOT / "data" / "works" / "artifacts"

# preferované coder modely (nejlepší → fallback), filtrované na to, co je na PC
_CODER_PREFS = ("qwen2.5-coder", "deepseek-coder-v2", "deepseek-coder",
                "qwen2.5:14b", "qwen2.5:7b")


def _cfg(config: dict) -> dict:
    return (config or {}).get("maker", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


def _slug(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", (s or "dilo").lower()).strip("_")
    return s[:40] or "dilo"


def _installed_models(config: dict) -> list:
    """Seznam modelů na PC (ollama list přes SSH). [] když nedostupné."""
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return []
        out = pc_remote.run(config, "ollama list", timeout=12)
        if not out:
            return []
        names = []
        for line in str(out).splitlines()[1:]:
            p = line.split()
            if p:
                names.append(p[0])
        return names
    except Exception:
        return []


def _coder_model(config: dict) -> str:
    """Vyber coder model: config override → nejlepší nainstalovaný z prefs →
    qwen2.5:14b jako rozumný default."""
    override = _cfg(config).get("coder_model")
    if override:
        return override
    installed = _installed_models(config)
    inst_base = {m.split(":")[0]: m for m in installed}
    for pref in _CODER_PREFS:
        base = pref.split(":")[0]
        if pref in installed:
            return pref
        if base in inst_base:
            return inst_base[base]
    return "qwen2.5:14b"


# ── brief → HTML/CSS artefakt (coder cíl) ────────────────────────────────────
def _warm_chat(config: dict):
    """Nahřej rezidentní chat model (hans-czech) zpět do VRAM po jednorázové
    exekuci coder modelu (který ho na GPU dočasně vytlačil)."""
    try:
        from scripts.ollama_client import ollama_warmup
        chat = ((config.get("dialog", {}) or {}).get("model")
                or "hans-czech:latest")
        ollama_warmup(chat, config=config)
    except Exception as e:
        _log.debug("maker warm chat: %s", e)


def _extract_html(raw: str) -> str:
    """Vytáhni HTML dokument z odpovědi modelu (odstraň markdown fences / omáčku)."""
    t = raw.strip()
    # ```html ... ``` blok
    m = re.search(r"```(?:html)?\s*(.*?)```", t, re.S | re.I)
    if m:
        t = m.group(1).strip()
    # od <!DOCTYPE nebo <html po </html>
    m2 = re.search(r"(<!DOCTYPE html.*?</html>)", t, re.S | re.I)
    if m2:
        return m2.group(1)
    m3 = re.search(r"(<html.*?</html>)", t, re.S | re.I)
    if m3:
        return m3.group(1)
    return t if "<" in t and ">" in t else ""


def _placeholder_svg(desc: str) -> str:
    """Neutrální inline SVG placeholder (když se obrázek nevyrenderuje / ComfyUI
    dole) — ať v HTML není rozbitý src='GEN:…'."""
    import base64 as _b64
    label = (desc or "obrázek")[:40].replace("<", "").replace(">", "")
    svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='800' height='450'>"
           "<rect width='100%%' height='100%%' fill='#e8e8ee'/>"
           "<text x='50%%' y='50%%' font-family='sans-serif' font-size='20' "
           "fill='#99a' text-anchor='middle' dominant-baseline='middle'>%s</text>"
           "</svg>" % label)
    return "data:image/svg+xml;base64," + _b64.b64encode(
        svg.encode("utf-8")).decode()


def _generate_images(config: dict, html: str, dest_dir: Path) -> str:
    """Najdi <img src="GEN:<popis>"> → vyrenderuj SDXL (ComfyUI) → přepiš src na
    lokální images/imgN.png. Nevyrenderované → SVG placeholder. Best-effort
    (ComfyUI dole → placeholdery). VRAM: LLM offloadnutý (coder už proběhl)."""
    if not _cfg(config).get("gen_images", True):
        return html
    import uuid
    # chytni GEN: v <img src="…"> I v CSS url('…')/url(…) — desc končí u "/'/)
    gens = re.findall(r'GEN:([^"\')]+)', html)
    if not gens:
        return html
    uniq = []
    for g in gens:
        if g not in uniq:
            uniq.append(g)
    uniq = uniq[:int(_cfg(config).get("max_images", 4))]
    mapping = {}
    try:
        from scripts.hans_art import _ckpt
        from scripts.avatar_render import (
            _comfy_url, _comfy_workflow, _comfy_submit, _comfy_wait,
            _first_image, _comfy_fetch_image, _comfy_free,
            _ollama_loaded, _ollama_unload)
        ckpt = _ckpt(config)
        if ckpt:
            base = _comfy_url(config)
            img_dir = dest_dir / "images"
            img_dir.mkdir(parents=True, exist_ok=True)
            _ollama_unload(config, _ollama_loaded(config))  # VRAM pro SDXL
            try:
                for i, desc in enumerate(uniq):
                    prompt = desc.strip() + ", high quality, sharp, detailed"
                    fn = "img%d.png" % (i + 1)
                    wf = _comfy_workflow(ckpt, prompt,
                                         uuid.uuid4().int % (2 ** 31),
                                         1024, 640, 26, 6.0)
                    pid = _comfy_submit(base, wf, uuid.uuid4().hex)
                    hist = _comfy_wait(base, pid, 300) if pid else None
                    img = _first_image(hist) if hist else None
                    if img and _comfy_fetch_image(base, img,
                                                  str(img_dir / fn)):
                        mapping[desc] = "images/" + fn
                        _log.info("maker: obrázek %d/%d '%s'", i + 1,
                                  len(uniq), desc[:40])
            finally:
                _comfy_free(config)
    except Exception as e:
        _log.warning("maker gen images: %s", e)
    # přepiš src: vyrenderované → lokální, zbytek → placeholder
    for desc in uniq:
        target = mapping.get(desc) or _placeholder_svg(desc)
        html = html.replace("GEN:" + desc, target)
    if uniq and len(mapping) < len(uniq):
        # HANS_MAKER_IMG_SURFACE_V1 — tiché selhání renderu (ComfyUI dole/VRAM)
        # dřív spadlo na placeholder bez stopy → dílo „bez grafiky". Zviditelnit.
        _log.warning("maker: obrázky JEN %d/%d vyrenderováno — zbytek placeholder "
                     "(ComfyUI dole nebo render selhal)", len(mapping), len(uniq))
    else:
        _log.info("maker: obrázky %d/%d vyrenderováno", len(mapping), len(uniq))
    return html


# ── HANS_MAKER_MULTIPAGE_V1 — vícestránkové dílo (landing + podstránky) ──────

_LANDING_SYSTEM = (
    "Jsi senior front-end vývojář. Vytvoř STYLOVOU rozcestníkovou (landing) "
    "stránku index.html — self-contained, VEŠKERÉ CSS v jednom <style>. "
    "APLIKUJ nastudované design principy z briefu (kompozice, vizuální "
    "hierarchie, barevné harmonie, typografie) tak, aby byly VIDĚT. STRUKTURA: "
    "působivý <header>/hero s <img src=\"GEN:<krátký ANGLICKÝ popis scény>\"> a "
    "výrazným nadpisem, poutavý úvodní odstavec k tématu (2-3 věty), pak SEKCE "
    "pro přehled kapitol, a nakonec patička. "
    "⚠️ KARTY KAPITOL NEPIŠ SÁM a NEVYMÝŠLEJ odkazy na podstránky — na místo, "
    "kam patří mřížka karet, vlož PŘESNĚ tento komentář na samostatný řádek: "
    "<!--HANS_CARDS--> (nic jiného tam nedávej; karty s odkazy doplní systém). "
    "Ve <style> ale POČÍTEJ s mřížkou karet: nastyluj .site-cards (responzivní "
    "grid) a .site-cards .card (hezká karta s hover) v duchu nastudovaných "
    "principů. Vrať POUZE kód index.html, nic dalšího."
)

# garantované CSS karet — použije se VŽDY (i když coder .site-cards nenastyloval)
_CARD_CSS = (
    "<style>.site-cards{display:grid;grid-template-columns:"
    "repeat(auto-fill,minmax(260px,1fr));gap:1.1rem;max-width:1100px;"
    "margin:2rem auto;padding:0 1rem}.site-cards .card{display:block;"
    "text-decoration:none;color:inherit;background:#fff;border:1px solid "
    "#e6e3dd;border-radius:12px;padding:1.1rem 1.25rem;box-shadow:0 2px 8px "
    "rgba(0,0,0,.05);transition:transform .15s ease,box-shadow .15s ease}"
    ".site-cards .card:hover{transform:translateY(-3px);box-shadow:0 10px 24px "
    "rgba(0,0,0,.12)}.site-cards .card h2{margin:.1rem 0 .5rem;font-size:1.12rem;"
    "line-height:1.3}.site-cards .card p{margin:0;font-size:.9rem;color:#5f5f5f;"
    "line-height:1.5}</style>"
)


def _landing_cards(subs: list, notes: list) -> str:
    """Deterministická STYLOVANÁ mřížka karet pro VŠECHNA pod-témata (title +
    lákadlo z první věty poznámky + správný odkaz). Nahrazuje nespolehlivé
    karty od codera i dřívější plain-ul fallback."""
    import html as _h
    txt_by = {n["sub"]: (n.get("text") or "") for n in notes}
    items = []
    for slug, sub in subs:
        first = re.split(r"(?<=[.!?])\s+", txt_by.get(sub, "").strip())[0:1]
        teaser = (first[0] if first else "")[:150]
        items.append(
            "<a class=\"card\" href=\"detail-%s.html\"><h2>%s</h2><p>%s</p></a>"
            % (slug, _h.escape(sub), _h.escape(teaser)))
    return "<section class=\"site-cards\">" + "".join(items) + "</section>"

_SUBPAGE_CSS_FALLBACK = (
    "<style>body{font-family:Georgia,serif;line-height:1.7;color:#222;"
    "background:#faf9f7;margin:0}.article{max-width:760px;margin:0 auto;"
    "padding:2.5rem 1.2rem}.article h1{font-size:2rem;line-height:1.2}"
    ".article img{max-width:100%;border-radius:10px;margin:1.2rem 0}"
    ".back a{color:#7a5;text-decoration:none;font-family:sans-serif}"
    ".article p{margin:1rem 0}</style>"
)


def _extract_style(html: str) -> str:
    m = re.search(r"<style[^>]*>.*?</style>", html or "", re.S | re.I)
    return m.group(0) if m else _SUBPAGE_CSS_FALLBACK


def _paragraphize(text: str) -> str:
    """Souvislý text poznámky → odstavce (po ~3 větách) pro čitelnost."""
    import html as _h
    text = (text or "").strip()
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) <= 1:
        sents = re.split(r"(?<=[.!?])\s+", text)
        blocks = []
        for i in range(0, len(sents), 3):
            chunk = " ".join(sents[i:i + 3]).strip()
            if chunk:
                blocks.append(chunk)
    return "\n".join("<p>%s</p>" % _h.escape(b) for b in blocks) or "<p></p>"


def _subpage_html(style: str, topic: str, sub: str, text: str,
                  img_desc: str, home: str = "index.html") -> str:
    import html as _h
    img_desc = (img_desc or "").replace("\"", " ").replace("'", " ").strip()
    return (
        "<!DOCTYPE html>\n<html lang=\"cs\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, "
        "initial-scale=1.0\">\n<title>%s — %s</title>\n%s\n</head>\n<body>\n"
        "<article class=\"article detail\">\n"
        "<p class=\"back\"><a href=\"%s\">← zpět na přehled</a></p>\n"
        "<h1>%s</h1>\n"
        "<img class=\"detail-img\" src=\"GEN:%s\" alt=\"%s\">\n"
        "%s\n"
        "<p class=\"back\"><a href=\"%s\">← zpět na přehled</a></p>\n"
        "</article>\n</body>\n</html>"
        % (_h.escape(sub), _h.escape(topic), style, home, _h.escape(sub),
           img_desc, _h.escape(sub), _paragraphize(text), home)
    )


def _image_prompts_for(config: dict, subs: list) -> dict:
    """Jeden LLM call → ANGLICKÝ image prompt pro každé CZ pod-téma (pořadím).
    {sub: en_prompt}; fallback {} → generický prompt."""
    out = {}
    if not subs:
        return out
    try:
        from scripts.ollama_client import ollama_generate
        lst = "\n".join("%d. %s" % (i + 1, s) for i, s in enumerate(subs))
        raw = ollama_generate(
            _coder_model(config),
            "Below are %d Czech topics. For EACH output ONE short English "
            "image-generation prompt (a concrete visual scene, 6-12 words). "
            "Output EXACTLY %d numbered lines '1.'..'%d.' and nothing else:\n%s"
            % (len(subs), len(subs), len(subs), lst),
            system="You output only the numbered list of English image prompts.",
            config=config, timeout=int(_cfg(config).get("llm_timeout", 600)),
            keep_alive=0, options={"temperature": 0.4, "num_predict": 1000,
                                   "num_gpu": int(_cfg(config).get("num_gpu", 99))})
        lines = [re.sub(r"^\s*\d+[.)]\s*", "", l).strip()
                 for l in (raw or "").splitlines() if l.strip()]
        if len(lines) >= len(subs):
            for s, p in zip(subs, lines[:len(subs)]):
                out[s] = p
    except Exception as e:
        _log.debug("maker image prompts: %s", e)
    return out


def _render_site_images(config: dict, pages: dict, dest_dir: Path, cap: int):
    """VŠECHNY GEN: napříč stránkami v JEDNOM průchodu (jeden VRAM handoff,
    globálně unikátní názvy), dosad zpět. Vrací (pages_out, n, total)."""
    if not _cfg(config).get("gen_images", True):
        return pages, 0, 0
    import uuid
    all_gens = []
    for html in pages.values():
        for g in re.findall(r"GEN:([^\"')]+)", html):
            if g not in all_gens:
                all_gens.append(g)
    all_gens = all_gens[:cap]
    mapping = {}
    if all_gens:
        try:
            from scripts.hans_art import _ckpt
            from scripts.avatar_render import (
                _comfy_url, _comfy_workflow, _comfy_workflow_flux,
                _comfy_submit, _comfy_wait,
                _first_image, _comfy_fetch_image, _comfy_free,
                _ollama_loaded, _ollama_unload)
            # HANS_MAKER_FLUX_V1 — web obrázky STEJNÝM modelem jako sny/malování
            # (FLUX, když hans_art.use_flux), ne slabší SDXL. Dřív maker jel na
            # SDXL → viditelně horší liga než Hansovy ostatní obrazy.
            acfg = (config.get("hans_art", {}) or {})
            use_flux = bool(acfg.get("use_flux", False))
            ckpt = (acfg.get("flux_ckpt", "flux1-dev-fp8.safetensors")
                    if use_flux else _ckpt(config))
            if ckpt:
                base = _comfy_url(config)
                img_dir = dest_dir / "images"
                img_dir.mkdir(parents=True, exist_ok=True)
                _ollama_unload(config, _ollama_loaded(config))
                try:
                    for i, desc in enumerate(all_gens):
                        prompt = desc.strip() + ", high quality, sharp, detailed"
                        fn = "img%d.png" % (i + 1)
                        seed = uuid.uuid4().int % (2 ** 31)
                        if use_flux:
                            wf = _comfy_workflow_flux(
                                ckpt, prompt, seed, 1024, 640,
                                int(acfg.get("flux_steps", 20)),
                                float(acfg.get("flux_guidance", 3.5)))
                        else:
                            wf = _comfy_workflow(ckpt, prompt, seed,
                                                 1024, 640, 26, 6.0)
                        pid = _comfy_submit(base, wf, uuid.uuid4().hex)
                        hist = _comfy_wait(base, pid, 300) if pid else None
                        img = _first_image(hist) if hist else None
                        if img and _comfy_fetch_image(base, img,
                                                      str(img_dir / fn)):
                            mapping[desc] = "images/" + fn
                            _log.info("maker site: obrázek %d/%d '%s'",
                                      i + 1, len(all_gens), desc[:40])
                finally:
                    _comfy_free(config)
        except Exception as e:
            _log.warning("maker site gen images: %s", e)
    out = {}
    for name, html in pages.items():
        for desc in set(re.findall(r"GEN:([^\"')]+)", html)):
            tgt = mapping.get(desc) or _placeholder_svg(desc)
            html = html.replace("GEN:" + desc, tgt)
        out[name] = html
    if all_gens and len(mapping) < len(all_gens):
        _log.warning("maker site: obrázky JEN %d/%d — zbytek placeholder",
                     len(mapping), len(all_gens))
    else:
        _log.info("maker site: obrázky %d/%d vyrenderováno",
                  len(mapping), len(all_gens))
    return out, len(mapping), len(all_gens)


# ── HANS_MAKER_PRINCIPLE_CHECK_V1 — propsaly se principy do CSS? ────────────
# Každý princip má DETERMINISTICKOU stopu v kódu. Kontroluje se jen to, co brief
# skutečně žádá (klíčová slova), ať se nevyčítá princip, který se neučil.
_PRINCIPLE_CHECKS = (
    # (co v briefu, lidský název, jak se pozná v CSS)
    (("kerning", "tracking", "letter-spacing", "prostrkání"),
     "kerning/tracking (letter-spacing)", "letter_spacing"),
    (("leading", "line-height", "řádkování", "radkovani"),
     "řádkování (line-height)", "line_height"),
    (("hierarch", "hierarchie"),
     "vizuální hierarchie (typová škála)", "type_scale"),
    (("third", "třetin", "tretin", "golden", "zlatý řez", "zlaty rez",
      "composition", "kompozic", "grid", "mřížka"),
     "kompozice (mřížka / zlatý řez)", "layout_grid"),
    (("colour", "color", "barv", "palette", "paleta"),
     "barevný akcent (ne jen šedá)", "accent_color"),
    (("harmon",),
     "barevná harmonie (≥2 odstíny)", "two_hues"),
)


def _css_colors(html: str) -> list:
    """Barvy z CSS jako (h, s, v). Bere #rgb, #rrggbb a rgb()."""
    import colorsys
    out = []
    for m in re.finditer(r"#([0-9a-fA-F]{3,6})\b", html or ""):
        h = m.group(1)
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        if len(h) != 6:
            continue
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        out.append(colorsys.rgb_to_hsv(r, g, b))
    for m in re.finditer(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", html or ""):
        r, g, b = (int(m.group(i)) / 255.0 for i in (1, 2, 3))
        out.append(colorsys.rgb_to_hsv(r, g, b))
    return out


def _has_trace(kind: str, html: str) -> bool:
    """Je v kódu stopa po daném principu?"""
    h = html or ""
    if kind == "letter_spacing":
        return "letter-spacing" in h
    if kind == "line_height":
        return "line-height" in h
    if kind == "type_scale":
        sizes = {m.group(1) for m in re.finditer(
            r"font-size\s*:\s*([0-9.]+(?:rem|em|px|%))", h)}
        return len(sizes) >= 3
    if kind == "layout_grid":
        return ("grid-template-columns" in h or "1.618" in h
                or re.search(r"display\s*:\s*grid", h) is not None
                or "columns:" in h)
    if kind in ("accent_color", "two_hues"):
        chrom = [c for c in _css_colors(h) if c[1] >= 0.25 and 0.12 <= c[2] <= 0.97]
        if kind == "accent_color":
            return len(chrom) >= 1
        hues = sorted(c[0] * 360 for c in chrom)
        return any(b - a >= 30 for a, b in zip(hues, hues[1:])) if len(hues) >= 2 else False
    return True


def principle_gaps(brief: str, html: str) -> list:
    """Které principy brief žádá, ale v kódu po nich není stopa. Čistá funkce."""
    b = (brief or "").lower()
    gaps = []
    for keys, label, kind in _PRINCIPLE_CHECKS:
        if not any(k in b for k in keys):
            continue                      # brief to nežádá → nevyčítej
        if not _has_trace(kind, html):
            gaps.append(label)
    return gaps


def _repair_principles(config: dict, model: str, prompt: str, system: str,
                       html: str, brief: str, what: str = "") -> str:
    """Jedno opravné kolo: modelu se VYJMENUJE, co v kódu chybí. Nová verze se
    přijme jen když je platná A má MENŠÍ mezeru — jinak zůstane původní
    (opravné kolo nesmí dílo zhoršit)."""
    gaps = principle_gaps(brief, html)
    if not gaps or not _cfg(config).get("principle_check", True):
        if gaps:
            _log.info("maker%s: principy nekontroluji (principle_check=false)", what)
        return html
    _log.warning("maker%s: v kódu CHYBÍ nastudované principy: %s — jedno "
                 "opravné kolo", what, "; ".join(gaps))
    try:
        from scripts.ollama_client import ollama_generate
        raw = ollama_generate(
            model,
            prompt + "\n\nPŘEDCHOZÍ VERZE principy NEAPLIKOVALA. Uprav CSS tak, "
            "aby bylo v kódu VIDĚT tohle:\n- " + "\n- ".join(gaps) +
            "\nKonkrétně: letter-spacing na nadpisech, mřížka "
            "(grid-template-columns) nebo poměr 1.618 v rozvržení, a alespoň "
            "jedna sytá akcentová barva (ne jen odstíny šedé). Obsah a strukturu "
            "zachovej. Vrať POUZE kompletní kód.",
            system=system, config=config,
            timeout=int(_cfg(config).get("llm_timeout", 600)), keep_alive=0,
            options={"temperature": float(_cfg(config).get("temperature", 0.3)),
                     "num_ctx": int(_cfg(config).get("num_ctx", 8192)),
                     "num_predict": int(_cfg(config).get("num_predict", 4096)),
                     "num_gpu": int(_cfg(config).get("num_gpu", 99))})
        fixed = _extract_html(raw) if (raw and raw.strip()) else ""
        if fixed and len(fixed) >= 120:
            after = principle_gaps(brief, fixed)
            if len(after) < len(gaps):
                _log.info("maker%s: opravné kolo pomohlo — chybí už jen %s",
                          what, "; ".join(after) or "nic")
                return fixed
            _log.info("maker%s: opravné kolo nepomohlo (chybí %d→%d) — "
                      "nechávám původní", what, len(gaps), len(after))
        else:
            _log.info("maker%s: opravné kolo nevrátilo použitelný kód", what)
    except Exception as e:
        _log.warning("maker%s: opravné kolo selhalo: %s", what, e)
    return html


def make_coder_site(config: dict, db_path: str, topic: str, brief: str,
                    deepen_round: int = 0) -> dict:
    """HANS_MAKER_MULTIPAGE_V1 — vícestránkové dílo: coder udělá stylovou
    landing (rozcestník s kartami), každé pod-téma dostane PODSTRÁNKU s PLNÝM
    textem studijní poznámky (grounded, ne LLM) + obrázkem. Vrací {status,...}."""
    import html as _h
    from scripts import hans_brief
    mat = hans_brief.gather_study_material(
        db_path, topic, int(_cfg(config).get("site_max_chars", 40000)))
    notes = mat.get("notes") or []
    if not notes:
        return {"status": "idle", "reason": "žádné studijní poznámky"}
    subs = [(_slug(n["sub"]), n["sub"]) for n in notes]
    dest_dir = _ART_DIR / _slug(topic) / ("v%d" % int(deepen_round))
    dest_dir.mkdir(parents=True, exist_ok=True)
    model = _coder_model(config)
    result = {"status": "deferred", "reason": "LLM nedostupný"}
    try:
        from scripts.ollama_client import ollama_generate
        cards = "\n".join("- %s  →  detail-%s.html" % (sub, slug)
                          for slug, sub in subs)
        raw = ollama_generate(
            model, "Téma: %s\n\nDESIGN BRIEF:\n%s\n\nPOD-TÉMATA (název → soubor "
            "podstránky):\n%s\n\nVrať index.html:" % (topic, brief, cards),
            system=_LANDING_SYSTEM, config=config,
            timeout=int(_cfg(config).get("llm_timeout", 600)), keep_alive=0,
            options={"temperature": float(_cfg(config).get("temperature", 0.3)),
                     "num_ctx": int(_cfg(config).get("num_ctx", 8192)),
                     "num_predict": int(_cfg(config).get("num_predict", 4096)),
                     "num_gpu": int(_cfg(config).get("num_gpu", 99))})
        landing = _extract_html(raw) if (raw and raw.strip()) else ""
        if not landing or len(landing) < 120:
            return {"status": "deferred", "reason": "landing prázdný"}
        # HANS_MAKER_PRINCIPLE_CHECK_V1 — landing nese STYL pro celý web
        # (`_extract_style` ho kopíruje do podstránek), takže opravou CSS tady
        # se principy propíšou do všech stránek naráz.
        landing = _repair_principles(
            config, model,
            "Téma: %s\n\nDESIGN BRIEF:\n%s\n\nVrať index.html:" % (topic, brief),
            _LANDING_SYSTEM, landing, brief, " site")
        img_prompts = _image_prompts_for(config, [sub for _, sub in subs])
        style = _extract_style(landing)
        pages = {"index.html": landing}
        for (slug, sub), n in zip(subs, notes):
            desc = img_prompts.get(sub) or ("historical illustration, %s" % sub)
            pages["detail-%s.html" % slug] = _subpage_html(
                style, topic, sub, n["text"], desc)
        # KARTY — deterministicky, STYLOVANĚ, VŽDY všechna pod-témata. Coder
        # nechává <!--HANS_CARDS-->; když ho vynechá, doplň stylované karty pro
        # NElinkovaná témata (dřív = ošklivý plain-ul seznam → „jen odkazy").
        if "<!--HANS_CARDS-->" in landing:
            landing = landing.replace("<!--HANS_CARDS-->",
                                      _landing_cards(subs, notes), 1)
        else:
            miss = [(s, u) for s, u in subs
                    if ("detail-%s.html" % s) not in landing]
            if miss:
                add = _landing_cards(miss, notes)
                landing = (landing.replace("</body>", add + "</body>", 1)
                           if "</body>" in landing else landing + add)
                _log.info("maker site: doplněno %d stylovaných karet "
                          "(coder vynechal marker)", len(miss))
        # garantované CSS karet (i když coder .site-cards nenastyloval)
        landing = (landing.replace("</head>", _CARD_CSS + "</head>", 1)
                   if "</head>" in landing else _CARD_CSS + landing)
        pages["index.html"] = landing
        cap = int(_cfg(config).get("site_max_images", len(subs) + 2))
        pages, rendered, total = _render_site_images(config, pages, dest_dir, cap)
        for name, html in pages.items():
            (dest_dir / name).write_text(html, encoding="utf-8")
        tbytes = sum(len(h) for h in pages.values())
        _log.info("maker site '%s' kolo %d: %d stránek, %d/%d obrázků, %d B (%s)",
                  topic, deepen_round, len(pages), rendered, total, tbytes, model)
        result = {"status": "made",
                  "path": str((dest_dir / "index.html").relative_to(_ROOT)),
                  "model": model, "bytes": tbytes, "pages": len(pages)}
    except Exception as e:
        _log.warning("maker site: %s", e)
    finally:
        _warm_chat(config)
    return result


def make_coder_artifact(config: dict, topic: str, brief: str,
                        deepen_round: int = 0) -> dict:
    """Coder model vykoná brief → samostatný index.html. Vrací {status, path,
    model, bytes}. Ukládá VERZOVANĚ (v<round>/) — verze se nepřepisují."""
    model = _coder_model(config)
    system = (
        "Jsi senior front-end vývojář. Dostáváš DESIGN BRIEF (v angličtině) "
        "sestavený z toho, co si autor nastudoval. Implementuj ho jako JEDEN "
        "samostatný soubor index.html s veškerým CSS vloženým v <style>. "
        "Vytvoř BOHATOU, VÍCESEKČNÍ stránku (hero + několik obsahových sekcí + "
        "patička) — NIKDY ne jediný izolovaný prvek (samotné tlačítko/kartu) "
        "ani prázdnou 'jednoduchou' ukázku. "
        "APLIKUJ VŠECHNY principy z briefu (kompozice, hierarchie, barevné "
        "harmonie, typografie…) tak, aby byly v layoutu VIDĚT — ty znáš jejich "
        "konkrétní realizaci (hodnoty, vzorce, CSS techniky), i když je brief "
        "neuvádí. Použij bohatý, smysluplný ukázkový obsah.\n"
        "OBRÁZKY: kde má být obrázek, použij <img src=\"GEN:<stručný ANGLICKÝ "
        "popis toho, co má obrázek zobrazovat>\" alt=\"…\"> — NEPOUŽÍVEJ externí "
        "URL ani placeholder služby; ty popisy se později vyrenderují. Použij "
        "1–4 obrázky, kde dávají smysl.\n"
        "Vrať POUZE kód jednoho HTML dokumentu, nic dalšího."
    )
    result = {"status": "deferred", "reason": "LLM nedostupný (výpadek/herní mód)"}
    try:
        from scripts.ollama_client import ollama_generate
        # coder model na GPU (rychlé) → dočasně vytlačí rezidentní hans-czech;
        # keep_alive=0 ho pak uvolní. Obrázky (SDXL) se renderují DOKUD je chat
        # offloadnutý; hans-czech nahřejeme zpět až úplně na konci (finally).
        raw = ollama_generate(
            model, "DESIGN BRIEF:\n%s\n\nVrať kompletní index.html:" % brief,
            system=system, config=config,
            timeout=int(_cfg(config).get("llm_timeout", 600)), keep_alive=0,
            options={"temperature": float(_cfg(config).get("temperature", 0.3)),
                     "num_ctx": int(_cfg(config).get("num_ctx", 8192)),
                     "num_predict": int(_cfg(config).get("num_predict", 4096)),
                     "num_gpu": int(_cfg(config).get("num_gpu", 99))})
        html = _extract_html(raw) if (raw and raw.strip()) else ""
        if not html or len(html) < 120:
            result = {"status": "deferred",
                      "reason": "model nevrátil použitelné HTML"}
        else:
            dest_dir = _ART_DIR / _slug(topic) / ("v%d" % int(deepen_round))
            dest_dir.mkdir(parents=True, exist_ok=True)
            # obrázky (VRAM: chat je po coder genu offloadnutý) — PŘED warmem
            html = _generate_images(config, html, dest_dir)
            dest = dest_dir / "index.html"
            dest.write_text(html, encoding="utf-8")
            _log.info("maker: artefakt '%s' → %s (%d B, model %s)", topic, dest,
                      len(html), model)
            result = {"status": "made", "path": str(dest.relative_to(_ROOT)),
                      "model": model, "bytes": len(html)}
    except Exception as e:
        _log.warning("maker coder: %s", e)
    finally:
        _warm_chat(config)   # vrať chat do VRAM až po obrázcích
    return result


# ── top-level: brief → artefakt (uzavření smyčky) ────────────────────────────
def has_artifact_for_round(db_path: str, topic: str, deepen_round: int) -> bool:
    """Existuje už artefakt pro téma v tomto kole prohloubení? (idempotence B)."""
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = c.execute("SELECT data FROM diary WHERE event_type='work_artifact'"
                         " ORDER BY ts DESC LIMIT 50").fetchall()
        c.close()
        for (data,) in rows:
            try:
                d = json.loads(data or "{}")
            except Exception:
                continue
            if d.get("topic") == topic and int(d.get("round", 0)) == int(deepen_round):
                return True
    except Exception:
        pass
    return False


def make_from_study(config: dict, db_path: str, topic: str,
                    target: str = "coder", deepen_round: int = 0) -> dict:
    """Celá smyčka od studia: vezmi (nebo postav) brief → nech nástroj vykonat →
    ulož artefakt + deník. Vrací {status, ...}. Deferral-safe."""
    if not enabled(config):
        return {"status": "idle", "reason": "vypnuto"}
    # HANS_STUDY_VRAM_HANDOFF_V1 (rozšíření na maker) — brief+coder běží na base
    # modelu (8GB); hans-czech (8GB) je rezidentní, 8+8 > 16GB VRAM. Uvolni ho
    # AKTIVNĚ (pause_warmup samo nestačí — keep_alive=-1 nevyprší), jinak coder
    # call ve dne 300s timeoutuje (v noci projde jen náhodou). Resume+warm finally.
    try:
        from scripts.ollama_client import (pause_warmup as _pw,
                                           ollama_unload_all as _ua)
        _pw(1800)
        _ua(config=config)
    except Exception:
        pass
    try:
        from scripts import hans_brief
        # 1) brief — reuse jen pro kolo 0; po prohloubení (round>0) přibyly hlubší
        # poznámky → postav brief ZNOVU, ať je nese.
        store = hans_brief.BriefStore(db_path)
        last = store.latest(topic)
        reuse = bool(last and last.get("target") == target and deepen_round == 0)
        brief = last.get("brief") if reuse else None
        if not brief:
            b = hans_brief.build_brief(config, db_path, topic, target)
            if b.get("status") != "built":
                return {"status": "deferred" if b.get("status") == "deferred"
                        else "idle", "reason": "brief: %s" % b.get("reason",
                                                                  b.get("status"))}
            brief = b["brief"]
        # 2) exekuce dle cíle
        if target == "coder":
            res = make_coder_site(config, db_path, topic, brief, deepen_round)
        elif target == "image":
            res = _make_image_artifact(config, db_path, topic, brief)
        else:
            return {"status": "idle",
                    "reason": "cíl '%s' zatím nemá executor" % target}
        # 3) deník + Hans si zapíše novou schopnost (co se naučil + jak použít)
        if res.get("status") == "made":
            try:
                _log_artifact(db_path, topic, target, res, deepen_round)
            except Exception as e:
                _log.debug("maker diary: %s", e)
            try:
                # HANS_LEARNED_CAPABILITIES_V1 — po prvním díle z domény si Hans
                # SÁM zapíše, co se naučil a jak to použít (idempotentní dle id).
                from scripts.hans_capabilities import (add_learned_capability,
                                                        detect_new_capabilities)
                if add_learned_capability(
                        "learned_" + _slug(topic),
                        "Nastudoval jsem téma „%s“ a umím z toho vytvořit reálné "
                        "dílo (stránku aplikující, co jsem se naučil)" % topic,
                        "/vytvor %s" % topic):
                    detect_new_capabilities(db_path)
            except Exception as e:
                _log.debug("maker learned cap: %s", e)
        return {**res, "topic": topic, "target": target, "round": deepen_round}
    finally:
        try:
            from scripts.ollama_client import resume_warmup as _rw
            _rw()
        except Exception:
            pass
        try:
            _warm_chat(config)
        except Exception:
            pass


def _make_image_artifact(config: dict, db_path: str, topic: str,
                         brief: str) -> dict:
    """Obrazový cíl: brief (vizuální prompt) → ComfyUI přes hans_art. Reuse
    ověřené render pipeline; artefakt do galerie source='study_artifact'."""
    try:
        from scripts import hans_art
        # brief je už vizuální prompt v EN → použij paint_subject se stylovým intro
        r = hans_art.paint_subject(config, db_path, brief[:400])
        if r:
            rel, _cap = r
            return {"status": "made", "path": rel, "model": "SDXL", "bytes": 0}
        return {"status": "deferred", "reason": "ComfyUI/render nevyšel"}
    except Exception as e:
        return {"status": "deferred", "reason": str(e)[:80]}


def _log_artifact(db_path: str, topic: str, target: str, res: dict,
                  deepen_round: int = 0):
    c = sqlite3.connect(db_path, timeout=10)
    note = "Vytvořil jsem dílo k tématu %s (%s): %s" % (topic, target,
                                                        res.get("path", ""))
    data = json.dumps({"topic": topic, "target": target,
                       "path": res.get("path"), "model": res.get("model"),
                       "round": deepen_round}, ensure_ascii=False)
    c.execute("INSERT INTO diary (ts, event_type, title, note, data) VALUES "
              "(?,?,?,?,?)", (time.time(), "work_artifact",
                              "Dílo: %s" % topic, note, data))
    c.commit()
    c.close()


def list_works(db_path: str = "") -> list:
    """Seznam děl ze souborů (verze se nepřepisují). Vrací [{topic, slug,
    versions:[{round, rel, mtime, has_images}]}]. topic z deníku (nebo slug)."""
    out = []
    if not _ART_DIR.exists():
        return out
    # slug → hezký název tématu z deníku
    names = {}
    if db_path:
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            for (data,) in c.execute("SELECT data FROM diary WHERE "
                                     "event_type='work_artifact'").fetchall():
                try:
                    d = json.loads(data or "{}")
                    if d.get("topic"):
                        names[_slug(d["topic"])] = d["topic"]
                except Exception:
                    pass
            c.close()
        except Exception:
            pass
    for slug_dir in sorted(_ART_DIR.iterdir()):
        if not slug_dir.is_dir():
            continue
        versions = []
        for vdir in sorted(slug_dir.iterdir()):
            idx = vdir / "index.html"
            if vdir.is_dir() and idx.exists():
                nm = vdir.name
                rnd = int(nm[1:]) if nm.startswith("v") and nm[1:].isdigit() else 0
                versions.append({"round": rnd,
                                 "rel": str(idx.relative_to(_ART_DIR)),
                                 "mtime": idx.stat().st_mtime,
                                 "has_images": (vdir / "images").exists()})
        if versions:
            out.append({"topic": names.get(slug_dir.name,
                                           slug_dir.name.replace("_", " ").title()),
                        "slug": slug_dir.name,
                        "versions": sorted(versions, key=lambda x: x["round"])})
    return out


def latest_artifacts(db_path: str, limit: int = 5) -> list:
    try:
        c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = c.execute("SELECT ts, title, note, data FROM diary WHERE "
                         "event_type='work_artifact' ORDER BY ts DESC LIMIT ?",
                         (limit,)).fetchall()
        c.close()
        out = []
        for ts, title, note, data in rows:
            try:
                d = json.loads(data or "{}")
            except Exception:
                d = {}
            out.append({"ts": ts, "title": title, "note": note, **d})
        return out
    except Exception:
        return []


if __name__ == "__main__":
    import sys
    cfg = json.loads((_ROOT / "config.json").read_text(encoding="utf-8"))
    topic = sys.argv[1] if len(sys.argv) > 1 else "Design"
    target = sys.argv[2] if len(sys.argv) > 2 else "coder"
    print("coder model:", _coder_model(cfg))
    r = make_from_study(cfg, "data/hans_diary.db", topic, target)
    print(json.dumps(r, ensure_ascii=False, indent=2))
