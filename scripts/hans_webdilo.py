"""HANS_WEB_DILO_V1 (26. 9.) — web z díla, druhá generace.

Pořadí (zadání uživatele 26. 9.): TEXT → OBRÁZKY k textu → VOLBA NÁSTROJŮ
(Hans si sám vybere, čím stránky postaví, aby vypadaly moderně a profesionálně)
→ STAVBA → KONTROLA v prohlížeči → OPRAVA.

Proč nová cesta (změřeno 26. 9., backlog `WEB_DILA_26_09`):
- text podstránky byl DOSLOVNÁ studijní poznámka (6–9 vět, ~8 % materiálu)
  a zdroj se zahazoval → teď text ze ZDROJE (`study_sources`), s kontrolou
  letopočtů a čísel proti němu;
- kód se od července nezměnil (pevné zadání, 0× JS, podstránky z pevné
  šablony) → teď si formu volí reasoning model podle OBSAHU, volbu zkontroluje
  program (slider bez obrázků, osa bez letopočtů neprojde) a knihovny se
  stáhnou k dílu (web běží bez internetu);
- vadu, kterou deterministická kontrola nevidí (prázdné záhlaví), našel
  v pilotu model na obrázky → hledač vad pro opravné kolo, ne rozhodčí.

Selže-li cokoli podstatného, vrací None a `hans_maker` postaví web postaru.
"""
from __future__ import annotations

import base64
import html as _h
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_webdilo")
_ROOT = Path(__file__).resolve().parent.parent


def _cfg(config: dict) -> dict:
    return ((config.get("maker", {}) or {}).get("web_v2", {}) or {})


def enabled(config: dict) -> bool:
    # ⚠️ Výchozí VYPNUTO (26. 9.): první celý test — texty dobré, ale volba
    # nástrojů se schématem u qwen3 vrátila úvahu místo plánu a opravná kola
    # vady neopravila. Zapnout `maker.web_v2.enabled` až po opravě a kontrole.
    return bool(_cfg(config).get("enabled", False))


def _gen(config, model, prompt, system, schema=None, num_predict=3000,
         num_ctx=16384, temperature=0.3, images=None, timeout=900, keep_alive=0,
         num_gpu=99):
    from scripts.ollama_client import ollama_generate
    opts = {"temperature": temperature, "num_ctx": num_ctx, "num_predict": num_predict}
    # HANS_WEB_QWEN3_NOTHINK_V1 (21. 10.) — qwen3 jako coder „přemýšlel nahlas“
    # v odpovědi i s think=False (27 000 zn úvah, HTML nedopsal); měkký přepínač
    if schema is None and re.search(r"qwen3", model, re.I):
        prompt = (prompt or "") + "\n/no_think"
    if num_gpu is not None:      # None = rozložení GPU/CPU nech na Ollamě
        opts["num_gpu"] = num_gpu
    return ollama_generate(
        model, prompt, system=system, config=config, timeout=timeout,
        keep_alive=keep_alive, images=images, format=schema,
        think=(False if re.search(r"qwen3|uigen-x", model, re.I) else None),
        options=opts)


def _json(raw) -> Optional[dict]:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        try:
            return json.loads(m.group(0)) if m else None
        except Exception:
            return None


# ── 1) ZDROJ ──────────────────────────────────────────────────────────────

def zdroj(config: dict, db_path: str, topic: str, sub: str):
    """Nejnovější uložený materiál k pod-tématu; když chybí (studováno před
    HANS_STUDY_SOURCES_KEEP_V1), dočte ho znovu a uloží. (material, url, main)."""
    try:
        con = sqlite3.connect(db_path, timeout=10)
        try:
            r = con.execute(
                "SELECT material, url, main_title FROM study_sources "
                "WHERE topic=? AND sub=? AND chars>0 ORDER BY ts DESC LIMIT 1",
                (topic, sub)).fetchone()
        finally:
            con.close()
        if r and r[0]:
            return r[0], r[1], r[2]
    except Exception as e:
        _log.debug("zdroj z DB: %s", e)
    try:
        from scripts import hans_study as hs
        mat, url, main = hs._gather_material(config, sub, topic, db_path=db_path)
    except Exception as e:
        _log.warning("webdilo: dočtení zdroje '%s' selhalo: %s", sub, e)
        return None, None, None
    if mat:
        try:
            con = sqlite3.connect(db_path, timeout=10)
            con.execute(
                "INSERT INTO study_sources (ts, program_id, idx, deepen_round, "
                "topic, sub, main_title, url, material, chars) "
                "VALUES (?,NULL,NULL,NULL,?,?,?,?,?,?)",
                (time.time(), topic, sub, main, url, mat, len(mat)))
            con.commit(); con.close()
        except Exception as e:
            _log.debug("uložení dočteného zdroje: %s", e)
    return mat, url, main


# ── 2) TEXT ───────────────────────────────────────────────────────────────

_TEXT_SYSTEM = (
    "Jsi redaktor kvalitního naučného webu. Z přiloženého MATERIÁLU napiš česky "
    "článek k zadanému pod-tématu. Piš věcně ve třetí osobě (žádné „já“, "
    "„zaujalo mě“, „je pozoruhodné“). Používej JEN fakta z materiálu: jména, "
    "letopočty, čísla a místa přebírej přesně; nic nedoplňuj z vlastní paměti. "
    "Drž se pod-tématu; co s ním nesouvisí, vynech. Když materiál k pod-tématu "
    "mnoho nemá, napiš kratší článek — raději kratší než vymyšlený. Struktura: "
    "perex (2–3 věty), 3–5 oddílů s krátkým mezititulkem, každý 1–3 odstavce. "
    "Navíc vypiš datované události z materiálu, které k pod-tématu patří.")

_TEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "perex": {"type": "string"},
        "oddily": {"type": "array", "items": {
            "type": "object",
            "properties": {"nadpis": {"type": "string"},
                           "odstavce": {"type": "array",
                                        "items": {"type": "string"}}},
            "required": ["nadpis", "odstavce"]}},
        "udalosti": {"type": "array", "items": {
            "type": "object",
            "properties": {"rok": {"type": "string"}, "co": {"type": "string"}},
            "required": ["rok", "co"]}},
    },
    "required": ["perex", "oddily", "udalosti"],
}

_PRVNI_OSOBA = re.compile(
    r"\b(jsem|jsme|zaujal[oa]? m[ěe]|m[ěe] zaujal\w*|je pozoruhodn\w*|"
    r"p[řr]ekvapil[oa]? m[ěe])\b", re.I)
_CISLO = re.compile(r"\b\d{3,4}\b")


def _vety(t: str) -> list:
    return [v for v in re.split(r"(?<=[.!?])\s+", (t or "").strip()) if v]


def _cista_veta(v: str, material: str) -> bool:
    """Věta bez opory (číslo, které ve zdroji není) nebo v 1. osobě → pryč."""
    if _PRVNI_OSOBA.search(v):
        return False
    return all(n in material for n in _CISLO.findall(v))


def text_stranky(config: dict, topic: str, sub: str, material: str) -> Optional[dict]:
    """Článek ze zdroje. Vrací {perex, oddily, udalosti, stat} nebo None."""
    model = _cfg(config).get("text_model") or ""
    if not model:
        from scripts import hans_study as hs
        model = hs._model(config)
    raw = _gen(config, model,
               "Téma webu: %s\nPod-téma článku: %s\n\nMATERIÁL:\n%s"
               % (topic, sub, (material or "")[:int(_cfg(config).get(
                   "material_max_chars", 22000))]),
               _TEXT_SYSTEM, schema=_TEXT_SCHEMA,
               num_predict=int(_cfg(config).get("text_num_predict", 3000)),
               keep_alive=90)   # stránky jdou za sebou — nenahrávat 9×
    j = _json(raw)
    if not isinstance(j, dict):
        return None
    vyr = 0
    def cist(t):
        nonlocal vyr
        vs = _vety(t)
        ok = [v for v in vs if _cista_veta(v, material)]
        vyr += len(vs) - len(ok)
        return " ".join(ok)
    perex = cist(str(j.get("perex") or ""))
    oddily = []
    for o in j.get("oddily") or []:
        if not isinstance(o, dict):
            continue
        ods = o.get("odstavce") or []
        if isinstance(ods, str):
            ods = [ods]
        ods = [x for x in (cist(str(p)) for p in ods) if len(x) > 30]
        if ods:
            oddily.append({"nadpis": str(o.get("nadpis") or "").strip(),
                           "odstavce": ods})
    udalosti = []
    for u in j.get("udalosti") or []:
        if not isinstance(u, dict):
            continue
        rok = str(u.get("rok") or "").strip()
        roky = _CISLO.findall(rok)
        if roky and all(r in material for r in roky) and u.get("co"):
            udalosti.append({"rok": rok, "co": str(u["co"]).strip()})
    znaku = len(perex) + sum(len(p) for o in oddily for p in o["odstavce"])
    if znaku < int(_cfg(config).get("text_min_chars", 400)):
        _log.info("webdilo: text '%s' po kontrole jen %d zn → poznámka", sub, znaku)
        return None
    return {"perex": perex, "oddily": oddily, "udalosti": udalosti,
            "stat": {"znaku": znaku, "vyrazeno_vet": vyr, "model": model}}


def _obsah_html(t: dict) -> str:
    out = ['<p class="perex">%s</p>' % _h.escape(t["perex"])] if t.get("perex") else []
    for o in t.get("oddily", []):
        out.append("<section>")
        if o.get("nadpis"):
            out.append("<h2>%s</h2>" % _h.escape(o["nadpis"]))
        out += ["<p>%s</p>" % _kod(_h.escape(p)) for p in o["odstavce"]]
        out.append("</section>")
    # HANS_WEB_NAVRH_V1 — datované události jako časová osa (dřív jen JSON,
    # který žádná stránka nezobrazila)
    ud = [u for u in (t.get("udalosti") or []) if u.get("rok") and u.get("co")]
    if len(ud) >= 2:
        def _rok(u):
            m = re.search(r"\d{3,4}", str(u["rok"]))
            return int(m.group(0)) if m else 9999
        out.append('<section class="hans-osa-oddil"><h2>Časová osa</h2><ol class="hans-osa">')
        out += ['<li><span class="rok">%s</span>%s</li>' % (_h.escape(str(u["rok"])),
                                                           _kod(_h.escape(u["co"])))
                for u in sorted(ud, key=_rok)]
        out.append("</ol></section>")
    return "\n".join(out)


def _kod(t: str) -> str:
    """HANS_WEB_NAVRH_V1 — `zápis` v textu → <code> (dřív holé zpětné apostrofy)."""
    return re.sub(r"`([^`]{1,120})`", r"<code>\1</code>", t or "")


def _poznamka_jako_text(note: str) -> dict:
    """Záloha: studijní poznámka jako jediný oddíl (dnešní obsah)."""
    return {"perex": "", "oddily": [{"nadpis": "", "odstavce": [
        p.strip() for p in re.split(r"\n\s*\n", note or "") if p.strip()] or [note]}],
        "udalosti": [], "stat": {"znaku": len(note or ""), "zaloha": True}}


# ── 2b) NÁVRHOVÝ SYSTÉM Z VLASTNÍCH POZNÁMEK ─────────────────────────────
# HANS_WEB_NAVRH_V1 (21. 10.) — uživatel: „píše o designu stránek a sám ho
# nepoužívá“. Plán bral výchozí šedé barvy z CSS knihoven; Hansovy poznámky
# (základní barva podle tématu, harmonická paleta, typografická škála) se
# k němu nedostaly. Teď si Hans z poznámek sestaví návrhový systém, kód z něj
# vyrobí CSS proměnné, coder je musí použít a sonda změří, jestli je stránka
# dodržuje. Volba se opírá o doslovnou citaci z poznámky (kontrola kódem).
_NAVRH_TEMATA = re.compile(r"barv|typograf|písm|pism|font|ux|ui\b|přístupn|pristupn|"
                           r"estetik|design|rozvržen|hierarch", re.I)
