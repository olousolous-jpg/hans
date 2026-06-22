"""scripts/avatar_render.py

AVATAR_RENDER_V1 — render avataru z descriptoru přes ComfyUI (SDXL).

Fáze 3 avatara (drahá větev). Z descriptoru (avatar_descriptor.py) složí SDXL
prompt, vyrenderuje sadu výrazů (idle/talking/greeting/thinking) přes ComfyUI
API na PC, stáhne obrázky a uloží do cache per verze. Označí descriptor
rendered=1.

VRAM orchestrace ([[ollama-vram-tiers]]): SDXL se nevejde vedle LLM (hans-czech
10.8 + llava 4.2 ≈ 15/16 GB). Před renderem se Ollama modely uvolní
(keep_alive=0 unload), po renderu se hans-czech nahřeje zpět. Render JEN vzácně
a v klidu (volá se za Severkou v noci). Deferral: když ComfyUI nedostupný,
descriptor zůstává rendered=0 → dožene se příště.

Checkpoint je přepínatelný: config hans_avatar.image_model (porovnání stylů).

API:
  render_pending(config, diary_db_path) -> bool     # entry point: najdi rendered=0 a vyrenderuj
  render_descriptor(config, descriptor, diary_db_path) -> bool
"""
from __future__ import annotations
import json
import logging
import os
import time
import urllib.request
import urllib.parse
import uuid
from typing import Optional

_log = logging.getLogger("avatar_render")

# Funkční výrazy (stavy interakce) → modifikátor promptu (anglicky, SDXL).
EXPRESSIONS = {
    "idle":     "neutral calm expression, looking forward",
    "talking":  "speaking, mouth slightly open, mid-conversation",
    "greeting": "warm welcoming smile, slight head bow",
    "thinking": "thoughtful pensive expression, looking slightly up",
}

# Nálady (hans_mood.MOODS) → modifikátor výrazu. Idle baseline dle aktuální nálady.
# Ukládá se jako mood_{name}.png. Drží stejný APPEARANCE, mění jen emoci/postoj.
MOODS = {
    "content":     "calm content expression, serene and at ease, faint pleasant look",
    "curious":     "curious intrigued expression, one eyebrow slightly raised, attentive",
    "lonely":      "wistful lonely expression, gaze slightly downcast, quietly pensive",
    "melancholic": "melancholic subdued expression, distant gaze, faint sadness",
    "engaged":     "alert attentive expression, present and ready to assist",
    "worried":     "concerned worried expression, slight frown, subtle tension",
}

# AVATAR_ACTIVITY_V1 — aktivitní scény (co Hans dělá) → act_{name}.png. Tier 1
# stilly; display je vybere dle hans_idle.current_activity_label() (přebíjí náladu).
ACTIVITIES = {
    "reading":  "absorbed in reading, holding an open book in both hands, eyes lowered to the pages",
    "watching": "watching a glowing screen to one side, face lit by the screen, attentive sideways gaze",
    "looking":  "glancing around the room, head turned to the side, alert curious sideways look",
}
# Aktivity potřebují ŠIRŠÍ záběr (ruce/kniha/scéna), jinak těsný portrét aktivitu
# ořízne a není vidět. STYL: drž skeleton promptu i seed JAKO nálady (renderovaný
# 3D look = seed 42 + „character portrait"; seed je v SDXL největší stylová páka,
# proto offset=0). „illustration" / jiný seed stahovaly styl do komiksu/tužky.
# Jen širší crop slovy — kniha se ukáže přes modifikátor, styl zůstane.
ACTIVITY_FRAMING = "character portrait, upper body, waist-up framing, hands visible"
ACTIVITY_SEED_OFFSET = 0

_NEG = ("lowres, blurry, deformed, extra limbs, bad anatomy, watermark, text, "
        "signature, multiple people, nsfw, "
        # AVATAR_ACTIVITY_V1 — anti-drift identity: reading kontext táhl „učence"
        # (brýle, knírek, starší/hubenější). Drž čistou tvář napříč rendery.
        "glasses, eyeglasses, moustache, mustache, beard, facial hair")

# Pole descriptoru, co tvoří popis vzhledu (anglicky).
_APPEARANCE = ("role", "attire", "age_look", "build", "demeanor", "setting",
               "palette", "identity_anchor")


def _acfg(config: dict) -> dict:
    return config.get("hans_avatar", {}) or {}


def _comfy_url(config: dict) -> str:
    return _acfg(config).get("comfyui_url", "http://127.0.0.1:8188").rstrip("/")


