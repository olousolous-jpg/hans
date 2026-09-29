"""HANS_QUERY_DIARY_V1 (25. 9.) — Hans si dotaz nad VLASTNÍMI záznamy sestaví sám.

Nápad uživatele 13. 8.: „aby si sám sestavil příkaz na otázku, kterou má
v záznamech — ať nemusíme všechny možnosti kódovat natvrdo“ („kolik filmů
jsi viděl?“). Záchranná síť pro dlouhý ocas souhrnných otázek, NE náhrada
natvrdo psaných příkazů (ty jdou první, jsou rychlé a bez LLM).

Změřeno 25. 9. (backlog `QUERY_DIARY_25_09`):
  • poptávka: 4 nezodpovězené souhrnné otázky za celou historii + 5 dalších,
    na které se natvrdo psaly příkazy (`/obrazy`, `/film`);
  • `hans-czech` (rezidentní, 0 VRAM navíc, medián 2,3 s) nad KURÁTOROVANÝMI
    pohledy: odložená sada 13/15 (s přijatelnými 14/15), qwen2.5:7b 11/21 —
    vymýšlí sloupce;
  • rozdělení přehrávání na `filmy` / `serialy` / `tv_vysilani` odstranilo
    celou třídu „zapomněl filtr na film“ (2 → 0) — tvar dat bije pravidlo
    v promptu.

Bezpečnost: SQL, ne Python. Spojení `mode=ro`, pohledy jen TEMP (soubor se
nemění), povolen jediný příkaz SELECT/WITH bez zakázaných slov, časový limit.
Chyby, které zbyly, jsou VĚROHODNÉ (číslo, které zní jako odpověď) → odpověď
vždy nese větu, z čeho se počítalo, a SQL chyba dostane jeden opravný pokus.
Jen pro ZNÁMÉ osoby — setkání říkají, kdo byl kdy doma.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time

_log = logging.getLogger("hans_query_diary")

VIEWS = r"""
CREATE TEMP VIEW prehravani AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  CASE WHEN note LIKE 'Typ: movie%' THEN 'film' WHEN note LIKE 'Typ: episode%' THEN 'epizoda'
       WHEN note LIKE 'Typ: channel%' THEN 'tv_kanal' ELSE 'jine' END typ, title titul
  FROM diary WHERE event_type='kodi_playing';
CREATE TEMP VIEW filmy AS SELECT cas, den, titul FROM prehravani WHERE typ='film';
CREATE TEMP VIEW serialy AS SELECT cas, den, titul FROM prehravani WHERE typ='epizoda';
CREATE TEMP VIEW tv_vysilani AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  title titul, CASE WHEN instr(note,'| kanál: ')>0 THEN trim(substr(substr(note,instr(note,'| kanál: ')+9),1,
       instr(substr(note,instr(note,'| kanál: ')+9)||' |',' |')-1)) END kanal
  FROM diary WHERE event_type='kodi_playing' AND note LIKE 'Typ: channel%';
CREATE TEMP VIEW cteni_clanku AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  title titul, CASE WHEN note LIKE '[%]%' THEN substr(note,2,instr(note,']')-2) ELSE '' END zdroj
  FROM diary WHERE event_type='web_read';
CREATE TEMP VIEW cteni_knih AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  trim(substr(title,1,instr(title,' — ')-1)) kniha FROM diary WHERE event_type='book_read';
CREATE TEMP VIEW knihy AS SELECT book_title kniha, author autor, total_chapters kapitol_celkem,
  current_chapter kapitola_ted, datetime(started_at,'unixepoch','localtime') zacatek,
  datetime(finished_at,'unixepoch','localtime') konec,
  CASE status WHEN 'reading' THEN 'cte' WHEN 'finished' THEN 'doctena' ELSE 'planovana' END stav FROM hans_library;
