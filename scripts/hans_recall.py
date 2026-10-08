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

from scripts.hans_recall_cetba import first_memory_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_cetba import _extract_topic   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import _vytcene_tema   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import _topic_stems   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import tema_entita   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import jmeno_entity   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import osoba_sedi   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import _vsechna_slova_sedi   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_READING_CHAPTERS_V1 — sufix „— kap. 41" na konci názvu knihy.
# Kotví se na KONEC, ať to nesebere číslo z názvu samotného díla.
_KAP_PAT = re.compile(r"\s*[—–-]?\s*kap\.?\s*(\d+)\s*$", re.I)


from scripts.hans_recall_cetba import _dedup_cteni   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_cetba import _je_z_posledni_noci   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import _pta_se_na_dnesek   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import dream_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import _zkrat_sen   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_cetba import reading_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── kdy jsi mě / X viděl ─────────────────────────────────────────────────────

from scripts.hans_recall_filmy import _resolve_person   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import films_watched_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import artwork_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import last_seen_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_filmy import _looks_like_film_query   # ROZDELENI_PRIKAZU_V1 — přesunuto


_NAZOR_NEZNA = ("v\u00edm jen velmi m\u00e1lo", "v\u00edm jen m\u00e1lo", "nezn\u00e1m",
                "ne\u010detl jsem", "nevid\u011bl jsem", "nem\u00e1m z\u00e1znam")


from scripts.hans_recall_filmy import _nazor_prvni_veta   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_FILM_OPINION_ANAFORA_V1 — pozná VÝPIS filmů od `films_watched_answer`
# (všechny tři tvary: dnes, časové okno, „Naposledy jsem sledoval“).
_VYPIS_FILMU_PAT = re.compile(
    r"^(Naposledy jsem sledoval|Dnes jsem u obrazovky zaznamenal"
    r"|.{0,40}? jsem u obrazovky zaznamenal)")


from scripts.hans_recall_filmy import film_list_titles   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import nazor_k_filmu   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import films_liked_among   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import book_from_thread   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import book_origin_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


_PRUBEH_RAMEC = {
    "poprve", "dlouho", "ctes", "ctete", "znovu", "kapitole", "kapitola",
    "kapitolu", "kapitol", "ktere", "kterou", "kolikate", "kolikatou", "nekdy",
    "driv", "drive", "predtim", "jsi", "jste", "knihu", "kniha", "knize",
    "tuhle", "tahle", "cetl", "cetla", "opakovane", "prave", "porad", "jeste",
    "vlastne", "vubec", "tedy", "takze", "jakou", "jake", "jaky", "rikal",
    "rikate", "rikas", "myslim", "prosim", "hansi"}


from scripts.hans_recall_filmy import _PRUBEH_OBSAHOVA_SLOVA   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import _kniha_z_otazky   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import book_progress_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import films_liked_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import _jmenuje_domacnost   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_filmy import film_knowledge_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_znalost import is_recent_activity_query   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import _okno_aktivity   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import recent_activity_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_znalost import _reorder_object_first   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_KNOWLEDGE_NOT_RELATIVE_V1 (6. 10.) — „znáte“ ve VZTAŽNÉ větě není dotaz
# na znalost. /tazatel: „…když mluvíte s někým, koho znáte delší dobu
# z domácnosti?“ → téma „delší dobu z domácnosti“ → „nemám záznamy o … stačí
# říct ‚nastuduj delší dobu z domácnosti‘“. Na 1 920 větách 3 ze 46 shod,
# všechny tři s nesmyslným tématem („dlouho“, „z domácnosti“).
_KC_VZTAZNA = re.compile(
    r"\b(?:koho|kter(?:[ée]ho|ou|[ée]|[ýy]|[ýy]m|[ýy]ch|[ée]mu)|jeho[zž]|jen[zž])"
    r"\s+(?:\w+\s+){0,2}$", re.IGNORECASE)


from scripts.hans_recall_znalost import _kc_match   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import is_knowledge_check_query   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import _extract_knowledge_topic   # ROZDELENI_PRIKAZU_V1 — přesunuto


# kvalifikátory, které v „co víš o jazyku X / o filmu X" nesou téma až za sebou
_TOPIC_QUALIFIERS = {
    "jazyku", "jazyce", "jazyk", "tematu", "tématu", "téma", "tema",
    "filmu", "film", "knize", "kniha", "knihy", "meste", "městě", "město",
    "projektu", "projekt", "autorovi", "autor", "pojmu", "pojem", "slovu",
    "slovo", "clanku", "článku", "clanek", "článek", "strance", "stránce",
    "stranka", "stránka", "webu", "web",
}


from scripts.hans_recall_znalost import _topic_core_prefixes   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import _topic_in_memory   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import reading_recall_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import knowledge_check_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import _hlavni_slovo_v_pameti   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_znalost import knowledge_check_bypass   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import _popis_dila   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import _prvni_veta   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import _self_state_trvale   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osebe import predmet_vlastniho_dila   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osebe import detail_vlastniho_dila   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import _radek_zprav   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import je_dotaz_na_vlastni_dilo   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import lasting_facts   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import self_state_facts   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import day_facts   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osebe import day_fact_lines   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── HANS_PERSON_CARD_V1 (18.8.) — „kdo je X?“ deterministicky ────────────────
# C4 (7.8.): fakt o dceři LEŽÍ v `relationships` (role, rodina, charakterizace
# 379 znaků), ale dotaz šel rovnou do RAG. Ten nic nenašel, a o tom, jestli Hans
# odpoví nebo abstinuje, pak rozhodoval self-consistency práh — TÁŽ otázka tedy
# jednou vrátila odpověď a podruhé „nemám spolehlivý záznam“.
# Pořadí je proto: domácnost → encyklopedie → nic. Bez LLM; hlas se přidá až
# nad výsledkem ([[prompt-debt-tool-calling]]: STAV → PAMĚŤ → HLAS).


from scripts.hans_recall_osoby import _family_sentence   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osoby import person_card   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osoby import person_card_voiced   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osoby import household_card   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osoby import household_card_voiced   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osoby import asks_about_person   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osoby import datetime_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osoby import asks_book_recommendation   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.hans_recall_osoby import book_recommendation   # ROZDELENI_PRIKAZU_V1 — přesunuto


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


from scripts.hans_recall_osoby import asker_state_answer   # ROZDELENI_PRIKAZU_V1 — přesunuto
