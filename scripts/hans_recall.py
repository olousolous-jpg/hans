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
               "study_note_part",   # HANS_STUDY_ARTICLE_REST_V1
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
        # HANS_KODI_FILTR_NOTE_V1 (3.9.) — značka `topic` má DVĚ podoby podle
        # toho, kterou cestou zápis šel (`hans_curiosity`):
        #   ř. 841 (odložený zápis, „mozek byl mimo") → data = JSON s topic
        #   ř. 726 (běžná cesta)                      → note = "[kodi] …"
        # Filtr uměl jen tu první, takže viděl 155 titulů místo 656 a čtení
        # k filmům propadalo do výpisu četby. Doloženo testem 3.9.: na
        # „cetl jsi neco zajimaveho?" Hans vypsal Smrtonosnou past a Tělesnou
        # stráž jako ČETBU a pak o filmu mluvil, jako by ho četl.
        rows = conn.execute(
            "SELECT DISTINCT title FROM diary "
            "WHERE event_type='web_read' AND ("
            "  note LIKE '[kodi]%'"
            "  OR (data LIKE '%\"kodi\"%' AND json_valid(data) "
            "      AND json_extract(data,'$.topic')='kodi'))").fetchall()
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


def _cz_when(ts: float, with_weekday: bool = True, slovy: bool = False) -> str:
    """'v pátek 25. dubna 2026 v 19:05' — česky, deterministicky.

    HANS_TIME_WORDS_SHARED_V1 (26. 9.) — `slovy=True` přidá čas i slovy pro
    podklad MODELU: záznam „24. září v 20:13“ Hans vyslovil „v osmnáct hodin
    třináct minut“ (převod čísla si dělal sám). Týž důvod jako
    TIME_AWARENESS_WORDS_V1 u aktuálního času. Výpisy pro člověka beze změny."""
    d = datetime.fromtimestamp(ts)
    day = f"{d.day}. {_MESICE_GEN[d.month]} {d.year}"
    out = f"{day} v {d:%H:%M}"
    if slovy:
        from scripts.cz_numbers import cz_clock_words
        out += f" ({cz_clock_words(d.hour, d.minute)})"
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


from scripts.hans_recall_rozhovory import _conv_keywords   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import _kw_groups   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_rozhovory import _day_bounds   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import _pick_year   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import resolve_time_range   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import _resolve_day_reference   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import conversation_recall   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_rozhovory import _split_exchange   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import _day_notes   # ROZDELENI_PRIKAZU_V1 — přesunuto


_TOPIC_SUM_SYSTEM = (
    "Jsi pečlivý archivář. Dostaneš DOSLOVNÝ přepis replik jednoho člověka "
    "z rozhovorů s Hansem. Napiš JEDNU až DVĚ věty česky o tom, o čem se "
    "bavili — vyjmenuj hlavní témata. Piš ve tvaru „Bavili jsme se hlavně "
    "o …“. Uveď POUZE témata, která se v přepisu skutečně objevují; NIC "
    "nedomýšlej, nehodnoť, nepřidávej rady."
)


from scripts.hans_recall_rozhovory import _summarize_topics   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_rozhovory import _extract_conv_topic   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_rozhovory import _is_echo_exchange   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import topic_conversation   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import chat_summary   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_rozhovory import is_recall_query   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_SOURCE_QUERY_V1 — dotaz na zdroj Hansova tvrzení ────────────────────

# HANS_SOURCE_META_MEMORY_V1 (30.8.) — OTÁZKA NA SPOLEHLIVOST PAMĚTI NENÍ
# DOTAZ NA ZDROJ. Doloženo simulovaným rozhovorem: „A na základě čeho tvrdíte,
# že si to PAMATUJETE SPRÁVNĚ, když jste si to sám zapsal?" → deterministický
# bypass odpověděl *„O tématu 'Design' jsem se dočetl na Wikipedii"* + odkaz,
# tedy zcela mimo — entitu si vzal z repliky o dvě výměny zpět.
#
# Je to táž třída jako `HANS_SOURCE_IS_SENSOR_V1` („odkud víš, že tu je Jana?"
# → zdrojem je KAMERA, ne článek): ne každé „na základě čeho" míří na četbu.
# Tady je předmětem otázky Hansova vlastní PAMĚŤ, a na to se odpovídá úvahou —
# což doloženě umí, když ho k ní pustíme (tahy 5 a 7 téhož rozhovoru).
# Proto detektor vrátí False → dotaz jde běžnou cestou, žádná šablona.
#
# ⚠️ Změřeno na 1337 reálných replikách: z 29 historických zachytů dotazu na
# zdroj by nově nevypadl ANI JEDEN.
_META_PAMET = re.compile(
    r"\b(že|ze)\s+(si\s+|se\s+)?(to\s+)?"
    r"(pamatuj|vzpom[íi]n|neplet|nem[ýy]l)"
    r"|\bpamatuje(te|š)\s+spr[áa]vn"
    r"|\bjste\s+si\s+jist\w*\s+(t[íi]m\s+)?(co\s+)?(si\s+)?(pam|vzpom)",
    re.IGNORECASE)


from scripts.hans_recall_rozhovory import is_memory_meta_query   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import is_source_query   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_ENTITY_WORDBOUND_V1 — číslovky nejsou zdroj tvrzení (viz níž).
_CISLOVKY = {"sedm", "osm", "devet", "devět", "deset", "jedna", "dva", "tri",
             "tři", "ctyri", "čtyři", "pet", "pět", "sest", "šest", "sto",
             "tisic", "tisíc", "nula", "jeden", "dvacet", "třicet"}


from scripts.hans_recall_zdroje import _find_entity_in_text   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_SOURCE_REFERENT_SCOPE_V1 (5.9.) — VÝPIS NENÍ TVRZENÍ.
# Hansova replika, která něco VYJMENOVÁVÁ (odrážky, číslování, dvojtečka na
# konci řádku, „mimo jiné:"), je REPORT o tom, co se dělo — ne tvrzení
# o světě. Náhodné slovo v takovém seznamu se nesmí stát „tématem, o kterém
# jsem se dočetl na Wikipedii".
# Doloženo třemi reálnými případy (12.–19. 8.), viz `_last_hans_topics`.
_VYCET_PAT = re.compile(
    r"(\n\s*[-•*·]\s|\n\s*\d+[.)]\s|mimo\s+jin[ée]:|:\s*\n)")


from scripts.hans_recall_zdroje import _je_vycet   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_SOURCE_READING_LIST_V1 (12. 9.) — VYPIS CETBY JE VYJIMKA.
# `_je_vycet` vylucuje vypisy jako referent, protoze polozka seznamu
# udalosti dne neni tema se zdrojem. U vypisu toho, co Hans CETL, to
# ale neplati: polozky JSOU tituly, ktere maji `source_url`. Bez teto
# vyjimky Hans na "a odkud to mas?" po vlastnim vypisu cetby popre,
# ze zdroj ma (doloheno 12. 9. v tykani i vykani).
# Zmereno: z 132 vypisu je 12 vypisu cetby; ostatnich 120 zustava skrytych.
_CETBA_PAT = re.compile(r"\((?:\u010detba|cetba)\)|\u010de?tl\s+jsem|"
                        r"do\u010detl\s+jsem", re.IGNORECASE)


from scripts.hans_recall_zdroje import _je_vypis_cetby   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import _last_hans_topics   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import sensor_source_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import sources_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_SOURCE_READING_URL_V1 — slova, KTERYMI se otazka na zdroj pta
# (ne to, NA CO se pta). Bez nich se "odkud cerpas informace o pocasi?"
# trefilo do titulu "Pravo na informace" — zmereno.
_ZDROJ_RAMEC = {
    "odkud", "kde", "cerpas", "cerpal", "cerpala", "cerpate", "informace",
    "informaci", "informacemi", "clanek", "clanku", "clanky", "zdroj",
    "zdroje", "zdrojem", "cetl", "cetla", "cetls", "cetli", "cetlas",
    "vlastne", "tohle", "tomhle", "tohohle", "rikas", "tvrdis", "presne",
    "material", "materialy", "podklad", "podklady", "vsechno",
    # HANS_SOURCE_READING_URL_V1 — pomocna slovesa a zajmena OBOU osob.
    # Regresni pripad to chytil hned: u vykani zustavalo "jste" jako
    # domnele tema, takze dotaz mel o jedno slovo vic a kratky rezim
    # (vsechna slova dotazu musi sednout) se na nej uz nespustil.
    # Vykaci varianta by se tak chovala HURE nez tykaci.
    "jste", "jsi", "jsem", "mate", "mas", "vite", "vis", "ktere", "ktery",
    "jake", "jaky", "jakym", "muzes", "muzete", "prosim"}


from scripts.hans_recall_zdroje import _zdroj_slova   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import _zdroj_z_cetby   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import _cerstve_dohledani   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_zdroje import sources_reply   # ROZDELENI_PRIKAZU_V1 — přesunuto


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
    # HANS_DREAM_RECALL_V1 (5.9.) — a rodina „zdálo/snilo se ti … o X".
    # Bez ní vracelo `_extract_topic` u snového dotazu PRÁZDNO, takže
    # „zdálo se ti něco o jednorožcích?" dostalo dnešní nesouvisející sen —
    # tedy táž třída chyby („šablona odpovídá na jinou otázku"), kvůli které
    # se `/sen` staví. Patří to SEM, a ne do vlastního vzoru v `dream_answer`:
    # o tom, co je téma dotazu, má být v tomhle souboru jedna pravda.
    # Změřeno na 1359 reálných větách: čtecí ani provenienční dotazy se nemění.
    # ⚠️ Musí to být UVNITŘ vnější skupiny — první pokus ji minul, takže
    # `m.group(1)` u snového dotazu neexistovala a padalo to na TypeError.
    r"(?:(?:ne)?(?:zd[áa]lo|snilo)\s+se\s+(?:ti|v[áa]m)\s+"
    r"(?:n[ěe]co\s+|n[ěe]kdy\s+)*o|"
    r"[čc]etla?\s+(?:jsi|sis)?\s*(?:n[ěe]co\s+)?o|"
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
              # HANS_READING_COLLOQUIAL_STOP_V1 (2.9.) — HOVOROVÉ „-ýho/-yho".
              # Spisovné tvary tu byly (i bez diakritiky), hovorové ne — a tak
              # uživatel píše. Doloženo: „cetl jsi dneska neco novyho?" →
              # „o ‚novyho' žádný záznam nemám", ačkoliv data Hans MĚL:
              # o tah později je na „o cem to bylo?" vypsal správně.
              # Změřeno na 1028 reálných větách: ani jedné se tím téma nerozbije.
              "novýho", "novyho", "dobrýho", "dobryho", "hezkýho", "hezkyho",
              "zajímavýho", "zajimavyho", "pěknýho", "peknyho",
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


# HANS_TOPIC_CONNECTIVES_V1 — spojky a predlozky, ktere samy tema netvori.
_TEMA_SPOJKY = {"nebo", "jen", "ale", "i", "a", "o", "na", "ze", "\u017ee", "v", "ve",
                "k", "ke", "s", "se", "z", "u", "do", "od", "po", "pro", "za"}


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
        return _vytcene_tema(q)
    words = [w for w in re.findall(r"[\wěščřžýáíéúůďťňó-]+", m.group(1))
             if w.lower() not in _STOPWORDS]
    # HANS_READING_TOPIC_SENTENCE_BOUND_V1 — reálné čtenářské téma je krátké
    # (1-4 slova: „hradech", „Sherlock Holmes"). Delší = pahýl z ukecané věty,
    # ne téma → radši prázdno = obecný výpis „co jsem četl", ne bogus „o ‚…'".
    if len(words) > 4:
        return _vytcene_tema(q)
    # HANS_TOPIC_CONNECTIVES_V1 (14. 9.) — tema slozene jen ze spojek
    # a predlozek neni tema. Dolozeno: „cetl jsi tu knihu nebo jen neco o ni?"
    # -> „nebo jen o" a Hans odpovedel, ze o „nebo jen o" zaznam nema.
    # ⚠️ Spojky se NEPRIDAVAJI do _STOPWORDS: „Na zapadni fronte klid" je titul.
    # Zmereno na 1 170 realnych vetach: meni se jen dolozena veta.
    if not any(len(w) >= 3 for w in words if w.lower() not in _TEMA_SPOJKY):
        return _vytcene_tema(q)
    return " ".join(words).strip() or _vytcene_tema(q)


