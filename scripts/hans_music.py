"""HANS_MUSIC_V1 (10. 9.) — Hans složí hudební příklad ke studované kapitole.

Nástroj: `chatmusician` (m-a-p/ChatMusician, LLaMA2-7B doučený na ABC
notaci, MIT, Q4_K_M ~4 GB). Nasazen na PC jako ollama model.
⚠️ VRAM: koexistuje s rezidentním hans-czech (8,06 + 5,18 = 13,2 z 16 GB),
takže NEPOTŘEBUJE `base_slot` ani handoff — změřeno naživo 10. 9.

Řetěz:  kapitola (česky)  →  zadání (anglicky, base model)
                          →  ABC notace (chatmusician)
                          →  úklid  →  HTML s vykreslenými notami (abcjs)

🔴 FORMULACE ZADÁNÍ ROZHODUJE, JESTLI PŘIJDOU NOTY NEBO PRÓZA.
Změřeno 10. 9.: „Compose a short example that demonstrates basic music
notation" vrátilo VÝKLAD (odstavce textu, ASCII tabulaturu), zatímco
„Develop a simple musical piece in C major, 4/4 time" vrátilo ABC.
Model je doučený na instrukcích tvaru „Develop a musical piece…" /
„Create a short melody…", takže zadání musí začínat týmž slovesem.
Proto `_SLOVESA` a proto se výstup po vygenerování OVĚŘUJE (`X:` hlavička).
"""
from __future__ import annotations

import re
from typing import Optional

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger(
    "hans_music")

MODEL = "chatmusician:latest"

# Slovesa, po kterých model skutečně skládá (viz docstring).
_SLOVESA = ("Develop a musical piece", "Develop a simple musical piece",
            "Create a short melody")

# ABC řádky, které model tahá z tréninkových dat (Music21) a do díla nepatří.
_BALAST = re.compile(r"^\s*(T:Music21|C:Music21|I:linebreak|%%|%\d)", re.I)


