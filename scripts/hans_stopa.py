"""HANS_STOPA_V1 (27. 9.) — stopa jednoho dotazu pro vizualizaci „jak Hans přemýšlí“.

Nápad uživatele: okno chatu a pod ním rozhodovací kroky spojené čarou, kudy
dotaz skutečně prošel. Stopa se NEPÍŠE ručně do rozhodovací logiky (35 vložených
volání do 1 200řádkové funkce by ji mohlo rozbít a časem by lhala). Místo toho:

1. `sys.settrace` jen pro rámec `send_chat_message`: které řádky se provedly
   a na kterém `return` funkce skončila. Kroky = úseky kódu mezi značkami
   `# NĚCO_V1`; úsek s provedeným řádkem = krok zkoušen, úsek s `return` =
   tady odpověď skončila, úsek bez provedeného řádku = sem se dotaz nedostal.
   Rozdělení čte ze zdrojáku při prvním použití, takže sedí i po změnách kódu.
2. Záznamy logu téhož vlákna během dotazu, přiřazené ke kroku, ve kterém
   vznikly — to jsou DŮVODY („GROUNDING: … ← film_recall“, agent, guard).

Nic nemění: jakákoli chyba stopy = dotaz proběhne jako bez ní.
Zápis `data/stopy/<id>.json` (posledních `KEEP`), čte web admin `/api/chat/stopa`.
"""
from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import threading
import time
from pathlib import Path

DIR = Path("data/stopy")
KEEP = 200
ZIVE_S = 0.25   # HANS_STOPA_ZIVE_V1 — jak často se zapisuje průběžný stav

# Hranice vrstev = první značka vrstvy (pořadí podle kódu).
VRSTVY = [
    ("Vstup", None),
    ("Příkazy", "HANS_CHAT_CHANNEL_AWARE_V1"),
    ("Přímé odpovědi", "HANS_KNOWLEDGE_CHECK_V1"),
    ("Agent", "HANS_AGENT_V1"),
    ("Podklad", "HANS_ASKER_PREFIX_RETRIEVAL_ONLY_V1"),
    ("Model", "__MODEL__"),
    ("Pojistky", "G4D_DEDUP_ADDRESS_V1"),
    ("Odpověď", "HANS_TEST_PERSON_V1"),
]

# HANS_STOPA_KOLAC_V1 (27. 9.) — strom i pro rozhovor Hanse s Koláčem
# (`HansDialog._run_dialog`). Vrstvy podle značek v té funkci.
VRSTVY_KOLAC = [
    ("Brána", None),
    ("Zkouška", "KOLAC_EXAM_WIRE_V1"),
    ("Téma a kontext", "HANS_STUDY_KOLAC_V1"),
    ("Dvě mysli", "HANS_KOLAC_MIND_V1"),
    ("Po rozhovoru", "KOLAC_STANCE_CHALLENGE_V1"),
    ("Zápis", "KOLAC_DIALOG_COHERENCE_V1"),
]

DRUHY = {
    "chat": {"vrstvy": VRSTVY, "model": ("self._stream_message(",)},
    "kolac": {"vrstvy": VRSTVY_KOLAC,
              "model": ("self._generate_two_minds(", "self._call_gemini(")},
}

