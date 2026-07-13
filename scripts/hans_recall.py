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
import re
import sqlite3
import time
from datetime import datetime
from typing import Optional

_log = logging.getLogger(__name__)

_DNY = ("pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle")
_MESICE_GEN = ("", "ledna", "února", "března", "dubna", "května", "června",
               "července", "srpna", "září", "října", "listopadu", "prosince")

# Čtecí event typy (co Hans reálně četl/studoval)
_READ_TYPES = ("web_read", "reading_takeaway", "book_read", "study_note",
               "book_completion_reflection")


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

_TOPIC_PAT = re.compile(
    r"(?:četla?\s+(?:jsi|sis)?\s*(?:něco\s+)?o|kdy\s+(?:jsi|sis)\s+četla?\s+o?|"
    r"četla?\s+jsi)\s+(.{2,60}?)\s*\??$",
    re.IGNORECASE,
)
_STOPWORDS = {"něco", "neco", "dnes", "dneska", "včera", "vcera", "naposledy",
              "nějakou", "nejakou", "knihu", "článek", "clanek", "si", "už",
              "uz", "vůbec", "vubec", "někdy", "nekdy", "ty"}


def _extract_topic(question: str) -> str:
    """Vytáhni téma z dotazu na čtení ('četl jsi o hradech?' → 'hradech').
    '' když dotaz téma nemá (obecné 'co jsi četl')."""
    q = (question or "").strip()
    m = _TOPIC_PAT.search(q)
    if not m:
        # slash tvar: „/cetl o hradech" → args = „o hradech"
        m = re.match(r"^o\s+(.{2,60}?)\s*\??$", q, re.IGNORECASE)
    if not m:
        return ""
    words = [w for w in re.findall(r"[\wěščřžýáíéúůďťňó-]+", m.group(1))
             if w.lower() not in _STOPWORDS]
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
                rows = [r for r in cand
                        if _wb.search(" ".join(str(x) for x in r[2:] if x))
                        ][:limit]
                if rows:
                    break
            if not rows:
                return (f"Prošel jsem svůj deník, pane — o „{topic}“ v něm "
                        f"žádný záznam čtení nemám. Nebudu si vymýšlet; "
                        f"jestli chcete, mohu si o tom něco přečíst.")
            lines = []
            for ts, etype, title, snip in rows:
                t = (title or "").strip() or "(bez názvu)"
                line = f"– {_cz_date(ts)}: {t}"
                if snip:
                    line += f" — {str(snip).strip()}"
                lines.append(line)
            return (f"Ano, pane — tohle mám o „{topic}“ ve svém deníku "
                    f"skutečně zapsáno:\n" + "\n".join(lines))
        # bez tématu → poslední čtení
        rows = conn.execute(
            f"SELECT ts, event_type, title, "
            f"substr(COALESCE(NULLIF(data,''),note),1,120) "
            f"FROM diary WHERE event_type IN ({qmarks}) "
            f"ORDER BY ts DESC LIMIT ?",
            (*_READ_TYPES, limit)).fetchall()
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
    conn = None
    try:
        conn = _ro(db_path)
        rows = conn.execute(
            "SELECT ts FROM diary WHERE event_type='person_seen' "
            "AND lower(title) LIKE ? ORDER BY ts DESC LIMIT 40",
            (f"%{person.lower()}%",)).fetchall()
        if not rows:
            return (f"V deníku nemám žádný záznam, že bych osobu „{person}“ "
                    f"viděl, pane.")
        last = rows[0][0]
        # předchozí NÁVŠTĚVA = starší záznam oddělený > 1 h mezerou
        prev = None
        for (ts,) in rows[1:]:
            if last - ts > 3600:
                prev = ts
                break
        who = "vás" if person == asker else f"osobu {person}"
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