def _ollama_url(config: dict) -> str:
    return (config.get("models", {}).get("base_url")
            or config.get("openwebui_direct", {}).get("ollama_url")
            or "http://127.0.0.1:11434").rstrip("/")


# ── Prompt z descriptoru ────────────────────────────────────────────────────
# AVATAR_STYLE_ANCHOR_V1 — explicitní stylová kotva drží JEDNOTNÝ medium (renderovaný
# 3D, barva, šedé pozadí) napříč seedy/prompty. Bez ní malá změna promptu překlápěla
# styl do komiksu/grayscale (nálady měly jen štěstí na seed 42).
_STYLE = ("full color, stylized 3d character render, semi-realistic, smooth "
          "volumetric shading, soft studio lighting, plain grey background")


def build_prompt(descriptor: dict, modifier: str,
                 framing: str = "character portrait, head and shoulders") -> str:
    parts = [str(descriptor.get(f, "")).strip() for f in _APPEARANCE]
    base = ", ".join(p for p in parts if p)
    return (f"{framing}, {base}, {modifier}, {_STYLE}, "
            "detailed face, consistent character")


def _render_targets() -> list:
    """[(filename, modifikátor)] — funkční výrazy + nálady + aktivity (AVATAR_ACTIVITY_V1)."""
    return ([(f"{k}.png", v) for k, v in EXPRESSIONS.items()]
            + [(f"mood_{k}.png", v) for k, v in MOODS.items()]
            + [(f"act_{k}.png", v) for k, v in ACTIVITIES.items()])


