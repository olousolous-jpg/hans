# -*- coding: utf-8 -*-
"""HANS_TOOLS_V1 (1.9.) — nativní tool-calling nad `hans_agent.ACTIONS`.

Krok 1 směru „LLM jako tlumočník, ne databáze": faktickou otázku nemá Hans
zodpovědět z hlavy, ale VYBRAT nástroj — program ho zavolá a Hans z výsledku
teprve formuluje.

Tenhle modul JEN ROZHODUJE. Nic nevykonává, nesahá na chat ani na deník, takže
se dá měřit samostatně a nasadit ve stínovém režimu vedle dnešního routeru.

⚠️ VŠECHNO ZDE STOJÍ NA MĚŘENÍ Z 1. 9., ne na odhadu:
  • `hans-czech` tool-calling NEUMÍ — Ollama odmítne na úrovni API
    (`does not support tools`), totéž jeho base OpenEuroLLM. Proto jiný model.
  • `qwen2.5:7b` to umí, má 4,7 GB → 8,1 + 4,7 = 12,8 GB < 16 GB VRAM, takže
    oba mohou být rezidentní. ŽÁDNÝ handoff, latence 0,5 s. [[study-vram-handoff]]
  • Přesnost proti reálnému schématu: 21/22 se šesti nástroji, 19/22 se všemi
    23 — a to zhoršení je vinou POČTU, ne obtížnosti vět (tytéž chybné věty
    s malou sadou projdou). Proto `KATEGORIE` níž. [[list-switches-model-to-matching]]

⛔ Popisy nástrojů se NEPÍŠÍ znovu — berou se z `ACTIONS[*].desc`, které jsou
   roky laděné na reálných větách. Dvě pravdy by se rozešly.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional, Tuple

_log = logging.getLogger(__name__)

# Kategorie = podmnožina nástrojů posílaná modelu. Vychází z měření: všech 23
# naráz stálo tři případy („kdo v domě bydlí" → home_status; „běž spát" a
# „teplota grafiky" → nic), a všechny tři se s malou sadou trefily.
# ⚠️ Členění je podle TOHO, CO SE PLETE, ne podle abecedy: `who_is_home` ×
# `household` × `person` musí být pohromadě, aby si model vybral mezi nimi,
# ne aby na jednu z nich narazil náhodou.
KATEGORIE = {
    "stav_domu":  ["report_who_is_home", "report_household", "report_person",
                   "report_home_status", "report_kolac_status"],
    "prostredi":  ["report_climate", "report_weather", "report_pc_health"],
    "prehravani": ["report_now_playing", "kodi_play_film", "kodi_pause",
                   "kodi_stop", "kodi_resume"],
    "ovladani":   ["pc_shutdown", "pc_wake", "hans_sleep", "hans_wake",
                   "game_mode_toggle", "guard_toggle"],
    "zapis":      ["add_note", "add_book_wishlist", "add_study_topic"],
    "ostatni":    ["translate_doc"],
}

_DEFAULT_MODEL = "qwen2.5:7b"
_DEFAULT_TIMEOUT = 20.0


def _cfg(config: dict) -> dict:
    return (config or {}).get("tools", {}) or {}


def _endpoint(config: dict) -> str:
    """Kde běží Ollama. Bere se z existujících klíčů — vlastní by se rozešel.

    ⚠️ `or "127.0.0.1"` se sem SCHVÁLNĚ nepíše: tichý fallback na localhost
    nechal roky mrtvý VRAM handoff renderu ([[silent-localhost-fallback]]).
    Když adresa není, vrátí se prázdno a volající to pozná.
    """
    c = config or {}
    for cesta in (("tools", "base_url"), ("openwebui_chat", "base_url"),
                  ("intent", "base_url"), ("self_insight", "reasoning_url")):
        node = c
        for k in cesta:
            node = (node or {}).get(k) if isinstance(node, dict) else None
        if isinstance(node, str) and node.strip():
            return node.strip().rstrip("/")
    return ""


def _arg_names(action) -> list:
    out = []
    for a in (getattr(action, "args", None) or []):
        out.append(a if isinstance(a, str) else getattr(a, "name", str(a)))
    return out


def tools_schema(action_ids) -> list:
    """ACTIONS → seznam nástrojů ve tvaru, kterému rozumí Ollama /api/chat."""
    from scripts.hans_agent import ACTIONS
    out = []
    for aid in action_ids:
        a = ACTIONS.get(aid)
        if a is None:
            continue
        props, req = {}, []
        for nm in _arg_names(a):
            props[nm] = {"type": "string", "description": nm}
            req.append(nm)
        out.append({"type": "function", "function": {
            "name": aid,
            "description": (getattr(a, "desc", "") or "")[:400],
            "parameters": {"type": "object", "properties": props,
                           "required": req}}})
    return out


def kategorie_pro(text: str) -> list:
    """Které kategorie nástrojů poslat. Zatím hrubě podle klíčových slov —
    ⚠️ je to VÝBĚR PODMNOŽINY, ne rozhodnutí o akci: když se netrefí, pošlou
    se všechny (19/22 pořád funguje). Chyba tedy zhorší přesnost, nezpůsobí
    špatnou akci.
    """
    t = (text or "").lower()
    vybrane = []
    if re.search(r"stupň|stupn|teplot|vlhkost|horko|zima|dusno|počas|pocas|"
                 r"venku|prší|prsi|graf|procesor|cpu|gpu|vram", t):
        vybrane.append("prostredi")
    if re.search(r"kdo|doma|bydl|domácnost|domacnost|přítom|pritom|koláč|kolac", t):
        vybrane.append("stav_domu")
    if re.search(r"film|tv|televiz|hraje|pusť|pust|pauz|zastav|pokračuj|pokracuj", t):
        vybrane.append("prehravani")
    if re.search(r"vypni|zapni|počítač|pocitac|spát|spat|vzbuď|vzbud|probuď|"
                 r"probud|hlídej|hlidej|herní|herni", t):
        vybrane.append("ovladani")
    if re.search(r"poznámk|poznamk|seznam|knih|nastuduj|zapiš|zapis|úkol|ukol", t):
        vybrane.append("zapis")
    if re.search(r"přelož|prelo|titulk|dabing", t):
        vybrane.append("ostatni")
    return vybrane or list(KATEGORIE.keys())


def rozhodni(config: dict, text: str,
             timeout: Optional[float] = None) -> Optional[Tuple[str, dict]]:
    """Vrátí (action_id, args) nebo None, když žádný nástroj nesedí.

    None je LEGITIMNÍ výsledek, ne selhání: konverzační věta („jak se dnes
    cítíš?") nástroj mít nemá a v měření to model zvládal spolehlivě.
    Volající pak pokračuje dnešní cestou.
    """
    import requests
    host = _endpoint(config)
    if not host:
        _log.debug("hans_tools: chybí endpoint Ollamy — přeskakuji")
        return None
    c = _cfg(config)
    ids = []
    for kat in kategorie_pro(text):
        ids.extend(KATEGORIE.get(kat, []))
    schema = tools_schema(ids)
    if not schema:
        return None
    try:
        r = requests.post(host + "/api/chat", json={
            "model": c.get("model", _DEFAULT_MODEL),
            "stream": False,
            "messages": [{"role": "user", "content": text or ""}],
            "tools": schema,
            "options": {"temperature": 0,
                        "num_ctx": int(c.get("num_ctx", 8192))},
        }, timeout=float(timeout or c.get("timeout_s", _DEFAULT_TIMEOUT)))
        data = r.json()
    except Exception as e:
        _log.debug("hans_tools: volání selhalo: %s", e)
        return None
    if isinstance(data, dict) and data.get("error"):
        # ⚠️ Nejčastější příčina: model bez podpory tools. Hlásit nahlas —
        # tiché None by vypadalo jako „nic nesedělo".
        _log.warning("hans_tools: model odmítl nástroje: %s",
                     str(data.get("error"))[:160])
        return None
    calls = ((data.get("message") or {}).get("tool_calls") or []) \
        if isinstance(data, dict) else []
    if not calls:
        return None
    fn = (calls[0] or {}).get("function") or {}
    aid = fn.get("name")
    if not aid:
        return None
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {}
    return (str(aid), args if isinstance(args, dict) else {})