POPISKY = {
    "KOLAC_EXAM_BRAIN_GATE_V1": "je mozek (PC) vzhůru?",
    "KOLAC_EXAM_WIRE_V1": "zkouška místo rozhovoru?",
    "KOLAC_EXAM_DENNI_STROP_V1": "denní strop zkoušek",
    "HANS_STUDY_KOLAC_V1": "co Hans studuje, výběr tématu",
    "HANS_KOLAC_MIND_V1": "dvě mysli: Hans ↔ Koláč",
    "HANS_DIALOG_OLLAMA_ONLY_V1": "jen přes Ollamu",
    "KOLAC_STANCE_CHALLENGE_V1": "Koláč zpochybnil postoj?",
    "HANS_KOLAC_LABEL_MATCH_V1": "kdo co řekl",
    "KOLAC_DIALOG_COHERENCE_V1": "historie rozhovorů",
    "TEDDY_TOPIC_NOTE_V1": "zápis do deníku",
    "HANS_CHAT_CHANNEL_AWARE_V1": "/url, /note, /read",
    "HANS_THREAD_V1": "nitka rozhovoru, chatové příkazy",
    "HANS_FILM_OPINION_ANAFORA_V1": "„a který se ti líbil?“ po výpisu",
    "HANS_CONFIRM_PRECEDENCE_V2": "agent čeká na potvrzení",
    "HANS_CMD_LLM_ROUTE_V1": "model vybírá příkaz, když vzory minou",
    "HANS_THREAD_LLMROUTE_V1": "model vybírá příkaz s kontextem",
    "HANS_CMD_LLM_ROUTE_V4": "vlákno i pro pevné příkazy",
    "HANS_STUDY_CONTENT_RECALL_V1": "co jsem studoval",
    "HANS_PROVENANCE_NOT_LIST_V1": "„odkud to máš?“",
    "HANS_CLAIM_HOLD_V1": "uživatel tvrdí opak",
    "HANS_KNOWLEDGE_CHECK_V1": "kontrola znalostí",
    "HANS_DATETIME_ANSWER_V1": "datum a čas",
    "HANS_ASKER_STATE_V1": "„vidíte mě?“, „kdo jsem?“",
    "HANS_BOOK_RECOMMEND_V1": "doporučení knihy",
    "HANS_PERSON_CARD_BYPASS_V1": "karta osoby",
    "HANS_INSTANT_LOOKUP_V1": "okamžité dohledání",
    "HANS_REMEMBER_HONEST_V1": "„pamatuješ si?“ poctivě",
    "HANS_SOURCE_QUERY_V1": "zdroje",
    "HANS_STUDY_DEEPEN_V2": "prohloubení studia",
    "HANS_CHAT_WAIT_FOR_PAINT_V1": "právě maluju",
    "HANS_AGENT_V1": "agent: akce z rozhovoru",
    "HANS_ASKER_PREFIX_RETRIEVAL_ONLY_V1": "podklad a paměť",
    "HANS_A1_ONLY_FOR_QUESTIONS_V1": "brzda A1 (nemám podklad)",
    "HANS_SELFCONSISTENCY_A1_V1": "hledání podkladu (grounding)",
    "HANS_OPINION_GROUNDING_G1_V1": "podklad pro názor",
    "HANS_EVIDENCE_V1": "důkazy pro kontrolu opory",
    "HANS_EVIDENCE_AB_V1": "měření opory (A/B)",
    "GROUNDING_GUARD_ACTIVE_V2": "kontrola opory — zásah",
    "GROUNDING_GUARD_ACTIVE_V3": "kontrola opory — zásah u tenkého podkladu",
    "HANS_A1_NOT_FOR_OWN_STATE_V1": "brzda A1 u vlastního stavu",
    "HANS_GUARD_QUOTE_NOTE_V1": "citace ze zápisků",
    "HANS_REFLECTIVE_ASK_V1": "úvahová otázka",
    "HANS_SOURCE_META_MEMORY_V1": "otázka na zdroj",
    "HANS_ANCHOR_LOOKUP_ON_ADMIT_V1": "dohledání po přiznání „nevím“",
    "HANS_GREETING_OUTPUT_TRIM_V2": "zkrácení pozdravu (sám pozdravil)",
    "HANS_READ_URL_NL_V1": "„přečti si…“ řečí",
    "HANS_DOWNTIME_V1": "výpadek mozku",
    "HANS_ANCHOR_LOOKUP_V1": "dohledání kotvy",
    "CLAIM_RETRACT_V1": "odvolání tvrzení",
    "__MODEL__": "hans-czech odpovídá",
    "G4D_DEDUP_ADDRESS_V1": "opakované oslovení",
    "HANS_FILM_DIRECTOR_CHECK_V1": "film a režisér",
    "GROUNDING_GUARD_V1": "kontrola opory v podkladu",
    "HANS_ADDRESSEE_V2": "oslovení cizí osoby",
    "HANS_WEEKDAY_FIX_V1": "den v týdnu",
    "HANS_GREETING_OUTPUT_TRIM_V1": "zkrácení pozdravu",
    "HANS_TEST_PERSON_V1": "uložení (deník, RAG)",
    "HANS_CHAT_RECALL_V1": "věrný záznam do paměti",
}