CREATE TEMP VIEW obrazy AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  title nazev FROM diary WHERE event_type='artwork';
CREATE TEMP VIEW pripady AS SELECT id, title nazev, CASE WHEN closed_at>0 THEN 'uzavreny' ELSE 'otevreny' END stav,
  datetime(opened_at,'unixepoch','localtime') otevren,
  CASE WHEN closed_at>0 THEN datetime(closed_at,'unixepoch','localtime') END uzavren,
  CASE WHEN closed_at>0 THEN round((closed_at-opened_at)/86400.0,1) END dni_trvani FROM kolac_cases;
CREATE TEMP VIEW setkani AS SELECT lower(person_id) osoba, datetime(started_at,'unixepoch','localtime') zacatek,
  datetime(ended_at,'unixepoch','localtime') konec, date(started_at,'unixepoch','localtime') den,
  round((ended_at-started_at)/60.0,1) minut FROM encounters;
CREATE TEMP VIEW rozhovory AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  lower(title) osoba FROM diary WHERE event_type='human_chat';
CREATE TEMP VIEW dialogy_kolac AS SELECT datetime(ts,'unixepoch','localtime') cas, date(ts,'unixepoch','localtime') den,
  trim(substr(note,7,instr(note||char(10),char(10))-7)) tema FROM diary WHERE event_type='teddy_dialog';
CREATE TEMP VIEW studium AS SELECT topic tema, CASE status WHEN 'completed' THEN 'dokonceno' WHEN 'active' THEN 'probiha'
  ELSE 'ceka' END stav, sessions_done sezeni, datetime(started_ts,'unixepoch','localtime') zacatek FROM study_program;
