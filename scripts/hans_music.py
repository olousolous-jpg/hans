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
            "5) NENÍ-LI kapitola o rytmu, metru nebo taktu, přidej na konec "
            "'Keep a single time signature throughout.' — jinak model mění "
            "takt uprostřed a u výkladové ukázky to jen plete.\n"
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
    """ABC → HTML blok s vykreslenými notami + přehráním (abcjs, lokálně).

    Stránka musí načíst `abcjs.js` — vloží ho `pridej_abcjs` (maker to dělá
    sám). HANS_MUSIC_PLAY_V1 (14. 9.): přehrání opravdu hraje a chyby ukáže.
    """
    import html as _h
    if not abc:
        return ""
    # ⚠️ abcjs 6 vystavuje globalni objekt ABCJS — `renderAbc` samo o sobe
    # NEEXISTUJE. Vykresleni i prehrani jsou v try: kdyz selze zvuk,
    # nesmi to shodit noty, a kdyz chybi knihovna, rekne to stranka.
    # HANS_MUSIC_SOUNDFONT_LOCAL_V1 — zvuky napred z ./soundfont/ u dila,
    # pri selhani vychozi sada z internetu (tataz FluidR3_GM).
    # ⚠️ `options` v init() je POVINNE: bez nej prime() spadne na
    # options.swing. Zmereno 14. 9. v Chromiu — tlacitko do te doby nehralo
    # nikde, protoze chyba sla jen do console.log.
    _id = "abc%d" % (abs(hash(abc)) % 100000)
    tpl = (
        '<figure class="hudebni-priklad">\n'
        '<figcaption>Hansův notový příklad@@POPIS@@</figcaption>\n'
        '<div id="@@ID@@" class="abc-noty"></div>\n'
        '<button class="abc-play" id="btn_@@ID@@" onclick="prehraj_@@ID@@()">▶ přehrát</button>\n'
        '<span class="abc-stav" id="stav_@@ID@@"></span>\n'
        '<details><summary>zápis v ABC notaci</summary><pre>@@ABCTXT@@</pre></details>\n'
        '<script>\n'
        'var vs_@@ID@@ = null, syn_@@ID@@ = null;\n'
        'try { vs_@@ID@@ = ABCJS.renderAbc("@@ID@@", @@ABCJS@@, {responsive:"resize"}); }\n'
        'catch(e) { document.getElementById("stav_@@ID@@").textContent = "Noty se nenačetly (chybí abcjs.js)."; }\n'
        'function prehraj_@@ID@@(){\n'
        '  var b = document.getElementById("btn_@@ID@@"), st = document.getElementById("stav_@@ID@@");\n'
        '  function chyba(e){ syn_@@ID@@ = null; b.textContent = "▶ přehrát";\n'
        '    st.textContent = "Přehrání selhalo: " + ((e && (e.message || e.status)) || e); }\n'
        '  try {\n'
        '    if (syn_@@ID@@) { syn_@@ID@@.stop(); syn_@@ID@@ = null; b.textContent = "▶ přehrát"; st.textContent = ""; return; }\n'
        '    if (!vs_@@ID@@) { chyba("noty nejsou vykreslené"); return; }\n'
        '    if (!ABCJS.synth.supportsAudio()) { chyba("prohlížeč neumí přehrát zvuk"); return; }\n'
        '    var ac = new (window.AudioContext || window.webkitAudioContext)();\n'
        '    st.textContent = "načítám zvuky…";\n'
        '    function spust(mistni){\n'
        '      var s = new ABCJS.synth.CreateSynth();\n'
        '      var o = mistni ? {soundFontUrl: "soundfont/", soundFontVolumeMultiplier: 3} : {};\n'
        '      return s.init({audioContext: ac, visualObj: vs_@@ID@@[0], options: o})\n'
        '        .then(function(){ return s.prime(); })\n'
        '        .then(function(r){ return [s, r]; });\n'
        '    }\n'
        '    spust(true).catch(function(){ return spust(false); })\n'
        '     .then(function(x){ var s = x[0], r = x[1];\n'
        '       syn_@@ID@@ = s; s.start(); b.textContent = "■ zastavit"; st.textContent = "";\n'
        '       setTimeout(function(){ if (syn_@@ID@@ === s) { syn_@@ID@@ = null; b.textContent = "▶ přehrát"; } },\n'
        '                  ((r && r.duration) || 0) * 1000 + 500); })\n'
        '     .catch(chyba);\n'
        '  } catch(e) { chyba(e); } }\n'
        '</script>\n'
        '</figure>'
    )
    return (tpl.replace("@@POPIS@@", (" — " + _h.escape(popis)) if popis else "")
               .replace("@@ABCTXT@@", _h.escape(abc))
               .replace("@@ABCJS@@", _js_string(abc))
               .replace("@@ID@@", _id))


