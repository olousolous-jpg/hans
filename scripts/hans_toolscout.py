"""HANS_TOOLSCOUT_V1 — Hans si najde nástroj (LLM) pro finální dílo.

Když Hans dostuduje doménu (study_program → completed), oblouk studia dnes končí
u textu. Pro reálné dílo (design → HTML/CSS, kód; 3D → mesh; hudba → MIDI) často
potřebuje SPECIALIZOVANÝ model. Tento modul mu dá schopnost si ho GROUNDOVANĚ
najít a NAVRHNOUT uživateli (human-in-the-loop jako Severka) — nikdy netahá sám.

KLÍČOVÝ PRINCIP (proti konfabulaci, [[anticonfabulation-guiding-principle]]):
meta-research je GROUNDOVANÝ ve skutečných datech z Ollama library (jméno,
velikost, popularita, capabilities — parsované z ollama.com), NE v LLM domněnce.
LLM smí jen (a) zvolit hledaný klíč z tématu, (b) seřadit REÁLNÉ kandidáty a
odůvodnit JEN skutečnými metadaty. Žádné vymyšlené benchmarky.

Tok: study completed → scout (search library) → filtr VRAM-fit → LLM návrh 1-2
modelů s odůvodněním → tool_proposals (pending) → uživatel schválí `/nastroj
schválit N` → Hans spustí `ollama pull` na PC → ověří `ollama list`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from typing import Optional

from scripts.logger import get_logger

_log = get_logger("hans_toolscout")

_SEARCH_URL = "https://ollama.com/search?q=%s"
# HANS_TOOLSCOUT_RANK_V2 — počet stažení: `</svg> <span >1.1M</span>` hned za
# ikonou stahování. ⚠️ Scraping cizího HTML je křehký ([[hans-toolscout]]) —
# když se čísla začnou tvářit jako velikosti modelu („3B" pulls), je rozbitá
# právě tahle kotva.
_PULLS_RE = re.compile(r"</svg>\s*<span[^>]*>\s*([\d.]+[KMB])\s*</span>")
_MODEL_URL = "https://ollama.com/library/%s"


def _cfg(config: dict) -> dict:
    return (config or {}).get("toolscout", {}) or {}


def enabled(config: dict) -> bool:
    return bool(_cfg(config).get("enabled", True))


# ── grounded search Ollama library (BEZ LLM) ─────────────────────────────────
def _fetch(url: str, timeout: int = 15) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        return urllib.request.urlopen(req, timeout=timeout).read().decode(
            "utf-8", "replace")
    except Exception as e:
        _log.debug("toolscout fetch %s: %s", url, e)
        return None


def _param_to_b(s: str) -> float:
    """'7b'→7.0, '1.5b'→1.5, '32b'→32, '480b'→480. 'm' → zlomek."""
    s = s.strip().lower()
    m = re.match(r"([0-9.]+)\s*([bm])", s)
    if not m:
        return 0.0
    v = float(m.group(1))
    return v / 1000.0 if m.group(2) == "m" else v


def _est_vram_gb(param_b: float) -> float:
    """Hrubý odhad VRAM pro Q4_K_M kvantizaci (~0.65 GB/B + režie). ODHAD,
    ne přesná hodnota — slouží jen ke klasifikaci fit, uživateli se přizná."""
    if param_b <= 0:
        return 0.0
    return round(param_b * 0.65 + 0.8, 1)


def search_library(query: str, limit: int = 12,
                   timeout: int = 15) -> list:
    """GROUNDED: parse ollama.com/search. Vrací list dictů {name, sizes[str],
    sizes_b[float], pulls, capabilities[], url}. Deferral-safe → [] při výpadku.

    ⚠️ Prázdný seznam je DVOJZNAČNÝ (výpadek sítě × knihovna nic nemá) —
    kdo to potřebuje rozlišit, ať volá `search_library_ex`."""
    return search_library_ex(query, limit, timeout)[0]


def search_library_ex(query: str, limit: int = 12,
                      timeout: int = 15) -> tuple:
    """HANS_TOOLSCOUT_NO_MATCH_V1 — jako `search_library`, ale vrací
    `(kandidáti, fetch_ok)`.

    Proč to existuje: `[]` se dosud vracelo I při výpadku sítě, I když
    knihovna na dotaz prostě nic nemá. Volající ta dvě selhání nerozlišil,
    takže beznadějné téma zkoušel donekonečna — doloženo 25.8.: téma
    „historie a památky" dávalo klíč `history`, ollama.com takový model
    nemá, a Toolscout se na něm zasekl ~20× za noc a BLOKOVAL frontu
    dalších dostudovaných témat (`break` po prvním tématu bez návrhu).
    `fetch_ok=True` + prázdno = platná odpověď „nic vhodného není".
    """
    html = _fetch(_SEARCH_URL % urllib.parse.quote(query), timeout)
    if not html:
        return [], False
    out = []
    seen = set()
    # HANS_TOOLSCOUT_PARSE_V2 (25.7.) — ollama.com zrušil x-test-* atributy;
    # karty se teď dělí podle odkazu /library/<name>. Velikosti = spany „7b/0.5b",
    # capabilities = indigo spany (tools/vision/thinking…), pulls = číslo s K/M/B.
    for part in re.split(r'href="/library/', html)[1:]:
        nm = re.match(r"([a-z0-9._-]+)\"", part)
        if not nm:
            continue
        name = nm.group(1)
        if name in seen:
            continue
        seen.add(name)
        card = part.split('href="/library/')[0]   # jen tato karta
        sizes = re.findall(r">\s*(\d+(?:\.\d+)?b)\s*<", card, re.IGNORECASE)
        caps = re.findall(r"text-indigo-600[^>]*>\s*([A-Za-z]+)\s*<", card)
        # HANS_TOOLSCOUT_RANK_V2 — pulls MUSÍ být kotvené na span za ikonou
        # stahování. „1. číslo s K/M/B" bralo číslo z POPISU modelu:
        # „Stable Code 3B is a coding model…" → pulls „3B" místo 1.1M
        # (2700× nafouknuto). Změřeno 25.8.: špatně bylo 17 z ~50 karet
        # (nemotron „70B", llama3.2-vision „11B", dolphin3 „8B"…), a protože
        # se podle pulls ŘADÍ, dostával přednost náhodný model.
        pm = _PULLS_RE.search(card)
        out.append({
            "name": name,
            "sizes": sizes,
            "sizes_b": [_param_to_b(s) for s in sizes],
            "pulls": pm.group(1) if pm else "?",
            "capabilities": [c.strip() for c in caps],
            "url": _MODEL_URL % name,
        })
        if len(out) >= limit:
            break
    return out, True


# ── VRAM fit klasifikace (proti reálné GPU + rezidentní chat) ─────────────────
def _fit_class(param_b: float, config: dict) -> dict:
    """Vrací {size_b, est_gb, fit, note}. fit ∈ coexist/on_demand/too_big.
    coexist = vejde se VEDLE rezidentního chatu; on_demand = jen sám (chat se
    dočasně odloží jako herní mód); too_big = ani sám se nevejde."""
    c = _cfg(config)
    gpu = float(c.get("gpu_total_gb", 16.0))
    resident = float(c.get("chat_resident_gb", 10.8))
    safety = float(c.get("safety_gb", 1.0))
    est = _est_vram_gb(param_b)
    if est <= 0:
        return {"size_b": param_b, "est_gb": est, "fit": "unknown", "note": ""}
    if est + resident + safety <= gpu:
        return {"size_b": param_b, "est_gb": est, "fit": "coexist",
                "note": "vejde se vedle mého chatu"}
    if est + safety <= gpu:
        return {"size_b": param_b, "est_gb": est, "fit": "on_demand",
                "note": "poběží jen samostatně (chat dočasně odložím)"}
    return {"size_b": param_b, "est_gb": est, "fit": "too_big",
            "note": "ani samostatně se nevejde do mé GPU"}


def _best_fitting_size(cand: dict, config: dict) -> Optional[dict]:
    """Z velikostí modelu vyber NEJVĚTŠÍ, která se ještě vejde (coexist>on_demand).
    Vrací {size_tag, **fit} nebo None když se nevejde žádná."""
    best = None
    for tag, b in zip(cand["sizes"], cand["sizes_b"]):
        fc = _fit_class(b, config)
        if fc["fit"] in ("coexist", "on_demand"):
            # preferuj větší model, ale coexist má přednost před on_demand
            rank = (0 if fc["fit"] == "coexist" else 1, b)
            if best is None or rank > best[0]:
                best = (rank, {"size_tag": tag, **fc})
    return best[1] if best else None


def scout_tools_for_topic(config: dict, topic: str,
                          keyword: str = "") -> dict:
    """Najdi vhodné nástroje pro doménu. keyword = hledaný klíč (když prázdný,
    použije téma). Vrací {topic, keyword, candidates:[{name,size_tag,est_gb,fit,
    pulls,capabilities,url}]}. Kandidáti = jen ti, co se vejdou. GROUNDED."""
    kw = (keyword or topic).strip()
    raw, _ok = search_library_ex(
        kw, limit=int(_cfg(config).get("search_limit", 12)))
    cands = []
    for r in raw:
        fit = _best_fitting_size(r, config)
        if not fit:
            continue
        cands.append({
            "name": r["name"], "size_tag": fit["size_tag"],
            "est_gb": fit["est_gb"], "fit": fit["fit"], "note": fit["note"],
            "pulls": r["pulls"], "capabilities": r["capabilities"],
            "url": r["url"],
        })
    return {"topic": topic, "keyword": kw, "candidates": cands,
            "fetch_ok": _ok}


# ── LLM: klíč z tématu + groundované odůvodnění (deferral-safe) ───────────────
# deterministická mapa domén → typ nástroje (fallback když LLM dole; není to
# fakt, jen hledací dotaz — výsledky se pak groundují ze skutečné knihovny)
_KEYWORD_HINTS = {
    "coder": ("design", "web", "webov", "html", "css", "kód", "kod", "program",
              "aplikac", "rozhran", "software", "backend", "frontend"),
    "code": ("3d", "mesh", "blender", "model", "cad", "sochař"),
    "math": ("matematik", "algebra", "geometri", "statistik", "výpočet"),
    "vision": ("obraz", "vidění", "foto", "vizuál", "grafik"),
    "audio": ("hudb", "zvuk", "kompozic", "mid", "audio", "nota"),
}


def _keyword_hint(topic: str) -> str:
    low = (topic or "").lower()
    for kw, terms in _KEYWORD_HINTS.items():
        if any(t in low for t in terms):
            return kw
    return topic


def _derive_keyword(config: dict, topic: str) -> str:
    """LLM zvolí 1 hledaný klíč (typ nástroje) z tématu. Fallback = deterministická
    mapa domén, pak téma. NENÍ to fakt, jen hledací dotaz → výsledky jsou grounded."""
    try:
        from scripts.ollama_client import ollama_generate
        model = ((config.get("dialog", {}) or {}).get("model")
                 or "hans-czech:latest")
        sys = ("Jsi asistent pro výběr AI nástroje. Uživatel dostudoval doménu. "
               "Urči JEDNO anglické klíčové slovo pro hledání specializovaného "
               "LLM v knihovně Ollama, který by uměl VYTVOŘIT finální dílo v té "
               "doméně. Např. design/web → 'coder'; 3D → 'code'; matematika → "
               "'math'; vidění → 'vision'. Vrať POUZE to jedno slovo, anglicky.")
        raw = ollama_generate(model, "Doména: %s\n\nKLÍČ:" % topic, system=sys,
                              config=config, timeout=25, keep_alive=-1,
                              options={"temperature": 0.1, "num_predict": 8})
        if raw:
            w = re.findall(r"[a-zA-Z]+", raw)
            if w:
                return w[0].lower()
    except Exception as e:
        _log.debug("toolscout keyword: %s", e)
    return _keyword_hint(topic)


def _rationale(config: dict, topic: str, cand: dict) -> str:
    """Groundované odůvodnění návrhu — LLM smí použít JEN skutečná metadata.
    Fallback = deterministická věta. ŽÁDNÉ vymyšlené benchmarky."""
    facts = ("model=%s, velikost=%s (~%s GB), popularita=%s stažení, "
             "schopnosti=%s, umístění=%s" % (
                 cand["name"], cand["size_tag"], cand["est_gb"], cand["pulls"],
                 ", ".join(cand["capabilities"]) or "neuvedeno", cand["note"]))
    try:
        from scripts.ollama_client import ollama_generate
        model = ((config.get("dialog", {}) or {}).get("model")
                 or "hans-czech:latest")
        sys = ("Jsi Hans a navrhuješ svému pánovi nástroj pro finální dílo. "
               "Napiš 1-2 věty, PROČ tento model, ČESKY, v 1. osobě. Použij "
               "VÝHRADNĚ uvedená fakta (velikost, popularita, schopnosti, jak se "
               "vejde do paměti). NEVYMÝŠLEJ žádné benchmarky ani čísla, která "
               "nejsou v datech. Přiznej, že popularita/velikost není totéž co "
               "kvalita, pokud to je namístě.")
        raw = ollama_generate(
            model, "Doména: %s\nFakta o modelu: %s\n\nODŮVODNĚNÍ:" % (topic, facts),
            system=sys, config=config, timeout=30, keep_alive=-1,
            options={"temperature": 0.4, "num_predict": 90})
        if raw and raw.strip():
            return raw.strip()
    except Exception as e:
        _log.debug("toolscout rationale: %s", e)
    return ("Navrhuji %s (%s, ~%s GB, %s stažení, %s). %s." % (
        cand["name"], cand["size_tag"], cand["est_gb"], cand["pulls"],
        ", ".join(cand["capabilities"]) or "obecný", cand["note"]))


# ── ToolStore (tabulka tool_proposals) ───────────────────────────────────────
class ToolStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure()

    def _conn(self):
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure(self):
        try:
            c = self._conn()
            c.execute("""CREATE TABLE IF NOT EXISTS tool_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, topic TEXT,
                tool_name TEXT, size_tag TEXT, est_gb REAL, fit TEXT,
                pulls TEXT, capabilities TEXT, rationale TEXT, url TEXT,
                status TEXT DEFAULT 'pending')""")
            c.commit()
            c.close()
        except Exception as e:
            _log.warning("toolscout ensure: %s", e)

    def has_for_topic(self, topic: str) -> bool:
        """Už existuje živý (pending/approved/installed) návrh pro téma?

        HANS_TOOLSCOUT_NO_MATCH_V1 — `none` se počítá TAKY: je to uzavřený
        výsledek („hledal jsem, nic vhodného není"), ne chyba. Bez toho se
        beznadějné téma zkouší donekonečna a drží frontu (viz
        `search_library_ex`). `rejected` se schválně NEPOČÍTÁ — když návrh
        zamítneš, Hans smí příště nabídnout jiný.
        """
        try:
            c = self._conn()
            r = c.execute(
                "SELECT 1 FROM tool_proposals WHERE topic=? AND status IN "
                "('pending','approved','installed','none') LIMIT 1",
                (topic,)).fetchone()
            c.close()
            return r is not None
        except Exception:
            return False

    def add(self, topic: str, cand: dict, rationale: str) -> int:
        c = self._conn()
        cur = c.execute(
            "INSERT INTO tool_proposals (ts, topic, tool_name, size_tag, est_gb, "
            "fit, pulls, capabilities, rationale, url, status) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,'pending')",
            (time.time(), topic, cand["name"], cand["size_tag"], cand["est_gb"],
             cand["fit"], cand["pulls"], ",".join(cand["capabilities"]),
             rationale, cand["url"]))
        c.commit()
        pid = cur.lastrowid
        c.close()
        return pid

    def mark_none(self, topic: str, keyword: str) -> int:
        """HANS_TOOLSCOUT_NO_MATCH_V1 — zapiš, že pro téma nic vhodného není.

        ⛔ ZÁMĚRNĚ se neukládá žádný „náhradní" nástroj — vymyšlený návrh by
        byl konfabulace ([[anticonfabulation-guiding-principle]]). Řádek nese
        jen fakt, že se hledalo a nic nesedlo, a `status='none'` ho drží mimo
        výpis pendingů (`list('pending')`), takže se nedá omylem schválit.
        """
        c = self._conn()
        cur = c.execute(
            "INSERT INTO tool_proposals (ts, topic, tool_name, size_tag, "
            "est_gb, fit, pulls, capabilities, rationale, url, status) VALUES "
            "(?,?,'','',0,'','','',?,'','none')",
            (time.time(), topic,
             "Hledal jsem v knihovně modelů pod klíčem „%s“ a nic vhodného "
             "jsem nenašel. Pro tohle téma nástroj nenavrhuji." % keyword))
        c.commit()
        pid = cur.lastrowid
        c.close()
        return pid

    def list(self, status: str = None) -> list:
        c = self._conn()
        if status:
            rows = c.execute(
                "SELECT id, topic, tool_name, size_tag, est_gb, fit, pulls, "
                "capabilities, rationale, url, status FROM tool_proposals "
                "WHERE status=? ORDER BY id DESC", (status,)).fetchall()
        else:
            rows = c.execute(
                "SELECT id, topic, tool_name, size_tag, est_gb, fit, pulls, "
                "capabilities, rationale, url, status FROM tool_proposals "
                "ORDER BY id DESC LIMIT 20").fetchall()
        c.close()
        keys = ("id", "topic", "tool_name", "size_tag", "est_gb", "fit", "pulls",
                "capabilities", "rationale", "url", "status")
        return [dict(zip(keys, r)) for r in rows]

    def get(self, pid: int) -> Optional[dict]:
        for p in self.list():
            if p["id"] == pid:
                return p
        c = self._conn()
        r = c.execute("SELECT id, topic, tool_name, size_tag, est_gb, fit, pulls,"
                      " capabilities, rationale, url, status FROM tool_proposals "
                      "WHERE id=?", (pid,)).fetchone()
        c.close()
        if not r:
            return None
        keys = ("id", "topic", "tool_name", "size_tag", "est_gb", "fit", "pulls",
                "capabilities", "rationale", "url", "status")
        return dict(zip(keys, r))

    def set_status(self, pid: int, status: str) -> bool:
        c = self._conn()
        cur = c.execute("UPDATE tool_proposals SET status=? WHERE id=?",
                        (status, pid))
        c.commit()
        ok = cur.rowcount > 0
        c.close()
        return ok


# ── top-level: návrh + install ───────────────────────────────────────────────
def propose_tool(config: dict, db_path: str, topic: str,
                 max_props: int = 2) -> dict:
    """Scout + LLM ranking → uloží top N kandidátů jako pending. Vrací
    {status: proposed/idle/deferred, proposals:[...]}. Idempotentní per téma."""
    if not enabled(config):
        return {"status": "idle", "reason": "vypnuto"}
    store = ToolStore(db_path)
    if store.has_for_topic(topic):
        return {"status": "idle", "reason": "návrh pro téma už existuje"}
    kw = _derive_keyword(config, topic)
    scouted = scout_tools_for_topic(config, topic, kw)
    cands = scouted["candidates"]
    if not cands:
        # HANS_TOOLSCOUT_NO_MATCH_V1 — dvě různé situace, dva různé závěry.
        if scouted.get("fetch_ok"):
            store.mark_none(topic, kw)      # knihovna nic nemá → HOTOVO
            return {"status": "none",
                    "reason": "knihovna nic vhodného pod „%s“ nemá" % kw}
        return {"status": "deferred", "reason": "hledání selhalo (síť?)"}
    # seřaď (viz HANS_TOOLSCOUT_RANK_V2 níž): popularita, pak coexist
    def _pull_num(p):
        m = re.match(r"([0-9.]+)\s*([BMK])?", p.get("pulls", "") or "")
        if not m:
            return 0.0
        v = float(m.group(1))
        return v * (1e9 if m.group(2) == "B" else
                    1e6 if m.group(2) == "M" else
                    1e3 if m.group(2) == "K" else 1)
    # HANS_TOOLSCOUT_RANK_V2 — POPULARITA PRVNÍ, „vejde se vedle chatu" až
    # jako rozřazovač shody. Dřív byl `coexist` tvrdá první podmínka, takže
    # malý model vždy porazil větší — změřeno 25.8. na tématu hradů: klíč
    # `vision` by vybral `moondream:1.8b`, ačkoliv Hans má `qwen2.5vl:7b`,
    # tedy DOWNGRADE. `on_demand` je přitom běžný režim (chat se dočasně
    # odloží jako při herním módu), není důvod se mu vyhýbat za cenu kvality.
    _mam = installed_models(config)
    if _mam:
        # Nenavrhuj, co už na PC je — jinak hrozí „vezmi si menší verzi toho,
        # co máš". Prázdná množina = PC dole → radši nefiltrovat než zahodit
        # kandidáty kvůli neznalosti.
        _pred = len(cands)
        cands = [c for c in cands if c["name"] not in _mam]
        if _pred != len(cands):
            _log.debug("toolscout: %d kandidátů vynecháno (už nainstalováno)",
                       _pred - len(cands))
        if not cands:
            store.mark_none(topic, kw)
            return {"status": "none",
                    "reason": "vše vhodné pod „%s“ už mám nainstalované" % kw}
    cands.sort(key=lambda c: (-_pull_num(c), 0 if c["fit"] == "coexist" else 1))
    props = []
    for cand in cands[:max_props]:
        rat = _rationale(config, topic, cand)
        pid = store.add(topic, cand, rat)
        props.append({"id": pid, **cand, "rationale": rat})
    _log.info("toolscout: %d návrh(ů) pro '%s' (klíč '%s')", len(props), topic, kw)
    return {"status": "proposed", "topic": topic, "keyword": kw,
            "proposals": props}


def pull_model(config: dict, name: str) -> dict:
    """Spusť `ollama pull <name>` na PC (Ollama tam běží) přes SSH, ODPOJENĚ
    (stahování je GB/pomalé). Vrací {ok, detail}. Volá se JEN po schválení."""
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return {"ok": False, "detail": "pc_remote vypnut — stáhni ručně: "
                    "ollama pull %s" % name}
        # bezpečné jméno modelu (jen povolené znaky)
        if not re.match(r"^[a-zA-Z0-9._:-]+$", name):
            return {"ok": False, "detail": "podezřelé jméno modelu"}
        cmd = ("nohup ollama pull %s > /tmp/hans_pull_%s.log 2>&1 & echo started"
               % (name, re.sub(r"[^a-zA-Z0-9]", "_", name)))
        out = pc_remote.run(config, cmd, timeout=20)
        if out is not None and "started" in str(out):
            return {"ok": True, "detail": "stahování spuštěno na PC (na pozadí)"}
        return {"ok": False, "detail": "spuštění se nepovedlo (%r)" % out}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:100]}


def installed_models(config: dict) -> set:
    """HANS_TOOLSCOUT_RANK_V2 — množina ZÁKLADNÍCH jmen modelů na PC.

    Jedno `ollama list` pro celou dávku (ne dotaz na každého kandidáta).
    Prázdná množina = nevíme (PC dole / pc_remote vypnut) — volající pak
    NESMÍ nic filtrovat ani razítkovat, aby se z neznalosti nestal závěr.
    """
    try:
        from scripts import pc_remote
        if not pc_remote.enabled(config):
            return set()
        out = pc_remote.run(config, "ollama list", timeout=12)
        if not out:
            return set()
        naz = set()
        for ln in str(out).splitlines()[1:]:      # 1. řádek je hlavička
            ln = ln.strip()
            if ln:
                naz.add(ln.split()[0].split(":")[0])
        return naz
    except Exception as e:
        _log.debug("installed_models: %s", e)
        return set()


def is_installed(config: dict, name: str) -> bool:
    """Ověř `ollama list` na PC, že model už je stažený."""
    return name.split(":")[0] in installed_models(config)


def verify_approved(config: dict, db_path: str) -> int:
    """HANS_TOOLSCOUT_VERIFY_V1 — orazítkuj `installed` AŽ když model reálně je.

    `pull_model` spouští `ollama pull` ODPOJENĚ (`nohup … &`) a vrací ok už při
    „started", takže dřív se `installed` nastavovalo hned po schválení — když
    stahování selhalo (síť, místo na disku), Hans si myslel, že nástroj má.
    Tahle kontrola běží v nočním ticku a povýší `approved → installed` teprve
    podle `ollama list`. Neúspěšný pull zůstane `approved` = poctivé „schváleno,
    ale ještě to tu není". Vrací počet povýšených.
    """
    naz = installed_models(config)
    if not naz:
        return 0                      # nevíme → nerazítkovat (viz docstring)
    store = ToolStore(db_path)
    n = 0
    for p in store.list("approved"):
        if (p.get("tool_name") or "").split(":")[0] in naz:
            if store.set_status(p["id"], "installed"):
                n += 1
                _log.info("HANS_TOOLSCOUT_VERIFY_V1: %s dorazil → installed",
                          p.get("tool_name"))
    return n


if __name__ == "__main__":
    import sys
    cfg = json.loads(open("config.json", encoding="utf-8").read())
    topic = sys.argv[1] if len(sys.argv) > 1 else "Design"
    print("=== scout '%s' ===" % topic)
    kw = _derive_keyword(cfg, topic)
    print("klíč:", kw)
    sc = scout_tools_for_topic(cfg, topic, kw)
    for c in sc["candidates"][:6]:
        print("  %-22s %-5s ~%4sGB %-9s %8s  %s" % (
            c["name"], c["size_tag"], c["est_gb"], c["fit"], c["pulls"],
            ",".join(c["capabilities"])))
