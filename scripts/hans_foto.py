"""hans_foto.py — HANS_FOTO_V1 (27. 9.) — fotky poslané Hansovi na Matrix.

Dvě věci nad jednou fotkou (nápad uživatele 22. a 27. 9.):
  * POSUDEK — „hodí se ta kabelka k těm šatům?“: model, který vidí obrázky
    (`qwen2.5vl:7b`), napíše anglicky vlastní postřeh v 1. osobě, hans-czech
    ho řekne česky. Změřeno 27. 9. na zkušební fotce: česky přímo z vision
    modelu spletl barvu kabelky (hnědá → „tmavě zelená“) a byl 2× pomalejší;
    anglicky pojmenoval všechny barvy správně. Postřeh v 1. osobě, jinak
    hlasový krok psal „Stylista poznamenal…“.
  * ÚPRAVA — „změň barvu kabelky na modrou“: FLUX Kontext v ComfyUI na PC
    (`uprav`).

SOUKROMÍ (rozhodnuto 27. 9. předem, fotky budou skoro jistě lidí z domu):
  * posílat smí jen známé osoby (Matrix bere jen účty z `matrix.users`),
  * fotky zůstávají na Pi (`data/foto/`, gitignorováno) a na PC v LAN, žádná
    cloudová služba,
  * nic se nezapisuje do deníku, paměti (RAG) ani mezi Hansova díla,
  * po `foto.keep_days` (7) dnech se originály i výsledky smažou (`uklid`).
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

_log = logging.getLogger("hans_foto")

DIR = Path("data/foto")
_INDEX = DIR / "index.json"


def _cfg(config: dict) -> dict:
    return (config or {}).get("foto", {}) or {}


# ── úložiště ─────────────────────────────────────────────────────────────────
def _nacti() -> list:
    try:
        return json.loads(_INDEX.read_text(encoding="utf-8"))
    except Exception:
        return []


def _zapis(rows: list):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_INDEX)


def uloz(person: str, room: str, data: bytes, mimetype: str = "") -> str:
    """Uloží přijatou fotku, vrátí cestu."""
    DIR.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
        (mimetype or "").lower(), ".jpg")
    p = DIR / ("%d_%s%s" % (int(time.time() * 1000),
                            re.sub(r"\W", "", (person or "x").lower()) or "x", ext))
    p.write_bytes(data)
    rows = _nacti()
    rows.append({"path": str(p), "person": (person or "").lower(), "room": room,
                 "ts": time.time(), "druh": "prijata"})
    _zapis(rows)
    _log.info("foto: přijata od %s (%d kB)", person, len(data) // 1024)
    return str(p)


def pridej_vysledek(person: str, room: str, path: str):
    rows = _nacti()
    rows.append({"path": path, "person": (person or "").lower(), "room": room,
                 "ts": time.time(), "druh": "vysledek"})
    _zapis(rows)


def nedavne(person: str, room: str, okno_s: float, max_n: int = 4,
            druh: str = "prijata") -> list:
    """Cesty k posledním fotkám osoby v téže místnosti (nejstarší první)."""
    ted = time.time()
    rows = [r for r in _nacti() if r.get("person") == (person or "").lower()
            and r.get("room") == room and r.get("druh") == druh
            and ted - r.get("ts", 0) <= okno_s and Path(r["path"]).exists()]
    return [r["path"] for r in rows[-max_n:]]


def posledni_ts(person: str, room: str) -> float:
    ts = [r.get("ts", 0) for r in _nacti() if r.get("person") == (person or "").lower()
          and r.get("room") == room]
    return max(ts) if ts else 0.0


# ── HANS_FOTO_DOPORUCENI_V1 (28. 9.) — úprava „podle toho, co jsi doporučil“ ──
# Doloženo: „uprav obrázek dámy podle doporučení, co navrhuješ“ → pokyn pro
# Kontext „Adjust the lady's image according to my suggestions.“ — model
# posudek nezná, 8 minut renderu naprázdno. Posudek se proto ukládá
# (samostatný soubor, index.json nese jen fotky) a odkaz na něj se převede
# na JEDNU konkrétní změnu; když posudek žádnou nenavrhl, úprava se nespustí.
_POSUDKY = DIR / "posudky.json"
_ODKAZ = re.compile(
    r"(doporu\w*|navrh\w*|navrhuj\w*|poradil\w*|radil\w*|podle\s+(?:tebe|vás|vas|"
    r"sebe|toho,?\s+co)|jak\s+(?:jsi|jste|sis|jste\s+si)\s+\w+|co\s+(?:jsi|jste)\s+"
    r"(?:říkal|rikal|psal|navrhl)\w*|tvého\s+návrhu|vašeho\s+návrhu)", re.I)


def odkazuje_na_posudek(text: str) -> bool:
    return bool(_ODKAZ.search(text or ""))


def uloz_posudek(person: str, room: str, text: str):
    try:
        rows = json.loads(_POSUDKY.read_text(encoding="utf-8")) if _POSUDKY.exists() else []
    except Exception:
        rows = []
    rows.append({"person": (person or "").lower(), "room": room, "ts": time.time(),
                 "text": (text or "")[:2000]})
    DIR.mkdir(parents=True, exist_ok=True)
    _POSUDKY.write_text(json.dumps(rows[-20:], ensure_ascii=False), encoding="utf-8")


def posledni_posudek(person: str, room: str, okno_s: float) -> str:
    try:
        rows = json.loads(_POSUDKY.read_text(encoding="utf-8"))
    except Exception:
        return ""
    ted = time.time()
    ok = [r for r in rows if r.get("person") == (person or "").lower()
          and r.get("room") == room and ted - r.get("ts", 0) <= okno_s]
    return ok[-1]["text"] if ok else ""


def pokyn_z_posudku(config: dict, posudek: str, pokyn: str) -> Optional[str]:
    """Z posudku vytáhni JEDNU konkrétní změnu jako anglický rozkaz pro
    Kontext; None = posudek žádnou změnu nenavrhuje (nebo mozek dole)."""
    from scripts.ollama_client import ollama_generate
    model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
    fmt = {"type": "object", "properties": {"zmena": {"type": "boolean"},
                                             "en": {"type": "string"}},
           "required": ["zmena", "en"]}
    raw = ollama_generate(
        model, "Tvůj posudek k fotce:\n%s\n\nPokyn uživatele: %s" % (
            posudek.strip()[:1500], pokyn.strip()[:300]),
        system=("Uživatel chce fotku upravit podle tvého posudku. Najdi v posudku "
                "KONKRÉTNÍ navrženou změnu (co změnit a na jakou barvu či podobu). "
                "Když ji posudek obsahuje, zmena=true a do en napiš JEDNU krátkou "
                "anglickou větu v rozkazovacím způsobu pro obrazový model, např. "
                "„Change the color of the bag to burgundy.“ Když posudek jen chválí "
                "nebo žádnou změnu nenavrhuje, zmena=false a en prázdné."),
        config=config, timeout=60, format=fmt,
        options={"temperature": 0.1, "num_predict": 120, "num_ctx": 4096})
    try:
        d = json.loads(raw or "{}")
    except Exception:
        return None
    en = str(d.get("en") or "").strip().strip('"“”„')
    if not d.get("zmena") or not re.search(r"[a-zA-Z]{3}", en):
        return None
    return en.rstrip(".") + _zachovat(en)


def uklid(config: dict) -> int:
    """Smaže fotky starší než `foto.keep_days` (originály i výsledky)."""
    dni = float(_cfg(config).get("keep_days", 7))
    hranice = time.time() - dni * 86400
    rows, zustava, n = _nacti(), [], 0
    try:                                   # HANS_FOTO_DOPORUCENI_V1 — posudky taky
        if _POSUDKY.exists():
            _p = [r for r in json.loads(_POSUDKY.read_text(encoding="utf-8"))
                  if r.get("ts", 0) >= hranice]
            _POSUDKY.write_text(json.dumps(_p, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    for r in rows:
        if r.get("ts", 0) < hranice:
            try:
                Path(r["path"]).unlink(missing_ok=True)
                n += 1
            except Exception:
                zustava.append(r)
        else:
            zustava.append(r)
    if n:
        _zapis(zustava)
        _log.info("foto: úklid — smazáno %d starších než %g dní", n, dni)
    return n


# ── co chce člověk s fotkou ──────────────────────────────────────────────────
_UPRAVA = re.compile(
    r"\b(uprav|upravte|udělej|udělejte|udelej|udelejte|přidej|přidejte|pridej|"
    r"přidělej|pridelej|odstraň|odstraňte|odstran|odeber|odeberte|smaž|smaz|"
    r"změň|změňte|zmen|zmente|přebarvi|přebarvěte|prebarvi|obarvi|vyměň|vymen|"
    r"nahraď|nahrad|zasněž|dej\s+tam|dejte\s+tam|zkus\s+(?:ji|je|to|ho)\s+v|"
    r"ukaž(?:te)?\s+(?:mi\s+)?(?:(?:ty|ta|tu|ten|to|tyhle|tuhle)\s+)?\w+\s+v|"
    r"jak\s+by\s+(?:to|ty|ta|ten)\s+\w*\s*vypadal\w*\s+v|"
    # HANS_FOTO_EDIT_VERBS_V1 (28. 9., přání uživatele) — nejen oblečení:
    # odmazání lidí, rozmazání pozadí, styl malby, světlo… (FLUX Kontext to
    # umí, jen věta nedošla k úpravě a skončila jako posudek)
    r"(?:od|vy|za|pře|pre)?maž\w*|(?:od|vy|za|pre)?maz(?:at|te|ej)\b|"
    r"rozmaž\w*|rozmaz\w*|retuš\w*|retus\w*|vyretuš\w*|oprav\s+(?:to|ji|fotku)|"
    r"zesvětli\w*|zesvetli\w*|ztmav\w*|zaostři\w*|zaostri\w*|zbav\s+\w+|"
    r"převeď|preved|proměň|promen|předělej|predelej|"
    r"ať\s+(?:je|jsou|to|tam|vypadá|vypada)|at\s+(?:je|jsou|tam|vypada))", re.I)
    # ⚠️ samotné „jako obraz/malba“ ZÁMĚRNĚ ne: „vypadá to jako obraz?“ je otázka
_K_FOTCE = re.compile(
    r"\b(fot\w*|obráz\w*|obraz\w*|snímk\w*|tohle|tohle|tenhle|tahle|tyhle|"
    r"hodí|hodi|ladí|ladi|sluší|slusi|pasuje|barv\w*|šaty|saty|kabel\w*|bot\w*|"
    r"outfit|oblečen\w*|oblecen\w*|vypadá|vypada|co\s+říkáš|co\s+rikas|"
    r"co\s+na\s+to|jak\s+se\s+ti\s+líbí|jak\s+se\s+vám\s+líbí)\b", re.I)


def zamer(text: str) -> str:
    """'uprava' | 'posudek'."""
    return "uprava" if _UPRAVA.search(text or "") else "posudek"


def patri_k_fotce(person: str, room: str, text: str, config: dict) -> bool:
    """Týká se zpráva poslední fotky? Hned po fotce (`foto.hned_s`, 5 min) každá
    zpráva, později (do `foto.okno_s`, 30 min) jen s odkazem na fotku / úpravu."""
    c = _cfg(config)
    ts = 0.0
    for r in _nacti():
        if r.get("person") == (person or "").lower() and r.get("room") == room \
                and r.get("druh") == "prijata":
            ts = max(ts, r.get("ts", 0))
    if not ts:
        return False
    stari = time.time() - ts
    # HANS_FOTO_NOT_PAINT_V1 (28. 9.) — „namaluj Boris Becker“ 20 s po fotce
    # se vzal jako posudek fotky (každá zpráva do 5 min). Pokyn k malování
    # a lomítkový příkaz k fotce nepatří.
    if re.search(r"^\s*/|\b(namaluj|nakresli|namalovat|nakreslit)\w*", text or "", re.I):
        return False
    if stari <= float(c.get("hned_s", 300)):
        return True
    if stari <= float(c.get("okno_s", 1800)):
        return bool(_K_FOTCE.search(text or "") or _UPRAVA.search(text or ""))
    return False


# ── POSUDEK ─────────────────────────────────────────────────────────────────
_POSTREH_EN = (
    "Somebody at home shows you {n} and asks (in Czech): \"{q}\"\n"
    "Answer as a thoughtful friend with a good eye for style. Write in FIRST "
    "PERSON (\"I see…\", \"I would…\"), 3-5 sentences: name the relevant items "
    "and their exact colors, say honestly whether they go together and why, "
    "and give one concrete suggestion if something could be better. Do not "
    "describe the person's body. If the question is not about clothing, just "
    "answer it about what you see.")


def _pro_vision(path: str, max_px: int = 1024) -> str:
    """Fotka → base64 JPEG ≤ max_px na delší straně, otočená podle EXIF."""
    import io
    from PIL import Image, ImageOps
    im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    im.thumbnail((max_px, max_px))
    b = io.BytesIO()
    im.save(b, "JPEG", quality=85)
    return base64.b64encode(b.getvalue()).decode()


def k_posudku(person: str, room: str, config: dict, max_n: int = 4) -> list:
    """HANS_FOTO_POSUDEK_SIZE_V1 — k posudku jen fotky poslané spolu:
    nejnovější + ty do `hned_s` před ní (šaty a kabelka zvlášť), ne všechno
    za 30 min — 28. 9. šla do posudku i dřívější, už upravovaná fotka."""
    c = _cfg(config)
    vse = nedavne(person, room, float(c.get("okno_s", 1800)), max_n)
    if not vse:
        return []
    rows = {r["path"]: r.get("ts", 0) for r in _nacti()}
    posl = rows.get(vse[-1], 0)
    return [p for p in vse if posl - rows.get(p, 0) <= float(c.get("hned_s", 300))]


def posud(config: dict, paths: list, otazka: str) -> Optional[str]:
    """Česká odpověď na otázku k fotkám, nebo None (mozek není)."""
    from scripts.ollama_client import ollama_generate
    c = _cfg(config)
    # HANS_FOTO_POSUDEK_SIZE_V1 (28. 9.) — fotka z telefonu (2268×4032) sama
    # zabrala 4108 tokenů > okno 4096 → Ollama 400 a Hans hlásil „mozek
    # nedostupný“. Zkušební fotka z Commons (45 kB) to 27. 9. nepoznala.
    # Zmenšit na `vision_max_px` (EXIF otočení), okno `vision_num_ctx`.
    imgs = []
    for p in paths:
        try:
            imgs.append(_pro_vision(p, int(c.get("vision_max_px", 1024))))
        except Exception as e:
            _log.info("foto: fotku %s nešlo připravit: %s", p, e)
    if not imgs:
        return None
    t0 = time.time()
    en = ollama_generate(
        c.get("vision_model", "qwen2.5vl:7b"),
        _POSTREH_EN.format(n="a photo" if len(imgs) == 1 else "%d photos" % len(imgs),
                           q=(otazka or "").strip()[:400] or "Co na to říkáš?"),
        images=imgs, config=config, timeout=int(c.get("timeout", 180)),
        keep_alive=int(c.get("vision_keep_alive", 120)),
        options={"temperature": 0.3, "num_predict": 350,
                 "num_ctx": int(c.get("vision_num_ctx", 8192))})
    if not en:
        return None
    _log.info("foto: posudek (vision %.0f s): %.200s", time.time() - t0, en)
    try:
        from scripts.hans_persona import persona_core
        core = persona_core(config, with_address=False)
    except Exception:
        core = ""
    system = ((core + "\n\n") if core else "") + (
        "Níže je TVŮJ VLASTNÍ postřeh k fotce, zapsaný anglicky. Řekni ho "
        "tazateli česky, přirozeně a spisovně, vykej, 3–5 vět. Barvy a věci "
        "pojmenuj přesně podle postřehu, nic nepřidávej. Piš běžnými českými slovy.")
    model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
    cz = ollama_generate(
        model, "Otázka: %s\n\nTvůj postřeh (anglicky):\n%s" % ((otazka or "").strip(), en),
        system=system, config=config, timeout=120,
        options={"temperature": 0.2, "num_predict": 350, "num_ctx": 4096})
    _log.info("foto: posudek hotov za %.0f s", time.time() - t0)
    return (cz or "").strip() or None


# ── ÚPRAVA (FLUX Kontext) ────────────────────────────────────────────────────
def _kontext_workflow(image_name: str, prompt_en: str, seed: int, c: dict) -> dict:
    """FLUX.1 Kontext dev (úprava podle věty), ComfyUI API formát. Výsledek do
    PreviewImage (temp složka ComfyUI), ne do output/ — fotky lidí z domu."""
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": c.get("kontext_unet", "flux1-dev-kontext_fp8_scaled.safetensors"),
            "weight_dtype": "default"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {
            "clip_name1": "clip_l.safetensors",
            "clip_name2": c.get("kontext_t5", "t5xxl_fp8_e4m3fn_scaled.safetensors"),
            "type": "flux"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "5": {"class_type": "FluxKontextImageScale", "inputs": {"image": ["4", 0]}},
        "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt_en, "clip": ["2", 0]}},
        "8": {"class_type": "ReferenceLatent", "inputs": {
            "conditioning": ["7", 0], "latent": ["6", 0]}},
        "9": {"class_type": "FluxGuidance", "inputs": {
            "conditioning": ["8", 0], "guidance": float(c.get("kontext_guidance", 2.5))}},
        "10": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["7", 0]}},
        "11": {"class_type": "KSampler", "inputs": {
            "seed": seed, "steps": int(c.get("kontext_steps", 20)), "cfg": 1.0,
            "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
            "model": ["1", 0], "positive": ["9", 0], "negative": ["10", 0],
            "latent_image": ["6", 0]}},
        "12": {"class_type": "VAEDecode", "inputs": {"samples": ["11", 0], "vae": ["3", 0]}},
        "13": {"class_type": "PreviewImage", "inputs": {"images": ["12", 0]}},
    }


def _pokyn_en(config: dict, pokyn: str) -> Optional[str]:
    """Český pokyn → krátký anglický rozkaz pro Kontext (rezidentní hans-czech)."""
    from scripts.ollama_client import ollama_generate
    model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
    out = ollama_generate(
        model, pokyn.strip()[:400],
        system=("Přelož pokyn k úpravě fotografie do angličtiny jako JEDNU krátkou, "
                "konkrétní větu v rozkazovacím způsobu pro obrazový model. Příklady: "
                "„Change the color of the dress to navy blue.“ · „Remove the people "
                "in the background.“ · „Make it a snowy winter scene with snow on "
                "the ground and trees.“ · „Blur the background.“ · „Turn the photo "
                "into an oil painting.“ Vrať jen tu anglickou větu."),
        config=config, timeout=60,
        options={"temperature": 0.1, "num_predict": 80, "num_ctx": 2048})
    out = (out or "").strip().strip('"“”„').splitlines()
    out = out[0].strip() if out else ""
    if not out or not re.search(r"[a-zA-Z]{3}", out):
        return None
    return out.rstrip(".") + _zachovat(out)


# HANS_FOTO_EDIT_VERBS_V1 — u změny CELÉHO obrazu (styl, zima, noc) by
# „keep everything else unchanged“ úpravu brzdilo; drží se jen kompozice
_CELY_OBRAZ = re.compile(r"\b(paint\w*|style|sketch|drawing|watercolor|comic|cartoon|"
                         r"winter|snow\w*|night\w*|evening|sunset|autumn|summer|spring|"
                         r"black and white|sepia|vintage|scene)\b", re.I)


def _zachovat(en: str) -> str:
    if _CELY_OBRAZ.search(en or ""):
        return ". Keep the composition, people and their faces the same."
    return ". Keep everything else in the photo unchanged."


def _pro_comfy(path: str) -> Optional[str]:
    """Kopie pro ComfyUI: otočená podle EXIF (fotky z mobilu), nejdelší strana
    ≤ 1568 px, PNG. Vrací dočasnou cestu."""
    try:
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        im.thumbnail((1568, 1568))
        tmp = str(DIR / ("_comfy_%d.png" % int(time.time() * 1000)))
        im.save(tmp)
        return tmp
    except Exception as e:
        _log.warning("foto: příprava pro ComfyUI selhala: %s", e)
        return None


def _zrus_zaseknute(config: dict, base: str):
    """HANS_FOTO_STUCK_CLEANUP_V1 (28. 9.) — po vypršení úprava v ComfyUI
    BĚŽELA DÁL (zaseknutá v načítání FLUXu, swap PC 20 GB) a držela 14,8 GB
    VRAM → hans-czech „Load failed“, Hans bez mozku, dokud ComfyUI někdo
    ručně nerestartoval. `/free` sám nestačí, úloha se musí zrušit; přerušení
    platí až mezi uzly, proto když fronta neopadne, restart služby."""
    import urllib.request as _ur
    def _post(cesta, data):
        try:
            _ur.urlopen(_ur.Request(base + cesta, data=json.dumps(data).encode(),
                                    headers={"Content-Type": "application/json"}),
                        timeout=15).read()
        except Exception:
            pass
    _post("/interrupt", {})
    _post("/queue", {"clear": True})
    for _ in range(4):
        time.sleep(5)
        try:
            with _ur.urlopen(base + "/queue", timeout=10) as r:
                if not json.loads(r.read().decode()).get("queue_running"):
                    _log.info("foto: zaseknutá úloha ComfyUI zrušena")
                    return
        except Exception:
            break
    try:
        from scripts import pc_remote
        pc_remote.run(config, "systemctl --user restart comfyui")
        _log.warning("foto: úloha ComfyUI nešla zrušit → služba ComfyUI restartována")
        time.sleep(15)
    except Exception as e:
        _log.warning("foto: restart ComfyUI selhal: %s", e)


# HANS_FOTO_FAIL_REASON_V1 (28. 9., pokyn uživatele) — Hans při každém selhání
# úpravy tvrdil „počítač je vypnutý nebo se hraje“, i když úprava jen nestihla
# doběhnout (málo RAM na PC při souběhu s tréninkem). Důvod se teď pamatuje
# a odpověď říká, co se skutečně stalo.
_DUVODY = {
    "vypnuto": "Úpravy fotek mám teď vypnuté.",
    "hra": "Na počítači se teď hraje, grafiku mu nechci brát. Zkuste to prosím, až dohraje.",
    "pokyn": "Nerozuměl jsem, co přesně mám na fotce změnit. Napište to prosím jinak, "
             "třeba „změň barvu kabelky na modrou“.",
    "pc": "Počítač s grafikou je vypnutý nebo na něm neběží kreslicí program, "
          "takže fotku teď upravit nemůžu.",
    "priprava": "Fotku se mi nepodařilo připravit k úpravě.",
    "nedobehla": "Úprava nestihla doběhnout — počítač má teď nejspíš málo paměti "
                 "(běží na něm jiná práce). Zkuste to prosím později.",
    "chyba": "Při úpravě se něco pokazilo a výsledek nevznikl. Zkuste to prosím znovu.",
    "cerna": "Úprava vyšla jako prázdný černý obrázek (výpočet na grafice se pokazil), "
             "takže vám ho neposílám. Zkuste to prosím znovu.",
}
_posledni_duvod = ""


def duvod_selhani() -> str:
    """Česká věta, proč poslední `uprav` nevrátila fotku."""
    return _DUVODY.get(_posledni_duvod, _DUVODY["chyba"])


def uprav(config: dict, path: str, pokyn: str, en: str = "") -> Optional[str]:
    """HANS_FOTO_EDIT_V1 — upravená fotka (cesta v data/foto) nebo None
    (herní mód, PC/ComfyUI dole, model chybí, render selhal). Nahraný originál
    se z PC hned smaže (ComfyUI input/). Důvod None → `duvod_selhani()`."""
    global _posledni_duvod
    import uuid
    c = _cfg(config)
    _posledni_duvod = "chyba"
    if not c.get("edit_enabled", True):      # vypnuto (např. do prvního testu)
        _posledni_duvod = "vypnuto"
        return None
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _log.info("foto: úprava odložena — herní mód")
            _posledni_duvod = "hra"
            return None
    except Exception:
        pass
    en = en or _pokyn_en(config, pokyn)    # HANS_FOTO_DOPORUCENI_V1: en z posudku
    if not en:
        _log.warning("foto: pokyn k úpravě nešel přeložit: %.80s", pokyn)
        _posledni_duvod = "pokyn"
        return None
    from scripts.avatar_render import (_comfy_url, _comfy_upload_image, _comfy_submit,
                                       _comfy_wait, _first_image, _comfy_fetch_image,
                                       _ollama_loaded, _ollama_unload, _comfy_free,
                                       _ollama_warm)
    from scripts.hans_art import _comfy_ready
    if not _comfy_ready(config):
        _posledni_duvod = "pc"
        return None
    tmp = _pro_comfy(path)
    if not tmp:
        _posledni_duvod = "priprava"
        return None
    base = _comfy_url(config)
    dest = str(DIR / ("%d_uprava.png" % int(time.time() * 1000)))
    name, ok, t0 = None, False, time.time()
    loaded = _ollama_loaded(config)
    _ollama_unload(config, loaded)
    try:
        name = _comfy_upload_image(base, tmp)
        if name:
            wf = _kontext_workflow(name, en, uuid.uuid4().int % (2 ** 31), c)
            _log.info("foto: úprava start — %s", en)
            pid = _comfy_submit(base, wf, uuid.uuid4().hex)
            hist = _comfy_wait(base, pid, timeout=int(c.get("render_timeout", 600))) if pid else None
            img = _first_image(hist) if hist else None
            ok = bool(img) and _comfy_fetch_image(base, img, dest)
            # HANS_FOTO_BLACK_GUARD_V1 (29. 9.) — FLUX Kontext ve fp16 na RX 6800
            # občas přeteče (NaN, ComfyUI „invalid value encountered in cast“)
            # a vrátí celý černý obrázek; 29. 9. se takový poslal jako hotový.
            # Jednobarevný výsledek = selhání → fronta zkusí znovu s jiným seedem.
            _cerna = False
            if ok:
                try:
                    from PIL import Image as _Im
                    with _Im.open(dest) as _im:
                        _ext = _im.convert("RGB").getextrema()
                    if all(lo == hi for lo, hi in _ext):
                        _log.warning("foto: úprava vyšla jednobarevná %s (NaN?) → selhání", _ext)
                        Path(dest).unlink(missing_ok=True)
                        ok = False
                        _cerna = True
                        _posledni_duvod = "cerna"
                except Exception as _be:
                    _log.debug("foto: kontrola černé: %s", _be)
            if not ok and not _cerna:
                _log.warning("foto: úprava nedoběhla (pid=%s, hist=%s)", pid, bool(hist))
                _posledni_duvod = "nedobehla" if pid else "pc"
                _zrus_zaseknute(config, base)        # HANS_FOTO_STUCK_CLEANUP_V1
    except Exception as e:
        _log.warning("foto: úprava selhala: %s", e)
    finally:
        _comfy_free(config)
        _ollama_warm(config, (config.get("models", {}) or {}).get("dialog", "hans-czech:latest"))
        try:
            Path(tmp).unlink(missing_ok=True)
        except Exception:
            pass
        if name:   # soukromí: originál na PC nenechávat
            try:
                from scripts import pc_remote
                pc_remote.run(config, "rm -f ~/ComfyUI/input/%s" % re.sub(r"[^\w.\-]", "", name))
            except Exception:
                pass
    if ok:
        _log.info("foto: úprava hotova za %.0f s", time.time() - t0)
        return dest
    return None