def _cfg(config: dict) -> dict:
    return (config or {}).get("music", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


def zadani_pro_kapitolu(config: dict, kapitola: str,
                        kontext: str = "") -> Optional[str]:
    """Z české kapitoly udělej ANGLICKÉ zadání pro skladatele.

    Vrací větu začínající jedním ze `_SLOVESA`. Bez LLM (mozek dole)
    vrací None — volající to má odložit, ne skládat naslepo.
    """
    try:
        from scripts.ollama_client import ollama_generate
        # 🔴 ZÁMĚRNĚ hans-czech, NE base model. Base (8 GB) se vedle
        # rezidentního hans-czech (8,06) a chatmusiciana (5,18) do 16 GB
        # NEVEJDE — změřeno 10. 9.: volání spadlo na 60s timeout a vyrobilo
        # by přesně ten VRAM thrashing, který dopoledne řešil
        # HANS_HC_YIELD_TO_BASE_V1. Jde o jednu překladovou větu, na to
        # persona-finetune stačí.
        model = (_cfg(config).get("brief_model")
                 or (config.get("hans_dialog", {}) or {}).get("ollama_model")
                 or "hans-czech:latest")
        sysp = (
            "Jsi hudební redaktor. Dostaneš název kapitoly o hudbě a napíšeš "
            "JEDNU anglickou větu — zadání pro skladatele, který umí jen "
            "skládat noty (ne vysvětlovat).\n"
            "PRAVIDLA:\n"
            "1) Věta MUSÍ začínat 'Develop a musical piece' nebo "
            "'Create a short melody'.\n"
            "2) Uveď tóninu a takt (např. 'in D minor, 3/4 time').\n"
            "3) Uveď charakter nebo postup, který kapitolu vystihuje.\n"
            "4) Žádné vysvětlování, žádné 'that demonstrates'. Jen zadání.\n"
            "Odpověz POUZE tou jednou anglickou větou.")
        user = "Kapitola: %s" % kapitola
        if kontext:
            user += "\nO čem je: %s" % kontext[:400]
        raw = ollama_generate(model, user, system=sysp, config=config,
                              timeout=60, options={"temperature": 0.3,
                                                   "num_predict": 80})
        if not raw or not raw.strip():
            return None
        veta = raw.strip().split("\n")[0].strip().strip('"')
        if not veta.lower().startswith(("develop", "create")):
            # model neposlechl tvar → vezmi bezpečný default, ať nevznikne próza
            _log.info("music: zadání nezačíná slovesem (%.40s) → default", veta)
            return ("%s inspired by the topic \"%s\", in D major, 4/4 time."
                    % (_SLOVESA[0], kapitola))
        return veta
    except Exception as e:
        _log.debug("music: zadání selhalo: %s", e)
        return None


def vycisti_abc(abc: str, titul: str = "") -> str:
    """Vyhoď balast z tréninkových dat a dosaď pořádný titul."""
    radky, videl_t = [], False
    for r in (abc or "").splitlines():
        if _BALAST.match(r):
            continue
        if r.startswith("T:"):
            if videl_t:
                continue            # model titul opakuje (3x „Music21 Fragment")
            videl_t = True
            if titul:
                r = "T:" + titul
        radky.append(r.rstrip())
    out = "\n".join(radky).strip()
    if titul and not videl_t and out.startswith("X:"):
        prvni, _, zbytek = out.partition("\n")
        out = "%s\nT:%s\n%s" % (prvni, titul, zbytek)
    return out


def sloz(config: dict, zadani: str, titul: str = "") -> Optional[str]:
    """Zavolej ChatMusician a vrať VYČIŠTĚNOU ABC notaci, nebo None.

    None = mozek dole NEBO model vrátil prózu místo not. Obojí je pro
    volajícího totéž: příklad se do díla nedá, ať se nevkládá nesmysl.
    """
    if not zadani:
        return None
    try:
        from scripts.ollama_client import ollama_generate
        raw = ollama_generate(
            _cfg(config).get("model", MODEL), zadani, config=config,
            timeout=int(_cfg(config).get("timeout_s", 180)),
            keep_alive=_cfg(config).get("keep_alive", "10m"),
            options={"temperature": 0.2, "top_k": 40, "top_p": 0.9,
                     "repeat_penalty": 1.1,
                     "num_predict": int(_cfg(config).get("num_predict", 1536))})
    except Exception as e:
        _log.debug("music: chatmusician selhal: %s", e)
        return None
    if not raw:
        return None
    # regex z karty modelu; když nesedí, model odpověděl PRÓZOU → zahoď
    m = re.search(r"(X:\d+\s*\n(?:[^\n]*\n?)*)", raw)
    if not m:
        _log.info("music: model vrátil prózu místo notace (%.50s)",
                  raw.strip().replace("\n", " "))
        return None
    abc = vycisti_abc(m.group(1), titul)
    if "K:" not in abc:                 # bez tóniny to abcjs nevykreslí
        _log.info("music: notace bez K: (tónina) → zahazuji")
        return None
    return abc


def html_blok(abc: str, popis: str = "") -> str:
    """ABC → HTML blok s vykreslenými notami + přehráním (abcjs, lokálně)."""
    import html as _h
    if not abc:
        return ""
    # ⚠️ abcjs 6 vystavuje globalni objekt ABCJS — `renderAbc` samo o sobe
    # NEEXISTUJE. Prehrani je v try/catch: synth potrebuje AudioContext
    # a gesto uzivatele, a kdyz selze, nesmi to shodit vykresleni not.
    _id = "abc%d" % (abs(hash(abc)) % 100000)
    return (
        '<figure class="hudebni-priklad">\n'
        '<figcaption>Hansův notový příklad%s</figcaption>\n'
        '<div id="%s" class="abc-noty"></div>\n'
        '<button class="abc-play" onclick="prehraj_%s()">▶ přehrát</button>\n'
        '<details><summary>zápis v ABC notaci</summary><pre>%s</pre></details>\n'
        '<script>\n'
        'var vs_%s = ABCJS.renderAbc("%s", %s, {responsive:"resize"});\n'
        'function prehraj_%s(){ try {\n'
        '  if (!ABCJS.synth.supportsAudio()) { alert("Prohlížeč neumí přehrát zvuk."); return; }\n'
        '  var ac = new (window.AudioContext || window.webkitAudioContext)();\n'
        '  var s = new ABCJS.synth.CreateSynth();\n'
        '  s.init({audioContext: ac, visualObj: vs_%s[0]})\n'
        '   .then(function(){ return s.prime(); })\n'
        '   .then(function(){ s.start(); });\n'
        '} catch(e) { console.log("abc audio:", e); } }\n'
        '</script>\n'
        '</figure>'
        % ((" — " + _h.escape(popis)) if popis else "", _id, _id,
           _h.escape(abc), _id, _id, _js_string(abc), _id, _id)
    )


def _js_string(s: str) -> str:
    """Bezpečný JS řetězec — ABC je víceřádkové a má uvozovky u akordů."""
    import json as _j
    return _j.dumps(s)
