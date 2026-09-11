#!/usr/bin/env python3
"""Centrální Ollama klient — jednotný timeout, keep_alive, retry.

Použití:
    from scripts.ollama_client import ollama_chat, ollama_generate, ollama_warmup
"""
from __future__ import annotations

import logging
import os          # HANS_TRANSLATE_PRIORITY_V1 — getpid/kill u osirele pauzy
import threading   # HANS_WARMUP_PAUSE_REFCOUNT_V1 — zámek nad čítačem pauzy
import time
from pathlib import Path
from typing import Optional

import requests

from scripts._log_circuit import for_url as _breaker_for, is_conn_error

_log = logging.getLogger("ollama_client")

# ── OLLAMA_GAME_MODE_V1 — herní mód ────────────────────────
# Flag soubor = sdílený signál napříč procesy (Hans, web_admin, subprocess skripty).
# Když existuje, Hans NEvolá Ollamu → VRAM zůstane volná pro hru na PC.
_PAUSE_FLAG = Path(__file__).resolve().parent.parent / "data" / ".ollama_paused"


# HANS_TRANSLATE_PRIORITY_V1 (29.8.) — druhy vlastnik pauzy: PREKLAD.
# Poradi je HRA > PREKLAD > RUTINY. Flagy jsou ZAMERNE dva: binarni flag
# neunese dva vlastniky. S jednim by nastalo tohle — preklad zapne pauzu,
# mezitim zacne hra, watcher zapne herni mod (flag uz existuje, nic se
# nestane), preklad dojede a flag smaze → Hans si vleze do VRAM uprostred
# hry. Takhle kazdy maze jen svuj a guard cte oba.
_TRANSLATE_FLAG = Path(__file__).resolve().parent.parent / "data" / ".ollama_paused_translate"


def translate_pause_on() -> bool:
    """True = prave bezi preklad a Hansovy rutiny maji pockat."""
    try:
        return _TRANSLATE_FLAG.exists()
    except Exception:
        return False


def game_pause_on() -> bool:
    """True = HRA (ne preklad). Preklad podle toho pozna, ze ma cekat."""
    try:
        return _PAUSE_FLAG.exists()
    except Exception:
        return False


def set_translate_pause(on: bool, ollama_url: str | None = None,
                        config: dict | None = None) -> dict:
    """Pauza drzena PREKLADEM. Nesaha na herni flag — viz komentar vyse."""
    try:
        if on:
            _TRANSLATE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            # PID + cas: podle nich se pozna OSIRELY flag po spadlem prekladu.
            # Bez toho by Hans zustal bez mozku natrvalo.
            _TRANSLATE_FLAG.write_text("%d %f" % (os.getpid(), time.time()))
            time.sleep(0.4)
            freed = ollama_unload_all(ollama_url, config)
            _log.info("PREKLAD MA PREDNOST — rutiny pockaji, uvolneno %d modelu", freed)
            return {"translate_pause": True, "unloaded": freed}
        try:
            _TRANSLATE_FLAG.unlink()
        except FileNotFoundError:
            pass
        _log.info("preklad dobehl — Hansovy rutiny mohou zase bezet")
        return {"translate_pause": False}
    except Exception as exc:
        _log.error("set_translate_pause(%s) selhal: %s", on, exc)
        return {"error": str(exc)}


def clear_stale_translate_pause() -> bool:
    """Uvolni pauzu po prekladu, ktery uz nebezi. Vraci True, kdyz uklidil."""
    try:
        if not _TRANSLATE_FLAG.exists():
            return False
        pid = int((_TRANSLATE_FLAG.read_text().split() or ["0"])[0])
        if pid > 0:
            try:
                os.kill(pid, 0)      # jen test existence, signal se neposila
                return False         # proces bezi → pauza je opravnena
            except ProcessLookupError:
                pass
            except PermissionError:
                return False         # bezi pod jinym uzivatelem
        _TRANSLATE_FLAG.unlink()
        _log.warning("uklizena osirela pauza po prekladu (PID %s uz nebezi)", pid)
        return True
    except Exception as exc:
        _log.debug("clear_stale_translate_pause: %s", exc)
        return False