_PISMA = {
    "bezpatkove": "system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif",
    "patkove": "Georgia, Cambria, 'Times New Roman', Times, serif",
    "humanisticke": "'Gill Sans', 'Trebuchet MS', Calibri, 'Noto Sans', 'DejaVu Sans', sans-serif",
    "geometricke": "Futura, 'Century Gothic', 'URW Gothic', Avenir, sans-serif",
    "strojopisne": "ui-monospace, 'SFMono-Regular', Menlo, Consolas, 'DejaVu Sans Mono', monospace",
}
_ROLE = ("pozadi", "povrch", "text", "text_tlumeny", "primarni", "akcent")
_NAVRH_SYSTEM = (
    "You are a web designer who studied web design and wrote the NOTES below "
    "himself. Design the visual system of a website about the given topic by "
    "APPLYING WHAT YOUR NOTES SAY (how to choose a base colour, build a palette, "
    "use few colours, type scale, hierarchy, whitespace, contrast). Do not use "
    "generic grey defaults; the base colour must suit the topic. For every "
    "decision give the reason and quote, VERBATIM, the sentence from the notes it "
    "follows (copy it exactly, in Czech).")


def _hex(h):
    h = str(h or "").strip()
    if re.fullmatch(r"#[0-9a-fA-F]{6}", h):
        return h.lower()
    if re.fullmatch(r"#[0-9a-fA-F]{3}", h):
        return "#" + "".join(c * 2 for c in h[1:]).lower()
    return None


def _rgb(h):
    return [int(h[i:i + 2], 16) for i in (1, 3, 5)]


def _lum(c):
    a = [v / 255 for v in c]
    a = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in a]
    return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2]


def _kontrast(a, b):
    la, lb = _lum(_rgb(a)), _lum(_rgb(b))
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _dorovnej(barva, pozadi, cil):
    """Ztmav/zesvětli barvu, dokud nemá vůči pozadí kontrast `cil`."""
    if _kontrast(barva, pozadi) >= cil:
        return barva
    tmavsi = _lum(_rgb(pozadi)) > 0.18
    c = _rgb(barva)
    for k in range(1, 21):
        n = [round(v * (1 - k / 20)) if tmavsi else round(v + (255 - v) * k / 20) for v in c]
        h = "#%02x%02x%02x" % tuple(n)
        if _kontrast(h, pozadi) >= cil:
            return h
    return "#000000" if tmavsi else "#ffffff"


def _norm_cit(s):
    return re.sub(r"\s+", " ", re.sub(r"[„“\"'”]", "", s or "")).strip().lower()


def _poznamky_o_designu(db_path: str, notes: list) -> list:
    """Hansovy studijní poznámky o designu z JAKÉHOKOLI programu (dílo o hradech
    má navrhovat podle toho, co se naučil o webu, ne podle poznámek o hradech).
    Poznámky díla samotného jen tehdy, když jsou o designu."""
    out, vid = [], set()
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = con.execute("SELECT title, COALESCE(NULLIF(data,''),note) FROM diary "
                           "WHERE event_type='study_note' ORDER BY ts DESC LIMIT 400").fetchall()
        con.close()
    except Exception:
        rows = []
    for title, text in rows:
        sub = title.split("—", 1)[1].strip() if "—" in (title or "") else (title or "")
        if _NAVRH_TEMATA.search(sub) and sub not in vid and (text or "").strip():
            vid.add(sub); out.append({"sub": sub, "text": text.strip()})
    for n in notes:
        if _NAVRH_TEMATA.search(n.get("sub", "")) and n["sub"] not in vid:
            vid.add(n["sub"]); out.append(n)
    return out


def navrh_system(config: dict, topic: str, notes: list, db_path: str = "",
                 obsah: str = "") -> Optional[dict]:
    vyber = _poznamky_o_designu(db_path, notes) if db_path else \
        [n for n in notes if _NAVRH_TEMATA.search(n.get("sub", ""))]
    if not vyber:
        _log.info("webdilo: návrhový systém — žádné poznámky o designu, vynechávám")
        return None
    text = "\n\n".join("## %s\n%s" % (n["sub"], n["text"]) for n in vyber)[:14000]
    sablona = json.dumps({
        "zakladni_barva": "#rrggbb", "proc_barva_cz": "<1 česká věta>",
        "citace_barva": "<doslovná věta z poznámek>",
        "paleta": {r: "#rrggbb" for r in _ROLE},
        "proc_paleta_cz": "<1 česká věta>", "citace_paleta": "<doslovná věta z poznámek>",
        "pismo_nadpisy": "|".join(_PISMA), "pismo_text": "|".join(_PISMA),
        "zaklad_px": 17, "pomer_skaly": 1.25, "proc_typografie_cz": "<1 česká věta>",
        "citace_typografie": "<doslovná věta z poznámek>",
        "mezera_rem": 1.25, "zaobleni_px": 6}, ensure_ascii=False)
    model = _cfg(config).get("plan_model", "qwen3:30b")
    for pokus in range(2):
        j = _json(_gen(config, model, "Website topic: %s\nWhat the site is about "
                       "(the base colour and mood must suit THIS):\n%s\n\nMY NOTES ON "
                       "DESIGN (Czech):\n%s\n\nAnswer ONLY this JSON (replace every "
                       "<...>, choose one font family name from the list):\n%s"
                       % (topic, obsah or topic, text, sablona),
                       _NAVRH_SYSTEM, schema="json", num_predict=1500, temperature=0.4,
                       num_gpu=None, timeout=1200))
        ds = _over_navrh(j, text)
        if ds:
            return ds
        _log.warning("webdilo: návrhový systém pokus %d nepoužitelný", pokus + 1)
    return None


def _over_navrh(j, poznamky: str) -> Optional[dict]:
    if not isinstance(j, dict):
        return None
    pal = {r: _hex((j.get("paleta") or {}).get(r)) for r in _ROLE}
    if not all(pal[r] for r in ("pozadi", "text", "primarni")):
        return None
    pal["povrch"] = pal["povrch"] or pal["pozadi"]
    pal["akcent"] = pal["akcent"] or pal["primarni"]
    pal["text_tlumeny"] = pal["text_tlumeny"] or pal["text"]
    # kontrast hlídá kód (WCAG 4,5:1), ne model
    for r, cil in (("text", 7.0), ("text_tlumeny", 4.5), ("primarni", 4.5)):
        pal[r] = _dorovnej(pal[r], pal["pozadi"], cil)
        pal[r] = _dorovnej(pal[r], pal["povrch"], 4.5)
    pn = _norm_cit(poznamky)
    opory = {}
    for k in ("barva", "paleta", "typografie"):
        c = str(j.get("citace_" + k) or "").strip()
        opory[k] = {"proc": str(j.get("proc_%s_cz" % k) or "").strip()[:300],
                    "citace": c if len(_norm_cit(c)) >= 25 and _norm_cit(c)[:60] in pn else ""}
    try:
        zaklad = min(20.0, max(16.0, float(j.get("zaklad_px") or 17)))
        pomer = min(1.414, max(1.125, float(j.get("pomer_skaly") or 1.25)))
        mezera = min(2.0, max(0.75, float(j.get("mezera_rem") or 1.25)))
        zaobl = min(16, max(0, int(float(j.get("zaobleni_px") or 6))))
    except Exception:
        zaklad, pomer, mezera, zaobl = 17.0, 1.25, 1.25, 6
    ph = str(j.get("pismo_nadpisy") or "").strip().lower()
    pt = str(j.get("pismo_text") or "").strip().lower()
    return {"paleta": pal, "zakladni_barva": _hex(j.get("zakladni_barva")) or pal["primarni"],
            "pismo_nadpisy": ph if ph in _PISMA else "bezpatkove",
            "pismo_text": pt if pt in _PISMA else "bezpatkove",
            "skala": [round(zaklad * pomer ** i, 1) for i in range(-1, 6)],
            "zaklad_px": zaklad, "pomer": pomer, "mezera_rem": mezera, "zaobleni_px": zaobl,
            "opory": opory}


def _fs_jmeno(i: int) -> str:
    return "fs-s" if i == 0 else "fs-%d" % (i - 1)


def navrh_css(ds: dict) -> str:
    p, s = ds["paleta"], ds["skala"]
    v = ["--barva-%s:%s" % (r.replace("_", "-"), p[r]) for r in _ROLE]
    # v rem (Hansovo pravidlo: velikost písma v relativních jednotkách)
    v += ["--%s:%.3frem" % (_fs_jmeno(i), x / 16.0) for i, x in enumerate(s)]
    v += ["--pismo-nadpisy:%s" % _PISMA[ds["pismo_nadpisy"]],
          "--pismo-text:%s" % _PISMA[ds["pismo_text"]],
          "--mezera:%grem" % ds["mezera_rem"], "--zaobleni:%dpx" % ds["zaobleni_px"]]
    return (":root{%s}\n"
            "body{background:var(--barva-pozadi);color:var(--barva-text);"
            "font-family:var(--pismo-text);font-size:var(--fs-0);line-height:1.6}\n"
            "h1,h2,h3{font-family:var(--pismo-nadpisy);line-height:1.2}"
            "h1{font-size:var(--fs-4)}h2{font-size:var(--fs-2)}h3{font-size:var(--fs-1)}\n"
            "a{color:var(--barva-primarni)}"
            "code{font-family:%s;font-size:.92em;background:var(--barva-povrch);"
            "padding:.1em .35em;border-radius:4px}\n"
            ".hans-osa{list-style:none;margin:calc(var(--mezera)*1.5) 0;padding:0 0 0 1.25rem;"
            "border-left:3px solid var(--barva-akcent)}"
            ".hans-osa li{margin:0 0 var(--mezera);position:relative}"
            ".hans-osa li::before{content:'';position:absolute;left:-1.72rem;top:.45em;"
            "width:.7rem;height:.7rem;border-radius:50%%;background:var(--barva-akcent)}"
            ".hans-osa .rok{display:block;font-weight:700;color:var(--barva-primarni)}"
            % (";".join(v), _PISMA["strojopisne"]))


def navrh_pro_kodera(ds: dict) -> str:
    p = ds["paleta"]
    return ("HANS'S DESIGN SYSTEM (derived from his own study notes — follow it):\n"
            "Colours (use ONLY these, through the CSS variables): "
            + ", ".join("--barva-%s %s" % (r.replace("_", "-"), p[r]) for r in _ROLE)
            + ".\nType scale (use ONLY these font sizes): "
            + ", ".join("--" + _fs_jmeno(i) for i in range(len(ds["skala"])))
            + " = " + ", ".join("%gpx" % x for x in ds["skala"])
            + ".\nFonts: headings var(--pismo-nadpisy), text var(--pismo-text). Spacing "
            "unit var(--mezera), corner radius var(--zaobleni). The :root block with "
            "these variables is already in every page — do not redefine them, do not "
            "add other colours or font sizes. Make the base colour visible (headers, "
            "links, accents) and use generous whitespace.")


def navrh_cz(ds: dict) -> str:
    """Věta do kritiky díla: co Hans zvolil a o kterou svou poznámku se opřel."""
    out = []
    for k, co in (("barva", "Hlavní barvu %s jsem zvolil" % ds["paleta"]["primarni"]),
                  ("typografie", "Typografickou škálu %g × %g px jsem zvolil"
                   % (ds["pomer"], ds["zaklad_px"]))):
        o = ds["opory"].get(k) or {}
        if o.get("citace"):
            out.append("%s podle své poznámky: „%s“" % (co, o["citace"][:200]))
        elif o.get("proc"):
            out.append("%s, protože %s (doslovnou oporu v poznámkách jsem nenašel)"
                       % (co, o["proc"][:200].rstrip(".").lower()))
    return (". ".join(out) + ".") if out else ""


