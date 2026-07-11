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


def _resolve_day_reference(query: str, now: float):
    """Časová reference v dotazu → (start_ts, end_ts) daného dne, jinak None.
    „v pátek" → nejbližší MINULÝ pátek; „včera"/„dnes"/„předevčírem"."""
    q = (query or "").lower()
    dt_now = datetime.fromtimestamp(now)
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
    day = datetime.fromordinal(target)
    start = day.timestamp()
    return (start, start + 86400)


def conversation_recall(db_path: str, query: str, days: int = 30,
                        min_age_hours: float = 0.3, limit: int = 4) -> list:
    """Recall PŘEDCHOZÍHO rozhovoru (deterministicky). Když dotaz nese ČASOVOU
    referenci („v pátek"/„včera"), zúží se na TEN den (nezávisle na doslovných
    slovech) a v něm seřadí dle shody (se synonymy). Jinak čistě dle klíčových
    slov přes celé okno. Vynechá právě proběhlou výměnu (min_age_hours). Vrací
    [(kdy, note)] nebo []."""
    now = time.time()
    groups = _kw_groups(query)
    day_ref = _resolve_day_reference(query, now)
    try:
        conn = _ro(db_path)
        if day_ref:
            lo, hi = day_ref
            hi = min(hi, now - min_age_hours * 3600)
            rows = conn.execute(
                "SELECT ts, note FROM diary WHERE event_type='human_chat' AND "
                "ts>=? AND ts<=? ORDER BY ts DESC", (lo, hi)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, note FROM diary WHERE event_type='human_chat' AND "
                "ts>=? AND ts<=? ORDER BY ts DESC",
                (now - days * 86400, now - min_age_hours * 3600)).fetchall()
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


def is_recall_query(text: str) -> bool:
    """Ptá se uživatel na dřívější rozhovor? (pamatuješ / mluvili jsme / říkal jsi)"""
    t = (text or "").lower()
    return bool(re.search(
        r"pamatuj|vzpom[íi]n|mluvili\s+jsme|bavili\s+jsme|[řr][íi]kal\s+jsi|"
        r"co\s+jsme|jsme\s+se\s+bavili|zm[íi]nil\s+jsi|navrh\w*\s+jsi|"
        r"[řr]ekl\s+jsi", t))


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
