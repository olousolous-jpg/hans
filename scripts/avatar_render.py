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

# HANS_NEG_SPLIT_V1 (5.8.) — negativ rozdělen na OBECNÝ a AVATAROVÝ.
# Doloženo: `_NEG` (celý, včetně avatarové části) se sdílel do KAŽDÉHO rendru
# přes `_comfy_workflow*`, takže když Hans maloval Salvadora Dalího, měl
# v negativním promptu „moustache" — aktivně si odmazával nejcharakterističtější
# rys té osoby (na obraze z 4.8. knír opravdu chybí a podoba nesedí; totéž
# vousy u Jacka Blacka, plnovous u Gandalfa). Anti-drift patří JEN na Hansovu
# vlastní tvář, ne na portréty cizích lidí.
_NEG_BASE = ("lowres, blurry, deformed, extra limbs, bad anatomy, watermark, "
             "text, signature, multiple people, nsfw")

# AVATAR_ACTIVITY_V1 — anti-drift identity: reading kontext táhl „učence"
# (brýle, knírek, starší/hubenější). Drž čistou tvář napříč rendery.
_NEG_AVATAR = "glasses, eyeglasses, moustache, mustache, beard, facial hair"

# Výchozí = obojí → avatarové cesty (avatar_render, paint_self) se nemění.
_NEG = _NEG_BASE + ", " + _NEG_AVATAR

# Pole descriptoru, co tvoří popis vzhledu (anglicky).
_APPEARANCE = ("role", "attire", "age_look", "build", "demeanor", "setting",
               "palette", "identity_anchor")


def _acfg(config: dict) -> dict:
    return config.get("hans_avatar", {}) or {}


def _comfy_url(config: dict) -> str:
    return _acfg(config).get("comfyui_url", "http://127.0.0.1:8188").rstrip("/")


def render_status(config: dict, timeout: float = 3.0) -> Optional[dict]:
    """HANS_RENDER_STATUS_V1 (5.8.) — běží právě render obrazu?

    Ptáme se ComfyUI na frontu (`/queue`), ne vlastního stavu v procesu:
    render může spustit hans_art, hans_maker i avatar, a Hans se navíc mohl
    mezitím restartovat — fronta na PC je jediná pravda, která to všechno vidí.

    Vrací {"running": bool, "pending": int, "prompt": str} nebo None, když
    ComfyUI neodpovídá (PC spí / zatuhlý) — None = NEVÍM, ne „nemaluje".
    Krátký timeout schválně: tohle visí na /stav, nesmí ho zdržet.

    ⚠️ Měřeno 5.8.: None NEZNAMENÁ jen „PC dole". Během renderu fronta
    odpovídá normálně, ale v okamžiku DOKONČENÍ (VAE decode + uvolnění VRAM)
    ComfyUI na pár sekund přestane odbavovat HTTP a probe vyprší. Proto se
    při None radši mlčí, než aby se tvrdilo „nemaluji" — volající to nesmí
    číst jako zápor.
    """
    try:
        import requests
        r = requests.get("%s/queue" % _comfy_url(config), timeout=timeout)
        if not r.ok:
            return None
        q = r.json() or {}
    except Exception:
        return None
    running = q.get("queue_running") or []
    pending = q.get("queue_pending") or []
    out = {"running": bool(running), "pending": len(pending), "prompt": ""}
    # z běžící úlohy vytáhni POZITIVNÍ prompt: negativ poznáme podle toho, že
    # začíná naším `_NEG_BASE` (nespoléhat na číslo uzlu — grafy se liší)
    try:
        graph = running[0][2] if running and len(running[0]) > 2 else {}
        for node in (graph or {}).values():
            if (node or {}).get("class_type") != "CLIPTextEncode":
                continue
            txt = ((node.get("inputs") or {}).get("text") or "").strip()
            if txt and not txt.startswith(_NEG_BASE[:20]):
                out["prompt"] = txt
                break
    except Exception:
        pass
    return out