def game_mode_on() -> bool:
    """True = Ollama se nepouziva. Drzi ji bud HRA, nebo PREKLAD."""
    try:
        return _PAUSE_FLAG.exists() or _TRANSLATE_FLAG.exists()
    except Exception:
        return False


def ollama_unload_all(ollama_url: str | None = None,
                      config: dict | None = None) -> int:
    """Uvolni VŠECHNY právě nahrané modely z VRAM (keep_alive=0). Vrátí počet."""
    url = _resolve_url(ollama_url, config)
    models = []
    try:
        r = requests.get(f"{url}/api/ps", timeout=10)
        r.raise_for_status()
        models = [m.get("model") or m.get("name")
                  for m in (r.json() or {}).get("models", [])]
    except Exception as exc:
        # HANS_UNLOAD_QUIET_V1 (5.8.) — když Ollama vůbec neběží (noční
        # shutdown PC / herní mód), NENÍ co uvolňovat a hláška je šum: 138
        # WARNINGů za noc 4.→5.8., tj. 90 % všech. Nedostupný endpoint =
        # DEBUG; skutečné chyby (běžící server odpoví chybou) zůstávají
        # WARNING, ať se neschová něco reálného.
        _unreachable = isinstance(
            exc, (requests.exceptions.ConnectionError,
                  requests.exceptions.Timeout))
        if _unreachable:
            _log.debug("unload_all: Ollama nedostupná (%s) — není co uvolnit",
                       type(exc).__name__)
        else:
            _log.warning("unload_all: /api/ps selhal: %s", exc)
    n = 0
    for m in models:
        if not m:
            continue
        try:
            requests.post(f"{url}/api/generate",
                          json={"model": m, "prompt": "", "keep_alive": 0},
                          timeout=30)
            _log.info("Ollama unload: %s", m)
            n += 1
        except Exception as exc:
            _log.warning("unload %s selhal: %s", m, exc)
    return n


def loaded_vram(ollama_url: str | None = None,
                config: dict | None = None) -> list:
    """Co právě drží VRAM: [{'name':..., 'gb':...}] pro modely se size_vram>0.
    Chyba → []. Slouží k ověření, že herní mód reálně uvolnil grafiku."""
    url = _resolve_url(ollama_url, config)
    try:
        r = requests.get(f"{url}/api/ps", timeout=10)
        r.raise_for_status()
        out = []
        for m in (r.json() or {}).get("models", []):
            vram = int(m.get("size_vram", 0) or 0)
            if vram > 0:
                out.append({"name": m.get("model") or m.get("name") or "?",
                            "gb": round(vram / 1e9, 1)})
        return out
    except Exception as exc:
        _log.warning("loaded_vram: /api/ps selhal: %s", exc)
        return []


def set_game_mode(on: bool, ollama_url: str | None = None,
                  config: dict | None = None) -> dict:
    """Zapni/vypni herní mód. on=True: vytvoř flag (Hans přestane volat Ollamu) +
    uvolni VRAM. on=False: smaž flag (mozek zase k dispozici)."""
    try:
        if on:
            _PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
            _PAUSE_FLAG.write_text(str(time.time()))   # flag PRVNÍ → nové volání se gate
            time.sleep(0.4)
            freed = ollama_unload_all(ollama_url, config)
            _log.info("HERNÍ MÓD ZAP — uvolněno %d modelů, Ollama se nepoužívá", freed)
            _log_game_mode_diary(config, True)
            return {"game_mode": True, "unloaded": freed}
        try:
            _PAUSE_FLAG.unlink()
        except FileNotFoundError:
            pass
        _log.info("HERNÍ MÓD VYP — Ollama opět k dispozici")
        _log_game_mode_diary(config, False)
        return {"game_mode": False}
    except Exception as exc:
        _log.error("set_game_mode(%s) selhal: %s", on, exc)
        return {"error": str(exc)}