"""

# Popisky pohledů pro větu „z čeho jsem počítal“ (deterministicky, ne od modelu).
POPISKY = {
    "filmy": "puštěné filmy", "serialy": "puštěné díly seriálů",
    "tv_vysilani": "živé TV vysílání", "cteni_clanku": "přečtené články",
    "cteni_knih": "přečtené kapitoly knih", "knihy": "moje knihovna",
    "obrazy": "moje obrazy", "pripady": "Koláčovy případy",
    "setkani": "kdo byl doma (kamera)", "rozhovory": "rozhovory v chatu",
    "dialogy_kolac": "rozhovory s Koláčem", "studium": "studijní programy",
}

_SCHEMA = """Dnes je {DNES} (letošní rok {ROK}).
Máš jen tyto tabulky (SQLite). Časy jsou místní text 'YYYY-MM-DD HH:MM:SS', `den` je 'YYYY-MM-DD'.
Dnešek = date('now','localtime'); včera = date('now','localtime','-1 day'); posledních N dní = den >= date('now','localtime','-N days').
Osoby (sloupec osoba) jsou malými písmeny bez diakritiky: {OSOBY}.
- filmy(cas, den, titul) — puštěné filmy (jen filmy). Jeden řádek = jedno spuštění.
- serialy(cas, den, titul) — puštěné díly seriálů. Jeden řádek = jeden díl.
- tv_vysilani(cas, den, titul, kanal) — živé TV vysílání; titul = pořad, kanal = stanice (zaznamenává se až od 25. 9. 2026, starší NULL).
- cteni_clanku(cas, den, titul, zdroj) — přečtené články; zdroj ∈ 'kodi','object','interest','news','random_wiki','goal', ''.
- cteni_knih(cas, den, kniha) — jedna přečtená kapitola knihy = jeden řádek.
- knihy(kniha, autor, kapitol_celkem, kapitola_ted, zacatek, konec, stav) — stav ∈ 'cte','doctena','planovana'. Názvy knih jsou bez diakritiky, hledej přes LIKE '%…%'.
- obrazy(cas, den, nazev) — obrazy, které jsi namaloval.
- pripady(id, nazev, stav, otevren, uzavren, dni_trvani) — Koláčovy detektivní případy; stav ∈ 'otevreny','uzavreny'.
- setkani(osoba, zacatek, konec, den, minut) — kdy byl kdo doma před kamerou (jedno setkání = jeden řádek).
- rozhovory(cas, den, osoba) — jedna výměna v chatu s člověkem = jeden řádek.
- dialogy_kolac(cas, den, tema) — tvoje rozhovory s Koláčem.
- studium(tema, stav, sezeni, zacatek) — studijní programy; stav ∈ 'dokonceno','probiha','ceka'.
Pravidla:
- Kdo byl doma / koho jsi viděl / kdo tu byl = tabulka setkani (kamera). rozhovory = jen psaní v chatu.
- „Kolik filmů / knih / témat“ = count(DISTINCT …); „kolikrát“ = count(*).
- „Nejdéle / nejvíc času“ = sum(minut) … GROUP BY … ORDER BY sum DESC. „Nejčastěji“ = GROUP BY … ORDER BY count(*) DESC.
- „Poslední X“ = ORDER BY čas té události DESC LIMIT 1 (u uzavřených případů podle `uzavren` a jen stav='uzavreny').
- „Který / jaký“ se ptá na NÁZEV, ne na číslo.
- Měsíc bez roku = letošní rok: den LIKE 'RRRR-MM%'. Nikdy nepiš do dotazu slova jako „včera“, vždy výraz date(...).
- Když otázka NEJDE zodpovědět z těchto tabulek (názor, pocit, obecná znalost, čas), odpověz jen: NELZE
Odpověz JEN jedním SQL dotazem SELECT (nebo NELZE), bez vysvětlení a bez ```.

Příklady:
Otázka: kolik obrazů jsi namaloval celkem?
SQL: SELECT count(*) FROM obrazy
Otázka: kdy jsi naposledy četl nějaký článek?
SQL: SELECT max(cas) FROM cteni_clanku
Otázka: kolik minut tu byl {OSOBA1} dnes?
SQL: SELECT round(sum(minut)) FROM setkani WHERE osoba='{OSOBA1}' AND den=date('now','localtime')
Otázka: kolik obrazů jsi namaloval včera?
SQL: SELECT count(*) FROM obrazy WHERE den=date('now','localtime','-1 day')
Otázka: kolik kapitol jsi přečetl v srpnu?
SQL: SELECT count(*) FROM cteni_knih WHERE den LIKE '{ROK}-08%'
Otázka: co tě na tom filmu nejvíc zaujalo?
SQL: NELZE
"""

# Brána: jen souhrnné otázky (jinak by každá věta stála 2+ s navíc).
_BRANA = re.compile(
    r"\b(kolik\w*|kolikrat\w*|kdy\s+(?:jsi|jste|jsme|tu|byl\w*|naposledy|poprve)|naposledy|poprve"
    r"|nejcasteji|nejvic\w*|nejdel\w*|nejdriv|prumer\w*|celkem|kter\w+\s+den)\b")
_ZAKAZ = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|pragma|replace"
                    r"|vacuum|reindex|load_extension)\b", re.I)


def _fold(s: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c)).lower()


def je_kandidat(veta: str) -> bool:
    v = _fold(veta)
    if len(v) > 200:
        return False
    return bool(_BRANA.search(v))


def _spojeni(db_path: str) -> sqlite3.Connection:
    import os
    if not os.path.isabs(db_path):   # relativní cesta = od kořene projektu, ne od cwd
        db_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), db_path)
    c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
    c.executescript(VIEWS)
    return c


def _vycisti(txt: str) -> str:
    t = re.sub(r"(?s)<think>.*?</think>", "", txt or "").strip()
    t = t.strip("`").strip()
    t = re.sub(r"^(?:sql)\s*[:\n]", "", t, flags=re.I).strip()
    m = re.search(r"(?is)\b(select|with)\b.*", t)
    if m:
        t = m.group(0).split("\n\n")[0]
    return t.strip().rstrip(";").strip()