_ZNACKA = re.compile(r"\s*#\s*(?:[─—-]+\s*)?([A-Z][A-Z0-9_]*_V\d+)")
_mapy: dict = {}
_lock = threading.Lock()


def _mapa(func, model=("self._stream_message(",)):
    """[(řádek, značka)] seřazeně + řádky volání modelu. Cache podle kódu."""
    code = func.__code__
    m = _mapy.get(code)
    if m is not None:
        return m
    lines, start = inspect.getsourcelines(func)
    kroky, videno = [], set()
    for i, l in enumerate(lines):
        mm = _ZNACKA.match(l)
        if mm:
            z = mm.group(1)
            # opakovaná značka hned za sebou (víceřádkový komentář) = jeden krok
            if kroky and kroky[-1][1] == z:
                continue
            kroky.append((start + i, z))
            videno.add(z)
        elif any(p in l for p in model):
            kroky.append((start + i, "__MODEL__"))
    konec = start + len(lines)
    m = (kroky, konec)
    _mapy[code] = m
    return m


class _Sber(logging.Handler):
    def __init__(self, tid, stav):
        super().__init__(level=logging.INFO)
        self.tid, self.stav, self.zaznamy = tid, stav, []

    def emit(self, rec):
        if rec.thread != self.tid or len(self.zaznamy) >= 300:
            return
        try:
            self.zaznamy.append({"t": round(rec.created - self.stav["t0"], 3),
                                 "radek": self.stav["radek"],
                                 "kdo": rec.name, "uroven": rec.levelname,
                                 "zprava": rec.getMessage()[:400]})
        except Exception:
            pass


def spust(bound_method, *args, rid=None, kanal="", osoba="", zprava="",
          druh="chat", **kwargs):
    """Zavolá `bound_method(*args, **kwargs)` a uloží stopu. Výsledek vrací beze změny."""
    try:
        func = bound_method.__func__
        kroky, konec_fn = _mapa(func, DRUHY[druh]["model"])
    except Exception:
        return bound_method(*args, **kwargs)
    code = func.__code__
    stav = {"t0": time.time(), "radek": 0, "navrat": None, "prikaz": None,
            "agent": None, "druh": druh}
    provedeno: dict = {}
    try:   # rozhodnutí agenta čteme z JEHO rámce (proměnné decision/aid/conf)
        from scripts.hans_agent import AgentRouter as _AR
        agent_code = _AR.propose.__code__
    except Exception:
        agent_code = None

    def _lokal(frame, event, arg):
        if event == "line":
            stav["radek"] = frame.f_lineno
            provedeno.setdefault(frame.f_lineno, round(time.time() - stav["t0"], 3))
            if druh == "kolac" and "tema" not in stav:   # téma hned (živý strom)
                try:
                    t = frame.f_locals.get("topic")
                    if t is not None:
                        stav["tema"] = str(getattr(t, "subject", None) or t)[:120]
                except Exception:
                    pass
        elif event == "return":
            stav["navrat"] = frame.f_lineno
            try:
                c = frame.f_locals.get("_cmd")
                if c:
                    stav["prikaz"] = str(c[0] if isinstance(c, (tuple, list)) else c)
                if druh == "kolac":             # HANS_STOPA_KOLAC_V1
                    t = frame.f_locals.get("topic")
                    if t is not None:
                        stav["tema"] = str(getattr(t, "subject", None) or t)[:120]
                    dl = frame.f_locals.get("dialog")
                    if dl:
                        stav["dialog"] = str(dl)[:4000]
            except Exception:
                pass
        return _lokal

    def _agent(frame, event, arg):
        if event == "return":
            try:
                L = frame.f_locals
                d = L.get("decision") or {}
                stav["agent"] = {"akce": L.get("aid") or (d.get("action") if isinstance(d, dict) else None),
                                 "router": d.get("action") if isinstance(d, dict) else None,
                                 "jistota": L.get("conf"),
                                 "vysledek": "návrh" if arg else "bez akce",
                                 "t": round(time.time() - stav["t0"], 3)}
            except Exception:
                pass
        return _agent

    def _globalni(frame, event, arg):
        if frame.f_code is code:
            return _lokal
        if agent_code is not None and frame.f_code is agent_code:
            return _agent
        return None

    sber = _Sber(threading.get_ident(), stav)
    root = logging.getLogger()
    predtim = sys.gettrace()
    root.addHandler(sber)
    # HANS_STOPA_ZIVE_V1 (27. 9.) — průběžný stav pro živý strom: vlastní vlákno
    # (settrace platí jen pro vlákno dotazu, tohle se tedy samo nestopuje)
    # zapisuje `zive.json` á `ZIVE_S`. Chyba zápisu = jen chybí živý pohled.
    hotovo = threading.Event()
    rid_z = rid or time.strftime("%Y%m%d_%H%M%S")

    def _zive():
        while not hotovo.wait(ZIVE_S):
            try:
                d = _sestav(rid_z, kanal, osoba, zprava, None, None, stav,
                            dict(provedeno), kroky, konec_fn, list(sber.zaznamy),
                            bezi=True)
                _zapis_atomicky(DIR / "zive.json", d)
            except Exception:
                pass
    threading.Thread(target=_zive, name="stopa-zive", daemon=True).start()
    sys.settrace(_globalni)
    vysledek, chyba = None, None
    try:
        vysledek = bound_method(*args, **kwargs)
    except BaseException as e:
        chyba = e
    finally:
        sys.settrace(predtim)
        root.removeHandler(sber)
        hotovo.set()
    try:
        _h = getattr(bound_method, "__self__", None)
        stav["podklad"] = {"vysledek": getattr(_h, "_grounding_outcome", None),
                           "cesta": getattr(_h, "_grounding_cesta", None)}
    except Exception:
        stav["podklad"] = None
    try:
        _uloz(rid_z, kanal, osoba, zprava, vysledek, chyba, stav, provedeno,
              kroky, konec_fn, sber.zaznamy)
        _uloz_strom(func, kroky, druh)
    except Exception as e:
        logging.getLogger("hans_stopa").debug("stopa neuložena: %s", e)
    if chyba is not None:
        raise chyba
    return vysledek


