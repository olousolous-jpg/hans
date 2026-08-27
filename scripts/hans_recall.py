#!/usr/bin/env python3
"""
HANS_RECALL_SHORTCIRCUIT_V1 — deterministický short-circuit vnitřních
paměťových dotazů (#1 z anti-konfabulačního pořadí).

Faktické dotazy dohledatelné PŘÍMO V DATECH se NEposílají do LLM — odpoví
se deterministickou šablonou z deníku (vzor HANS_LIVE_PLAYBACK_QUERY_V1):

  - „první / nejstarší vzpomínka"  → MIN(ts) z deníku (řeší doložený případ
    [[first-memory-confabulation]] — Hans si vymýšlel rok 2024 s přesnými čísly)
  - „co / kdy jsi četl (o X)"      → reálné čtecí eventy z deníku
  - „kdy jsi mě / X viděl"          → person_seen

Nulová konfabulace: negeneruje se. Když data nejsou, přizná to („o tom nemám
záznam") místo výmyslu. Registrace příkazů je v chat_commands.py — tady jsou
jen čisté read-only funkce (testovatelné offline).
"""
from __future__ import annotations

import logging
from scripts.logger import log_once
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

from scripts.cz_names import address as _cz_address  # HANS_NAME_INFLECTION_V1

_log = logging.getLogger(__name__)

_DNY = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")
_MESICE_GEN = ("", "ledna", "února", "března", "dubna", "května", "června",
               "července", "srpna", "září", "října", "listopadu", "prosince")

# Čtecí event typy (co Hans reálně četl/studoval)
_READ_TYPES = ("web_read", "reading_takeaway", "book_read", "study_note",
               "book_completion_reflection", "book_reflection")

# HANS_READING_KODI_SPLIT_V1 (25.8.) — CETBA vs CLANEK KVULI FILMU.
# Nalez uzivatele 24.8.: „dotaz na cetbu — do ni micha filmy" (Jakubuv
# zebrik, Na sever severozapadni linkou). Retez: `kodi_playing` →
# MOVIE_GROUNDING_V1 si o filmu precte Wikipedii → `web_read` +
# `reading_takeaway`. Hans opravdu cetl, takze zaznam je spravne — jen to
# neni JEHO cetba a do vypisu „co jsi cetl" nepatri.
#
# ⚠️ ZMERENO 25.8., proc nestaci `json_extract(data,'$.topic')='kodi'`,
# jak navrhoval backlog: priznak `topic` nese POUZE `web_read`.
# `reading_takeaway` ma v `data` jen prozu reflexe, takze by filtr chytil
# 59 ze 154 filmovych zaznamu za 30 dni (38 %) a reflexe k TEMUZ filmu by
# ve vypisu zustala. Spojka je shodny TITUL: 95 z 493 `reading_takeaway`
# (19 %) ma titul shodny s nejakym kodi `web_read`.
#
# ⛔ Filtruji se JEN tyto dva typy — `book_read`/`book_reflection`/
# `study_note` nikdy, aby se kniha se stejnym nazvem jako film neschovala.
_KODI_FILTR_TYPY = ("web_read", "reading_takeaway")


def _kodi_tituly(conn) -> set:
    """Tituly, jejichz clanek vznikl kvuli prehravanemu filmu (lower)."""
    try:
        # `LIKE` prefiltr drzi json parsovani mimo vetsinu z 63k radku;
        # `json_valid` je NUTNY — starsi `web_read` maji `data` prazdne
        # a `json_extract` na nich shodi cely dotaz na „malformed JSON".
        rows = conn.execute(
            "SELECT DISTINCT title FROM diary "
            "WHERE event_type='web_read' AND data LIKE '%\"kodi\"%' "
            "AND json_valid(data) "
            "AND json_extract(data,'$.topic')='kodi'").fetchall()
    except Exception as e:          # rozbity dotaz nesmi shodit cely /cetl
        _log.debug("_kodi_tituly selhal: %s", e)
        return set()
    return {(r[0] or "").strip().lower() for r in rows if (r[0] or "").strip()}


def _je_k_filmu(etype, title, kodi: set) -> bool:
    """Je tenhle cteci zaznam jen clanek k prehravanemu filmu?"""
    return bool(kodi) and etype in _KODI_FILTR_TYPY and \
        (title or "").strip().lower() in kodi


_DNY_AKUZ = ("v pondělí", "v úterý", "ve středu", "ve čtvrtek", "v pátek",
             "v sobotu", "v neděli")


def _cz_when(ts: float, with_weekday: bool = True) -> str:
    """'v pátek 25. dubna 2026 v 19:05' — česky, deterministicky."""
    d = datetime.fromtimestamp(ts)
    day = f"{d.day}. {_MESICE_GEN[d.month]} {d.year}"
    out = f"{day} v {d:%H:%M}"
    if with_weekday:
        out = f"{_DNY_AKUZ[d.weekday()]} {out}"
    return out


def _cz_date(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d.day}. {_MESICE_GEN[d.month]}"