def _log_game_mode_diary(config: dict | None, on: bool) -> None:
    """HANS_GAME_MODE_DIARY_V1 — zaznamenej přepnutí herního módu do deníku.
    NEUTRÁLNĚ (jen fakt přepnutí, ŽÁDNÉ pre-vysvětlení následku) — aby případné
    budoucí odvození souvislosti (herní mód ↔ výpadek mozku) bylo GENUINNÍ, ne
    parafráze zadaného faktu. Best-effort, čistý SQL (funguje i bez mozku)."""
    try:
        cfg = config or {}
        db = (cfg.get("diary_db")
              or (cfg.get("hans_idle", {}) or {}).get("diary_db")
              or "data/hans_diary.db")
        note = "Zapnul jsem herní mód." if on else "Vypnul jsem herní mód."
        import sqlite3
        conn = sqlite3.connect(db, timeout=5.0)
        conn.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) "
            "VALUES (?,?,?,?,?)",
            (time.time(), "game_mode", "herní mód", note, "on" if on else "off"))
        conn.commit()
        conn.close()
    except Exception as exc:
        _log.debug("_log_game_mode_diary: %s", exc)

# ── Defaults ───────────────────────────────────────────────
DEFAULT_URL     = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT = 120          # sekundy
DEFAULT_KEEP_ALIVE = -1         # drž model v VRAM napořád  # KEEPALIVE_FIX_V2
MAX_RETRIES     = 1            # 1 retry při timeout (celkem 2 pokusy)
CONNECT_TIMEOUT = 3            # OLLAMA_CONNECT_TIMEOUT_V1 — s, jen na navázání
# spojení. Vypnutý PC se tím pozná za sekundy místo za celý read timeout (doloženo
# 22.8. v noci: sen čekal 2×120 s na mrtvý stroj, než sáhl po fallbacku). Read mez
# zůstává plná — pomalou inferenci tahle změna ZÁMĚRNĚ nezkracuje.

# HANS_WARMUP_PAUSE_V1 — VRAM handoff: uspat keepalive warmup, dokud noční
# base-model analytika drží VRAM. Bez toho 4min pin hans-czech (8GB) evictuje
# base OpenEuroLLM (8GB) uprostřed generování (8+8 > 16GB VRAM) → 300s timeouty.
_warmup_pause_until = 0.0
# HANS_WARMUP_PAUSE_REFCOUNT_V1 (8.9.) — pauzu drží VÍC dávek najednou a
# `resume_warmup` ji dřív NULOVAL bez ohledu na to, kdo ji nastavil: kdo
# skončil první, odemkl keepalive i všem ostatním. Doloženo v noci 8.9.:
# studium si vzalo pauzu ve 03:01:39 (na 20 min), immune doběhl po DVOU
# sekundách ve 03:02:33 a svým `finally: resume_warmup()` ji zrušil → od
# 03:05:57 se hans-czech re-pinoval á 4 min doprostřed studijní session →
# 8+8 > 16 GB → read timeouty 300/300/90 s a DVA výpisky z odborných prací
# spadly na anglický abstrakt místo Hansova výpisku.
# Fix: počítadlo držitelů. Pauza padne až když ji pustí POSLEDNÍ.
# ⚠️ `_warmup_pause_until` zůstává jako auto-expiry STROP — nepárové
# `resume` (výjimka mimo finally, viz hans_evening_reflection) tím pauzu
# nezasekne napořád. Pojistka nesmí být závislá jen na čítači.
_warmup_pause_depth = 0
_warmup_state_lock = threading.Lock()

def pause_warmup(seconds: float) -> None:
    """Uspi keepalive warmup na `seconds` (auto-expiry = cap, kdyby dávka
    spadla bez resume). Idempotentní: okno jen prodlouží, nezkrátí.
    HANS_WARMUP_PAUSE_REFCOUNT_V1: zvyšuje počet držitelů."""
    global _warmup_pause_until, _warmup_pause_depth
    with _warmup_state_lock:
        _warmup_pause_depth += 1
        _warmup_pause_until = max(_warmup_pause_until,
                                  time.time() + float(seconds))