def _zapis_atomicky(p: Path, data: dict):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def _sestav(rid, kanal, osoba, zprava, vysledek, chyba, stav, provedeno, kroky,
            konec_fn, zaznamy, bezi=False):
    """Data stopy. `bezi` = průběžný stav: bez „zachytil“, s krokem `ted`."""
    V = DRUHY[stav.get("druh", "chat")]["vrstvy"]
    hranice = [(z, v) for v, z in V if z]
    vrstva = V[0][0]
    navrat = stav["navrat"]
    out = []
    for i, (od, z) in enumerate(kroky):
        do = kroky[i + 1][0] if i + 1 < len(kroky) else konec_fn
        for hz, hv in hranice:
            if z == hz:
                vrstva = hv
        radky = [r for r in provedeno if od <= r < do]
        if not bezi and navrat is not None and od <= navrat < do:
            s = "zachytil"
        elif radky:
            s = "zkousen"
        else:
            s = "nedosel"
        out.append({"znacka": z, "popisek": POPISKY.get(z, ""), "vrstva": vrstva,
                    "od": od, "do": do, "stav": s,
                    "t": min((provedeno[r] for r in radky), default=None),
                    "log": [x for x in zaznamy if od <= (x["radek"] or 0) < do]})
    pred = [x for x in zaznamy if not any(k["od"] <= (x["radek"] or 0) < k["do"] for k in out)]
    ted = None
    if bezi:
        r = stav.get("radek") or 0
        ted = next((k["znacka"] + "@" + str(k["od"]) for k in out
                    if k["od"] <= r < k["do"]), None)
    if stav.get("druh") == "kolac" and stav.get("tema"):   # HANS_STOPA_KOLAC_V1
        zprava = stav["tema"]
    data = {"bezi": bezi, "ted": ted, "id": rid or time.strftime("%Y%m%d_%H%M%S"), "ts": stav["t0"],
            "kanal": kanal, "osoba": osoba, "zprava": zprava,
            "druh": stav.get("druh", "chat"),
            "odpoved": (str(vysledek) if vysledek is not None else
                        stav.get("dialog") or "")[:4000],
            "chyba": repr(chyba) if chyba else None,
            "trvani_s": round(time.time() - stav["t0"], 2), "navrat_radek": navrat,
            "vrstvy": [v for v, _ in V], "kroky": out, "log_mimo": pred,
            "volby": {"prikaz": stav.get("prikaz"), "agent": stav.get("agent"),
                      "podklad": stav.get("podklad"), "tema": stav.get("tema")}}
    return data