# ── 3) VOLBA NÁSTROJŮ ─────────────────────────────────────────────────────

_VOLBA_SYSTEM = (
    "You are the art director and front-end lead of a small studio. You receive "
    "FINISHED content (texts, images, dated events) for a multi-page website. "
    "Decide HOW to present it so the site looks modern and professional, and "
    "choose the technologies yourself. Anything is allowed (plain HTML/CSS, modern "
    "CSS, JavaScript, npm libraries, SVG, canvas…) under these rules: no build "
    "step; everything is downloaded and served locally offline (web fonts only as "
    "npm @fontsource/<font> packages or system fonts, NEVER Google Fonts links); "
    "every choice must be justified by a CONCRETE content feature from the "
    "inventory (numbers matter: a slider needs several images on the same page, a "
    "timeline needs enough dated events). Prefer few, well-known libraries; none "
    "is a valid choice. The previous works' review notes are lessons — use them.")

_VOLBA_SCHEMA = {
    "type": "object",
    "properties": {
        "concept": {"type": "string"},
        "zduvodneni_cz": {"type": "string"},
        "layout": {"type": "string"},
        "typography_colors": {"type": "string"},
        "components": {"type": "array", "items": {
            "type": "object",
            "properties": {"what": {"type": "string"},
                           "where": {"type": "string"},
                           "content_reason": {"type": "string"}},
            "required": ["what", "where", "content_reason"]}},
        "libraries": {"type": "array", "items": {
            "type": "object",
            "properties": {"npm_package": {"type": "string"},
                           "purpose": {"type": "string"}},
            "required": ["npm_package", "purpose"]}},
    },
    "required": ["concept", "zduvodneni_cz", "layout", "typography_colors",
                 "components", "libraries"],
}

_POZADAVKY = (   # (vzor komponenty, co obsah musí mít, kontrola)
    (re.compile(r"slider|carousel|gallery|galer|lightbox|slideshow", re.I),
     "aspoň 3 obrázky na stránce", lambda inv: max(
         (p["obrazky"] for p in inv["stranky"]), default=0) >= 3),
    (re.compile(r"timeline|časov|chronolog", re.I),
     "aspoň 4 datované události na některé stránce", lambda inv: max(
         (p["udalosti"] for p in inv["stranky"]), default=0) >= 4),
    (re.compile(r"\bmap\b|mapa|leaflet", re.I),
     "souřadnice míst (dílo je nemá)", lambda inv: False),
)


def inventar(topic: str, stranky: list) -> dict:
    return {"tema": topic, "stranky": [
        {"nadpis": s["sub"], "znaku": s["text"]["stat"]["znaku"],
         "oddilu": len(s["text"]["oddily"]), "udalosti": len(s["text"]["udalosti"]),
         "obrazky": 1, "ukazka": (s["text"].get("perex") or
                                  (s["text"]["oddily"][0]["odstavce"][0]
                                   if s["text"]["oddily"] else ""))[:200]}
        for s in stranky]}


def zkontroluj_volbu(plan: dict, inv: dict) -> list:
    """Vyřadí komponenty, na které obsah nemá. Vrací seznam vyřazených (důvody)."""
    pryc, zustava = [], []
    for c in plan.get("components") or []:
        txt = "%s %s" % (c.get("what", ""), c.get("where", ""))
        vadi = next((p for p in _POZADAVKY if p[0].search(txt) and not p[2](inv)), None)
        if vadi:
            pryc.append("%s — obsah nemá: %s" % (c.get("what"), vadi[1]))
        else:
            zustava.append(c)
    plan["components"] = zustava
    libs = []
    for l in plan.get("libraries") or []:
        txt = "%s %s" % (l.get("npm_package", ""), l.get("purpose", ""))
        vadi = next((p for p in _POZADAVKY if p[0].search(txt) and not p[2](inv)), None)
        if vadi:
            pryc.append("knihovna %s — obsah nemá: %s" % (l.get("npm_package"), vadi[1]))
        else:
            libs.append(l)
    plan["libraries"] = libs
    return pryc


def volba(config: dict, inv: dict, lekce: str = "") -> Optional[dict]:
    model = _cfg(config).get("plan_model", "qwen3:30b")
    prompt = ("Website topic: %s\nContent inventory (Czech):\n%s\n\n"
              "Lessons from reviews of previous works:\n%s\n\n"
              "Write zduvodneni_cz in Czech: 2-3 sentences, first person, why this "
              "form suits this content."
              % (inv["tema"], json.dumps(inv["stranky"], ensure_ascii=False, indent=1),
                 lekce or "(none yet)"))
    # ⚠️ BEZ JSON schématu: qwen3 + schéma + think:false vepsal 26. 9. celou
    # úvahu do `concept` a do ostatních polí jen popisky („layout“). Pilot
    # s format:"json" a šablonou v zadání fungoval → tak to zůstává.
    sablona = json.dumps({
        "concept": "<1-2 sentences: the visual idea>",
        "zduvodneni_cz": "<2-3 české věty v 1. osobě>",
        "layout": "<concrete layout of landing page and article pages>",
        "typography_colors": "<fonts and hex colors>",
        "components": [{"what": "...", "where": "...", "content_reason": "..."}],
        "libraries": [{"npm_package": "...", "purpose": "..."}]}, ensure_ascii=False)
    for pokus in range(2):
        # qwen3:30b (17,5 GB) se do 16 GB VRAM celý nevejde — num_gpu 99 = 500
        plan = _json(_gen(config, model, prompt + "\n\nAnswer ONLY this JSON "
                          "(replace every <...>):\n" + sablona, _VOLBA_SYSTEM,
                          schema="json", num_predict=2000, temperature=0.4,
                          num_gpu=None, timeout=1200))
        vada = _vada_planu(plan)
        if not vada:
            return plan
        _log.warning("webdilo: volba pokus %d nepoužitelná (%s)", pokus + 1, vada)
    return None


def _vada_planu(plan) -> str:
    """Plán, který je jen popisek nebo úvaha, se nepoužije."""
    if not isinstance(plan, dict):
        return "není JSON"
    for k in ("concept", "zduvodneni_cz", "layout", "typography_colors"):
        v = str(plan.get(k) or "").strip()
        if len(v) < 25 or v.startswith("<"):
            return "pole %s je prázdné nebo popisek" % k
        if len(v) > 1500:
            return "pole %s je úvaha, ne plán (%d zn)" % (k, len(v))
    return ""


# ── knihovny: ověř v registru, stáhni k dílu ──────────────────────────────

def _jsd(url: str, timeout=20):
    import requests
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r


def stahni_knihovny(libs: list, dest: Path, limit: int = 3) -> tuple:
    """Vrací ([{npm, version, js, css}], [chyby]). Cesty relativně k dílu."""
    ok, chyby = [], []
    for l in (libs or [])[:limit]:
        pkg = re.sub(r"[^a-z0-9@/._-]", "", str(l.get("npm_package") or "").lower())
        if not pkg:
            continue
        try:
            ver = _jsd("https://data.jsdelivr.com/v1/packages/npm/%s/resolved"
                       "?specifier=latest" % pkg).json().get("version")
            if not ver:
                raise ValueError("balíček nenalezen")
            base = "https://cdn.jsdelivr.net/npm/%s@%s" % (pkg, ver)
            rec = {"npm": pkg, "version": ver, "purpose": l.get("purpose", "")}
            ldir = dest / "lib" / pkg.replace("/", "_")
            ldir.mkdir(parents=True, exist_ok=True)
            if pkg.startswith("@fontsource/"):
                css = _jsd(base + "/index.css").text
                css2 = css
                for f in set(re.findall(r"url\(\.?/?(files/[^)]+?\.woff2)\)", css)):
                    if "latin" not in f:
                        continue
                    (ldir / "files").mkdir(exist_ok=True)
                    (ldir / f).write_bytes(_jsd(base + "/" + f).content)
                # jen latin + latin-ext (čeština); ostatní @font-face pryč
                css2 = "\n".join(b for b in re.findall(r"/\*[^*]*\*/\s*@font-face\s*\{[^}]*\}|@font-face\s*\{[^}]*\}", css)
                                 if "latin" in b)
                (ldir / "index.css").write_text(css2 or css, encoding="utf-8")
                rec["css"] = "lib/%s/index.css" % ldir.name
            else:
                ep = _jsd("https://data.jsdelivr.com/v1/packages/npm/%s@%s/entrypoints"
                          % (pkg, ver)).json().get("entrypoints") or {}
                for kind in ("js", "css"):
                    f = (ep.get(kind) or {}).get("file")
                    if not f:
                        continue
                    data = _jsd(base + f).content
                    if len(data) > 1_500_000:
                        raise ValueError("%s příliš velký (%d B)" % (f, len(data)))
                    name = os.path.basename(f)
                    (ldir / name).write_bytes(data)
                    rec[kind] = "lib/%s/%s" % (ldir.name, name)
            if rec.get("js") or rec.get("css"):
                ok.append(rec)
            else:
                chyby.append("%s: bez souboru ke stažení" % pkg)
        except Exception as e:
            chyby.append("%s: %s" % (pkg, e))
    return ok, chyby


# ── 4) STAVBA ─────────────────────────────────────────────────────────────

_STAVBA_SYSTEM = (
    "You are a senior front-end developer. Implement the art director's plan "
    "exactly. Use ONLY the local library files listed (exact relative paths); no "
    "other external URLs, no Google Fonts. Must be responsive (no horizontal "
    "scroll at 390px), accessible (alt texts, contrast ≥ 4.5:1), semantic HTML. "
    "Professional basics: the footer stays in normal document flow (never "
    "position:fixed/sticky over content); site navigation with many or long page "
    "titles must stay readable and must not overflow (e.g. a vertical list or a "
    "<details> menu on narrow screens); every text has sufficient contrast with "
    "ITS OWN background; one restrained colour palette from the plan; images "
    "max-width:100%; long words may wrap (overflow-wrap:anywhere). The art "
    "director's plan and its justification are INTERNAL — never show them on the "
    "page; write a real short intro about the topic instead. No template filler "
    "(\u201eVáš název\u201c, lorem ipsum, example.com, a made-up year): the footer "
    "names the site topic. "
    "Write all text in Czech. Return ONLY the complete HTML document.")

_PLACEHOLDERY = (
    "Use these placeholders literally (the system fills them per page):\n"
    "{{TEMA}} site topic · {{NAZEV}} page title · {{OBSAH}} article body HTML "
    "(already contains <p class=\"perex\">, <section><h2>, <p>) · {{OBRAZ}} image "
    "src · {{OBRAZ_ALT}} image alt · {{PREDCHOZI}} / {{DALSI}} hrefs of previous / "
    "next page · {{NAV}} list of <li><a> links to all pages · {{DATA}} JSON "
    "{\"udalosti\": [{\"rok\", \"co\"}]} of this page — put it EXACTLY as "
    "<script type=\"application/json\" id=\"data-stranky\">{{DATA}}</script>; any "
    "JavaScript component (e.g. timeline) must read its data from there and must "
    "render nothing when the list is empty.")


def _lib_tagy(libs: list) -> str:
    return "\n".join(
        (['<link rel="stylesheet" href="%s">' % l["css"]] if l.get("css") else [])
        + (['<script src="%s" defer></script>' % l["js"]] if l.get("js") else [])
        for l in libs) if libs else ""


def _html_z(raw: str) -> str:
    m = re.search(r"<!DOCTYPE[\s\S]*</html>|<html[\s\S]*</html>", raw or "", re.I)
    return m.group(0) if m else ""