def _ollama_url(config: dict) -> str:
    """HANS_ART_OLLAMA_URL_FIX_V1 (5.8.) — SDÍLENÁ resoluce s `ollama_client`.

    Doloženo 5.8. měřením při živém renderu: obě lokální klíče (`models.
    base_url`, `openwebui_direct.ollama_url`) v configu NEEXISTUJÍ, takže se
    tady vždy vracelo `127.0.0.1:11434` — tedy Ollama na PI, ne na PC. Celý
    VRAM handoff kolem renderu proto mířil na špatný stroj: `_ollama_loaded`
    vracel prázdno, `_ollama_unload` neměl co uvolnit a `_ollama_warm` psal
    do prázdna. Dokud na Pi běžel mini model, aspoň to tiše odpojovalo JEHO;
    po jeho odstranění (5.8.) byl handoff úplně inertní.

    Následek: hans-czech (8 GB) zůstával na PC v VRAM přes celý render, FLUX
    se nevešel a mlel v lowvram — naměřeno 475 s místo ~70 s. Tohle je pravý
    kořen „render timeoutů", ne pomalý ComfyUI.

    Pravdu o URL má `ollama_client._resolve_url` (čte `openwebui_chat.base_url`)
    → jedna resoluce místo dvou, které si nesedly."""
    for key, sub in (("models", "base_url"),
                     ("openwebui_direct", "ollama_url")):
        val = (config.get(key, {}) or {}).get(sub)
        if val:
            return str(val).rstrip("/")
    try:
        from scripts.ollama_client import _resolve_url
        return _resolve_url(None, config)
    except Exception:
        return "http://127.0.0.1:11434"


# ── Prompt z descriptoru ────────────────────────────────────────────────────
# AVATAR_STYLE_ANCHOR_V1 — explicitní stylová kotva drží JEDNOTNÝ medium (renderovaný
# 3D, barva, šedé pozadí) napříč seedy/prompty. Bez ní malá změna promptu překlápěla
# styl do komiksu/grayscale (nálady měly jen štěstí na seed 42).
_STYLE = ("full color, stylized 3d character render, semi-realistic, smooth "
          "volumetric shading, soft studio lighting, plain grey background")


def _resize_to_temp(path: str, max_side: int = 1024) -> Optional[str]:
    """Zmenši obrázek na ~max_side (SDXL nativní) a ulož do /tmp PNG pro upload.

    AVATAR_IDENTITY_REF_V1 — přesunuto sem z hans_art: potřebují ji OBA (art
    i render avatara), a hans_art z tohohle modulu už importuje comfy helpery,
    takže opačný směr by byl kruhový import."""
    try:
        import cv2
        img = cv2.imread(path)
        if img is None:
            return None
        h, w = img.shape[:2]
        s = max_side / float(max(h, w))
        if s < 1.0:
            img = cv2.resize(img, (int(w * s), int(h * s)),
                             interpolation=cv2.INTER_AREA)
        out = os.path.join("/tmp", "hans_ref_%d.png" % (int(time.time())))
        cv2.imwrite(out, img)
        return out
    except Exception as e:
        _log.warning("avatar: resize reference selhal: %s", e)
        return None


def identity_reference(config: dict, version: int) -> str:
    """AVATAR_IDENTITY_REF_V1 — tvář, ze které se má nová verze odvodit.

    Pořadí: explicitní `hans_avatar.identity_reference` → nejnovější
    vyrenderované `data/avatar/vN/idle.png` STARŠÍ než `version`. Hledá se
    starší schválně: verze, kterou právě renderujeme, může mít z dřívějšího
    (nepovedeného) běhu vlastní idle.png a ta by se sama sobě stala kotvou."""
    ref = str((config.get("hans_avatar", {}) or {}).get("identity_reference", "")).strip()
    if ref and os.path.exists(ref):
        return ref
    for v in range(int(version) - 1, 0, -1):
        p = os.path.join("data", "avatar", "v%d" % v, "idle.png")
        if os.path.exists(p):
            return p
    return ""


def _wait_for_comfy(base: str, limit_s: float = 180.0) -> bool:
    """AVATAR_RENDER_RETRY_V1 — počkej, až ComfyUI po pádu zase odpovídá."""
    t0 = time.time()
    while time.time() - t0 < limit_s:
        try:
            urllib.request.urlopen(f"{base}/system_stats", timeout=6).read()
            return True
        except Exception:
            time.sleep(5)
    return False