def resume_warmup() -> None:
    """Pusť svůj podíl na pauze warmupu (konec analytické dávky).
    HANS_WARMUP_PAUSE_REFCOUNT_V1: pauza padne až s POSLEDNÍM držitelem —
    krátká dávka tím přestala odemykat VRAM dlouhé, která pořád běží."""
    global _warmup_pause_until, _warmup_pause_depth
    with _warmup_state_lock:
        if _warmup_pause_depth > 0:
            _warmup_pause_depth -= 1
        else:
            # HANS_WARMUP_UNPAIRED_RESUME_V1 (8.9.) — resume bez pause.
            # Refcount z principu nepozná, KDO ho volá, takže nepárové
            # volání by pauzu shodilo úplně stejně jako dřív. Dnes takové
            # v kódu není (všech 6 dávek volá párově); tahle hláška je tu,
            # aby se budoucí nepárové volání neschovalo jako tiché odemčení
            # VRAM uprostřed cizí dávky — přesně to se hledalo celé ráno 8.9.
            _log.warning("resume_warmup bez odpovídajícího pause_warmup — "
                         "VRAM pauza je odemčená, ačkoli ji nikdo nedržel")
        if _warmup_pause_depth <= 0:
            _warmup_pause_depth = 0
            _warmup_pause_until = 0.0

def warmup_paused() -> bool:
    return _warmup_pause_depth > 0 and time.time() < _warmup_pause_until


def gpu_busy() -> bool:
    """HANS_GPU_BUSY_SHARED_V1 (11. 9.) — bezi na GPU TEZKA prace?

    Sjednocuje dva signaly, ktere dosud zily zvlast:
      • `base_slot_busy()` — nocni base-model davka (studium, Severka, immune)
      • `warmup_paused()`  — render obrazu (`_ollama_unload` ji nastavuje)
    Automatika se ma ptat TOHOHLE, ne jednoho z nich. Do 11. 9. cetl
    `hans_dialog` jen slot, takze do renderu klidne vlitl a hans-czech
    (10,3 GB ve VRAM) vyhodil ComfyUI na `HIP out of memory` pri alokaci
    36 MiB. Zmereno: 34 padu ComfyUI za 14 dni, cizi volani v okne
    68 % renderu.

    ⛔ NEPOUZIVAT na odpoved zivemu cloveku — chat musi jet i pri malovani.
    ⚠️ `warmup_paused()` je globalni (nezna vlakno), takze vlakno, ktere
    pauzu SAMO drzi, tu dostane True. Pro dnesni volajici to nevadi
    (render si llava vola naprimo, ne pres ne), ale pri rozsirovani na
    dalsi mista to overit.
    Fail-safe: pri chybe False — radeji thrashing nez zastavena automatika.
    """
    try:
        return bool(base_slot_busy() or warmup_paused())
    except Exception:
        return False


def gpu_busy_label() -> str:
    """Kdo tu tezkou praci drzi (pro log). Prazdno = nikdo/nevim."""
    try:
        lbl = base_slot_label()
        if lbl:
            return lbl
        return "render obrazu" if warmup_paused() else ""
    except Exception:
        return ""


# HANS_BASE_SLOT_V1 (8.9.) — VÝLUČNOST base-model dávek napříč cestami.
# Samotný refcount výše brání předčasnému odemčení, ale nebrání tomu, aby
# dvě base-model dávky (8 GB každá) běžely SOUČASNĚ. Doloženo 7.9.: studium
# + `narrative` + reasoning qwen3:30b naráz → warmup sám čekal 193,9 s na
# VRAM a jeden `/api/generate` skončil fatálním timeoutem.
# Dosavadní `_creative_busy` v `hans_routine._night_tick` je LOKÁLNÍ
# proměnná jednoho ticku, takže cesta „brain_up catchup" (kterou studium
# 8.9. šlo) o ní vůbec neví.
# ⚠️ Slot má vlastní auto-expiry ze stejného důvodu jako pauza: držitel,
# který spadne bez `release`, nesmí zablokovat noční rutinu napořád.
# ⚠️ FAIL-OPEN: když se dávka slotu nedočká, pokračuje BEZ něj (jen WARNING).
# Horší než dosavadní stav to nebude a noční okno se tím nikdy nezasekne.
_base_slot_until = 0.0
_base_slot_label = ""
_base_slot_token = 0
_base_slot_seq = 0
# HANS_BASE_SLOT_REENTRANT_V1 (8.9.) — slot MUSÍ poznat vlastní vlákno.
# Odhaleno regresním testem: kdyby base-model dávka zavolala uvnitř sebe
# druhou (dnes se to v kódu nestává, ale nic tomu nebrání), čekala by na
# vlastní slot celých `wait_s` a noční rutina by na 5 minut ztuhla. Zámek,
# který nepozná svého držitele, je past — proto reentrance podle `ident`.
_base_slot_thread = None
_base_slot_depth = 0