def postav(config: dict, plan: dict, libs: list, topic: str, stranky: list,
           oprava: dict = None, prirucka: str = "") -> tuple:
    """(index_html, sablona_html). `oprava` = {"index": [vady], "sablona": [vady],
    "stary": (index, sablona)} → opravné kolo nad předchozí verzí."""
    from scripts import hans_maker as hm
    model = hm._coder_model(config)
    spolecne = ("ART DIRECTOR PLAN:\n%s\n\nLOCAL LIBRARIES (include exactly):\n%s\n"
                % (json.dumps(plan, ensure_ascii=False, indent=1),
                   _lib_tagy(libs) or "(none)"))
    if prirucka:           # HANS_PRIRUCKA_V1 — co se Hans sám naučil
        spolecne += "\n" + prirucka + "\n"
    karty = json.dumps([{"href": "detail-%s.html" % s["slug"], "title": s["sub"],
                         "teaser": _zkrat((s["text"].get("perex") or ""), 160),
                         "image": "GEN:%s" % s["img"]} for s in stranky],
                       ensure_ascii=False, indent=1)
    def jedna(co, zadani, stare=None, vady=None):
        if oprava is not None and not vady:
            return stare           # opravné kolo: bez vad se nesahá
        if stare and vady and (oprava or {}).get("css_nejdriv") and all(
                _CSS_VADA.search(v) for v in vady):
            # HANS_WEB_CSS_REPAIR_V1 — vzhledové vady opravuj DOPLŇKEM CSS:
            # 26. 9. coder vracel celý dokument beze změny (kontrast 4,13 u
            # patičky zůstal 2 kola); krátký blok CSS nemůže rozbít placeholdery.
            raw = _gen(config, model, spolecne + "\nThe page below has these visual "
                       "defects:\n- " + "\n- ".join(vady) + "\n\nReturn ONLY one "
                       "<style> block with CSS overrides that fix them (use selectors "
                       "that exist in the page; e.g. darker text colour for low "
                       "contrast). No other text.\n\nPAGE:\n" + stare,
                       _STAVBA_SYSTEM, num_predict=1200, temperature=0.2,
                       num_gpu=_cfg(config).get("coder_num_gpu", 99))
            m = re.search(r"<style[^>]*>[\s\S]*?</style>", raw or "", re.I)
            if m and "</head>" in stare:
                return stare.replace("</head>", m.group(0) + "\n</head>", 1)
            _log.warning("webdilo: CSS oprava (%s) bez <style> — %r", co, (raw or "")[:120])
            return stare
        if stare and vady:
            # ⚠️ U šablony MUSÍ opravné zadání nést i placeholdery: bez nich
            # model {{OBSAH}} a spol. „dovyplnil“ nebo vyhodil a všech 8 oprav
            # 26. 9. skončilo v koši (web zůstal v původní verzi).
            drz = ("\n\nThis is a TEMPLATE. " + _PLACEHOLDERY + "\nKeep EVERY "
                   "{{PLACEHOLDER}} exactly as it is in the current version."
                   if co == "template" else "")
            p = (spolecne + "\nFIX these defects found by the review, keep "
                 "everything else:\n- " + "\n- ".join(vady) + drz
                 + "\n\nCURRENT %s:\n%s" % (co, stare))
        else:
            p = spolecne + "\n" + zadani
        raw = _gen(config, model, p, _STAVBA_SYSTEM, num_predict=7000, temperature=0.3,
                   num_gpu=_cfg(config).get("coder_num_gpu", 99))   # None = Ollama sama
        h = _html_z(raw)
        if not h:
            _log.warning("webdilo: coder (%s) nevrátil celé HTML — %d zn, konec: %r",
                         co, len(raw or ""), (raw or "")[-120:])
        return h
    stary = (oprava or {}).get("stary") or (None, None)
    idx = jedna("index.html",
                "Build index.html — landing page of the site „%s“ (Czech). Hero "
                "with a heading and the image GEN:%s, a short intro, then one card "
                "per page (image, title, teaser, link), footer. Pages:\n%s"
                % (topic, stranky[0]["img"] if stranky else topic, karty),
                stary[0], (oprava or {}).get("index"))
    sab = jedna("template",
                "Build the article page TEMPLATE shared by all subpages (link to "
                "index.html, site navigation, article with image, prev/next).\n"
                + _PLACEHOLDERY, stary[1], (oprava or {}).get("sablona"))
    if oprava is not None:     # co se neopravilo, zůstává
        idx = idx or stary[0]
        sab = sab or stary[1]
    return idx, sab


def _zkrat(t: str, n: int) -> str:
    """HANS_WEB_UZKY_SLOUPEC_V1 — zkrácení na celé slovo s „…“ (karty končily „rozl.“)."""
    t = (t or "").strip()
    return t if len(t) <= n else t[:n].rsplit(" ", 1)[0].rstrip(",;:–-") + "…"


def _stitek(sub: str, maxlen: int = 32) -> str:
    """Krátký štítek do navigace: část před dvojtečkou, zkrácená na slovo."""
    t = sub.split(":")[0].strip()
    if len(t) <= maxlen:
        return t
    return t[:maxlen].rsplit(" ", 1)[0].rstrip(",;–-") + "…"


_POVINNE = ("{{OBSAH}}", "{{NAZEV}}", "{{OBRAZ}}")
_CSS_VADA = re.compile(r"contrast|overflow|font sizes|line length|handbook rule", re.I)


def _chybi_placeholdery(sab: str) -> list:
    return [p for p in _POVINNE if p not in (sab or "")]


def vypln(sablona: str, topic: str, stranky: list) -> dict:
    nav = "\n".join('<li><a href="detail-%s.html" title="%s">%s</a></li>'
                    % (s["slug"], _h.escape(s["sub"]), _h.escape(_stitek(s["sub"])))
                    for s in stranky)
    out = {}
    for i, s in enumerate(stranky):
        pred = "detail-%s.html" % stranky[i - 1]["slug"] if i > 0 else "index.html"
        dal = ("detail-%s.html" % stranky[i + 1]["slug"]
               if i + 1 < len(stranky) else "index.html")
        data = json.dumps({"udalosti": s["text"]["udalosti"]},
                          ensure_ascii=False).replace("</", "<\\/")
        h = sablona
        for k, v in (("{{TEMA}}", _h.escape(topic)), ("{{NAZEV}}", _h.escape(s["sub"])),
                     ("{{OBSAH}}", _obsah_html(s["text"])),
                     ("{{OBRAZ}}", "GEN:%s" % s["img"]),
                     ("{{OBRAZ_ALT}}", _h.escape(s["sub"])),
                     ("{{PREDCHOZI}}", pred), ("{{DALSI}}", dal),
                     ("{{NAV}}", nav), ("{{DATA}}", data)):
            h = h.replace(k, v)
        out["detail-%s.html" % s["slug"]] = h
    return out


# ── 5) KONTROLA ───────────────────────────────────────────────────────────

_SONDA = r"""<script>
window.__E=[];addEventListener('error',function(e){if(e.target&&e.target!==window&&e.target.tagName)return;__E.push(String(e.message||e.type))},true);
addEventListener('load',function(){setTimeout(function(){
 var all=[].slice.call(document.querySelectorAll('body *'));
 var bad=[].slice.call(document.images).filter(function(i){return !i.naturalWidth}).length;
 var over=all.filter(function(e){var r=e.getBoundingClientRect();return r.width>0&&r.right>innerWidth+2&&!(e.parentElement&&e.parentElement.getBoundingClientRect().right>innerWidth+2)}).slice(0,3).map(function(e){return e.tagName.toLowerCase()+(typeof e.className==='string'&&e.className?'.'+e.className.split(' ')[0]:'')+'('+Math.round(e.getBoundingClientRect().right)+'px)'});
 function rgb(s){var m=(s||'').match(/[\d.]+/g);return m?m.map(Number):null}
 function lum(c){var a=c.slice(0,3).map(function(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)});return 0.2126*a[0]+0.7152*a[1]+0.0722*a[2]}
 function pozadi(e){while(e&&e.nodeType===1){var cs=getComputedStyle(e);if(cs.backgroundImage&&cs.backgroundImage!=='none')return null;var c=rgb(cs.backgroundColor);if(c&&(c.length<4||c[3]>0.5))return c;e=e.parentElement}return [255,255,255]}
 var low=[],sizes={},kn=99,kv=99,fx={};
 function sel(e){var p=[],n=e;for(var i=0;i<3&&n&&n!==document.body&&n.tagName;i++){var s=n.tagName.toLowerCase();if(typeof n.className==='string'&&n.className.trim())s+='.'+n.className.trim().split(/\s+/)[0].replace(/[^\w-]/g,'');p.unshift(s);n=n.parentElement}return p.join(' ')}
 function adj(f,b,t){var Lb=lum(b),tm=Lb>0.18;for(var k=0;k<=20;k++){var c=f.slice(0,3).map(function(v){return Math.round(tm?v*(1-k/20):v+(255-v)*k/20)});var L=lum(c);if((Math.max(L,Lb)+0.05)/(Math.min(L,Lb)+0.05)>=t)return 'rgb('+c.join(',')+')'}return tm?'#000':'#fff'}
 all.forEach(function(e){
  var t=[].slice.call(e.childNodes).some(function(n){return n.nodeType===3&&n.textContent.trim().length>1});
  if(!t)return;var r=e.getBoundingClientRect();if(!r.width||!r.height)return;
  var cs=getComputedStyle(e);if(cs.visibility==='hidden'||+cs.opacity<0.1)return;
  sizes[Math.round(parseFloat(cs.fontSize))]=1;
  var b=pozadi(e);if(!b)return;var f=rgb(cs.color);if(!f)return;
  var L1=lum(f),L2=lum(b),cr=(Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05);
  var fs=parseFloat(cs.fontSize),big=fs>=24||(fs>=18.66&&+cs.fontWeight>=700);
  if(big)kv=Math.min(kv,cr);else kn=Math.min(kn,cr);
  var cil=(big?__KVP__:__KNP__);if(cr<cil){var sl=sel(e);if(!(sl in fx))fx[sl]=adj(f,b,cil+0.2)}
  if(cr<(big?3:4.5))low.push(e.tagName.toLowerCase()+' '+cr.toFixed(1)+':1 „'+e.textContent.trim().slice(0,24)+'“');
 });
 var enc=encodeURIComponent;
 var ps=[].slice.call(document.querySelectorAll('p')).filter(function(p){return p.innerText.trim().length>80&&p.getBoundingClientRect().height>0});
 var dl=[],rh=[],pf=[];
 ps.forEach(function(p){var cs=getComputedStyle(p),fs=parseFloat(cs.fontSize),lh=parseFloat(cs.lineHeight)||fs*1.2,h=p.getBoundingClientRect().height,n=Math.max(1,Math.round(h/lh));dl.push(p.innerText.length/n);rh.push(lh/fs);pf.push(fs)});
 function med(a){if(!a.length)return -1;a=a.slice().sort(function(x,y){return x-y});return a[Math.floor(a.length/2)]}
 var cile=[].slice.call(document.querySelectorAll('button,nav a,header a,[role=button],a.btn,a.button')).map(function(e){var r=e.getBoundingClientRect();return Math.min(r.width,r.height)}).filter(function(v){return v>0});
 var ff=getComputedStyle(document.body).fontFamily.toLowerCase(),gen=/(serif|sans-serif|monospace|system-ui|cursive|fantasy)\s*$/.test(ff)?1:0;
 var css='';[].slice.call(document.styleSheets).forEach(function(s){try{[].slice.call(s.cssRules).forEach(function(r){css+=r.cssText+'\n'})}catch(x){}});
 [].slice.call(document.querySelectorAll('[style]')).forEach(function(e){css+=e.getAttribute('style')+';\n'});
 var mq=/@media[^{]*\((min|max)-width/i.test(css)?1:0;
 var fsd=(css.match(/font-size\s*:\s*[^;}]+/gi)||[]),frel=fsd.filter(function(x){return /(rem|em|vw|vh|%|clamp|calc)/i.test(x)}).length,rp=(!fsd.length||frel/fsd.length>=0.8)?1:0;
 var fg=all.some(function(e){return /^(inline-)?(flex|grid)$/.test(getComputedStyle(e).display)})?1:0;
 var imgs=[].slice.call(document.images),fi=imgs.every(function(i){var r=i.getBoundingClientRect(),q=i.parentElement;return getComputedStyle(i).maxWidth!=='none'&&(!r.width||!q||r.width<=q.getBoundingClientRect().width+1)})?1:0,na=imgs.filter(function(i){return !i.hasAttribute('alt')}).length;
 var lg=(document.documentElement.getAttribute('lang')||'').trim()?1:0;
 var sem=['header','nav','main','footer'].filter(function(t){return document.querySelector(t)}).length;
 var anim=/@keyframes|animation(-name)?\s*:\s*(?!none)|transition(-property)?\s*:\s*(?!none)/i.test(css),pm=anim?(/prefers-reduced-motion/i.test(css)?1:0):1;
 var BOXP=/^(width|height|top|left|right|bottom|margin|padding)/i,bx=0;
 [].slice.call(document.styleSheets).forEach(function(s){try{[].slice.call(s.cssRules).forEach(function(r){if(r.type===7)[].slice.call(r.cssRules).forEach(function(k){for(var i=0;i<k.style.length;i++)if(BOXP.test(k.style[i]))bx++})})}catch(x){}});
 all.forEach(function(e){var cs=getComputedStyle(e);if(cs.transitionDuration&&cs.transitionDuration.split(',').some(function(d){return parseFloat(d)>0})&&cs.transitionProperty.split(',').some(function(p){return BOXP.test(p.trim())}))bx++});
 var mo=[];ps.forEach(function(p){var cs=getComputedStyle(p),lh=parseFloat(cs.lineHeight)||parseFloat(cs.fontSize)*1.2,n=p.nextElementSibling,mt=n?parseFloat(getComputedStyle(n).marginTop)||0:0;if(n&&n.tagName==='P')mo.push(Math.max(parseFloat(cs.marginBottom)||0,mt)/lh)});
 var fl={};all.forEach(function(e){var r=e.getBoundingClientRect();if(!(r.width>0&&r.right>innerWidth+2))return;var q=e.parentElement;for(var i=0;i<4&&q&&q!==document.body;i++,q=q.parentElement){var cs=getComputedStyle(q);if(/flex/.test(cs.display)&&/^row/.test(cs.flexDirection)&&cs.flexWrap==='nowrap'&&q.children.length>=3){fl[sel(q)]=q.children.length;break}}});
 var pw=ps.map(function(p){return p.getBoundingClientRect().width}),gq='';
 if(ps.length){var q=ps[0].parentElement;for(var i=0;i<6&&q&&q!==document.body;i++,q=q.parentElement){var cs=getComputedStyle(q);if(cs.display==='grid'&&cs.gridTemplateColumns.split(' ').length>1){gq=sel(q)+' ('+cs.gridTemplateColumns.split(' ').length+' columns, '+q.children.length+' children)';break}}}
 var cols={};all.forEach(function(e){var r=e.getBoundingClientRect();if(!r.width||!r.height)return;var cs=getComputedStyle(e);if(cs.visibility==='hidden')return;var tx=[].slice.call(e.childNodes).some(function(n){return n.nodeType===3&&n.textContent.trim().length>1});var f=rgb(cs.color),b=rgb(cs.backgroundColor);if(tx&&f)cols[f.slice(0,3).join(',')]=1;if(b&&(b.length<4||b[3]>0.5))cols[b.slice(0,3).join(',')]=1});
 document.title='SONDA COL='+enc(Object.keys(cols).slice(0,40).join(';'))+' FSL='+Object.keys(sizes).join(',')+' PW='+(pw.length?med(pw).toFixed(0):-1)+' GQ='+enc(gq)+' FL='+enc(JSON.stringify(Object.keys(fl).slice(0,3).map(function(k){return [k,fl[k]]})))+' MQ='+mq+' RP='+rp+' FG='+fg+' FI='+fi+' NA='+na+' LG='+lg+' SEM='+sem+' PM='+pm+' BX='+bx+' MO='+(mo.length?med(mo).toFixed(2):-1)+' DL='+med(dl).toFixed(0)+' RH='+med(rh).toFixed(2)+' PF='+(pf.length?Math.min.apply(null,pf):-1)+' TS='+(cile.length?Math.min.apply(null,cile).toFixed(0):-1)+' NV='+document.querySelectorAll('nav a').length+' GF='+gen+' KN='+kn.toFixed(2)+' KV='+kv.toFixed(2)+' E='+__E.length+' SW='+document.documentElement.scrollWidth+' IW='+innerWidth+' IMG='+bad+' H1='+document.querySelectorAll('h1').length+' TXT='+document.body.innerText.length+' M='+enc(__E.slice(0,3).join('|'))+' O='+enc(over.join(','))+' K='+low.length+' KM='+enc(low.slice(0,3).join(' | '))+' F='+Object.keys(sizes).length+' FX='+enc(JSON.stringify(Object.keys(fx).slice(0,25).map(function(k){return [k,fx[k]]})));
},1500)});
</script>"""


