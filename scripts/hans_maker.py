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
    _log.info("maker: obrázky %d/%d vyrenderováno", len(mapping), len(uniq))
    return html


def make_coder_artifact(config: dict, topic: str, brief: str,
                        deepen_round: int = 0) -> dict:
    """Coder model vykoná brief → samostatný index.html. Vrací {status, path,
    model, bytes}. Ukládá VERZOVANĚ (v<round>/) — verze se nepřepisují."""
    model = _coder_model(config)
    system = (
        "Jsi senior front-end vývojář. Dostáváš DESIGN BRIEF (v angličtině) "
        "sestavený z toho, co si autor nastudoval. Implementuj ho jako JEDEN "
        "samostatný soubor index.html s veškerým CSS vloženým v <style>. "
        "APLIKUJ VŠECHNY principy z briefu (kompozice, hierarchie, barevné "
        "harmonie, typografie…) — ty znáš jejich konkrétní realizaci (hodnoty, "
        "vzorce, CSS techniky), i když je brief neuvádí. Použij smysluplný "
        "ukázkový obsah.\n"
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
        res = make_coder_artifact(config, topic, brief, deepen_round)
    elif target == "image":
        res = _make_image_artifact(config, db_path, topic, brief)
    else:
        return {"status": "idle", "reason": "cíl '%s' zatím nemá executor" % target}
    # 3) deník + Hans si zapíše novou schopnost (co se naučil + jak použít)
    if res.get("status") == "made":
        try:
            _log_artifact(db_path, topic, target, res, deepen_round)
        except Exception as e:
            _log.debug("maker diary: %s", e)
        try:
            # HANS_LEARNED_CAPABILITIES_V1 — po prvním díle z domény si Hans SÁM
            # zapíše, co se naučil a jak to použít (idempotentní dle id).
            from scripts.hans_capabilities import (add_learned_capability,
                                                    detect_new_capabilities)
            if add_learned_capability(
                    "learned_" + _slug(topic),
                    "Nastudoval jsem téma „%s“ a umím z toho vytvořit reálné dílo "
                    "(stránku/artefakt aplikující, co jsem se naučil)" % topic,
                    "/vytvor %s" % topic):
                # okamžitá self-detekce (Hans si nové schopnosti všimne hned,
                # nečeká na periodickou kontrolu ani na restart)
                detect_new_capabilities(db_path)
        except Exception as e:
            _log.debug("maker learned cap: %s", e)
    return {**res, "topic": topic, "target": target, "round": deepen_round}


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