def acquire_base_slot(label: str, hold_s: float,
                      wait_s: float = 900.0) -> Optional[int]:
    """Zaber slot pro base-model dávku. Vrátí token (pro `release_base_slot`)
    nebo None, když se ho nedočkal — volající pak běží dál bez výlučnosti.
    Reentrantní: totéž vlákno slot dostane hned a drží ho do posledního
    `release_base_slot`."""
    global _base_slot_until, _base_slot_label, _base_slot_token
    global _base_slot_seq, _base_slot_thread, _base_slot_depth
    _me = threading.get_ident()
    _dead = time.time() + float(wait_s)
    _cekal = False
    while True:
        with _warmup_state_lock:
            _ted = time.time()
            _volny = _ted >= _base_slot_until
            if _base_slot_thread == _me and not _volny:
                # vnořené volání z téhož vlákna — neblokuj se o sebe sama
                _base_slot_depth += 1
                _base_slot_until = max(_base_slot_until, _ted + float(hold_s))
                return _base_slot_token
            if _volny:
                _base_slot_seq += 1
                _base_slot_token = _base_slot_seq
                _base_slot_until = _ted + float(hold_s)
                _base_slot_label = str(label)
                _base_slot_thread = _me
                _base_slot_depth = 1
                if _cekal:
                    _log.info("VRAM slot: %s zabral po čekání", label)
                return _base_slot_token
            _drzi, _zbyva = _base_slot_label, _base_slot_until - _ted
        if time.time() >= _dead:
            _log.warning("VRAM slot: %s se nedočkal (drží '%s' ještě %.0f s) "
                         "— běžím bez výlučnosti", label, _drzi, _zbyva)
            return None
        if not _cekal:
            _log.info("VRAM slot: %s čeká, běží '%s' (zbývá %.0f s)",
                      label, _drzi, _zbyva)
            _cekal = True
        time.sleep(2.0)

def release_base_slot(token: Optional[int]) -> None:
    """Uvolni slot. Pustí ho JEN vlastník — dávka, které mezitím vypršel
    hold, tím nesmí sebrat slot tomu, kdo ho po ní legitimně zabral.
    Vnořené držení se odpočítává (HANS_BASE_SLOT_REENTRANT_V1)."""
    global _base_slot_until, _base_slot_label, _base_slot_thread
    global _base_slot_depth
    if not token:
        return
    with _warmup_state_lock:
        if _base_slot_token != token:
            return
        if _base_slot_depth > 0:
            _base_slot_depth -= 1
        if _base_slot_depth <= 0:
            _base_slot_depth = 0
            _base_slot_until = 0.0
            _base_slot_label = ""
            _base_slot_thread = None


def base_slot_busy() -> bool:
    """HANS_HC_YIELD_TO_BASE_V1 — drzi base slot JINE vlakno?

    Pro hans-czech konzumenty, kteri se jinak vklini doprostred base davky
    a vyhodi base model z VRAM. Zmereno v noci na 10. 9. (okno 03:00-04:23):
    z 24 prepnuti modelu jich 8 zpusobily `hans_synthesis:_call` (5x)
    a `hans_dialog:_one_line` (3x) — 7,6 z 11,6 min VRAM rezie.

    ⚠️ REENTRANCE: vlaknu, ktere slot DRZI, vraci False — jinak by si base
    davka zablokovala vlastni prubezna volani.
    Fail-safe: pri jakekoli chybe False (radeji thrashing nez zastaveny hook).
    """
    try:
        with _warmup_state_lock:
            if time.time() >= _base_slot_until:
                return False
            if _base_slot_thread == threading.get_ident():
                return False
            return True
    except Exception:
        return False


def base_slot_label() -> str:
    """HANS_HC_YIELD_TO_BASE_V1 — cim je slot drzen (pro poctivou hlasku v logu)."""
    try:
        with _warmup_state_lock:
            return _base_slot_label if time.time() < _base_slot_until else ""
    except Exception:
        return ""


import contextlib as _contextlib


