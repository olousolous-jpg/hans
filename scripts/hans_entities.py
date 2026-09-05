"""HANS_ENTITY_STORE_C1_V1 — strukturovaný store entit + disambiguace.

Anti-konfabulace, vůdčí princip [[anticonfabulation-guiding-principle]] bod C1:
typovaný záznam entit, o kterých Hans REÁLNĚ četl (osoba/místo/dílo/pojem se
zdrojem a definiční větou). Faktický dotaz na entitu se resolvuje DETERMINISTICKY
proti store, ne generací → zabíjí:
  • kolizi jmen (Erich Sorge = skladatel z Hansova čtení, NE špión Richard Sorge
    z parametrické paměti = slepé místo A1),
  • fantomy (AJ II bez záznamu → resolve None → abstinence přes A1/#2).

Glos = PRVNÍ DEFINIČNÍ VĚTA zdrojového článku (verbatim, žádný LLM → nic se
nevymyslí). Doplňuje RAG (G3B) a A1 (self-consistency): entity store = tvrdá
priorita u ZNÁMÝCH entit; A1/#2 = síť u neznámých/nestabilních.

Store se plní při ČTENÍ (forward, `capture_from_reading`), dotazuje při chatu
(`resolve`). Deferral-nezávislý (čistě SQLite, žádná síť/LLM).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import unicodedata
from typing import List, Optional

_log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    name_norm      TEXT NOT NULL UNIQUE,
    etype          TEXT DEFAULT 'pojem',
    gloss          TEXT,
    source         TEXT,
    source_title   TEXT,
    aliases        TEXT DEFAULT '[]',
    disambig       TEXT DEFAULT '',
    lang           TEXT DEFAULT 'cs',
    first_ts       REAL,
    last_ts        REAL,
    evidence_count INTEGER DEFAULT 1
);
"""


def _norm(s: str) -> str:
    """Lowercase, bez diakritiky, sjednocené mezery — klíč pro matching."""
    s = (s or "").strip().lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _tokens(s: str) -> List[str]:
    """Slova z normalizovaného textu (bez diakritiky, lowercase)."""
    return re.findall(r"[a-z0-9]+", _norm(s))