# ── VRAM orchestrace (uvolni LLM pro SDXL, pak vrať) ────────────────────────
def _ollama_unload(config: dict, models: list) -> None:
    """keep_alive=0 → modely se po (prázdném) requestu uvolní z VRAM."""
    url = _ollama_url(config)
    for m in models:
        try:
            data = json.dumps({"model": m, "prompt": "", "keep_alive": 0}).encode()
            req = urllib.request.Request(f"{url}/api/generate", data=data,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
            _log.info("avatar: Ollama unload %s", m)
        except Exception as _e:
            _log.debug("avatar: unload %s selhal: %s", m, _e)


def _ollama_loaded(config: dict) -> list:
    try:
        with urllib.request.urlopen(f"{_ollama_url(config)}/api/ps", timeout=10) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return []


def _comfy_free(config: dict) -> None:
    """AVATAR_RENDER_COMFY_FREE_V1 — uvolni VRAM v ComfyUI po renderu.
    Jinak ComfyUI drží SDXL checkpoint (~7GB) rezidentně → hans-czech (10.8GB)
    se nenahraje → _ollama_warm vyprší → chat/analytika timeout. ComfyUI /free API."""
    try:
        base = _comfy_url(config)
        data = json.dumps({"unload_models": True, "free_memory": True}).encode()
        req = urllib.request.Request(f"{base}/free", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        _log.info("avatar: ComfyUI VRAM uvolněna (/free)")
    except Exception as _e:
        _log.debug("avatar: ComfyUI /free selhal: %s", _e)


def _ollama_warm(config: dict, model: str) -> None:
    try:
        data = json.dumps({"model": model, "prompt": "ok", "keep_alive": -1,
                           "stream": False}).encode()
        req = urllib.request.Request(f"{_ollama_url(config)}/api/generate", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=120).read()
        _log.info("avatar: hans-czech nahřát zpět")
    except Exception as _e:
        _log.debug("avatar: warm %s selhal: %s", model, _e)


# ── ComfyUI API ─────────────────────────────────────────────────────────────
def _comfy_workflow(ckpt: str, prompt: str, seed: int, w: int, h: int,
                    steps: int, cfg: float) -> dict:
    """Minimální SDXL txt2img graf (ComfyUI API format)."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": _NEG, "clip": ["4", 1]}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": 1.0, "model": ["4", 0],
                         "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        # AVATAR_RENDER_TEMP_OUTPUT_V1 — PreviewImage ukládá do ComfyUI temp/ (ne
        # output/) → ComfyUI ho maže při restartu, rendery se trvale nehromadí na
        # disku PC (SSH na PC = refused, delete-output API ComfyUI nemá). Fetch
        # funguje stejně — history nese type=temp, _comfy_fetch_image je type-aware.
        "9": {"class_type": "PreviewImage",
              "inputs": {"images": ["8", 0]}},
    }


# AVATAR_TEMPLATE_IMG2IMG_V1 — odvozuj nálady/výrazy/aktivity ze ŠABLONY (jedna
# kanonická tvář) přes img2img → stejný Hans + styl, mění se jen výraz/póza.
def _comfy_upload_image(base: str, local_path: str) -> Optional[str]:
    """Nahraj obrázek do ComfyUI input/ (POST /upload/image). Vrací jméno k LoadImage."""
    import uuid as _uuid
    try:
        img = open(local_path, "rb").read()
        boundary = "----hans" + _uuid.uuid4().hex
        body = (("--%s\r\nContent-Disposition: form-data; name=\"image\"; "
                 "filename=\"hans_tmpl.png\"\r\nContent-Type: image/png\r\n\r\n"
                 % boundary).encode() + img + ("\r\n--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(f"{base}/upload/image", data=body,
                                     headers={"Content-Type":
                                              "multipart/form-data; boundary=%s" % boundary})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r).get("name")
    except Exception as _e:
        _log.warning("avatar: upload template selhal: %s", _e)
        return None


def _comfy_workflow_img2img(ckpt: str, prompt: str, seed: int, image_name: str,
                            denoise: float, steps: int, cfg: float,
                            vae_name: str = "sdxl_vae.safetensors") -> dict:
    """SDXL img2img graf — LoadImage(template) → VAEEncode → KSampler(denoise<1).
    AVATAR_IMG2IMG_VAE_FIX_V1: VAE z checkpointu sd_xl_base dělá u img2img encode
    barevné fleky/ghost text → samostatný VAELoader (sdxl-vae-fp16-fix) pro
    encode i decode. Když vae_name prázdný, fallback na VAE z checkpointu (['4',2])."""
    vae = ["12", 0] if vae_name else ["4", 2]
    wf = {
        "10": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "4":  {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ckpt}},
        "11": {"class_type": "VAEEncode",
               "inputs": {"pixels": ["10", 0], "vae": vae}},
        "6":  {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7":  {"class_type": "CLIPTextEncode", "inputs": {"text": _NEG, "clip": ["4", 1]}},
        "3":  {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                          "sampler_name": "dpmpp_2m", "scheduler": "karras",
                          "denoise": denoise, "model": ["4", 0],
                          "positive": ["6", 0], "negative": ["7", 0],
                          "latent_image": ["11", 0]}},
        "8":  {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": vae}},
        "9":  {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
    }
    if vae_name:
        wf["12"] = {"class_type": "VAELoader", "inputs": {"vae_name": vae_name}}
    return wf


def _comfy_submit(base: str, workflow: dict, client_id: str) -> Optional[str]:
    data = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{base}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("prompt_id")


def _comfy_wait(base: str, prompt_id: str, timeout: int = 300) -> Optional[dict]:
    """Poll /history dokud render nedoběhne. Vrátí history záznam nebo None."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/history/{prompt_id}", timeout=15) as r:
                hist = json.load(r)
            if prompt_id in hist:
                return hist[prompt_id]
        except Exception as _e:
            _log.debug("avatar: history poll: %s", _e)
        time.sleep(2)
    return None


def _comfy_fetch_image(base: str, img: dict, dest_path: str) -> bool:
    q = urllib.parse.urlencode({"filename": img["filename"],
                                "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    try:
        with urllib.request.urlopen(f"{base}/view?{q}", timeout=60) as r:
            data = r.read()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        with open(dest_path, "wb") as f:
            f.write(data)
        return True
    except Exception as _e:
        _log.warning("avatar: fetch image selhal: %s", _e)
        return False


def _first_image(hist: dict) -> Optional[dict]:
    for node in (hist.get("outputs") or {}).values():
        imgs = node.get("images") or []
        if imgs:
            return imgs[0]
    return None


# ── Hlavní render ───────────────────────────────────────────────────────────
def render_descriptor(config: dict, descriptor: dict, diary_db_path: str) -> bool:
    """Vyrenderuje VŠECHNY výrazy descriptoru přes ComfyUI, uloží do cache
    data/avatar/v{N}/{expr}.png. Vrací True při úspěchu (aspoň idle). Deferral-safe."""
    acfg = _acfg(config)
    ckpt = acfg.get("image_model", "")
    if not ckpt:
        _log.warning("avatar: hans_avatar.image_model není nastaven — render skip")
        return False
    base = _comfy_url(config)
    # ComfyUI dostupný?
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception as _e:
        _log.warning("avatar: ComfyUI nedostupný (%s) — render odložen", _e)
        return False

    ver = int(descriptor.get("version", 1))
    seed = int(acfg.get("seed_value", 42))  # fixní seed = konzistence napříč výrazy
    w = int(acfg.get("width", 768)); h = int(acfg.get("height", 768))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.0))
    cache_dir = os.path.join("data", "avatar", f"v{ver}")
    client_id = uuid.uuid4().hex

    # VRAM: uvolni LLM před renderem
    loaded = _ollama_loaded(config)
    _ollama_unload(config, loaded)

    targets = _render_targets()
    n_ok = 0
    try:
        for fname, modifier in targets:
            # AVATAR_ACTIVITY_V1 — aktivity: širší záběr + jiný seed (jinak portrét
            # ořízne ruce/knihu → aktivita není vidět). Výrazy/nálady beze změny.
            _is_act = fname.startswith("act_")
            prompt = build_prompt(descriptor, modifier,
                                  ACTIVITY_FRAMING if _is_act
                                  else "character portrait, head and shoulders")
            _seed = seed + ACTIVITY_SEED_OFFSET if _is_act else seed
            wf = _comfy_workflow(ckpt, prompt, _seed, w, h, steps, cfg_s)
            try:
                pid = _comfy_submit(base, wf, client_id)
                hist = _comfy_wait(base, pid) if pid else None
                img = _first_image(hist) if hist else None
                if img and _comfy_fetch_image(base, img,
                                              os.path.join(cache_dir, fname)):
                    n_ok += 1
                    _log.info("avatar: vyrenderován %s (%d/%d, v%d)",
                              fname, n_ok, len(targets), ver)
                else:
                    _log.warning("avatar: %s se nevyrenderoval", fname)
            except Exception as _e:
                _log.warning("avatar: render %s selhal: %s", fname, _e)
    finally:
        # VRAM: nejdřív uvolni ComfyUI (SDXL drží ~7GB), JINAK se hans-czech nenahraje
        # → chat timeout (AVATAR_RENDER_COMFY_FREE_V1). Pak teprve vrať hans-czech.
        _comfy_free(config)
        # VRAM: vrať hans-czech (chat ready)
        _ollama_warm(config, config.get("models", {}).get("dialog", "hans-czech:latest"))

    # rendered=1 JEN když projdou VŠECHNY výrazy+nálady (jinak retry příště — deferral)
    if n_ok == len(targets):
        _mark_rendered(diary_db_path, ver)
        return True
    _log.warning("avatar: render NEÚPLNÝ (%d/%d) — rendered zůstává 0, dožene se příště",
                 n_ok, len(targets))
    return False


def _mark_rendered(diary_db_path: str, version: int) -> None:
    import sqlite3
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute("UPDATE avatar_descriptors SET rendered=1 WHERE version=?", (version,))
        db.commit(); db.close()
        _log.info("avatar: descriptor v%d označen rendered=1", version)
    except Exception as _e:
        _log.warning("avatar: mark_rendered selhal: %s", _e)


def render_pending(config: dict, diary_db_path: str) -> bool:
    """Entry point: najdi nejnovější descriptor s rendered=0 a vyrenderuj.
    Volá se za Severkou (v noci). Deferral-safe — nikdy nehází."""
    try:
        from scripts.avatar_descriptor import latest_descriptor
        import sqlite3
        db = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True, timeout=3.0)
        row = db.execute("SELECT version, descriptor FROM avatar_descriptors "
                         "WHERE COALESCE(rendered,0)=0 ORDER BY version DESC LIMIT 1").fetchone()
        db.close()
        if not row:
            return False
        d = json.loads(row[1]); d["version"] = row[0]
        _log.info("avatar: render pending v%d", row[0])
        return render_descriptor(config, d, diary_db_path)
    except Exception as _e:
        _log.warning("render_pending selhal: %s", _e)
        return False


# ── Smoke (python3 -m scripts.avatar_render) ────────────────────────────────
if __name__ == "__main__":
    import sys
    cfg = json.load(open("config.json", encoding="utf-8"))
    db = cfg.get("diary_db", "data/hans_diary.db")
    print("ComfyUI:", _comfy_url(cfg), "| image_model:", _acfg(cfg).get("image_model", "(nenastaven)"))
    from scripts.avatar_descriptor import latest_descriptor
    d = latest_descriptor(db)
    if not d:
        print("Žádný descriptor — spusť /avatar gen"); sys.exit(0)
    print("Render v%d, výrazy+nálady: %s" % (
        d.get("version"), [t[0] for t in _render_targets()]))
    print("Příklad promptu (idle):\n ", build_prompt(d, EXPRESSIONS["idle"]))
    if "--render" in sys.argv:
        print("RENDER:", render_descriptor(cfg, d, db))
    else:
        print("(dry-run; pro skutečný render přidej --render a nastav hans_avatar.image_model)")
    sys.exit(0)