def spust(c: sqlite3.Connection, sql: str):
    if not re.match(r"(?is)^(select|with)\b", sql) or ";" in sql or _ZAKAZ.search(sql):
        raise ValueError("odmítnuto klecí")
    t0 = time.time()
    c.set_progress_handler(lambda: 1 if time.time() - t0 > 3 else 0, 10000)
    try:
        cur = c.execute(sql)
        return [d[0] for d in (cur.description or [])], cur.fetchmany(20)
    finally:
        c.set_progress_handler(None, 0)


def _osoby(config: dict) -> str:
    """Osoby, které v setkáních SKUTEČNĚ jsou (ne jen `known_persons` z configu —
    tam 25. 9. chyběla jedna osoba z domácnosti a model za ni dosadil jinou)."""
    jm = set()
    try:
        c = _spojeni((config.get("paths", {}) or {}).get("diary_db")
                     or config.get("diary_db") or "data/hans_diary.db")
        try:
            jm = {_fold(r[0]) for r in c.execute(
                "SELECT osoba FROM setkani GROUP BY osoba HAVING count(*) >= 3") if r[0]}
        finally:
            c.close()
    except Exception:
        pass
    if not jm:
        jm = {_fold(k) for k in ((config.get("known_persons") or {}) if isinstance(config, dict) else {})}
    return ", ".join(sorted(jm)) if jm else "(jména domácnosti)"


def _model(config: dict) -> str:
    # ⚠️ NE `openwebui_chat.model_name` — to je embeddingový bge-m3 (první verze
    # tudy volala embedding model a vracela prázdno na všech 35 otázek).
    return ((config.get("query_diary", {}) or {}).get("model")
            or (config.get("models", {}) or {}).get("utility") or "hans-czech:latest")


def _zeptej(config: dict, otazka: str, oprava: str = "") -> str:
    from scripts.ollama_client import ollama_chat
    sch = (_SCHEMA.replace("{DNES}", time.strftime("%Y-%m-%d"))
           .replace("{ROK}", time.strftime("%Y")).replace("{OSOBY}", _osoby(config))
           .replace("{OSOBA1}", (_osoby(config).split(", ") or ["osoba"])[0]))
    msgs = [{"role": "system", "content": sch},
            {"role": "user", "content": "Otázka: %s\nSQL:" % otazka}]
    if oprava:
        msgs += [{"role": "assistant", "content": oprava.split("\n", 1)[0]},
                 {"role": "user", "content": "Ten dotaz skončil chybou: %s. Oprav ho, "
                  "použij jen sloupce z tabulek výše. SQL:" % oprava.split("\n", 1)[1]}]
    return ollama_chat(_model(config), msgs, config=config,
                       options={"temperature": 0, "num_predict": 300, "num_ctx": 4096}) or ""


def kontrola(otazka: str, sql: str) -> str:
    """Deterministická kontrola významu dotazu. '' = v pořádku, jinak důvod
    (ten dostane model jako opravný pokus). Změřeno 25. 9.: „kdy tu byl naposledy
    <osoba mimo seznam>“ → dotaz na JINOU osobu; „první článek dnes“ → bez
    filtru na den. Obojí věrohodné, obojí špatně."""
    q = _fold(otazka)
    s = sql.lower()
    for jm in re.findall(r"osoba\s*=\s*'([^']+)'", s):
        kmen = _fold(jm)[:max(3, len(jm) - 1)]
        if kmen not in q:
            return ("otázka se na osobu '%s' neptá — použij osobu z otázky, a když "
                    "v seznamu není, odpověz NELZE" % jm)
    if re.search(r"\b(dnes|dneska|dnesni\w*)\b", q) and "'now'" not in s:
        return "otázka se ptá na dnešek, dotaz nemá filtr den=date('now','localtime')"
    if re.search(r"\b(vcera|vcerejs\w*)\b", q) and "-1 day" not in s:
        return "otázka se ptá na včerejšek, dotaz nemá filtr den=date('now','localtime','-1 day')"
    return ""