@_contextlib.contextmanager
def base_model_batch(config: Optional[dict] = None, pause_s: float = 1800,
                     label: str = ""):
    """HANS_BASE_MODEL_BATCH_V1 — VRAM handoff pro dávku běžící na BASE modelu
    (8GB) vedle rezidentního hans-czech (8GB > 16GB VRAM). Na vstupu:
      1) pause_warmup — oba keepalive (ping_model + ollama_warmup) přestanou
         re-pinovat hans-czech,
      2) ollama_unload_all — AKTIVNĚ uvolní hans-czech HNED (pause samo nestačí:
         keep_alive=-1 nevyprší a Ollama ho neevictuje ani pro nový request →
         base model se nevejde → 300s timeout). Na výstupu resume_warmup.
    hans-czech se dotáhne on-demand při reálném chatu. Sjednocuje handoff, který
    dřív měly jen study/maker inline (immune/evening_reflection měly jen pause →
    thrashing)."""
    _tok = None
    try:
        # HANS_BASE_SLOT_V1 — nejdřív výlučnost, teprve pak unload: dvě dávky
        # by si jinak navzájem vyhazovaly model z VRAM (thrashing).
        try:
            # wait_s ZÁMĚRNĚ nižší než hold: čekání nesmí držet noční tick
            # déle, než trvá typická dávka. Po vypršení se běží bez slotu
            # (fail-open) — horší než stav před HANS_BASE_SLOT_V1 to není.
            # HANS_BASE_SLOT_WAIT_CFG_V1 (9. 9.) — 300 s bylo na reálnou délku
            # dávky málo: 9. 9. trvala studijní session 20,7 min a immune se
            # slotu nedočkal (`se nedočkal … běžím bez výlučnosti`) — tedy
            # přesně to, co měl slot odstranit. Klíč, ne natvrdo: strop se
            # bude měnit s délkou dávek a pravidlo wait_s < hold má zůstat vidět.
            _wait_s = 900.0
            try:
                _wait_s = float(((config or {}).get("ollama", {}) or {})
                                .get("base_slot_wait_s", 900.0))
            except Exception:
                pass
            _wait_s = max(0.0, min(_wait_s, float(pause_s)))
            _tok = acquire_base_slot(label or "base dávka", pause_s,
                                     wait_s=_wait_s)
        except Exception as _se:
            _log.debug("base_model_batch slot: %s", _se)
        pause_warmup(pause_s)
        try:
            ollama_unload_all(config=config)
        except Exception as _ue:
            _log.debug("base_model_batch unload: %s", _ue)
        yield
    finally:
        try:
            resume_warmup()
        except Exception:
            pass
        try:
            release_base_slot(_tok)
        except Exception:
            pass

# OLLAMA_CLIENT_MARKER (idempotence)


_localhost_hlaseno = False


def _resolve_url(ollama_url: str | None, config: dict | None) -> str:
    """Zjisti Ollama URL — explicitní arg > config > default.

    HANS_CONFIG_WATCH_MERGED_V1 (11. 9.) — pád na `DEFAULT_URL` se HLÁSÍ.
    Když v configu chybí `openwebui_chat.base_url`, ptal se Hans potichu
    sám sebe (127.0.0.1) a navenek to vypadalo jako výpadek PC: počítač
    byl online, Ollama odpovídala za 0,9 s, a přesto hlásil mozek offline.
    Hlásí se jen jednou za běh — je to stav, ne událost.
    """
    global _localhost_hlaseno
    if ollama_url:
        return ollama_url.rstrip("/")
    if config:
        base = (config.get("openwebui_chat", {}) or {}).get("base_url")
        if base:
            return base.rstrip("/")
        if not _localhost_hlaseno:
            _localhost_hlaseno = True
            _log.error("openwebui_chat.base_url v configu CHYBI — ptam se "
                       "%s, tedy sam sebe. Nejspis se config prepsal jen "
                       "verejnou pulkou (viz HANS_CONFIG_WATCH_MERGED_V1).",
                       DEFAULT_URL)
    return DEFAULT_URL