class _Server:
    def __init__(self, root: str):
        import http.server, socketserver, functools
        class Q(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        self.srv = socketserver.TCPServer(("127.0.0.1", 0),
                                          functools.partial(Q, directory=root))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown(); self.srv.server_close()


_ZAKLAD_CSS = ("<style>*,*::before,*::after{box-sizing:border-box}"
               "img,video,svg{max-width:100%;height:auto}</style>")
_VYPLN = re.compile(r"V[áa][šs] n[áa]zev|lorem ipsum|example\.com|your (site|name|title)|Tesslate|Vytvo[řr]eno (s|pomoc[íi])"
                    r"|and so on for|repeat (the above|for each)|(more|other) (cards|articles) here"
                    r"|\{\{[^}]{1,60}\}\}", re.I)   # HANS_WEB_NAVRH_V1

_CHROM = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
          "--hide-scrollbars", "--disable-extensions", "--no-first-run"]


def _json_list(t: str) -> list:
    try:
        x = json.loads(t)
        return x if isinstance(x, list) else []
    except Exception:
        return []


def _sonda_js(prahy: tuple) -> str:
    """Sonda s Hansovými prahy kontrastu (běžný, velký text)."""
    return (_SONDA.replace("__KNP__", "%g" % prahy[0])
            .replace("__KVP__", "%g" % prahy[1]))


def _sonda(url: str, w: int) -> dict:
    r = subprocess.run(_CHROM + ["--window-size=%d,900" % w, "--virtual-time-budget=6000",
                                 "--dump-dom", url], capture_output=True, text=True,
                       timeout=90)
    t = re.search(r"<title>SONDA (.*?)</title>", r.stdout or "")
    if not t:
        return {"chyba": "sonda nedoběhla"}
    d = dict(x.split("=", 1) for x in t.group(1).split(" ") if "=" in x)
    from urllib.parse import unquote
    return {"js_chyby": int(d.get("E", 0)), "sirka": int(d.get("SW", 0)),
            "okno": int(d.get("IW", 0)), "rozbite_obr": int(d.get("IMG", 0)),
            "h1": int(d.get("H1", 0)), "text": int(d.get("TXT", 0)),
            "zpravy": unquote(d.get("M", "")), "pretece": unquote(d.get("O", "")),
            "flex_radky": _json_list(unquote(d.get("FL", "") or "[]")),   # HANS_WEB_PRETECENI_V1
            "sirka_odstavce": int(float(d.get("PW", -1))),           # HANS_WEB_UZKY_SLOUPEC_V1
            "barvy": [x for x in unquote(d.get("COL", "")).split(";") if x],   # HANS_WEB_NAVRH_V1
            "velikosti": [float(x) for x in (d.get("FSL") or "").split(",") if x],
            "mrizka": unquote(d.get("GQ", "")),
            "kontrast": int(d.get("K", 0)), "kontrast_ukazky": unquote(d.get("KM", "")),
            "velikosti_pisma": int(d.get("F", 0)),
            "fixy": _json_list(unquote(d.get("FX", "") or "[]")),
            # HANS_PRIRUCKA_V1 — hodnoty pro měřená pravidla příručky
            "m": {k: (None if float(d.get(z, -1)) < 0 or float(d.get(z, -1)) >= 99 else float(d[z]))
                  for k, z in (("delka_radku", "DL"), ("radkovani", "RH"),
                               ("velikost_pisma_textu", "PF"), ("velikost_cilu", "TS"),
                               ("polozky_navigace", "NV"), ("obecne_pismo", "GF"),
                               ("kontrast", "KN"), ("kontrast_velky", "KV"),
                               # HANS_PRIRUCKA_METRIKY_V2
                               ("media_dotazy", "MQ"), ("relativni_pismo", "RP"),
                               ("flex_grid", "FG"), ("fluidni_obrazky", "FI"),
                               ("lang", "LG"), ("semanticke_znacky", "SEM"),
                               ("omezeny_pohyb", "PM"), ("mezera_odstavcu", "MO"))},
            "chybi_alt": int(d.get("NA", 0)), "animace_box": int(d.get("BX", 0))}


def _snimek(url: str, w: int, path: str):
    # virtuální čas nechá doběhnout animace (fade-in dával vybledlý snímek)
    subprocess.run(_CHROM + ["--window-size=%d,900" % w, "--virtual-time-budget=4000",
                             "--screenshot=%s" % path, url],
                   capture_output=True, timeout=90)


_VL_Q = ("You are a strict web design reviewer. Screenshot of a website page "
         "(first screen, %s). List concrete VISUAL defects only (empty areas, "
         "missing or unreadable heading, broken or overlapping layout, poor "
         "contrast, unstyled elements, placeholder text). Do not complain that "
         "content continues below the fold. Answer JSON.")
_VL_SCHEMA = {"type": "object", "properties": {
    "defects": {"type": "array", "items": {"type": "string"}},
    "score": {"type": "integer"}}, "required": ["defects", "score"]}


def zkontroluj(config: dict, pages: dict, dest: Path, snimky_dir: Path,
               prahy: tuple = (4.5, 3.0)) -> dict:
    """Všechny stránky: JS chyby, přetečení na úzkém okně, rozbité obrázky,
    nadpis a text. Úvod + 1. podstránka: snímky → model na obrázky hledá vady."""
    tmp = tempfile.mkdtemp(prefix="webdilo_")
    try:
        shutil.copytree(dest, tmp + "/s", dirs_exist_ok=True)
        for name, h in pages.items():
            sj = _sonda_js(prahy)
            hh = h.replace("<head>", "<head>" + sj, 1) if "<head>" in h else sj + h
            Path(tmp, "s", "_sonda_" + name).write_text(hh, encoding="utf-8")
        soubory = {str(p.relative_to(tmp + "/s")) for p in Path(tmp, "s").rglob("*")
                   if p.is_file() and not p.name.startswith("_sonda_")}
        srv = _Server(tmp + "/s")
        try:
            vysl = {}
            for name in pages:
                u = "http://127.0.0.1:%d/_sonda_%s" % (srv.port, name)
                # headless Chromium neumí okno užší než 500 px — 400 dával
                # oříznutý snímek 500px stránky a „uříznuté nadpisy“
                vysl[name] = {"desktop": _sonda(u, 1280), "uzke": _sonda(u, 500),
                              "_html": pages[name], "_soubory": soubory}
            snimky_dir.mkdir(parents=True, exist_ok=True)
            vady = {}
            vzor = [n for n in pages if n != "index.html"][:1]
            vl = _cfg(config).get("vision_model", "qwen2.5vl:7b")
            for name in ["index.html"] + vzor:
                for w, popis in ((1280, "desktop"), (500, "mobile")):
                    p = snimky_dir / ("%s_%d.png" % (name.replace(".html", ""), w))
                    _snimek("http://127.0.0.1:%d/%s" % (srv.port, name), w, str(p))
                    if not p.exists():
                        continue
                    j = _json(_gen(config, vl, _VL_Q % popis, None, schema=_VL_SCHEMA,
                                   num_predict=400, num_ctx=8192, temperature=0,
                                   images=[base64.b64encode(p.read_bytes()).decode()],
                                   timeout=300)) or {}
                    vady.setdefault(name, []).extend(
                        "[%s] %s" % (popis, d) for d in (j.get("defects") or [])[:5])
                    vysl.setdefault(name, {})["vl_%s" % popis] = j.get("score")
        finally:
            srv.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"stranky": vysl, "vady_vl": vady}


