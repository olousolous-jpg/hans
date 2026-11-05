#!/usr/bin/env python3
"""
HANS_BEHAVIOUR_EVIDENCE_V1 — persona-free evidence o tom, co Hans DĚLAL.

PROČ existuje: Severčin vstup byl z devíti desetin ozvěna. `stances` se
extrahují z večerní reflexe, kterou `hans_synthesis.synthesize` generuje
s předřazeným `persona_core` a se stylovým promptem, který modelu doslova
říká „Máš britskou rezervovanost a smysl pro detail". Nejsilnější Hansovy
postoje jsou pak „Všímám si detailů" (×48) a „Cením si pečlivosti" (×44) —
tedy ta věta zpátky. Severka pak porovnává ROLI s tvrzeními, která z té role
vznikla, a nutně dojde k „drift malý". Koníčky jsou navíc saturované
(13 koníčků, 12 z nich mezi 57–68) → nulová rozlišovací síla.

CO tenhle modul dělá jinak: nebere NIC, co napsal model v Hansově hlase.
Tři vrstvy, každá tvrdší než ta předchozí:

  1. VLASTNÍ POZOROVÁNÍ (`self_insights.insight_en`) — jediný existující
     rozbor, jehož reasoning prompt personu NEOBSAHUJE (persona vstupuje až
     v překladovém kroku, proto se bere ANGLICKÝ originál, ne `insight_cs`).
  2. TVRDÁ ČÍSLA — čím reálně trávil čas. Počítané ze SQL, ne vyprávěné.
  3. ODEZVA SVĚTA — co Hans NEMOHL napsat: co uživatel přijal a co odmítl,
     kolikrát ho opravil, co Hans vzdal, kde mu Koláč postoj oslabil.
     Tahle vrstva je jediná úplně mimo jeho dosah, a proto nejcennější.

⚠️ Vědomě NEOBSAHUJE `spontaneous` (šablony — `HANS_SPONTANEOUS_TEMPLATE_MARK_V1`),
`evening_reflection`, `introspection` ani dialogy s Koláčem: všechno to píše
model v hlase persony. Archiv 10.6. navíc explicitně rozhodl, že Koláč není
zdroj postojů („ten kecá").

⚠️ HANS_BEHAVIOUR_EVIDENCE_V2 (27.8.) opravila DVĚ vady V1, obě vlastní výroby:
  a) `ignored` se slepilo s `rejected` → hlásilo se „přijal/odmítl 10 / 37".
     `ignored` ale není odmítnutí, nýbrž „pán zrovna řešil něco jiného".
     Skutečná bilance je 10 ANO : 11 NE. Viz poznámka u `_odezva`.
  b) Čtyři položky se počítaly za CELOU HISTORII pod nadpisem „posledních
     60 dní" (cíle, dostudovaná témata, zamítnutá prohloubení). Teď má okno
     všechno a nadpisy ho jmenují.
📌 Obecně: čísla v tomhle bloku rozhodují o IDENTITĚ, takže každý ukazatel
musí říkat přesně to, co měří — jinak si Hans o sobě odvodí nepravdu.

API:  block(config, diary_db_path, window_days=60) -> str   ('' když nic)
"""
from __future__ import annotations

import json
import os
import sqlite3
import time

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger(
    "hans_behaviour_evidence")

WINDOW_DAYS = 60
_MAX_INSIGHTS = 6


def _ro(path: str):
    return sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=3.0)


def _q1(conn, sql, args=()):
    try:
        r = conn.execute(sql, args).fetchone()
        return (r[0] if r and r[0] is not None else 0)
    except Exception:
        return 0


def _insights(conn, since: float) -> list:
    """Vlastní pozorování — ANGLICKÝ originál (persona-free krok)."""
    try:
        rows = conn.execute(
            "SELECT lens_id, insight_en FROM self_insights "
            "WHERE length(insight_en) > 50 AND ts >= ? "
            "ORDER BY ts DESC LIMIT ?", (since, _MAX_INSIGHTS)).fetchall()
    except Exception:
        return []
    seen, out = set(), []
    for lens, txt in rows:                     # jeden nejnovější na lens
        if lens in seen:
            continue
        seen.add(lens)
        out.append((lens, " ".join((txt or "").split())[:420]))
    return out