def _upload_reference(base: str, ref_path: str) -> Optional[str]:
    """AVATAR_IDENTITY_REF_V1 — zmenši referenční tvář a nahraj do ComfyUI.
    Vrací jméno, pod kterým ji zná ComfyUI (pro LoadImage), nebo None.
    Po restartu ComfyUI se volá znovu (AVATAR_RENDER_RETRY_V1)."""
    if not ref_path:
        return None
    tmp = _resize_to_temp(ref_path)
    if not tmp:
        return None
    try:
        return _comfy_upload_image(base, tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


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
    """keep_alive=0 → modely se po (prázdném) requestu uvolní z VRAM.

    HANS_RENDER_VRAM_LOCK_V1 (5.8.) — uvolnit VRAM NESTAČÍ: keepalive warmup
    (`ollama_client.warmup` á pár minut i chatový ping) model během renderu
    zase napinuje. Doloženo 5.8.: FLUX render startoval 16:13:51, v 16:14:39
    byl hans-czech korektně venku, ale v 16:15:24 už měl ComfyUI k dispozici
    jen 2,4 GB VRAM a 9,2 GB transformeru odložil do RAM → render mlel přes
    45 minut, Hans na něj po `render_timeout` rezignoval a ComfyUI zůstal
    obsazený zaseklou úlohou, takže nešlo malovat vůbec.

    Proto se s uvolněním rovnou USPÍ warmup (`pause_warmup`) — stejný handoff,
    jaký má noční base-model dávka (HANS_WARMUP_PAUSE_V1). Pauza má auto-expiry
    (render_timeout + rezerva), aby se sama zahojila i u volajících, kteří
    model zpátky nenahřívají (room_observer, hans_place, enrollment).
    `_ollama_warm` ji ruší (`resume_warmup`) = konec GPU práce.

    ⚠️ Nekryje REÁLNÝ CHAT: když uživatel během renderu napíše, model se
    nahraje (keep_alive=-1) a VRAM zase chybí. Pauza zabíjí jen automatické
    re-piny, což byl doložený případ."""
    try:
        from scripts.ollama_client import pause_warmup
        _rt = float((config.get("hans_avatar", {}) or {}).get(
            "render_timeout", 600))
        pause_warmup(_rt + 180)
    except Exception as _pe:
        _log.debug("avatar: pause_warmup selhal: %s", _pe)
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


def _comfy_reclaim_gpu(config: dict, after_render: bool = True) -> bool:
    """COMFY_GPU_RECLAIM_V1 (14.8.) — `/free` vrátí VRAM, ale NE FRONTY na GPU.

    DOLOŽENO A ZREPRODUKOVÁNO (14.8. 07:58–08:04, řízený pokus):
        výchozí stav          GPU  0 %   7 W    0 hlášek jádra
        FLUX render           OK
        po renderu + /free    GPU 99 %  50 W  138× „amdgpu: Runlist is getting
                                              oversubscribed due to too many queues"
        +60 s klidu           GPU 99 %  45 W  ← samo NESPADNE
        restart ComfyUI       GPU  0 %   7 W  ← okamžitě čisté
    Týž otisk měl provozní incident 13.8. večer: hlášky od 22:35:50 (render start
    22:35:52) běžely ještě HODINU po dokončení renderu a `pc_busy` kvůli tomu
    hlásil „něco zatěžuje grafiku" → odložené vypnutí PC se nikdy neprovedlo
    a stroj běžel celou noc. V noci bez malování (analytika) je hlášek NULA.

    Rozlišovač: **příkon**, ne procenta. Rezidentní model v klidu = 45–51 W,
    skutečná inference 150–219 W. Zaseklý runlist = 99 % při ~45 W, tedy
    „vytížení bez práce" → restartovat. Když je příkon vysoký, něco opravdu
    počítá a NESAHÁME na to.

    Potvrzuje starší poznámku „ComfyUI po FLUXu nevrátí VRAM" (5.8.) — táž
    rodina, tentýž viník; teď doložená v provozu i v kernel logu.
    """
    try:
        from scripts import pc_remote
    except Exception:
        return False
    cfg = _acfg(config) or {}
    if not bool(cfg.get("gpu_reclaim", True)):
        return False
    # Restartovat smíme JEN ComfyUI běžící na tomtéž stroji, na který máme SSH —
    # jinak bychom sahali na cizí/lokální službu.
    host = str(((config or {}).get("pc_remote", {}) or {}).get("host", "") or "")
    if not host or host not in _comfy_url(config):
        return False
    busy_pct = float(cfg.get("gpu_reclaim_pct", 15.0))
    # COMFY_RECLAIM_WATT_FLOOR_V1 (18.8.) — práh 100 W byl NAD reálnou prací, tak
    # se restartovalo do běžícího výpočtu. Data ze 17.–18.8. (7 zásahů) separují
    # obě populace čistě:
    #   • zásek runlistu:  99 % při 49 W  (2×, restart oprávněný)
    #   • reálná práce:  19–53 % při 74–95 W  (5×, 19:37–20:18 á 10 min — restart
    #     nic nespravil, protože nebylo co spravovat; nejspíš Ollama inference)
    # 65 W leží mezi nimi s rezervou na obě strany. Drží princip
    # [[gpu-busy-percent-lies]]: rozhoduje PŘÍKON, procenta lžou.
    work_w = float(cfg.get("gpu_reclaim_work_watt", 65.0))

    def _read():
        try:
            out = pc_remote.run(config, (
                "cat /sys/class/drm/card*/device/gpu_busy_percent 2>/dev/null | head -1; "
                "/opt/rocm/bin/rocm-smi --showpower 2>/dev/null | "
                "grep -oE '[0-9]+\\.[0-9]+' | head -1"), timeout=15)
            vals = [v.strip() for v in (out or "").splitlines() if v.strip()]
            return (float(vals[0]) if len(vals) > 0 else None,
                    float(vals[1]) if len(vals) > 1 else None)
        except Exception as e:
            _log.debug("comfy reclaim: stav GPU nezjištěn (%s)", e)
            return None, None

    # COMFY_RECLAIM_SETTLE_V1 (14.8.) — NEMĚŘIT HNED PO `/free`. Zaseklý runlist
    # se projeví se ZPOŽDĚNÍM: první ostrý běh (08:19) změřil GPU okamžitě po
    # uvolnění, přečetl hodnotu pod prahem a TIŠE se vrátil — a grafika pak
    # vylezla na 99 % a zůstala tam (ověřeno ručním voláním o minutu později,
    # které restart provedlo správně). Reprodukční skript, který měřil až po 5 s,
    # 99 % viděl. Proto: nech to usadit a při nízké hodnotě zkus ještě jednou.
    # COMFY_RECLAIM_PERIODIC_V1 (14.8.) — `after_render=False` je PERIODICKÁ
    # kontrola (health watcher á 10 min). Tam se nečeká na usazení ani nezkouší
    # podruhé: zaseklý stav je v tu chvíli už dávno ustálený, kdežto hned po
    # renderu naskakuje se zpožděním. Pojistka pro případ, že úklid po renderu
    # neproběhl — Hans se restartoval uprostřed, selhalo SSH, nebo měl ComfyUI
    # frontu. Bez ní by zaseklý runlist visel do dalšího renderu a stál ~40 W.
    if after_render:
        time.sleep(float(cfg.get("gpu_reclaim_settle_s", 4.0)))
    busy, watt = _read()
    if after_render and busy is not None and busy < busy_pct:
        time.sleep(float(cfg.get("gpu_reclaim_retry_s", 6.0)))
        busy2, watt2 = _read()
        if busy2 is not None:
            busy, watt = busy2, watt2
    if busy is None:
        return False
    if busy < busy_pct:
        return False                 # GPU spadla sama → nic neřešíme
    if watt is not None and watt >= work_w:
        _log.info("avatar: GPU %.0f %% při %.0f W — něco opravdu počítá, "
                  "ComfyUI nerestartuji", busy, watt)
        return False
    # COMFY_RECLAIM_QUEUE_GUARD_V1 (14.8.) — NIKDY nerestartuj, když má ComfyUI
    # co dělat. Render umí spustit hans_art, hans_maker i avatar a klidně z JINÉHO
    # procesu (Hans běží pořád, testy zvlášť) — restart uprostřed cizí úlohy ji
    # zabije („úloha zmizela, server nejspíš restartoval"). Fronta na PC je jediná
    # pravda, která vidí všechny (HANS_RENDER_STATUS_V1). Nevím ≠ prázdno: když
    # se stav nepodaří zjistit, radši NErestartuji.
    try:
        st = render_status(config)
    except Exception:
        st = None
    if st is None:
        _log.info("avatar: stav fronty ComfyUI neznámý → nerestartuji")
        return False
    if st.get("running") or int(st.get("pending") or 0) > 0:
        _log.info("avatar: ComfyUI má práci (running=%s, pending=%s) → "
                  "nerestartuji", st.get("running"), st.get("pending"))
        return False
    _log.warning("avatar: GPU uvízla na %.0f %% při %.0f W (fronty po renderu) "
                 "→ restartuji ComfyUI, ať se grafika uvolní", busy, watt or -1)
    if pc_remote.run(config, "systemctl --user restart comfyui", timeout=25) is None:
        _log.warning("avatar: restart ComfyUI se nezdařil")
        return False
    # Počkej, až se zvedne — jinak by další render narazil na mrtvou službu.
    base = _comfy_url(config)
    for _ in range(15):
        time.sleep(2)
        try:
            urllib.request.urlopen(f"{base}/system_stats", timeout=5).read()
            _log.info("avatar: ComfyUI zpět po restartu, grafika uvolněna")
            return True
        except Exception:
            continue
    _log.warning("avatar: ComfyUI po restartu do 30 s neodpověděl")
    return True


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
        _comfy_reclaim_gpu(config)   # COMFY_GPU_RECLAIM_V1
    except Exception as _e:
        _log.debug("avatar: ComfyUI /free selhal: %s", _e)


def _ollama_warm(config: dict, model: str) -> None:
    # HANS_RENDER_VRAM_LOCK_V1 — konec GPU práce → warmup smí zase pinovat.
    try:
        from scripts.ollama_client import resume_warmup
        resume_warmup()
    except Exception as _re:
        _log.debug("avatar: resume_warmup selhal: %s", _re)
    # AVATAR_WARM_GAME_GUARD_V1 (13.8.) — NEPINUJ model zpět, když se HRAJE.
    # Tohle je přímé urllib volání MIMO `ollama_client`, takže se na něj
    # herní gate nevztahuje — přesně footgun, před kterým varuje CLAUDE.md
    # („každý přímý HTTP na Ollamu mimo ollama_client MUSÍ mít vlastní
    # game_mode_on() guard"). Doloženo 13.8. 14:22: uprostřed 5hodinové hry
    # snímal room_observer místnost, model se odložil a hned „nahřál zpět"
    # s keep_alive=-1 → 9,6 GB ve VRAM (13,4 z 17,2 GB obsazeno) po celý
    # zbytek hry, ačkoli herní mód běžel. O pár řádků níž (ComfyUI, ř. ~585)
    # týž guard správně je. [[ollama-vram-tiers]]
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _log.info("avatar: hans-czech NEnahřívám zpět — běží hra "
                      "(VRAM patří jí); vrátí se po skončení herního módu")
            return
    except Exception as _ge:
        _log.debug("avatar: game_mode check selhal: %s", _ge)
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
                    steps: int, cfg: float, negative: str = "") -> dict:
    """Minimální SDXL txt2img graf (ComfyUI API format).
    negative="" → výchozí `_NEG` (obecný + avatarový anti-drift). Cesty, které
    malují CIZÍ osobu, si předají `_NEG_BASE` (HANS_NEG_SPLIT_V1)."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative or _NEG, "clip": ["4", 1]}},
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


# HANS_ART_FLUX_V1 — FLUX.1-dev (all-in-one fp8) txt2img graf. All-in-one
# checkpoint nese model+CLIP+VAE → CheckpointLoaderSimple jako u SDXL. Rozdíly
# proti SDXL: (a) FluxGuidance node místo CFG (FLUX jede CFG=1, negativ ignoruje);
# (b) EmptySD3LatentImage (16kanálový FLUX latent, NE 4kanálový EmptyLatentImage);
# (c) euler/simple sampler. Node "7" (prázdný negativ) ponechán, ať
# HANS_ART_LESSON_V1 injektáž negativu nespadne (u CFG=1 je stejně neúčinná).
def _comfy_workflow_flux(ckpt: str, prompt: str, seed: int, w: int, h: int,
                         steps: int, guidance: float) -> dict:
    """Minimální FLUX.1-dev txt2img graf (ComfyUI API format)."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptySD3LatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": "", "clip": ["4", 1]}},
        "10": {"class_type": "FluxGuidance",
               "inputs": {"conditioning": ["6", 0], "guidance": guidance}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": 1.0,
                         "sampler_name": "euler", "scheduler": "simple",
                         "denoise": 1.0, "model": ["4", 0],
                         "positive": ["10", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "PreviewImage",
              "inputs": {"images": ["8", 0]}},
    }