def _fold(s: str) -> str:
    """Bez diakritiky (oběd→obed) — uživatelé píšou bez háčků, deník s nimi."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def _ro(db_path: str) -> sqlite3.Connection:
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)


# ── konverzační recall (HANS_CHAT_RECALL_V2) — „pamatuješ na náš rozhovor o X" ─
# Sémantický RAG vágní recall dotaz často nedohledá (uložené repliky = kuchařský
# text, dotaz = „pamatuješ co jsi navrhl"). Tady deterministicky prohledáme
# skutečný human_chat (obě strany) na obsahová slova dotazu — spolehlivě najde
# původní výměnu, kterou RAG mine.
_CONV_STOP = {
    "pamatuješ", "pamatujes", "vzpomínáš", "vzpominas", "náš", "naš", "ten",
    "tom", "tam", "který", "ktery", "která", "ktera", "které", "ktere", "jsi",
    "jsem", "jsme", "mi", "mě", "me", "že", "ze", "se", "si", "už", "uz",
    "před", "pred", "byl", "byla", "bylo", "kdy", "kde", "proč", "proc",
    "řekl", "rekl", "říkal", "rikal", "mluvili", "bavili", "povídali",
    "povidali", "nějak", "nejak", "prosím", "prosim", "můžeš", "muzes",
    "tomhle", "tamtom", "jak", "and", "the", "můj", "muj", "moje", "tvůj",
}


# synonymové shluky pro časté recall domény — dotaz a původní zpráva se často
# NEPŘEKRÝVAJÍ doslovně („recept/doporučil" × „oběd/navrhni") → shluk je spojí.
_SYN_CLUSTERS = [
    {"recep", "jídl", "jidl", "oběd", "obed", "večeř", "vecer", "snída", "snida",
     "pokrm", "vaře", "vare", "uvař", "uvar", "jíst", "jist", "kuchy", "chuť",
     "chut", "ingred", "chod", "menu", "svač", "polév", "polev"},   # jídlo/recept
    {"dopor", "navrh", "nabíd", "nabid", "řekl", "rekl", "zmíni", "zmini",
     "radil", "porad", "navrho", "říka", "rika", "sliby", "slíb", "slib"},
    #                                                     doporučit/navrhnout/slíbit
    {"film", "seri", "kino", "sledo", "kouka", "díval", "dival", "epizod",
     "pořad", "porad"},                                                  # filmy/TV
    {"kníh", "knih", "čet", "cet", "kapit", "autor", "romá", "roma",
     "povíd", "povid", "příbě", "pribe"},                             # knihy/čtení
    {"koup", "náku", "naku", "objed", "poříd", "porid", "sezn", "seznam"},
    #                                                          nákup/seznam/pořízení
    {"schůz", "schuz", "setk", "návště", "navste", "termí", "termi", "sraz",
     "domlu", "domluv", "sejde"},                        # schůzka/setkání/termín
    {"zdrav", "nemoc", "bolí", "boli", "lék", "lekar", "dokto", "cvič", "cvic"},
    #                                                              zdraví/lékař
    {"cest", "výlet", "vylet", "dovol", "prázd", "prazd", "hotel", "leten"},
    #                                                              cestování/výlet
    {"prác", "prac", "úkol", "ukol", "projekt", "termín", "termin", "šéf", "sef"},
    #                                                                   práce/úkol
    {"počít", "pocit", "kompu", "notebo", "mobil", "aplik", "program", "web",
     "software"},                                                     # technika/PC
]
_DNY = {"pondělí": 0, "pondeli": 0, "úterý": 1, "utery": 1, "středa": 2,
        "streda": 2, "středu": 2, "stredu": 2, "čtvrtek": 3, "ctvrtek": 3,
        "pátek": 4, "patek": 4, "pátkem": 4, "sobota": 5, "sobotu": 5,
        "sobotě": 5, "sobote": 5, "neděle": 6, "nedele": 6, "neděli": 6,
        "nedeli": 6}


def _conv_keywords(query: str) -> list:
    """Obsahová slova z dotazu (bez stopwords, ≥4 znaky), stemovaná na prefix
    kvůli českému skloňování (sobotní→sobot, oběd→oběd, recept→recep)."""
    kws = []
    for w in re.findall(r"[a-zA-Zá-žÁ-Ž]{4,}", (query or "").lower()):
        if w in _CONV_STOP:
            continue
        stem = w[:5]
        if stem not in kws:
            kws.append(stem)
    return kws


def _kw_groups(query: str) -> list:
    """Query klíčová slova → shluky pro shodu (zpráva odpovídá shluku, obsahuje-li
    kterýkoli stem). Rozšíří o synonyma."""
    groups = []
    for kw in _conv_keywords(query):
        grp = {kw}
        for cl in _SYN_CLUSTERS:
            if kw in cl:
                grp |= cl
        groups.append(grp)
    return groups


# ── HANS_RECALL_DATE_V3 — časová reference vč. KONKRÉTNÍHO data ──────────────
# Doložený případ (13.7.): „dokážeš vytáhnout vzpomínku z 27. dubna 2026?" →
# Hans abstinoval, protože recall uměl jen „včera/v pátek". Tady se rozpozná
# i datum slovem („27. dubna 2026"), číslem („27.4.", „27. 4. 2026") a týdenní
# rozsahy („minulý týden"). Deterministicky, žádný LLM.

_MES_WORD = {}
for _i, _names in enumerate((
        ("ledna", "leden", "lednu"),
        ("února", "unora", "únor", "unor", "únoru", "unoru"),
        ("března", "brezna", "březen", "brezen", "březnu", "breznu"),
        ("dubna", "duben", "dubnu"),
        ("května", "kvetna", "květen", "kveten", "květnu", "kvetnu"),
        ("června", "cervna", "červen", "cerven", "červnu", "cervnu"),
        ("července", "cervence", "červenec", "cervenec", "červenci", "cervenci"),
        ("srpna", "srpen", "srpnu"),
        ("září", "zari"),
        ("října", "rijna", "říjen", "rijen", "říjnu", "rijnu"),
        ("listopadu", "listopad"),
        ("prosince", "prosinec", "prosinci"),
), start=1):
    for _n in _names:
        _MES_WORD[_n] = _i


def _day_bounds(d) -> tuple:
    start = datetime(d.year, d.month, d.day).timestamp()
    return (start, start + 86400)


def _pick_year(day: int, month: int, year: Optional[int], dt_now) -> Optional[int]:
    """Rok z dotazu, nebo NEJBLIŽŠÍ MINULÝ výskyt toho dne (27. dubna v červenci
    2026 → 2026; 27. prosince v červenci 2026 → 2025)."""
    if year:
        return year
    for y in (dt_now.year, dt_now.year - 1):
        try:
            if datetime(y, month, day).date() <= dt_now.date():
                return y
        except ValueError:
            return None
    return None


def resolve_time_range(query: str, now: Optional[float] = None):
    """Časová reference v dotazu → (start_ts, end_ts, popisek), jinak None.
    Umí: dnes / včera / předevčírem / den v týdnu (nejbližší minulý) /
    konkrétní datum slovem i číslem / tento a minulý týden."""
    now = now or time.time()
    q = (query or "").lower()
    dt_now = datetime.fromtimestamp(now)

    # 1) konkrétní datum slovem: „27. dubna 2026", „27 dubna"
    m = re.search(r"\b(\d{1,2})\.?\s*([a-zá-ž]{4,10})(?:\s+(\d{4}))?", q)
    if m and m.group(2) in _MES_WORD:
        day, mon = int(m.group(1)), _MES_WORD[m.group(2)]
        year = _pick_year(day, mon, int(m.group(3)) if m.group(3) else None, dt_now)
        if year:
            try:
                d = datetime(year, mon, day)
                lo, hi = _day_bounds(d)
                return (lo, hi, _cz_when(lo, with_weekday=True).split(" v ")[0])
            except ValueError:
                pass

    # 2) konkrétní datum číslem: „27.4.2026", „27. 4.", „27/4"
    # (obě tečky povinné — jinak by „verze 2.5" byla 2. května)
    m = (re.search(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.(?:\s*(\d{4}))?", q)
         or re.search(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{4}))?", q))
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= mon <= 12:
            year = _pick_year(day, mon,
                              int(m.group(3)) if m.group(3) else None, dt_now)
            if year:
                try:
                    d = datetime(year, mon, day)
                    lo, hi = _day_bounds(d)
                    return (lo, hi, _cz_when(lo, with_weekday=True).split(" v ")[0])
                except ValueError:
                    pass

    # 3) týdenní rozsahy
    if re.search(r"\bminul\w*\s+t[ýy]d", q):
        mon_this = dt_now.date().toordinal() - dt_now.weekday()
        lo = datetime.fromordinal(mon_this - 7).timestamp()
        return (lo, lo + 7 * 86400, "minulý týden")
    if re.search(r"\bt[ée]nhle\s+t[ýy]d|\btento\s+t[ýy]d|\btento\s+t[ýy]den", q):
        mon_this = dt_now.date().toordinal() - dt_now.weekday()
        lo = datetime.fromordinal(mon_this).timestamp()
        return (lo, now, "tento týden")

    # 4) relativní dny
    target = None
    if re.search(r"\bp[řr]edev[čc][íi]r", q):
        target = dt_now.date().toordinal() - 2
    elif re.search(r"\bv[čc]er", q):
        target = dt_now.date().toordinal() - 1
    elif re.search(r"\bdnes|\bdneska", q):
        target = dt_now.date().toordinal()
    else:
        for name, wd in _DNY.items():
            if re.search(r"\b" + name + r"\b", q):
                # nejbližší minulý (nebo dnešní) výskyt daného dne v týdnu
                back = (dt_now.weekday() - wd) % 7
                target = dt_now.date().toordinal() - back
                break
    if target is None:
        return None
    lo, hi = _day_bounds(datetime.fromordinal(target))
    return (lo, hi, _cz_when(lo, with_weekday=True).split(" v ")[0])


def _resolve_day_reference(query: str, now: float):
    """Zpětně kompatibilní obal (start_ts, end_ts) — bez popisku."""
    r = resolve_time_range(query, now)
    return (r[0], r[1]) if r else None


def conversation_recall(db_path: str, query: str, days: int = 30,
                        min_age_hours: float = 0.3, limit: int = 4,
                        person: Optional[str] = None) -> list:
    """Recall PŘEDCHOZÍHO rozhovoru (deterministicky). Když dotaz nese ČASOVOU
    referenci („v pátek"/„včera"), zúží se na TEN den (nezávisle na doslovných
    slovech) a v něm seřadí dle shody (se synonymy). Jinak čistě dle klíčových
    slov přes celé okno. Vynechá právě proběhlou výměnu (min_age_hours). Vrací
    [(kdy, note)] nebo []."""
    now = time.time()
    groups = _kw_groups(query)
    day_ref = _resolve_day_reference(query, now)
    # Cizí rozhovory se nevynášejí — každý dostane jen své (title = osoba).
    who = (person or "").strip().lower()
    p_sql = " AND lower(title)=?" if who else ""
    try:
        conn = _ro(db_path)
        if day_ref:
            lo, hi = day_ref
            hi = min(hi, now - min_age_hours * 3600)
            args = [lo, hi] + ([who] if who else [])
            rows = conn.execute(
                "SELECT ts, note FROM diary WHERE event_type='human_chat' AND "
                "ts>=? AND ts<=?" + p_sql + " ORDER BY ts DESC",
                tuple(args)).fetchall()
        else:
            args = [now - days * 86400, now - min_age_hours * 3600] + (
                [who] if who else [])
            rows = conn.execute(
                "SELECT ts, note FROM diary WHERE event_type='human_chat' AND "
                "ts>=? AND ts<=?" + p_sql + " ORDER BY ts DESC",
                tuple(args)).fetchall()
        conn.close()
    except Exception:
        return []

    def _score(note):
        low = (note or "").lower()
        return sum(1 for g in groups if any(s in low for s in g))

    if day_ref:
        # den je kotva → vezmi všechny, seřaď dle shody (i skóre 0 projde, ale
        # nejrelevantnější první); prázdný den → nic
        scored = [( _score(n), ts, n) for ts, n in rows if (n or "").strip()]
        scored.sort(key=lambda x: (-x[0], -x[1]))
    else:
        if not groups:
            return []
        need = max(1, (len(groups) + 1) // 2)
        scored = [(s, ts, n) for ts, n in rows
                  for s in (_score(n),) if s >= need]
        scored.sort(key=lambda x: (-x[0], -x[1]))
    return [(_cz_when(ts), (note or "").strip()) for _s, ts, note in scored[:limit]]


# ── HANS_CHAT_SUMMARY_V1 — „o čem jsme se bavili (v pátek / 27. dubna)" ──────
# Sumář rozhovorů TÉ OSOBY, co se ptá (cizí chaty se nezobrazí). Deterministicky
# z human_chat — repliky jdou VERBATIM, nic se nedomýšlí. Když ten den chat není,
# poctivě to přizná a nabídne, co si ten den zapsal jinak (deník).

# Vjemový firehose — do „co jsem si ten den zapsal" nepatří (šum).
_DIARY_NOISE = {
    "person_seen", "teddy_arrived", "teddy_gone", "idle_start", "idle_end",
    "brain_down", "brain_still_down", "brain_up", "movie_browsed",
    "kodi_playing", "dialog_reflection", "teddy_dialog", "game_mode",
    "capability_gained", "morning_health", "heartbeat",
}


def _split_exchange(note: str, person: str) -> tuple:
    """'jmeno: dotaz\\nHans: odpověď' → (dotaz, odpověď). Robustní vůči tvaru."""
    txt = (note or "").strip()
    m = re.split(r"\n(?=\w+:)", txt, maxsplit=1)
    user = m[0].strip()
    reply = m[1].strip() if len(m) > 1 else ""
    user = re.sub(r"^\s*%s\s*:\s*" % re.escape(person or ""), "", user,
                  flags=re.IGNORECASE)
    user = re.sub(r"^\s*\w+\s*:\s*", "", user) if ":" in user[:20] else user
    reply = re.sub(r"^\s*\w+\s*:\s*", "", reply)
    return (user.strip(), reply.strip())


def _day_notes(conn, lo: float, hi: float, limit: int = 4) -> list:
    """Co si Hans ten den zapsal (mimo vjemový šum) — pro poctivý fallback."""
    rows = conn.execute(
        "SELECT ts, event_type, title, "
        "substr(COALESCE(NULLIF(data,''),note),1,110) "
        "FROM diary WHERE ts>=? AND ts<? ORDER BY ts ASC", (lo, hi)).fetchall()
    out = []
    for ts, etype, title, snip in rows:
        if etype in _DIARY_NOISE:
            continue
        txt = (str(snip or "").strip() or str(title or "").strip())
        if not txt:
            continue
        out.append("– %s: %s" % (_cz_date(ts), txt))
        if len(out) >= limit:
            break
    return out


_TOPIC_SUM_SYSTEM = (
    "Jsi pečlivý archivář. Dostaneš DOSLOVNÝ přepis replik jednoho člověka "
    "z rozhovorů s Hansem. Napiš JEDNU až DVĚ věty česky o tom, o čem se "
    "bavili — vyjmenuj hlavní témata. Piš ve tvaru „Bavili jsme se hlavně "
    "o …“. Uveď POUZE témata, která se v přepisu skutečně objevují; NIC "
    "nedomýšlej, nehodnoť, nepřidávej rady."
)


def _summarize_topics(config: Optional[dict], lines: list) -> Optional[str]:
    """Kondenzace SKUTEČNÝCH replik na témata (materiál injektovaný → nízké
    riziko konfabulace). LLM dole / herní mód → None a volající vypíše seznam."""
    if not config or not lines:
        return None
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return None
    model = ((config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    body = "\n".join("- %s" % l for l in lines[:80])
    try:
        out = ollama_generate(
            model, "Repliky:\n%s\n\nO čem se bavili?" % body,
            system=_TOPIC_SUM_SYSTEM, config=config, timeout=90, keep_alive=0,
            options={"temperature": 0.2, "num_predict": 120, "num_ctx": 8192})
    except Exception as e:
        _log.warning("_summarize_topics selhal: %s", e)
        return None
    out = (out or "").strip()
    return out.split("\n")[0].strip() if out else None


# ── HANS_CHAT_TOPIC_RECALL_V1 — „připomeň rozhovor o Maradonovi" ─────────────

_TOPIC_ASK = re.compile(
    r"(?:rozhovor\w*|bavil[iy]\s+jsme\s+se|mluvil[iy]\s+jsme|"
    r"[řr]e[čc]\w*|[čc]em|detail\w*|recept\w*|postup\w*|z[áa]znam\w*|"
    r"z[áa]pis\w*|napsal|psal|poslal|navrhl|doporu[čc]il|[řr][íi]kal)"
    r"\s+o\s+(.{2,60}?)\s*[\?\.!]?$", re.IGNORECASE)

# Když dotaz téma nepojmenuje („ukaž mi ten recept"), je tématem sama věc.
_TOPIC_BARE = re.compile(r"\b(recept\w*|postup\w*|n[áa]vrh\w*)\b",
                         re.IGNORECASE)

# Ocas dotazu, který NENÍ součástí tématu: datum, čas, „na sobotu", „z pátku"
_TOPIC_TAIL = re.compile(
    r"\s*(?:\bna\b|\bz\b|\bze\b|\bv\b|\bve\b)?\s*"
    r"(?:\d{1,2}\s*[./]\s*\d{1,2}(?:\s*[./]\s*\d{2,4})?|\d{1,2}:\d{2}|"
    r"pond[ěe]l\w*|[úu]ter\w*|st[řr]ed\w*|[čc]tvrt\w*|p[áa]t\w*|sobot\w*|"
    r"ned[ěe]l\w*|v[čc]er\w*|dnes\w*|minul\w+\s+t[ýy]dn\w*)\s*", re.IGNORECASE)


def _extract_conv_topic(query: str) -> str:
    """Téma z dotazu na konkrétní rozhovor („připomeň rozhovor o Maradonovi"
    → „Maradonovi"; „pošli detail o rychlem obedu na sobotu 10.7. 14:18"
    → „rychlem obedu"). '' když dotaz téma nemá (obecné „o čem jsme se bavili").
    Časové údaje se odřežou — nejsou téma, jen zpřesnění."""
    q = (query or "").strip()
    if re.search(r"o\s+[čc]em\b", q, re.IGNORECASE):
        return ""    # „o čem jsme se bavili" = obecný sumář, ne téma
    m = _TOPIC_ASK.search(q)
    if not m:
        b = _TOPIC_BARE.search(q)
        return b.group(1) if b else ""
    topic = m.group(1).strip()
    # odřež datum/čas/den z konce i zbytky předložek
    prev = None
    while topic and topic != prev:
        prev = topic
        topic = _TOPIC_TAIL.sub(" ", topic).strip()
        topic = re.sub(r"\s+(na|z|ze|v|ve|o)$", "", topic).strip()
    topic = re.sub(r"^(ten|ta|to|toho|tom)\s+", "", topic,
                   flags=re.IGNORECASE).strip()
    return "" if len(topic) < 3 else topic


# Výměny, které do vybavení NEPATŘÍ — nejsou zdroj, jen ozvěna:
#  (a) Hans se v nich k tématu nevyjádřil (abstinence),
#  (b) uživatel si v nich téma jen VYŽÁDAL ZPĚT (Hansovo převyprávění — právě
#      tam si domýšlí, viz doložený koriandr 13.7.),
#  (c) zdvořilostní vata („ok, děkuji“).
_NOISE_REPLY = re.compile(
    r"nem[áa]m\s+(spolehliv|ov[ěe][řr]en|[žz][áa]dn)\w*\s+z[áa]znam|"
    r"nebudu\s+si\s+(nic\s+)?vym[ýy][šs]let|nerad\s+bych\s+si\s+dom[ýy][šs]lel|"
    r"si\s+t[íi]m\s+nejsem\s+jist", re.IGNORECASE)
_NOISE_USER = re.compile(
    r"^\s*(ok|jo|jj|dobr[ée]|super|d[íi]k\w*|d[ěe]kuj\w*|to\s+sta[čc][íi]|"
    r"sta[čc][íi]\s+to)\b|"
    r"(p[řr]ipome[ňnt]|po[šs]l\w*\s+detail|detail\s+o\s|zopakuj|"
    r"co\s+jsme\s+se\s+bavil|o\s+[čc]em\s+jsme)", re.IGNORECASE)


def _is_echo_exchange(user: str, reply: str) -> bool:
    """Ozvěna, ne zdroj — vyžádané převyprávění / abstinence / zdvořilost."""
    return bool(_NOISE_REPLY.search(reply or "")
                or _NOISE_USER.search((user or "").strip()))


def topic_conversation(db_path: str, person: Optional[str], topic: str,
                       limit: int = 3, days: int = 120) -> str:
    """Doslovné vybavení konkrétního rozhovoru na dané téma (obě strany).
    Deterministické hledání v human_chat té osoby. Nic nenalezeno → přizná to."""
    who = (person or "").strip().lower()
    # Skóruj podle VŠECH obsahových slov tématu, ne jen podle nejdelšího —
    # „rychlem obedu“ musí trefit výměnu, kde je OBOJÍ (jinak se chytne
    # náhodná zmínka „rychle“). Diakritika folded (uživatel píše bez háčků),
    # koncovky uříznuté (skloňování: „obedu“ → „obed“).
    toks = [w for w in re.split(r"[^\wá-žÁ-Ž]+", topic.lower()) if len(w) >= 4]
    pref = [_fold(w)[: max(4, len(w) - 2)] for w in toks]
    if not who or not pref:
        return ""
    conn = None
    try:
        conn = _ro(db_path)
        now = time.time()
        rows = conn.execute(
            "SELECT ts, note FROM diary WHERE event_type='human_chat' "
            "AND lower(title)=? AND ts>=? ORDER BY ts DESC",
            (who, now - days * 86400)).fetchall()
        scored = []
        for ts, n in rows:
            fn = _fold(n or "").lower()
            score = sum(1 for p in pref if p in fn)
            if not score:
                continue
            u, r = _split_exchange(n, who)
            if _is_echo_exchange(u, r):
                continue    # ozvěna (vyžádané převyprávění / abstinence)
            scored.append((score, ts, n))
        best = max((s for s, _t, _n in scored), default=0)
        hits = [(ts, n) for s, ts, n in scored if s == best]
        if not hits:
            return ("O „%s“ nemám s vámi žádný rozhovor zapsaný. Nebudu si ho "
                    "vymýšlet." % topic)
        found = len(hits)
        # PŮVODNÍ výměna má přednost před pozdějšími (v nich už Hans o tématu
        # jen mluví — a případně si domýšlí; origin nese skutečný obsah).
        hits = sorted(hits, key=lambda x: x[0])[:limit]
        # Přiber bezprostřední POKRAČOVÁNÍ (do 10 min) — „posli postup přípravy“
        # je samostatná výměna, ale nese vlastní jádro odpovědi (celý recept).
        by_ts = dict(hits)
        for ts0, _n0 in list(hits):
            for ts, n in rows:
                if ts0 < ts <= ts0 + 600 and ts not in by_ts:
                    u2, r2 = _split_exchange(n, who)
                    if not _is_echo_exchange(u2, r2):
                        by_ts[ts] = n
        hits = sorted(by_ts.items())[: limit + 2]
        parts = []
        for ts, note in hits:
            u, r = _split_exchange(note, who)
            # Odpověď dáváme CELOU (recept/postup se nesmí utnout uprostřed) —
            # je to doslovný zápis, ne převyprávění.
            blk = "[%s]\nVy: „%s“" % (_cz_when(ts), u[:300])
            if r:
                blk += "\nJá: „%s“" % (r if len(r) <= 1400
                                       else r[:1400] + " …(zkráceno)")
            parts.append(blk)
        out = ("Tady je, co o „%s“ máme v deníku doslova zapsáno:\n\n%s"
               % (topic, "\n\n".join(parts)))
        if found > len(hits):
            out += "\n\n(K tomu tématu mám ještě %d starších výměn.)" % (
                found - len(hits))
        # Navázání: uživatel může rovnou upřesnit („zjisti o tom víc“).
        out += ("\n\nChcete-li, mohu si o tom zjistit víc — stačí říct "
                "„zjisti víc o %s“." % topic)
        return out
    except Exception as e:
        _log.warning("topic_conversation selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def chat_summary(db_path: str, person: Optional[str], query: str = "",
                 now: Optional[float] = None, max_lines: int = 12,
                 config: Optional[dict] = None,
                 detail_max: int = 6) -> str:
    """Sumář toho, o čem se daná osoba s Hansem bavila. S časovou referencí
    v dotazu („v pátek", „27. dubna 2026", „minulý týden") → jen to období;
    bez ní → poslední den, kdy spolu mluvili. Žádný LLM, nulová konfabulace."""
    now = now or time.time()
    rng = resolve_time_range(query, now)
    conn = None
    try:
        conn = _ro(db_path)
        who = (person or "").strip().lower()
        if not who:
            return ("Nevím jistě, s kým mluvím — sumář rozhovorů proto "
                    "nesestavím.")

        if rng:
            lo, hi, label = rng
        else:
            row = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type='human_chat' "
                "AND lower(title)=?", (who,)).fetchone()
            if not row or not row[0]:
                return ("V deníku nemám zapsaný žádný náš rozhovor. "
                        "Nebudu si nic vymýšlet.")
            lo, hi = _day_bounds(datetime.fromtimestamp(row[0]))
            label = _cz_when(lo, with_weekday=True).split(" v ")[0]

        rows = conn.execute(
            "SELECT ts, note FROM diary WHERE event_type='human_chat' "
            "AND lower(title)=? AND ts>=? AND ts<? ORDER BY ts ASC",
            (who, lo, hi)).fetchall()

        if not rows:
            extra = _day_notes(conn, lo, hi)
            out = ("%s jsme spolu podle deníku vůbec nemluvili — žádný náš "
                   "rozhovor z té doby zapsaný nemám a nebudu si ho vymýšlet."
                   % label.capitalize())
            if extra:
                out += "\nZapsal jsem si tehdy jen tohle:\n" + "\n".join(extra)
            return out

        parsed = []
        for ts, note in rows:
            u, r = _split_exchange(note, who)
            if u:
                parsed.append((ts, u.replace("\n", " ").strip(),
                               (r or "").replace("\n", " ").strip()))
        if not parsed:
            return ("%s mám sice rozhovor zapsaný, ale bez čitelného obsahu."
                    % label.capitalize())

        # Výpis je DOSLOVNÝ (obě strany z deníku) — nic se negeneruje znovu.
        multi_day = len({_cz_date(ts) for ts, _u, _r in parsed}) > 1
        lines = []
        for ts, u, r in parsed:
            when = datetime.fromtimestamp(ts).strftime("%H:%M")
            prefix = ("%s %s" % (_cz_date(ts), when)) if multi_day else when
            blk = "– %s\n   Vy: „%s“" % (prefix, u[:160])
            if r:
                blk += "\n   Já: „%s“" % r[:220]
            lines.append(blk)

        total = len(lines)
        head = ("%s jsme spolu vedli %d %s."
                % (label.capitalize(), total,
                   "výměnu" if total == 1 else
                   ("výměny" if total < 5 else "výměn")))

        # Delší období → nejdřív TÉMATA (kondenzace skutečných replik),
        # podrobnosti až na vyžádání („připomeň rozhovor o X“).
        if total > detail_max:
            topics = _summarize_topics(config, [u for _ts, u, _r in parsed])
            if topics:
                return ("%s %s\nChcete-li si některý připomenout doslova, "
                        "řekněte třeba „připomeň rozhovor o …“ — vypíšu ho, "
                        "jak je zapsán."
                        % (head, topics))

        shown = lines[:max_lines]
        out = (head + " Tady je doslovný zápis z deníku:\n" + "\n".join(shown))
        if total > max_lines:
            out += ("\n(… a dalších %d výměn. Konkrétní si vyžádejte: "
                    "„připomeň rozhovor o …“.)" % (total - max_lines))
        return out
    except Exception as e:
        _log.warning("chat_summary selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def is_recall_query(text: str) -> bool:
    """Ptá se uživatel na dřívější rozhovor? (pamatuješ / mluvili jsme / říkal jsi)"""
    # Tolerantní k i/y a překlepům („bavily", „pripomenou") — striktní vzor by
    # dotaz pustil do volné generace a Hans by si rozhovor VYMYSLEL.
    t = (text or "").lower()
    return bool(re.search(
        r"pamatuj|vzpom[íi]n|p[řr]ipome[ňnt]|mluvil[iy]\s+jsme|"
        r"bavil[iy]\s+jsme|[řr][íi]kal\s+jsi|co\s+jsme|jsme\s+se\s+bavil[iy]|"
        r"zm[íi]nil\s+jsi|navrh\w*\s+jsi|[řr]ekl\s+jsi", t))


# ── HANS_SOURCE_QUERY_V1 — dotaz na zdroj Hansova tvrzení ────────────────────

def is_source_query(text: str) -> bool:
    """Ptá se uživatel „odkud to víš / kde jsi to četl / máš k tomu zdroj / odkaz"?
    Tolerantní k i/y a překlepům + bez diakritiky. NEsmí trefit obecnou zvědavost
    („co je zdroj X"). Rozšířeno o reálné formulace uživatele („mas odkaz na
    clanek, kde se o tom pisr?" — chat 17.7. 11:59)."""
    t = (text or "").lower()
    # Klasické + reálné formulace dotazu na provenienci Hansova tvrzení.
    pats = (
        # „odkud to (víš/máš)"
        r"\bodkud\s+(to|tohle|to\s+m[áa][šs]|to\s+v[íi][šs])",
        # „kde jsi to (četl/našel/vzal/slyšel)" i bez „jsi"
        r"\bkde\s+(jsi|si)?\s*(to|tohle|toto)?\s*(?:vzal|na[šs]el|na[čc]etl|[čc]etl|sly[šs]el|na[šs]el)",
        # „kde to najdu / kde se to dočtu / kde se o tom píše"
        r"\bkde\s+(to|se\s+(to|o\s+tom))\s+(?:najdu|na[čc]tu|do[čc]t[eě][šs]?|d[ao]ct[eěií]?|p[íi][šs]e)",
        # „na základě čeho / z čeho to víš/máš / podle čeho"
        r"\bna\s+z[áa]klad[ěe]\s+[čc]eho",
        r"\bz\s+[čc]eho\s+(to|tohle)?\s*(v[íi][šs]|m[áa][šs])",
        r"\bpodle\s+[čc]eho",
        # „máš (k tomu) zdroj / odkaz / článek / důkaz"
        r"\bm[áa][šs]\s+(k\s+tomu\s+)?(zdroj|odkaz|[čc]l[áa]nek|d[ůu]kaz|citaci|pramen)",
        r"\bjak[ýy]\s+(m[áa][šs])?\s*(zdroj|odkaz|pramen)",
        # „dej mi / ukaž mi / pošli mi (odkaz / zdroj / článek)"
        r"\b(dej|ukaz|uk[áa][žz]|po[šs]li|hoď|hoď mi)\s+(mi\s+)?(odkaz|zdroj|[čc]l[áa]nek|pramen)",
        # „(můžeš / můžeš mi) ukázat/dát/poslat (zdroj/odkaz/článek)"
        r"\b(m[ůu][žz]e[šs]|dok[áa][žz]e[šs])\s+(m[eě]?\s+|mi\s+)?(uk[áa]zat|d[áa]t|posl[aá]t|pou[žz][ií]t)\s+.*?(zdroj|odkaz|[čc]l[áa]nek|pramen)",
        # „uveď/uved zdroj"
        r"\buve[ďdt]\s+(zdroj|odkaz|pramen)",
        # „proč si to myslíš"
        r"\bpro[čc]\s+si\s+(to|tohle)?\s*mysl[íi][šs]",
        # samotný „důkaz?"
        r"\bd[ůu]kaz\b",
        # samostatné „zdroj?" / „odkaz." / „a článek?" — krátký standalone dotaz
        r"^\s*(a\s+)?(zdroj|odkaz|[čc]l[áa]nek|pramen)(\s+pros[íi]m)?\s*[\.!\?]*\s*$",
        # HANS_SOURCE_QUERY_V2 (5.8.) — doplněno po měření na 305 reálných
        # zprávách: vzory chytily 7 dotazů, ale ze 7 ZKUŠEBNÍCH formulací
        # propadly 4. Doložený reálný miss: „muze me ukazat zdroj odkud jsi
        # cerpal?" (chat 17.7.) — obsahuje „zdroj" i „odkud", ale ani jeden
        # vzor nesedl, protože „odkud" nebylo následováno „to".
        # ⚠️ Zkoušeno nahradit mini modelem na Pi (bod 2 z nápadu) — ZAMÍTNUTO:
        # 9/15, propadly přesně tyhle formulace a u „odkud jsi čerpal?" model
        # místo klasifikace ZAČAL ODPOVÍDAT („z encyklopedie"). Regex je tu
        # měřeně lepší; detail v backlogu.
        r"\b[čc]erpal",                      # „odkud/z čeho jsi čerpal"
        r"\bodkud\s+(jsi|si|m[áa][šs]|jste)",  # „odkud jsi to vzal/čerpal"
        r"\bz\s+[čc]eho\s+(jsi|si)\s+(to\s+)?(vzal|m[áa][šs]|[čc]erpal)",
        r"\bkde\s+(ses|jsi\s+se)\s+(to\s+)?dozv[ěe]d[ěe]l",
        r"\bm[ůu][žz]e[šs]\s+(to\s+)?dolo[žz]it",
        r"\bjak\s+v[íi][šs]\s*,?\s*[žz]e",
        r"\bm[áa][šs]\s+(na\s+to\s+)?(n[ěe]jak[ýy]\s+)?(zdroj|odkaz|pramen)",
    )
    return any(re.search(p, t) for p in pats)


def _find_entity_in_text(db_path: str, text: str) -> Optional[tuple]:
    """Najdi entity.name, která je zmíněna v textu (case insensitive, whole word).
    Vrací (name, source_url) nebo None. Preferuje delší jméno (specifičtější)."""
    if not text:
        return None
    conn = None
    try:
        conn = _ro(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, source FROM entities WHERE source IS NOT NULL "
            "AND source != '' AND length(name) >= 4").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()

    t_lower = text.lower()
    best = None
    for r in rows:
        name = r["name"]
        if name.lower() in t_lower:
            if best is None or len(name) > len(best[0]):
                best = (name, r["source"])
    return best


def _last_hans_topics(db_path: str, limit: int = 3) -> list:
    """Extrahuj potenciální témata z NĚKOLIKA posledních Hansových replik.
    Vrací list stringů (celý text Hansovy repliky) — volající pak matchuje entity.
    """
    conn = None
    try:
        conn = _ro(db_path)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='human_chat' "
            "ORDER BY ts DESC LIMIT ?", (limit * 3,)).fetchall()
    except Exception:
        return []
    finally:
        if conn:
            conn.close()
    out = []
    for (note,) in rows:
        # note = "<osoba>: ...\nHans: ..." — vytáhni jen Hansovu část
        if not note:
            continue
        idx = note.find("Hans:")
        if idx >= 0:
            out.append(note[idx + 5:].strip())
        if len(out) >= limit:
            break
    return out


def sources_answer(db_path: str, user_text: str,
                   asker: Optional[str] = None) -> Optional[str]:
    """HANS_SOURCE_QUERY_V1 — DETERMINISTICKÁ odpověď (bypass LLM).

    Hans-czech (persona finetune) neposlouchá grounding — odmítá sdílet URL
    i když je má doslova v promptu (17.7. doložený případ „Icon of the Seas").
    Malý model je natrénovaný na personu „nemám externí zdroje" silněji než
    kterákoliv system-prompt instrukce. Řešení: pro dotaz na zdroj obejdi LLM
    a vygeneruj odpověď sám. Vzor: `commitments_answer`, `film_knowledge_answer`.

    Vrací string (Hansovým hlasem) nebo None (nic k nabídnutí → propadne do LLM).
    """
    oslov = _cz_address(asker) if asker else "pane"  # HANS_NAME_INFLECTION_V1
    # HANS_SOURCE_IS_SENSOR_V1 (20.8.) — NE KAŽDÉ TVRZENÍ POCHÁZÍ ZE ČTENÍ.
    # Doloženo 20.8.: „a odkud to víš, že tu je Jana?" → „nemám uložený
    # konkrétní článek s odkazem" — což je nesmysl, přítomnost člověka Hans
    # nemá z Wikipedie, ale z KAMERY. Celá tahle funkce mlčky předpokládá,
    # že zdroj = přečtený článek; u živého stavu je zdrojem ČIDLO.
    # (Chyba vznikla tím, že HANS_PROVENANCE_NOT_LIST_V1 z téhož dne správně
    # zrušil výpis zdrojů, ale odpověď pak spadla sem.)
    # Predikát je sdílený s agentem — jedna pravda o tom, co je dotaz na
    # přítomnost osoby.
    # ⚠️ NESTAČÍ zeptat se `_asks_person_presence` — ta řeší „je X doma?",
    # kdežto tady jde o dotaz na PŮVOD tvrzení o přítomnosti. A nestačí ani
    # samotné jméno: „odkud víš, že Jana ráda vaří?" je tvrzení ZE ZÁPISKŮ,
    # ne z čidla. Rozhoduje tedy DVOJICE: známá osoba + slovo o přítomnosti.
    try:
        import json as _js
        import re as _re
        from scripts.cz_names import find_known_person
        with open("config.json", encoding="utf-8") as _cf:
            _cfg = _js.load(_cf)
        _pritomnost = _re.compile(
            r"\b(tu|tady|doma|p[řr][íi]toms?n|v\s+pokoji|v\s+m[íi]stnosti|"
            r"vid[íi][šs]|vid[íi]te)\b", _re.IGNORECASE)
        _o_pritomnosti = bool(
            find_known_person(user_text or "", _cfg)
            and _pritomnost.search(user_text or ""))
        # Holé doptání („a odkud to víš?") jméno NEOBSAHUJE — předmětem je
        # POSLEDNÍ Hansova replika. Když ta hlásila, koho vidí, je zdrojem
        # kamera. Funkce si poslední repliky stejně tahá (fallback níž),
        # tak se použije týž zdroj místo nového mechanismu.
        # ⚠️ Anaforická větev jen u SKUTEČNĚ HOLÉHO doptání. Regresní sada
        # chytila, že jinak přebije i otázku s vlastním předmětem: „odkud víš,
        # že Jana ráda vaří?" dostalo odpověď „vidím to kamerou“ jen proto, že
        # poslední replika náhodou hlásila, koho Hans vidí.
        _hole = (len((user_text or "").split()) <= 6
                 and _re.search(r"\bto\b", user_text or "", _re.IGNORECASE))
        if not _o_pritomnosti and _hole:
            _hlaseni = _re.compile(
                r"(vid[íi]m\s+(tu|tady)|nikoho\s+nevid[íi]m|"
                r"zahl[ée]dl\s+jsem|je\s+doma|jsou\s+doma)", _re.IGNORECASE)
            for _r in _last_hans_topics(db_path, limit=2):
                if _hlaseni.search(_r or ""):
                    _o_pritomnosti = True
                    break
        if _o_pritomnosti:
            return ("To nemám ze zápisků, %s — vidím to kamerou. "
                    "Hlásím, koho právě rozpoznávám v místnosti." % oslov)
    except Exception:
        pass


    hit = _find_entity_in_text(db_path, user_text)
    if not hit:
        # fallback z posledních Hansových replik (user řekl jen „a odkud to víš")
        for hans_reply in _last_hans_topics(db_path, limit=3):
            hit = _find_entity_in_text(db_path, hans_reply)
            if hit:
                break

    if hit:
        name, url = hit
        return ("Ano, %s. O tématu '%s' jsem se dočetl na Wikipedii. "
                "Zde je odkaz: %s" % (oslov, name, url))

    # nic konkrétního — poctivé přiznání (bez konfabulace)
    return ("K tomu, o čem jsme mluvili, nemám v paměti uložený konkrétní "
            "článek s odkazem, %s. Zůstává mi jen obecná znalost, kterou "
            "jsem si osvojil — konkrétní zdroj Vám k tomu nabídnout nemohu, "
            "nechci si nic vymýšlet." % oslov)


def sources_reply(db_path: str, user_text: str = "", limit: int = 5) -> str:
    """HANS_SOURCE_QUERY_V1 — grounding blok pro dotaz „odkud to víš".

    STRATEGIE (17.7. — přepracováno na FAKTA, ne instrukce; malý model neuměl
    dvoustupňovou inferenci „najdi téma v seznamu → vytáhni URL"):
      1. Zkus najít KONKRÉTNÍ entitu zmíněnou v user promptu → hotová URL.
      2. Fallback na entitu z předchozí Hansovy repliky (dohledá téma o kterém
         právě mluvil, i když user řekne jen „a odkud to víš").
      3. Když nic nenajde → čestně přiznat obecnou znalost.
    """
    # 1) entita v samotném dotazu ("ukaž mi zdroj o Icon of the Seas")
    hit = _find_entity_in_text(db_path, user_text)
    if not hit:
        # 2) entita v Hansově předchozí odpovědi (user: „a odkud to víš?")
        for hans_reply in _last_hans_topics(db_path, limit=3):
            hit = _find_entity_in_text(db_path, hans_reply)
            if hit:
                break

    if hit:
        name, url = hit
        return (
            "\n\nUZIVATEL SE PTA NA ZDROJ. Toto jsou TVA FAKTA (ne instrukce, "
            "ne 'externi zdroje' - jsou to zaznamy z tve pameti):\n\n"
            "O tematu \"%s\" mas v pameti clanek na URL: %s\n\n"
            "Odpovez uzivateli PRESNE tuto URL. Priklad odpovedi: 'Cetl jsem "
            "o tom na Wikipedii. Odkaz: %s'. NEtvrd 'nemam pristup k externim "
            "zdrojum' - mas ho tady, prave ted, v tomto promptu."
        ) % (name, url, url)

    # 3) nic konkrétního — poctivé přiznání
    return (
        "\n\nUZIVATEL SE PTA NA ZDROJ. K tematu, o kterem jste mluvili, "
        "NEMAS v pameti zadny konkretni ulozeny clanek s URL. Odpovez "
        "cestne: 'To vim z obecne znalosti, konkretni clanek jsem k tomu "
        "necetl'. NIKDY nevymyslej URL, nazvy clanku, ani citace.")


# ── první / nejstarší vzpomínka ──────────────────────────────────────────────

def first_memory_answer(db_path: str) -> str:
    """Nejstarší záznam v deníku — MIN(ts), deterministicky. Žádný LLM."""
    conn = None
    try:
        conn = _ro(db_path)
        row = conn.execute(
            "SELECT ts, event_type, title, note FROM diary "
            "ORDER BY ts ASC LIMIT 1").fetchone()
        if not row:
            return "Můj deník je zatím prázdný, pane — nemám žádné vzpomínky."
        total = conn.execute("SELECT COUNT(*) FROM diary").fetchone()[0]
        ts, etype, title, note = row
        when = _cz_when(ts)
        detail = ""
        if note:
            detail = f" — poznamenal jsem si tehdy: „{str(note).strip()[:120]}“"
        elif title:
            detail = f" — týkal se: {str(title).strip()[:80]}"
        return (f"Podíval jsem se do svého deníku, pane. Můj úplně nejstarší "
                f"záznam vznikl {when} (typ „{etype}“){detail}. Od té doby "
                f"mám zapsáno {total} záznamů. Nic staršího si nepamatuji — "
                f"dřívější vzpomínky nemám.")
    except Exception as e:
        _log.warning("first_memory_answer selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── co / kdy jsi četl ────────────────────────────────────────────────────────

# HANS_RECALL_NODIACRITICS_V1 (13.8.) — uživatel píše z telefonu BEZ DIAKRITIKY
# („cetl jsi o hradech?"). Vzor čekal jen „četl" → téma se nevytáhlo VŮBEC
# a Hans místo recallu vrátil „naposledy jsem četl…", ačkoli deník záznamy měl
# (690 řádků se Sherlockem, 2370 s hradem). Registrační `nl_patterns` u /cetl
# tolerantní verzi `[čc]etl` používají už dřív — tady se jen srovnává krok.
# HANS_READING_TOPIC_SENTENCE_BOUND_V1 — capture NESMÍ přejít přes hranici věty
# (?.!). Dřív `(.{2,60}?)...$` kotvené na konec spolklo u dvouvětného dotazu
# „Četl jsi něco zajímavého? Rád bych se o tom dozvěděl víc." celý druhý úsek →
# pahýlové „téma" a rozbité „tohle mám o ‚…zajímavého Rád bych…'". `[^?.!]`
# zastaví u prvního otazníku/tečky.
_TOPIC_PAT = re.compile(
    # HANS_SOURCES_TOPIC_V1 (21.8.) — přibyla rodina „čerpal … o X".
    # Bez ní se provenienční dotaz tvářil jako BEZ tématu a /zdroje vypsalo
    # poslední čtení — doloženo 20.8.: „odkud jsi čerpal informace
    # o normalizaci" → seznam dnešní četby, která s normalizací nesouvisí.
    # Změřeno na 1171 reálných větách: mění se 3, všechny jsou tenhle tvar.
    r"(?:[čc]etla?\s+(?:jsi|sis)?\s*(?:n[ěe]co\s+)?o|"
    r"kdy\s+(?:jsi|sis)\s+[čc]etla?\s+o?|"
    r"[čc]erpal\w*\s+(?:informace\s+|[úu]daje\s+|to\s+)?o|"
    r"[čc]etla?\s+jsi)\s+([^?.!]{2,60}?)\s*\??$",
    re.IGNORECASE,
)
_STOPWORDS = {"něco", "neco", "dnes", "dneska", "včera", "vcera", "naposledy",
              "nějakou", "nejakou", "knihu", "článek", "clanek", "si", "už",
              "uz", "vůbec", "vubec", "někdy", "nekdy", "ty",
              # hodnotící přídavná jména / plevel — samy o sobě nejsou téma
              # („četl jsi něco zajímavého?" nemá dát topic „zajímavého")
              "zajímavého", "zajimaveho", "zajímavé", "zajimave", "zajímavou",
              "zajimavou", "zajímavý", "zajimavy", "hezkého", "hezkeho",
              "pěkného", "pekneho", "dobrého", "dobreho", "nového", "noveho",
              "víc", "vic", "více", "vice", "rád", "rada", "bych", "dozvěděl",
              "dozvedel",
              # zájmena — nejsou téma; „četl jsi JI?" nesmí dát bogus topic „ji“
              # → falešné „o ‚ji' nemám záznam". Prázdné téma → radši nech projít.
              "ji", "ho", "je", "jej", "něj", "nej", "ni", "ně", "to", "tom",
              "toho", "této", "teto", "tuto", "tu", "ten", "tim", "tím"}


# Explicitní PŘEDMĚT „o knize/filmu/tématu X“ — má přednost před koncovým
# „…četl jsi JI?“ (jinak _TOPIC_PAT chytne zájmeno). Řeší compound dotaz
# „co víš o knize Sherlock Holmes? četl jsi ji?“.
_SUBJECT_PAT = re.compile(
    r"\bo\s+(?:knize|kn[íi][žz]ce|kn[íi]žk\w*|filmu|seri[áa]lu|po[řr]adu|"
    r"t[éeě]\s+knize|t[ée]matu|autorovi|spisovateli)\s+(.{2,50}?)\s*[?.!,]",
    re.IGNORECASE)


def _extract_topic(question: str) -> str:
    """Vytáhni téma z dotazu na čtení ('četl jsi o hradech?' → 'hradech').
    '' když dotaz téma nemá (obecné 'co jsi četl')."""
    q = (question or "").strip()
    m = _SUBJECT_PAT.search(q)          # nejdřív explicitní předmět „o knize X“
    if not m:
        m = _TOPIC_PAT.search(q)
    if not m:
        # slash tvar: „/cetl o hradech" → args = „o hradech"
        m = re.match(r"^o\s+(.{2,60}?)\s*\??$", q, re.IGNORECASE)
    if not m:
        return ""
    words = [w for w in re.findall(r"[\wěščřžýáíéúůďťňó-]+", m.group(1))
             if w.lower() not in _STOPWORDS]
    # HANS_READING_TOPIC_SENTENCE_BOUND_V1 — reálné čtenářské téma je krátké
    # (1-4 slova: „hradech", „Sherlock Holmes"). Delší = pahýl z ukecané věty,
    # ne téma → radši prázdno = obecný výpis „co jsem četl", ne bogus „o ‚…'".
    if len(words) > 4:
        return ""
    return " ".join(words).strip()


def _topic_stems(topic: str) -> list[str]:
    """Hrubé pahýly pro LIKE — poslední 1-3 znaky pryč (české skloňování).
    Bere jen NEJDELŠÍ (nejspecifičtější) slovo tématu — shoda na obecném
    slově víceslovného tématu („kvantová" z „kvantová chromodynamika")
    by dala falešné „mám o tom záznam". Radši poctivé „nemám záznam"."""
    words = sorted((w for w in topic.split() if len(w) >= 3),
                   key=len, reverse=True)
    if not words:
        return []
    w = words[0]
    out = []
    for cut in (0, 1, 2, 3):
        stem = w[: len(w) - cut] if cut else w
        if len(stem) >= 3 and stem.lower() not in (s.lower() for s in out):
            out.append(stem)
    return out


# ── HANS_TOPIC_ENTITY_AWARE_V1 (21.8.) — ZEPTEJ SE, CO TO TÉMA JE ──────────
# Dosud se téma hledalo jako ŘETĚZEC: uřízni pár znaků a hledej podřetězec.
# Jenže „Václav Svoboda" není řetězec, je to OSOBA — a Hans to ví, entity
# store drží typované záznamy z jeho čtení (`etype='osoba'`). Jen se ho nikdo
# neptal, tak se místo toho vymýšlela pravidla o počtu uříznutých znaků.
# U osoby se proto nehledá pahýl, ale ŽÁDÁ SE CELÉ JMÉNO (křestní i příjmení),
# skloňování řeší táž funkce jako u entit. Tím zmizí kolize „Svoboda" ×
# „svobodou projevu" u kořene, ne záplatou.
def tema_entita(topic: str):
    """Známá entita pro dané téma (dict), nebo None. Nikdy nevyhodí výjimku —
    bez entity store se prostě hledá po staru."""
    t = (topic or "").strip()
    if not t:
        return None
    try:
        import json as _json
        from scripts.hans_entities import EntityStore
        with open("config.json", encoding="utf-8") as f:
            cfg = _json.load(f)
        es = EntityStore(cfg, cfg.get("diary_db") or "data/hans_diary.db")
        return es.resolve(t)
    except Exception:
        return None


def jmeno_entity(ent) -> str:
    """Kanonické jméno bez závorkového upřesnění („Václav Svoboda (politik
    KSČ)" → „Václav Svoboda"). Prázdno, když entita není osoba ani postava."""
    if not ent or (ent.get("etype") not in ("osoba", "postava")):
        return ""
    try:
        from scripts.hans_entities import _PAREN
        return _PAREN.sub("", ent.get("name") or "").strip()
    except Exception:
        return (ent.get("name") or "").strip()


def osoba_sedi(text: str, jmeno: str) -> bool:
    """Je v textu CELÉ jméno osoby (každé jeho slovo), i skloňované?"""
    if not jmeno or not text:
        return False
    try:
        from scripts.hans_entities import _tokens, _tok_match
    except Exception:
        return jmeno.lower() in (text or "").lower()
    t_slova = _tokens(text)
    for w in _tokens(jmeno):
        if len(w) < 3:
            continue
        if not any(w == x or _tok_match(w, x) for x in t_slova):
            return False
    return True


def _vsechna_slova_sedi(text: str, topic: str) -> bool:
    """HANS_READING_TOPIC_ALLWORDS_V1 (21.8.) — u VÍCESLOVNÉHO tématu musí
    v záznamu sedět KAŽDÉ slovo, ne jen to nejdelší.

    Doloženo 21.8.: „četl jsi o Václavu Svobodovi?" vrátilo „Meditations —
    kap. 12", protože pahýl „Svobodo" (uříznuté dva znaky) sedl na
    „svobodou projevu" — a hledání se u prvního pahýlu se shodou zastaví,
    takže se k pahýlu „Svobod" a skutečnému článku nikdy nedostane.
    Příjmení Svoboda JE běžné slovo, takže řezáním se ta kolize odstranit
    nedá; odstraní ji až požadavek, aby sedělo i „Václav".
    Jednoslovné téma zůstává beze změny (není co křížit).
    """
    slova = [w for w in (topic or "").split() if len(w) >= 3]
    if len(slova) < 2:
        return True
    for w in slova:
        varianty = []
        for cut in (0, 1, 2, 3):
            v = w[:len(w) - cut] if cut else w
            if len(v) >= 3 and v not in varianty:
                varianty.append(v)
        if not any(re.search(r"(?i)\b" + re.escape(v), text) for v in varianty):
            return False
    return True


def _dedup_cteni(rows, delsi_vyhrava: bool = False):
    """HANS_READING_DEDUP_V1 — jeden titul = jeden řádek výpisu.

    `rows` jsou (ts, event_type, title, snip) seřazené od nejnovějšího.
    Klíč je normalizovaný TITUL (ne dvojice s typem): týž článek bývá
    zapsaný pod několika typy a uživateli je to jedno — vidí dvakrát totéž.
    `delsi_vyhrava` u tématického dotazu ponechá nejobsáhlejší úryvek,
    protože tam je hodnota v poznámce, ne v názvu.
    """
    nej = {}
    poradi = []
    for r in rows:
        t = (r[2] or "").strip().lower()
        if not t:
            poradi.append(r)          # bez názvu nelze slučovat
            continue
        stav = nej.get(t)
        if stav is None:
            nej[t] = r
            poradi.append(("__klic__", t))
        elif delsi_vyhrava and len(str(r[3] or "")) > len(str(stav[3] or "")):
            # ponech novější datum, ale obsažnější úryvek
            nej[t] = (stav[0], stav[1], stav[2], r[3])
    out = []
    for x in poradi:
        out.append(nej[x[1]] if (isinstance(x, tuple) and len(x) == 2
                                 and x[0] == "__klic__") else x)
    return out


def reading_answer(db_path: str, question: str = "",
                   limit: int = 4) -> str:
    """Co/kdy jsem četl — reálné čtecí eventy z deníku, deterministicky.
    S tématem v dotazu → hledání; bez → poslední čtení."""
    topic = _extract_topic(question)
    conn = None
    try:
        conn = _ro(db_path)
        qmarks = ",".join("?" * len(_READ_TYPES))
        if topic:
            # HANS_TOPIC_ENTITY_AWARE_V1 — je téma ZNÁMÁ OSOBA? Pak se
            # nehledá pahýl, ale celé jméno (viz komentář u tema_entita).
            _osoba = jmeno_entity(tema_entita(topic))
            if _osoba:
                _log.info("HANS_TOPIC_ENTITY_AWARE_V1: téma %r je osoba %r "
                          "→ vyžaduji celé jméno", topic[:40], _osoba)
            # hledej podle tématu (title i note, hrubé stemy na skloňování)
            rows = []
            for stem in _topic_stems(topic):
                like = f"%{stem}%"
                cand = conn.execute(
                    f"SELECT ts, event_type, title, "
                    f"substr(COALESCE(NULLIF(data,''),note),1,160) "
                    f"FROM diary WHERE event_type IN ({qmarks}) "
                    f"AND (title LIKE ? OR note LIKE ? OR data LIKE ?) "
                    f"ORDER BY ts DESC LIMIT ?",
                    (*_READ_TYPES, like, like, like, limit * 4)).fetchall()
                # LIKE nemá hranice slov („hradech" chytá i „Vinohradech")
                # → post-filtr: stem musí začínat na hranici slova
                _wb = re.compile(r"(?i)\b" + re.escape(stem))
                rows = []
                for r in cand:
                    _txt = " ".join(str(x) for x in r[2:] if x)
                    if not _wb.search(_txt):
                        continue
                    if _osoba:
                        # u osoby rozhoduje jméno, ne pahýl tématu
                        if not osoba_sedi(_txt, _osoba):
                            continue
                    # HANS_READING_TOPIC_ALLWORDS_V1 — víceslovné téma musí
                    # sednout celé, jinak stačí náhodná shoda na jednom slově.
                    elif not _vsechna_slova_sedi(_txt, topic):
                        continue
                    rows.append(r)
                if rows:
                    break
            if not rows:
                return (f"Prošel jsem svůj deník, pane — o „{topic}“ v něm "
                        f"žádný záznam čtení nemám. Nebudu si vymýšlet; "
                        f"jestli chcete, mohu si o tom něco přečíst.")
            rows = _dedup_cteni(rows, delsi_vyhrava=True)[:limit]
            # HANS_READING_KODI_SPLIT_V1 — tady se NEFILTRUJE. Na cileny
            # dotaz („cetl jsi o Jakubove zebriku?") je vylouceni FALESNE
            # ZAPRENI — presne trida chyby, kterou recall resi od 15.7.
            # Zaznam tedy zustava, jen rekne, odkud se vzal.
            _kodi = _kodi_tituly(conn)
            lines = []
            for ts, etype, title, snip in rows:
                t = (title or "").strip() or "(bez názvu)"
                line = f"– {_cz_date(ts)}: {t}"
                if _je_k_filmu(etype, title, _kodi):
                    line += " (k filmu)"
                if snip:
                    line += f" — {str(snip).strip()}"
                lines.append(line)
            return (f"Ano, pane — tohle mám o „{topic}“ ve svém deníku "
                    f"skutečně zapsáno:\n" + "\n".join(lines))
        # bez tématu → poslední čtení
        # HANS_READING_DEDUP_V1 (21.8.) — týž titul má v deníku i několik
        # záznamů (web_read + reading_takeaway, opakované čtení), takže se
        # z LIMITu ukrajovala místa a výpis „posledních čtyř" ukázal jen dvě
        # věci dvakrát (doloženo 20.8. uživatelem i 21.8.: „Pride and
        # Prejudice — kap. 46" 2×, „Design" 2×). Načti víc a ořízni AŽ po
        # sloučení. Táž zásada jako HANS_SOURCES_DEDUP_V2 u /zdroje.
        rows = conn.execute(
            f"SELECT ts, event_type, title, "
            f"substr(COALESCE(NULLIF(data,''),note),1,120) "
            f"FROM diary WHERE event_type IN ({qmarks}) "
            f"ORDER BY ts DESC LIMIT ?",
            (*_READ_TYPES, limit * 8)).fetchall()
        # HANS_READING_KODI_SPLIT_V1 — filmy ven JESTE PRED dedupem i orezem,
        # jinak by ukrajovaly mista z LIMITu presne jako duplicity, ktere
        # resil HANS_READING_DEDUP_V1 (proto je nasobitel 5 → 8).
        _kodi = _kodi_tituly(conn)          # JEDNOU, ne v kazde iteraci
        rows = [r for r in rows if not _je_k_filmu(r[1], r[2], _kodi)]
        rows = _dedup_cteni(rows)[:limit]
        if not rows:
            return ("V deníku zatím žádné čtení zapsané nemám, pane.")
        lines = []
        for ts, etype, title, snip in rows:
            t = (title or "").strip() or "(bez názvu)"
            kind = {"book_read": "kniha", "study_note": "studium",
                    "book_completion_reflection": "dočtená kniha"}.get(
                        etype, "četba")
            lines.append(f"– {_cz_date(ts)} ({kind}): {t}")
        return ("Podle mého deníku jsem naposledy četl toto, pane:\n"
                + "\n".join(lines))
    except Exception as e:
        _log.warning("reading_answer selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── kdy jsi mě / X viděl ─────────────────────────────────────────────────────

def _resolve_person(question: str, config: dict,
                    asker: Optional[str]) -> Optional[str]:
    """Koho se dotaz týká: 'mě' → tazatel; jinak zkus person_name_forms."""
    low = (question or "").lower()
    if re.search(r"\bm[ěe]\b|\bmne\b", low):
        return asker
    forms_map = (config or {}).get("person_name_forms", {}) or {}
    words = set(re.findall(r"[a-zěščřžýáíéúůďťňó]+", low))
    for pid, forms in forms_map.items():
        if words & set(f.lower() for f in forms):
            return pid
    return asker


def films_watched_answer(db_path: str, question: str = "",
                         limit: int = 5) -> str:
    """Jaký film/pořad jsem viděl/sledoval — z deníku (kodi_playing),
    deterministicky. „dnes" v dotazu → dnešní; jinak posledních pár. Žádný LLM.
    Řeší, aby Hans neabstoval na „jaký film jsi viděl", když to v deníku má."""
    conn = None
    try:
        conn = _ro(db_path)
        q = (question or "").lower()
        today = "dnes" in q or "dneska" in q
        if today:
            midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            rows = conn.execute(
                "SELECT ts, title FROM diary WHERE event_type='kodi_playing' "
                "AND ts >= ? ORDER BY ts DESC", (midnight,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, title FROM diary WHERE event_type='kodi_playing' "
                "ORDER BY ts DESC LIMIT ?", (limit * 5,)).fetchall()
        if not rows:
            return ("Nemám záznam o žádném filmu či pořadu, který bych "
                    + ("dnes " if today else "") +
                    "sledoval, pane. Nebudu si nic vymýšlet.")
        seen, titles = set(), []
        for ts, title in rows:
            t = (title or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                titles.append((ts, t))
            if len(titles) >= limit:
                break
        if today:
            names = "; ".join("„%s“" % t for _, t in titles)
            return "Dnes jsem u obrazovky zaznamenal: %s." % names
        last_ts, last = titles[0]
        out = "Naposledy jsem sledoval „%s“ (%s)." % (last, _cz_when(last_ts))
        if len(titles) > 1:
            out += " Předtím: %s." % "; ".join("„%s“" % t for _, t in titles[1:])
        return out
    except Exception:
        return ("K filmům teď nemám přístup do deníku, pane.")
    finally:
        if conn:
            conn.close()


def last_seen_answer(db_path: str, config: dict, question: str,
                     asker: Optional[str]) -> str:
    """Kdy jsem osobu naposledy viděl — přímo z person_seen. Žádný LLM."""
    person = _resolve_person(question, config, asker)
    if not person:
        return "Nevím jistě, koho máte na mysli, pane."
    # HANS_LAST_SEEN_NAME_V1 — `person` je KLÍČ z `person_name_forms` („jana"),
    # ne jméno k vyslovení. Bez skloňování z toho lezlo „Naposledy jsem osobu
    # jana viděl" (malé písmeno, 1. pád). Tvary drží config (known_persons.acc),
    # `cz_names.acc` je jen přečte — nic se tu nevymýšlí.
    from scripts.cz_names import acc as _cz_acc
    who = "vás" if person == asker else _cz_acc(person, config)
    conn = None
    try:
        conn = _ro(db_path)
        rows = conn.execute(
            "SELECT ts FROM diary WHERE event_type='person_seen' "
            "AND lower(title) LIKE ? ORDER BY ts DESC LIMIT 40",
            (f"%{person.lower()}%",)).fetchall()
        if not rows:
            return (f"V deníku nemám žádný záznam, že bych {who} viděl, pane.")
        last = rows[0][0]
        # předchozí NÁVŠTĚVA = starší záznam oddělený > 1 h mezerou
        prev = None
        for (ts,) in rows[1:]:
            if last - ts > 3600:
                prev = ts
                break
        gap_min = (time.time() - last) / 60.0
        if gap_min < 15:
            out = f"Vidím {who} právě teď, pane"
        elif gap_min < 90:
            out = (f"Naposledy jsem {who} viděl před "
                   f"{int(round(gap_min))} minutami")
        else:
            out = f"Naposledy jsem {who} viděl {_cz_when(last)}"
        if prev:
            out += f"; předtím {_cz_when(prev)}"
        return out + ". Tak to mám zapsáno v deníku."
    except Exception as e:
        _log.warning("last_seen_answer selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Recall filmu podle TITULU (HANS_FILM_RECALL_V1) ──────────────────────────
# Doložený případ „Proud krve“: Hans na „co víš o filmu X“ zapřel („nemám
# záznam“), ačkoli v deníku má vlastní `movie_opinion` (děj, žánr, názor) a
# `kodi_playing` (kdy viděl). conversation_recall hledá jen v human_chat a RAG
# kolekce hans_filmy tyhle deníkové eventy neindexuje → false-negative brzdy.
# Řešení: REVERZNÍ shoda — vezmi TITULY z deníku jako slovník a najdi, který se
# vyskytuje v dotazu (žádné křehké parsování názvu). Vrátí GROUNDED blok
# z Hansových VLASTNÍCH záznamů (nic se nedomýšlí).

_FILM_CTX = re.compile(
    r"\b(film|filmu|filmy|filmem|serial|serialu|seri[aá]l|po[řr]ad|kino|"
    r"co v[ií][sš] o|rekni mi o|reknes mi o|zn[aá][sš]|vid[eě]l jsi|"
    r"vim o|v[ií][sš] o|pamatuje[sš] .*film)\b", re.IGNORECASE)


def _looks_like_film_query(question: str) -> bool:
    """Vypadá dotaz jako na film / „co víš o X“? (levný gate před DB reverzní
    shodou — ať se titulový slovník netahá na každou zprávu)."""
    return bool(_FILM_CTX.search(_fold(question or "")))


def film_knowledge_answer(db_path: str, question: str = "") -> Optional[str]:
    """HANS_FILM_RECALL_V1 — když dotaz zmiňuje FILM podle názvu, dohledej v
    deníku Hansovy VLASTNÍ záznamy o tom filmu (movie_opinion = názor/děj,
    kodi_playing = kdy viděl) a vrať GROUNDED blok. None = žádný známý titul
    v dotazu → normální tok. Deterministické, žádný LLM."""
    if not question or not _looks_like_film_query(question):
        return None
    conn = None
    try:
        conn = _ro(db_path)
        q_fold = _fold(question).lower()
        # slovník titulů z deníku (distinct, filmové event types)
        rows = conn.execute(
            "SELECT DISTINCT title FROM diary WHERE event_type IN "
            "('movie_opinion','kodi_playing','movie_browsed','film_suggestion') "
            "AND title IS NOT NULL AND length(title) >= 4").fetchall()
        # reverzní shoda na hranici slov; jen distinktivní tituly
        best = None
        for (title,) in rows:
            tf = _fold(title).lower().strip()
            if len(tf) < 4:
                continue
            multiword = " " in tf
            if not multiword and len(tf) < 6:
                continue  # krátké jednoslovné (Hra, Past…) → riziko falešné shody
            if re.search(r"\b" + re.escape(tf) + r"\b", q_fold):
                if best is None or len(tf) > len(_fold(best).lower()):
                    best = title
        if not best:
            return None
        # Hansovy vlastní názory/poznámky (obsah bývá v `data`, fallback `note`)
        ops = conn.execute(
            "SELECT ts, COALESCE(NULLIF(data,''), note) FROM diary "
            "WHERE event_type='movie_opinion' AND title=? "
            "AND COALESCE(NULLIF(data,''), note) IS NOT NULL "
            "ORDER BY ts DESC LIMIT 4", (best,)).fetchall()
        # kolikrát/kdy viděl
        seen = conn.execute(
            "SELECT COUNT(*), MAX(ts) FROM diary WHERE event_type='kodi_playing' "
            "AND title=?", (best,)).fetchone()
        notes = [str(n).strip() for _, n in ops if n and str(n).strip()]
        if not notes and not (seen and seen[0]):
            return None  # titul se objevil, ale nic konkrétního → nech projít dál
        parts = [f"SKUTEČNÝ ZÁZNAM o „{best}“ z TVÉHO deníku (odpověz JEN z něj, "
                 f"nic si nedomýšlej; na co tu není, přiznej „to si nevybavuji“):"]
        if notes:
            parts.append("Tvé dřívější poznámky a názory:")
            parts.extend(f"- {n}" for n in notes)
        if seen and seen[0]:
            kdy = _cz_when(seen[1]) if seen[1] else "dříve"
            krat = "jednou" if seen[0] == 1 else f"{seen[0]}×"
            parts.append(f"(V záznamu přehrávání: viděl jsi to {krat}, naposledy {kdy}.)")
        return "\n\n" + "\n".join(parts)
    except Exception as e:
        _log.warning("film_knowledge_answer selhal: %s", e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ── Smoke (python3 -m scripts.hans_recall) ───────────────────────────────────
if __name__ == "__main__":
    import json
    cfg = {}
    try:
        cfg = json.load(open("config.json", encoding="utf-8"))
    except Exception:
        pass
    db = cfg.get("diary_db", "data/hans_diary.db")

    print("=== first_memory_answer ===")
    print(first_memory_answer(db))

    print("\n=== _extract_topic ===")
    for q in ("co jsi četl?", "četl jsi něco o hradech?",
              "kdy jsi četl o Sherlocku Holmesovi?",
              "četl jsi Ivanhoe?", "cos dneska četl"):
        print(f"  {q!r} → {_extract_topic(q)!r}")

    print("\n=== reading_answer (obecné) ===")
    print(reading_answer(db))
    print("\n=== reading_answer (téma 'hradech') ===")
    print(reading_answer(db, "četl jsi něco o hradech?"))
    print("\n=== reading_answer (téma 'kvantová chromodynamika') ===")
    print(reading_answer(db, "četl jsi něco o kvantové chromodynamice?"))

    print("\n=== last_seen_answer ===")
    print(last_seen_answer(db, cfg, "kdy jsi mě naposledy viděl?", "standa"))


# ── HANS_RECENT_ACTIVITY_V1 (18.7.) — „co jsi se dnes dozvěděl / co sis zapsal"

_RECENT_QUERY_RE = re.compile(
    r"(co\s+(?:jsi\s+se\s+)?(?:dnes|za\s+dnesek|te[dď])\s+"
    r"(?:dozv[ěe]d[eě]l|nau[čc]il|zjistil|na[čc]etl|[čc]etl|napadlo)|"
    r"n[ěe]jak[ée]\s+(?:zajímavosti|zaj[ií]mavosti|z[áa]zna?my?)\s+(?:dnes|dneska)?|"
    r"co\s+sis?\s+dnes\s+zapsal|"
    r"co\s+jsi\s+dnes\s+d[ěe]lal|"
    r"jak\s+jsi\s+d[ne]s\s+str[áa]vil|"
    # HANS_RECENT_ACTIVITY_NIGHT_V1 (20.8.) — NOC je totéž jako „dnes".
    # Doloženo: „co jsi dělal v noci?" bránou neprošlo, odpověď proto skládal
    # model volně — a persona majordoma si domyslela noční službu („byl jsem
    # v režimu hlídání", „ujistil jsem se, že dveře a okna jsou zavřená"),
    # ačkoli hlídání bylo vypnuté. Deterministická odpověď z deníku přitom
    # existovala, jen se k ní dotaz nedostal. Odstranění důvodu improvizovat,
    # ne další brzda — táž logika jako HANS_NUMERALS_AS_DIGITS_V1.
    r"co\s+jsi\s+(?:d[ěe]lal|prov[áa]d[ěe]l)\s+(?:dnes\s+)?v\s+noci|"
    r"co\s+jsi\s+(?:d[ěe]lal|prov[áa]d[ěe]l)\s+p[řr]es\s+noc|"
    r"jak\s+jsi\s+str[áa]vil\s+(?:tu\s+)?noc|"
    r"co\s+bylo\s+v\s+noci)",
    re.I,
)


def is_recent_activity_query(text: str) -> bool:
    """Ptá se uživatel „co jsi se dnes dozvěděl / co sis zapsal / jaké
    zajímavosti dnes / co jsi dnes dělal"? Deterministický gate."""
    return bool(_RECENT_QUERY_RE.search(_fold(text or "")))


def recent_activity_answer(db_path: str, days: int = 1,
                           max_items_per_type: int = 3) -> Optional[str]:
    """HANS_RECENT_ACTIVITY_V1 — deterministický recall Hansovy vlastní
    aktivity za posledních N dní (default 1 = dnešek). Vrátí grounded blok
    z deníku (study_note, book_reflection, reading_takeaway, web_read,
    movie_opinion, introspection, spontaneous, art_generated) — Hans z toho
    LLM vytvoří lidskou odpověď, ale je grounded ve faktech.

    Účel: opravit false-negative anti-konfab („nemám záznam") na dotaz na
    dnešní aktivitu, když Hans REÁLNĚ dnes něco dělal a to je v deníku.
    """
    since = time.time() - days * 86400.0
    # kategorie k výpisu (label → event_type, kolik z každého)
    cats = [
        ("Studoval jsem", "study_note", max_items_per_type),
        ("Četl jsem", "web_read", max_items_per_type),
        ("Zaujalo mě ze čtení", "reading_takeaway", max_items_per_type),
        ("Zapsal jsem k filmu/pořadu", "movie_opinion", max_items_per_type),
        ("Zapsal jsem ke knize", "book_reflection", max_items_per_type),
        ("Mě napadlo (spontaneous)", "spontaneous", max_items_per_type),
        ("Uvažoval jsem (introspection)", "introspection", max_items_per_type),
    ]
    lines = []
    total = 0
    conn = None
    try:
        conn = _ro(db_path)
        conn.row_factory = sqlite3.Row
        for label, etype, lim in cats:
            # HANS_SPONTANEOUS_TEMPLATE_MARK_V1/V2 (27.8.; V2 kotví na začátek
            # pole — `%"template"%` kdekoli by tiše zahodilo článek,
            # který o šablonách jen píše) — „Mě napadlo"
            # nesmí být šablona. Filtr je psaný obecně (platí na kterýkoli typ
            # označený jako šablona), ne jen na `spontaneous`.
            rows = conn.execute(
                "SELECT ts, title, note, data FROM diary "
                "WHERE event_type=? AND ts >= ? "
                "AND coalesce(note, data, '') != '' "
                "AND coalesce(data,'') NOT LIKE '{\"template\":%' "
                "ORDER BY ts DESC LIMIT ?",
                (etype, since, lim)).fetchall()
            if not rows:
                continue
            lines.append(f"{label}:")
            for r in rows:
                _content = (r["note"] or r["data"] or "").strip()
                _title = (r["title"] or "").strip()
                _snip = (_content[:180] + ("…" if len(_content) > 180 else ""))
                if _title:
                    lines.append(f"  • [{_title}] {_snip}")
                else:
                    lines.append(f"  • {_snip}")
                total += 1
    except Exception as e:
        _log.warning("recent_activity_answer: %s", e)
        return None
    finally:
        if conn:
            conn.close()
    if total == 0:
        return None  # Hans dnes reálně nic nedělal → pusť anti-konfab
    return ("SKUTEČNÉ zápisky z tvého deníku za dnešek (odpověz JEN z nich; "
            "shrň lidsky, nevymýšlej nic, co v nich není):\n\n"
            + "\n".join(lines))


# ── HANS_KNOWLEDGE_CHECK_V1 (18.7.) — „znáš X?" když X NENÍ v paměti ────────
# Doložený bug (18.7. 21:15): user „Znáš Červený trpaslík?" → hans-czech
# halucinoval „Ano, mám v paměti záznamy" (LEŽ, RAG žádný match). Prompt
# klauzule V2 nezakázala + persona finetune ji ignoruje. Fix: grounding blok
# s explicit markerem PŘED user query (G4B_GROUNDING_POSITION_V1 = poslední
# slovo). Když detekt „znáš X?" a X není v deníku/entities → grounding říká
# „PAMĚŤ NEOBSAHUJE X, můžeš odpovědět obecnou znalostí, ale NIKDY 'mám záznam'".

_KNOWLEDGE_CHECK_RE = re.compile(
    r"\b(?:zn[áa][sš]|zn[áa]te|sly[šs]el(?:a)?\s+jsi\s+o|"
    r"co\s+v[íi][šs]\s+o|"
    r"zjisti[t]?\s+v[íi]ce?\s+o|"          # HANS_KNOWLEDGE_CHECK_V2
    r"[řr]ekni\s+mi\s+o|pov[ěe]z\s+mi\s+o|"  # HANS_KNOWLEDGE_CHECK_V2
    r"m[áa][šs]\s+z[áa]zna?m\s+o|"
    # HANS_STUDY_CONTENT_RECALL_V1 (14.8.) — „co sis odnesl ZE STUDIA X" /
    # „co ses naučil o X" je otázka na OBSAH tématu X, ne na stav programu.
    # Bez tohohle ji router poslal na /studium (výpis pod-témat) místo na
    # recall zápisků. Vyžaduje PŘEDMĚT za sebou → „jak jde studium?" (stav,
    # bez předmětu) se sem nechytí a jde správně na /studium.
    r"co\s+(?:sis|ses|jsi\s+s[ie])\s+"
    r"(?:odnes\w*|nau[čc]il\w*|dozv[ěe]d\w*|zapamatoval\w*)"
    r"(?:\s+(?:ze|z)\s+studi\w+)?(?:\s+o)?|"
    r"nev[íi][šs]\s+co\s+je|nev[íi][šs]\s+kdo\s+je)"
    r"\s+([\w\s\d\.\-']+?)"
    r"(?:[?.,;\n]|$)",                     # HANS_KNOWLEDGE_CHECK_V2 — i konec řetězce
    re.I,
)


# ── HANS_KNOWLEDGE_WORDORDER_V1 (19.8.) — předmět PŘED slovesem ─────────────
# `_KNOWLEDGE_CHECK_RE` čeká pořadí „co ses DOZVĚDĚL o divadle". Čeština běžně
# staví i obráceně („co ses O TOM DIVADLE dozvěděl?") a takový dotaz branou
# propadl — Hans pak odpověděl volnou generací a rozešel se s VLASTNÍM zápiskem
# (doloženo 19.8.: řekl rok 1963 a „Zdeňka Svěřínského", ač jeho study_note
# uvádí 1966 a Jiřího Šebánka).
# ⚠️ NEPÍŠU nový mechanismus — `HANS_STUDY_CONTENT_RECALL_V1` už existuje
# a funguje, jen ho míjí slovosled. Táž třída jako HANS_HRAJE_WORDORDER_V1
# („co teď běží v tv"). Proto se věta jen PŘESKLÁDÁ do tvaru, kterému rozumí.
_OBJ_FIRST_RE = re.compile(
    r"\bco\s+(sis|ses|jsi\s+si|jsi\s+se)\s+(o|na)\s+(.{2,60}?)\s+"
    r"(odnes\w*|nau[čc]il\w*|dozv[ěe]d\w*|zapamatoval\w*)",
    re.IGNORECASE)


def _reorder_object_first(text: str) -> str:
    """Přeskládá „co ses o tom divadle dozvěděl" → „co ses dozvěděl o tom
    divadle". Když vzor nesedí, vrací text beze změny."""
    t = text or ""
    try:
        return _OBJ_FIRST_RE.sub(
            lambda m: "co %s %s %s %s" % (m.group(1), m.group(4),
                                          m.group(2), m.group(3)), t)
    except Exception:
        return t


def is_knowledge_check_query(text: str) -> bool:
    """Ptá se uživatel „znáš X?" / „co víš o X?" / „máš záznam o X?"? Levný gate.
    Regex je unicode-safe → volám na ORIGINÁLU (bez _fold), ať `_extract_topic`
    dostane originální text s diakritikou."""
    return bool(_KNOWLEDGE_CHECK_RE.search(_reorder_object_first(text)))


def _extract_knowledge_topic(text: str) -> Optional[str]:
    """Vytáhne X z „znáš X?" — capture group regexu. Očištěno o pomocná slova."""
    m = _KNOWLEDGE_CHECK_RE.search(text or "")
    if not m:
        return None
    x = m.group(1).strip(" .,?!;:'\"")
    # Odstranit prefix „ten/tu/to/ta/serial/film/kniha" (pomocná slova bez informace)
    # HANS_KNOWLEDGE_CHECK_V2 — skloněné tvary media-typu („o filmU/seriálU/
    # knizE X"), jinak „filmu Proud krve" nesedne na paměť → falešná nabídka
    # studia u filmu, který Hans zná.
    x = re.sub(r"^(?:seri[áa]l\w*|film\w*|kn[ií]\w+|posta?v\w*|typ\w*)\s+",
               "", x, flags=re.I)
    return x.strip() or None


# kvalifikátory, které v „co víš o jazyku X / o filmu X" nesou téma až za sebou
_TOPIC_QUALIFIERS = {
    "jazyku", "jazyce", "jazyk", "tematu", "tématu", "téma", "tema",
    "filmu", "film", "knize", "kniha", "knihy", "meste", "městě", "město",
    "projektu", "projekt", "autorovi", "autor", "pojmu", "pojem", "slovu",
    "slovo", "clanku", "článku", "clanek", "článek", "strance", "stránce",
    "stranka", "stránka", "webu", "web",
}


def _topic_core_prefixes(topic: str) -> list:
    """HANS_RECALL_STEM_V2 — jádrová slova tématu (bez kvalifikátorů) oříznutá
    na KMEN (declension-safe). 'jazyku dadština' → ['dadš']; 'hradech' → 'hrad'.
    Ořez -3 znaky (česká koncovka mění poslední 1–3 znaky kmene), podlaha 4
    (kmen 'hrad' má 4 znaky) — kratší by v textu náhodně splýval, proto se
    4znakové prefixy hledají jen v titulu (viz `_topic_in_memory`)."""
    import re as _re
    words = [w for w in _re.split(r"[^0-9a-zá-žA-ZÁ-Ž]+", (topic or "").lower())
             if len(w) >= 4 and w not in _TOPIC_QUALIFIERS]
    return [w[:max(4, len(w) - 3)] for w in words]


def _topic_in_memory(db_path: str, topic: str) -> bool:
    """True když topic MÁ nějaký záznam v deníku / entities. Declension-safe
    (HANS_RECALL_DECLENSION_V1): matchuje na PREFIX každého jádrového slova
    ('jazyku dadština'/'dadštině' → 'dadšt'). U víceslovných témat musí najít
    VŠECHNA jádrová slova (AND) → 'žirafí polévka' nedá false-positive jen
    protože 'polévka' někde je."""
    if not topic or len(topic) < 3:
        return False
    prefixes = _topic_core_prefixes(topic)
    if not prefixes:
        return False
    conn = None
    # HANS_RECALL_NODIA_DB_V1 (13.8.) — SQL LIKE NESKLÁDÁ DIAKRITIKU, takže
    # dotaz bez háčků minul zápisek s háčky: „co vis o historii opevneni?" →
    # jádrový prefix 'opevne' × uložené 'opevnění' → Hans ZAPŘEL tři vlastní
    # zápisky („nic jsem si o tom nezapsal ani nečetl", doloženo 13.8. 16:31,
    # ačkoli má study_note „Historie opevnění"). Uživatel píše z telefonu bez
    # diakritiky, deník ji má — přesně na to `_fold` odjakživa je, jen se tady
    # nevolalo (klasické „komponenta existuje, ale nikdo ji nevolá").
    # POŘADÍ: nejdřív prostá shoda (rychlá), teprve při neúspěchu složená
    # (volá python funkci nad řádky) → dotazy S diakritikou nic nestojí navíc.
    _TYPES = ("'web_read','study_note','book_read','book_reflection',"
              "'movie_opinion','kodi_playing','reading_takeaway'")
    # HANS_RECALL_NODIA_SPLIT_V1 (13.8.) — `UNION` nutil SQLite vyhodnotit OBĚ
    # strany, i když malá tabulka `entities` odpověděla hned: změřeno 1557 ms
    # vs 124 ms pro tytéž dotazy spuštěné ZVLÁŠŤ s předčasným koncem
    # (entities 2 ms → diary 122 ms). Rozděleno = 12× rychleji v běžném případě,
    # kdy se téma najde. Případ „nenajde" zůstává ~1,6 s (plný sken je nutný).
    # `%(f)s` = obalová funkce nad sloupcem: prázdná pro prostou shodu,
    # `nodia` pro shodu bez diakritiky.
    # HANS_RECALL_STEM_V2 — postav JEDEN dotaz, který vyžaduje VŠECHNA jádrová
    # slova v TÉMŽE řádku (AND). Dřív se každé slovo hledalo zvlášť napříč
    # deníkem → „historie fotbaloveho mistrovstvi" našlo 3 slova ve 3 různých
    # záznamech = falešně „mám záznam" (měřeno: 4 z 8 negativů). Krátký prefix
    # (≤4 zn) jen v TITULU — v dlouhém textu poznámky by 4 znaky splynuly.
    def _clauses(fn):
        """(SQL fragment 'A AND B AND …', args) pro obal `fn` ('' / 'nodia')."""
        col_t = ("%s(lower(title))" % fn) if fn else "lower(title)"
        col_n = ("%s(lower(note))" % fn) if fn else "lower(note)"
        parts, args = [], []
        for p in prefixes:
            needle = "%" + (_fold(p) if fn else p) + "%"
            if len(p) <= 4:                      # krátký → jen titul
                parts.append("(%s LIKE ?)" % col_t)
                args.append(needle)
            else:                                # delší → titul i poznámka
                parts.append("(%s LIKE ? OR %s LIKE ?)" % (col_t, col_n))
                args += [needle, needle]
        return " AND ".join(parts), args

    def _ent_clause(fn):
        """entities má jen `name` → všechny prefixy v jednom jménu (AND)."""
        col = ("%s(lower(name))" % fn) if fn else "lower(name)"
        parts, args = [], []
        for p in prefixes:
            parts.append("(%s LIKE ?)" % col)
            args.append("%" + (_fold(p) if fn else p) + "%")
        return " AND ".join(parts), args

    try:
        conn = _ro(db_path)
        try:
            conn.create_function("nodia", 1, _fold)
            _has_nodia = True
        except Exception:
            _has_nodia = False      # starší sqlite → zůstane jen prostá shoda

        def _found(fn: str) -> bool:
            ec, ea = _ent_clause(fn)                 # levné entities napřed
            if conn.execute("SELECT 1 FROM entities WHERE %s LIMIT 1" % ec,
                            ea).fetchone():
                return True
            dc, da = _clauses(fn)
            return bool(conn.execute(
                "SELECT 1 FROM diary WHERE %s AND event_type IN (%s) LIMIT 1"
                % (dc, _TYPES), da).fetchone())

        if _found(""):                              # rychlá prostá shoda
            return True
        if _has_nodia and _found("nodia"):          # až pak dražší bez háčků
            return True
        return False
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def reading_recall_answer(db_path: str, question: str = "") -> Optional[str]:
    """HANS_READING_RECALL_V1 — dotaz „co víš o X?" → dohledej Hansovo VLASTNÍ
    čtení o X (web_read/reading_takeaway/study_note, declension-safe) a vrať
    GROUNDED blok s tím, co si přečetl. None = nic → normální tok. Deterministické,
    žádný LLM. Řeší „přečteno ale nezapamatováno": ruční odkaz z chatu jde do
    web_read, ale RAG ho na tenkém souhrnu semanticky nedohledá; tady se najde
    přímo z deníku (declension-safe AND na jádrových slovech)."""
    # HANS_KNOWLEDGE_WORDORDER_V1 — i tady, jinak brána
    # pustí dotaz dál, ale hledání téma nenajde.
    question = _reorder_object_first(question)
    if not question or not is_knowledge_check_query(question):
        return None
    prefixes = _topic_core_prefixes(_extract_knowledge_topic(question) or "")
    if not prefixes:
        return None
    conn = None
    try:
        conn = _ro(db_path)
        where = " AND ".join(
            ["lower(coalesce(note,'')||coalesce(title,'')||coalesce(data,'')) "
             "LIKE ?"] * len(prefixes))
        params = ["%" + p + "%" for p in prefixes]
        rows = conn.execute(
            "SELECT coalesce(NULLIF(note,''), data) AS body FROM diary "
            "WHERE event_type IN ('web_read','reading_takeaway','study_note') AND "
            + where + " ORDER BY ts DESC LIMIT 3", params).fetchall()
        conn.close()
        conn = None
        bits = []
        for (body,) in rows:
            b = re.sub(r"^\[[^\]]{1,30}\]\s*", "", (body or "").strip())  # ořízni [topic]
            if b and len(b) > 15:
                bits.append(b[:400])
        if not bits:
            return None
        return ("\n\nZ TVÉ ČTENÁŘSKÉ PAMĚTI (co sis o tom sám přečetl a zapsal "
                "— odpověz z tohohle, ne z domýšlení):\n"
                + "\n".join("• " + x for x in bits[:3]) + "\n")
    except Exception:
        if conn:
            conn.close()
        return None


def knowledge_check_answer(db_path: str, user_text: str) -> Optional[str]:
    """HANS_KNOWLEDGE_CHECK_V1 — grounding blok „PAMĚŤ NEOBSAHUJE X" pro
    dotaz „znáš X?". None = X JE v paměti (nech normální recall/RAG cestu)
    nebo detektor selhal (dotaz není typu 'znáš X?').

    Anti-konfab silnější než system prompt klauzule (G4B position: grounding
    sedí těsně před user query, přebíjí conversation history i persona)."""
    # HANS_KNOWLEDGE_WORDORDER_V1 — i tady, jinak brána
    # pustí dotaz dál, ale hledání téma nenajde.
    user_text = _reorder_object_first(user_text)
    topic = _extract_knowledge_topic(user_text)
    if not topic:
        return None
    if _topic_in_memory(db_path, topic):
        # X JE v paměti — nech film_knowledge_answer / recall / RAG odpovědět
        return None
    return (
        "\n\nDŮLEŽITÉ FAKTUM O TVÉ PAMĚTI: v tvých vlastních záznamech "
        "(deník, entity, čtená paměť) NENÍ žádný záznam o \"%s\". Nic "
        "konkrétního jsi si o tom nezapsal ani nepamatuješ z vlastní "
        "zkušenosti.\n\n"
        "PRAVIDLA PRO ODPOVĚĎ:\n"
        "1. NIKDY neříkej „mám v paměti záznamy o %s\" ani „nedávno "
        "jsem si to pročetl\" — byla by to lež (PAMĚŤ NEOBSAHUJE).\n"
        "2. Pokud tě to napadá z obecné znalosti (z tréninku): odpověz "
        "poctivě „V paměti to nemám zapsané, ale obecně vím, že %s "
        "je...\" — jasně rozliš OBECNOU ZNALOST od PAMĚTI.\n"
        "3. Když nevíš ani obecně: „O tomto pojmu nic konkrétního nevím, "
        "pane.\"\n\n"
        "Klíč: rozlišuj OBECNÁ ZNALOST (z tréninku) vs. PAMĚŤ (co jsi "
        "sám prožil/četl/zapsal). Nesměšuj je." % (topic, topic, topic))


def knowledge_check_bypass(db_path: str, user_text: str,
                           asker: Optional[str] = None) -> Optional[str]:
    """HANS_KNOWLEDGE_CHECK_V1 BYPASS (18.7.) — deterministická odpověď na
    „znáš X?" když X NENÍ v Hansově paměti. Analogicky `sources_answer`
    (bypass mimo LLM), protože grounding block nezabral — hans-czech persona
    finetune si vždy vyfabuluje „mám v paměti záznamy".

    Vrátí string nebo None. None = X JE v paměti nebo dotaz není typu 'znáš X?'
    → nech normální recall/RAG cestu.

    Text šetří obecnou znalost (bypass nemá LLM) — přiznává „nemám v paměti"
    a nabízí uživateli, že se to může Hans naučit (studium, čtení, atd.).
    """
    topic = _extract_knowledge_topic(user_text)
    if not topic:
        return None
    if _topic_in_memory(db_path, topic):
        return None  # nech normální cestu, X JE v paměti
    oslov = _cz_address(asker) if asker else "pane"  # HANS_NAME_INFLECTION_V1
    # Kompaktní honestní odpověď + nabídka pokud chce ať Hans si to zapíše
    return ("V paměti nemám žádné vlastní záznamy o '%s', %s. "
            "Nic jsem si o tom nezapsal ani nečetl (obecně to znám možná "
            "z tréninku, ale nechci to vydávat za vlastní paměť). "
            "Kdybyste chtěl, mohu si to zařadit do studia — stačí říct "
            "'nastuduj %s'." % (topic, oslov, topic))


# ── HANS_SELF_STATE_V1 (5.8.) — grounded blok „jak se mám a co jsem dnes dělal"
def self_state_facts(db_path: str, max_items: int = 6,
                     mood: str = "", mood_reason: str = "",
                     runtime: dict = None) -> str:
    """Stručný VÝČET dnešní Hansovy činnosti z deníku (fakta, ne vyprávění).

    Proč: na „jak se máš?" / „co jsi dnes dělal?" model dosud odpovídal z ničeho
    a plodil vatu („Službu plním, a to je pro mne dostatečné") nebo komoleniny
    („zkoumal jsem historii zeleného, pana"). `recent_activity_answer` sice
    existuje, ale visí na frázovém detektoru a na dotaz typu „jak se máš" se
    nepřipojí. Tenhle blok je krátký a dává se do promptu jako FAKTA, ze
    kterých má persona čerpat — Hans pak řekne, co doopravdy dělal.

    Vrací "" když dnes není co hlásit (pak ať model nemluví o ničem).
    """
    import datetime as _dt
    start = _dt.datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp()
    # (label, event_type) — pořadí = důležitost pro vyprávění o dni
    cats = [("studoval jsem", "study_note"),
            ("napsal jsem dílo", "work_artifact"),
            ("namaloval jsem", "artwork"),
            ("četl jsem", "web_read"),
            ("zapsal jsem si ke knize", "book_reflection"),
            ("napadlo mě", "synthesis_idea"),
            ("uvědomil jsem si o sobě", "self_critique"),
            ("hovořil jsem s Koláčem", "teddy_dialog")]
    out = []
    conn = None
    try:
        conn = _ro(db_path)
        conn.row_factory = sqlite3.Row
        for label, etype in cats:
            rows = conn.execute(
                "SELECT title, note, data FROM diary WHERE event_type=? "
                "AND ts >= ? ORDER BY id DESC LIMIT 2", (etype, start)).fetchall()
            if not rows:
                continue
            det = []
            for r in rows:
                t = (r["title"] or "").strip()
                if not t:
                    t = ((r["note"] or r["data"] or "").strip().split("\n")[0])[:60]
                if t:
                    det.append(t[:70])
            if det:
                out.append("%s: %s" % (label, "; ".join(det)))
            if len(out) >= max_items:
                break
    except Exception as e:
        _log.debug("self_state_facts: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _tiche:
                log_once(  # HANS_NO_SILENT_CTX_V1
                    _log, "self_state_facts(ř. 1735)",
                    "self_state_facts: blok kontextu selhal (ř. 1735): %s", _tiche)
    head = []
    # HANS_SELF_STATE_AWAKE_V1 (7.8.) — PROVOZNÍ STAV jako první fakt.
    # Bez něj si model režim vymýšlel: 7.8. 10:52 tvrdil „Jsem v režimu
    # spánku, sleduji pouze bezpečnostní kamery", ačkoli spánek skončil
    # v 09:00 a hlídání bylo vypnuté. Stav je deterministicky zjistitelný —
    # tak ať ho čte, místo aby vyprávěl.
    if runtime:
        _st = []
        if runtime.get("sleeping") is not None:
            _st.append("spím (noční režim)" if runtime["sleeping"]
                       else "jsem vzhůru, v běžném provozu")
        if runtime.get("vision") is not None:
            _st.append("kamerou vidím" if runtime["vision"]
                       else "kameru mám vypnutou")
        # HANS_SELF_STATE_NO_OFF_MODES_V1 (20.8.) — VYPNUTÝ hlídací režim se
        # NEZMIŇUJE. Doloženo 20.8.: na „co jsi dělal v noci?" Hans odpověděl
        # „byl jsem v režimu hlídání domu", ačkoli tenhle blok měl v promptu
        # a stálo v něm „hlídací režim je vypnutý" — tedy si protiřečil
        # s vlastním podkladem.
        # A/B změřeno: v izolaci model negaci zvládne (3/3 „byl vypnutý"),
        # v plném ~14 KB promptu ji překlopí. Zmínka je semínko, šum kolem
        # spouštěč — a semínko jde odstranit. Bez ní odpoví „nemám o tom
        # informace", což je pravda; obsah noci nese zbytek bloku (studium,
        # četba). Táž logika jako HANS_NUMERALS_AS_DIGITS_V1: odebrat důvod,
        # proč model improvizuje, místo hlídání výsledku.
        # ⚠️ Netýká se ostatních režimů: „kameru mám vypnutou" i „spím" se
        # uvádět MUSÍ — tam je výchozí očekávání OPAČNÉ (že vidí a bdí),
        # takže vynechání by vyrobilo chybu na druhou stranu.
        if runtime.get("guard"):
            _st.append("hlídací režim je zapnutý")
        if _st:
            head.append("teď: " + ", ".join(_st))
    if mood:
        head.append("nálada: %s%s" % (
            mood, (" (důvod: %s)" % mood_reason) if mood_reason else ""))
    if not out and not head:
        return ""
    # Instrukce s TVAREM odpovědi: samotná fakta nestačila — persona je jen
    # olízla a vrátila vatu („Službu plním, a to je pro mne dostatečné").
    # Model potřebuje říct, KOLIK a CO má z bloku použít.
    return ("FAKTA O MĚ A O MÉM DNEŠKU — čerpej z NICH, nic si nepřidávej "
            "(co tu není, dnes nebylo):\n"
            + ("- " + "\n- ".join(head + out) if (head or out) else "")
            + "\n\nKdyž se ptá, jak se mám nebo co jsem dělal: odpověz 2–4 větami, "
              "řekni jak se cítím a PROČ, a jmenuj DVĚ KONKRÉTNÍ věci z dneška "
              "(téma studia, název díla, co jsem četl). Žádné obecné fráze "
              "typu „plním službu\" — ty nic neříkají."
              # HANS_SELF_STATE_AWAKE_V1 — bez téhle věty model řádek „teď:"
              # přečetl, ale stejně dodal vlastní verzi režimu.
              "\nO SVÉM REŽIMU (spánek, kamera, hlídání) mluv POUZE podle "
              "řádku „teď:\" výše. Nikdy netvrď, že něco přepínáš nebo "
              "jsi přepnul — sám to udělat neumíš, děje se to na povel.")


# ── HANS_DAY_AT_HOME_V1 (7.8.) — „co se dnes dělo v domě?" ───────────────────
# Nález C5: dotaz na DNEŠEK zpětně vracel AKTUÁLNÍ stav („na TV hraje X,
# vidím tu Y") — správná odpověď na jinou otázku. Hans neměl kam takový dotaz
# poslat: `night_summary` je až noční a je o něm samém, ne o dění v domě.
#
# ⚠️ Fakta se sbírají TADY, aby existoval JEDEN zdroj pravdy — `_write_night_
# summary` v `hans_routine` dělal totéž vlastním SQL. Druhá kopie by se časem
# rozešla (viz pravidlo „protáhni existující mechanismus" v CLAUDE.md).
def day_facts(db_path: str, date_str: Optional[str] = None) -> dict:
    """Fakta o jednom dni z deníku. Čistě SQL, žádný LLM, deferral-safe.

    Vrací dict s klíči: date, n_events, n_dialogs, types, people (se
    začátkem/koncem přítomnosti), reads, takeaways, films, moments.
    """
    import datetime as _dt
    day = date_str or _dt.datetime.now().strftime("%Y-%m-%d")
    out = {"date": day, "n_events": 0, "n_dialogs": 0, "types": [],
           "people": [], "reads": [], "takeaways": [], "films": [],
           "moments": []}
    conn = None
    try:
        conn = _ro(db_path)
        D = "date(ts,'unixepoch','localtime')=?"

        def q(sql):
            try:
                return conn.execute(sql, (day,)).fetchall()
            except Exception:
                return []

        out["n_events"] = (q(f"SELECT COUNT(*) FROM diary WHERE {D}") or [[0]])[0][0]
        out["n_dialogs"] = (q("SELECT COUNT(*) FROM diary WHERE "
                              f"event_type='teddy_dialog' AND {D}") or [[0]])[0][0]
        out["types"] = [(r[0], r[1]) for r in q(
            f"SELECT event_type, COUNT(*) FROM diary WHERE {D} "
            "GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 5")]
        # Osoby VČETNĚ času — „co se dělo" je hlavně kdo tu byl a kdy.
        out["people"] = [(r[0], r[1], r[2]) for r in q(
            "SELECT title, MIN(ts), MAX(ts) FROM diary WHERE "
            f"event_type='person_seen' AND {D} AND title NOT IN "
            "('','Unknown','?','unknown_person') GROUP BY title ORDER BY MIN(ts)")]
        out["reads"] = [r[0] for r in q(
            f"SELECT DISTINCT title FROM diary WHERE event_type='web_read' AND {D} "
            "AND title<>'' ORDER BY ts DESC LIMIT 4")]
        out["takeaways"] = [r[0] for r in q(
            "SELECT coalesce(data,note) FROM diary WHERE "
            f"event_type='reading_takeaway' AND {D} AND coalesce(data,note)<>'' "
            "ORDER BY ts DESC LIMIT 2")]
        out["films"] = [r[0] for r in q(
            "SELECT DISTINCT title FROM diary WHERE event_type IN "
            f"('kodi_playing','movie_opinion') AND {D} AND title<>'' "
            "ORDER BY ts DESC LIMIT 3")]
        out["moments"] = [r[0] for r in q(
            "SELECT coalesce(NULLIF(note,''),data) FROM diary WHERE "
            f"COALESCE(importance,0)>=6 AND {D} AND "
            "coalesce(NULLIF(note,''),data)<>'' AND event_type NOT IN "
            "('human_chat','night_summary') ORDER BY importance DESC, ts DESC LIMIT 3")]
    except Exception as e:
        _log.debug("day_facts: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def day_fact_lines(f: dict, config: dict = None) -> list:
    """`day_facts` → české věty pro LLM grounding i pro deterministický výpis.

    Jména se zobrazují přes `cz_names.display_name` — jinak by v textu byla
    tak, jak jsou klíče v konfiguraci (malá písmena, bez diakritiky).
    """
    import time as _t

    def _nm(n):
        # HANS_DAY_AT_HOME_GENDER_V1 (7.8.) — jméno + ROD. Bez rodu hlasový
        # krok skloňoval ženská jména jako mužská („pan" u ženy);
        # týž fix jako HANS_SELF_INSIGHT_GENDER_V1 u vhledů.
        try:
            from scripts import cz_names
            disp = cz_names.display_name(n, config) or n
            g = cz_names.person_gender(n, config)
            if g == "žena":
                return "paní " + disp
            if g == "muž":
                return "pan " + disp
            return disp
        except Exception:
            return n

    def _hm(ts):
        return _t.strftime("%H:%M", _t.localtime(ts))

    lines = []
    if f.get("people"):
        parts = []
        for name, t0, t1 in f["people"]:
            parts.append("%s (%s–%s)" % (_nm(name), _hm(t0), _hm(t1))
                         if t1 - t0 > 300 else "%s (%s)" % (_nm(name), _hm(t0)))
        lines.append("V domě jsem dnes viděl: " + ", ".join(parts) + ".")
    elif f.get("n_events"):
        # Jen když se ten den VŮBEC něco dělo. Na úplně prázdném dni musí
        # zůstat prázdný seznam, jinak by `_write_night_summary` považoval
        # fakta za neprázdná a nechal model psát reflexi o ničem (dřív spadl
        # na statistiku) — regrese chycená testem na dni bez záznamů.
        lines.append("Dnes jsem v domě nikoho neviděl.")
    if f.get("films"):
        lines.append("Na televizi běželo: " + ", ".join(f["films"]) + ".")
    if f.get("moments"):
        lines.append("Výrazné chvíle dne: "
                     + " / ".join(m.strip()[:180] for m in f["moments"]))
    if f.get("reads"):
        lines.append("Sám jsem četl: " + ", ".join(f["reads"]) + ".")
    if f.get("takeaways"):
        lines.append("Z četby mě zaujalo: "
                     + " / ".join(t.strip()[:180] for t in f["takeaways"]))
    if f.get("n_dialogs"):
        lines.append("Rozhovorů s Koláčem: %d." % f["n_dialogs"])
    return lines


# ── HANS_PERSON_CARD_V1 (18.8.) — „kdo je X?“ deterministicky ────────────────
# C4 (7.8.): fakt o dceři LEŽÍ v `relationships` (role, rodina, charakterizace
# 379 znaků), ale dotaz šel rovnou do RAG. Ten nic nenašel, a o tom, jestli Hans
# odpoví nebo abstinuje, pak rozhodoval self-consistency práh — TÁŽ otázka tedy
# jednou vrátila odpověď a podruhé „nemám spolehlivý záznam“.
# Pořadí je proto: domácnost → encyklopedie → nic. Bez LLM; hlas se přidá až
# nad výsledkem ([[prompt-debt-tool-calling]]: STAV → PAMĚŤ → HLAS).


def _family_sentence(pid: str, links: dict, config: dict) -> str:
    """Rodinné vazby jako ŠTÍTKY s dvojtečkou („Rodiče: Standa a Jana“).

    ⚠️ Záměrně NE větná vazba: čeština by chtěla 2. pád („dcera Standy a Jany“)
    a `cz_names` umí jen vokativ a akuzativ. Vymýšlet další skloňování kvůli
    jedné větě se nevyplatí — štítek je gramaticky bezpečný v každém pádu.
    """
    if not links:
        return ""
    try:
        from scripts.cz_names import display_name as _dn
    except Exception:
        return ""
    def _join(ids):
        return " a ".join(_dn(i, config) or i for i in ids if i)
    out = []
    if links.get("parents"):
        out.append("rodiče: %s" % _join(links["parents"]))
    if links.get("spouse"):
        out.append("partner: %s" % (_dn(links["spouse"], config) or links["spouse"]))
    if links.get("children"):
        out.append("děti: %s" % _join(links["children"]))
    return "; ".join(out)


#: HANS_HOUSEHOLD_PRIVACY_V1 — odpověď cizímu tazateli. Radši zdvořilé
#: odmítnutí než mlčení: kdyby cesta jen zmlkla, odpověď doskládá model
#: z ostatního kontextu a domácnost může vyzradit stejně.
_PRIVACY_REFUSAL = ("O lidech z tohoto domu mluvím jen s těmi, koho znám, "
                    "pane. Snad mi to prominete.")


def person_card(db_path: str, query: str, config: dict,
                asker: str = "") -> str:
    """Deterministická odpověď na „kdo je X / co víš o X“. "" = nevím (pak ať
    odpoví běžná cesta; NIC se nedomýšlí).

    Pořadí: (1) `relationships` — domácnost zná Hans nejlíp a má o ní vlastní
    pozorování; (2) `entities` — lidé z jeho čtení (Bud Spencer). Přísné
    `resolve` (bez `loose`, etype='osoba'), aby „co víš o hradech“ netrefilo
    člověka.
    """
    q = (query or "").strip()
    if not q:
        return ""
    # (1) DOMÁCNOST
    try:
        from scripts.cz_names import (find_known_person, display_name,
                                      is_known_person)
        pid = find_known_person(q, config)
        # HANS_HOUSEHOLD_PRIVACY_V1 — o ČLENU DOMÁCNOSTI jen se známou osobou.
        # Encyklopedické osoby (větev 2) zůstávají volné — Bud Spencer je
        # veřejný fakt, ne soukromí domu.
        if pid and asker and not is_known_person(asker, config):
            _log.info("person_card: %r není známá osoba → soukromí domácnosti",
                      asker)
            return _PRIVACY_REFUSAL
        if pid:
            from scripts.hans_relationships import Relationships
            card = Relationships(config).get(pid)
            if card:
                nm = card.display_name or display_name(pid, config) or pid
                head = nm
                if card.role:
                    head += " — " + card.role
                head += "."
                fam = _family_sentence(pid, card.family_links or {}, config)
                if fam:
                    head += " (%s)" % fam
                ch = (card.characterization or "").strip()
                if ch:
                    head += " " + (ch[:400] + ("…" if len(ch) > 400 else ""))
                return head
    except Exception as e:
        _log.debug("person_card: domácnost (%s)", e)
    # (2) ENCYKLOPEDIE (Hansovo čtení)
    try:
        from scripts.hans_entities import EntityStore
        ent = EntityStore(config).resolve(q, etype="osoba")
        if ent:
            # ⚠️ NE `fact_block()` — ta věta („Ověřený fakt o „X“ (z mého
            # čtení, zdroj: …)“) je GROUNDING PRO MODEL, ne odpověď člověku.
            # Když ji vrátíme přímo (a to se stane vždy, když hlasový krok
            # neprojde kontrolou), uživatel čte vnitřek stroje. Doloženo
            # živě 18.8. Skládáme proto vlastní, čitelnou podobu.
            g = (ent.get("gloss") or "").strip()
            nm = ent.get("name") or "?"
            if not g:
                return "%s — mám o něm záznam, ale bez bližšího popisu." % nm
            src = (ent.get("source") or "").strip()
            out = g if g.lower().startswith(nm.lower()[:6]) else "%s: %s" % (nm, g)
            if src:
                out += " (z mého čtení, zdroj: %s)" % src
            return out
    except Exception as e:
        _log.debug("person_card: entity (%s)", e)
    return ""


def person_card_voiced(db_path: str, query: str, config: dict,
                       asker: str = "") -> str:
    """HANS_PERSON_CARD_VOICE_V1 (18.8.) — táž fakta, ale Hansovým hlasem.

    Bez tohohle kroku dostal uživatel do chatu SYROVOU KARTU
    („Jana — paní domu. (partner: Standa; děti: Klára) …“), případně rovnou
    vnitřní grounding řetězec z `fact_block` („Ověřený fakt o „X“ (z mého
    čtení, zdroj: …)“). To je podklad PRO MODEL, ne odpověď člověku —
    doloženo živým dialogem 18.8.

    Kontrakt je stejný jako u `/dnes` (HANS_DAY_AT_HOME_V1): fakta vzniknou
    DETERMINISTICKY, hlas je smí jen přeformulovat. Když mozek není
    (herní mód / PC dole), vrátí se karta holá — radši strohé než žádné.
    """
    card = person_card(db_path, query, config, asker=asker)
    if not card or card == _PRIVACY_REFUSAL:
        return card   # odmítnutí jde jak je, model ho nepřebásňuje
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return card
    except Exception:
        pass
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_core
        try:
            core = persona_core(config, with_address=False)
        except Exception:
            core = ""
        model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + (
            "Někdo se tě ptá na konkrétního člověka. Odpověz souvisle "
            "(2-4 věty, tvým hlasem). "
            # Doloženo živě 18.8.: na „kdo je Bud Spencer?" model odpověděl
            # „Jmenuji se Carlo Pedersoli… zemřel jsem“ — vzal „první osobu“
            # jako pokyn mluvit ZA TU OSOBU. Proto se to říká výslovně.
            "⚠️ O té osobě mluv ve TŘETÍ osobě („je“, „byl“) — první osoba "
            "patří jen tobě, Hansovi. NIKDY nemluv jako ona. "
            # HANS_DAY_AT_HOME_EXACT_V1 (7.8.) — týž hlasový krok jinde komolil
            # čísla; tady z „1929“ udělal „roku devětadvacátého“.
            "LETOPOČTY, DATA a ČÍSLA opiš PŘESNĚ číslicemi tak, jak jsou ve "
            "faktech (1929, 2016) — nepřepisuj je slovy. "
            "Vyjdi POUZE z faktů níže — "
            "co v nich není, nevíš, a nic si nepřimýšlej: žádné domněnky o "
            "povaze, zvycích ani vztazích navíc. Jména, role a rodinné vazby "
            "opiš PŘESNĚ tak, jak jsou uvedené. Když je mezi fakty zdroj "
            "(odkaz), zmiň, odkud to máš; když tam žádný zdroj NENÍ, o zdroji "
            # Doloženo živě 18.8.: u domácnosti model připsal
            # „(zdroj neuváděn)“ — pro uživatele je to šum o vnitřku.
            "nepiš NIC, ani že chybí. Žádný nadpis, žádné odrážky, "
            "žádné uvozovky kolem celé odpovědi.")
        out = ollama_generate(
            model, "FAKTA O OSOBĚ:\n" + card + "\n\nOdpověz na dotaz: " + (query or ""),
            system=system, config=config, timeout=60)
        txt = (out or "").strip().strip('"')
        # HANS_PERSON_CARD_VOICE_V1 — DETERMINISTICKÁ KONTROLA MÍSTO DŮVĚRY.
        # Instrukce „čísla opiš číslicemi" nestačila: doloženo 18.8., z „1929"
        # a „2016" udělal hans-czech „roku devětadvacátého" a „šestnáctého".
        # To není sloh, to je posunutý FAKT. Prompt už nezesiluji (vzor
        # [[prompt-debt-tool-calling]]) — radši ověřím výsledek: když se z faktů
        # ztratí letopočet, hlasovou verzi nepřijmu a vrátím kartu.
        import re as _re
        years = set(_re.findall(r"\b(1[89]\d\d|20\d\d)\b", card))
        if years and not years.issubset(set(
                _re.findall(r"\b(1[89]\d\d|20\d\d)\b", txt))):
            _log.info("person_card_voiced: hlas ztratil letopočet %s → karta",
                      sorted(years - set(_re.findall(
                          r"\b(1[89]\d\d|20\d\d)\b", txt))))
            return card
        # Krátká odpověď = model se nechytil → radši fakta než pahýl.
        if len(txt) >= 40:
            return txt[:1200]
    except Exception as e:
        _log.warning("person_card_voiced: hlas selhal (%s) — vracím kartu", e)
    return card


def household_card(db_path: str, config: dict, asker: str = "") -> str:
    """HANS_HOUSEHOLD_CARD_V1 (18.8.) — SLOŽENÍ DOMÁCNOSTI, ne kdo je vidět.

    Doloženo dialogem 18.8.: na „kdo v tomhle domě žije" Hans odpověděl
    „pan Standa, Stando a slečna Klára" — do výčtu se vplížilo OSLOVENÍ a jeden
    člen domácnosti CHYBĚL, protože odpověď skládal model z hlavy. Seznam
    přitom leží v `relationships`. Bez LLM; "" když store nic nedá.
    """
    # HANS_HOUSEHOLD_PRIVACY_V1 — složení domácnosti není veřejná informace.
    try:
        from scripts.cz_names import is_known_person
        if asker and not is_known_person(asker, config):
            _log.info("household_card: %r není známá osoba → odmítám", asker)
            return _PRIVACY_REFUSAL
    except Exception:
        pass
    try:
        from scripts.hans_relationships import Relationships
    except Exception as e:
        _log.debug("household_card: import (%s)", e)
        return ""
    try:
        cards = [c for c in (Relationships(config).all_cards() or [])
                 if (c.display_name or "").strip()]
    except Exception as e:
        _log.debug("household_card: store (%s)", e)
        return ""
    if not cards:
        return ""
    # Pořadí: pán/paní domu první, pak zbytek — ne náhodné z DB.
    def _rank(c):
        r = (c.role or "").lower()
        return (0 if "pán" in r else 1 if "paní" in r else 2, c.person_id)
    # ⚠️ ZÁMĚRNĚ `display_name`, NE `formal_name`: ten sáhne po plném jméně
    # z configu („Johana"), jenže doma se jí říká Jana — a kontrola
    # úplnosti v hlasovém kroku porovnává právě `display_name`, takže by si
    # věta s kontrolou protiřečila. Role („paní domu") titul stejně nese.
    parts = []
    for c in sorted(cards, key=_rank):
        nm = c.display_name
        parts.append("%s (%s)" % (nm, c.role) if c.role else nm)
    if len(parts) == 1:
        return "V domě žije %s." % parts[0]
    return "V domě žijí %s a %s." % (", ".join(parts[:-1]), parts[-1])


def household_card_voiced(db_path: str, config: dict, asker: str = "") -> str:
    """Totéž Hansovým hlasem, ale s KONTROLOU ÚPLNOSTI: když ve vyslovené
    verzi chybí něčí jméno, nepřijme se. Právě vynechaný člen domácnosti byl
    ta chyba, kvůli které tohle vzniklo — hezčí věta za cenu ztraceného
    člověka nestojí."""
    card = household_card(db_path, config, asker=asker)
    if not card or card == _PRIVACY_REFUSAL:
        return card   # odmítnutí se NEvyslovuje modelem, jde jak je
    try:
        from scripts.hans_relationships import Relationships
        names = [(c.display_name or "").strip()
                 for c in (Relationships(config).all_cards() or [])
                 if (c.display_name or "").strip()]
    except Exception:
        names = []
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return card
    except Exception:
        pass
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_core
        try:
            core = persona_core(config, with_address=False)
        except Exception:
            core = ""
        model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + (
            "Pán domu se ptá, KDO V DOMĚ ŽIJE. Odpověz jednou až dvěma větami "
            "svým hlasem. Vyjmenuj VŠECHNY osoby z faktů níže i s jejich rolí — "
            "nikoho nevynechej, nikoho nepřidávej a role neměň. Nepleť do "
            "výčtu oslovení toho, s kým mluvíš. Žádné odrážky.")
        out = ollama_generate(model, "FAKTA:\n" + card + "\n\nOdpověz.",
                              system=system, config=config, timeout=60)
        txt = (out or "").strip().strip('"')
        if txt and all(n in txt for n in names) and len(txt) >= 20:
            return txt[:600]
        if txt:
            _log.info("household_card_voiced: hlas vynechal jméno → karta")
    except Exception as e:
        _log.warning("household_card_voiced: %s", e)
    return card


# HANS_PERSON_ASK_PAT_V1 (18.8.) — ptá se věta NA OSOBU jako takovou?
# Dotazovací tvary, u kterých má smysl vrátit kartu. Sám o sobě NESTAČÍ —
# volající musí navíc mít ve větě známou osobu, jinak by karta vyskočila
# i na „co víš o hradech". A obráceně: jméno samo taky nestačí, jinak přeteče
# na konverzační věty („myslíš, že by Jana měla radost z kávovaru?" —
# doloženo živě 18.8.).
_PERSON_ASK_PAT = __import__("re").compile(
    r"(kdo\s+(je|to\s+je|byl|byla)|"
    r"co\s+v[íi][šs]\s+o|co\s+o\s+(n[ěe]m|n[íi]|nich)\s+v[íi][šs]|"
    r"co\s+je\s+za[čc]|zn[áa][šs]|[řr]ekni\s+mi\s+o|pov[ěe]z\s+mi\s+o|"
    r"[řr]ekni\s+mi\s+n[ěe]co\s+o|co\s+mi\s+[řr]ekne[šs]\s+o)",
    __import__("re").IGNORECASE)


def asks_about_person(query: str, config: dict) -> bool:
    """True = věta jmenuje známou osobu A ptá se na ni. Obě podmínky musí
    platit současně — viz komentář u `_PERSON_ASK_PAT`."""
    q = (query or "").strip()
    if not q or not _PERSON_ASK_PAT.search(q):
        return False
    try:
        from scripts.cz_names import find_known_person
        if find_known_person(q, config):
            return True
    except Exception:
        pass
    # osoba z Hansova čtení (Bud Spencer) — rozhodne až `person_card`
    return True


# ── HANS_DATETIME_ANSWER_V1 (19.8.) — kolikátého je / kolik je hodin ─────────
# Datum a čas jsou ŽIVÝ STAV jako počasí nebo co běží na TV — patří mezi
# deterministické odpovědi, ne k modelu. Dnešní řetěz byl jinak absurdní:
# model dostane správné datum v kontextu, přesto ho rozepíše špatně („sobota,
# patnáctého srpna roku dvoutisíc šestého" místo středy 19. 8. 2026), A1
# self-consistency to pozná jako nestabilní — a Hans ABSTINUJE na otázku,
# jejíž odpověď má přímo před sebou. Doloženo 19.8. v testu očima cizího člověka.
_DATE_ASK = __import__("re").compile(
    r"(kolik[áa]t[ée]ho\s+(je|m[áa]me)|jak[ée]\s+je\s+dnes\s+datum|"
    r"jak[ýy]\s+je\s+dnes(ka)?\s+den|co\s+je\s+dnes\s+za\s+den|"
    r"jak[ée]\s+m[áa]me\s+datum|kter[ýy]\s+je\s+dnes\s+den)",
    __import__("re").IGNORECASE)
_TIME_ASK = __import__("re").compile(
    r"(kolik\s+(je|m[áa]me)\s+hodin|kolik\s+je\s+ted|kolik\s+je\s+te[ďd])",
    __import__("re").IGNORECASE)
_DNY_CZ = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")


def datetime_answer(query: str) -> str:
    """Deterministická odpověď na dotaz po datu / čase. "" = není to on.

    Datum i čas se vrací ROVNOU SLOVY (`cz_numbers`), aby to sedělo i pro
    hlasový výstup — model si číslice rozepsat neumí.
    ⚠️ Omezeno na KRÁTKÉ dotazy: „kolik je hodin práce před námi?" má být
    normální hovor, ne výpis hodin.
    """
    q = (query or "").strip()
    if not q or len(q.split()) > 7:
        return ""
    want_date = bool(_DATE_ASK.search(q))
    want_time = bool(_TIME_ASK.search(q))
    if not (want_date or want_time):
        return ""
    import datetime as _dt
    now = _dt.datetime.now()
    try:
        from scripts.cz_numbers import normalize as _n
        d_words = _n(f"{now.day}.{now.month}.{now.year}").strip()
    except Exception:
        d_words = ""
    den = _DNY_CZ[now.weekday()]
    # čas taky slovy — `cz_numbers` to umí („09:31" → „devět hodin třicet jedna
    # minut"); číslice by hlasová syntéza přečetla špatně, což je celý důvod,
    # proč ten modul vznikl.
    try:
        from scripts.cz_numbers import normalize as _n2
        cas = _n2(now.strftime("%H:%M")).strip() or now.strftime("%H:%M")
    except Exception:
        cas = now.strftime("%H:%M")
    if want_date and not want_time:
        return ("Dnes je %s %s." % (den, d_words) if d_words
                else "Dnes je %s %d.%d.%d." % (den, now.day, now.month, now.year))
    if want_time and not want_date:
        return "Je %s." % cas
    return ("Dnes je %s %s, %s." % (den, d_words or "", cas)).replace("  ", " ")


# ── HANS_BOOK_RECOMMEND_V1 (19.8.) — doporučení z VLASTNÍ četby ─────────────
# Doloženo 19.8. (test očima cizího člověka): na „doporučte mi knihu" Hans
# vymyslel titul „Království z kamene" od Josefa Matějky včetně děje. V deníku
# o ní NIC, na Wikipedii neexistuje (autor ano, kniha ne).
# ⚠️ Příčina není „model rád fabuluje" — cesta pro doporučení knihy prostě
# NEEXISTOVALA, přestože Hans má 6 dočtených knih a 479 reflexí. Nedáváme sem
# brzdu, ale chybějící cestu; fabrikace tím ztrácí důvod.
_BOOK_ASK = __import__("re").compile(
    r"(doporu[čc]|tip)\w*\s+(mi\s+|n[ěe]jak\w+\s+|na\s+)*(kn[ií]\w+|[čc]etb\w+)"
    r"|co\s+(bych|si)\s+.{0,20}p[řr]e[čc][íi]st"
    r"|n[ěe]jak\w+\s+kn[ií]\w+\s+(na|k)\s+[čc]ten",
    __import__("re").IGNORECASE)


def asks_book_recommendation(query: str) -> bool:
    """Ptá se věta na DOPORUČENÍ KNIHY? (film sem NEpatří)"""
    q = (query or "").strip()
    if not q or len(q.split()) > 12:
        return False
    if __import__("re").search(r"\bfilm|seri[áa]l|po[řr]ad\b", q, __import__("re").IGNORECASE):
        return False
    return bool(_BOOK_ASK.search(q))


def book_recommendation(db_path: str, config: dict = None) -> str:
    """Doporučení z knih, které Hans DOČETL, i s tím, co si o nich zapsal.
    "" když nic nedočetl — pak ať odpoví běžná cesta, nic se nevyrábí."""
    import sqlite3
    try:
        with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                             timeout=3.0) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT title, ts FROM diary WHERE event_type='book_finished' "
                "ORDER BY ts DESC LIMIT 8").fetchall()
            if not rows:
                return ""
            import random
            pick = random.choice([dict(r) for r in rows])
            titul = (pick.get("title") or "").replace("Docetl:", "").strip()
            if not titul:
                return ""
            # vlastní reflexe k té knize (proč ho zaujala) — ne obsah z internetu
            refl = db.execute(
                "SELECT COALESCE(data, note) AS t FROM diary "
                "WHERE event_type='book_completion_reflection' "
                "AND COALESCE(title,'') LIKE ? ORDER BY ts DESC LIMIT 1",
                ("%" + titul + "%",)).fetchone()
    except Exception as e:
        _log.debug("book_recommendation: %s", e)
        return ""
    out = "Z toho, co jsem dočetl, bych doporučil „%s“." % titul
    t = (refl["t"] if refl else "") or ""
    if t:
        t = " ".join(t.split())
        out += " " + (t[:260] + ("…" if len(t) > 260 else ""))
    return out


# ── HANS_ASKER_STATE_V1 (19.8.) — otázky NA TAZATELE ────────────────────────
# Dva doložené rozpory z testu očima cizího člověka (19.8.):
#   „Vidíte mě?"   → „Ano, vidím vás."  … o dvě výměny později „Teď tu nikoho
#                     nevidím." (kamera nikoho neviděla — zdvořilost z hlavy)
#   „Kdo jsem já?" → „jste pán domu a hlava rodiny, s Janou vychováváte dceru"
#                     (řečeno NEZNÁMÉMU člověku — konfabulace i únik zároveň)
# Obojí je živý stav, ne úloha pro model: kdo je vidět, ví `_present_names`,
# kdo je kdo, vědí `known_persons` a `relationships`.
_SEES_ME = __import__("re").compile(
    r"\b(vid[íi][šs]\s+m[ěe]|vid[íi]te\s+m[ěe]|vid[íi][šs]\s+mne|"
    r"vid[íi]te\s+mne|kouk[áa][šs]\s+na\s+m[ěe]|zn[áa][šs]\s+m[ěe]j"
    r"|m[ůu][žz]e[šs]\s+m[ěe]\s+vid[ěe]t)\b", __import__("re").IGNORECASE)
_WHO_AM_I = __import__("re").compile(
    r"(kdo\s+jsem(\s+j[áa])?\b|v[íi][šs]\s+kdo\s+jsem|v[íi][ée]te\s+kdo\s+jsem"
    r"|pozn[áa]v[áa][šs]\s+m[ěe]|pozn[áa]v[áa]te\s+m[ěe]|zn[áa][šs]\s+m[ěe]\b"
    r"|zn[áa]te\s+m[ěe]\b)", __import__("re").IGNORECASE)


def asker_state_answer(query: str, asker: str, present_names, config: dict) -> str:
    """Deterministická odpověď na „vidíte mě?" / „kdo jsem já?". "" = není to on."""
    q = (query or "").strip()
    if not q or len(q.split()) > 8:
        return ""
    try:
        from scripts.cz_names import is_known_person, display_name
    except Exception:
        return ""
    known = bool(asker) and is_known_person(asker, config)
    disp = (display_name(asker, config) if known else (asker or "")).strip()

    if _SEES_ME.search(q):
        # „vidíš mě RÁD?" je otázka na vztah, ne na kameru (chyceno vlastním
        # protipříkladem při testu — vzor jinak odpověděl výpisem z kamery).
        if __import__("re").search(r"\br[áa]d[aoy]?\b", q, __import__("re").IGNORECASE):
            return ""
        names = [n for n in (present_names or [])
                 if n and n not in ("Unknown", "?", "")]
        me = [n for n in names
              if disp and n.strip().lower() == disp.strip().lower()
              or (asker and n.strip().lower() == asker.strip().lower())]
        if me:
            return "Ano, vidím vás, pane."
        if names:
            try:
                from scripts.cz_names import accusative as _acc
                vid = ", ".join(_acc(n, config) or n for n in names)
            except Exception:
                vid = ", ".join(names)
            return "Vás teď nevidím, pane — v místnosti vidím %s." % vid
        return "Teď tu nikoho nevidím, pane — kamera je prázdná."

    if _WHO_AM_I.search(q):
        if not known:
            # Žádné domýšlení identity a ŽÁDNÉ údaje o domácnosti
            # (táž hranice jako HANS_HOUSEHOLD_PRIVACY_V1).
            return ("Neznám vás, pane — ve svých záznamech vás nemám. "
                    "Rád se to dozvím, představíte-li se.")
        try:
            from scripts.hans_relationships import Relationships
            card = Relationships(config).get(str(asker).strip().lower())
        except Exception:
            card = None
        if card and card.role:
            return "Jste %s, %s." % (card.display_name or disp, card.role)
        return "Jste %s — znám vás ze svých záznamů." % (disp or asker)
    return ""