# HANS_READING_FRONTED_TOPIC_V1 (4.9.) — VYTCENE TEMA JAKO ZALOHA.
# Doloženo 4.9. v BEZICIM Hansovi: „a rukodelna prace, cetl jsi o tom neco?"
# -> `_extract_topic` vrati '' (tema stoji PRED carkou, ve vete uz je jen
# anaforicke „o tom", a „tom"/„neco" jsou stopwordy) -> `/cetl` vysypal ctyri
# posledni cetby, ani jednu o remesle.
# Predikat je SDILENY (`hans_intent.vytcene_tema`) s branou na `/zajmy`
# v `chat_commands._thread_guard` — je to tataz trida vety, tak at se
# nerozejdou. Cisla mereni jsou u predikatu.
# ⚠️ Jen ZALOHA: kdyz tema najde puvodni cesta, vytceni se neptame.
def _vytcene_tema(question: str) -> str:
    try:
        from scripts.hans_intent import vytcene_tema
        return vytcene_tema(question)
    except Exception:
        return ""


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
        from scripts.hans_entities import EntityStore
        from scripts.config_io import load as _cio_load   # HANS_RECALL_CONFIG_IO_V1
        cfg = _cio_load()
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


# HANS_READING_CHAPTERS_V1 — sufix „— kap. 41" na konci názvu knihy.
# Kotví se na KONEC, ať to nesebere číslo z názvu samotného díla.
_KAP_PAT = re.compile(r"\s*[—–-]?\s*kap\.?\s*(\d+)\s*$", re.I)


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
    kapitoly = {}          # HANS_READING_CHAPTERS_V1 — čísla kapitol na klíč
    for r in rows:
        t = (r[2] or "").strip().lower()
        if not t:
            poradi.append(r)          # bez názvu nelze slučovat
            continue
        # HANS_READING_CHAPTERS_V1 (3.9.) — kniha čtená po kapitolách zabrala
        # celý výpis: „Já robot — kap. 41 / 40 / 39 / 38" jsou čtyři různé
        # tituly, takže je dedup na titulu neslučoval. Klíč je proto kniha BEZ
        # kapitoly; rozsah se vrátí do názvu níž, ať se neztratí, kolik toho
        # přečetl.
        m = _KAP_PAT.search(t)
        klic = t[:m.start()].strip(" -—–") if m else t
        if m:
            try:
                kapitoly.setdefault(klic, []).append(int(m.group(1)))
            except ValueError:
                pass
        stav = nej.get(klic)
        if stav is None:
            nej[klic] = r
            poradi.append(("__klic__", klic))
        elif delsi_vyhrava and len(str(r[3] or "")) > len(str(stav[3] or "")):
            # ponech novější datum, ale obsažnější úryvek
            nej[klic] = (stav[0], stav[1], stav[2], r[3])
    out = []
    for x in poradi:
        if isinstance(x, tuple) and len(x) == 2 and x[0] == "__klic__":
            r = nej[x[1]]
            ks = sorted(set(kapitoly.get(x[1], [])))
            if len(ks) > 1:            # sloučeno víc kapitol → ukaž rozsah
                titul = _KAP_PAT.sub("", str(r[2] or "")).strip(" -—–")
                r = (r[0], r[1], "%s — kap. %d–%d" % (titul, ks[0], ks[-1]),
                     r[3])
            out.append(r)
        else:
            out.append(x)
    return out


# ── HANS_DREAM_RECALL_V1 (5.9.) — NA SEN SE DALO ZEPTAT AZ TED ──────────────
# Nalez z testovaciho rozhovoru 5. 9.: „zdalo se ti dneska neco?" -> Hans
# odpovedel improvizovanou uvahou o fotografii. Pritom ma v deniku 303 zaznamu
# `event_type='dream'` a od 5. 9. v nich dokonce sam vystupuje jako postava.
# Retezec „zdalo" nebyl v kodu jako vzor ANI JEDNOU, takze dotaz propadl do
# volneho hovoru a model si sen vymyslel.
#
# Volny hovor smi fabulovat [[free-chat-may-confabulate]], ale tohle je NAROK
# NA VLASTNI PAMET — tatáž trida jako `HANS_REMEMBER_HONEST_V1` ze 4. 9.:
# Hans ma zaznam a misto nej vyda vymysl.
#
# Deterministicky, bez LLM — presne jako `reading_answer` o kus niz.
_DREAM_DEN_ZACATEK_H = 18   # sen zapsany po 18:00 patri k NOCI, ktera prave zacina


def _je_z_posledni_noci(ts: float) -> bool:
    """Patri sen k noci, ktera prave skoncila (nebo zacina)?

    ⚠️ Neni to „dnesni datum". Sny se zapisuji ve dvou vlnach — zmereno
    na 303 zaznamech: **188 ve 22 hodin a 94 o pulnoci** — takze sen z 22:01
    vcerejsiho dne je „dnes v noci", kdezto podle kalendare je vcerejsi.
    Hranice je proto 18:00 predchoziho dne, ne pulnoc.
    """
    ted = datetime.now()
    dnes0 = ted.replace(hour=0, minute=0, second=0, microsecond=0)
    hranice = dnes0.timestamp() - (24 - _DREAM_DEN_ZACATEK_H) * 3600
    return float(ts) >= hranice


def _pta_se_na_dnesek(question: str) -> bool:
    """Ptal se vylozene na DNESNI noc? (pak je „nic" poctiva odpoved)"""
    q = _fold(question or "").lower()
    return bool(re.search(r"\b(dnes\w*|dneska|v noci|tuhle noc|te noci|"
                          r"minulou noc|posledni noc)\b", q))