_KONTROLA = {"max_velikosti_pisma": 7, "max_prazdnych_odkazu": 2}


def _odkazy(html: str, soubory: set) -> list:
    """Mrtvé odkazy (na soubor, který v díle není) a odkazy „nikam“ (#)."""
    out = []
    hrefs = re.findall(r"""<a\b[^>]*\bhref\s*=\s*["']([^"']*)["']""", html or "", re.I)
    mrtve = sorted({h for h in hrefs if h and not re.match(r"(?i)(https?:|mailto:|tel:|#|javascript:)", h)
                    and h.split("#")[0].split("?")[0] not in soubory})
    if mrtve:
        out.append("links to pages that do not exist: %s — link only to the pages "
                   "of this site" % ", ".join(mrtve[:5]))
    nikam = sum(1 for h in hrefs if h.strip() in ("#", "") or h.lower().startswith("javascript:"))
    if nikam > _KONTROLA["max_prazdnych_odkazu"]:
        out.append("%d links that lead nowhere (href=\"#\") — remove them" % nikam)
    return out


def _podstranky_v(html: str) -> set:
    """HANS_WEB_KARTY_V1 — na které podstránky (detail-*.html) úvod odkazuje."""
    return set(re.findall(r"""href\s*=\s*["'](detail-[^"'#?]+\.html)""", html or "", re.I))


def _doplnit_karty(idx: str, stranky: list) -> str:
    """HANS_WEB_KARTY_V1 — coder 21. 10. napsal jednu kartu a zbytek nahradil
    komentářem „Repeat the above article structure for each page“. Chybějící
    karty se naklonují z té první (odkaz, nadpis, perex, obrázek ze stránky)."""
    try:
        import copy
        from bs4 import BeautifulSoup, Comment
        if not idx or not stranky:
            return idx
        soup = BeautifulSoup(idx, "html.parser")
        det = re.compile(r"^detail-.*\.html$")
        odkazy = soup.find_all("a", href=det)
        if not odkazy:
            return idx
        mam = {a.get("href") for a in odkazy}
        chybi = [s for s in stranky if "detail-%s.html" % s["slug"] not in mam]
        if not chybi:
            return idx
        karta = odkazy[0]
        def _je_karta(x):
            return x.name in ("article", "li") or "card" in " ".join(x.get("class") or []).lower()
        while (not _je_karta(karta) and karta.parent is not None
               and karta.parent.name not in ("body", "main", "html", "section", "ul", "ol")
               and len(karta.parent.find_all("a", href=det)) <= 1):
            karta = karta.parent
        posledni = karta
        for o in odkazy[1:]:              # vkládej za poslední existující kartu
            k = o
            while (k.parent is not None and k.parent is not karta.parent
                   and k.parent.name not in ("body", "html")):
                k = k.parent
            if k.parent is karta.parent:
                posledni = k
        for s in chybi:
            k = copy.copy(karta)
            for a_ in k.find_all("a", href=det) + ([k] if k.name == "a" else []):
                a_["href"] = "detail-%s.html" % s["slug"]
            for im in k.find_all("img"):
                im["src"] = "GEN:%s" % s["img"]
                im["alt"] = s["sub"]
            h = k.find(re.compile(r"^h[1-6]$"))
            if h is not None:
                h.string = s["sub"]
            pp = k.find("p")
            if pp is not None:
                _pe = (s["text"].get("perex") or "").strip()
                pp.string = _pe if len(_pe) <= 160 else _pe[:160].rsplit(" ", 1)[0] + "…"
            posledni.insert_after(k)
            posledni = k
        for c in soup.find_all(string=lambda x: isinstance(x, Comment)
                               and re.search(r"repeat|for each", x, re.I)):
            c.extract()
        _log.info("webdilo: HANS_WEB_KARTY_V1 doplněno %d karet na úvod", len(chybi))
        return str(soup)
    except Exception as e:
        _log.warning("webdilo: doplnění karet selhalo: %s", e)
        return idx


def problemy(kontrola: dict) -> dict:
    """Deterministické problémy → {"index": [...], "sablona": [...]}."""
    out = {"index": [], "sablona": []}
    pretek = {}
    uzky = {}
    # HANS_WEB_KARTY_V1 (21. 10.) — opravné kolo 21. 10. přepsalo úvod na 3 kB
    # s odkazem na 1 ze 14 podstránek a to kolo vyhrálo jako „bez vad“
    _det = sorted(n for n in kontrola["stranky"] if n != "index.html")
    _ix = (kontrola["stranky"].get("index.html") or {}).get("_html")
    if _det and _ix is not None:
        _chybi = [n for n in _det if n not in _podstranky_v(_ix)]
        if _chybi:
            out["index"].append("the landing page links to only %d of %d subpages — add a "
                                "card with a link for EVERY page; missing: %s"
                                % (len(_det) - len(_chybi), len(_det), ", ".join(_chybi[:6])))
    for name, v in kontrola["stranky"].items():
        cil = "index" if name == "index.html" else "sablona"
        for rez, d in (("desktop", v.get("desktop") or {}), ("úzké okno", v.get("uzke") or {})):
            if d.get("chyba"):
                out[cil].append("page did not load (%s)" % rez)
                continue
            if d.get("js_chyby"):
                out[cil].append("JavaScript errors (%s): %s" % (rez, d.get("zpravy")))
            if d.get("sirka", 0) > d.get("okno", 0) + 2:
                # HANS_WEB_PRETECENI_V1 (21. 10.) — dřív 1 hláška za stránku (14×
                # totéž ze šablony) a bez příčiny → coder šablonu nezměnil
                pr = pretek.setdefault((cil, d["okno"]), {"n": 0, "max": 0, "vzor": "",
                                                          "flex": {}})
                pr["n"] += 1
                pr["max"] = max(pr["max"], d["sirka"])
                pr["vzor"] = pr["vzor"] or (d.get("pretece") or "?")
                for s_, k_ in (d.get("flex_radky") or []):
                    pr["flex"][s_] = max(pr["flex"].get(s_, 0), k_)
            if d.get("rozbite_obr"):
                out[cil].append("%d broken images" % d["rozbite_obr"])
            # HANS_WEB_UZKY_SLOUPEC_V1 (21. 10.) — článek ve třetině šířky (mřížka
            # o 3 sloupcích na <main> s jediným článkem); hlídala se jen horní mez
            _pw = d.get("sirka_odstavce") or -1
            if rez == "desktop" and 0 < _pw < 0.4 * min(d.get("okno") or 1280, 1100):
                uz = uzky.setdefault(cil, {"n": 0, "min": 9999, "max": 0, "okno": d.get("okno") or 0,
                                           "pricina": ""})
                uz["n"] += 1; uz["min"] = min(uz["min"], _pw); uz["max"] = max(uz["max"], _pw)
                uz["pricina"] = uz["pricina"] or d.get("mrizka") or ""
            if not d.get("h1"):
                out[cil].append("page has no <h1> heading")
            if d.get("kontrast"):
                out[cil].append("%d text elements with too low contrast (WCAG: 4.5:1, "
                                "large text 3:1), e.g. %s" % (d["kontrast"], d.get("kontrast_ukazky")))
            if d.get("velikosti_pisma", 0) > int(_KONTROLA.get("max_velikosti_pisma", 7)):
                out[cil].append("%d different font sizes — use a small consistent type "
                                "scale (at most %d)" % (d["velikosti_pisma"],
                                                         _KONTROLA.get("max_velikosti_pisma", 7)))
        for vada in _odkazy(v.get("_html", ""), v.get("_soubory") or set()):
            out[cil].append(vada)
        m = _VYPLN.search(re.sub(r"<[^>]+>", " ", v.get("_html", "")))
        if m:
            out[cil].append("template filler text on the page: \u201e%s\u201c" % m.group(0))
    ds = kontrola.get("navrh")                                   # HANS_WEB_NAVRH_V1
    if ds:
        pal = [_rgb(h) for h in ds["paleta"].values()] + [[255, 255, 255], [0, 0, 0]]
        for name, v in kontrola["stranky"].items():
            cil = "index" if name == "index.html" else "sablona"
            d = v.get("desktop") or {}
            mimo = []
            for c in d.get("barvy") or []:
                try:
                    rgb = [int(x) for x in c.split(",")]
                except Exception:
                    continue
                if min(sum((a_ - b_) ** 2 for a_, b_ in zip(rgb, q)) for q in pal) > 40 ** 2:
                    mimo.append("rgb(%s)" % c)
            if len(mimo) > 2:
                out[cil].append("%d colours outside Hans's palette (%s) — use only the "
                                "--barva-* variables" % (len(mimo), ", ".join(mimo[:5])))
            mimo_s = sorted({x for x in d.get("velikosti") or []
                             if min(abs(x - y) for y in ds["skala"]) > 1.5})
            if len(mimo_s) > 1:
                out[cil].append("font sizes outside Hans's type scale (%s px) — use only "
                                "the --fs-* variables" % ", ".join("%g" % x for x in mimo_s[:6]))
    for cil, uz in uzky.items():
        out[cil].append("article text squeezed into a narrow column on desktop on %d page(s) "
                        "(paragraphs %d–%dpx wide in a %dpx window)%s — let the article use "
                        "the full content width" % (uz["n"], uz["min"], uz["max"], uz["okno"],
                                                     ("; CAUSE: grid on %s" % uz["pricina"])
                                                     if uz["pricina"] else ""))
    for (cil, okno), pr in pretek.items():
        msg = ("horizontal overflow at %dpx width on %d page(s) (content up to %dpx); "
               "elements sticking out: %s" % (okno, pr["n"], pr["max"], pr["vzor"]))
        if pr["flex"]:
            msg += ("; CAUSE: %s — display:flex in a row without wrapping, children "
                    "squeezed side by side; stack them (flex-direction: column) or "
                    "flex-wrap: wrap" % ", ".join("%s (%d children)" % kv
                                                  for kv in pr["flex"].items()))
        out[cil].append(msg)
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


def _flex_css(kon: dict) -> dict:
    """HANS_WEB_PRETECENI_V1 — přetečení z flexu v řádku opraví Hans sám
    (coder 21. 10. šablonu se 7 oddíly vedle sebe dvakrát vrátil beze změny)."""
    out = {"index": "", "sablona": ""}
    for name, st in kon["stranky"].items():
        cil = "index" if name == "index.html" else "sablona"
        _d = st.get("desktop") or {}                     # HANS_WEB_UZKY_SLOUPEC_V1
        _pw = _d.get("sirka_odstavce") or -1
        if _d.get("mrizka") and 0 < _pw < 0.4 * min(_d.get("okno") or 1280, 1100):
            _g = _d["mrizka"].split(" (")[0]
            if re.fullmatch(r"[\w\s.-]+", _g) and _g not in out[cil]:
                out[cil] += "%s{grid-template-columns:1fr!important}\n" % _g
        for rez in ("desktop", "uzke"):
            for s_, _k in ((st.get(rez) or {}).get("flex_radky") or []):
                if re.fullmatch(r"[\w\s.-]+", s_ or "") and s_ not in out[cil]:
                    out[cil] += "%s{flex-wrap:wrap}%s>*{flex:1 1 100%%;min-width:0}\n" % (s_, s_)
    return out