def _prefix_len(a: str, b: str) -> int:
    """Delka spolecneho prefixu dvou tokenu."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


# ── HANS_ENTITY_PROPER_NOUN_PREFIX_V1 (5.9.) ────────────────────────────────
# ROZHODCIM NENI DELKA, ALE PRAVOPIS.
#
# Doloheny pripad (zkouska Kolacem #30, 5.9. 00:27): dotaz o Bohuslavu
# MARTINU resolvoval entitu MARTAN ("hypoteticky obyvatel planety Mars") ->
# C1 ji vydala jako autoritativni fakt -> 'grounded' -> abstinencni brzda A1
# se NESPUSTILA -> 15 vet bez opory. [[partial-grounding-disables-abstention]]
#
# ⛔ TRI CESTY ZMERENY, DVE ZAMITNUTY — nezkouset znovu (detail v BACKLOG.md):
# (a) Pomerove pravidlo na DELKU prefixu je slepa ulice, protoze
#         gotika ~ goticke   n=4 min=6 max=7   LEGITIMNI
#         martan ~ martinu   n=4 min=6 max=7   VADNA
#     maji TOTOZNY TVAR. Archiv u HANS_ENTITY_PREFIX_MAXLEN_V1 mel pravdu.
# (b) Shoda entity s `kotva_tematu`: na 1 359 vetach 8 vyher a 8 ztrat
#     (kotva bere prvni velke pismeno, casto vedlejsi slovo: „Kde", „Osmi").
# (c) Oznacit `entita_c1` za tenkou cestu pro grounding_guard NESTACI —
#     zmereno na odpovedi #30: guard zahodi 1 vetu z 15 (prah 2), protoze
#     `overlap == 0.0` znamena „neni o tematu, neni co overovat".
#
# ✅ CO FUNGUJE: ceske VLASTNI JMENO se sklonuje jen v koncovce, takze spravna
# shoda drzi dlouhy prefix; nahodna shoda dvou RUZNYCH jmen se rozejde brzy.
# Obecna jmena se odvozovanim meni vic (gotika->goticke) a spatne obecne jmeno
# je min nebezpecne. Proto: token psany ve VETE velkym pismenem musi prefixem
# pokryt >= `proper_noun_prefix_ratio` delsiho tokenu; token psany malym si
# drzi dnesni volnejsi pravidlo. Presna shoda projde vzdy.
#
# ZMERENO na 1 359 realnych vetach z `human_chat`: resolvuje se 188 -> 187,
# tj. JEDINY rozdil (`Cesko` u dotazu na Cesky raj — tam byla stejne spatne).
# Zavre pritom tri DOLOZENE vady: martan~Martinu (#30), svatba~Svatyne (21.8.)
# a spencer~Spenat (4.8., „Bud Spencer" -> obraz o spenatu).
# ⚠️ Prahy 0,65 (ztrati Daliho na preklep „Salvator") a 0,68 (ztrati
#    Radeckeho) jsou HORSI — neposouvat nahoru.
#
# 🟡 ZNAMA MEZ: rozdil gotika~goticke × martan~martinu je JEN ve velkem
# pismenu. Kdyz uzivatel pise bez velkych pismen („co vis o martinu"), je
# pravidlo NECINNE a plati dnesni chovani. Je to tedy FAIL-OPEN: umi jen ubrat
# falesne shody u velkych pismen, nikdy nepridat novou.
#
# ⚠️ ZAMERNE SE NEMENI `_tok_match` — pouziva ho 5 dalsich modulu
# (film_director_check, kodi_client, hans_recall, hans_lessons, hans_study)
# a ty se opiraji o jeho dokumentovane chovani. Pritvrzeni sedi JEN v
# `resolve()`, tedy tam, kde bylo zmereno. [[action-description-is-router-change]]
_PROPER_RATIO_DEFAULT = 0.62


def _upper_folded(text: str) -> set:
    """Slozene tvary tech slov vety, ktera jsou psana VELKYM pismenem.

    Deli se stejnym vzorem jako `_tokens`, jen se pocita pres SYROVY text,
    aby se zachovala velikost pismen. Slova, ktera se po normalizaci
    nerozpadnou na `[a-z0-9]+`, se ignoruji.
    """
    out = set()
    for raw in re.findall(r"\w+", text or "", re.UNICODE):
        if not raw[:1].isupper():
            continue
        for f in re.findall(r"[a-z0-9]+", _norm(raw)):
            out.add(f)
    return out


def _tok_match(a: str, b: str) -> bool:
    """Dva tokeny odpovídají téže bázi navzdory českému skloňování:
    sdílený prefix ≥4 znaky A ≥ (kratší délka − 3). „cardiffsky"↔„cardiffskem",
    „hrad"↔„hradu", „design"↔„design"."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    # HANS_ENTITY_PREFIX_MAXLEN_V1 (21.8.) — prefix musi pokryt i DELSI
    # token, ne jen kratsi. Puvodni pravidlo (min-3) povolilo shodu, kde
    # se delsi token za prefixem jeste dlouho lisi: objektiv~objevil,
    # trenbolon~trenuje, stromboli~stromy, zlatovlaska~zlate. Zmereno na
    # 1138 realnych vetach: 161 -> 149 shod, 12 falesnych trid pryc,
    # jedina ztrata je startrek ~ Star Trek (film).
    # ⚠️ Nerozlisi svatba~svatyne (prefix 4, delky 6/7) — ma PRESNE tvar
    # legitimni dvojice gotika~gotice, takze retezcove pravidlo by zabilo
    # obe. Tu tridu resi az HANS_NOTES_BEFORE_ENTITY_V1 (zapisky napred).
    return (n >= 4 and n >= min(len(a), len(b)) - 3
            and n >= max(len(a), len(b)) - 3)


# Odseknout závorkové upřesnění z titulu Wikipedie: „Aj (faraon)" → base „aj",
# ale ponecháme i plný tvar jako alias (disambiguační stopa).
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")