ABCJS_TAG = '<script src="abcjs.js"></script>'


def pridej_abcjs(html: str) -> str:
    """HANS_MUSIC_PLAY_V1 — stránka s notovým blokem načte abcjs.js (jednou).

    Bez toho `ABCJS` neexistuje → noty se nevykreslí a ▶ nehraje. Dřív
    maker knihovnu jen zkopíroval k dílu a značku čekal od kodéru.
    """
    if not html or "abc-noty" not in html or 'src="abcjs.js"' in html:
        return html
    if "</head>" in html:
        return html.replace("</head>", ABCJS_TAG + "\n</head>", 1)
    return ABCJS_TAG + "\n" + html


# HANS_MUSIC_SOUNDFONT_LOCAL_V1 — tatáž sada, kterou abcjs bez nastavení
# stahuje z GitHubu (FluidR3_GM, CC BY 3.0) → zvuk díla se nemění.
SOUNDFONT_URL = "https://paulrosen.github.io/midi-js-soundfonts/FluidR3_GM/"
_SF_NASTROJ = "acoustic_grand_piano-mp3"
_SF_JMENA = ("C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B")
_SF_LICENCE = (
    "Zvuky klavíru: FluidR3_GM (Frank Wen), převod midi-js-soundfonts\n"
    "(Benjamin Gleitzman, Paul Rosen). Licence Creative Commons Attribution 3.0\n"
    "https://creativecommons.org/licenses/by/3.0/\n"
)


def pridej_soundfont(dest_dir) -> bool:
    """Zkopíruj zvuky klavíru k dílu do `<dílo>/soundfont/`.

    Zdroj je `data/soundfont/` (gitignored, 88 mp3 ≈ 2 MB); chybí-li tón,
    stáhne se jednou. Selhání NENÍ fatální — stránka pak zvuky tahá
    z internetu, noty se vykreslí tak jako tak. Vrací True, když dílo
    kopii má.
    """
    import os as _os
    import shutil as _sh
    import urllib.request as _ur
    root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    sf = _os.path.join(root, "data", "soundfont")
    src = _os.path.join(sf, _SF_NASTROJ)
    try:
        _os.makedirs(src, exist_ok=True)
        for n in range(21, 109):                      # A0 … C8
            jm = "%s%d.mp3" % (_SF_JMENA[n % 12], n // 12 - 1)
            cil = _os.path.join(src, jm)
            if not _os.path.exists(cil):
                with _ur.urlopen(SOUNDFONT_URL + _SF_NASTROJ + "/" + jm,
                                 timeout=20) as r:
                    data = r.read()
                with open(cil, "wb") as w:
                    w.write(data)
        lic = _os.path.join(sf, "LICENCE.txt")
        if not _os.path.exists(lic):
            with open(lic, "w", encoding="utf-8") as w:
                w.write(_SF_LICENCE)
        _sh.copytree(sf, _os.path.join(str(dest_dir), "soundfont"),
                     dirs_exist_ok=True)
        return True
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "hudba: zvuky k dílu se nepřidaly (%s) — přehrání půjde "
            "z internetu", e)
        return False


def _js_string(s: str) -> str:
    """Bezpečný JS řetězec — ABC je víceřádkové a má uvozovky u akordů."""
    import json as _j
    return _j.dumps(s)