def _hp_pravidla(db_path: str) -> list:
    try:
        from scripts import hans_prirucka as _hp
        return _hp.pravidla(db_path)
    except Exception:
        return []


def _cislo(v) -> str:
    return ("%g" % v) if isinstance(v, (int, float)) else str(v)


def _porusena_pravidla(db_path: str, kon: dict) -> dict:
    """HANS_PRIRUCKA_V1 — měřená pravidla příručky proti nejhoršímu případu
    na stránkách (úvod zvlášť, podstránky = šablona)."""
    try:
        from scripts import hans_prirucka as _hp
    except Exception:
        return {"index": [], "sablona": []}
    out = {}
    for cil in ("index", "sablona"):
        vals = {}
        for name, v in kon["stranky"].items():
            if (name == "index.html") != (cil == "index"):
                continue
            d = (v.get("desktop") or {})
            for k, x in (d.get("m") or {}).items():
                if x is None:
                    continue
                horsi_min = k in ("kontrast", "kontrast_velky", "radkovani",
                                  "velikost_cilu", "velikost_pisma_textu", "obecne_pismo",
                                  "mezera_odstavcu", "semanticke_znacky") or k in _hp.ANO_NE
                vals[k] = x if k not in vals else (min(vals[k], x) if horsi_min else max(vals[k], x))
            if d.get("velikosti_pisma"):
                vals["velikosti_pisma"] = max(vals.get("velikosti_pisma", 0), d["velikosti_pisma"])
            # HANS_PRIRUCKA_METRIKY_V2 — počty převedené na ano/ne (nejhorší stránka)
            for k, x in (("alt_obrazku", 0 if d.get("chybi_alt") else 1),
                         ("bez_animace_box_modelu", 0 if d.get("animace_box") else 1)):
                if "m" in d:
                    vals[k] = min(vals.get(k, 1), x)
        # kontrastní pravidlo pod 4,5 je pravidlo pro VELKÝ text
        porus = []
        for p in _hp.zkontroluj(db_path, {k: vals.get(k) for k in _hp.METRIKY if k != "kontrast"}):
            porus.append(p)
        for p in _hp.pravidla(db_path):
            if p["metrika"] != "kontrast" or p["prah"] is None:
                continue
            x = vals.get("kontrast_velky" if p["prah"] < 4.5 else "kontrast")
            if x is not None and x < p["prah"]:
                porus.append({"pravidlo": p["pravidlo"], "citace": p["citace"],
                              "zdroj": p["zdroj"], "metrika": "kontrast",
                              "namereno": round(x, 2), "prah": p["prah"]})
        vid, uniq = set(), []
        for p in porus:            # stejné (metrika, práh) z dvou citací = jedno
            k = (p["metrika"], p["prah"])
            if k not in vid:
                vid.add(k); uniq.append(p)
        out[cil] = uniq
    return out


_POPIS_METRIKY = {"kontrast": "kontrast", "delka_radku": "délka řádku",
                  "radkovani": "řádkování", "velikost_cilu": "velikost ovládacích prvků",
                  "velikost_pisma_textu": "velikost písma", "obecne_pismo": "záložní písmo",
                  "polozky_navigace": "počet položek navigace",
                  "velikosti_pisma": "počet velikostí písma",
                  "media_dotazy": "breakpointy", "relativni_pismo": "relativní velikost písma",
                  "flex_grid": "flexbox/grid", "fluidni_obrazky": "pružné obrázky",
                  "alt_obrazku": "popisky obrázků", "lang": "jazyk stránky",
                  "semanticke_znacky": "sémantické značky", "omezeny_pohyb": "omezený pohyb",
                  "bez_animace_box_modelu": "animace bez přepočtu rozvržení",
                  "mezera_odstavcu": "mezera mezi odstavci",
                  "pretekani": "přetékání obsahu do strany"}


def _prahy_kontrastu(db_path: str) -> tuple:
    kn, kv = 4.5, 3.0
    for p in _hp_pravidla(db_path):
        if p["metrika"] == "kontrast" and p["prah"] is not None:
            if p["prah"] >= 4.5:
                kn = max(kn, p["prah"])
            else:
                kv = max(kv, p["prah"])
    return kn, kv


def _vymahani_css(db_path: str, kon: dict, kritika: dict) -> dict:
    """CSS, kterým Hans uplatní porušená měřitelná pravidla."""
    out = {}
    for cil in ("index", "sablona"):
        radky = []
        for v in kritika.get(cil) or []:
            m, prah = v["metrika"], v["prah"]
            if m == "kontrast":
                fx = {}
                for name, st in kon["stranky"].items():
                    if (name == "index.html") == (cil == "index"):
                        for sl, barva in ((st.get("desktop") or {}).get("fixy") or []):
                            if re.fullmatch(r"[\w\s.-]+", sl or "") and sl not in fx:
                                fx[sl] = barva
                radky += ["%s{color:%s!important}" % (sl, b) for sl, b in list(fx.items())[:30]]
            elif m == "delka_radku" and prah:
                # ch = šířka „0“; česká písmena jsou užší → 72ch dalo 83 znaků
                radky.append("p,li{max-width:%dch}" % max(30, int(prah * 0.75)))
            elif m == "radkovani" and prah:
                radky.append("p,li{line-height:%g!important}" % prah)
            elif m == "velikost_pisma_textu" and prah:
                radky.append("p,li{font-size:max(%gpx,1rem)!important}" % prah)
            elif m == "omezeny_pohyb":      # HANS_PRIRUCKA_METRIKY_V2
                radky.append("@media (prefers-reduced-motion:reduce){*,*::before,*::after"
                             "{animation-duration:.01ms!important;animation-iteration-count:1!important;"
                             "transition-duration:.01ms!important;scroll-behavior:auto!important}}")
            elif m == "mezera_odstavcu" and prah:
                radky.append("p{margin-bottom:%gem!important}" % round(prah * 1.6, 2))
            elif m == "velikost_cilu" and prah:
                radky.append("nav a,header a,button,[role=button]{min-height:%dpx;"
                             "display:inline-flex;align-items:center}" % prah)
        out[cil] = "\n".join(dict.fromkeys(radky))
    return out


def _vloz_css(html: str, css: str) -> str:
    if not css or not html:
        return html
    html = re.sub(r'<style id="hans-pravidla">[\s\S]*?</style>\s*', "", html)
    blok = '<style id="hans-pravidla">/* Hansova pravidla */\n%s\n</style>\n' % css
    return html.replace("</head>", blok + "</head>", 1) if "</head>" in html else blok + html


def _kritika_cz(kritika: dict, n_pravidel: int) -> str:
    """Hansova kritika z čísel — ne „posuď, jestli se povedlo“ (ozvěna)."""
    vety = []
    for cil, vs in kritika.items():
        kde = "na úvodní stránce" if cil == "index" else "na podstránkách"
        for v in vs:
            if v["prah"] is None:       # HANS_PRIRUCKA_METRIKY_V2 — ano/ne pravidlo
                vety.append("Porušil jsem své pravidlo „%s“ %s (zdroj %s)."
                            % (v["pravidlo"], kde, v.get("zdroj") or "?"))
                continue
            vety.append("Porušil jsem své pravidlo „%s“ %s (naměřeno %s, hranice %s; zdroj %s)."
                        % (v["pravidlo"], kde, _cislo(v["namereno"]), _cislo(v["prah"]),
                           v.get("zdroj") or "?"))
    if not vety:
        return ("Dílo splňuje všechna měřená pravidla mé příručky (%d pravidel celkem)."
                % n_pravidel) if n_pravidel else ""
    return " ".join(vety)


# ── HLAVNÍ CESTA ──────────────────────────────────────────────────────────