def _first_sentence(text: str, max_chars: int = 320) -> str:
    """První definiční věta z úvodu článku (verbatim). Ošetří běžné zkratky
    (č., tzv., mj., např., n. l., stol., akademické tituly), aby se věta
    neuťala předčasně.

    HANS_FIRST_SENT_TITLES_V1 (20.7.): akademické/profesní tituly „BDP.",
    „Ph.D.", „M.D.", „BSc.", „Ing." atd. dřív tříštily větu → Rimmerova
    definiční věta („Arnold Jidáš Rimmer, BDP. SDP. … je fiktivní postava…")
    se ořízla na „Arnold Jidáš Rimmer, BDP." bez slovesa → guard v
    capture_from_reading odmítl (chybí „je/byl") → prázdná gloss v entity
    store → paint_subject nevěděl že jde o postavu. Generický pattern:
    2-5 VELKÝCH PÍSMEN s tečkou (BDP, SDP, MSc, PhDr, Bc), případně řetězec
    přerušený tečkami (Ph.D., M.Sc., n. l.)."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return ""
    # ochrana zkratek — dočasně nahradíme tečku
    # HANS_FIRST_SENT_TITLES_V1 (20.7.): přidány akademické/profesní tituly
    # (Ph.D., MUDr., BSc., BDP.) — bez toho se věta „Rimmer, BDP. SDP. … je
    # fiktivní postava" ořezala na „Rimmer, BDP." (bez slovesa) → guard v
    # capture_from_reading odmítl → prázdná gloss → paint neuměl.
    _ABBR = ("č.", "tzv.", "mj.", "např.", "tj.", "n. l.", "př. n. l.",
             "st.", "stol.", "roz.", "cca.", "resp.", "atd.", "apod.",
             "nar.", "zem.", "vl. jm.", "vl.",  # biografická data
             # akademické/profesní tituly (CS + EN — Wiki cituje obojí)
             "Ph.D.", "M.D.", "M.Sc.", "M.A.", "Sc.D.", "B.Sc.", "B.A.",
             "MUDr.", "PhDr.", "RNDr.", "JUDr.", "PaedDr.", "MVDr.",
             "Ing.", "Mgr.", "Bc.", "MBA.", "DiS.", "BSc.", "MSc.",
             # kněžské/šlechtické (Wiki hojně)
             "sv.", "sv ", "prof.", "doc.", "gen.", "kpt.", "npor.")
    for a in _ABBR:
        t = t.replace(a, a.replace(".", "\x00"))
    # Fiktivní / méně obvyklé zkratky velkými písmeny (BDP., SDP., BSc., PhD.,
    # atd. — cokoli 2-4 kapitálek + tečka). Chráníme VŽDY: Wiki definiční věty
    # obvykle končí malým slovem („detektiv.", „postava.") ne kapitálkovou
    # zkratkou → riziko splísknutí legitimního konce věty minimální.
    t = re.sub(r"\b([A-Z]{2,4})\.", lambda m: m.group(1) + "\x00", t)
    # ochrana pořadových čísel/letopočtů „1.", „18.", „2016." → nesplítat
    t = re.sub(r"(\d)\.", lambda m: m.group(1) + "\x00", t)
    m = re.search(r"[.!?](?:\s|$)", t)
    sent = t[: m.start() + 1] if m else t
    sent = sent.replace("\x00", ".")
    return sent[:max_chars].strip()


# Heuristická klasifikace typu z definiční věty (české vzory „X je/byl …").
#
# HANS_ENTITY_CLASSIFY_V3 (23.8.) — proč se filmy, písně a události vedly jako
# `osoba` (změřeno: 60 ze 104 „osob" stálo JEN na tomhle):
#  (a) starý `_PERSON` měl jako první alternativu HOLÉ `\bbyl[aiy]?\b` — bez
#      jakéhokoli podstatného jména člověka. „Adidas Yeezy **byla** módní
#      spolupráce" → osoba. A ptal se PRVNÍ, takže přebil i větu, která svůj
#      druh říká výslovně („Rotherham Town FC **byl** fotbalový **klub**").
#  (b) vzory braly kmen bez konce slova, takže je chytalo i přídavné jméno
#      („**hra**nice" → dílo, „Dvůr **Král**ové" → osoba).
#
# Čtyři pravidla, na kterých teď stojí (každé vzniklo z konkrétního nálezu):
#  1. Rozhoduje DEFINIČNÍ věta a v ní PRVNÍ spona. Další věty mluví o výrobci
#     nebo autorech („Návštěvníci … Výrobcem byla ČST" → organizace).
#  2. Druhové jméno se hledá v okně ZA sponou (45 znaků; u lidí 80, protože
#     výčet profesí bývá dlouhý: „je český divadelní, filmový … herec").
#  3. Když jich padne víc, vyhrává BLIŽŠÍ ke sponě — to je podmět věty
#     („je sci-fi **film**, který natočil **režisér** X" → dílo).
#  4. Osobní jméno musí být v 1. pádě (`\b` bez pádové koncovky): „je komedie
#     **režiséra** Jana Hřebejka" je 2. pád a mluví o DÍLE, ne o člověku.
# Žánrová přídavná jména („filmová bondovka") jsou až záloha, protože
# „**filmový** režisér" je člověk. Holé „byl" zůstalo poslední před `pojem`.
_COP = re.compile(r"\b(je|jsou|byl|byla|byli|byly|bylo)\b", re.IGNORECASE)
_WIN = 45           # okno za sponou pro druhové jméno
_WIN_PERSON = 80    # u lidí delší (výčet profesí)
_CASE = r"(?:a|u|e|ě|em|y|ů|ům|ech|ách|ami|ou|í|i|o)?"
_SENT_BREAK = re.compile(r"\.\s+(?=[A-ZÁ-Ž])")


def _n(nouns: str) -> "re.Pattern":
    """Podstatné jméno v 1. pádě: pádová koncovka ano, přípona ne
    („film|filmu" ✓, „filmový" ✗)."""
    return re.compile(r"\b(?:" + nouns + r")" + _CASE + r"\b", re.IGNORECASE)


# HANS_ENTITY_POSTAVA_V1 (20.7.) — fiktivní / literární / filmová postava.
# Kritérium pro dopad: paint_subject volá img2img z Wiki portrétu (viz gate
# etype in ('osoba','postava')).
_POSTAVA = re.compile(
    r"\b(fiktivní\s+postav|literární\s+postav|filmov[áa]\s+postav|"
    r"seri[áa]lov[áa]\s+postav|animovan[áa]\s+postav|hlavní\s+postav|"
    r"vedlejší\s+postav|pohádkov[áa]\s+postav|kreslen[áa]\s+postav|"
    r"komiksov[áa]\s+postav)", re.IGNORECASE)
# Obecné „postava/hrdina" jen když věta zároveň mluví o fikci — jinak by byl
# „Hrdina Sovětského svazu" (Richard Sorge) fiktivní postavou.
_POSTAVA_GEN = re.compile(r"\b(postav[ay]|hrdin[aoy])\b", re.IGNORECASE)
_FIKCE = re.compile(r"\b(fiktivn|literárn|román|povídk|komiks|seriál|film|"
                    r"pohádk|animovan|spisovatel|kreslíř|večerníč)", re.IGNORECASE)
# Ženské tvary se vypisují zvlášť — končí na -a/-ka, kmen mužského tvaru
# s `\b` by je nechytil.
_PERSON = re.compile(
    r"\b(?:"
    r"spisovatel|spisovatelka|skladatel|skladatelka|malíř|malířka|politik|"
    r"politička|král|královna|císař|císařovna|faraon|herec|herečka|"
    r"vědec|vědkyně|fyzik|filozof|filosof|hudebník|zpěvák|zpěvačka|generál|"
    r"vojevůdce|panovník|panovnice|režisér|režisérka|architekt|architektka|"
    r"básník|básnířka|autor|autorka|matematik|objevitel|vynálezce|"
    r"lyžař|lyžařka|sportovec|sportovkyně|fotbalista|fotbalistka|hokejista|"
    r"atlet|atletka|jezdec|náčelník|kníže|vévoda|vévodkyně|"
    r"biolog|chemik|lékař|lékařka|historik|historička|novinář|novinářka|"
    r"podnikatel|voják|důstojník|cestovatel|misionář|kněz|papež|šlechtic|"
    r"rytíř|zločinec|filantrop|teolog|astronom|geolog|sochař|fotograf|"
    r"redaktor|redaktorka|scenárista|scenáristka|dramaturg|dramaturgyně|"
    r"moderátor|moderátorka|producent|producentka|překladatel|překladatelka|"
    r"kreslíř|kreslířka|ilustrátor|ilustrátorka|rozvědčík|špion|konstruktér|"
    r"dabér|dabérka|kameraman|zpravodaj|reportér|učitel|učitelka|profesor"
    r")\b", re.IGNORECASE)
_PLACE = _n(r"měst|hrad|zámek|hora|řek|jezer|stát|obec|vesnic|ostrov|pohoří|"
            r"kraj|region|čtvrť|náměstí|budov|katedrál|stavb|pyramid|pevnost|"
            r"tvrz|klášter|chrám|most|amfiteátr|ulic|přítok|park|"
            # HANS_ENTITY_CLASSIFY_V4 (26.8.) — doloženo backfillem faktů:
            r"osad|samot|lokalit|nalezišt")
_WORK = _n(r"film|kniha|knih|román|romanet|oper|skladb|album|píseň|písn|obraz|"
           r"báseň|básn|hra|seriál|dílo|díl|hymn|časopis|komedie|komiks|"
           r"muzikál|symfoni|povídk|sbírk|pohádk|epos|dobrodružství|pořad|"
           r"epizod|bondovk|"
           # HANS_ENTITY_CLASSIFY_V4 — „byl třídílný CYKLUS" a „je spaghetti
           # WESTERN" spadaly na osobu, protože predikátové jméno chybělo.
           r"cykl|western|dokument|thriller|sitcom|muzik[áa]l")
_ORG = _n(r"organizace|společnost|firm|klub|stran|spolek|instituce|univerzit|"
          r"škol|tým|kapel|nakladatelství|sdružení|stanic")
_EVENT = _n(r"válk|bitv|turnaj|revoluce|povstání|událost|mistrovství|festival|"
            r"olympiád|tažení|soutěž|operace")
# Technické objekty: mají „byla" a žádné druhové jméno z výčtů výš, takže by
# spadly do poslední záchrany a staly se „osobou" (doloženo: družice FalconSAT-2,
# sonda Luna 13). Vrací `pojem` — nic lepšího pro ně dnes není.
_THING = _n(r"družic|sond|satelit|raket|letoun|kluzák|vozidl|stroj|přístroj|"
            r"počítač|program|protokol|norm|jednotk|prvek|slitin|materiál|"
            r"spoluprác|značk|projekt|název|označení|technologi|metod|"
            # HANS_ENTITY_CLASSIFY_V4 (26.8.) — predikátová jména, která
            # backfill faktů odhalil: bez nich spadly na „osobu" LOĎ, obec
            # i PRŮMYSLOVÁ REVOLUCE.
            r"loď|lodi|lodě|plavidl|změn|směs|tradic|způsob|soustav|systém|"
            r"říš|impéri|zvyk|obdob")
# Žánrová přídavná jména — slabší signál (viz „filmový režisér"), proto záloha.
_ADJ_WORK = re.compile(r"\b(filmov|seri[áa]lov|komiksov|animovan|televizní|"
                       r"hudební|divadelní|operní)", re.IGNORECASE)
# Poslední záchrana: holé „byl/byla". Dokud stálo první, spolklo film, klub,
# město i módní spolupráci.
# ⚠️ HANS_ENTITY_CLASSIFY_V4 (26.8.) — hledalo se v CELÉ VĚTĚ, takže stačilo
# „byla" z VEDLEJŠÍ věty: „Konobrže **je** zaniklá osada, která **byla**
# součástí obce" → osoba. Test se proto dělá na HLAVNÍ sponě, ne hledáním.
# HANS_ENTITY_NO_INDEX_PAGES_V1 — názvy stránek, které NEJSOU entity.
_INDEX_PAGE = re.compile(
    r"^\s*(seznam|list of|kategorie|category|portál|portal|wikiprojekt)\b"
    r"|\((rozcestník|rozcestnik|disambiguation)\)\s*$", re.IGNORECASE)

_PERSON_WEAK = re.compile(r"\bbyl[aiy]?\b", re.IGNORECASE)


def _classify(gloss: str) -> str:
    s = _first_sentence(gloss or "")
    if _POSTAVA.search(s) or (_POSTAVA_GEN.search(s) and _FIKCE.search(s)):
        return "postava"
    m = _COP.search(s)
    # Okno nesmí přetéct do DALŠÍ věty. `_first_sentence` nedělí za letopočtem
    # („…z roku 2015. Natočil jej režisér Tarantino" → film by byl osoba),
    # protože chrání pořadová čísla; tady se dělí před VELKÝM písmenem, aby
    # „je 19. film" zůstalo celé.
    def _cut(x: str) -> str:
        return _SENT_BREAK.split(x, 1)[0]

    win = _cut(s[m.end(): m.end() + _WIN]) if m else ""
    win_person = _cut(s[m.end(): m.end() + _WIN_PERSON]) if m else ""
    cands = []
    for pat, typ in ((_PERSON, "osoba"), (_PLACE, "místo"), (_WORK, "dílo"),
                     (_ORG, "organizace"), (_EVENT, "událost")):
        mm = pat.search(win_person if typ == "osoba" else win)
        if mm:
            cands.append((mm.end(), typ))
    if cands:
        return min(cands, key=lambda t: t[0])[1]   # bližší ke sponě = podmět
    if _ADJ_WORK.search(win):
        return "dílo"
    if _THING.search(win):
        return "pojem"
    # HANS_ENTITY_CLASSIFY_V4 — jen když je HLAVNÍ spona v minulém čase.
    if m and _PERSON_WEAK.fullmatch(m.group(0)):
        return "osoba"
    return "pojem"



class EntityStore:
    def __init__(self, config: dict, db_path: Optional[str] = None):
        self.config = config or {}
        self.cfg = (self.config.get("entity_store", {}) or {})
        self.db_path = (db_path
                        or self.config.get("diary_db")
                        or (self.config.get("hans_idle", {}) or {}).get("diary_db")
                        or "data/hans_diary.db")
        self._ensure()

    def _conn(self, ro: bool = False):
        if ro:
            return sqlite3.connect("file:%s?mode=ro" % self.db_path,
                                   uri=True, timeout=5)
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure(self):
        try:
            with self._conn() as c:
                c.executescript(_SCHEMA)
        except Exception as e:
            _log.warning("entity store schema: %s", e)

    # ── zápis ────────────────────────────────────────────────────────────
    def upsert(self, name: str, gloss: str, *, source: str = "",
               source_title: str = "", lang: str = "cs",
               etype: Optional[str] = None,
               aliases: Optional[List[str]] = None) -> bool:
        """Vlož/posil entitu. Glos se drží PRVNÍ dobrý (definiční, ze zdroje);
        opakované čtení jen posílá evidence_count. Vrátí True při zápisu."""
        nm = (name or "").strip()
        nn = _norm(nm)
        if not nm or len(nn) < 2:
            return False
        gloss = (gloss or "").strip()
        et = etype or _classify(gloss)
        now = time.time()
        al = aliases or []
        # base tvar bez závorky jako alias (Aj (faraon) → aj)
        base = _PAREN.sub("", nm).strip()
        if base and _norm(base) != nn:
            al.append(_norm(base))
        al = sorted(set(a for a in (al or []) if a and a != nn))
        try:
            with self._conn() as c:
                row = c.execute(
                    "SELECT id, gloss, evidence_count, aliases FROM entities "
                    "WHERE name_norm=?", (nn,)).fetchone()
                if row:
                    _id, _gloss, _ev, _al = row
                    merged = sorted(set(json.loads(_al or "[]")) | set(al))
                    new_gloss = _gloss
                    # doplň glos jen když chybí (neplýtvej, neměň definici)
                    if (not (_gloss or "").strip()) and gloss:
                        new_gloss = gloss
                    c.execute(
                        "UPDATE entities SET last_ts=?, evidence_count=?, "
                        "gloss=?, aliases=?, source=COALESCE(NULLIF(source,''),?),"
                        "source_title=COALESCE(NULLIF(source_title,''),?) "
                        "WHERE id=?",
                        (now, (_ev or 1) + 1, new_gloss, json.dumps(merged),
                         source, source_title, _id))
                    return True
                c.execute(
                    "INSERT INTO entities (name, name_norm, etype, gloss, "
                    "source, source_title, aliases, lang, first_ts, last_ts, "
                    "evidence_count) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
                    (nm, nn, et, gloss, source, source_title,
                     json.dumps(al), lang, now, now))
                return True
        except Exception as e:
            _log.warning("entity upsert '%s': %s", nm, e)
            return False

    def capture_from_reading(self, title: str, raw_text: str, *,
                             url: str = "", lang: str = "cs") -> bool:
        """Zachyť entitu z právě přečteného článku: name=vyřešený titul,
        gloss=první definiční věta (verbatim ze zdroje → 0 konfabulace).

        HANS_ENTITY_NO_INDEX_PAGES_V1 (26.8.) — ROZCESTNÍKY a SEZNAMY se
        nezachytávají. Nejsou to entity, jsou to indexy Wikipedie, a v paměti
        jen ředí recall („co víš o X" může vrátit výpis dílů). Odhalil je
        backfill faktů: 8 kusů s `je to = seznam na projektech Wikimedia`,
        vesměs „Seznam dílů seriálu …" z dohledávání epizod.
        ⚠️ Ověřeno proti CELÉ DB (463 entit): pravidlo odmítne přesně 10 a
        všech 10 jsou opravdu indexy — žádná legitimní entita nepadne.
        """
        if not (self.cfg.get("enabled", True)):
            return False
        if _INDEX_PAGE.search(title or ""):
            _log.debug("entity: %r je index/rozcestník → nezachytávám", title)
            return False
        gloss = _first_sentence(raw_text or "")
        # glos musí opravdu vypadat definičně (obsahuje „je/byl" apod.),
        # jinak je to náhodná první věta → radši bez glosu (jen jméno+zdroj).
        if gloss and not re.search(r"\b(je|byl|byla|bylo|jsou|patří|označuje)\b",
                                   gloss, re.IGNORECASE):
            gloss = ""
        return self.upsert(title, gloss, source=url,
                           source_title=title, lang=lang)

    def set_disambig(self, name: str, note: str) -> bool:
        try:
            with self._conn() as c:
                cur = c.execute("UPDATE entities SET disambig=? WHERE name_norm=?",
                                (note or "", _norm(name)))
                return cur.rowcount > 0
        except Exception:
            return False

    # ── dotaz ────────────────────────────────────────────────────────────
    def _all_keys(self):
        """(id, name_norm, aliases[]) všech entit — pro matching v Pythonu."""
        try:
            with self._conn(ro=True) as c:
                rows = c.execute(
                    "SELECT id, name_norm, aliases FROM entities").fetchall()
            out = []
            for _id, nn, al in rows:
                keys = [nn] + [a for a in json.loads(al or "[]") if a]
                out.append((_id, keys))
            return out
        except Exception:
            return []

    def get(self, entity_id: int):
        try:
            with self._conn(ro=True) as c:
                r = c.execute(
                    "SELECT name, etype, gloss, source, source_title, "
                    "disambig, evidence_count FROM entities WHERE id=?",
                    (entity_id,)).fetchone()
            if not r:
                return None
            # HANS_ENTITY_FACTS_IN_CHAT_V1 — `id` je tu proto, aby se k entitě
            # daly dohledat strukturovaná fakta (`entity_facts`). Přidání klíče
            # nic nerozbije: konzumenti čtou konkrétní jména, ne pořadí.
            return {"id": entity_id, "name": r[0], "etype": r[1], "gloss": r[2],
                    "source": r[3], "source_title": r[4],
                    "disambig": r[5], "evidence_count": r[6]}
        except Exception:
            return None

    def resolve(self, query: str, loose: bool = False,
                etype: Optional[str] = None):
        """Najdi ZNÁMOU entitu zmíněnou v dotazu. Vrátí dict entity nebo None.
        Token-prefix matching (české skloňování mění koncovky, drží prefixy):
        klíč se trefí, když KAŽDÝ jeho obsahový token (≥4 znaky) má v dotazu
        token se shodným prefixem. Krátká jména (bez obsahového tokenu ≥4)
        se ignorují (nejednoznačná → riziko false-positive). Vyhraje
        nejspecifičtější (nejvíc znaků) shoda.
        loose=True: navíc povolí shodu jen na POSLEDNÍM tokenu (příjmení) —
        „pan Sorge" → „Erich Robert Sorge". Volnější (riziko false-positive),
        používat jen kde to nevadí (grounding malby), NE v chatu na fakta."""
        if not (self.cfg.get("enabled", True)):
            return None
        q_tokens = _tokens(query)
        if not q_tokens:
            return None
        q_content = [t for t in q_tokens if len(t) >= 4]
        # HANS_ENTITY_PROPER_NOUN_PREFIX_V1 — matcher pro TENHLE dotaz
        _q_upper = _upper_folded(query)
        _ratio = float(self.cfg.get("proper_noun_prefix_ratio",
                                    _PROPER_RATIO_DEFAULT))

        def _match_q(key_tok: str, q_tok: str) -> bool:
            if not _tok_match(key_tok, q_tok):
                return False
            if key_tok == q_tok or q_tok not in _q_upper:
                return True     # presna shoda / obecne jmeno → jako dosud
            return (_prefix_len(key_tok, q_tok)
                    >= _ratio * max(len(key_tok), len(q_tok)))
        best_id, best_score = None, 0
        for _id, keys in self._all_keys():
            for k in keys:
                kt = [t for t in _tokens(k) if len(t) >= 4]
                if not kt:
                    continue  # jen krátké tokeny → moc nejednoznačné
                matched = [t for t in kt
                           if any(_match_q(t, qt) for qt in q_tokens)]
                # plná shoda VŠECH tokenů, NEBO (u víceslovných jmen) shoda
                # prvního I posledního tokenu — řeší prostřední jména
                # („Erich Robert Sorge" ↔ dotaz „Erich Sorge").
                full = len(matched) == len(kt)
                ends = (len(kt) >= 2 and kt[0] in matched and kt[-1] in matched)
                # loose: příjmení (poslední token) samo stačí — ALE jen když
                # dotaz NEMÁ žádný jiný obsahový token, který se s entitou
                # rozchází (jinak „Richard Sorge" ≠ „Erich … Sorge" → NEmatchuj
                # špatného jmenovce). Tj. všechny obsahové tokeny dotazu musí
                # sednout na entitu.
                q_all_matched = all(
                    any(_match_q(kt_tok, qt) for kt_tok in kt)
                    for qt in q_content)
                # HANS_ENTITY_SURNAME_PERSON_ONLY_V1 (4.8.) — „shoda na posledním
                # tokenu" dává smysl JEN u jmen osob (příjmení: „pan Sorge" →
                # „Erich Robert Sorge"). U ostatních entit je to jen slabá shoda
                # na náhodném koncovém slově — a `_tok_match` bere prefix ≥4, což
                # spolehlivě splete i věci bez souvislosti.
                # Doloženo 4.8.: „namaluj Bud Spencer" → „Což takhle dát si
                # ŠPENÁT" (spen|cer ↔ spen|at) → obraz Jiřího Sováka a Vladimíra
                # Menšíka místo herce. Proto: surname jen pro osoby/postavy.
                _et = (self.get(_id) or {}).get("etype")
                surname = (loose and len(kt) >= 2 and kt[-1] in matched
                           and q_all_matched and _et in ("osoba", "postava"))
                # etype filtr (HANS_ART_PULID_V1): omez na daný druh (např.
                # 'osoba' → v „osoba + objekt" najdi osobu, ne přebíjející objekt)
                _ok_etype = (etype is None
                             or (self.get(_id) or {}).get("etype") == etype)
                if full or ends:
                    score = sum(len(t) for t in matched)
                    if score > best_score and _ok_etype:
                        best_id, best_score = _id, score
                elif surname:
                    score = len(kt[-1])   # slabší priorita než plná/koncová
                    if score > best_score and _ok_etype:
                        best_id, best_score = _id, score
        if best_id is None:
            return None
        return self.get(best_id)

    def fact_block(self, entity: dict) -> str:
        """Autoritativní fakt z entity pro grounding (jako vztahová karta)."""
        if not entity:
            return ""
        parts = []
        g = (entity.get("gloss") or "").strip()
        if g:
            parts.append(g)
        d = (entity.get("disambig") or "").strip()
        if d:
            parts.append("Upřesnění: " + d)
        src = (entity.get("source") or "").strip()
        body = " ".join(parts) if parts else (
            "%s — o tomto pojmu mám záznam, ale bez bližší definice."
            % entity.get("name", "?"))
        nm = entity.get("name", "?")
        if src:
            pre = "Ověřený fakt o „%s“ (z mého čtení, zdroj: %s): " % (nm, src)
        else:
            pre = "Ověřený fakt o „%s“ (z mého čtení): " % nm

        # HANS_ENTITY_FACTS_IN_CHAT_V1 (26.8.) — připoj STRUKTUROVANÁ fakta.
        # Doloženo kontrolním rozhovorem: Hans měl v `entity_facts` 722 faktů
        # s proveniencí (Kost = gotická architektura, Cardiff = novogotika)
        # a v chatu na „v jakém slohu je hrad Kost?" odpověděl „nemám
        # detailnější informace". Korpus byl odříznutý od jediného místa,
        # kde se ho někdo ptá — fakta se dosud napojila jen do malování.
        # Váže se na `entity.id`, takže se NIC nedohledává a nehrozí jmenovec:
        # když je entita resolvovaná správně, jsou správně i fakta.
        struktura = self._facts_line(entity.get("id"))
        return pre + body + (("\n" + struktura) if struktura else "")

    # Pořadí je kurátorované: co odpovídá na „jaký/kde/kdy", ne co je v DB první.
    _FACT_ORDER = ("je to", "sloh", "leží v", "stát", "vznik", "vlastník",
                   "autor", "vydáno", "žánr", "narození", "úmrtí", "profese",
                   "národnost", "sídlo", "datum", "místo", "vystupuje v")

    def _facts_line(self, entity_id) -> str:
        """HANS_ENTITY_FACTS_IN_CHAT_V1 — řádek strukturovaných faktů (Wikidata).
        Prázdné = entita fakta nemá; to je legitimní a nic se nedomýšlí."""
        if not entity_id:
            return ""
        try:
            with self._conn(ro=True) as c:
                rows = dict(c.execute(
                    "SELECT klic, hodnota FROM entity_facts WHERE entity_id=?",
                    (entity_id,)).fetchall())
        except Exception:
            return ""                     # tabulka nemusí existovat (starší DB)
        if not rows:
            return ""
        poradi = [k for k in self._FACT_ORDER if rows.get(k)]
        poradi += [k for k in rows if k not in self._FACT_ORDER]
        return ("Ověřená fakta (Wikidata): "
                + "; ".join("%s = %s" % (k, rows[k]) for k in poradi))

    def count(self) -> int:
        try:
            with self._conn(ro=True) as c:
                return int(c.execute(
                    "SELECT COUNT(*) FROM entities").fetchone()[0])
        except Exception:
            return 0