def _uloz(rid, kanal, osoba, zprava, vysledek, chyba, stav, provedeno, kroky,
          konec_fn, zaznamy):
    data = _sestav(rid, kanal, osoba, zprava, vysledek, chyba, stav, provedeno,
                   kroky, konec_fn, zaznamy)
    DIR.mkdir(parents=True, exist_ok=True)
    p = DIR / (re.sub(r"[^A-Za-z0-9_-]", "", str(data["id"])) + ".json")
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    (DIR / "posledni.json").write_text(json.dumps(data, ensure_ascii=False),
                                       encoding="utf-8")
    _zapis_atomicky(DIR / "zive.json", data)   # HANS_STOPA_ZIVE_V1 — konec běhu
    with _lock:
        stare = sorted((f for f in DIR.glob("*.json")
                        if f.name not in ("posledni.json", "zive.json")
                        and not f.name.startswith("strom")),
                       key=lambda f: f.stat().st_mtime)
        for f in stare[:-KEEP]:
            try:
                f.unlink()
            except Exception:
                pass


_strom_ulozen: set = set()


def _uloz_strom(func, kroky, druh="chat"):
    """Úplný strom MOŽNOSTÍ (ne jen prošlá cesta): vrstvy → kroky → volby
    (chatové příkazy, akce agenta, cesty podkladu). Bere se z kódu, takže sedí
    i po změnách. Zapisuje se jen při změně kódu funkce."""
    global _strom_ulozen
    soubor = DIR / ("strom.json" if druh == "chat" else "strom_%s.json" % druh)
    klic = (func.__code__, len(kroky))
    if klic in _strom_ulozen and soubor.exists():
        return
    if druh == "kolac":               # HANS_STOPA_KOLAC_V1 — téma je jediná volba
        V = VRSTVY_KOLAC
        hranice = {z: v for v, z in V if z}
        vrstvy, vr = {v: [] for v, _ in V}, V[0][0]
        for od, z in kroky:
            vr = hranice.get(z, vr)
            vrstvy[vr].append({"znacka": z, "popisek": POPISKY.get(z, ""), "od": od})
        data = {"vrstvy": [{"nazev": v, "kroky": vrstvy[v]} for v, _ in V],
                "volby": {"tema": {"vrstva": "Téma a kontext", "za": "HANS_STUDY_KOLAC_V1",
                                   "nazev": "téma", "moznosti": []}}}
        DIR.mkdir(parents=True, exist_ok=True)
        soubor.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _strom_ulozen.add(klic)
        return
    prikazy, akce, cesty = [], [], []
    try:
        from scripts import chat_commands as _cc
        prikazy = sorted(_cc._COMMANDS.keys())
    except Exception:
        pass
    try:
        from scripts.hans_agent import ACTIONS as _A
        akce = sorted(_A.keys())
    except Exception:
        pass
    try:
        src = inspect.getsource(inspect.getmodule(func))
        cesty = sorted(set(re.findall(r"_vysledek_groundingu\(\s*'\w+'\s*,\s*'(\w+)'", src)))
    except Exception:
        pass
    hranice = {z: v for v, z in VRSTVY if z}
    vrstvy, vr = {v: [] for v, _ in VRSTVY}, VRSTVY[0][0]
    for od, z in kroky:
        vr = hranice.get(z, vr)
        vrstvy[vr].append({"znacka": z, "popisek": POPISKY.get(z, ""), "od": od})
    data = {"vrstvy": [{"nazev": v, "kroky": vrstvy[v]} for v, _ in VRSTVY],
            "volby": {"prikaz": {"vrstva": "Příkazy", "za": "HANS_THREAD_V1",
                                 "nazev": "chatový příkaz", "moznosti": prikazy},
                      "agent": {"vrstva": "Agent", "za": "HANS_AGENT_V1",
                                "nazev": "akce agenta", "moznosti": akce},
                      "podklad": {"vrstva": "Podklad", "za": "HANS_SELFCONSISTENCY_A1_V1",
                                  "nazev": "cesta podkladu", "moznosti": cesty}}}
    DIR.mkdir(parents=True, exist_ok=True)
    soubor.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _strom_ulozen.add(klic)