# HANS_ART_PULID_V1 — FLUX.1-dev + PuLID: zachovej PODOBU osoby z referenčního
# fota (ref_image v ComfyUI input/) a slož NOVOU scénu z promptu (osoba na
# motorce ap.). Řeší, že img2img drží kompozici → scénu nešlo přidat. Vyžaduje
# custom node ComfyUI_PuLID_Flux_ll + modely pulid_flux/EVA-CLIP/antelopev2.
# provider=CPU (onnxruntime tu nemá ROCm provider; face detekce na CPU stačí).
def _comfy_workflow_flux_pulid(ckpt: str, prompt: str, seed: int, w: int, h: int,
                               steps: int, guidance: float, ref_image: str,
                               pulid_weight: float = 0.9,
                               pulid_model: str = "pulid_flux_v0.9.1.safetensors",
                               provider: str = "CPU") -> dict:
    """FLUX+PuLID txt2img: podoba z ref_image, scéna z promptu."""
    return {
        "4":  {"class_type": "CheckpointLoaderSimple",
               "inputs": {"ckpt_name": ckpt}},
        "20": {"class_type": "PulidFluxModelLoader",
               "inputs": {"pulid_file": pulid_model}},
        "21": {"class_type": "PulidFluxEvaClipLoader", "inputs": {}},
        "22": {"class_type": "PulidFluxInsightFaceLoader",
               "inputs": {"provider": provider}},
        "23": {"class_type": "LoadImage", "inputs": {"image": ref_image}},
        "24": {"class_type": "ApplyPulidFlux",
               "inputs": {"model": ["4", 0], "pulid_flux": ["20", 0],
                          "eva_clip": ["21", 0], "face_analysis": ["22", 0],
                          "image": ["23", 0], "weight": float(pulid_weight),
                          "start_at": 0.0, "end_at": 1.0}},
        "6":  {"class_type": "CLIPTextEncode",
               "inputs": {"text": prompt, "clip": ["4", 1]}},
        "10": {"class_type": "FluxGuidance",
               "inputs": {"conditioning": ["6", 0], "guidance": guidance}},
        "7":  {"class_type": "CLIPTextEncode",
               "inputs": {"text": "", "clip": ["4", 1]}},
        "5":  {"class_type": "EmptySD3LatentImage",
               "inputs": {"width": w, "height": h, "batch_size": 1}},
        "3":  {"class_type": "KSampler",
               "inputs": {"seed": seed, "steps": steps, "cfg": 1.0,
                          "sampler_name": "euler", "scheduler": "simple",
                          "denoise": 1.0, "model": ["24", 0],
                          "positive": ["10", 0], "negative": ["7", 0],
                          "latent_image": ["5", 0]}},
        "8":  {"class_type": "VAEDecode",
               "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9":  {"class_type": "PreviewImage",
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
                            vae_name: str = "sdxl_vae.safetensors",
                            negative: str = "") -> dict:
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
        "7":  {"class_type": "CLIPTextEncode", "inputs": {"text": negative or _NEG, "clip": ["4", 1]}},
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


def _comfy_workflow_ipadapter(ckpt: str, prompt: str, seed: int, w: int, h: int,
                              steps: int, cfg: float, image_name: str,
                              ipadapter_file: str, clip_vision_name: str,
                              weight: float = 0.75,
                              weight_type: str = "linear",
                              negative: str = "") -> dict:
    """SDXL txt2img graf s IP-ADAPTEREM: prázdný latent (NOVÁ kompozice/póza dle
    dimenzí w×h) + referenční obrázek přes IP-Adapter → drží PODOBU reference,
    ale volná kompozice (např. celá postava z portrétní reference)."""
    return {
        "4": {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": ckpt}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode",
              "inputs": {"text": prompt, "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative or _NEG, "clip": ["4", 1]}},
        "10": {"class_type": "LoadImage",
               "inputs": {"image": image_name}},
        "11": {"class_type": "IPAdapterModelLoader",
               "inputs": {"ipadapter_file": ipadapter_file}},
        "12": {"class_type": "CLIPVisionLoader",
               "inputs": {"clip_name": clip_vision_name}},
        "13": {"class_type": "IPAdapterAdvanced",
               "inputs": {"model": ["4", 0], "ipadapter": ["11", 0],
                          "image": ["10", 0], "clip_vision": ["12", 0],
                          "weight": weight, "weight_type": weight_type,
                          "combine_embeds": "concat", "start_at": 0.0,
                          "end_at": 1.0, "embeds_scaling": "V only"}},
        "3": {"class_type": "KSampler",
              "inputs": {"seed": seed, "steps": steps, "cfg": cfg,
                         "sampler_name": "dpmpp_2m", "scheduler": "karras",
                         "denoise": 1.0, "model": ["13", 0],
                         "positive": ["6", 0], "negative": ["7", 0],
                         "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode",
              "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
    }


def _comfy_submit(base: str, workflow: dict, client_id: str) -> Optional[str]:
    data = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(f"{base}/prompt", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r).get("prompt_id")


# HANS_CHAT_WAIT_FOR_PAINT_V1 (5.8.) — „právě maluji, ozvu se, až domaluji".
# Příznak je IN-PROCESS a s deadlinem, ne dotaz na frontu ComfyUI. Důvod:
# fronta umí zůstat obsazená úlohou, kterou Hans dávno vzdal (doloženo 5.8.,
# job mlel 8 minut po jeho timeoutu) — a na tom by se chat zasekl natrvalo.
# Takhle drží jen tak dlouho, jak dlouho Hans SÁM na obraz čeká.
_render_until = 0.0


def render_in_progress() -> bool:
    """Čeká Hans právě na obraz? (Deadline se sám zahojí, kdyby úklid selhal.)"""
    return time.time() < _render_until


def _comfy_wait(base: str, prompt_id: str, timeout: int = 300) -> Optional[dict]:
    """Poll /history dokud render nedoběhne. Vrátí history záznam nebo None.

    HANS_COMFY_DEAD_JOB_V1 (5.8.) — pozná, že ComfyUI pod námi SPADL a úloha
    s ním. Doloženo: render psa začal 11:51:43, ComfyUI ve 12:01:54 spadl
    (systemd ho zvedl, NRestarts=1) a Hans dál čekal na soubor, který nikdy
    nepřijde — vyčerpal by celých 900 s a teprve pak spadl na fallback.
    Restart vyprázdní /history i frontu, takže „server odpovídá, ale NAŠE
    úloha zmizela z fronty A NENÍ v historii" = mrtvá úloha → skonči hned.
    Kontroluje se až po `grace` sekundách, aby se nezaměnil krátký okamžik
    mezi přijetím a zařazením do fronty."""
    global _render_until
    deadline = time.time() + timeout
    started = time.time()
    grace = 20.0
    _render_until = deadline          # chat od téhle chvíle ví, že maluju
    try:
        return _comfy_wait_loop(base, prompt_id, deadline, started, grace)
    finally:
        _render_until = 0.0


def _comfy_wait_loop(base: str, prompt_id: str, deadline: float,
                     started: float, grace: float) -> Optional[dict]:
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/history/{prompt_id}", timeout=15) as r:
                hist = json.load(r)
            if prompt_id in hist:
                return hist[prompt_id]
            if time.time() - started > grace and not _comfy_job_alive(base, prompt_id):
                _log.warning("ComfyUI: úloha %s zmizela (server nejspíš "
                             "restartoval) — nečekám do timeoutu", prompt_id[:8])
                return None
        except Exception as _e:
            _log.debug("avatar: history poll: %s", _e)
        time.sleep(2)
    return None


def _comfy_job_alive(base: str, prompt_id: str) -> bool:
    """Je naše úloha pořád ve frontě (běžící nebo čekající)? Chyba → True
    (nedostupná fronta NENÍ důkaz smrti; radši čekej dál)."""
    try:
        with urllib.request.urlopen(f"{base}/queue", timeout=10) as r:
            q = json.load(r)
    except Exception:
        return True
    for key in ("queue_running", "queue_pending"):
        for item in (q.get(key) or []):
            # položka fronty: [priorita, prompt_id, prompt, extra, outputs]
            if isinstance(item, (list, tuple)) and len(item) > 1 and item[1] == prompt_id:
                return True
    return False


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
    try:  # OLLAMA_GAME_MODE_V1 — ComfyUI render mimo Ollama gate → nezabírat VRAM za hry
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _log.info("avatar: herní mód — render odložen")
            return False
    except Exception:
        pass
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

    # AVATAR_IDENTITY_REF_V1 — podoba z PŘEDCHOZÍ tváře. Bez toho si plain
    # txt2img u každé verze vymyslí jiného člověka (doloženo v3: z šedovlasého
    # padesátníka mladík), protože `identity_anchor` je jen slovo v promptu.
    _ref_path = identity_reference(config, ver)
    _ref_name = None
    _ipa_w = float(acfg.get("identity_weight", 0.6))
    _ipa_model = acfg.get("identity_ipadapter_file",
                          "ip-adapter-plus_sdxl_vit-h.safetensors")
    _ipa_clip = acfg.get("identity_clip_vision",
                         "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors")
    _retries = max(1, int(acfg.get("render_retries", 3)))
    if _ref_path and _ipa_w > 0:
        _ref_name = _upload_reference(base, _ref_path)
        if _ref_name:
            _log.info("avatar: podoba z reference %s (IP-Adapter w=%.2f)",
                      _ref_path, _ipa_w)
        else:
            _log.warning("avatar: referenci %s se nepodařilo nahrát — "
                         "renderuji bez ní (tvář se může změnit)", _ref_path)
    else:
        _log.info("avatar: bez referenční tváře (v%d je první nebo vypnuto)", ver)

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
            # AVATAR_IDENTITY_REF_V1 — s referencí drž podobu, bez ní postaru
            if _ref_name:
                wf = _comfy_workflow_ipadapter(ckpt, prompt, _seed, w, h, steps,
                                               cfg_s, _ref_name, _ipa_model,
                                               _ipa_clip, _ipa_w)
            else:
                wf = _comfy_workflow(ckpt, prompt, _seed, w, h, steps, cfg_s)
            # AVATAR_RENDER_RETRY_V1 — ComfyUI padá na ROCm memory fault a bere
            # s sebou rozpracovanou úlohu; po jeho návratu týž obrázek projde.
            for _pokus in range(1, _retries + 1):
                if _pokus > 1:
                    if not _wait_for_comfy(base):
                        _log.warning("avatar: ComfyUI nenaběhl — %s vzdávám", fname)
                        break
                    # referenci je po restartu nutné nahrát znovu
                    if _ref_path and _ipa_w > 0:
                        _new_ref = _upload_reference(base, _ref_path)
                        if _new_ref:
                            _ref_name = _new_ref
                            wf = _comfy_workflow_ipadapter(
                                ckpt, prompt, _seed, w, h, steps, cfg_s,
                                _ref_name, _ipa_model, _ipa_clip, _ipa_w)
                    _log.info("avatar: %s — pokus %d/%d", fname, _pokus, _retries)
                try:
                    pid = _comfy_submit(base, wf, client_id)
                    hist = _comfy_wait(base, pid) if pid else None
                    img = _first_image(hist) if hist else None
                    if img and _comfy_fetch_image(base, img,
                                                  os.path.join(cache_dir, fname)):
                        n_ok += 1
                        _log.info("avatar: vyrenderován %s (%d/%d, v%d)",
                                  fname, n_ok, len(targets), ver)
                        break
                    _log.warning("avatar: %s se nevyrenderoval (pokus %d/%d)",
                                 fname, _pokus, _retries)
                except Exception as _e:
                    _log.warning("avatar: render %s selhal (pokus %d/%d): %s",
                                 fname, _pokus, _retries, _e)
    finally:
        # VRAM: nejdřív uvolni ComfyUI (SDXL drží ~7GB), JINAK se hans-czech nenahraje
        # → chat timeout (AVATAR_RENDER_COMFY_FREE_V1). Pak teprve vrať hans-czech.
        _comfy_free(config)
        # VRAM: vrať hans-czech (chat ready)
        _ollama_warm(config, config.get("models", {}).get("dialog", "hans-czech:latest"))

    # rendered=1 JEN když projdou VŠECHNY výrazy+nálady (jinak retry příště — deferral)
    if n_ok == len(targets):
        _mark_rendered(diary_db_path, ver)
        # HANS_AVATAR_ANIMATE_V1 (17.7.) — po úspěšném SDXL renderu regenerovat
        # i animované LivePortrait klipy (mrkání/talk/idle/talkloop) z nové vN
        # tváře. Async (thread), deferral-safe. Gate `hans_avatar.animate.enabled`.
        try:
            from scripts.hans_avatar_animate import (
                enabled as _anim_enabled, regenerate_clips_async as _anim_go)
            if _anim_enabled(config):
                _anim_go(ver, config)
                _log.info("avatar: HANS_AVATAR_ANIMATE_V1 spuštěn na pozadí pro v%d", ver)
        except Exception as _ae:
            _log.warning("avatar: animate hook selhal: %s", _ae)
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