def dream_answer(db_path: str, question: str = "",
                 limit: int = 3, asker: Optional[str] = None) -> str:
    """Co se Hansovi zdalo — z deniku, deterministicky.

    S tematem v dotazu -> hledani mezi sny; bez tematu -> posledni sen.
    """
    oslov = _cz_address(asker) if asker else "pane"
    topic = _extract_topic(question)
    conn = None
    try:
        conn = _ro(db_path)
        if topic:
            rows = []
            videno = set()
            for stem in _topic_stems(topic):
                for r in conn.execute(
                        "SELECT ts, COALESCE(NULLIF(note,''),data) "
                        "FROM diary WHERE event_type='dream' "
                        "AND COALESCE(NULLIF(note,''),data) LIKE ? "
                        "ORDER BY ts DESC LIMIT ?", ("%%%s%%" % stem, limit * 3)):
                    # LIKE je jen LEVNÝ PŘEDVÝBĚR — sám o sobě chytá i pahýl
                    # UVNITŘ jiného slova. Doloženo při stavbě: téma „hradech"
                    # (pahýl „hrad") vytáhlo sen o ZA-HRAD-Ě a Hans na dotaz po
                    # hradech odpověděl „ano, zdálo se mi" a vypsal zahradu.
                    # Falešné potvrzení je horší než žádný nález, proto se
                    # shoda ověří ještě na ZAČÁTKU SLOVA.
                    if not re.search(r"(?<![\w])" + re.escape(stem),
                                     r[1] or "", re.IGNORECASE):
                        continue
                    if r[0] in videno:
                        continue
                    videno.add(r[0]); rows.append(r)
            rows.sort(key=lambda r: -r[0])
            rows = rows[:limit]
            if not rows:
                # ⚠️ Formulace je schválně o HLEDÁNÍ, ne o neexistenci.
                # `_topic_stems` je hrubý pahýl a české střídání kmene mu
                # uteče: doloženo „o vlacích" × sen o „vlacích" zapsaný jako
                # „vlaky" (k/c). Tvrdit za takového stavu „nic se mi nezdálo"
                # by bylo falešné zapření — což je tatáž třída chyby, kterou
                # tenhle příkaz opravuje. Radši přiznat mez hledání.
                return ("Sny si zapisuji, %s, ale o „%s“ jsem v nich "
                        "nic nenašel. Nebudu si vymýšlet — zkuste to prosím "
                        "říct jinak, hledám podle slov." % (oslov, topic))
            lines = ["– %s: %s" % (_cz_date(ts), _zkrat_sen(txt))
                     for ts, txt in rows]
            return ("Ano, %s — o „%s“ se mi zdálo:\n%s"
                    % (oslov, topic, "\n".join(lines)))
        row = conn.execute(
            "SELECT ts, COALESCE(NULLIF(note,''),data) FROM diary "
            "WHERE event_type='dream' ORDER BY ts DESC LIMIT 1").fetchone()
        if not row or not (row[1] or "").strip():
            return "Žádný sen zatím zapsaný nemám, %s." % oslov
        ts, txt = row[0], row[1]
        if _je_z_posledni_noci(ts):
            return "Dnes v noci se mi zdálo tohle, %s:\n%s" % (oslov, _zkrat_sen(txt))
        if _pta_se_na_dnesek(question):
            # Nevydavat starsi sen za dnesni — to je tatáž trida chyby jako
            # falesny narok na pamet.
            return ("Dnes v noci se mi nic nezdálo, %s — aspoň nic, co bych si "
                    "byl zapsal. Poslední sen mám z %s:\n%s"
                    % (oslov, _cz_date(ts), _zkrat_sen(txt)))
        return ("Poslední sen mám v deníku z %s, %s:\n%s"
                % (_cz_date(ts), oslov, _zkrat_sen(txt)))
    except Exception as e:
        _log.debug("dream_answer: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _zkrat_sen(txt: str, limit: int = 600) -> str:
    t = " ".join(str(txt or "").split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def reading_answer(db_path: str, question: str = "",
                   limit: int = 4, asker: Optional[str] = None) -> str:
    """Co/kdy jsem četl — reálné čtecí eventy z deníku, deterministicky.
    S tématem v dotazu → hledání; bez → poslední čtení.

    HANS_READING_ASKER_V1 (5.9.) — oslovení podle TAZATELE. Dosud tu byly
    ctyri natvrdo psane „pane", takze vypis cetby oslovoval „pane" i zenu.
    `_cmd_cetl` jmeno mel a jen ho nepredaval. Vzor je tentyz jako o par set
    radku vys (`oslov = _cz_address(asker) if asker else "pane"`), aby v tomhle
    souboru nebyly dve pravdy o osloveni.
    ⚠️ „pane" zustava JEN jako fallback pro volani BEZ tazatele (skripty,
    regresni behoun) — tam neni z ceho vokativ odvodit."""
    oslov = _cz_address(asker) if asker else "pane"   # HANS_READING_ASKER_V1
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
                return (f"Prošel jsem svůj deník, {oslov} — o „{topic}“ v něm "
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
            return (f"Ano, {oslov} — tohle mám o „{topic}“ ve svém deníku "
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
            return (f"V deníku zatím žádné čtení zapsané nemám, {oslov}.")
        lines = []
        for ts, etype, title, snip in rows:
            t = (title or "").strip() or "(bez názvu)"
            kind = {"book_read": "kniha", "study_note": "studium",
                    "book_completion_reflection": "dočtená kniha"}.get(
                        etype, "četba")
            lines.append(f"– {_cz_date(ts)} ({kind}): {t}")
        return (f"Podle mého deníku jsem naposledy četl toto, {oslov}:\n"
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
        # HANS_FILM_DAY_RANGE_V1 (23. 9.) — okno znalo JEN slovo „dnes“, takže
        # „co jsi včera viděl za filmy?“ vrátilo výpis posledních (tedy
        # DNEŠNÍCH) filmů. Doloženo sadou A 23. 9. Protažen hotový parser
        # `resolve_time_range` (včera, předevčírem, den v týdnu, datum,
        # tento/minulý týden) — týž, který používá recall rozhovorů.
        # 📏 Reálně 0× z 1 584 výměn; je to pokrytí tvaru, ne častá vada.
        _rng = None
        if not today:
            try:
                _rng = resolve_time_range(q)
            except Exception:
                _rng = None
        if _rng:
            _lbl = (_rng[2] or "").strip()
            _lbl = (_lbl[:1].upper() + _lbl[1:]) if _lbl else "V tu dobu"
            rows = conn.execute(
                "SELECT ts, title FROM diary WHERE event_type='kodi_playing' "
                "AND ts >= ? AND ts < ? ORDER BY ts DESC",
                (_rng[0], _rng[1])).fetchall()
            _seen, _tit = set(), []
            for _ts, _t in rows:
                _t = (_t or "").strip()
                if _t and _t.lower() not in _seen:
                    _seen.add(_t.lower())
                    _tit.append(_t)
                if len(_tit) >= limit * 2:
                    break
            if not _tit:
                return ("%s jsem podle deníku žádný film ani pořad "
                        "nesledoval, pane. Nebudu si nic vymýšlet." % _lbl)
            return "%s jsem u obrazovky zaznamenal: %s." % (
                _lbl, "; ".join("„%s“" % t for t in _tit))
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


def artwork_answer(db_path: str, question: str = "", limit: int = 5) -> str:
    """HANS_ARTWORK_RECALL_V1 (30.8.) — CO JSEM NAMALOVAL, z deníku, bez LLM.

    PROČ: na „namaloval jsi neco novyho?" Hans odpověděl *„Vzhledem k mé roli
    nemám možnost vytvářet obrazy samostatně, malování nastane pouze na základě
    vašeho pokynu"* a na „ukaz mi to" dokonce *„jsem textový asistent"*.
    Obojí je DOLOŽENĚ NEPRAVDA — `artwork` má 89 záznamů za 30 dní, poslední
    „Sen" vznikl v noci bez jakéhokoli pokynu.

    Táž třída jako „Proud krve" (14.7.): **zapírá, co ví**. Tehdy se to opravilo
    pro filmy (`film_knowledge_answer`) a CLAUDE.md si nechala otevřené „další
    zapíraná kategorie, až se najde reálný případ". Tohle je ten případ, takže
    se PROTAHUJE hotový vzor (`films_watched_answer`), nestaví nový mechanismus.

    ⚠️ Nestačilo by doručit `hans_capabilities`: „umím malovat" je odpověď na
    JINOU otázku. Tady se ptá na PROVEDENOU PRÁCI → musí přijít z dat.
    """
    conn = None
    try:
        conn = _ro(db_path)
        q = (question or "").lower()
        dnes = "dnes" in q or "dneska" in q
        # HANS_WORK_RECALL_IN_ARTWORK_V1 (15. 9.) — "vytvoril jste k tomu dilo?"
        # sedne na vzor /obrazy (_ART_MINULE zna "vytvoril"), jenze DILO neni
        # obraz: `work_artifact` (stranka k dostudovanemu tematu) sem nechodil.
        # Doloženo 15. 9.: na dotaz na dilo k hudbe Hans ukazal obraz a pak
        # tvrdil, ze skladby nevytvoril, ackoli "Dilo: hudba" existuje.
        # Rozhoduje slovo v dotazu: dilo/skladba/web bez slova o malovani.
        if (re.search(r"\bd[\u00edi]l(?:o|a|u|e|em)\b|skladb|\bweb|str[\u00e1a]nk", q)
                and not re.search(r"obraz|malov|namal|kresl", q)):
            import json as _json_d
            _temata = []
            for _ts, _tit, _dat in conn.execute(
                    "SELECT ts, title, data FROM diary WHERE event_type='work_artifact' "
                    "ORDER BY ts DESC LIMIT 40").fetchall():
                try:
                    _tp = (_json_d.loads(_dat or "{}") or {}).get("topic") or ""
                except Exception:
                    _tp = ""
                _tp = _tp or re.sub(r"^D\u00edlo:\s*", "", _tit or "")
                if _tp and _tp.lower() not in [x[1].lower() for x in _temata]:
                    _temata.append((_ts, _tp))
                if len(_temata) >= 3:
                    break
            if _temata:
                # HANS_DILO_TENSE_V1 (2. 10.) — dřív „webovou stránku, kterou
                # SESTAVÍM, když téma dostuduji“ i o HOTOVÉM díle → tazatel
                # (B21) to právem četl jako slib a Hans se zamotal.
                _out = ("Naposledy jsem vytvo\u0159il d\u00edlo k t\u00e9matu \u201e%s\u201c (%s) \u2014 "
                        "webovou str\u00e1nku; takovou stavím v\u017edy, kdy\u017e t\u00e9ma dostuduji."
                        % (_temata[0][1], _cz_when(_temata[0][0])))
                if len(_temata) > 1:
                    _out += " P\u0159edt\u00edm k t\u00e9mat\u016fm: %s." % ", ".join(
                        "\u201e%s\u201c" % _t for _, _t in _temata[1:])
                _obr = conn.execute(
                    "SELECT title FROM diary WHERE event_type='artwork' "
                    "ORDER BY ts DESC LIMIT 1").fetchone()
                if _obr and _obr[0]:
                    _out += " Obrazy maluji zvl\u00e1\u0161\u0165 \u2014 naposledy \u201e%s\u201c." % _obr[0]
                return _out
        if dnes:
            midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            rows = conn.execute(
                "SELECT ts, title, note FROM diary WHERE event_type='artwork' "
                "AND ts >= ? ORDER BY ts DESC", (midnight,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, title, note FROM diary WHERE event_type='artwork' "
                "ORDER BY ts DESC LIMIT ?", (limit * 6,)).fetchall()
        if not rows:
            return ("Nemám záznam o žádném obrazu, který bych "
                    + ("dnes " if dnes else "") +
                    "namaloval, pane. Nebudu si nic vymýšlet.")
        videno, dila = set(), []
        for ts, title, note in rows:
            t = (title or "").strip()
            if t and t.lower() not in videno:
                videno.add(t.lower())
                dila.append((ts, t, (note or "").strip()))
            if len(dila) >= limit:
                break
        if dnes:
            return "Dnes jsem namaloval: %s." % "; ".join(
                "„%s“" % t for _, t, _ in dila)
        ts0, t0, note0 = dila[0]
        out = "Naposledy jsem maloval „%s“ (%s)." % (t0, _cz_when(ts0))
        if note0:
            # vlastní poznámka k obrazu — Hansův komentář, ne domněnka
            out += " Poznamenal jsem si k tomu: %s" % note0.rstrip(".") + "."
        if len(dila) > 1:
            out += " Předtím: %s." % "; ".join("„%s“" % t for _, t, _ in dila[1:])
        # HANS_ARTWORK_WHERE_V1 (30.8.) — KDE je obraz k vidění. Bez tohohle
        # Hans na navazující „ukaz mi to" odpověděl „nemám možnost zobrazovat
        # obrazy jen tak ze záznamu", což je NEPRAVDA: obrazy leží v
        # `data/hans_art/` a jsou na nástěnce web adminu. Věta se dostane do
        # historie hovoru, takže na ni model může navázat.
        out += " Obrazy mám na nástěnce (Co Hans namaloval)."
        return out
    except Exception:
        return "K obrazům teď nemám přístup do deníku, pane."
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


_NAZOR_NEZNA = ("v\u00edm jen velmi m\u00e1lo", "v\u00edm jen m\u00e1lo", "nezn\u00e1m",
                "ne\u010detl jsem", "nevid\u011bl jsem", "nem\u00e1m z\u00e1znam")


def _nazor_prvni_veta(data: str) -> Optional[str]:
    """HANS_FILM_OPINION_ANAFORA_V1 — první věta vlastního názoru na film,
    nebo None (krátká, přiznání neznalosti, jméno z domácnosti).
    Vytaženo z `films_liked_answer`, aby obě odpovědi měly TÝŽ filtr."""
    veta = (data or "").strip().split("\n")[0].strip()
    m = re.search(r"^(.{20,180}?[.!?])(\s|$)", veta)
    if m:
        veta = m.group(1).strip()
    elif len(veta) > 180:
        veta = veta[:180].rstrip() + "\u2026"
    if len(veta) < 20:
        return None
    if any(_n in veta.lower() for _n in _NAZOR_NEZNA):
        return None
    if _jmenuje_domacnost(veta):
        return None
    return veta


# HANS_FILM_OPINION_ANAFORA_V1 — pozná VÝPIS filmů od `films_watched_answer`
# (všechny tři tvary: dnes, časové okno, „Naposledy jsem sledoval“).
_VYPIS_FILMU_PAT = re.compile(
    r"^(Naposledy jsem sledoval|Dnes jsem u obrazovky zaznamenal"
    r"|.{0,40}? jsem u obrazovky zaznamenal)")


def film_list_titles(text: str) -> list:
    """Tituly z Hansova výpisu filmů; [] když text výpisem není."""
    t = (text or "").strip()
    if not _VYPIS_FILMU_PAT.search(t):
        return []
    return [x.strip() for x in re.findall(r"„([^“]{1,120})“", t) if x.strip()]


def nazor_k_filmu(db_path: str, title: str) -> Optional[str]:
    """HANS_FILM_RECOMMEND_V1 — první věta Hansova vlastního názoru na PŘESNĚ
    tento titul (`movie_opinion`), jinak None. `title = ?`, ne `lower()` —
    SQLite `lower()` nemění ne-ASCII („Čelisti“)."""
    t = (title or "").strip()
    if not t:
        return None
    conn = None
    try:
        conn = _ro(db_path)
        for (data,) in conn.execute(
                "SELECT data FROM diary WHERE event_type='movie_opinion' "
                "AND title = ? AND data IS NOT NULL AND trim(data) != '' "
                "ORDER BY ts DESC LIMIT 5", (t,)).fetchall():
            v = _nazor_prvni_veta(data)
            if v:
                return v
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def films_liked_among(db_path: str, titles: list, limit: int = 2) -> str:
    """HANS_FILM_OPINION_ANAFORA_V1 (23. 9.) — „a který se ti z nich líbil
    nejvíc?“ po výpisu filmů. Názor se bere JEN k filmům z toho výpisu.
    Doloženo sadou A 23. 9.: anafora nenesla slovo „film“, šla přes
    self_state a Hans si estetiku filmu vymyslel. K žádnému názor → přizná."""
    if not titles:
        return ""
    conn = None
    ven = []
    try:
        conn = _ro(db_path)
        for t in titles:
            tl = t.lower()
            rows = conn.execute(
                "SELECT title, data FROM diary WHERE event_type='movie_opinion' "
                "AND data IS NOT NULL AND trim(data) != '' AND title IS NOT NULL "
                "AND (lower(title) = ? OR instr(lower(title), ?) > 0 "
                "OR instr(?, lower(title)) > 0) ORDER BY ts DESC LIMIT 5",
                (tl, tl, tl)).fetchall()
            for _tt, data in rows:
                # krátký titul (≤ 3 znaky) jen při přesné shodě
                if len((_tt or "").strip()) <= 3 and (_tt or "").lower() != tl:
                    continue
                v = _nazor_prvni_veta(data)
                if v:
                    ven.append((t, v))
                    break
            if len(ven) >= max(1, limit):
                break
    except Exception:
        return ""
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    if not ven:
        return ("K žádnému z těch filmů nemám zapsaný vlastní názor, pane, "
                "takže si nebudu vymýšlet, který se mi líbil nejvíc.")
    if len(ven) == 1:
        return "Z nich mě nejvíc zaujal „%s“. %s" % ven[0]
    return ("Názor mám zapsaný k těmhle z nich:\n"
            + "\n".join("\u2013 %s \u2014 %s" % (t, v) for t, v in ven))


def book_from_thread(db_path: str, texts: list) -> Optional[dict]:
    """HANS_BOOK_ORIGIN_V1 — kniha z `hans_library`, o které vlákno mluví.
    Shoda = slovo titulu (≥ 5 znaků) se ve vlákně objeví jako začátek
    slova (bez diakritiky). Nejnověji založená kniha vyhrává."""
    txt = " " + _fold(" ".join(str(x) for x in (texts or []))).lower()
    if not txt.strip():
        return None
    conn = None
    try:
        conn = _ro(db_path)
        rows = conn.execute(
            "SELECT book_id, book_title, author, COALESCE(url,'') FROM "
            "hans_library WHERE status IN ('reading','finished') "
            "ORDER BY started_at DESC").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    for bid, title, author, url in rows:
        slova = [w for w in re.findall(r"\w+", _fold(title or "").lower())
                 if len(w) >= 5]
        if any(re.search(r"(?<!\w)" + re.escape(w[:6]), txt) for w in slova):
            return {"id": bid or "", "title": title or "", "author": author or "",
                    "url": url or ""}
    return None


def book_origin_answer(book: dict) -> str:
    """HANS_BOOK_ORIGIN_V1 (23. 9.) — odkud Hans knihu MÁ, z `hans_library`.
    Doloženo sadou A: „kde jsi ji vzal?“ (o čtené knize) → výpis webových
    zdrojů. Knihy jsou buď nahrané uživatelem (`user_*`), nebo z Project
    Gutenberg. 🔒 Bez jména — kdo knihu nahrál, se neříká (smí to slyšet
    i cizí)."""
    t = book.get("title") or "tu knihu"
    bid = book.get("id") or ""
    url = book.get("url") or ""
    if bid.startswith("user_"):
        return ("Knihu „%s“ mi do knihovny nahrál někdo z domácnosti, pane — "
                "sám jsem si ji nesháněl." % t)
    if not url:
        try:
            from scripts.hans_library import _FALLBACK_BOOKS
            url = next((b.get("url") or "" for b in _FALLBACK_BOOKS
                        if b.get("id") == bid), "")
        except Exception:
            url = ""
    if "gutenberg" in url:
        return ("Knihu „%s“ jsem si stáhl z Project Gutenberg, kde je volně "
                "dostupná, pane: %s" % (t, url))
    return ("Knihu „%s“ mám ve své knihovně, ale odkud přesně pochází, "
            "zapsané nemám, pane." % t)


_PRUBEH_RAMEC = {
    "poprve", "dlouho", "ctes", "ctete", "znovu", "kapitole", "kapitola",
    "kapitolu", "kapitol", "ktere", "kterou", "kolikate", "kolikatou", "nekdy",
    "driv", "drive", "predtim", "jsi", "jste", "knihu", "kniha", "knize",
    "tuhle", "tahle", "cetl", "cetla", "opakovane", "prave", "porad", "jeste",
    "vlastne", "vubec", "tedy", "takze", "jakou", "jake", "jaky", "rikal",
    "rikate", "rikas", "myslim", "prosim", "hansi"}


def _PRUBEH_OBSAHOVA_SLOVA(question: str) -> bool:
    """Nese otázka o průběhu čtení JMÉNO (slovo ≥ 4 mimo rámec dotazu)?"""
    return any(len(w) >= 4 and w not in _PRUBEH_RAMEC
               for w in re.findall(r"\w+", _fold(question or "").lower()))


def _kniha_z_otazky(db_path: str, question: str) -> Optional[dict]:
    """HANS_BOOK_PROGRESS_ANSWER_V1 — kniha z `hans_library` jmenovaná
    v otázce: slovo otázky (≥ 4 znaky) je ZAČÁTKEM slova titulu (≥ 5).
    „le guin" → „Le Guinova Ursula – …" (autor je v titulu)."""
    slova_q = [w for w in re.findall(r"\w+", _fold(question or "").lower())
               if len(w) >= 4]
    if not slova_q:
        return None
    conn = None
    try:
        conn = _ro(db_path)
        rows = conn.execute(
            "SELECT book_id, book_title, author, COALESCE(url,'') FROM "
            "hans_library WHERE status IN ('reading','finished') "
            "ORDER BY started_at DESC").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    for bid, title, author, url in rows:
        slova_t = [w for w in re.findall(r"\w+", _fold(title or "").lower())
                   if len(w) >= 5]
        # oboustranně: „guin" → „guinova", „doriana" → „dorian"
        if any(t.startswith(q) or (len(q) >= 5 and q.startswith(t))
               for q in slova_q for t in slova_t):
            return {"id": bid or "", "title": title or "", "author": author or "",
                    "url": url or ""}
    return None


def book_progress_answer(db_path: str, question: str,
                         thread_texts: Optional[list] = None) -> str:
    """HANS_BOOK_PROGRESS_ANSWER_V1 (23. 9.) — ODKDY a JAK DLOUHO knihu čtu,
    na které jsem kapitole a jestli POPRVÉ. Deterministicky z `hans_library`
    a deníku (`book_read`), bez LLM.

    Doloženo 23. 9.: „tu knihu od le guin čteš poprvé?" → „četl jsem ji již
    dříve" a „jak dlouho už ji čteš?" → „dva dny" (skutečně poprvé, 6 dní).
    Popisek v podkladu (`HANS_BOOK_PROGRESS_LABEL_V1`) model PŘEHLÍŽEL a LLM
    router `/cetl` zamítl — jenže ani `/cetl` průběh neuměl. Rozhodnutí se
    proto nedává modelu: odpověď je z dat. 📏 Reálně 1× z 2 342 vět.
    Kniha: z otázky → z vlákna → jediná rozečtená. Nenajde-li se, vrací ''
    (volající jde dosavadní cestou)."""
    kn = _kniha_z_otazky(db_path, question)
    # Jmenuje-li otázka něco, co v knihovně NENÍ („čteš Doriana Graye?"
    # a Dorian tam není), NESMÍ se odpovědět o jiné knize z vlákna.
    if not kn and _PRUBEH_OBSAHOVA_SLOVA(question):
        return ""
    if not kn and thread_texts:
        kn = book_from_thread(db_path, thread_texts)
    conn = None
    try:
        conn = _ro(db_path)
        if not kn:
            rows = conn.execute(
                "SELECT book_id, book_title FROM hans_library "
                "WHERE status='reading'").fetchall()
            if len(rows) != 1:
                return ""
            kn = {"id": rows[0][0], "title": rows[0][1]}
        r = conn.execute(
            "SELECT started_at, finished_at, status, current_chapter, "
            "total_chapters FROM hans_library WHERE book_id=? "
            "ORDER BY started_at DESC LIMIT 1", (kn["id"],)).fetchone()
        if not r:
            return ""
        pocet_cteni = conn.execute(
            "SELECT COUNT(*) FROM hans_library WHERE book_id=?",
            (kn["id"],)).fetchone()[0]
        kap = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT title), MAX(ts) FROM diary "
            "WHERE event_type='book_read' AND title LIKE ?",
            (kn["title"] + " \u2014 kap.%",)).fetchone()
    except Exception:
        return ""
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    start, fin, status, cur, tot = r
    if not start:
        return ""
    d = lambda x: datetime.fromtimestamp(x).strftime("%-d. %-m.")
    poprve = pocet_cteni <= 1 and (not kap or kap[0] == kap[1])
    konec = fin if (status == "finished" and fin) else time.time()
    dni = max(0, int((konec - start) // 86400))
    dni_s = ("necelý den" if dni == 0 else "%d %s" % (
        dni, "den" if dni == 1 else ("dny" if dni < 5 else "dní")))
    t = kn.get("title") or "tu knihu"
    if status == "finished" and fin:
        out = ("„%s“ jsem dočetl %s, pane — četl jsem ji od %s, tedy %s."
               % (t, d(fin), d(start), dni_s))
    else:
        out = "„%s“ čtu od %s, pane, tedy %s" % (t, d(start), dni_s)
        if cur:
            _ts = str(tot or "")
            # „ze" před sto…/sedm…/sedmnáct… (ze 113, ze 7, ze 17)
            _pr = ("ze" if (_ts[:1] == "7" or _ts[:2] == "17"
                            or (len(_ts) == 3 and _ts[:1] == "1")) else "z")
            out += " — teď jsem u kapitoly %s%s" % (
                cur, (" %s %s" % (_pr, tot)) if tot else "")
            if kap and kap[2]:
                out += " (naposledy %s)" % d(kap[2])
        out += "."
    out += (" Podle deníku ji čtu poprvé." if poprve and status != "finished"
            else " Četl jsem ji poprvé." if poprve
            else " Podle deníku jsem ji četl víckrát.")
    return out


def films_liked_answer(db_path: str, limit: int = 3) -> Optional[str]:
    """HANS_FILM_OPINION_ANSWER_V1 (22. 9.) — na dotaz \u201ekter\u00fd film se ti
    l\u00edbil?\u201c odpov\u011bz z VLASTN\u00cdCH n\u00e1zor\u016f (`movie_opinion`), ne v\u00fdpisem
    sledovan\u00fdch (`kodi_playing`).

    Doloženo pam\u011b\u0165ovou sadou 22. 9.: na \u201ea jak\u00fd film se ti l\u00edbil?\u201c vr\u00e1til
    Hans seznam naposledy sledovan\u00fdch \u2014 co\u017e na ot\u00e1zku po OBLIB\u011a neodpov\u00edd\u00e1.
    Je to t\u0159et\u00ed v\u00fdskyt t\u0159\u00eddy \u201ev\u00fdpis m\u00edsto odpov\u011bdi\u201c (po `/sen`
    a `/anomalie`, `HANS_LIST_NOT_CLAIM_V1` 21. 9.).
    📌 Data pro to existuj\u00ed: `movie_opinion` m\u00e1 2 205 z\u00e1znam\u016f s obsahem
    ve sloupci `data` \u2014 jen k nim \u017e\u00e1dn\u00e1 odpov\u011b\u010f nesahala.
    ⚠️ Obsah je v `data`, ne v `note` \u2014 t\u00e1\u017e past jako u `reading_takeaway`.

    Vrac\u00ed CS v\u011btu nebo None (\u017e\u00e1dn\u00fd n\u00e1zor \u2192 vol\u00e1 se dosavadn\u00ed cesta).
    """
    conn = None
    try:
        conn = _ro(db_path)
        rows = conn.execute(
            "SELECT title, data FROM diary WHERE event_type='movie_opinion' "
            "AND data IS NOT NULL AND trim(data) != '' AND title IS NOT NULL "
            "AND trim(title) != '' ORDER BY ts DESC LIMIT 60").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    # ⚠️ Tytez film mivá v deniku VIC TITULU ("Krouzek sebevrahu" x
    # "Suicide Circle: Krouzek sebevrahu"), takze pouha shoda klicu nestaci
    # a odpoved by tentyz film nabidla dvakrat. Dedup proto i na OBSAZENI.
    # ⛔ A vyrazuji se zaznamy, kde Hans priznava, ze film NEZNA — na otazku
    # "co te zaujalo" je priznani neznalosti spatna odpoved (na to ma jine
    # cesty). Doloženo pri stavbe: treti polozka znela "Pripustim, ze o filmu
    # vim jen velmi malo".
    _NEZNA = ("v\u00edm jen velmi m\u00e1lo", "v\u00edm jen m\u00e1lo", "nezn\u00e1m",
              "ne\u010detl jsem", "nevid\u011bl jsem", "nem\u00e1m z\u00e1znam")
    videl, ven = set(), []
    for title, data in rows:
        t = (title or "").strip()
        klic = t.lower()
        if not t or klic in videl:
            continue
        if any(klic in _v or _v in klic for _v in videl):
            continue
        # jen PRVNI veta nazoru — cely odstavec by z odpovedi udelal esej.
        # HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) — veta, ktera jmenuje clena
        # domacnosti, se vynecha (odpoved jde deterministicky i cizimu).
        # Filtr je sdileny s `films_liked_among` (HANS_FILM_OPINION_ANAFORA_V1).
        veta = _nazor_prvni_veta(data)
        if not veta:
            continue
        videl.add(klic)
        ven.append((t, veta))
        if len(ven) >= max(1, limit):
            break
    if not ven:
        return None
    if len(ven) == 1:
        return "Z film\u016f, co jsem vid\u011bl, m\u011b zaujal %s. %s" % (ven[0][0], ven[0][1])
    hlava = "Z film\u016f, co jsem vid\u011bl, m\u011b zaujaly tyhle:"
    telo = "\n".join("\u2013 %s \u2014 %s" % (t, v) for t, v in ven)
    return hlava + "\n" + telo


def _jmenuje_domacnost(text: str) -> bool:
    """HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) — jmenuje text clena domacnosti?
    Sdileny predikat `cz_names.find_known_person` (pady, diakritika), ne novy.
    ⚠️ Porovnava na prefix, takze obcas chytne i nevinne slovo (anglicke
    "old", "sarkofagy") — na strane soukromi prijatelna chyba: poznamka
    se jen vynecha. Selhani = False (dosavadni chovani)."""
    try:
        from scripts.cz_names import find_known_person
        return bool(find_known_person(text or ""))
    except Exception:
        return False


def film_knowledge_answer(db_path: str, question: str = "",
                          asker: str = "") -> Optional[str]:
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
                # krátké jednoslovné (Hra, Past…) → riziko falešné shody.
                # HANS_FILM_SHORT_TITLE_V1 (25. 9.) — VÝJIMKA, když věta titul
                # výslovně označí: „film Duna“, „o filmu Duna“, „Duna“ v uvozovkách.
                # Změřeno na 1 675 reálných větách (98 filmových dotazů): 1 změna
                # („pusť film ring“ → Ring, správně), 0 falešných.
                if not re.search(r"\bfilm\w*\s+[„\"']?" + re.escape(tf) + r"\b"
                                 r"|[„\"']" + re.escape(tf) + r"[“\"']", q_fold):
                    continue
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
        # HANS_FILM_FIRST_SEEN_V1 (16. 9.) — i MIN(ts): na dotaz \u201ekdy jsi ho
        # videl poprve?\u201c blok dosud nabizel jen \u201enaposledy\u201c a model si prvni
        # zhlednuti VYMYSLEL (16. 9.: \u201epred peti lety, v roce 2021\u201c, pritom
        # nejstarsi zaznam v deniku je z dubna 2026).
        seen = conn.execute(
            "SELECT COUNT(*), MAX(ts), MIN(ts) FROM diary "
            "WHERE event_type='kodi_playing' "
            "AND title=?", (best,)).fetchone()
        notes = [str(n).strip() for _, n in ops if n and str(n).strip()]
        # HANS_FILM_KODI_FACTS_V1 (27. 9.) — údaje PŘEHRÁVAČE do podkladu.
        # Doloženo 26. 9.: „co víte o filmu Duna?“ → blok nesl jen „viděl jsi to
        # 3×“ a model doplnil „1984, David Lynch“, ačkoli záznam přehrávání
        # měl rok 2021 i režii Villeneuve. Změřeno: z 1 493 titulů má záznam
        # rok u 611, režii u 506, děj u 959; 305 titulů nemá žádný názor, takže
        # jim podklad nesl jen počet zhlédnutí.
        fakta = []
        try:
            _kr = conn.execute(
                "SELECT COALESCE(NULLIF(note,''), data) FROM diary "
                "WHERE event_type='kodi_playing' AND title=? "
                "ORDER BY ts DESC LIMIT 1", (best,)).fetchone()
            for _cast in str((_kr or [""])[0] or "").split(" | "):
                _m = re.match(r"\s*(rok|žánr|režie|děj)[:\s]\s*(.+)", _cast)
                if not _m:
                    continue
                _k, _v = _m.group(1), _m.group(2).strip()
                if _k == "děj" and len(_v) > 400:
                    _cut = max(_v.rfind(". ", 0, 400), _v.rfind("! ", 0, 400),
                               _v.rfind("? ", 0, 400))
                    _v = _v[:_cut + 1] if _cut > 100 else _v[:400].rstrip() + "…"
                fakta.append("%s %s" % (_k, _v) if _k == "rok" else "%s: %s" % (_k, _v))
        except Exception:
            fakta = []
        # HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) — CIZIMU tazateli nedavej
        # poznamky, ktere jmenuji cleny domacnosti. Doloženo sadou B 23. 9.:
        # blok s takovou vetou lezel v promptu ciziho (ven neprosla).
        # Zmereno: 65 z 2 224 nazoru na filmy jmenuje nekoho z domacnosti,
        # 59 z 1 180 titulu se to tyka. Ctvrty zdroj tridy
        # HANS_PROMPT_HOUSEHOLD_PRIVACY_V1/V2 — tentyz predikat tazatele.
        # Prazdny `asker` chovani NEMENI (jako V1: interni cesty bez mluvciho).
        if asker and notes:
            try:
                from scripts.cz_names import is_known_person as _ikp
                if not _ikp(asker):
                    notes = [n for n in notes if not _jmenuje_domacnost(n)]
            except Exception:
                pass
        if not notes and not (seen and seen[0]):
            return None  # titul se objevil, ale nic konkrétního → nech projít dál
        parts = [f"SKUTEČNÝ ZÁZNAM o „{best}“ z TVÉHO deníku (odpověz JEN z něj, "
                 f"nic si nedomýšlej; na co tu není, přiznej „to si nevybavuji“):"]
        if notes:
            parts.append("Tvé dřívější poznámky a názory:")
            parts.extend(f"- {n}" for n in notes)
        if fakta:   # HANS_FILM_KODI_FACTS_V1
            parts.append("Údaje přehrávače o tom, co jsi viděl: " + "; ".join(fakta))
        if seen and seen[0]:
            kdy = _cz_when(seen[1], slovy=True) if seen[1] else "dříve"
            krat = "jednou" if seen[0] == 1 else f"{seen[0]}×"
            # HANS_FILM_FIRST_SEEN_V1 — \u201epoprve\u201c jen kdyz se od \u201enaposledy\u201c
            # lisi o vic nez den. Zmereno: z 483 vicekrat videnych titulu je to
            # 205; u zbylych 278 jde o reprisu v tyz den, kde by to byl sum.
            _prvni = ""
            try:
                if (len(seen) > 2 and seen[2] and seen[1]
                        and (float(seen[1]) - float(seen[2])) > 86400.0):
                    _prvni = ", poprvé %s" % _cz_when(seen[2])
            except Exception:
                _prvni = ""
            parts.append(f"(V záznamu přehrávání: viděl jsi to {krat}{_prvni}, "
                         f"naposledy {kdy}.)")
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
    cfg = {}
    try:
        from scripts.config_io import load as _cio_load   # HANS_RECALL_CONFIG_IO_V1
        cfg = _cio_load()
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
    r"co\s+bylo\s+v\s+noci|"
    # HANS_RECENT_ACTIVITY_YESTERDAY_V1 (11. 9.) — VCEREJSEK je totez jako
    # „dnes", jen jine okno. Bez teto vetve dotaz propadl modelu a ten
    # vydal DNESNI zaznamy za vcerejsi (doloženo: „zopakoval jsem si
    # zaznamy o filmu…", ktere vznikly 13 minut pred dotazem).
    # ⚠️ Zamerne jen 2. osoba jednotneho cisla („co jsi delal"), takze
    # „co jsme resili vcera" (nas rozhovor) ani „co rikas na to, ze jsem
    # se vcera pustil do…" (uzivateluv vcerejsek) se nechytnou.
    r"co\s+jsi\s+(?:se\s+)?v[čc]era\s+"
    r"(?:d[ěe]lal|prov[áa]d[ěe]l|dozv[ěe]d[eě]l|nau[čc]il|zjistil|[čc]etl|studoval)|"
    r"co\s+jsi\s+(?:d[ěe]lal|prov[áa]d[ěe]l)\s+v[čc]era(?:\s+v\s+noci)?|"
    r"co\s+jsi\s+v[čc]era\s+v\s+noci|"
    # HANS_RECENT_ACTIVITY_FORMS_V1 (16. 9.) — tytez dotazy v dalsich tvarech:
    # stazene „cos“ (= co jsi), VYKANI „co jste“ a podstatne jmeno
    # „vcerejsek“. Slovesa zamerne TATAZ mnozina jako vyse.
    r"cos\s+(?:v[čc]era\s+)?"
    r"(?:d[ěe]lal|prov[áa]d[ěe]l|dozv[ěe]d[eě]l|nau[čc]il|zjistil|[čc]etl|studoval)"
    r"(?:\s+v[čc]era)?|"
    r"co\s+jste\s+(?:se\s+)?(?:v[čc]era\s+)?"
    r"(?:d[ěe]lal|prov[áa]d[ěe]l|dozv[ěe]d[ěe]l|nau[čc]il|zjistil|[čc]etl|studoval)"
    r"(?:\s+v[čc]era)?|"
    r"jak\s+(?:jsi|jste)\s+str[áa]vil\s+v[čc]erej[šs]ek|"
    r"co\s+bylo\s+v[čc]era)",
    re.I,
)

# HANS_RECENT_ACTIVITY_FORMS_V1 — co NESMI projit, i kdyz tam slovo „vcera“ je.
# Puvodni vzor to resil tim, ze byl uzky; po rozsireni to musi rict vylucne.
#   „co jsme resili vcera“        = nas ROZHOVOR, ne Hansuv den
#   „ze jsem se vcera pustil do…“ = UZIVATELUV den
#   „rikal jsi vcera, ze…“        = odkaz na drivejsi repliku
_RECENT_NOT_RE = re.compile(
    r"(co\s+jsme|jsme\s+se\s+v[čc]era|jsem\s+se\s+v[čc]era|"
    r"jsi\s+[řr][íi]kal|jsi\s+[řr][íi]kala)",
    re.I,
)


def is_recent_activity_query(text: str) -> bool:
    """Ptá se uživatel „co jsi se dnes dozvěděl / co sis zapsal / jaké
    zajímavosti dnes / co jsi dnes dělal"? Deterministický gate."""
    f = _fold(text or "")
    if _RECENT_NOT_RE.search(f):   # HANS_RECENT_ACTIVITY_FORMS_V1
        return False
    return bool(_RECENT_QUERY_RE.search(f))


def _okno_aktivity(text: str, days: int):
    """HANS_RECENT_ACTIVITY_YESTERDAY_V1 — z dotazu urci ČASOVÉ OKNO.

    Vrací (od, do, popis). `do=None` = bez horní meze (dosavadní chování).
    ⚠️ Horní mez je to podstatné: bez ní vrátí dotaz na VČEREJŠEK i dnešní
    záznamy — tedy přesně tu záměnu, kvůli které to vzniklo."""
    import datetime as _dt
    f = _fold(text or "")
    if re.search(r"\bv[čc]era\b", f):
        dnes = _dt.datetime.now().replace(hour=0, minute=0, second=0,
                                          microsecond=0)
        vcera = dnes - _dt.timedelta(days=1)
        # ⚠️ „VČERA V NOCI" NENÍ kalendářní včerejšek. Noc z 10. na 11. patří
        # kalendářně z větší části na 11., ale mluvčí jí myslí „ta, co právě
        # skončila". Doloženo daty: studium v tu noc má značku 11. 9. 03:04.
        # Kalendářní okno by ji vyloučilo a Hans by o té noci neřekl NIC.
        if re.search(r"\bnoc", f):
            return ((vcera + _dt.timedelta(hours=18)).timestamp(),
                    (dnes + _dt.timedelta(hours=9)).timestamp(),
                    "Záznamy z noci na %s" % dnes.strftime("%d.%m.%Y"))
        return (vcera.timestamp(), dnes.timestamp(),
                "Záznamy z %s" % vcera.strftime("%d.%m.%Y"))
    return (time.time() - days * 86400.0, None, "")


def recent_activity_answer(db_path: str, days: int = 1,
                           max_items_per_type: int = 3,
                           text: str = "") -> Optional[str]:
    """HANS_RECENT_ACTIVITY_V1 — deterministický recall Hansovy vlastní
    aktivity za posledních N dní (default 1 = dnešek). Vrátí grounded blok
    z deníku (study_note, book_reflection, reading_takeaway, web_read,
    movie_opinion, introspection, spontaneous, art_generated) — Hans z toho
    LLM vytvoří lidskou odpověď, ale je grounded ve faktech.

    Účel: opravit false-negative anti-konfab („nemám záznam") na dotaz na
    dnešní aktivitu, když Hans REÁLNĚ dnes něco dělal a to je v deníku.
    """
    since, _do, _popis = _okno_aktivity(text, days)
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
                # HANS_RECENT_ACTIVITY_YESTERDAY_V1 — HORNI MEZ
                "AND (? < 0 OR ts < ?) "
                "AND coalesce(note, data, '') != '' "
                "AND coalesce(data,'') NOT LIKE '{\"template\":%' "
                "ORDER BY ts DESC LIMIT ?",
                (etype, since, (-1 if _do is None else _do),
                 (0 if _do is None else _do), lim)).fetchall()
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
    return ("SKUTEČNÉ zápisky z tvého deníku (%s)" % (_popis or "za dnešek") + " (odpověz JEN z nich; "
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


# HANS_KNOWLEDGE_NOT_RELATIVE_V1 (6. 10.) — „znáte“ ve VZTAŽNÉ větě není dotaz
# na znalost. /tazatel: „…když mluvíte s někým, koho znáte delší dobu
# z domácnosti?“ → téma „delší dobu z domácnosti“ → „nemám záznamy o … stačí
# říct ‚nastuduj delší dobu z domácnosti‘“. Na 1 920 větách 3 ze 46 shod,
# všechny tři s nesmyslným tématem („dlouho“, „z domácnosti“).
_KC_VZTAZNA = re.compile(
    r"\b(?:koho|kter(?:[ée]ho|ou|[ée]|[ýy]|[ýy]m|[ýy]ch|[ée]mu)|jeho[zž]|jen[zž])"
    r"\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


def _kc_match(text: str):
    """Shoda `_KNOWLEDGE_CHECK_RE`, která není vztažnou větou. None = není dotaz."""
    t = text or ""
    m = _KNOWLEDGE_CHECK_RE.search(t)
    if m and _KC_VZTAZNA.search(t[:m.start()]):
        return None
    return m


def is_knowledge_check_query(text: str) -> bool:
    """Ptá se uživatel „znáš X?" / „co víš o X?" / „máš záznam o X?"? Levný gate.
    Regex je unicode-safe → volám na ORIGINÁLU (bez _fold), ať `_extract_topic`
    dostane originální text s diakritikou."""
    return bool(_kc_match(_reorder_object_first(text)))


def _extract_knowledge_topic(text: str) -> Optional[str]:
    """Vytáhne X z „znáš X?" — capture group regexu. Očištěno o pomocná slova."""
    m = _kc_match(text or "")
    if not m:
        return None
    x = m.group(1).strip(" .,?!;:'\"")
    # Odstranit prefix „ten/tu/to/ta/serial/film/kniha" (pomocná slova bez informace)
    # HANS_KNOWLEDGE_CHECK_V2 — skloněné tvary media-typu („o filmU/seriálU/
    # knizE X"), jinak „filmu Proud krve" nesedne na paměť → falešná nabídka
    # studia u filmu, který Hans zná.
    # HANS_KNOWLEDGE_TOPIC_ANAFORA_V1 (21. 9.) — UKAZOVACÍ ZÁJMENO NENÍ TÉMA.
    # Doloženo rozhovorem 21. 9. (A/15): „a odkud znas TEN PROJEKT?" (myšlen
    # Hansův vlastní projekt) → téma 'ten projekt' → dohledání na Wikipedii →
    # Hans odpověděl obecnou definicí z hesla „Řízení projektů" a připsal jí
    # zdroj. To je tvrzení o světě s doloženým zdrojem, tedy nejdražší druh
    # chyby — a přitom otázka mířila na něco, o čem se právě mluvilo.
    #
    # Zájmeno je ANAFORA: ukazuje do hovoru, ne na pojem. Řeší se TÝMŽ
    # způsobem, jakým funkce už odstraňuje „film/seriál/kniha" — jen o řádek
    # dřív. Táž třída jako zájmena v `_STOPWORDS` (15. 7., „četl jsi JI?").
    #
    # ⚠️ ZMĚŘENO na 1 565 skutečných zprávách, a měření OBRÁTILO první návrh:
    # téma se zájmenem vznikne jen 3× a pokaždé je to LEGITIMNÍ „co vis o tom
    # vraku u sicilie?". Plošná abstinence by zabila jediné reálné výskyty.
    # Proto se zájmeno jen ODŘÍZNE (dotaz 'vraku u sicilie' je navíc lepší)
    # a abstinuje se, teprve když po něm nezbude nic než holý kvalifikátor
    # („projekt", „film", „kniha") — tedy když ve větě žádné téma nebylo.
    # ⚠️ `(?:\s+|$)` a ne `\s+`: zájmeno i kvalifikátor stojí často NA KONCI
    # („odkud znas TU KNIHU?", „a odkud znas TOHLE?"). S požadavkem na mezeru
    # za slovem vzor na konci věty nesepne a zbude téma 'knihu' / 'tohle' —
    # týž tvar chyby jako `kamer` × „kameře" (20. 9.).
    # HANS_KNOWLEDGE_TOPIC_FILLER_V1 (7. 10.) — VÝPLŇ PŘED TÉMATEM A HOLÉ ZÁJMENO.
    # /tazatel 7. 10.: „znas neco o starych hradech?“ → téma „neco o starych
    # hradech“ → „nemám žádné záznamy“, ačkoli hrady Hans studoval; „…znas je?“
    # → téma „je“. Výplň se odřízne, osobní zájmeno tématem není (None = běžná
    # cesta, kde předmět doplní vlákno). 📏 2 369 reálných vět: 0 změn.
    x = re.sub(r"^(?:n[ěe]co(?:\s+m[áa]lo)?|n[ěe]jak\w*|cokoli\w*|p[áa]r\s+v[ěe]c[íi]|"
               r"v[íi]ce?|trochu)\s+(?:o|z|ze)\s+", "", x, flags=re.I)
    if re.fullmatch(r"(?:je|ho|ji|jej|jich|n[ěe]m|n[íi]|nich|n[ěe]j|jim|mu)", x, flags=re.I):
        return None
    x = re.sub(r"^(?:ten|ta|to|tu|toho|tom|tomu|t[ée]|ty|ti|t[ěe]ch|t[íi]m|"
               r"tohle|tenhle|tahle|tamten|tamta|onen|ona)(?:\s+|$)",
               "", x, flags=re.I)
    x = re.sub(r"^(?:seri[áa]l\w*|film\w*|kn[ií]\w+|posta?v\w*|typ\w*)(?:\s+|$)",
               "", x, flags=re.I)
    x = x.strip()
    # Zbyl holý kvalifikátor bez jména („projekt", „knihu") → ve větě žádné
    # téma nebylo. Kontroluje se jak výčtem, tak týmž vzorem jako výš —
    # výčet nemá všechny pády.
    if x and " " not in x and (
            x.lower() in _TOPIC_QUALIFIERS
            or re.fullmatch(r"(?:seri[áa]l\w*|film\w*|kn[ií]\w+|posta?v\w*"
                            r"|typ\w*)", x, flags=re.I)):
        return None
    # HANS_KNOWLEDGE_TOPIC_LEN_V1 (1. 10.) — téma delší než 6 slov není téma,
    # ale kus souvětí („třeba jiné díla s obdobnou atmosférou jako ta Falešná
    # kočička“) → bypass „nemám záznamy o ‚…‘“, ačkoli film Hans ten den četl
    # (/tazatel 1. 10.). Změřeno na 1 700 větách: skutečná témata 1–4 slova,
    # 9 a 10 slov jen 2× a obě nesmysl.
    if x and len(x.split()) > 6:
        return None
    return x or None


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
        # HANS_READING_RECALL_WORD_START_V1 (8. 10.) — dřív `LIKE %prefix%` kdekoli
        # v textu a tři NEJNOVĚJŠÍ shody: „hradu Kost“ → „na-hrad-it“ + „kost-i“
        # v článku o veganské stravě, a model si k tomu vymyslel „Kostiště“
        # (čtení „Hrad Kost“ v deníku je). Teď musí každé slovo tématu ZAČÍNAT
        # slovo titulu nebo textu, shoda v titulu jde první a jednoslovné téma
        # se bere jen z titulu (čtyřpísmenný kmen v těle textu je šum).
        # 📏 52 reálných dotazů na znalost: 9 beze změny, 24 jiný výběr, 5 šum →
        # nic („Merkuru“ → Sfinx, Strážci Galaxie), 7 nově nalezeno („vrak
        # u Sicílie“, „vyšetřování ztráty třídní knihy“).
        vzory = [re.compile(r"(?<![a-z0-9])" + re.escape(_fold(p).lower()))
                 for p in prefixes]
        conn = _ro(db_path)
        nalez = []
        for ts, title, body in conn.execute(
                "SELECT ts, coalesce(title,''), coalesce(NULLIF(note,''), data, '') "
                "FROM diary WHERE event_type IN ('web_read','reading_takeaway','study_note')"):
            ft = _fold(title).lower()
            v_titulu = all(v.search(ft) for v in vzory)
            if not v_titulu:
                if len(vzory) < 2:
                    # téma uložené při ručním čtení jako úvodní [značka] poznámky
                    # (HANS_READ_TOPIC_V1) platí jako titul — kvůli tomu čtení vzniklo
                    _zn = re.match(r"\s*\[([^\]]{1,40})\]", body or "")
                    if not (_zn and vzory[0].search(_fold(_zn.group(1)).lower())):
                        continue
                    nalez.append((0, -float(ts or 0), body))
                    continue
                fb = _fold(body).lower()
                if not all(v.search(ft) or v.search(fb) for v in vzory):
                    continue
            nalez.append((0 if v_titulu else 1, -float(ts or 0), body))
        nalez.sort(key=lambda r: r[:2])
        rows, _videno = [], set()
        for _a, _b, body in nalez:
            _k = (body or "")[:80]
            if _k in _videno:
                continue
            _videno.add(_k)
            rows.append((body,))
            if len(rows) >= 3:
                break
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
    if _topic_in_memory(db_path, topic) or _hlavni_slovo_v_pameti(db_path, topic):
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


def _hlavni_slovo_v_pameti(db_path: str, topic: str) -> bool:
    """HANS_KNOWLEDGE_HEAD_NOUN_V1 (7. 10.) — víceslovné téma, které jako celek
    v paměti není, ale jeho POSLEDNÍ slovo ano („starých hradech“ → hrady).

    `_topic_in_memory` žádá všechna slova v témže záznamu (záměr z 13. 8., brání
    falešnému „mám záznam“). Pro ZAPŘENÍ je to ale moc přísné: přívlastek
    („staré“, „gotické“) v zápisku být nemusí a Hans pak tvrdí „nic jsem si
    o tom nezapsal ani nečetl“ o tématu, které studoval — a dohledá místo toho
    náhodné heslo. Tady se nic netvrdí, jen se NEzapře: věta jde běžnou cestou.
    📏 2 613 vět: 22 zapření, 6 z nich takhle přejde na běžnou cestu."""
    w = [x for x in re.findall(r"\w+", topic or "") if len(x) >= 5]
    if len((topic or "").split()) < 2 or not w:
        return False
    try:
        return bool(_topic_in_memory(db_path, w[-1]))
    except Exception:
        return False


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
    if _topic_in_memory(db_path, topic) or _hlavni_slovo_v_pameti(db_path, topic):
        return None  # nech normální cestu, X JE v paměti
    oslov = _cz_address(asker) if asker else "pane"  # HANS_NAME_INFLECTION_V1
    # Kompaktní honestní odpověď + nabídka pokud chce ať Hans si to zapíše
    return ("V paměti nemám žádné vlastní záznamy o '%s', %s. "
            "Nic jsem si o tom nezapsal ani nečetl (obecně to znám možná "
            "z tréninku, ale nechci to vydávat za vlastní paměť). "
            "Kdybyste chtěl, mohu si to zařadit do studia — stačí říct "
            "'nastuduj %s'." % (topic, oslov, topic))


# ── HANS_SELF_STATE_V1 (5.8.) — grounded blok „jak se mám a co jsem dnes dělal"
def _popis_dila(data_json: str) -> str:
    """HANS_SELF_STATE_WORKS_V1 (30. 9.) — co dílo JE, ne jen jeho téma.
    Test 30. 9.: s řádkem „napsal jsem dílo: Dílo: <téma>“ Hans o webu se
    6 stránkami a skladbou tvrdil „první fáze vývoje nástroje“, „interaktivní
    vizualizace“, cizímu „esej“ a předstíral přehrání skladby."""
    import json as _json
    import os as _os
    try:
        d = _json.loads(data_json or "{}")
    except Exception:
        return ""
    tema = str(d.get("topic") or "").strip()
    cesta = str(d.get("path") or "")
    if not tema:
        return ""
    if d.get("target") != "coder" or not cesta:
        return "„%s“" % tema
    adr = _os.path.dirname(cesta)
    bits = []
    try:
        stranky = [f for f in _os.listdir(adr) if f.endswith(".html")]
        if stranky:
            bits.append("%d %s" % (len(stranky), "stránka" if len(stranky) == 1
                                   else "stránky" if len(stranky) < 5 else "stránek"))
        if _os.path.isdir(_os.path.join(adr, "images")) and _os.listdir(_os.path.join(adr, "images")):
            bits.append("s obrázky")
        _dj = _os.path.join(adr, "dilo.json")
        if _os.path.exists(_dj):
            _h = (_json.load(open(_dj, encoding="utf-8")) or {}).get("hudba")
            if isinstance(_h, dict) and (_h.get("popis") or _h.get("styl")):
                bits.append("se skladbou na úvodní stránce (%s)"
                            % (_h.get("popis") or _h["styl"]))
    except Exception:
        pass
    return "webové stránky na téma „%s“%s — hotové, uložené u mě" % (
        tema, (" (" + ", ".join(bits) + ")") if bits else "")


def _prvni_veta(text: str, max_chars: int = 110) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= max_chars else s[:max_chars].rsplit(" ", 1)[0] + "…"


def _self_state_trvale(conn) -> list:
    """HANS_SELF_STATE_LASTING_V1 (1. 10.) — co Hans VYTVOŘIL a co STUDUJE,
    bez ohledu na dnešek. Blok self_state nesl jen dnešní záznamy, takže web
    hotový 29. 9. v něm o den později chyběl a model na „je ta stránka
    hotová?“ odpověděl v jednom rozhovoru „v rané fázi“, „hotová“
    i „rozpracovaná“ a cizímu „teprve ji chystám“; další studium si vymýšlel
    („Jára Cimrman“), ačkoli fronta programů je v DB (/tazatel 1. 10., 9×)."""
    out = []
    try:
        r = conn.execute("SELECT ts, data FROM diary WHERE event_type='work_artifact' "
                         "ORDER BY ts DESC LIMIT 1").fetchone()
        if r and _popis_dila(r["data"] or ""):
            import datetime as _dtm
            _d = _dtm.datetime.fromtimestamp(r["ts"])
            out.append("moje poslední dílo ze studia: %s (dokončil jsem ho %d. %d.)"
                       % (_popis_dila(r["data"] or ""), _d.day, _d.month))
    except Exception as e:
        _log.debug("self_state trvale (dilo): %s", e)
    try:
        import json as _json
        rows = conn.execute("SELECT title, kind, status, current_index, outline "
                            "FROM writing_project ORDER BY id DESC").fetchall()
        akt = [x for x in rows if x["status"] == "active"]
        hot = [x for x in rows if x["status"] == "completed"]
        if akt:
            n = len(_json.loads(akt[0]["outline"] or "[]"))
            out.append("rozepsané dílo: %s „%s“ (píšu sekci %d z %d)"
                       % (akt[0]["kind"] or "esej", akt[0]["title"],
                          min((akt[0]["current_index"] or 0) + 1, n or 1), n))
        if hot:
            out.append("dokončená psaná díla: %d, poslední %s „%s“"
                       % (len(hot), hot[0]["kind"] or "esej", hot[0]["title"]))
    except Exception as e:
        _log.debug("self_state trvale (psani): %s", e)
    try:
        import json as _json
        akt = conn.execute("SELECT topic, current_index, curriculum FROM study_program "
                           "WHERE status='active' ORDER BY id ASC").fetchall()
        cek = conn.execute("SELECT topic FROM study_program WHERE status='pending' "
                           "ORDER BY id ASC").fetchall()
        if akt:
            n = len(_json.loads(akt[0]["curriculum"] or "[]"))
            # HANS_SELF_STATE_MORE_V1 — i KDY studium začalo (/tazatel 2. 10.: „od 21. srpna“, správně 17. 9.)
            _zac = ""
            try:
                _st = conn.execute("SELECT started_ts FROM study_program WHERE status='active' "
                                   "ORDER BY id ASC LIMIT 1").fetchone()
                if _st and _st[0]:
                    import datetime as _dtz
                    _z = _dtz.datetime.fromtimestamp(_st[0])
                    _zac = ", začal jsem %d. %d." % (_z.day, _z.month)
            except Exception:
                pass
            out.append("teď studuji: „%s“ (podtéma %d z %d%s)"
                       % (akt[0]["topic"], min((akt[0]["current_index"] or 0) + 1, n or 1), n, _zac))
        dalsi = [x["topic"] for x in list(akt[1:]) + list(cek)]
        if dalsi:
            out.append("další studium v pořadí: " + ", ".join("„%s“" % d for d in dalsi[:3]))
    except Exception as e:
        _log.debug("self_state trvale (studium): %s", e)
    # HANS_SELF_STATE_MORE_V1 (2. 10.) — poslední obraz a poslední dočtená kniha.
    # /tazatel 2. 10.: o obraze „Sen“ (1. 10. 23:24, muž na stezce do lesa)
    # tvrdil „14 h 27 min od pátku“ a popsal zříceninu s notami; jako poslední
    # dočtenou knihu jmenoval rozečtený dokument místo dočtené knihy.
    try:
        import datetime as _dto
        r = conn.execute("SELECT ts, title, note FROM diary WHERE event_type='artwork' "
                         "ORDER BY ts DESC LIMIT 1").fetchone()
        if r and r["title"]:
            _d = _dto.datetime.fromtimestamp(r["ts"])
            _co = re.split(r"(?<=[.!?])\s", (r["note"] or "").strip(), 1)[0][:140]
            out.append("můj poslední obraz: „%s“ (namaloval jsem ho %d. %d. v %s)%s"
                       % (r["title"], _d.day, _d.month, _d.strftime("%H:%M"),
                          (" — " + _co) if _co else ""))
    except Exception as e:
        _log.debug("self_state trvale (obraz): %s", e)
    # HANS_SELF_STATE_READING_V1 (4. 10.) — rozečtená kniha: /tazatel B1 Hans
    # tvrdil „čtu Zápisky z deníku Anny Frankové“ (nikdy nečetl), skutečná
    # četba byla jiná a v přehledu chyběla.
    try:
        r = conn.execute("SELECT ts, title FROM diary WHERE event_type='book_read' "
                         "AND ts > ? ORDER BY ts DESC LIMIT 1",
                         (time.time() - 3 * 86400,)).fetchone()
        if r and r["title"]:
            _cte = "teď čtu: %s" % re.sub(r"\s+—\s+kap\.\s*(\d+)$", r" (kapitola \1)",
                                          r["title"].strip())
            # HANS_SELF_STATE_AUTHOR_V1 (6. 10.) — bez autora v přehledu si ho
            # model na otázku „kdo to napsal“ vymyslel (/tazatel: jiné jméno
            # v každém pokusu, 3 ze 4) — a autor v knihovně celou dobu je.
            try:
                _kn = re.sub(r"\s+—\s+kap\..*$", "", r["title"].strip())
                _a = conn.execute("SELECT author, total_chapters FROM hans_library "
                                  "WHERE book_title = ? LIMIT 1", (_kn,)).fetchone()
                if _a and (_a[0] or "").strip():
                    _cte += " — autor: %s" % _a[0].strip()
                    if _a[1]:
                        _cte += ", kniha má %d kapitol" % int(_a[1])
            except Exception as _ae:
                _log.debug("self_state trvale (autor): %s", _ae)
            out.append(_cte)
    except Exception as e:
        _log.debug("self_state trvale (cteni): %s", e)
    try:
        import datetime as _dtk
        r = conn.execute("SELECT ts, title FROM diary WHERE event_type='book_finished' "
                         "ORDER BY ts DESC LIMIT 1").fetchone()
        if r and r["title"]:
            _d = _dtk.datetime.fromtimestamp(r["ts"])
            out.append("poslední dočtená kniha: %s (dočetl jsem ji %d. %d.)"
                       % (re.sub(r"^Do[čc]etl:\s*", "", r["title"]), _d.day, _d.month))
    except Exception as e:
        _log.debug("self_state trvale (kniha): %s", e)
    return out


# HANS_A1_OWN_WORK_IN_PROMPT_V1 (1. 10.) — otázka na VLASTNÍ čin/zážitek
# („proč sis vybral X“, „co vás zaujalo při tvorbě…“), jejíž předmět stojí
# v trvalém přehledu děl a studia (= opora je v PROMPTU, ne v RAG).
_VLASTNI_CIN = re.compile(
    r"\b(?:(?:vybral|zvolil|vytv[aá][rř]el|vytvo[rř]il|tvo[rř]il|napsal|psal|"
    r"namaloval|maloval|slo[zž]il|skl[aá]dal|studoval|[cč]etl)\s*(?:jsi|jste|sis|si)?|"
    r"(?:jsi|jste|sis)\s+(?:si\s+)?(?:vybral|zvolil|vytv[aá][rř]el|vytvo[rř]il|tvo[rř]il|"
    r"napsal|psal|namaloval|maloval|slo[zž]il|skl[aá]dal|studoval|[cč]etl)|"
    r"zaujal[oa]?\s+(?:t[eě]|v[aá]s)|(?:t[eě]|v[aá]s)\s+(?:nejv[ií]c\w*\s+)?zaujal[oa]?|"
    r"tvorb[eěu])\b", re.IGNORECASE)
_DRUHA_OS = re.compile(r"\b(?:jsi|jste|sis|t[eě]|ti|tob[eě]|tv[uůoáé]\w*|v[aá]s|v[aá]m|"
                       r"va[sš]\w*)\b", re.IGNORECASE)
_VLASTNI_STOP = {"proc", "jsi", "jste", "sis", "vybral", "zvolil", "zaujalo",
                 "zaujal", "nejvice", "pravy", "prave", "tvorbe", "tvorbu", "vas",
                 "tebe", "kdyz", "jste", "vytvarel", "stranku", "stranky"}


# obecná slova nic neukotví („obraz“ ~ „s obrázky“ v přehledu → falešná shoda)
_VLASTNI_OBECNE = ("obraz", "podtem", "tvor", "stud", "tema", "dil", "prac",
                   "esej", "knih", "clan", "sekc", "hotov",
                   "kter",   # HANS_SELF_STATE_MORE_V1: „která“ v popisu obrazu ≠ předmět
                   "zprav", "titul", "medi", "odkaz", "sbir", "kazdou", "hodin")  # HANS_SELF_STATE_NEWS_V1


def predmet_vlastniho_dila(text: str, db_path: str) -> str:
    """HANS_A1_OWN_WORK_IN_PROMPT_V1 — vrátí slovo z otázky, které stojí
    v `lasting_facts` („bach“), když se otázka ptá na Hansův vlastní čin.
    '' = ne. Změřeno na 565 přepisech F1 (~50 dní): 15 má 2. osobu + sloveso
    vlastního činu, a jen 2 z nich mají předmět v přehledu děl (Bach na webu,
    tvorba webových stránek) — „co tě zaujalo na Heideggerovi“ nebo „kde jsi
    četl o Gulagu“ zůstávají pod A1 (opora by byla v paměti, ne v promptu)."""
    import unicodedata as _ud
    t = text or ""
    if not (_VLASTNI_CIN.search(t) and _DRUHA_OS.search(t)):
        return ""
    fold = lambda s: "".join(c for c in _ud.normalize("NFD", (s or "").lower())
                             if _ud.category(c) != "Mn")
    lf = fold(" ".join(lasting_facts(db_path)))
    if not lf:
        return ""
    lf_slova = set(re.findall(r"\w{4,}", lf))
    for w in re.findall(r"\w{4,}", fold(t)):
        if w in _VLASTNI_STOP or w.startswith(_VLASTNI_OBECNE):
            continue
        k = w[:4] if len(w) <= 6 else w[:5]
        if any(x.startswith(k) for x in lf_slova):
            return w
    return ""


# HANS_OWN_WORK_DETAIL_V1 (4. 10.) — /tazatel 3. 10.: přehled děl nesl jen NÁZEV
# a stav eseje, a na „jaký je váš přístup?“ model obsah vymyslel (Batman,
# Wonder Woman, Jung — v eseji není ani jedno; je o Starkovi, Quillovi
# a týmovém traumatu). Obraz „Sen“ popsal jako „olejomalbu na plátně
# 60×80 cm“ — obrazy jsou digitální. Když se otázka týká vlastního psaní
# nebo malby, do promptu jde SKUTEČNÁ osnova / popis a technika.
_DOTAZ_PSANI = re.compile(r"\b(?:esej\w*|pov[íi]dk\w*|d[íi]l[oaue]m?\b|p[íi][šs]e[šs]|p[íi][šs]ete|"
                          r"sekc\w*|kapitol\w*|osnov\w*)", re.IGNORECASE)
_DOTAZ_MALBA = re.compile(r"\b(?:obraz\w*|malb\w*|namaloval\w*|maloval\w*|maluj\w*|"
                          r"pl[áa]tn\w*|techni\w*)", re.IGNORECASE)


def detail_vlastniho_dila(text: str, db_path: str) -> list:
    """Řádky navíc do promptu, když se otázka týká Hansova psaní / malby."""
    out = []
    t = text or ""
    psani, malba = bool(_DOTAZ_PSANI.search(t)), bool(_DOTAZ_MALBA.search(t))
    if not (psani or malba):
        return out
    conn = None
    try:
        import json as _json
        conn = _ro(db_path)
        conn.row_factory = sqlite3.Row
        if psani:
            r = conn.execute("SELECT id, title, current_index, outline FROM writing_project "
                             "WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                osn = _json.loads(r["outline"] or "[]")
                hot = int(r["current_index"] or 0)
                body = "; ".join("%d) %s%s" % (i + 1, str(o)[:160],
                                               " [napsáno]" if i < hot else "")
                                 for i, o in enumerate(osn))
                out.append("osnova mé rozepsané eseje „%s“ (o obsahu mluv JEN podle ní, "
                           "jiné příklady ani teorie v ní nejsou): %s" % (r["title"], body))
        if malba:
            r = conn.execute("SELECT title, note FROM diary WHERE event_type='artwork' "
                             "ORDER BY ts DESC LIMIT 1").fetchone()
            if r and r["title"]:
                out.append("můj poslední obraz „%s“ — co na něm je: %s"
                           % (r["title"], re.sub(r"\s+", " ", (r["note"] or "").strip())[:400]))
            # HANS_OWN_WORK_DETAIL_V2 (4. 10.) — i STARŠÍ obrazy: /tazatel zapřel
            # Lendla s Agassim i Göringa, které o pár tahů dřív sám vyjmenoval
            import datetime as _dta
            _vid, _dal = {((r["title"] if r else "") or "").strip().lower()}, []
            for x in conn.execute("SELECT ts, title FROM diary WHERE event_type='artwork' "
                                  "ORDER BY ts DESC LIMIT 20").fetchall()[1:]:
                _t = re.sub(r"\s+", " ", (x["title"] or "").strip())[:90]
                if _t and _t.lower() not in _vid:
                    _vid.add(_t.lower())
                    _d = _dta.datetime.fromtimestamp(x["ts"])
                    _dal.append("„%s“ (%d. %d.)" % (_t, _d.day, _d.month))
                if len(_dal) >= 8:
                    break
            if _dal:
                out.append("mé dřívější obrazy (od nejnovějšího): " + "; ".join(_dal))
            out.append("technika mých obrazů: DIGITÁLNÍ obrazy, které vytvářím na počítači "
                       "generativním modelem — žádné plátno, olej, štětce ani rozměry v centimetrech")
    except Exception as e:
        _log.debug("detail_vlastniho_dila: %s", e)
    finally:
        if conn is not None:
            conn.close()
    return out


def _radek_zprav() -> str:
    """HANS_SELF_STATE_NEWS_V1 (4. 10.) — /tazatel 4. 10.: „nemám přístup ke
    sledování aktuálních zpráv“ (2×), protože výčet schopností se k otázce
    o sobě nevkládá. Řádek ze SKUTEČNÉHO stavu sběru (poslední běh)."""
    try:
        import os as _os
        import datetime as _dtz
        p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "data", "hans_zpravy.db")
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=3)
        try:
            ts = c.execute("SELECT MAX(ts) FROM sbery").fetchone()[0]
        finally:
            c.close()
        if not ts:
            return ""
        d = _dtz.datetime.fromtimestamp(ts)
        return ("zprávy: každou hodinu sbírám titulky z českých i zahraničních médií "
                "(naposledy %d. %d. v %s); CO v nich je, vím jen z výpisu zpráv — titulky "
                "ani odkazy si nevymýšlej, nabídni, že zprávy vypíšeš (i s odkazy)"
                % (d.day, d.month, d.strftime("%H:%M")))
    except Exception:
        return ""


def je_dotaz_na_vlastni_dilo(text: str, db_path: str) -> bool:
    """HANS_OWN_WORK_A1_SKIP_V1 (4. 10.) — ptá se věta na Hansův VLASTNÍ obraz
    nebo psaní? (pak opora je v promptu z `detail_vlastniho_dila` a brzda A1
    ani dohledání na Wikipedii nemají běžet). /tazatel 4. 10.: „jaké postavy
    v eseji rozebíráte?“ → „nemám spolehlivý záznam“, „jakou technikou jsi to
    dělal?“ → Wikipedie „Sen“ (stav ve spánku), Lendl s Agassim zapřen.
    Podmínka: slovo o psaní/malbě A ZÁROVEŇ 2. osoba nebo slovo z názvu
    vlastního obrazu/eseje (jinak „co víš o obrazu Mona Lisa“ zůstává pod A1)."""
    import unicodedata as _ud
    t = text or ""
    if not (_DOTAZ_PSANI.search(t) or _DOTAZ_MALBA.search(t)):
        return False
    if _DRUHA_OS.search(t) or re.search(r"\bsv(?:[ůu]j|[ée]|ou|[ée]m|[ée]ho|ými?)\b", t, re.I):
        return True
    fold = lambda s: "".join(c for c in _ud.normalize("NFD", (s or "").lower())
                             if _ud.category(c) != "Mn")
    conn = None
    try:
        conn = _ro(db_path)
        nazvy = [r[0] for r in conn.execute(
            "SELECT title FROM diary WHERE event_type='artwork' ORDER BY ts DESC LIMIT 20")]
        nazvy += [r[0] for r in conn.execute(
            "SELECT title FROM writing_project ORDER BY id DESC LIMIT 3")]
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()
    slova = {w[:5] for n in nazvy for w in re.findall(r"\w{5,}", fold(n))}
    return any(w[:5] in slova for w in re.findall(r"\w{5,}", fold(t))
               if not w.startswith(_VLASTNI_OBECNE))


def lasting_facts(db_path: str) -> list:
    """HANS_SELF_STATE_LASTING_V1 — trvalé řádky pro chatový prompt (díla, studium)."""
    conn = None
    try:
        conn = _ro(db_path)
        conn.row_factory = sqlite3.Row
        return _self_state_trvale(conn) + [x for x in (_radek_zprav(),) if x]
    except Exception as e:
        _log.debug("lasting_facts: %s", e)
        return []
    finally:
        if conn is not None:
            conn.close()


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
            ("vytvořil jsem dílo", "work_artifact"),   # HANS_SELF_STATE_WORKS_V1
            ("napsal jsem esej", "work_created"),
            ("namaloval jsem", "artwork"),
            # HANS_SELF_STATE_ARTICLE_LABEL_V1 (1. 10.) — web_read jsou ČLÁNKY
            # (Wikipedie); z „četl jsem: Falešná kočička (film, 1926)“ model 3×
            # udělal přečtenou KNIHU („dočetl jsem Falešnou kočičku“). Knihy
            # mají vlastní řádek „zapsal jsem si ke knize“.
            ("četl jsem článek o", "web_read"),
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
                if etype == "work_artifact":
                    t = _popis_dila(r["data"]) or t
                # HANS_SELF_STATE_IDEA_TEXT_V1 (1. 10.) — titulek nápadu jsou
                # semínka „A × B × C“; z „Hrabě Monte Christo (film) × …“ model
                # cizímu řekl „momentálně přehrávám Hraběte Monte Christo“.
                # Titulek sebekritiky je jen jméno persony („uvědomil jsem si
                # o sobě: Hans“). Obojí → první věta obsahu.
                elif etype == "synthesis_idea" and (r["data"] or "").strip():
                    t = "„%s“" % _prvni_veta(r["data"])
                elif etype == "self_critique" and (r["note"] or "").strip():
                    t = _prvni_veta(r["note"])
                if not t:
                    t = ((r["note"] or r["data"] or "").strip().split("\n")[0])[:60]
                if t:
                    det.append(t[:200] if etype == "work_artifact" else
                               t[:120] if etype in ("synthesis_idea", "self_critique")
                               else t[:70])
            if det:
                out.append("%s: %s" % (label, "; ".join(det)))
            if len(out) >= max_items:
                break
        trvale = _self_state_trvale(conn)            # HANS_SELF_STATE_LASTING_V1
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
        # HANS_SELF_STATE_ASKER_VISIBLE_V1 (15. 9.) — plni handler JEN u otazky
        # na videni; stala zminka by byla semenko (HANS_SELF_STATE_NO_OFF_MODES_V1).
        if runtime.get("asker_visible") is True:
            _st.append("toho, kdo se m\u011b te\u010f pt\u00e1, vid\u00edm p\u0159ed kamerou")
        elif runtime.get("asker_visible") is False:
            _st.append("toho, kdo se m\u011b te\u010f pt\u00e1, p\u0159ed kamerou nevid\u00edm "
                       "\u2014 mluv\u00edme spolu jen p\u0159es zpr\u00e1vy")
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
    if not out and not head and not trvale:
        return ""
    # Instrukce s TVAREM odpovědi: samotná fakta nestačila — persona je jen
    # olízla a vrátila vatu („Službu plním, a to je pro mne dostatečné").
    # Model potřebuje říct, KOLIK a CO má z bloku použít.
    return ("FAKTA O MĚ A O MÉM DNEŠKU — čerpej z NICH, nic si nepřidávej "
            "(co tu není, dnes nebylo):\n"
            + ("- " + "\n- ".join(head + out) if (head or out) else "")
            + (("\nCO JSEM VYTVOŘIL A CO STUDUJI (platí trvale, ne jen dnes — o stavu "
                "svých děl a studia mluv JEN podle tohohle):\n- " + "\n- ".join(trvale))
               if trvale else "")
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


def day_fact_lines(f: dict, config: dict = None,
                   asker: str = None) -> list:
    """`day_facts` → české věty pro LLM grounding i pro deterministický výpis.

    Jména se zobrazují přes `cz_names.display_name` — jinak by v textu byla
    tak, jak jsou klíče v konfiguraci (malá písmena, bez diakritiky).

    HANS_DAY_FACTS_PRIVACY_V1 (9. 9.) — `asker` je NEPOVINNÝ a bez něj se
    chování NEMĚNÍ. Je to schválně: druhý volající je noční souhrn
    (`hans_routine._write_night_summary`), který jména dostávat MUSÍ.
    Gate se tedy zapíná jen tam, kde odpověď čte člověk — dnes `/dnes`.
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
    # ── HANS_DAY_FACTS_PRIVACY_V1 (9. 9.) — ČTVRTÝ zdroj úniku domácnosti ──
    # 8. 9. se únik zavíral na TŘECH místech (prompt, surroundings, who_home)
    # a tohle čtvrté se minulo, protože `day_facts`/`day_fact_lines` tazatele
    # VŮBEC NEDOSTALY — `_cmd_dnes(handler, name, args)` ho přitom v parametru
    # má. Doloženo naživo 9. 9.: cizí „co se dnes delo doma?" →
    # „Dnes ráno, od 09:29 do 10:31, navštívila nás paní <jméno>." (i s časy).
    # Predikát je SDÍLENÝ (`cz_names.is_known_person`), ne nový.
    _cizi = False
    if asker:
        try:
            from scripts import cz_names as _czn
            _cizi = not _czn.is_known_person(asker, config)
        except Exception:
            _cizi = False          # fail-open: chyba predikátu nesmí umlčet dům
    if _cizi:
        # ⚠️ Odmítá se OBOJE — výčet i „nikoho jsem neviděl". Ta druhá věta
        # totiž cizímu prozradí, že je dům PRÁZDNÝ, což je při use-case
        # `/hlidej` (dovolená, prázdný dům) horší než jmenný výčet.
        # A říká se to VÝSLOVNĚ, nemlčí se: HANS_VISION_NOT_DENIED_V1 doložil,
        # že z vynechané věty si model domyslí popření vlastního zraku.
        if f.get("people") or f.get("n_events"):
            lines.append(_PRIVACY_REFUSAL)
    elif f.get("people"):
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

# HANS_CAMERA_STRANGER_V1 (23. 9., pokyn uzivatele) — co Hans vidi kamerou,
# nerika NEZNAMEMU. Priznava zrak (HANS_VISION_NOT_DENIED_V1 plati dal),
# jen obsah nesdeli. Sdileno agentem i handlerem, at odmitaji stejne.
_CAMERA_REFUSAL = ("Kamerou vidím, ale co v ní je, sděluji jen lidem, "
                   "které znám. Snad mi to prominete.")


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
                # HANS_PRAVA_V1 (27. 9.) — Hansovy postřehy o JINÉ osobě jen
                # s oprávněním; jinak zůstane jméno, role a rodina.
                try:
                    from scripts.hans_prava import muze as _pm
                    if ch and not _pm(config, asker or "", "karta_osoby", o_kom=pid):
                        ch = ""
                except Exception:
                    pass
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
        # HANS_CAMERA_STRANGER_ASKER_V1 (27. 9.) — HANS_CAMERA_STRANGER_V1 platil
        # jen v agentovi; tahle deterministická cesta ho neznala. Doloženo testem
        # nováčka: cizí „vidíš mě přes kameru?“ → „kamera je prázdná“, a kdyby
        # v místnosti někdo byl, dostal by „v místnosti vidím <jména domácnosti>“.
        if not known:
            return _CAMERA_REFUSAL
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
