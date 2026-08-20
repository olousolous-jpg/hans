"""HANS_AGENT_V1 — agentní vrstva: kontextové akce z konverzace.

Uživatel v chatu napíše „je tu tma" a Hans podle KONTEXTU pozná, že by
ocenil rozsvícení, a NAVRHNE akci („rozsvítím ti obývák? [ano/ne]") — ne
jen odpoví textem. JEDNA zastřešující vrstva pro vše: 1 LLM router, 1
whitelist akcí, 1 confirm smyčka, 1 deník. Přidat akci = přidat 1 spec.

Tok (v `openwebui_direct_handler.send_chat_message`, PO parse_command):
  1. pending confirm? (osoba má návrh + řekne ano/ne) → proveď / zruš
  2. pre-gate (deterministické hinty) → věrohodně akce? jinak běžný chat
  3. LLM router (hans-czech, rezidentní) → {action,args,confidence,propose_text}
  4. validace: whitelist + práh + cooldown + anti-echo + grounding argumentů
  5. návrh (propose_text) jako Hansova odpověď + ulož pending → čeká na ano

Pojistky (proti otravování + konfabulaci akcí, vzor anti-konfab principu):
  - whitelist strict (router nezná nic mimo něj)
  - vždy confirm (human-in-the-loop jako Severka)
  - confidence práh (pod → mlčí, běžný chat)
  - cooldown per (akce, args) + anti-echo po odmítnutí
  - grounding argumentů (film jen z knihovny; neznámý → nenavrhne)
  - jen aktivní konverzace (send_chat_message = někdo píše, ne do prázdna)
  - deník `agent_action` (návrh + výsledek → pozdější tuning)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_YES = {"ano", "jo", "jasně", "jasan", "pusť", "pust", "spusť", "spust",
        "dej", "davej", "dávej", "ok", "okej", "tak jo", "prosím", "prosim",
        "sure", "yes", "můžeš", "muzes", "do toho", "platí", "plati", "beru"}
# PC_SHUTDOWN_DEFER_V1 — TŘETÍ odpověď na potvrzení: „ne teď, ale až dodělá".
# Doloženo 11.8. 23:07: „ne, můžeš vypnout až doděláš co je rozpracováno" se
# vyhodnotilo jako nový požadavek (začíná „ne" a má 8 slov) a propadlo do chatu.
# Proto se hledá VZOR KDEKOLI ve větě, ne krátká odpověď — formulace odložení
# je z podstaty delší než „ano"/„ne".
# HANS_AGENT_ACTION_VERBS_V1 (12.8.) — brána znala slovesa MLUVENÍ (popis,
# řekni, ukaž, pověz), ale ne slovesa KONÁNÍ — přitom agent je právě od akcí.
# Změřeno na 465 reálných zprávách: doplnění pustí 77 dalších a VŠECH 77 jsou
# skutečné povely, ani jeden běžný hovor. Většinu z nich stejně odchytí dřív
# `parse_command` (běží PŘED agentem), takže reálný dopad je malý a bezpečný —
# jde hlavně o povely, na které chatový příkaz není (připomeň, probuď, běž).
_ACTION_VERBS = {
    "vypni", "zapni", "namaluj", "nakresli", "probud", "probuď",
    "pripomen", "připomeň", "zkus", "pust", "pusť", "spust", "spusť",
    "otevri", "otevři", "zavri", "zavři", "jdi", "bez", "běž",
    "hlidej", "hlídej", "nastav", "posli", "pošli", "zjisti", "najdi",
    "zapamatuj", "uloz", "ulož", "prestan", "přestaň", "poznamenej",
    # HANS_ACTION_VERBS_WRITE_V1 (20.8.) — synonyma pro ZÁPIS chyběla, i když
    # „poznamenej" a „zapamatuj" tu byly. Doloženo: „z dnešního hovoru si
    # ZAPIŠ, že sis vymyslel to divadlo" agent neviděl a odpověď obstaral
    # volný hovor — Hans prohlásil, že si zápis udělal, a nezapsalo se nic.
    "zapis", "zapiš", "zaznamenej", "poznac", "poznač",
}

_LATER_PAT = re.compile(
    r"a[žz]\s+(to\s+)?(dod[ěe]l[áa]|dokon[čc][íi]|skon[čc][íi]|dojede|"
    r"bude[šs]\s+hotov|bude\s+hotov[oý]?|p[řr]estane)"
    r"|po\s+dokon[čc]en[íi]|a[žz]\s+to\s+dob[ěe]hne"
    r"|a[žz]\s+dod[ěe]l[áa][šs]|a[žz]\s+skon[čc][íi][šs]", re.I)

_NO = {"ne", "nech", "nechci", "nemusíš", "nemusis", "raději ne", "radeji ne",
       "zruš", "zrus", "ne díky", "ne diky", "nedávej", "nedavej", "no",
       "later", "teď ne", "ted ne", "ne teď", "ne ted"}


def _norm(s: str) -> str:
    return (s or "").strip().lower().strip("!?.,")


# HANS_AGENT_NO_WORDGATE_V1 — strukturální (gramatické) začátky otázek/žádostí.
# NENÍ to seznam spouštěcích slov akcí (to dělá LLM router), jen hrubý signál
# „tohle je dotaz/žádost, ať se Hans zamyslí“. Diakritika i bez ní.
_REQUEST_OPENERS = frozenset({
    # tázací zájmena/příslovce
    "co", "kdo", "kde", "kdy", "kolik", "jak", "proc", "proč", "kam", "odkud",
    "ci", "čí", "jaka", "jaky", "jake", "jaká", "jaký", "jaké", "která", "ktery",
    "který", "které", "kterou",
    # sloveso „být“ na začátku = zjišťovací otázka („je někdo doma“, „jsou tu…“)
    "je", "jsou", "byl", "byla", "bylo",
    # 2. osoba / zdvořilá žádost
    "muzes", "můžeš", "muzeš", "mohl", "mohla", "mohls", "dokazes", "dokážeš",
    "zvladnes", "zvládneš", "prosim", "prosím", "chci", "chtel", "chtěl",
    "potreboval", "potřeboval",
    # HANS_AGENT_IMPERATIVE_OPENERS_V1 (7.8.) — ROZKAZY. Docstring
    # `_looks_like_request` je sliboval od začátku („tázací/rozkazovací
    # začátek"), ale v množině nebyly → „popis co se deje doma" se k routeru
    # vůbec nedostalo a odpovídal model z hlavy (vymyslel si režim spánku
    # a bezpečnostní kamery). Imperativ dosud prošel JEN tam, kde ho někdo
    # vypsal do hintů konkrétní akce — přesně ten seznam slov, který měl
    # HANS_AGENT_NO_WORDGATE_V1 odstranit.
    # ⚠️ Držet u sloves typu „sděl mi / ukaž mi", ne u obecných rozkazů —
    # brána jen POUŠTÍ k routeru, ale širší brána = víc šancí na únos
    # běžného hovoru (vzor HANS_AGENT_SOCIAL_GUARD).
    "popis", "popiš", "rekni", "řekni", "povez", "pověz", "ukaz", "ukaž",
    "vypis", "vypiš", "zjisti", "shrn", "shrň", "povidej", "povídej",
    "najdi", "zkontroluj", "mrkni", "koukni", "spocitej", "spočítej",
})


def _args_hash(action: str, args: dict) -> str:
    raw = action + "|" + json.dumps(args or {}, sort_keys=True,
                                    ensure_ascii=False)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


# HANS_CHAT_STUDY_BRIDGE_GUARD — recall-fráze ("řekni mi TEĎ, co víš"), které
# NIKDY nejsou pokyn ke studiu (i když router splete "víc" a "si"). Chrání
# odpovědní/film-recall cestu před únosem do add_study_topic.
_RECALL_PAT = re.compile(
    r"zjisti[t]?\s+v[ií]c|co\s+v[ií][šs]|[řr]ekni\s+mi\s+o|"
    # HANS_STUDY_KNOWN_TOPIC_V1 (6.8.) — formulace, které guard minul a Hans
    # na ně nabídl studium tématu, jež už má odškrtnuté (5× 08:48–09:06).
    # „mi/me/mě" — uživatel píše bez diakritiky a s překlepy („co ME muzes")
    r"co\s+(mi|me|m[ěe])?\s*m[ůu][žz]e[šs]\s+[řr][íi]c|"
    r"pov[ěe]z\s+mi|[řr]ekni\s+(mi\s+)?co|co\s+v[íi]te\s+o|"
    r"jen\s+[řr]ekni|nezji[šs][ťt]uj|nestuduj|"
    # HANS_AGENT_SOCIAL_GUARD_V1 (4.8.) — „kdo je X?" je dotaz TEĎ, ne pokyn
    # ke studiu. Doloženo testem: „kdo je Bud Spencer?" → router navrhl
    # add_study_topic (conf 0.90) místo odpovědi.
    r"kdo\s+(je|byl|to\s+je)\b|"
    r"zn[áa][šs]\b|pamatuje[šs]", re.IGNORECASE)


# HANS_FILM_RECOMMEND_V1 — žádá věta o DOPORUČENÍ (ne o spuštění)?
def _asks_recommendation(message: str) -> bool:
    """True = „doporuč mi film", „co bys vybral", „máš tip" — konverzační
    žádost o názor. Rozkaz s názvem („pusť Kruh") sem NEspadá."""
    import re as _re, unicodedata as _ud
    msg = "".join(c for c in _ud.normalize("NFKD", (message or "").lower())
                  if not _ud.combining(c))
    if not msg:
        return False
    return bool(_re.search(
        r"(doporuc|dopurac|co\s+bys\s+(mi\s+)?(vybral|navrhl|doporucil)|"
        r"mas\s+(nejaky\s+)?tip|na\s+co\s+se\s+(mam|mame)\s+podivat|"
        r"co\s+bych\s+si\s+mel\s+pustit|co\s+navrhujes|"
        r"co\s+stoji\s+za\s+(to\s+)?(videni|shlednuti))", msg))


# HANS_OPINION_NOT_ORDER_V1 (19.8.) — ptá se věta na NÁZOR, ne o akci?
# Doloženo živě 18.8.: „myslíš, že by Kláru bavil ten film o Bondovi?" → agent
# navrhl film PUSTIT (conf 0.70). Prompt routeru přitom UŽ obsahuje „akci zvol
# JEN u jasného pokynu" i „buď konzervativní" — nezabralo, takže další věta do
# promptu je prompt debt ([[prompt-debt-tool-calling]]) a rozhoduje se to tady.
_OPINION_PAT = re.compile(
    r"\b(mysl[íi][šs]|co\s+bys\s+[řr]ekl|co\s+ty\s+na|co\s+[řr][íi]k[áa][šs]\s+na|"
    r"bavilo\s+by|l[íi]bilo\s+by|m[ěe]l[aoy]?\s+by\s+radost|co\s+soud[íi][šs]|"
    r"p[řr]ipad[áa]\s+ti|zd[áa]\s+se\s+ti)\b", re.IGNORECASE)
# ⚠️ BEZ TOHOHLE by veto zabilo i ZDVOŘILOU ŽÁDOST („myslíš, že bys mohl pustit
# toho Bonda?"), která dnes funguje správně (conf 0.95). Proto musí platit OBOJÍ:
# názorový tvar A ZÁROVEŇ žádná žádost směrem k Hansovi.
_REQUEST_TO_HANS = re.compile(
    r"\b(bys\s+mohl|bys\s+mohla|m[ůu][žz]e[šs]|mohl\s+bys|mohla\s+bys|"
    r"pus[ťt]|zapni|vypni|p[řr]idej|nastuduj|ud[ěe]lej|za[řr]i[ďd]|spus[ťt]|"
    r"dej|ho[ďd]|p[řr]ehraj|zastav|pozastav)\b", re.IGNORECASE)


def _asks_opinion(message: str) -> bool:
    """True = věta se ptá na NÁZOR a nic po Hansovi nechce."""
    msg = message or ""
    if not _OPINION_PAT.search(msg):
        return False
    return not _REQUEST_TO_HANS.search(msg)


# HANS_CAP_QUESTION_NOT_ORDER_V1 (19.8.) — dotaz „UMÍŠ to?" není rozkaz.
# Doloženo testem očima cizího člověka (08:47): „umíte pustit něco na
# televizi?" → agent zvolil kodi_play_film a `titul` si vzal z HISTORIE
# (Pelíšky, tři výměny zpět — router dostává posledních N výměn jako
# kontext). Hans pak odmítal pustit film, na který se nikdo neptal.
# Táž třída jako HANS_FILM_RECOMMEND_V1 (titul z živého stavu), proto TÝŽ
# způsob: potlačit akci deterministicky, ne dopisovat větu do promptu.
_CAP_QUESTION_PAT = re.compile(
    r"\b(um[íi][šs]|um[íi]te|dok[áa][žz]e[šs]|dok[áa][žz]ete|"
    r"zvl[áa]dne[šs]|zvl[áa]dnete)\b", re.IGNORECASE)


def _asks_capability(message: str, args=None) -> bool:
    """True = věta se PTÁ, jestli to Hans umí, a NEjmenuje předmět akce.

    Předmět (`titul`) si router doplňuje i z kontextu — a právě to je ten
    bug. Když ale předmět ve VĚTĚ je („umíš pustit Pelíšky?"), je to
    legitimní zdvořilá žádost a projde beze změny.
    """
    msg = message or ""
    if not _CAP_QUESTION_PAT.search(msg):
        return False
    import unicodedata as _ud

    def _fold(s):
        return "".join(c for c in _ud.normalize("NFKD", (s or "").lower())
                       if not _ud.combining(c))

    m = _fold(msg)
    for v in (args or {}).values():
        v = _fold(str(v or "")).strip()
        if v and v in m:
            return False        # předmět je ve větě → skutečná žádost
    return True


# HANS_PRESENCE_ASK_V1 — ptá se věta na PŘÍTOMNOST konkrétní známé osoby?
_PRESENCE_PAT = None


def _asks_person_presence(message: str, config: dict) -> bool:
    """True = věta jmenuje známou osobu z `known_persons` a ptá se, kde je
    nebo jestli je doma. Deterministické: jména i pády bere z configu,
    diakritiku skládá pryč, takže „janu"/„Jana"/„jana" sedí stejně."""
    import re as _re, unicodedata as _ud

    def _fold(s):
        return "".join(c for c in _ud.normalize("NFKD", (s or "").lower())
                       if not _ud.combining(c))

    msg = _fold(message)
    if not msg:
        return False
    # HANS_PERSON_CARD_V1 (18.8.) — hledání jména se přesunulo do
    # `cz_names.find_known_person`, ať ho sdílí s dotazem „kdo je X?“
    # (`hans_recall.person_card`). Dvě kopie by se dřív nebo později rozešly.
    try:
        from scripts.cz_names import find_known_person as _fkp
        if not _fkp(message, config):
            return False
    except Exception:
        return False
    return bool(_re.search(
        r"(doma|je\s+tu|je\s+tady|jsou\s+tu|jsou\s+tady|kde\s+je|kde\s+jsou|"
        r"videl|vidis|vidite|dorazil|prisel|prisla|je\s+pryc|odesel|odesla)", msg))


def _looks_like_recall(message: str) -> bool:
    return bool(_RECALL_PAT.search(message or ""))


# ── Registr akcí (whitelist) ─────────────────────────────────────────────────
# Každá akce: hints (pre-gate slova), args (co router vyplní), needs_confirm,
# cooldown_s, grounding(handler,args)->(ok,resolved_args,msg), run(handler,args)
# ->str. grounding=None → argumenty se neověřují (bez-argumentové akce).

class Action:
    def __init__(self, aid, desc, hints, args, run,
                 grounding=None, needs_confirm=True, cooldown_s=60,
                 reject_text=None):
        # HANS_AGENT_SPEAK_REJECT_V1 (5.8.) — když grounding akci ODMÍTNE,
        # `propose` dosud vrátil None = agent mlčky ustoupí a odpověď doskládá
        # persona. Ta pak POTVRDÍ akci, která se nestala. Doloženo 5.8. 11:16:
        # „kodi_play_film grounding zamítl (film není v knihovně)" a Hans přesto
        # řekl „Rozumím, pane. Připravuji přehrání filmu Kruh" — uživatel pak
        # řešil, jak zastavit horor, který nikdy nenaběhl. Konfabulace AKCE je
        # horší než konfabulace faktu: uživatel podle ní jedná.
        # `reject_text` = věta, kterou Hans řekne MÍSTO ticha ({titul} se doplní).
        self.id = aid
        self.desc = desc          # popis pro LLM router
        self.hints = hints        # pre-gate klíčová slova (lower, bez diakr.)
        self.args = args          # jména argumentů
        self.run = run            # (handler, args) -> str
        self.grounding = grounding
        self.needs_confirm = needs_confirm
        self.cooldown_s = cooldown_s
        self.reject_text = reject_text


# ── Handlery akcí ────────────────────────────────────────────────────────────

def _run_kodi_play(handler, args) -> str:
    m = args.get("_movie") or {}
    mid, title = m.get("movieid"), m.get("title", args.get("titul", ""))
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi or mid is None:
        return "Bohužel se mi teď nedaří k přehrávači připojit, pane."
    ok = kodi.play_movie(mid)
    return (f"Pouštím „{title}“." if ok
            else "Nepodařilo se mi film spustit, pane.")


# ── HANS_KODI_ALT_TITLE_V1 (5.8.) — český název → název v knihovně ───────────
# Doloženo: uživatel chtěl „Kruh", Kodi film vede jako „Ring" (originál リング).
# Lexikálně se to netrefí NIKDY — chybí překlad distribučního názvu.
# Deterministická cesta (bez LLM, tedy bez rizika výmyslu):
#   „Kruh" → cs.wikipedia prefixsearch „Kruh (film" → „Kruh (film, 2002)"
#          → langlink EN „The Ring (2002 film)" → ořízni závorku → „The Ring"
#          → find_movie („The Ring" ⊂ „Ring") → TREFA
# Kandidát musí ZAČÍNAT dotazem a mít v titulu „(film" — jinak prefixsearch
# přihodí i „Kouř (film)" nebo „Kruh u Jilemnice".
def _alt_titles(title: str, lang: str = "cs", limit: int = 6) -> list:
    """Alternativní (originální/anglické) názvy filmu. [] když nic."""
    import re as _re
    import unicodedata as _ud
    try:
        import requests as _rq
    except Exception:
        return []

    def _fold(x):
        x = _ud.normalize("NFKD", (x or "").lower())
        return "".join(c for c in x if not _ud.combining(c)).strip()

    q = (title or "").strip()
    if not q:
        return []
    api = "https://%s.wikipedia.org/w/api.php" % lang
    # HANS_WIKI_THROTTLE_CALLERS_V1 — sdílená kvóta, viz web_reader._get.
    hdr = {"User-Agent": "HansBot/1.0 (+https://github.com/olousolous-jpg/hans)"}
    try:
        from scripts import _wiki_throttle as _wt
        _wt.acquire(api)
    except Exception as e:
        log.debug("alt_titles: throttle (%s)", e)
        return []
    try:
        r = _rq.get(api, params={"action": "query", "list": "prefixsearch",
                                 "pssearch": "%s (film" % q, "pslimit": limit,
                                 "psnamespace": 0, "format": "json"},
                    headers=hdr, timeout=12).json()
        cands = [x["title"] for x in r.get("query", {}).get("prefixsearch", [])]
    except Exception as e:
        log.debug("alt_titles prefixsearch: %s", e)
        return []
    qf = _fold(q)
    cands = [c for c in cands if _fold(c).startswith(qf) and "(film" in _fold(c)]
    out = []
    for c in cands:
        try:
            rr = _rq.get(api, params={"action": "query", "prop": "langlinks",
                                      "lllimit": 50, "titles": c,
                                      "redirects": 1, "format": "json"},
                         headers=hdr, timeout=12).json()
        except Exception:
            continue
        for _, pg in (rr.get("query", {}).get("pages", {}) or {}).items():
            for ll in pg.get("langlinks", []):
                if ll.get("lang") not in ("en", "sk", "de"):
                    continue
                t = _re.sub(r"\s*\([^)]*\)\s*$", "", ll.get("*", "")).strip()
                if t and t.lower() != q.lower() and t not in out:
                    out.append(t)
    return out


def _reject_kodi_play(handler, args) -> str:
    """HANS_KODI_OUTSIDE_LIB_V1 (5.8.) — film NENÍ v knihovně (ani pod
    alternativním názvem). Místo holého „nemám" řekni, CO to je — Hans o
    filmech čte, tak ať to není němá zeď. Zdroj = Wikipedia (grounded, žádný
    výmysl); když ani ta nic nemá, přizná se to.
    Backlog „Film mimo knihovnu — web search" (14.7.), bod 1 a 3."""
    title = (args.get("titul") or "").strip() or "ten film"
    gloss = ""
    try:
        from scripts.web_reader import WebReader
        w = WebReader(getattr(handler, "config", {}) or {})
        art = None
        for q in ("%s (film)" % title, title):
            art = w.wikipedia_article(q, lang="cs", max_chars=700)
            if art and (art.get("text") or "").strip():
                break
        if art:
            from scripts.hans_entities import _first_sentence
            g = _first_sentence(art.get("text") or "")
            # rozcestník = k ničemu (doloženo „Kruh" → „Kruh může být…")
            import re as _re
            if g and not _re.search(r"m[uů][žz]e b[ýy]t|rozcestn[íi]k", g, _re.I):
                gloss = g.strip()
    except Exception as e:
        log.debug("reject_kodi_play wiki: %s", e)
    base = ("Film „%s\" v knihovně nemám, pane — a nechci předstírat, "
            "že ho pouštím." % title)
    if gloss:
        return base + (" Vím o něm aspoň tolik: %s" % gloss[:300])
    return base + (" Kodi ho možná vede pod jiným názvem; zkuste mi ho říct "
                   "tak, jak je uložený.")


def _ground_kodi_play(handler, args):
    title = (args.get("titul") or "").strip()
    if not title:
        return False, args, "bez názvu"
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return False, args, "kodi nedostupné"
    m = kodi.find_movie(title)
    if not m:
        # HANS_KODI_ALT_TITLE_V1 — zkus originální/anglický název („Kruh"→„Ring")
        for alt in _alt_titles(title):
            m = kodi.find_movie(alt)
            if m:
                log.info("agent: '%s' nalezen pod alternativním názvem '%s' → '%s'",
                         title, alt, m.get("title"))
                break
    if not m:
        return False, args, "film není v knihovně"
    args["_movie"] = m
    args["titul"] = m.get("title", title)  # kanonický název z knihovny
    return True, args, ""


def _run_sleep(handler, args) -> str:
    hi = getattr(handler, "_hans_idle", None)
    rt = getattr(hi, "_routine", None) if hi else None
    if not rt or not hasattr(rt, "set_manual_sleep"):
        return "Uspat se teď neumím, pane."
    try:
        rt.set_manual_sleep(True)
        return "Dobře, ztiším se a odpočinu si. Kdykoli mě probuďte."
    except Exception:
        return "Uspání se nezdařilo, pane."


def _run_book_wishlist(handler, args) -> str:
    title = (args.get("titul") or "").strip()
    if not title:
        return "Který titul mám přidat, pane?"
    try:
        from scripts.hans_art import add_to_wishlist
        dbp = _diary_path(handler)
        res = add_to_wishlist(dbp, title)
        if res == "exists":
            return f"„{title}“ už na svém seznamu ke čtení mám."
        return f"Přidal jsem „{title}“ na seznam ke čtení."
    except Exception as e:
        log.warning("book_wishlist: %s", e)
        return "Přidání na seznam se nezdařilo, pane."


def _ground_book(handler, args):
    title = (args.get("titul") or "").strip()
    if len(title) < 2:
        return False, args, "bez názvu"
    return True, args, ""


# ── Info dotazy (instant, bez potvrzení — jen odpoví z živých dat) ───────────

def _run_weather(handler, args) -> str:
    try:
        from scripts.weather_chmu import WeatherCHMU
        _w = (getattr(handler, "config", {}) or {}).get("weather", {}) or {}
        s = WeatherCHMU(lat=float(_w.get("lat", 50.08)),
                        lon=float(_w.get("lon", 14.42))).get_context_string()
        return s.replace("Počasí:", "Za oknem:").strip() if s else \
            "Aktuální počasí se mi teď nedaří zjistit, pane."
    except Exception:
        return "Aktuální počasí se mi teď nedaří zjistit, pane."


def _run_pc_health(handler, args) -> str:
    try:
        from scripts import pc_remote
        lines = pc_remote.display_lines(handler.config)
        if not lines:
            return "Počítač je teď nedostupný — nejspíš spí, pane."
        return "Počítač: " + ", ".join(lines) + "."
    except Exception:
        return "Stav počítače se mi teď nedaří zjistit, pane."


def _run_household(handler, args) -> Optional[str]:
    """HANS_HOUSEHOLD_CARD_V1 — kdo v domě BYDLÍ (ne kdo je právě vidět)."""
    cfg = getattr(handler, "config", {}) or {}
    _ar = getattr(handler, "_agent_inst", None) or getattr(handler, "_agent", None)
    _who = getattr(_ar, "_raw_name", "") or ""
    try:
        from scripts.hans_recall import household_card_voiced
        return household_card_voiced(
            cfg.get("diary_db", "data/hans_diary.db"), cfg, asker=_who) or None
    except Exception as e:
        log.debug("report_household: %s", e)
        return None


def _run_person_info(handler, args) -> Optional[str]:
    """HANS_PERSON_CARD_ACTION_V1 — „kdo je X?“ / „co víš o X?“ z DETERMINISTICKÝCH
    úložišť (`relationships`, pak `entities`). Prázdno → vrať None, ať odpoví
    běžná cesta; NIC se nedomýšlí (C4, 7.8.: fakt ležel v DB, ale dotaz šel do
    RAG a o odpovědi rozhodoval práh self-consistency)."""
    cfg = getattr(handler, "config", {}) or {}
    q = (args or {}).get("jmeno") or ""
    # Instance routeru visí na handleru jako `_agent_inst`
    # (`openwebui_direct_handler._agent_router`), NE `_agent` — na to jsem
    # při stavbě naletěl a surová věta nikdy nedorazila.
    _ar = getattr(handler, "_agent_inst", None) or getattr(handler, "_agent", None)
    raw = getattr(_ar, "_raw_message", "") or ""
    _who = getattr(_ar, "_raw_name", "") or ""   # HANS_HOUSEHOLD_PRIVACY_V1
    try:
        # HANS_PERSON_CARD_VOICE_V1 — hlasový krok; bez mozku spadne na kartu
        from scripts.hans_recall import person_card_voiced
        db = cfg.get("diary_db", "data/hans_diary.db")
        # napřed celá věta (nese pád), teprve pak samotné jméno z routeru
        return (person_card_voiced(db, raw, cfg, asker=_who)
                or person_card_voiced(db, q, cfg, asker=_who) or None)
    except Exception as e:
        log.debug("report_person: %s", e)
        return None


def _run_who_home(handler, args) -> str:
    hi = getattr(handler, "_hans_idle", None)
    names = [n for n in (getattr(hi, "_present_names", None) or [])
             if n and n not in ("Unknown", "?", "")]
    if names:
        # HANS_AGENT_NAME_CASE_V1 (5.8.) — jméno v AKUZATIVU a s velkým
        # písmenem: `_present_names` nese konfigurační klíč (malým, 1. pád), takže
        # Hans hlásil „Vidím tu jana." (1. pád místo 4.). Doloženo v chatu 12:54.
        # `cz_names` to umí od HANS_NAME_INFLECTION_V2 — jen se to tady
        # nepoužilo.
        try:
            from scripts.cz_names import accusative as _acc
            cfg = getattr(handler, "config", {}) or {}
            names = [_acc(n, cfg) or n for n in names]
        except Exception:
            pass
        if len(names) == 1:
            return f"Vidím tu {names[0]}."
        return "Vidím tu: " + ", ".join(names) + "."
    # fallback — nedávno spatření z deníku (posl. 15 min)
    try:
        import sqlite3
        db = sqlite3.connect(_diary_path(handler))
        r = db.execute(
            "SELECT title FROM diary WHERE event_type='person_seen' "
            "AND ts > ? ORDER BY ts DESC LIMIT 1",
            (time.time() - 900,)).fetchone()
        db.close()
        if r and r[0]:
            from scripts.cz_names import acc as _cz_acc  # HANS_NAME_INFLECTION_V2
            return f"Naposledy jsem tu zahlédl {_cz_acc(r[0])}, teď tu ale nikoho nevidím."
    except Exception:
        pass
    return "Teď tu nikoho nevidím, pane."


def _run_now_playing(handler, args) -> str:
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se teď nedaří připojit, pane."
    try:
        np = kodi.get_now_playing()
    except Exception:
        np = None
    if np and np.get("title"):
        t = np.get("title")
        yr = np.get("year")
        return f"Na TV právě hraje „{t}“{f' ({yr})' if yr else ''}."
    return "Na TV teď nic nehraje, pane."


def _run_kolac_status(handler, args) -> str:
    """KOLAC_STATUS_V1 — „co dělá Koláč?" z REÁLNÉHO stavu (poslední dialog),
    ne z domýšlení. Koláč = Hansův animatronický společník, NE osoba v domě
    (jinak router odpovídal home_status: „na TV hraje…, vidím Janu")."""
    from scripts.hans_kolac import kolac_name
    kn = kolac_name(getattr(handler, "config", {}) or {})
    ts = note = None
    try:
        import sqlite3
        db = sqlite3.connect(_diary_path(handler))
        r = db.execute("SELECT ts, note FROM diary WHERE "
                       "event_type='teddy_dialog' ORDER BY ts DESC LIMIT 1").fetchone()
        db.close()
        if r:
            ts, note = r
    except Exception:
        pass
    topic = ""
    if note:
        m = re.search(r"Téma:\s*(.+)", note)
        if m:
            topic = m.group(1).strip().splitlines()[0][:80]
    if topic:
        ago = time.time() - (ts or 0)
        if ago < 3600:
            return f"{kn} a já jsme se před chvílí bavili o „{topic}“, pane."
        return (f"{kn} zrovna tiše přemítá po mém boku; naposledy jsme rozprávěli "
                f"o „{topic}“.")
    return f"{kn} zrovna nic neprovádí, pane — tiše přemítá po mém boku."


def _run_home_status(handler, args) -> str:
    """HANS_AGENT_HOME_STATUS_V1 — kompozitní odpověď na VÁGNÍ „děje se něco
    doma?“. Agreguje ŽIVÁ deterministická data (co hraje + kdo je doma), NIKDY
    nedomýšlí. Prázdný zdroj = „klid“, NE staré datum (přesně proti případu
    „Proud krve“, kde chat konfabuloval starý Kodi titul jako přítomnost)."""
    try:
        np = _run_now_playing(handler, {}) or ""
    except Exception:
        np = ""
    try:
        wh = _run_who_home(handler, {}) or ""
    except Exception:
        wh = ""
    _l = lambda s: s.lower()
    plays = bool(np) and "nic nehraje" not in _l(np) and "nedaří připojit" not in _l(np)
    home  = bool(wh) and "nikoho nevid" not in _l(wh)
    parts = []
    if plays:
        parts.append(np.rstrip("."))
    if home:
        parts.append(wh.rstrip("."))
    if not parts:
        return "Doma je klid, pane — na TV nic nehraje a nikoho tu teď nevidím."
    return ". ".join(parts) + "."


# ── Ovládání médií (confirm) ─────────────────────────────────────────────────

def _run_kodi_pause(handler, args) -> str:
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se nedaří připojit, pane."
    return "Pozastavuji." if kodi.pause_playback() else \
        "Pozastavení se nezdařilo, pane."


def _run_pc_wake(handler, args) -> str:
    """HANS_AGENT_ACTIONS_V3 — probuď PC magic packetem. Sdílí `pc_remote.wake`
    s mostem i noční rutinou (HANS_WOL_SHARED_V1) = jedna pravda o WOL."""
    cfg = getattr(handler, "config", {}) or {}
    mac = str(cfg.get("wol_pc_mac", "") or "")
    if not mac:
        return "Nemám nastavenou MAC adresu počítače, pane."
    try:
        from scripts import pc_remote
        if not pc_remote.wake(config=cfg, mac=mac):
            return "Probouzecí signál se nepodařilo odeslat, pane."
    except Exception as e:
        log.warning("pc_wake selhal: %s", e)
        return "Probuzení počítače se nezdařilo, pane."
    # Vědomě NEČEKÁME na náběh (~40 s) — agent musí odpovědět hned;
    # ověření naběhnutí dělá most (`_wol_verify`), když se ptá uživatel.
    return "Posílám počítači probouzecí signál, pane. Chvíli mu to potrvá."


def _run_hans_wake(handler, args) -> str:
    """HANS_AGENT_ACTIONS_V3 — probuď SEBE (opak `_run_sleep`). Týž setter,
    opačný stav, aby se ty dvě cesty nemohly rozejít."""
    hi = getattr(handler, "_hans_idle", None)
    rt = getattr(hi, "_routine", None) if hi else None
    if not rt or not hasattr(rt, "set_manual_sleep"):
        return "Probudit se teď neumím, pane."
    try:
        rt.set_manual_sleep(False)
        return "Jsem vzhůru, pane. K službám."
    except Exception:
        return "Probuzení se nezdařilo, pane."


def _run_kodi_resume(handler, args) -> str:
    """HANS_AGENT_ACTIONS_V3 — pokračovat v pozastaveném přehrávání."""
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se nedaří připojit, pane."
    try:
        ok = kodi.play()
    except Exception as e:
        log.warning("kodi_resume selhal: %s", e)
        ok = False
    return "Pouštím dál." if ok else "Nepodařilo se pokračovat, pane."


def _run_game_mode(handler, args) -> str:
    """HANS_AGENT_ACTIONS_V3 — herní mód zap/vyp. Deleguje na `_cmd_herni`
    (HANS_UNIFY_ACTIONS_V1 — chat i agent volají TÝŽ kód)."""
    mode = (args.get("mode") or "").strip().lower()
    arg = "vyp" if mode in ("off", "vyp", "stop", "ne", "konec") else "zap"
    try:
        from scripts.chat_commands import _cmd_herni
        return _cmd_herni(handler, None, arg)
    except Exception as e:
        log.warning("game_mode_toggle selhal: %s", e)
        return "Herní mód se teď přepnout nepodařilo, pane."


def _run_kodi_stop(handler, args) -> str:
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return "K přehrávači se nedaří připojit, pane."
    return "Zastaveno." if kodi.stop_playback() else \
        "Zastavení se nezdařilo, pane."


def _ground_playing(handler, args):
    """Pauza/stop dávají smysl jen když něco hraje."""
    kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
    if not kodi:
        return False, args, "kodi nedostupné"
    try:
        np = kodi.get_now_playing()
    except Exception:
        np = None
    if not np or not np.get("title"):
        return False, args, "nic nehraje"
    return True, args, ""


# ── Studijní téma z chatu (confirm) ──────────────────────────────────────────

def _run_add_study(handler, args) -> str:
    topic = (args.get("tema") or "").strip()
    if len(topic) < 2:
        return "Které téma mám nastudovat, pane?"
    try:
        from scripts.hans_study import add_pending_topic
        res = add_pending_topic(_diary_path(handler), topic)
        if res == "exists":
            return f"Téma „{topic}“ už mám ve studijním plánu."
        return (f"Dobře — „{topic}“ jsem si zařadil do studijního plánu. "
                f"Pustím se do něj, jakmile dokončím současné studium.")
    except Exception as e:
        log.warning("add_study: %s", e)
        return "Zařazení tématu se nezdařilo, pane."


def _ground_study(handler, args):
    t = (args.get("tema") or "").strip()
    return (len(t) >= 2, args, "" if len(t) >= 2 else "bez tématu")


# ── Poznámky / paměťové sliby (confirm, light) ───────────────────────────────

def _run_add_note(handler, args) -> str:
    text = (args.get("text") or "").strip()
    if len(text) < 2:
        return "Co mám poznamenat, pane?"
    # HANS_REMINDER_ADD_V1 — nese-li žádost ČAS, není to poznámka, ale
    # PŘIPOMÍNKA. Řeší se uvnitř add_note ZÁMĚRNĚ: druhá akce s překrývajícími
    # nápovědami („připomeň" je v hints obou) by nutila router rozhodovat mezi
    # dvěma skoro stejnými volbami a mýlil by se. Takhle rozhoduje DATA —
    # buď z textu vyjde termín, nebo ne — a splést se nemá kde.
    # Doloženo 12.8. 09:29: „připomeň mi, že mám provést měření dnes v 17:00"
    # skončilo jako odpověď o prázdném kalendáři a NIC se neuložilo.
    try:
        from scripts.hans_commitments import _parse_due, add_reminder
        if _parse_due(text) > 0:
            ok, say = add_reminder(_diary_path(handler),
                                   getattr(handler, "_last_person", "") or "",
                                   text, when_phrase=text)
            if ok:
                return say
    except Exception as _re:
        log.warning("add_reminder v add_note selhalo: %s", _re)
    try:
        import sqlite3
        db = sqlite3.connect(_diary_path(handler))
        db.execute(
            "CREATE TABLE IF NOT EXISTS hans_notes ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, "
            "done INTEGER NOT NULL DEFAULT 0)")
        db.execute("INSERT INTO hans_notes (ts, text, done) VALUES (?,?,0)",
                   (time.time(), text[:500]))
        db.commit()
        db.close()
        return f"Poznamenal jsem si: {text}"
    except Exception as e:
        log.warning("add_note: %s", e)
        return "Poznámku se nepodařilo uložit, pane."


def _ground_note(handler, args):
    t = (args.get("text") or "").strip()
    return (len(t) >= 2, args, "" if len(t) >= 2 else "prázdná poznámka")


# HANS_UNIFY_ACTIONS_V1 — /vypnipc a /hlidej žily jen jako regexy v
# chat_commands (paralelní vrstva vedle routeru). Tady dostávají agentní akci
# sdílející TÝŽ kód (deleguje na _cmd_*), aby router chytil i novní formulace,
# co regex mine, a shutdown dostal potvrzení. Regexy zůstávají jako rychlá,
# na mozku nezávislá cesta. (WOL zůstává deterministický — přes agenta by byl
# k ničemu: běží na PC, který WOL teprve zapíná.)
def _run_pc_shutdown(handler, args) -> str:
    from scripts.chat_commands import _cmd_vypnipc
    return _cmd_vypnipc(handler, None, "")


def _shutdown_confirm_text(handler) -> str:
    """HANS_SHUTDOWN_CONTEXT_V1 — potvrzení vypnutí PC s GROUNDED stavem, ať
    uživatel ví, co vypnutím ukončí.

    Stav se ptá `pc_deferred_shutdown.pc_busy` — JEDNA pravda pro potvrzení
    i pro odložené vypnutí, aby si ty dvě cesty neodporovaly (dřív měla každá
    vlastní práh a obě na TEPLOTU, která skutečnou práci míjela).
    """
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.pc_deferred_shutdown import pc_busy
        busy, why = pc_busy(cfg)
    except Exception:
        busy, why = False, ""
    if busy:
        return ("Než počítač vypnu, pane — %s. Vypnout, nebo počkat, "
                "až to dodělá?" % why)
    return ("Na počítači teď nevidím nic rozpracovaného, pane. "
            "Opravdu mám počítač vypnout?")


def _run_guard(handler, args) -> str:
    from scripts.chat_commands import _cmd_hlidej
    # _cmd_hlidej si záměr (zapni/vypni) přečte z řetězce sám.
    return _cmd_hlidej(handler, None, (args.get("mode") or "").strip().lower())


ACTIONS: dict[str, Action] = {
    "kodi_play_film": Action(
        "kodi_play_film",
        "Pustit konkrétní film na TV. Argument 'titul' = název filmu, který "
        "uživatel chce vidět (musí být v knihovně).",
        hints=["film", "pust", "pusť", "koukni", "podivat", "podívat",
               "smrtonosn", "bond", "sledovat", "na tv", "dej to", "spust"],
        args=["titul"], run=_run_kodi_play, grounding=_ground_kodi_play,
        needs_confirm=True, cooldown_s=300,
        reject_text=_reject_kodi_play),
    "hans_sleep": Action(
        "hans_sleep",
        "Uspat sebe (Hanse) — ztišit se, přestat mluvit. Když uživatel řekne "
        "ať jde spát / ať je ticho / dobrou noc s přáním klidu.",
        hints=["spát", "spat", "spi", "ztich", "ticho", "klid", "unaven",
               "dobrou noc", "jdi spat", "bež spát", "odpočin"],
        args=[], run=_run_sleep, grounding=None,
        needs_confirm=True, cooldown_s=120),
    "add_book_wishlist": Action(
        "add_book_wishlist",
        "Přidat knihu na seznam ke čtení. Argument 'titul' = název knihy, "
        "kterou uživatel zmíní že by si chtěl přečíst / ať přečteš.",
        hints=["kniha", "knihu", "přečíst", "precist", "číst", "cist",
               "na seznam ke", "wishlist", "knížk", "knizk"],
        args=["titul"], run=_run_book_wishlist, grounding=_ground_book,
        needs_confirm=True, cooldown_s=30),

    # ── Info dotazy (instant, bez potvrzení) ────────────────────────────────
    "report_weather": Action(
        "report_weather",
        "Odpovědět na dotaz o AKTUÁLNÍM počasí / jak je venku.",
        hints=["počasí", "pocasi", "venku", "prší", "prsi", "sněží", "snezi",
               "teplo venku", "zima venku", "za oknem", "slunečno"],
        args=[], run=_run_weather, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_pc_health": Action(
        "report_pc_health",
        "Odpovědět na dotaz o stavu POČÍTAČE (teploty, VRAM, RAM, zda běží).",
        hints=["počítač", "pocitac", "jak je na tom pc", "teplota pc",
               "vram", "kolik ram", "gpu", "grafick", "jak se má počítač"],
        args=[], run=_run_pc_health, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_household": Action(
        "report_household",
        "Odpovědět na dotaz, KDO V DOMĚ BYDLÍ / ŽIJE — složení domácnosti "
        "(kdo do ní patří a jakou má roli). ⚠️ NE kdo je právě doma nebo "
        "v místnosti TEĎ — to je report_who_is_home. Rozdíl: „kdo tu bydlí“ "
        "je trvalý stav, „kdo je doma“ je aktuální pozorování.",
        hints=["kdo tu bydli", "kdo tu žije", "kdo tu zije", "kdo v dome",
               "kdo v domě", "domacnost", "domácnost", "kdo sem patri"],
        args=[], run=_run_household, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_person": Action(
        "report_person",
        "Odpovědět na dotaz KDO JE konkrétní člověk / co o něm Hans ví / jaký "
        "je — identita a charakteristika osoby (člen domácnosti i osobnost "
        "z Hansova čtení). Patří sem i „co víš o <jméno>?“, „řekni mi o "
        "<jméno>“, „znáš <jméno>?“, „co je zač <jméno>?“ — tedy i tvary, kde "
        "jméno stojí v jiném pádě („o Janě“, „o Kláře“). "
        "⚠️ NE dotaz na PŘÍTOMNOST nebo polohu („je X doma?“, "
        "„kde je X?“, „kdo je doma?“) — ten patří report_who_is_home.",
        hints=["kdo je", "kdo to je", "co vis o", "co víš o", "co je zac",
               "co je zač", "znas", "znáš", "kdo byl", "rekni mi o"],
        args=["jmeno"], run=_run_person_info, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_who_is_home": Action(
        "report_who_is_home",
        "Odpovědět na dotaz o PŘÍTOMNOSTI osob TEĎ (koho Hans právě VIDÍ) — "
        "kdo je doma / v místnosti / "
        "kdo tu je, ale i „kde je <jméno>?“, „je <jméno> doma?“, „co dělá "
        "<jméno>?“ (odpověď = koho Hans právě VIDÍ; NEDOMÝŠLET činnost, jen "
        "přítomnost). Vyber TUTO akci u dotazů na aktuální polohu/přítomnost "
        "konkrétní osoby.",
        hints=["kdo je doma", "kdo je tu", "je někdo doma", "je nekdo doma",
               "kdo tu je", "někdo tu", "nekdo tu", "jsem sám", "jsem sam",
               "kdo tady", "kde je", "co dělá", "co dela"],
        args=[], run=_run_who_home, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_now_playing": Action(
        "report_now_playing",
        "Odpovědět na dotaz, co PRÁVĚ hraje na TV.",
        hints=["co hraje", "co běží", "co bezi", "co dávají", "co davaji",
               "co se přehrává", "co je na tv"],
        args=[], run=_run_now_playing, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_home_status": Action(
        "report_home_status",
        "Odpovědět na ŠIROKÝ/VÁGNÍ dotaz o dění doma jako celku — „děje se "
        "něco doma?“, „co se doma děje?“, „co je doma nového?“, „je doma "
        "všechno v pořádku?“, „jak to vypadá doma?“. Shrne živý stav (co hraje "
        "na TV + kdo je doma). Vyber TUTO akci místo report_now_playing/"
        "report_who_is_home, když dotaz NENÍ konkrétně jen o TV ani jen o "
        "přítomnosti, ale o dění doma obecně.",
        hints=["děje se něco", "deje se neco", "co se doma", "co je doma",
               "doma nového", "doma noveho", "vypadá to doma", "vypada to doma",
               "všechno v pořádku doma", "vsechno v poradku doma",
               "jak to doma", "co je doma nového", "něco nového doma"],
        args=[], run=_run_home_status, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "report_kolac_status": Action(
        "report_kolac_status",
        "Odpovědět na dotaz o KOLÁČOVI (Hansův animatronický společník/"
        "topinkovač, se kterým Hans rozmlouvá) — „co dělá Koláč?“, „co Koláč?“, "
        "„jak se má Koláč?“, „co říká Koláč?“, „nezlobí Koláč?“. Odpověď = co "
        "Koláč teď dělá (z posledního dialogu). Koláč NENÍ osoba v domě ani "
        "„dění doma“ — u dotazu o Koláčovi NIKDY nevybírej report_who_is_home "
        "ani report_home_status.",
        hints=["kolac", "kolač", "koláč", "co dela kolac", "nezlobi kolac"],
        args=[], run=_run_kolac_status, grounding=None,
        needs_confirm=False, cooldown_s=10),

    # ── Ovládání médií (confirm) ────────────────────────────────────────────
    "kodi_pause": Action(
        "kodi_pause",
        "Pozastavit (pauza) běžící film/přehrávání na TV.",
        hints=["pauza", "pauzni", "pozastav", "zastav to", "dej pauzu",
               "stopni", "stop na chvíli"],
        args=[], run=_run_kodi_pause, grounding=_ground_playing,
        needs_confirm=True, cooldown_s=15),
    "kodi_stop": Action(
        "kodi_stop",
        "Úplně zastavit (ukončit) běžící film/přehrávání na TV.",
        hints=["vypni film", "vypni to", "ukonči film", "ukonci film",
               "zastav film", "vypni tv", "konec filmu"],
        args=[], run=_run_kodi_stop, grounding=_ground_playing,
        needs_confirm=True, cooldown_s=15),

    # ── Studijní téma z chatu (confirm) ─────────────────────────────────────
    "add_study_topic": Action(
        "add_study_topic",
        "Zařadit téma ke studiu do hloubky. Argument 'tema' = co si uživatel "
        "přeje aby Hans nastudoval / prostudoval / naučil se.",
        # HANS_CHAT_STUDY_BRIDGE_V1 — „zjisti víc o" ZÁMĚRNĚ NENÍ hint
        # (patří k null/recall); studijní request = „zjisti si / podívej se".
        hints=["nastuduj", "prostuduj", "studuj", "nauč se", "nauc se",
               "zaměř se na", "zamer se na", "prozkoumej", "zjisti si",
               "zjistit si", "podívej se na", "podivej se na", "mrkni na"],
        args=["tema"], run=_run_add_study, grounding=_ground_study,
        needs_confirm=True, cooldown_s=30),

    # ── Poznámky / paměťové sliby (confirm, light) ──────────────────────────
    "add_note": Action(
        "add_note",
        "Přidat poznámku / úkol / položku na seznam. Argument 'text' = co si "
        "uživatel přeje poznamenat (nákup, připomínka, TODO).",
        hints=["poznamenej", "zapiš si", "zapis si", "na nákup", "na nakup",
               "nezapomeň", "nezapomen", "připomeň", "pripomen", "na seznam",
               "poznámka", "poznamka", "dej na seznam"],
        args=["text"], run=_run_add_note, grounding=_ground_note,
        needs_confirm=True, cooldown_s=10),

    # ── Ovládání PC + hlídání (mutace, confirm) ─────────────────────────────
    "pc_shutdown": Action(
        "pc_shutdown",
        "Vypnout stolní POČÍTAČ (PC) na povel — když uživatel jasně řekne ať "
        "vypneš/zhasneš počítač. NENÍ to uspání tebe (Hanse) — to je hans_sleep.",
        hints=["vypni pc", "vypni počítač", "vypni pocitac", "zhasni pc",
               "vypnout počítač", "vypnout pocitac", "vypni ten pocitac"],
        args=[], run=_run_pc_shutdown, grounding=None,
        needs_confirm=True, cooldown_s=60),
    "guard_toggle": Action(
        "guard_toggle",
        "Zapnout nebo VYPNOUT hlídací režim místnosti (při pohybu/změně světla "
        "pošle snímek na Telegram — pro hlídání prázdného domu). Argument "
        "'mode' = 'on' (zapni hlídání) nebo 'stop' (vypni hlídání). Volič u "
        "„hlídej dům/místnost“, „zapni hlídání“, „vypni hlídání“. Na pouhý "
        "DOTAZ na stav („hlídáš ještě?“) tuto akci NEvol.",
        hints=["hlídej", "hlidej", "hlídání", "hlidani", "stráž", "straz",
               "hlídat dům", "hlidat dum", "hlídej místnost", "zapni hlidani"],
        args=["mode"], run=_run_guard, grounding=None,
        needs_confirm=True, cooldown_s=15),

    # ── HANS_AGENT_ACTIONS_V3 (13.8.) — dosud chybějící protipóly ───────────
    # Brána rozkazy propouštěla (HANS_AGENT_NO_WORDGATE_V1), ale agent na ně
    # neměl akci → spadly do chatu a persona na ně jen odpověděla slovy.
    "pc_wake": Action(
        "pc_wake",
        "Probudit / zapnout stolní POČÍTAČ (PC) přes síť (Wake-on-LAN) — když "
        "uživatel řekne ať zapneš/probudíš/nahodíš počítač. OPAK akce "
        "pc_shutdown; nezaměňuj s ní („vypni pc\u201c = pc_shutdown).",
        hints=["zapni pc", "zapni počítač", "zapni pocitac", "probuď pc",
               "probud pc", "nahoď pc", "nahod pc", "nastartuj pc",
               "zapni ten pocitac", "probuď počítač", "wol"],
        args=[], run=_run_pc_wake, grounding=None,
        needs_confirm=True, cooldown_s=60),
    "hans_wake": Action(
        "hans_wake",
        "Probudit SEBE (Hanse) ze spánku — když uživatel řekne ať se probudíš / "
        "vstáváš / už nespíš. OPAK akce hans_sleep. NENÍ to zapnutí počítače "
        "(to je pc_wake).",
        hints=["probuď se", "probud se", "vzbuď se", "vzbud se", "vstávej",
               "vstavej", "už nespi", "uz nespi", "jsi vzhůru", "prober se"],
        args=[], run=_run_hans_wake, grounding=None,
        needs_confirm=False, cooldown_s=30),
    "kodi_resume": Action(
        "kodi_resume",
        "Pokračovat v POZASTAVENÉM filmu/přehrávání na TV (odpauzovat). "
        "Bez argumentu — když uživatel chce pustit DÁL to, co běželo. "
        "Pro spuštění konkrétního filmu podle názvu je kodi_play_film.",
        hints=["pusť to dál", "pust to dal", "pokračuj", "pokracuj",
               "odpauzuj", "zruš pauzu", "zrus pauzu", "hraj dál", "hraj dal",
               "spusť to zas", "pusť to zpátky"],
        args=[], run=_run_kodi_resume, grounding=None,
        needs_confirm=False, cooldown_s=10),
    "game_mode_toggle": Action(
        "game_mode_toggle",
        "Zapnout nebo vypnout HERNÍ MÓD (Hans uvolní grafiku pro hru a přestane "
        "používat svůj mozek; vypnutím si ho vezme zpět). Argument 'mode' = "
        "'on' (jdu hrát / uvolni grafiku) nebo 'off' (dohrál jsem / vrať mozek). "
        "Na pouhý DOTAZ na stav („máš herní mód?\u201c) tuto akci NEvol.",
        hints=["herní mód", "herni mod", "jdu hrát", "jdu hrat", "budu hrát",
               "budu hrat", "uvolni grafiku", "dohrál jsem", "dohral jsem",
               "dohrál", "vrať mozek", "vrat mozek"],
        args=["mode"], run=_run_game_mode, grounding=None,
        needs_confirm=True, cooldown_s=15),
}


def _diary_path(handler) -> str:
    hi = getattr(handler, "_hans_idle", None)
    db = getattr(hi, "_db", None) if hi else None
    if db is not None:
        # sqlite3.Connection — potřebujeme cestu; zkus config
        pass
    cfg = getattr(handler, "config", {}) or {}
    return (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")


class Proposal:
    def __init__(self, action: Action, args: dict, text: str,
                 confidence: float, reason: str = ""):
        self.action = action
        self.args = args
        self.text = text
        self.confidence = confidence
        self.reason = reason
        self.ts = time.time()
        self.hash = _args_hash(action.id, {k: args.get(k) for k in action.args})


class AgentRouter:
    """LLM router + confirm smyčka + cooldown + deník. Instanci drží handler."""

    def __init__(self, config: dict):
        self.config = config or {}
        self._intent = None      # HANS_AGENT_SOCIAL_GUARD_V1 (lazy, sdílený)
        c = (config.get("agent", {}) or {})
        self.enabled = bool(c.get("enabled", False))
        self.threshold = float(c.get("confidence_threshold", 0.7))
        self.context_msgs = int(c.get("context_msgs", 5))
        self.model = c.get("model", "hans-czech:latest")
        self.num_predict = int(c.get("num_predict", 160))
        self.temperature = float(c.get("temperature", 0.1))
        self.timeout = int(c.get("timeout", 30))
        self.cooldown_default = int(c.get("cooldown_default_s", 60))
        self.reject_cooldown = int(c.get("reject_cooldown_s", 3600))
        # HANS_AGENT_NO_WORDGATE_V1 — True: pusť router i na obecné otázky/žádosti
        # (ne jen na hint slova akcí). False = staré chování (jen hinty).
        self.route_all_requests = bool(c.get("route_all_requests", True))
        # HANS_AGENT_KODI_CONFIRM_V1 — u návrhu filmu zrcadli potvrzení i na TV
        self.kodi_confirm_to_tv = bool(c.get("kodi_confirm_to_tv", True))
        self.kodi_confirm_countdown = int(c.get("kodi_confirm_countdown_s", 45))
        self._pending: dict[str, Proposal] = {}       # name → návrh
        self._last_fire: dict[str, float] = {}         # hash → ts (cooldown)
        self._rejected: dict[str, float] = {}          # name|hash → ts (echo)

    # ── pre-gate ────────────────────────────────────────────────────────────
    # HANS_AGENT_NO_WORDGATE_V1 — pre-gate dřív propustil router JEN když zpráva
    # trefila hint slovo konkrétní akce → novým formulacím („děje se něco doma?“)
    # se router NIKDY nezeptal a propadly do LLM = konfabulace živého stavu.
    # Teď: hint = rychlá cesta (zpětná kompatibilita), NAVÍC pusť router, kdykoli
    # zpráva STRUKTURÁLNĚ vypadá jako dotaz/žádost (otazník / tázací nebo
    # rozkazovací začátek). Rozhodnutí CO spustit dál dělá LLM router (whitelist);
    # heuristika jen rozhoduje JESTLI se Hans vůbec zamyslí. Router = rezidentní
    # hans-czech (VRAM zdarma), cena = latence jednoho krátkého volání.
    def _hint_match(self, text: str) -> bool:
        """HANS_HINT_CLITIC_ORDER_V1 (20.8.) — dvouslovná nápověda snese
        PROHOZENÉ pořadí.

        Doloženo: „z dnešního hovoru SI ZAPIŠ hlavně to, že sis vymyslel to
        divadlo" bránou neprošlo, protože nápověda je doslovné „zapiš si".
        Čeština klitika běžně přehazuje („si zapiš", „to dej", „spát jdi"),
        takže agent takovou větu vůbec neviděl → odpověděl volný hovor a
        Hans PROHLÁSIL, že si zápis udělal, ačkoli se nezapsalo nic.
        Táž třída jako HANS_HRAJE_WORDORDER_V1 a HANS_STUDY_CONTENT_RECALL_V1.

        ⚠️ ÚZKÉ SCHVÁLNĚ: povoluje se JEN prohození dvou SOUSEDNÍCH slov, ne
        „slova kdekoli ve větě" — 135 ze 210 nápověd je víceslovných a volné
        pořadí by z „na tv" udělalo past na skoro každou větu.
        **Změřeno na 966 reálných zprávách: projde +2, a jsou to přesně ty
        dvě doložené vadné.** Žádný jiný dopad.
        """
        import re as _re
        t = _norm(text)
        if len(t) < 2:
            return False
        for a in ACTIONS.values():
            for h in a.hints:
                if h in t:
                    return True
                w = h.split()
                if len(w) == 2 and _re.search(
                        r'\b%s\s+%s\b' % (_re.escape(w[1]), _re.escape(w[0])), t):
                    return True
        return False

    def _looks_like_request(self, text: str) -> bool:
        """Strukturální (NE per-akce sémantická) heuristika: vypadá zpráva jako
        otázka nebo žádost? Otazník, nebo tázací/rozkazovací začátek."""
        raw = (text or "").strip()
        if len(raw) < 2:
            return False
        if raw.endswith("?"):
            return True
        t = _norm(raw)
        first = t.split()[0] if t.split() else ""
        return first in _REQUEST_OPENERS or first in _ACTION_VERBS

    def _actionable(self, text: str) -> bool:
        if not self.route_all_requests:
            return self._hint_match(text)           # legacy chování (config off)
        return self._hint_match(text) or self._looks_like_request(text)

    # ── confirm smyčka ──────────────────────────────────────────────────────
    def _mentions_kolac(self, message: str) -> bool:
        """KOLAC_STATUS_GUARD_V1 — zmiňuje zpráva Koláče (jméno z configu,
        bez diakritiky)? Použito k přesměrování domácích akcí na kolac_status."""
        import unicodedata

        def _fold(s):
            return "".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                           if not unicodedata.combining(c))
        try:
            from scripts.hans_kolac import kolac_name
            kn = _fold(kolac_name(self.config))
        except Exception:
            kn = "kolac"
        fm = _fold(message)
        return (kn and kn in fm) or "kolac" in fm

    def check_confirmation(self, handler, name: str,
                           message: str) -> Optional[str]:
        """Má osoba čekající návrh a odpovídá ano/ne? → proveď/zruš, vrať text.
        Jinak None (žádný pending / nejednoznačné → nechá projít do chatu)."""
        pend = self._pending.get(name)
        if not pend:
            return None
        # návrh vyprší po 3 min bez odpovědi
        if time.time() - pend.ts > 180:
            self._pending.pop(name, None)
            return None
        m = _norm(message)
        words = m.split()
        first = words[0] if words else ""
        # HANS_AGENT_CONFIRM_PAYLOAD_V1 (5.8.) — POTVRZOVACÍ slovník se překrývá
        # s PŘÍKAZOVÝM: „pusť"/„spusť"/„dej" znamená jak „ano, pusť to", tak
        # „pusť TOHLE (něco jiného)". Nová žádost se pak spolkne jako souhlas
        # s čekajícím návrhem. Doloženo 5.8. při testu: čekal návrh na film
        # „Ring", přišlo „pust film Vykoupení z věznice Shawshank" → Hans pustil
        # RING (a to zrovna horor, kterého se domácnost bála).
        # Rozlišovač: potvrzení je KRÁTKÉ a bez nového obsahu („ano", „pusť to").
        # Jakmile věta nese další podstatné slovo, je to NOVÝ požadavek.
        # PC_SHUTDOWN_DEFER_V1 — „až dodělá" je odpověď, ne nový požadavek.
        # Smysl dává jen u vypínání PC; jinde se ignoruje.
        if (pend.action.id == "pc_shutdown" and _LATER_PAT.search(message or "")):
            self._pending.pop(name, None)
            self._log(handler, pend, "deferred")
            try:
                from scripts.pc_deferred_shutdown import request as _defer
                _defer(person=name or "", note=message[:120])
                return ("Dobře, pane — nechám ho dopracovat a vypnu ho, "
                        "jakmile bude mít klid. Dám vědět.")
            except Exception as _de:
                log.warning("odložené vypnutí neuloženo: %s", _de)
                return "Odložit vypnutí se mi teď nepovedlo, pane."

        _short = len(words) <= 2
        is_yes = (m in _YES
                  or (_short and (first in _YES
                                  or any(m.startswith(y + " ") for y in _YES))))
        is_no = (m in _NO
                 or (_short and (first in _NO
                                 or any(m.startswith(n + " ") for n in _NO))))
        if not is_yes and not is_no and (first in _YES or first in _NO):
            log.info("agent: '%.40s' vypadá jako potvrzení, ale nese vlastní "
                     "obsah → beru jako NOVÝ požadavek", message)
        if not is_yes and not is_no:
            # nejednoznačné — návrh zahoď a nech projít do běžného chatu
            self._pending.pop(name, None)
            self._log(handler, pend, "ignored")
            return None
        self._pending.pop(name, None)
        # HANS_AGENT_KODI_CONFIRM_V1 — odpověď padla v chatu → zavři i dialog na TV
        # (ať okno na TV nečeká na vypršení timeoutu / nezůstane přes hrající film).
        if pend.action.id == "kodi_play_film":
            self._cancel_kodi_dialog(handler)
        if is_no:
            self._rejected[f"{name}|{pend.hash}"] = time.time()
            self._log(handler, pend, "rejected")
            return "Dobře, nechám to být."
        # ANO → proveď
        try:
            result = pend.action.run(handler, pend.args)
        except Exception as e:
            log.warning("agent akce %s selhala: %s", pend.action.id, e)
            result = "Něco se při provádění pokazilo, pane."
        self._last_fire[pend.hash] = time.time()
        self._log(handler, pend, "accepted", result=result)
        return result

    # ── návrh ───────────────────────────────────────────────────────────────
    # HANS_AGENT_SOCIAL_GUARD_V1 — CÍLENÝ prompt, ne obecné „faktický ×
    # volný". Obecný klasifikátor (`hans_intent`) tuhle hranici netrefí:
    # „co se děje doma?" mu vyšlo jako volná konverzace → potlačil by
    # LEGITIMNÍ report_home_status (změřeno). Rozdíl „ptá se na MĚ × na DŮM"
    # je jiná otázka a chce vlastní příklady — s nimi 12/12.
    def _is_small_talk(self, message: str) -> bool:
        """HANS_AGENT_SOCIAL_GUARD_V1 — ptá se zpráva na HANSE (→ potlač
        stavovou akci), nebo na DŮM? Detektor je od 5.8. sdílený
        (`hans_intent.is_about_self`), protože ho potřebuje i chat pro grounded
        blok o sobě — dvě kopie promptu by se rozešly."""
        try:
            from scripts.hans_intent import is_about_self
            return is_about_self(message, self.config)
        except Exception as e:
            log.debug("agent social gate: %s", e)
            return False

    # HANS_AGENT_SLEEP_QUESTION_GUARD_V1 — rozkazová slovesa, která znamenají
    # SKUTEČNOU žádost o uspání. Když ve větě jsou, je to příkaz i s otazníkem
    # („můžeš jít spát?").
    _SLEEP_IMPERATIVE = re.compile(
        r"\b(b[ěe][zž]|jdi|jd[ěe]te|usni|usn[ěe]te|sp[ěe]te|ztich|utich|"
        r"odpo[čc]i[nň]|dobrou\s+noc|m[uů][zž]e[sš]\s+(j[íi]t\s+)?spa?t)",
        re.IGNORECASE)
    # Zápor u režimu = korekce („NEměl bys být v režimu spánku").
    _SLEEP_NEGATION = re.compile(r"\bne\w{0,3}(m[ěe]l|m[áa][sš]|jsi|budeš|bys)\b",
                                 re.IGNORECASE)

    def _sleep_question(self, message: str) -> bool:
        """Ptá se věta na režim / opravuje ho (→ NENÍ to žádost o uspání)?"""
        m = (message or "").strip()
        if not m:
            return False
        if self._SLEEP_IMPERATIVE.search(m):
            return False          # skutečný rozkaz — nech projít
        return m.endswith("?") or bool(self._SLEEP_NEGATION.search(m))

    def propose(self, handler, name: str, message: str) -> Optional[str]:
        """Vrátí propose_text (Hansův návrh + [ano/ne]) nebo None (běžný chat)."""
        # HANS_PERSON_CARD_ACTION_V1 (18.8.) — `report_person` potřebuje CELOU
        # větu, ne jen jméno vytažené routerem: „co víš o Janě?“ nese pád,
        # který config zná, kdežto router by mohl vrátit tvar jiný.
        # (19.8.: přiřazení bylo omylem NAD docstringem, takže docstring
        # přestal být docstringem — vráceno na správné pořadí.)
        self._raw_message = message or ""
        # HANS_HOUSEHOLD_PRIVACY_V1 — runnery potřebují vědět, KDO se ptá
        # (karty o domácnosti se cizímu člověku nevydávají).
        self._raw_name = name or ""
        if not self.enabled:
            return None
        try:
            if not self._actionable(message):
                return None
            decision = self._route(handler, name, message)
            if not decision:
                return None
            aid = decision.get("action")
            # KOLAC_STATUS_GUARD_V1 — deterministická pojistka: dotaz o Koláčovi
            # (společník) se NESMÍ zrouteovat na domácí/přítomnostní akce (router
            # občas zvolí home_status → „na TV hraje…, vidím Janu"). Přesměruj.
            if aid in ("report_home_status", "report_who_is_home",
                       "report_now_playing") and self._mentions_kolac(message):
                aid = "report_kolac_status"
            # HANS_FILM_RECOMMEND_V1 — žádost o DOPORUČENÍ není rozkaz pustit.
            # Doloženo 2×: „Doporučil bys mi film?" → nabídl pustit sportovní
            # přenos, co zrovna běžel (název si router vzal z živého stavu).
            # action=null → odpoví volný hovor z Hansových zápisků o filmech.
            if aid in ("kodi_play_film", "add_book_wishlist") and \
                    _asks_recommendation(message):
                log.info("HANS_FILM_RECOMMEND_V1: %s potlačen — věta žádá "
                         "doporučení, ne spuštění: %.50s", aid, message)
                return None
            # HANS_OPINION_NOT_ORDER_V1 — otázka na názor nesmí spustit akci
            # S NÁSLEDKEM. Informativní `report_*` se NEvetuje: u názorové
            # otázky je odpověď nanejvýš mimo mísu, ale nic neprovede — kdežto
            # „pustím film" uživatel řeší tím, že ho zastavuje.
            _act = ACTIONS.get(aid) if aid else None
            if _act is not None and _act.needs_confirm and _asks_opinion(message):
                log.info("HANS_OPINION_NOT_ORDER_V1: %s potlačen — věta se ptá "
                         "na názor, nežádá akci: %.50s", aid, message)
                return None
            # HANS_CAP_QUESTION_NOT_ORDER_V1 — „umíte pustit něco na televizi?"
            # je dotaz na schopnost, ne pokyn; bez předmětu ve větě by ho
            # router doplnil z historie. action=null → odpoví volný hovor,
            # který má v promptu blok schopností (`cap`) a řekne, že to umí.
            # Jen akce S NÁSLEDKEM (needs_confirm) — informativní report_*
            # nic neprovede, takže je zbytečné je vetovat.
            if _act is not None and _act.needs_confirm and \
                    _asks_capability(message, decision.get("args") or {}):
                log.info("HANS_CAP_QUESTION_NOT_ORDER_V1: %s potlačen — věta "
                         "se ptá na schopnost, nejmenuje předmět: %.50s",
                         aid, message)
                return None
            # HANS_PRESENCE_ASK_V1 — dotaz na přítomnost konkrétní osoby patří
            # VŽDY na who_home, i v ukecané formě („nevíš, jestli je Jana
            # doma?" končilo na report_kolac_status). Přepisujeme jen mezi
            # `report_*` akcemi, aby guard neukradl skutečný příkaz.
            if (aid and aid.startswith("report_") and aid != "report_who_is_home"
                    and not self._mentions_kolac(message)
                    and _asks_person_presence(message, self.config)):
                log.info("HANS_PRESENCE_ASK_V1: %s → report_who_is_home "
                         "(dotaz na přítomnost osoby): %.50s", aid, message)
                aid = "report_who_is_home"
            # HANS_CHAT_STUDY_BRIDGE_GUARD — recall („zjisti víc o / co víš o
            # X") se NESMÍ zrouteovat na studium (malý model občas splete „víc"
            # a „si") → nech odpovědní/film-recall cestu (action=null).
            if aid == "add_study_topic" and _looks_like_recall(message):
                return None
            # HANS_STUDY_KNOWN_TOPIC_V1 (6.8.) — DATOVÁ brzda: nenabízej
            # nastudovat něco, co UŽ máš odškrtnuté. Doloženo 5× po sobě
            # (08:48–09:06) na tématech, u kterých `/studium` hlásilo 12 z 12.
            # Regexový guard výše má nutně díry ve vzorech; tohle se ptá DAT,
            # takže je nezávislé na tom, jak se uživatel zeptá.
            # action=null → odpoví recall/LLM z vlastních zápisků (což umí:
            # v 08:59 na „ne, jen rekni co uz vis" odpověděl správně).
            if aid == "add_study_topic":
                try:
                    from scripts.hans_study import already_studied
                    _t = (decision.get("args") or {}).get("tema") or ""
                    _cov = already_studied(_t, _diary_path(handler)) if _t else None
                    if _cov:
                        log.info("HANS_STUDY_KNOWN_TOPIC_V1: '%s' už pokryto "
                                  "(%s) → nenabízím studium, odpovím z paměti",
                                  _t, _cov[0])
                        return None
                except Exception as _kte:
                    log.debug("already_studied: %s", _kte)
            # HANS_AGENT_SOCIAL_GUARD_V1 (4.8.) — zdvořilostní dotaz NA HANSE
            # („jak se ti daří?", „máš se dobře?", „co je u tebe nového?") se
            # NESMÍ zrouteovat na hlášení stavu domácnosti. Doloženo testem:
            # router odpovídal „Na TV právě hraje… Vidím tu Janu" (a to i
            # s conf 1.00) — věcně mimo, a ještě to zmíní třetí osobu.
            # Rozhoduje TÝŽ klasifikátor jako grounding (`hans_intent`), tedy
            # i mini model na Pi → pokrývá i formulace, které nikdo nevypsal
            # do seznamu. Cena je JEN u těchto tří akcí, ne u každé zprávy.
            # HANS_AGENT_SOCIAL_GUARD_V2 (6.8.) — `report_kolac_status` PATŘÍ
            # do stejné množiny. Doloženo živě: „jak se mas?" → „Koláč zrovna
            # tiše přemítá po mém boku" (conf 0.95). Router má v kontextu
            # posledních N výměn, a když se pár předchozích točilo kolem
            # Koláče, přetáhne k němu i osobní otázku. Guard kryl jen tři
            # „domácí" akce, takže tudy únos prošel.
            # HANS_AGENT_SLEEP_QUESTION_GUARD_V1 (7.8.) — OTÁZKA na režim ani
            # KOREKCE režimu není žádost o uspání. Doloženo 2× živě: „jsi
            # v rezimu spanku?" → návrh hans_sleep (conf 1.00) a „nemel by byt
            # v rezimu spanku, je 13:00" → „Přecházím do režimu spánku, pane."
            # Hans přitom nikdy neusnul (v logu žádné `SLEEP: aktivuji`) —
            # uživatel ale četl text návrhu jako hotovou akci.
            # Rozlišovač je TVAR VĚTY, ne téma: rozkaz („běž spát", „ztich se")
            # projde, tázací/záporná věta se potlačí a odpověď obstará
            # deterministický blok o režimu (HANS_SELF_STATE_AWAKE_V2).
            # ⚠️ ZÁMĚRNĚ ne přes `_is_small_talk` — ta vrací True i pro „běž
            # spát" (taky se týká Hanse) a zabila by legitimní příkaz.
            if aid == "hans_sleep" and self._sleep_question(message):
                log.info("agent: hans_sleep potlačen — věta se na režim PTÁ "
                         "nebo ho opravuje, nežádá o uspání: %.50s", message)
                return None
            if aid in ("report_home_status", "report_who_is_home",
                       "report_now_playing", "report_kolac_status"
                       ) and self._is_small_talk(message):
                log.info("agent: %s potlačen — dotaz je o Hansovi, "
                          "ne o domě/Koláčovi", aid)
                return None
            action = ACTIONS.get(aid)
            if not action:
                return None
            conf = float(decision.get("confidence", 0) or 0)
            if conf < self.threshold:
                return None
            args = {k: (decision.get("args", {}) or {}).get(k)
                    for k in action.args}
            h = _args_hash(aid, args)
            # anti-echo: nedávno odmítnuto touž osobou?
            rk = f"{name}|{h}"
            if time.time() - self._rejected.get(rk, 0) < self.reject_cooldown:
                return None
            # cooldown per (akce, args)
            cd = action.cooldown_s or self.cooldown_default
            if time.time() - self._last_fire.get(h, 0) < cd:
                return None
            # grounding argumentů
            if action.grounding:
                ok, args, gmsg = action.grounding(handler, args)
                if not ok:
                    log.info("agent: %s grounding zamítl (%s)", aid, gmsg)
                    # HANS_AGENT_SPEAK_REJECT_V1 — u akcí, kde by ticho svádělo
                    # personu k potvrzení, řekni PRAVDU místo mlčení.
                    if action.reject_text:
                        try:
                            if callable(action.reject_text):
                                return action.reject_text(handler, args)
                            return action.reject_text.format(
                                titul=(args.get("titul") or "").strip() or "ten titul")
                        except Exception as _re:
                            log.debug("reject_text: %s", _re)
                            return ("Tohle teď provést nemohu, pane.")
                    return None
            prop = Proposal(action, args,
                            decision.get("propose_text", ""), conf,
                            decision.get("reason", ""))
            # INSTANT akce (needs_confirm=False) — info dotazy jen odpoví
            # z živých dat, žádné ano/ne. Proveď hned a vrať výsledek.
            if not action.needs_confirm:
                try:
                    result = action.run(handler, args)
                except Exception as e:
                    log.warning("agent instant %s selhala: %s", aid, e)
                    return None
                if not result:
                    return None
                self._last_fire[h] = time.time()
                self._log(handler, prop, "answered", result=result)
                log.info("agent: instant %s conf=%.2f pro %s", aid, conf, name)
                return result
            # CONFIRM akce — navrhni + ulož pending, čekej na ano/ne.
            # HANS_SHUTDOWN_CONTEXT_V1 — u vypnutí PC přebij text živým stavem
            # (co na PC běží); LLM propose_text to vědět nemůže.
            # HANS_CONFIRM_TEXT_DETERMINISTIC_V1 (20.8.) — ZNĚNÍ NÁVRHU SKLÁDÁ
            # PROGRAM, ne model. Doloženo 19.–20.8. opakovaně: LLM `propose_text`
            # zněl jako HOTOVÁ VĚC — „Rozumím, zapíšu si to: 'divadlo'. Ještě
            # něco?" nebo „Dobře, zapíšu si: … Ještě něco?" — takže uživatel
            # neměl jak poznat, že se na něco čeká, a akce zůstala neprovedená.
            # Navíc si takový text sám končí otazníkem („Ještě něco?"), takže
            # ani pojistka „nekončí otázkou → přidej 'Mám to udělat?'" nezabrala.
            # ⚠️ Nic nového se nepsalo: `_default_text` pokrývá VŠECHNY
            # potvrzované akce („Mám si poznamenat „X"?") a fallback „Mám to
            # zařídit?" — jen ho dosud přebíjel model. U `pc_shutdown` tohle
            # rozhodnutí padlo už dřív (HANS_SHUTDOWN_CONTEXT_V1); teď platí
            # obecně. Model si `propose_text` dál vrací (schéma se nemění),
            # jen se na potvrzovací cestě nepoužije.
            if action.id == "pc_shutdown":
                text = _shutdown_confirm_text(handler)
            else:
                text = self._default_text(action, args).strip()
            if not text.endswith(("?",)):
                text += " Mám to udělat?"
            prop.text = text
            self._pending[name] = prop
            # HANS_AGENT_KODI_CONFIRM_V1 — u filmu zrcadli potvrzení i na TV
            # (dialog Ano/Ne přímo na Kodi, ne jen v chatu). Best-effort.
            if action.id == "kodi_play_film" and self._mirror_kodi_confirm(
                    handler, prop):
                text += " (potvrdit můžete i přímo na televizi.)"
                prop.text = text
            self._log(handler, prop, "proposed")
            log.info("agent: návrh %s conf=%.2f pro %s", aid, conf, name)
            return text
        except Exception as e:
            log.warning("agent.propose selhalo: %s", e)
            return None

    def _mirror_kodi_confirm(self, handler, prop: Proposal) -> bool:
        """HANS_AGENT_KODI_CONFIRM_V1 — ukaž potvrzení návrhu filmu i na TV.
        Reuse addon 'service.hans.suggest' (dialog Ano/Ne): na 'Pustit' addon film
        pustí, timeout/Ne nic neudělá (žádné auto-přehrání) → bezpečné zrcadlo chat
        potvrzení. Chat 'ano' i tlačítko na TV vedou k témuž filmu. Best-effort —
        selhání (Kodi dole) jen vrátí False, chat potvrzení běží dál."""
        if not self.kodi_confirm_to_tv:
            return False
        movie = (prop.args or {}).get("_movie")
        if not movie or movie.get("movieid") is None:
            return False
        hi = getattr(handler, "_hans_idle", None)
        kodi = getattr(hi, "kodi", None)
        if not kodi or not hasattr(kodi, "suggest_movie"):
            return False
        title = movie.get("title") or prop.args.get("titul") or "film"
        line = u'Mám pustit „%s"? Potvrďte „Pustit", nebo nechte být.' % title
        fcfg = (self.config.get("film_suggest", {}) or {})
        # Hansova tvář do dialogu (stejná cesta jako u návrhu filmu z klidu)
        face = None
        try:
            if fcfg.get("avatar_to_kodi", True) and hasattr(hi, "_tv_face_image"):
                face = hi._tv_face_image()
        except Exception:
            face = None
        # Hlas na TV — jen když nic nehraje (přehrání by běžící film přerušilo),
        # jako u vlastního návrhu filmu (voice_to_kodi).
        voice = None
        try:
            playing = bool(kodi.get_now_playing()) if hasattr(
                kodi, "get_now_playing") else True
            tts = getattr(handler, "tts_speaker", None)
            if (not playing and fcfg.get("voice_to_kodi", False)
                    and tts and hasattr(tts, "_get_mp3")):
                mp3 = tts._get_mp3(u'Mám pustit film „%s"?' % title)
                if mp3:
                    voice = str(mp3)
        except Exception:
            voice = None
        try:
            return bool(kodi.suggest_movie(
                movie, countdown=self.kodi_confirm_countdown, line=line,
                image_local=face, voice_local=voice,
                voice_volume=int(fcfg.get("voice_volume", 90)),
                voice_lead_ms=int(fcfg.get("voice_lead_ms", 900))))
        except Exception as e:
            log.warning("agent: zrcadlení potvrzení na TV selhalo: %s", e)
            return False

    def _cancel_kodi_dialog(self, handler):
        """Zavři otevřený návrhový dialog na TV (uživatel odpověděl v chatu)."""
        if not self.kodi_confirm_to_tv:
            return
        kodi = getattr(getattr(handler, "_hans_idle", None), "kodi", None)
        if kodi and hasattr(kodi, "cancel_dialog"):
            try:
                kodi.cancel_dialog()
            except Exception as e:
                log.warning("agent: zavření TV dialogu selhalo: %s", e)

    def _default_text(self, action: Action, args: dict) -> str:
        if action.id == "kodi_play_film":
            return f"Chcete, abych pustil „{args.get('titul')}“?"
        if action.id == "hans_sleep":
            return "Mám se ztišit a jít spát?"
        if action.id == "add_book_wishlist":
            return f"Mám „{args.get('titul')}“ přidat na seznam ke čtení?"
        if action.id == "kodi_pause":
            return "Mám pozastavit přehrávání?"
        if action.id == "kodi_stop":
            return "Mám zastavit přehrávání?"
        if action.id == "add_study_topic":
            return f"Mám si „{args.get('tema')}“ zařadit ke studiu?"
        if action.id == "add_note":
            return f"Mám si poznamenat „{args.get('text')}“?"
        if action.id == "pc_shutdown":
            return "Mám vypnout počítač?"
        if action.id == "guard_toggle":
            m = (args.get("mode") or "").strip().lower()
            if any(k in m for k in ("stop", "vypni", "off", "konec", "zru")):
                return "Mám vypnout hlídací režim?"
            return "Mám spustit hlídací režim?"
        return "Mám to zařídit?"

    # ── LLM router ──────────────────────────────────────────────────────────
    def _route(self, handler, name: str, message: str) -> Optional[dict]:
        from scripts.ollama_client import ollama_generate
        catalog = "\n".join(
            f"- {a.id}: {a.desc} Argumenty: {a.args or 'žádné'}."
            for a in ACTIONS.values())
        ctx = self._context(handler, name)
        system = (
            "Jsi router akcí pro domácího společníka Hanse. Z POSLEDNÍ zprávy "
            "uživatele (a kontextu) rozhodni, zda si uživatel PŘEJE nějakou "
            "konkrétní AKCI ze seznamu níže. Vrať POUZE JSON, nic jiného.\n\n"
            "AKCE (smíš zvolit JEN z tohoto seznamu, nic nevymýšlej):\n"
            + catalog +
            "\n\nJSON formát: {\"action\": <id akce nebo null>, \"args\": "
            "{...}, \"confidence\": <0.0-1.0>, \"reason\": \"<krátce proč>\", "
            "\"propose_text\": \"<jak se Hans zeptá, česky, uctivě, končí "
            "otázkou>\"}\n"
            "Pravidla: Když si uživatel žádnou akci ze seznamu nepřeje "
            "(běžná otázka, povídání), vrať action=null a confidence 0. "
            "DŮLEŽITÉ — ROZLIŠ DVA PODOBNÉ PŘÍPADY (HANS_CHAT_STUDY_BRIDGE_V1): "
            "(a) uživatel chce, abys mu TEĎ POVĚDĚL, co už víš („co víš o X“, "
            "„zjisti víc o X“, „řekni mi o X“, „znáš X?“, „pamatuješ na X“) → "
            "action=null (jen odpovíš; NEpouštěj film, NEpřidávej na seznam). "
            "(b) uživatel chce, abys šel něco NASTUDOVAT a ZAPAMATOVAL si to "
            "napříště („nastuduj X“, „nauč se o X“, „zjisti si o X“, „podívej "
            "se na X“, „mrkni na X“, „prozkoumej X“, „nechceš si zjistit o X“) → "
            "action=add_study_topic, args tema=X. Klíč: „zjisti SI / nauč se / "
            "nastuduj / podívej se na“ = BUDOUCÍ studium; „zjisti VÍC / co víš / "
            "řekni mi“ = odpověz teď. "
            "Akci (pustit/přidat/…) zvol JEN u jasného POKYNU něco UDĚLAT. "
            "Nikdy nevymýšlej akci mimo seznam. args vyplň jen když je znáš "
            "z textu (titul filmu/knihy). Buď konzervativní — při pochybnosti "
            "action=null.")
        prompt = f"{ctx}\nPOSLEDNÍ zpráva ({name}): {message}\n\nJSON:"
        raw = ollama_generate(
            self.model, prompt, system=system, config=self.config,
            timeout=self.timeout, keep_alive=-1,
            options={"temperature": self.temperature,
                     "num_predict": self.num_predict})
        if not raw:
            return None
        return self._parse_json(raw)

    @staticmethod
    def _parse_json(raw: str) -> Optional[dict]:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            d = json.loads(m.group(0))
            if not isinstance(d, dict) or not d.get("action"):
                return None
            return d
        except Exception:
            return None

    def _context(self, handler, name: str) -> str:
        parts = []
        # čas / fáze
        try:
            hi = getattr(handler, "_hans_idle", None)
            rt = getattr(hi, "_routine", None) if hi else None
            if rt and hasattr(rt, "phase_label"):
                parts.append(f"Situace: {rt.phase_label()}.")
        except Exception:
            pass
        # živý stav — co hraje na TV
        try:
            hi = getattr(handler, "_hans_idle", None)
            kodi = getattr(hi, "kodi", None) if hi else None
            if kodi and hasattr(kodi, "get_now_playing"):
                np = kodi.get_now_playing()
                if np and np.get("title"):
                    parts.append(f"Na TV právě hraje: {np.get('title')}.")
                else:
                    parts.append("Na TV teď nic nehraje.")
        except Exception:
            pass
        # posledních N výměn
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — agent kontext JEN z tohoto kanálu
            try:
                from scripts.openwebui_direct_handler import get_current_channel
                _ch = get_current_channel()
            except Exception:
                _ch = None
            hist = ((handler.conv_store.get_history_scoped(name, _ch)
                     if _ch else handler.conv_store.get_history(name)) or [])
            for msg in hist[-self.context_msgs:]:
                role = msg.get("role")
                c = (msg.get("content") or "")[:200]
                who = name if role == "user" else "Hans"
                if c:
                    parts.append(f"{who}: {c}")
        except Exception:
            pass
        return "\n".join(parts)

    # ── deník ───────────────────────────────────────────────────────────────
    def _log(self, handler, prop: Proposal, outcome: str, result: str = ""):
        try:
            hi = getattr(handler, "_hans_idle", None)
            if hi and hasattr(hi, "_log_entry"):
                hi._log_entry(
                    "agent_action",
                    f"{prop.action.id} → {outcome}",
                    data=json.dumps({"action": prop.action.id,
                                     "args": {k: prop.args.get(k)
                                              for k in prop.action.args},
                                     "outcome": outcome,
                                     "confidence": round(prop.confidence, 2)},
                                    ensure_ascii=False),
                    note=(result or prop.text)[:300])
        except Exception:
            pass