_DNY = ("v pondělí", "v úterý", "ve středu", "ve čtvrtek", "v pátek", "v sobotu", "v neděli")

# Tvary podstatného jména k počtu (1 / 2–4 / 5+) podle pohledu — jen pro count().
_TVARY = {
    "obrazy": ("obraz", "obrazy", "obrazů"), "filmy": ("film", "filmy", "filmů"),
    "serialy": ("díl", "díly", "dílů"), "tv_vysilani": ("pořad", "pořady", "pořadů"),
    "cteni_clanku": ("článek", "články", "článků"), "cteni_knih": ("kapitola", "kapitoly", "kapitol"),
    "knihy": ("kniha", "knihy", "knih"), "pripady": ("případ", "případy", "případů"),
    "rozhovory": ("výměna", "výměny", "výměn"), "dialogy_kolac": ("rozhovor", "rozhovory", "rozhovorů"),
    "studium": ("program", "programy", "programů"), "setkani": ("návštěva", "návštěvy", "návštěv"),
}


def _tvar(n: int, t3) -> str:
    return t3[0] if n == 1 else (t3[1] if 2 <= n <= 4 else t3[2])


def _datum(v: str) -> str:
    """'YYYY-MM-DD[ HH:MM:SS]' → „dnes v 11:17“, „včera“, „v pondělí 21. 9.“, „4. 8. 2026“."""
    from datetime import date, datetime
    m = re.match(r"^(\d{4})-(\d\d)-(\d\d)(?:[ T](\d\d):(\d\d))?", v)
    if not m:
        return v
    d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    cas = (" v %d:%s" % (int(m.group(4)), m.group(5))) if m.group(4) else ""
    dni = (datetime.now().date() - d).days
    if dni == 0:
        return "dnes" + cas
    if dni == 1:
        return "včera" + cas
    if 1 < dni < 7:
        return "%s %d. %d.%s" % (_DNY[d.weekday()], d.day, d.month, cas)
    if d.year == datetime.now().year:
        return "%d. %d.%s" % (d.day, d.month, cas)
    return "%d. %d. %d" % (d.day, d.month, d.year)


def _hodnota(v) -> str:
    if isinstance(v, float):
        return ("%d" % v) if v == int(v) else ("%.1f" % v).replace(".", ",")
    if isinstance(v, str) and re.match(r"^\d{4}-\d\d-\d\d", v):
        return _datum(v)
    return str(v)


def _pocet_s_tvarem(v, sql: str) -> str:
    """32 → „32 obrazů“, když jde o count() nad jedním pohledem se známými tvary."""
    s = sql.lower()
    if not isinstance(v, int) or not re.search(r"\bcount\(", s):
        return _hodnota(v)
    tab = [k for k in _TVARY if re.search(r"\b%s\b" % k, s)]
    if len(tab) != 1:
        return _hodnota(v)
    if tab[0] == "filmy" and "count(distinct" not in s:
        return "%d %s" % (v, "spuštění" if v != 1 else "spuštění") + " filmů"
    return "%d %s" % (v, _tvar(v, _TVARY[tab[0]]))


def _popis(sql: str) -> str:
    tabulky = [POPISKY[t] for t in POPISKY if re.search(r"\b%s\b" % t, sql)]
    s = sql.lower()
    if "count(distinct" in s:
        jak = "počet různých položek"
    elif re.search(r"\bcount\(", s):
        jak = "počet záznamů"
    elif re.search(r"\bsum\(", s):
        jak = "součet"
    elif re.search(r"\bavg\(", s):
        jak = "průměr"
    elif re.search(r"\bmax\(|order by [^)]*desc", s):
        jak = "nejnovější / největší"
    elif re.search(r"\bmin\(", s):
        jak = "nejstarší / nejmenší"
    else:
        jak = "výpis"
    return "%s — %s" % (", ".join(tabulky) or "moje záznamy", jak)