def _cinnost(conn, since: float) -> list:
    """Tvrdá čísla — čím trávil čas."""
    d = [
        ("studijních sezení", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='study_note' AND ts>=?", (since,))),
        # V2: okno i tady — dřív to bylo za CELOU historii pod nadpisem
        # „posledních 60 dní"
        ("dostudovaných témat do hloubky", _q1(conn, "SELECT count(*) FROM "
            "study_program WHERE status='completed' AND started_ts>=?", (since,))),
        ("namalovaných obrazů", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='artwork' AND ts>=?", (since,))),
        ("napsaných sekcí díla", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='writing_section' AND ts>=?", (since,))),
        ("dočtených knih", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='book_finished' AND ts>=?", (since,))),
        ("uzavřených případů", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='case_closed' AND ts>=?", (since,))),
        ("přečtených článků", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='web_read' AND ts>=?", (since,))),
        ("zápisků k filmům", _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='movie_opinion' AND ts>=?", (since,))),
        # HANS_SACHY_V1
        ("dohraných partií šachů s Koláčem", _q1(conn, "SELECT count(*) FROM diary "
            "WHERE event_type='sachy_partie' AND ts>=?", (since,))),
    ]
    return [(k, v) for k, v in d if v]


def _odezva(conn, since: float) -> tuple:
    """HANS_BEHAVIOUR_EVIDENCE_V3 — vrací (zvenčí, vnitřní dialog, vlastní výsledky).

    Dřív to byl JEDEN seznam pod nadpisem "tohle jsem si nenapsal sám",
    jenže dva z jeho řádků si Hans napsal sám (Koláč + vlastní ověření
    faktu) a jeden nebyl odezva vůbec (vlastní cíle). Severka to četla
    jako hlas okolí a postavila na tom návrh identity.
    ⛔ Koláč se NEVYHAZUJE — je to hlavní motor pohybu identity
    (2170 replik proti 986 lidským). Mění se štítek, ne vstup.
    """
    # `agent_action` nese výsledek v TITULKU za šipkou („… → accepted")
    def _agent(stav):
        return _q1(conn, "SELECT count(*) FROM diary WHERE event_type="
                   "'agent_action' AND title LIKE ? AND ts>=?",
                   ("%→ " + stav, since))
    # ⚠️ HANS_BEHAVIOUR_EVIDENCE_V2 (27.8.) — V1 SLEPILA `ignored` s `rejected`
    # a hlásila „přijal/odmítl 10 / 37", tedy poměr 4:1 v neprospěch Hanse.
    # Bylo to NEPRAVDIVÉ: `ignored` NENÍ odmítnutí. `hans_agent` ho zapisuje,
    # když uživatelova další zpráva není ano ani ne („nejednoznačné — návrh
    # zahoď a nech projít do běžného chatu"), tedy když mluvil o něčem jiném.
    # Skutečná bilance rozhodnutí je 10 ANO : 11 NE = vyrovnaná. Rozdíl je
    # zásadní: „skoro všechno mi zamítá" × „rozhodujeme se půl na půl".
    # Tenhle blok krmí rozhodnutí o IDENTITĚ, takže falešný signál tu váží víc
    # než kdekoli jinde. Drží se odděleně a s poctivými popisky.
    prijato, odmitnuto, minulo = (_agent("accepted"), _agent("rejected"),
                                  _agent("ignored"))
    # HANS_BEHAVIOUR_EVIDENCE_V3 — tri skupiny misto jednoho seznamu.
    ven, vnitr, vlastni = [], [], []

    # ── ZVENCI: rozhodl o tom clovek ───────────────────────────────────
    if prijato or odmitnuto:
        ven.append(("na mé návrhy pán řekl ano / ne", "%d / %d"
                    % (prijato, odmitnuto)))
    if minulo:
        ven.append(("mých návrhů přišlo ve chvíli, kdy pán řešil něco jiného",
                    str(minulo)))
    # HANS_FILM_VOLBA_V1 — návrhy filmů: rozhodl člověk (spustil / nespustil)
    try:
        nav = _q1(conn, "SELECT count(*) FROM diary WHERE event_type="
                  "'film_suggestion' AND ts>=?", (since,))
        pri = _q1(conn, "SELECT count(*) FROM diary WHERE event_type="
                  "'film_suggestion_accepted' AND ts>=?", (since,))
        if nav:
            ven.append(("mých návrhů filmu lidé přijali / celkem", "%d / %d"
                        % (pri, nav)))
    except Exception:
        pass
    # V3: JEN lesson_learned. fact_correction je G5K = vlastni overeni faktu,
    # ne oprava od uzivatele — patri do vnitrniho dialogu.
    n = _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='lesson_learned' AND ts>=?", (since,))
    if n:
        ven.append(("kolikrát mě pán opravil", str(n)))
    n = _q1(conn, "SELECT count(*) FROM deepen_proposals WHERE status IN "
            "('rejected','expired') AND ts>=?", (since,))
    if n:
        ven.append(("kolikrát pán zamítl, abych šel v tématu hlouběji", str(n)))
    # V3: merítko objemu — bez nej nejde poznat, ze lidskeho vstupu je malo.
    n = _q1(conn, "SELECT count(*) FROM diary WHERE event_type='human_chat' "
            "AND ts>=?", (since,))
    if n:
        ven.append(("rozhovorů s lidmi", str(n)))

    # ── VNITRNI DIALOG: vyrobil si to sam ──────────────────────────────
    n = _q1(conn, "SELECT count(*) FROM stance_history WHERE event='contradict' "
            "AND ts>=?", (since,))
    if n:
        # Rozpad je ODHAD podle hodiny: vecerni reflexe bezi 00-06, Kolacovy
        # debaty pres den. stance_history sloupec `source` nema — contradict()
        # ho dostava, ale zahazuje. Presny rozpad by chtel ALTER TABLE.
        _noc = _q1(conn, "SELECT count(*) FROM stance_history WHERE "
                   "event='contradict' AND ts>=? AND CAST(strftime('%H',ts,"
                   "'unixepoch','localtime') AS INT) BETWEEN 0 AND 6", (since,))
        # V3_FMT — jeden radek; podradky delaly dvojitou pomlcku ("-     z toho")
        vnitr.append(("kolikrát jsem v rozepři ustoupil ze svého postoje",
                      "%d (odhadem ~%d v debatě s Koláčem, ~%d ve večerní "
                      "reflexi)" % (n, n - _noc, _noc)))
    n = _q1(conn, "SELECT count(*) FROM diary WHERE "
            "event_type='fact_correction' AND ts>=?", (since,))
    if n:
        vnitr.append(("kolikrát jsem si sám ověřil a opravil fakt", str(n)))
    n = _q1(conn, "SELECT count(*) FROM diary WHERE event_type='teddy_dialog' "
            "AND ts>=?", (since,))
    if n:
        vnitr.append(("replik v dialogu s Koláčem", str(n)))

    # ── VLASTNI VYSLEDKY: nikdy to nebyla odezva okoli ─────────────────
    hotovo = _q1(conn, "SELECT count(*) FROM hans_goals WHERE status='completed' "
                 "AND opened_at>=?", (since,))
    vzdano = _q1(conn, "SELECT count(*) FROM hans_goals WHERE status='abandoned' "
                 "AND opened_at>=?", (since,))
    if hotovo or vzdano:
        vlastni.append(("cílů dotažených / vzdaných", "%d / %d"
                        % (hotovo, vzdano)))
    # HANS_UCI_KOLACE_V1 — Hans zkouší Koláče; soudí kód podle klíče
    try:
        from scripts.kolac_ucitel import bilance as _bil
        _db = conn.execute("PRAGMA database_list").fetchone()[2]
        b = _bil(_db, since)
        if b["zkouseno"]:
            vlastni.append(("kolikrát jsem zkoušel Koláče z toho, co umím (napoprvé věděl)",
                            "%d (%d)" % (b["zkouseno"], b["vedel"])))
        if b["opakovano"]:
            vlastni.append(("z mého vysvětlení si Koláč při opakování zapamatoval",
                            "%d z %d" % (b["zapamatoval"], b["opakovano"])))
    except Exception:
        pass
    # HANS_SACHY_V1 — výsledek počítá engine, ne já
    try:
        r = conn.execute("SELECT sum(vysledek='výhra'), sum(vysledek='remíza'), "
                         "sum(vysledek='prohra'), min(kolac_sila), max(kolac_sila) FROM "
                         "sachy_partie WHERE stav='konec' AND konec_ts>=?", (since,)).fetchone()
        if r and (r[0] or r[1] or r[2]):
            vlastni.append(("šachy s Koláčem výhry / remízy / prohry (výsledek počítá "
                            "šachový program)", "%d / %d / %d" % (r[0] or 0, r[1] or 0, r[2] or 0)))
    except Exception:
        pass
    return ven, vnitr, vlastni


def _pro_lidi(conn, since: float, config: dict) -> list:
    """HANS_BEHAVIOUR_PRO_LIDI_V1 (5. 11.) — co Hans udělal PRO LIDI v domácnosti.
    Blok činnosti počítal jen četbu a tvorbu → Severka viděla jen čtenáře.
    Čísla jsou malá (lidé se ptají málo), ale poctivá; testovací identity
    (`test_persons`) se nepočítají. Každý řádek jen když je nenulový."""
    out = []
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        import sqlite3 as _s
        c = _s.connect("file:%s?mode=ro" % os.path.join(_root, "data", "hans_letaky.db"),
                       uri=True, timeout=2.0)
        try:
            n = c.execute("SELECT count(*) FROM behy WHERE odeslano=1 AND ts>=?",
                          (since,)).fetchone()[0]
        finally:
            c.close()
        if n:
            out.append(("týdenních přehledů slev, které jsem sám poslal", str(n)))
    except Exception:
        pass
    try:
        st = json.load(open(os.path.join(_root, "data", "vylet_stav.json"), encoding="utf-8"))
        n = sum(1 for t in (st.get("odeslano_ts") or []) if float(t) >= since)
        if n:
            out.append(("návrhů víkendového výletu, které jsem sám poslal", str(n)))
    except Exception:
        pass
    test = [str(x) for x in ((config or {}).get("test_persons") or [])]
    ph = ",".join("?" * len(test)) or "''"
    n = _q1(conn, "SELECT count(*) FROM diary WHERE event_type='human_chat' AND ts>=? "
            "AND title NOT IN (%s) AND (note LIKE '%%právě podíval%%' "
            "OR note LIKE '%%co jsem teď našel%%')" % ph, (since, *test))
    if n:
        out.append(("otázek lidí, na které jsem odpověď dohledal", str(n)))
    for typ, pop in (("translate_done", "přeložených dokumentů na požádání"),
                     ("webshare_download", "filmů stažených na požádání"),
                     ("guard_alert", "upozornění z hlídání domu")):
        n = _q1(conn, "SELECT count(*) FROM diary WHERE event_type=? AND ts>=?",
                (typ, since))
        if n:
            out.append((pop, str(n)))
    return out


def block(config: dict, diary_db_path: str, window_days: int = None) -> str:
    """Sestaví blok pro Severku. Nikdy nevyhazuje výjimku; '' = není z čeho."""
    if window_days is None:
        window_days = int((config.get("severka", {}) or {}).get(
            "behaviour_window_days", WINDOW_DAYS))
    since = time.time() - window_days * 86400
    conn = None
    try:
        conn = _ro(diary_db_path)
        ins, cin = _insights(conn, since), _cinnost(conn, since)
        ven, vnitr, vlastni = _odezva(conn, since)   # HANS_BEHAVIOUR_EVIDENCE_V3
        pro = _pro_lidi(conn, since, config)          # HANS_BEHAVIOUR_PRO_LIDI_V1
    except Exception as e:
        _log.debug("behaviour block failed: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    if not (ins or cin or ven or vnitr or vlastni or pro):
        return ""
    p = []
    if cin:
        p.append("Čím jsem strávil posledních %d dní:" % window_days)
        p += ["- %s: %d" % (k, v) for k, v in cin]
    if pro:
        p.append("\nCo jsem udělal pro lidi v domácnosti za posledních %d dní:"
                 % window_days)
        p += ["- %s: %s" % (k, v) for k, v in pro]
    # V3_SEKCE — tri oddelene sekce s poctivymi popisky misto jedne.
    if ven:
        p.append("\nCo na mě doopravdy přišlo ZVENČÍ za posledních %d dní "
                 "(rozhodl o tom člověk, ne já):" % window_days)
        p += ["- %s: %s" % (k, v) for k, v in ven]
    if vnitr:
        p.append("\nCo vzešlo z mého VLASTNÍHO vnitřního dialogu za posledních "
                 "%d dní (vyrobil jsem si to sám — druhou myslí nebo večerní "
                 "reflexí, není to hlas okolí):" % window_days)
        p += ["- %s: %s" % (k, v) for k, v in vnitr]
    if vlastni:
        p.append("\nCo jsem sám dokázal za posledních %d dní:" % window_days)
        p += ["- %s: %s" % (k, v) for k, v in vlastni]
    if ins:
        p.append("\nCo jsem si sám všiml ve svých datech (vlastní rozbor):")
        p += ["- [%s] %s" % (l, t) for l, t in ins]
    return "\n".join(p)


if __name__ == "__main__":
    import json
    try:                                   # HANS_MAIN_CONFIG_IO_V1
        from scripts.config_io import load as _cfg_load
    except ImportError:                    # spusteno jako skript, root chybi
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
        from scripts.config_io import load as _cfg_load
    _c = _cfg_load()
    print(block(_c, _c.get("diary_db", "data/hans_diary.db")) or "(prázdné)")