def brain_available(config: dict | None = None, ollama_url: str | None = None,
                    timeout: float = 2.0) -> bool:
    """HANS_BRAIN_GATE_V1 — je jazykové centrum (Ollama) dostupné? Sonda
    /api/tags + herní mód. Autonomní rutiny (studium, introspekce, completion
    reflexe) tím poznají, jestli má smysl dělat drahou přípravu / LLM pokus,
    nebo rovnou odložit (deferred) — jinak v noci (PC shutdown) točí naprázdno
    / plýtvají síťovými dotazy (viz OpenAlex 429 storm, nález 27.7.). Vrací
    False při herním módu i nedostupnosti."""
    if game_mode_on():
        return False
    try:
        import requests as _r
        url = _resolve_url(ollama_url, config)
        return _r.get(f"{url}/api/tags", timeout=timeout).status_code == 200
    except Exception:
        return False


def ollama_chat(
    model: str,
    messages: list[dict],
    *,
    ollama_url: str | None = None,
    config: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
    stream: bool = False,
    options: dict | None = None,
) -> Optional[str]:
    """Pošle /api/chat request. Vrátí text odpovědi nebo None při chybě."""
    if game_mode_on():   # OLLAMA_GAME_MODE_V1 — herní mód: nech VRAM volnou
        return None
    url = _resolve_url(ollama_url, config)
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": stream,
        "keep_alive": keep_alive,
    }
    if options:
        payload["options"] = options

    return _post_with_retry(f"{url}/api/chat", payload, timeout,
                            _extract_chat)


def ollama_generate(
    model: str,
    prompt: str,
    *,
    system: str | None = None,
    images: list[str] | None = None,
    ollama_url: str | None = None,
    config: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
    stream: bool = False,
    options: dict | None = None,
) -> Optional[str]:
    """Pošle /api/generate request. Vrátí text odpovědi nebo None."""
    if game_mode_on():   # OLLAMA_GAME_MODE_V1
        return None
    url = _resolve_url(ollama_url, config)
    payload: dict = {
        "model": model,
        "prompt": prompt,
        "stream": stream,
        "keep_alive": keep_alive,
    }
    if system:
        payload["system"] = system
    if images:
        payload["images"] = images
    if options:
        payload["options"] = options

    return _post_with_retry(f"{url}/api/generate", payload, timeout,
                            _extract_generate)


def ollama_warmup(
    model: str,
    *,
    ollama_url: str | None = None,
    config: dict | None = None,
    keep_alive: int = DEFAULT_KEEP_ALIVE,
) -> bool:
    """Pošle prázdný request aby se model nahrál do VRAM. Vrátí True při úspěchu."""
    if game_mode_on():   # OLLAMA_GAME_MODE_V1 — nepřihřívej, ať VRAM zůstane volná
        return False
    if warmup_paused():  # HANS_WARMUP_PAUSE_V1 — base analytika drží VRAM
        _log.debug("Warmup: přeskočeno (%s) — noční analytika drží VRAM", model)
        return False
    url = _resolve_url(ollama_url, config)
    br = _breaker_for(url)  # LOG_CIRCUIT_V1
    try:
        # když už víme, že endpoint je dole, ani INFO nespamuj
        if br.snapshot().get("down"):
            _log.debug("Warmup: loading %s ... (endpoint stále down)", model)
        else:
            _log.info("Warmup: loading %s ...", model)
        t0 = time.time()
        r = requests.post(
            f"{url}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": keep_alive},
            timeout=(CONNECT_TIMEOUT, 300),   # OLLAMA_CONNECT_TIMEOUT_V1
        )
        r.raise_for_status()
        _log.info("Warmup: %s ready (%.1fs)", model, time.time() - t0)
        br.note_success(_log)
        return True
    except Exception as exc:
        if is_conn_error(exc):
            if br.should_log(exc):
                _log.error("Warmup failed for %s: %s", model, exc)
        else:
            _log.error("Warmup failed for %s: %s", model, exc)
        return False


# ── Internals ──────────────────────────────────────────────

