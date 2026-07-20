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


def _tok_match(a: str, b: str) -> bool:
    """Dva tokeny odpovídají téže bázi navzdory českému skloňování:
    sdílený prefix ≥4 znaky A ≥ (kratší délka − 3). „cardiffsky"↔„cardiffskem",
    „hrad"↔„hradu", „design"↔„design"."""
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n >= 4 and n >= min(len(a), len(b)) - 3


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
_PERSON = re.compile(
    r"\bbyl[aiy]?\b|\bje\b.{0,40}\b("
    r"spisovatel|skladatel|malíř|politik|král|císař|faraon|herec|herečka|"
    r"vědec|fyzik|filozof|hudebník|zpěvák|generál|vojevůdce|panovník|"
    r"režisér|architekt|básník|autor|matematik|objevitel|vynálezce)",
    re.IGNORECASE)
# HANS_ENTITY_POSTAVA_V1 (20.7.) — fiktivní / literární / filmová postava.
# Kritérium pro dopadu: paint_subject volá img2img z Wiki portrétu (viz gate
# etype in ('osoba','postava')). Wiki článek fiktivní postavy má obvykle
# obrázek herce/ilustrace (Rimmer→Chris Barrie, Sherlock→Paget kresba).
_POSTAVA = re.compile(
    r"\bje\b.{0,40}\b(fiktivní\s+postav|literární\s+postav|filmov[áa]\s+postav|"
    r"seri[áa]lov[áa]\s+postav|animovan[áa]\s+postav|hlavní\s+postav|"
    r"vedlejší\s+postav|hrdin[aoyi]|postav[ay]\s+seriálu|postav[ay]\s+filmu|"
    r"postav[ay]\s+románu|postav[ay]\s+knihy)",
    re.IGNORECASE)
_PLACE = re.compile(
    r"\bje\b.{0,40}\b(město|hrad|zámek|hora|řeka|jezero|stát|země|obec|"
    r"vesnice|ostrov|pohoří|kraj|region|čtvrť|náměstí|budova|katedrála)",
    re.IGNORECASE)
_WORK = re.compile(
    r"\bje\b.{0,40}\b(film|kniha|román|opera|skladba|album|píseň|obraz|"
    r"báseň|hra|seriál|dílo)",
    re.IGNORECASE)
_ORG = re.compile(
    r"\bje\b.{0,40}\b(organizace|společnost|firma|klub|strana|spolek|"
    r"instituce|univerzita|škola|tým)",
    re.IGNORECASE)
_EVENT = re.compile(
    r"\bje\b.{0,40}\b(válka|bitva|turnaj|revoluce|povstání|událost|"
    r"mistrovství|festival|olympiáda)",
    re.IGNORECASE)


def _classify(gloss: str) -> str:
    g = gloss or ""
    # POSTAVA první (silnější signál než _PERSON „byl" — fiktivní postava
    # může mít i „byl vytvořen…" což by shodl _PERSON, ale fikce má přednost).
    if _POSTAVA.search(g):
        return "postava"
    if _PERSON.search(g):
        return "osoba"
    if _PLACE.search(g):
        return "místo"
    if _WORK.search(g):
        return "dílo"
    if _ORG.search(g):
        return "organizace"
    if _EVENT.search(g):
        return "událost"
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
        gloss=první definiční věta (verbatim ze zdroje → 0 konfabulace)."""
        if not (self.cfg.get("enabled", True)):
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
            return {"name": r[0], "etype": r[1], "gloss": r[2],
                    "source": r[3], "source_title": r[4],
                    "disambig": r[5], "evidence_count": r[6]}
        except Exception:
            return None

    def resolve(self, query: str, loose: bool = False):
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
        best_id, best_score = None, 0
        for _id, keys in self._all_keys():
            for k in keys:
                kt = [t for t in _tokens(k) if len(t) >= 4]
                if not kt:
                    continue  # jen krátké tokeny → moc nejednoznačné
                matched = [t for t in kt
                           if any(_tok_match(t, qt) for qt in q_tokens)]
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
                    any(_tok_match(kt_tok, qt) for kt_tok in kt)
                    for qt in q_content)
                surname = (loose and len(kt) >= 2 and kt[-1] in matched
                           and q_all_matched)
                if full or ends:
                    score = sum(len(t) for t in matched)
                    if score > best_score:
                        best_id, best_score = _id, score
                elif surname:
                    score = len(kt[-1])   # slabší priorita než plná/koncová
                    if score > best_score:
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
        return pre + body

    def count(self) -> int:
        try:
            with self._conn(ro=True) as c:
                return int(c.execute(
                    "SELECT COUNT(*) FROM entities").fetchone()[0])
        except Exception:
            return 0