def _lekce(db_path: str) -> str:
    """Poučení z minulých děl (co kontrola našla) → do volby dalšího."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        rows = con.execute("SELECT data FROM diary WHERE event_type='work_web_review' "
                           "ORDER BY ts DESC LIMIT 3").fetchall()
        con.close()
    except Exception:
        return ""
    out = []
    for (d,) in rows:
        try:
            j = json.loads(d or "{}")
            out.append("- %s: used %s; remaining defects: %s"
                       % (j.get("tema"), ", ".join(j.get("knihovny") or []) or "no libraries",
                          "; ".join((j.get("zbyle_vady") or [])[:4]) or "none"))
        except Exception:
            pass
    return "\n".join(out)


def make_site(config: dict, db_path: str, topic: str, notes: list,
              dest_dir: Path, deepen_round: int = 0) -> Optional[dict]:
    """Postaví web do `dest_dir`. None = selhalo → volající použije starou cestu."""
    from scripts import hans_maker as hm
    t0 = time.time()
    stranky = []
    # hotové texty se drží u díla — opakovaný běh (oprava, výpadek) je nepíše znovu
    cache_p = dest_dir / "_texty.json"
    try:
        cache = json.loads(cache_p.read_text(encoding="utf-8"))
    except Exception:
        cache = {}
    for n in notes:
        sub = n["sub"]
        mat, url, main = zdroj(config, db_path, topic, sub)
        klic = "%s|%s" % (sub, url)
        t = cache.get(klic)
        if t is None:
            t = text_stranky(config, topic, sub, mat) if mat else None
            if t is not None:
                cache[klic] = t
                cache_p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        if t is None:
            t = _poznamka_jako_text(n.get("text", ""))
        t["stat"].update({"zdroj": url, "clanek": main})
        stranky.append({"sub": sub, "slug": hm._slug(sub), "text": t})
        _log.info("webdilo: text '%s' %d zn (%s)", sub, t["stat"]["znaku"],
                  "záloha poznámka" if t["stat"].get("zaloha") else "ze zdroje %s" % main)
    if not stranky:
        return None
    # OBRÁZKY K TEXTU — popisky z nového textu, ne ze staré poznámky
    pop_p = dest_dir / "_popisy.json"
    try:
        img_prompts = json.loads(pop_p.read_text(encoding="utf-8"))
    except Exception:
        img_prompts = {}
    if not all(s["sub"] in img_prompts for s in stranky):
        img_prompts = hm._image_prompts_for(
        config, [s["sub"] for s in stranky],
        [" ".join([s["text"].get("perex") or ""] + [p for o in s["text"]["oddily"]
                                                     for p in o["odstavce"]])
         for s in stranky], db_path=db_path)
        pop_p.write_text(json.dumps(img_prompts, ensure_ascii=False), encoding="utf-8")
    for s in stranky:
        s["img"] = (img_prompts.get(s["sub"]) or "historical illustration, %s"
                    % s["sub"]).replace('"', " ").replace("'", " ")
    inv = inventar(topic, stranky)
    plan = volba(config, inv, _lekce(db_path))
    if not plan:
        _log.warning("webdilo: volba nástrojů selhala"); return None
    vyrazeno = zkontroluj_volbu(plan, inv)
    # HANS_WEB_NAVRH_V1 — návrhový systém z vlastních poznámek (mezipaměť u díla)
    navrh_p = dest_dir / "_navrh.json"
    try:
        ds = json.loads(navrh_p.read_text(encoding="utf-8"))
    except Exception:
        ds = None
    if not ds and _cfg(config).get("navrh_system", True):
        ds = navrh_system(config, topic, notes, db_path,
                          "; ".join("%s: %s" % (s["sub"], (s["text"].get("perex") or "")[:160])
                                    for s in stranky[:8]))
        if ds:
            navrh_p.write_text(json.dumps(ds, ensure_ascii=False, indent=1), encoding="utf-8")
    if ds:
        plan["typography_colors"] = navrh_pro_kodera(ds)
        _log.info("webdilo: návrhový systém — základ %s, paleta %s, písmo %s/%s, škála %s, "
                  "opora %s", ds["zakladni_barva"], list(ds["paleta"].values()),
                  ds["pismo_nadpisy"], ds["pismo_text"], ds["skala"],
                  {k: bool(v.get("citace")) for k, v in ds["opory"].items()})
    libs, lib_chyby = stahni_knihovny(plan.get("libraries") or [], dest_dir)
    _log.info("webdilo: volba — %s | knihovny %s | vyřazeno %s | chyby %s",
              (plan.get("concept") or "")[:120], [l["npm"] for l in libs],
              vyrazeno, lib_chyby)
    try:
        from scripts import hans_prirucka as _hp
        prirucka = _hp.pro_stavbu(db_path)
    except Exception:
        prirucka = ""
    idx, sab = postav(config, plan, libs, topic, stranky, prirucka=prirucka)
    if not idx or not sab or _chybi_placeholdery(sab):
        _log.warning("webdilo: stavba nevrátila použitelné HTML (index %d, šablona %d, "
                     "OBSAH %s)", len(idx or ""), len(sab or ""), "{{OBSAH}}" in (sab or ""))
        return None
    # obrázky JEDNOU (FLUX ~1–2 min/kus) — opravná kola je jen dosazují
    popisy = [stranky[0]["img"]] + [s["img"] for s in stranky]
    popisy = list(dict.fromkeys(popisy))
    obr_p = dest_dir / "_obrazy.json"
    try:
        obr = {d: p for d, p in json.loads(obr_p.read_text(encoding="utf-8")).items()
               if (dest_dir / p).exists()}
    except Exception:
        obr = {}
    chybi = [d for d in popisy if d not in obr]
    vz, rendered, total = hm._render_site_images(
        config, {"p%d" % i: 'src="GEN:%s"' % d for i, d in enumerate(chybi)},
        dest_dir, len(chybi)) if chybi else ({}, 0, 0)
    for i, d in enumerate(chybi):
        m = re.search(r'src="([^"]*)"', vz.get("p%d" % i, ""))
        if m and not m.group(1).startswith("data:"):
            obr[d] = m.group(1)
    rendered, total = sum(1 for d in popisy if d in obr), len(popisy)
    obr_p.write_text(json.dumps(obr, ensure_ascii=False), encoding="utf-8")

    def dosad(pages):
        out = {}
        for name, h in pages.items():
            for d, tgt in obr.items():
                h = h.replace("GEN:" + d, tgt)
            # HANS_WEB_OBRAZKY_V1 — nevyrenderovaný obrázek pryč celý (dřív
            # src="" = rozbitý obrázek a k tomu „JS chyba“ v sondě)
            h = re.sub(r"<img\b[^>]*\bsrc\s*=\s*[\"']GEN:[^\"']*[\"'][^>]*>", "", h)
            h = re.sub(r"<figure\b[^>]*>\s*(<figcaption\b[^>]*>[\s\S]*?</figcaption>\s*)?</figure>",
                       "", h)
            h = re.sub(r"url\(\s*[\"']?GEN:[^\"')]+[\"']?\s*\)", "none", h)
            h = re.sub(r"GEN:[^\"')]+", "", h)
            # základ, který má každý profesionální web (a coder ho vynechává):
            # bez box-sizing přetekla patička `width:100%` + padding o 32 px
            if ds and 'id="hans-navrh"' not in h:          # HANS_WEB_NAVRH_V1
                _blok = '<style id="hans-navrh">%s</style>' % navrh_css(ds)
                h = (h.replace("<head>", "<head>\n" + _blok, 1) if "<head>" in h
                     else _blok + h)
            if ds and 'id="hans-navrh-promenne"' not in h and "</head>" in h:
                # coder 21. 10. :root předefinoval (Arial/Georgia) — Hansovy
                # proměnné až za jeho CSS a s !important
                _pv = re.search(r":root\{([^}]*)\}", navrh_css(ds)).group(1)
                h = h.replace("</head>", '<style id="hans-navrh-promenne">:root{%s}</style>\n</head>'
                              % ";".join(x + "!important" for x in _pv.split(";") if x), 1)
            if _ZAKLAD_CSS not in h:
                h = (h.replace("<head>", "<head>\n" + _ZAKLAD_CSS, 1)
                     if "<head>" in h else _ZAKLAD_CSS + h)
            out[name] = h
        return out

    historie = []
    kolo = 0
    vymahano = []
    while True:
        idx = _doplnit_karty(idx, stranky)               # HANS_WEB_KARTY_V1
        pages = dosad({"index.html": idx, **vypln(sab, topic, stranky)})
        for name, h in pages.items():
            (dest_dir / name).write_text(h, encoding="utf-8")
        kon = zkontroluj(config, pages, dest_dir, dest_dir / "_kontrola" / ("kolo%d" % kolo),
                         prahy=_prahy_kontrastu(db_path))
        kon["navrh"] = ds                                   # HANS_WEB_NAVRH_V1
        pr = problemy(kon)
        kritika = _porusena_pravidla(db_path, kon)      # HANS_PRIRUCKA_V1
        for cil, vs in kritika.items():
            pr[cil] += [("breaks Hans's own handbook rule „%s“ (measured %s, limit %s)"
                         % (v["pravidlo"], _cislo(v["namereno"]), _cislo(v["prah"])))
                        if v["prah"] is not None else
                        ("breaks Hans's own handbook rule „%s“ (not met on the page)" % v["pravidlo"])
                        for v in vs]
        vl = kon.get("vady_vl") or {}
        # opravuje se jen podle DETERMINISTICKÝCH vad; výtky modelu na obrázky
        # byly v testech 26. 9. z velké části nepravdivé a opravy podle nich
        # jen točily kola dokola — zůstávají ve zprávě (dilo.json)
        vady = {"index": list(pr["index"]), "sablona": list(pr["sablona"])}
        historie.append({"kolo": kolo, "problemy": pr, "vady_vl": vl, "kritika": kritika,
                         "vl_skore": {k: {kk: vv for kk, vv in v.items() if kk.startswith("vl_")}
                                      for k, v in kon["stranky"].items()},
                         "idx": idx, "sab": sab})
        _log.info("webdilo: kontrola kolo %d — deterministické %s | vady VL %d",
                  kolo, {k: len(v) for k, v in pr.items()},
                  sum(len(v) for v in vl.values()))
        # HANS_PRIRUCKA_VYMAHANI_V1 — měřitelná pravidla vymáhá Hans SÁM:
        # coder 26. 9. vadu neopravil ani přepisem, ani doplňkem CSS
        # (kontrast 1,68, řádek 96 zn zůstaly). Sonda zná prvek i barvy.
        _fx = _flex_css(kon)                              # HANS_WEB_PRETECENI_V1
        if not vymahano and (kritika.get("index") or kritika.get("sablona")
                             or _fx["index"] or _fx["sablona"]):
            css = _vymahani_css(db_path, kon, kritika)
            css = {k: (css.get(k, "") + "\n" + _fx[k]).strip() for k in ("index", "sablona")}
            if css["index"] or css["sablona"]:
                idx, sab = _vloz_css(idx, css["index"]), _vloz_css(sab, css["sablona"])
                vymahano = sorted({v["metrika"] for vs in kritika.values() for v in vs}
                                  | ({"pretekani"} if (_fx["index"] or _fx["sablona"]) else set()))
                _log.info("webdilo: vymáhám svá pravidla %s", vymahano)
                kolo += 1
                continue
        if kolo >= int(_cfg(config).get("opravna_kola", 2)) + (1 if vymahano else 0) \
                or not (vady["index"] or vady["sablona"]):
            break
        kolo += 1
        n_idx, n_sab = postav(config, plan, libs, topic, stranky,
                              oprava={**vady, "stary": (idx, sab), "css_nejdriv": kolo == 1},
                              prirucka=prirucka)
        _log.info("webdilo: oprava kolo %d — index %s, šablona %s", kolo,
                  "beze změny" if n_idx == idx else "nový %d zn" % len(n_idx or ""),
                  "beze změny" if n_sab == sab else (
                      "nová %d zn%s" % (len(n_sab or ""), "" if not _chybi_placeholdery(n_sab)
                                        else " BEZ %s → zahozena" % _chybi_placeholdery(n_sab))))
        if not n_sab or _chybi_placeholdery(n_sab):
            n_sab = sab
        # HANS_WEB_KARTY_V1 — oprava úvodu nesmí ubrat odkazy na podstránky
        if n_idx and len(_podstranky_v(n_idx)) < len(_podstranky_v(idx)):
            _log.info("webdilo: oprava úvodu ubrala odkazy (%d → %d) — zahozena",
                      len(_podstranky_v(idx)), len(_podstranky_v(n_idx)))
            n_idx = idx
        idx, sab = (n_idx or idx), n_sab
    # nejlepší kolo = nejméně deterministických problémů, pak vad VL
    def cena(h):
        return sum(len(v) for v in h["problemy"].values())
    best = min(historie, key=cena)
    if best is not historie[-1]:
        pages = dosad({"index.html": best["idx"], **vypln(best["sab"], topic, stranky)})
        for name, h in pages.items():
            (dest_dir / name).write_text(h, encoding="utf-8")
    # úklid: HTML stránek z horších kol nezůstávají (jména se nemění), snímky ano
    zbyle = sorted(set(sum(best["problemy"].values(), [])
                       + sum(best["vady_vl"].values(), [])))
    zbyva = {v["metrika"] for vs in (best.get("kritika") or {}).values() for v in vs}
    opraveno = [m for m in vymahano if m not in zbyva and not (   # HANS_WEB_PRETECENI_V1
        m == "pretekani" and any("horizontal overflow" in x
                                 for x in sum(best["problemy"].values(), [])))]
    zaznam = {
        "tema": topic, "kolo_prohloubeni": deepen_round, "plan": plan,
        "vyrazeno_z_volby": vyrazeno, "knihovny": [l["npm"] + "@" + l["version"] for l in libs],
        "chyby_knihoven": lib_chyby,
        "texty": [{"sub": s["sub"], **s["text"]["stat"], "udalosti": len(s["text"]["udalosti"]),
                   "oddilu": len(s["text"]["oddily"])} for s in stranky],
        "kola": [{k: v for k, v in h.items() if k not in ("idx", "sab")} for h in historie],
        "vybrane_kolo": best["kolo"], "zbyle_vady": zbyle,
        "prirucka_pravidel": len(_hp_pravidla(db_path)),
        "navrh": ds,                                            # HANS_WEB_NAVRH_V1
        "vymahano": vymahano,
        "kritika": ((("Při kontrole jsem porušil svá pravidla (%s) a opravil jsem to podle "
                      "nich. " % ", ".join(_POPIS_METRIKY.get(m, m) for m in opraveno))
                     if opraveno else "")
                    + _kritika_cz(best.get("kritika") or {}, len(_hp_pravidla(db_path)))
                    + ((" " + navrh_cz(ds)) if ds else "")).strip(),
        "obrazky": [rendered, total], "sekund": round(time.time() - t0)}
    (dest_dir / "dilo.json").write_text(json.dumps(zaznam, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
    try:
        con = sqlite3.connect(db_path, timeout=10)
        con.execute("INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
                    (time.time(), "work_web_review", "Web: %s" % topic,
                     ((plan.get("zduvodneni_cz") or "") + " " + zaznam["kritika"]).strip()[:1200],
                     json.dumps({k: zaznam[k] for k in ("tema", "knihovny", "zbyle_vady",
                                                        "vybrane_kolo", "vyrazeno_z_volby",
                                                        "kritika")},
                                ensure_ascii=False)))
        con.commit(); con.close()
    except Exception as e:
        _log.debug("webdilo deník: %s", e)
    _log.info("webdilo '%s': %d stránek, knihovny %s, kolo %d, zbylé vady %d, %d s",
              topic, len(pages), zaznam["knihovny"], best["kolo"], len(zbyle), zaznam["sekund"])
    return {"status": "made", "path": str((dest_dir / "index.html").resolve().relative_to(_ROOT)),
            "pages": len(pages), "bytes": sum(len(h) for h in pages.values()),
            "model": "webdilo", "zbyle_vady": len(zbyle)}