def _post_with_retry(url: str, payload: dict, timeout: int,
                     extractor) -> Optional[str]:
    """HANS_LLM_TRACE_V1 (9. 9.) — měřicí obal nad `_post_with_retry_impl`.

    JEDINÉ hrdlo, kterým tečou `ollama_chat` i `ollama_generate`, takže sem
    patří zápis „kdo si řekl o který model a jak dlouho čekal". Bez něj se
    z logu nedá zjistit, který noční krok vytáhl hans-czech doprostřed
    base-model dávky — hlášky nesou jen URL a mez timeoutu.
    ⚠️ Nesmí změnit chování: měření je v `try/except` a výsledek se vrací
    beze změny, výjimka se propaguje dál."""
    _t0 = time.time()
    try:
        _out = _post_with_retry_impl(url, payload, timeout, extractor)
    except BaseException as _e:
        _trace_zapis(payload, url, time.time() - _t0, type(_e).__name__)
        raise
    _trace_zapis(payload, url, time.time() - _t0,
                 "ok" if _out else "prazdno")
    return _out


def _trace_zapis(payload, url, trvani, vysledek):
    try:
        from scripts.llm_trace import zapis as _z
        _z((payload or {}).get("model", "?"), url, trvani, vysledek)
    except Exception:
        pass


def _post_with_retry_impl(url: str, payload: dict, timeout: int,
                          extractor) -> Optional[str]:
    """POST s retry při timeout. LOG_CIRCUIT_V1: potlač spam z mrtvého endpointu."""
    br = _breaker_for(url)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            r = requests.post(url, json=payload,
                              timeout=(CONNECT_TIMEOUT, timeout))  # OLLAMA_CONNECT_TIMEOUT_V1
            r.raise_for_status()
            out = extractor(r.json())
            br.note_success(_log)
            return out
        except requests.exceptions.ConnectTimeout as exc:
            # OLLAMA_CONNECT_TIMEOUT_LOG_V1 (26.8.) — ConnectTimeout je PODTŘÍDA
            # Timeout, takže dřív spadl do větve níž a ta vypsala READ mez
            # (25/120 s), i když se reálně čekalo jen CONNECT_TIMEOUT (3 s).
            # Log tím lhal o tom, jak dlouho Hans čekal, a svedl diagnostiku:
            # 26.8. ráno to vypadalo, že OLLAMA_CONNECT_TIMEOUT_V1 nefunguje,
            # přestože fungoval. Chování se NEMĚNÍ, mění se jen pravdivost hlášky.
            # ⚠️ Tahle větev MUSÍ zůstat NAD `except Timeout`, jinak ji nikdy
            # nedostane. A NEsmí se hlásit přes LOG_CIRCUIT breaker —
            # `_log_circuit.is_conn_error` timeouty záměrně nebere, aby se
            # neschovaly reálné pomalé cesty (rozhodnuto 23.8.).
            last_exc = exc
            if attempt <= MAX_RETRIES:
                _log.warning("Ollama nedostupná (spojení nenavázáno do %d s), "
                             "retry %d/%d: %s",
                             CONNECT_TIMEOUT, attempt, MAX_RETRIES, url)
            else:
                _log.error("Ollama nedostupná (spojení nenavázáno do %d s) ani "
                           "po %d pokusech — stroj je nejspíš vypnutý: %s",
                           CONNECT_TIMEOUT, attempt, url)
        except requests.exceptions.Timeout as exc:
            # sem už padá JEN read timeout — spojení stálo, ale odpověď nedorazila
            last_exc = exc
            if attempt <= MAX_RETRIES:
                _log.warning("Ollama neodpověděla do %d s, retry %d/%d: %s",
                             timeout, attempt, MAX_RETRIES, url)
            else:
                _log.error("Ollama neodpověděla do %d s ani po %d pokusech: %s",
                           timeout, attempt, url)
        except requests.exceptions.ConnectionError as exc:
            if br.should_log(exc):
                _log.error("Ollama connection error: %s — %s", url, exc)
            return None
        except Exception as exc:
            if is_conn_error(exc):
                if br.should_log(exc):
                    _log.error("Ollama connection error: %s — %s", url, exc)
            else:
                _log.error("Ollama request error: %s — %s", url, exc)
            return None
    return None


def _extract_chat(data: dict) -> Optional[str]:
    try:
        return data["message"]["content"].strip()
    except (KeyError, AttributeError):
        _log.error("Unexpected chat response: %s", data)
        return None


def _extract_generate(data: dict) -> Optional[str]:
    try:
        return data["response"].strip()
    except (KeyError, AttributeError):
        _log.error("Unexpected generate response: %s", data)
        return None