def odpovez(config: dict, db_path: str, otazka: str) -> dict | None:
    """{'text', 'sql', 'radky'} nebo None (nejde / NELZE / chyba → běžná cesta)."""
    t0 = time.time()
    raw = _zeptej(config, otazka)
    sql = _vycisti(raw)
    if not sql or sql.upper().startswith("NELZE") or "NELZE" in (raw or "")[:20].upper():
        _log.info("HANS_QUERY_DIARY_V1: NELZE (%.1f s): %.60s", time.time() - t0, otazka)
        return None
    c = _spojeni(db_path)
    try:
        try:
            duvod = kontrola(otazka, sql)
            if duvod:
                raise ValueError(duvod)
            cols, rows = spust(c, sql)
        except Exception as e:
            # jeden opravný pokus s chybovou hláškou (rozptyl modelu: sloupec navíc)
            sql2 = _vycisti(_zeptej(config, otazka, oprava="%s\n%s" % (sql, e)))
            if not sql2 or sql2.upper().startswith("NELZE"):
                return None
            try:
                duvod2 = kontrola(otazka, sql2)
                if duvod2:
                    raise ValueError(duvod2)
                cols, rows = spust(c, sql2)
                sql = sql2
            except Exception as e2:
                _log.info("HANS_QUERY_DIARY_V1: SQL selhalo i po opravě (%s): %s", e2, sql2[:160])
                return None
    finally:
        c.close()
    zdroj = _popis(sql)
    if not rows or all(v is None for v in rows[0]):
        text = "V mých záznamech jsem k tomu nic nenašel. (Hledal jsem v: %s.)" % zdroj
    elif len(rows) == 1 and len(rows[0]) == 1:
        text = "Podle mých záznamů: %s. (Spočítáno z: %s.)" % (_pocet_s_tvarem(rows[0][0], sql), zdroj)
    else:
        polozky = ["; ".join(_hodnota(v) for v in r if v is not None) for r in rows[:8]]
        text = "Podle mých záznamů: %s. (Spočítáno z: %s.)" % (", ".join(polozky), zdroj)
    _log.info("HANS_QUERY_DIARY_V1: %.1f s | %.60s | %s", time.time() - t0, otazka, sql[:200])
    return {"text": text, "sql": sql, "radky": rows}


# ── „jak jsi to spočítal?“ ─────────────────────────────────────────────────
# Poslední odpověď z paměti na osobu; dotaz na postup do 10 min ukáže SQL.
# Zachytává se PŘED routerem: „odkud to víš?“ by jinak šlo na /zdroje
# (výpis studijních odkazů), což na odpověď z paměti nesedí.
_POSLEDNI: dict = {}
_POSTUP = re.compile(
    r"\b(jak\s+(?:jsi|jste)\s+(?:to\s+)?(?:spocital|spocitala|zjistil|zjistila|vypocital|vypocitala|nasel|prisel)"
    r"|z\s+ceho\s+(?:jsi|jste|to)|odkud\s+to\s+(?:vis|vite|mas|mate)"
    r"|ukaz\w*\s+(?:mi\s+)?(?:ten\s+)?(?:dotaz|sql)|jaky\s+(?:dotaz|sql))")


def zapamatuj(jmeno: str, otazka: str, sql: str) -> None:
    _POSLEDNI[(jmeno or "").lower()] = {"ts": time.time(), "otazka": otazka, "sql": sql}


def odpoved_na_postup(jmeno: str, veta: str, okno_s: float = 600) -> str:
    """'' = nejde o dotaz na postup nebo žádná čerstvá odpověď z paměti."""
    p = _POSLEDNI.get((jmeno or "").lower())
    if not p or time.time() - p["ts"] > okno_s or not _POSTUP.search(_fold(veta)):
        return ""
    return ("Spočítal jsem to z vlastních záznamů tímhle dotazem (otázka „%s“):\n%s"
            % (p["otazka"], p["sql"]))
