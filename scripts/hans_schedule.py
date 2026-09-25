"""HANS_SCHEDULE_V1 — Hansův vlastní deklarativní rozvrh rutin (jeden zdroj).

Motivace ([[hans-own-schedule-declarative]] + [[robustness-silent-failure-audit]]):
Hans má víc autonomních rutin (analytika, reflexe, studium, catchup, guard),
dnes rozeseté v `config.json` + `_PHASE_SCHEDULE` + systemd timerech. Tři nezávislé
zdroje = nikdo neví, jestli rutina naposledy proběhla. Doložený případ: studium
„Design" 8/12 stojí 14 dní, `HANS_STUDY_SEQUENTIAL_V1` ho správně vybírá, ale
session se nespouští — a nikdo si toho nevšiml.

Tento modul přidává KONTROLNÍ VRSTVU (razítko + audit), NE náhradu za timery.
Každý autonomní tick zavolá `mark(name)`; `hans_health` pak ověří freshness.

TABULKA `hans_schedule`:
    name              — logický název rutiny (PK), např. 'nightly_analytics'
    kind              — 'periodic' | 'daily' | 'phase' (info, pro UI)
    period_s          — u periodic: cílová perioda (informativní)
    hour              — u daily/phase: cílová hodina (0-23)
    expected_gap_s    — MAX povolený gap; sebe-audit hlásí, když jde přes
    last_run_ts       — poslední úspěšný tick (0.0 = nikdy)
    last_run_ok       — 1 = OK, 0 = poslední tick byl SKIP (brain_down apod.)
    last_skip_reason  — když ok=0, proč (např. 'brain_down', 'game_mode')
    enabled           — 1 = zapnuto; 0 = vypnuto (audit ignoruje)
    note              — lidský popis (co ta rutina dělá)
    updated_ts        — kdy se ROW naposledy měnil (jakkoli)

MVP scope: READ-ONLY pro Hanse. NL editace („přesuň analytiku na 1:00")
přijde v kroku 4 s POTVRZENÍM + validací.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

# Seed rutin — pro každou: (name, kind, period_s, hour, expected_gap_s, note).
# expected_gap_s = 2× nominální period + malý buffer, ať drobný jitter nehlásí.
# `hour` u periodic = None; `period_s` u daily = None.
_SEED = [
    # ── HANS_SCHEDULE_NIGHT_STEPS_V1 (20.8.) — noční kroky, které se dosud
    # nehlásily nikam. Bez nich `_run_night_tasks` (602 ř., 17 gated kroků)
    # mlčí o tom, co reálně proběhlo, a „přestalo se to spouštět" se pozná
    # jen tím, že něco chybí v deníku.
    # ⚠️ PRAHY JSOU ZÁMĚRNĚ VOLNÉ (14 dní). Tohle kolo je MĚŘENÍ, ne audit:
    # nejdřív ať se pár nocí nasbírá, jak často kroky opravdu běží, a teprve
    # z těch dat se prahy utáhnou. Opačné pořadí už jednou vyrobilo falešné
    # poplachy (HANS_SCHEDULE_STUDY_GAP_V1: práh 4 h stál na mylném modelu).
    ("writing_session", "daily", None, None, 14 * 24 * 3600,
     "Autorská session (dílo na pokračování)"),
    ("synthesis_session", "daily", None, None, 14 * 24 * 3600,
     "Synteze nápadů (#2) z nesouvisejících semínek"),
    ("selfcritique", "daily", None, None, 14 * 24 * 3600,
     "Sebekritika (#6) z vlastních replik"),
    ("immune_check", "daily", None, None, 14 * 24 * 3600,
     "Imunitní kontrola paměti"),
    ("relationship_reflection", "daily", None, None, 14 * 24 * 3600,
     "Reflexe vztahových karet"),
    ("creation_reflection", "daily", None, None, 14 * 24 * 3600,
     "Reflexe vlastní tvorby"),

    # IMPORTANCE_SCHEDULE_V1 (29.8.) — skorovani dulezitosti. PRIDANO POZDE:
    # rutina stala od 26.6. do 29.8.2026 (18 017 neoskorovanych epizod) a nikdo
    # si toho nevsiml prave proto, ze tady nebyla — sebe-audit hlasil "v poradku".
    # Zivi autobiografickou vrstvu (self-defining memories), takze jeji mlceni
    # se navenek neprojevi nicim, co by slo videt.
    ("importance_scoring", "daily", None, None, 30 * 3600,
     "Skorovani dulezitosti epizod (autobiograficka vrstva)"),

    # HANS_FACTS_NIGHTLY_V1 (31.8.) — doplnění fakt z Wikidat pro nové entity.
    # PŘIDÁNO POZDĚ ze stejného důvodu jako `importance_scoring`: modul běžel
    # jednou ručně 26.8. a pak stál, protože ho nikdo nevolal a rozvrh o něm
    # nevěděl. Ticho se navenek projeví jen tím, že se obraz namaluje bez
    # slohu a datace — tedy ničím, co by šlo vidět.
    ("facts_backfill", "daily", None, None, 50 * 3600,
     "Doplnění fakt z Wikidat pro nové entity (0 % LLM)"),

    # Noční analytika (deepseek reasoning tier přes noc, 3:00).
    ("nightly_analytics", "daily", None, 3, 30 * 3600,
     "Noční analytika (deepseek reasoning tier): syntéza, sebekritika, stance"),
    # Ranní reflexe (souhrn co bylo, kam dál) — na začátku fáze morning ~6:00.
    ("morning_reflection", "daily", None, 6, 30 * 3600,
     "Ranní reflexe: shrnutí noci, plán dne"),
    # Večerní reflexe dne — 1× za noc ve 22:00+ (HANS_REFLECTION_BRAIN_UP_CATCHUP_V1).
    # Doloženo 7.8.: vypadla 2 dny po sobě a NIKDO to neohlásil, protože tu
    # rutina chyběla. 30h gap = jeden vynechaný den se ozve hned ráno.
    ("evening_reflection", "daily", None, 22, 30 * 3600,
     "Večerní reflexe dne (shrnutí dne, postoje, tendence)"),
    # HANS_SCHEDULE_STUDY_GAP_V1 (18.8.) — původní práh 4 h stál na MYLNÉM modelu
    # („tick á 30 min"). Studium se ve skutečnosti spouští jen v NOČNÍM OKNĚ
    # (`hans_routine._in_night_window`) nebo na brain_up catchup 1×/den, takže
    # přes den nemá jak uspět a 4 h by hlásily poplach každé odpoledne.
    # Naměřeno z deníku (21 dní, `study_note`): úspěch přijde jednou za pár dní,
    # mezi 7.–14.8. byla šestidenní mezera. 48 h = dvě zmeškané noci → chytí
    # reálné zaseknutí (i tu šestidenní mezeru), ale nekřičí na běžný rytmus.
    # HANS_STUDY_UNIFY_V1 — `period_s` u studia bylo 1800 (dědictví mylného
    # modelu „tick á 30 min"). Studium NEMÁ periodu, běží v nočním okně →
    # None, ať tabulka nepopisuje neexistující rytmus.
    ("study_tick", "periodic", None, None, 48 * 3600,
     "Studijní tick: postup v aktivním study_program"),
    # Zvědavost / čtení — periodic idle.
    # HANS_SCHEDULE_CURIOSITY_GAP_V1 (23. 9.) — 4 h -> 12 h. `mark()` pada jen
    # pri USPESNEM cteni; spoustec bez clanku (epizoda serialu, herni mod)
    # nezapise nic, takze 4 h merily rytmus spoustecu, ne zdravi ctecky.
    # Zmereno za 10 dni: 19 ze 120 mezer mezi ctenimi > 4 h, nejdelsi denni
    # 11,1 h, nad 12 h zadna. Obe zpravy hlidace z 22.-23. 9. byly tenhle sum.
    # Prazdne cteni se ZAMERNE nepocita jako beh — rozbita ctecka by pak
    # vypadala vecne svezi (viz HANS_SCHEDULE_LAST_OK_V1).
    ("curiosity_tick", "periodic", 30 * 60, None, 12 * 3600,
     "Zvědavý tick: čtení / prozkoumávání zájmů"),
    # Proton kalendář sync — každých 30 min (config.calendar.sync_interval_min).
    ("calendar_sync", "periodic", 30 * 60, None, 2 * 3600,
     "Sync Proton ICS kalendáře (nadcházející události)"),
    # Deferred catchup — po brain_up + po každém úspěšném čtení.
    # expected_gap NEmá tvrdý strop (může chybět celý den, když je málo pending);
    # nastaveno 25h → hlásí až po celém dni ticha.
    ("catchup_drain", "periodic", None, None, 25 * 3600,
     "Deferred pending catchup (po brain_up dojede backlog)"),
    # HANS_ANOMALY_CADENCE_BY_RUN_V1 (22. 9.) — detektor odchylek potreboval
    # misto, kam zapsat, ze BEZEL. Jeho vlastni stopa (`anomaly_note`) vznika
    # jen kdyz neco najde, takze v tichem tydnu nebylo podle ceho merit
    # kadenci. Proto `periodic`, NE `derived`.
    # Prah je zamerne volny: kadence je 7 dni, hlasi se az po 9 — dvakrat
    # volnejsi nez rytmus, at z toho nevznikne dalsi sum v hlidaci rozvrhu
    # (22. 9. zmereno, ze pet z sesti jeho zprav byl sum).
    # HANS_PREVENTION_V1 (25. 9.) — hodinový sběr souhrnů pro prevenci; když
    # tiše umře, trendy přestanou přibývat a nikdo si toho nevšimne.
    ("prevence_sber", "periodic", 3600, None, 3 * 3600,
     "Sběr denních souhrnů pro prevenci (chyby, samoopravy, disky, paměť)"),
    ("anomaly_run", "periodic", None, None, 9 * 24 * 3600,
     "Tydenni detektor odchylek v chovani (HANS_ANOMALY_V1)"),

    # ── HANS_SCHEDULE_DERIVED_V1 (18. 9.) — rutiny hlídané PODLE DAT ──────
    # Nepotřebují `mark()` v kódu: čerstvost se odvodí z `MAX(ts)` nad stopou,
    # kterou mechanismus v databázi stejně zanechává (viz `_DERIVED` níž).
    # Prahy = 2x historicke p95 mezery, min. 3 dny; v zavorce namerena p95.
    ("stance_contradict", "derived", None, None, 6 * 24 * 3600,
     "Oslabeni postoje (Kolac/reflexe zpochybnily nazor) [p95 2,8 d]"),
    ("kolac_memory", "derived", None, None, 3 * 24 * 3600,
     "Kolacova vlastni pamet pozic z dialogu [p95 0,6 d]"),
    ("stance_new", "derived", None, None, 16 * 24 * 3600,
     "Vznik noveho postoje [p95 8 d]"),
    ("lesson_learned", "derived", None, None, 12 * 24 * 3600,
     "Korekce / ponauceni [p95 5,8 d]"),
    ("artwork", "derived", None, None, 3 * 24 * 3600,
     "Namalovany obraz [p95 1 d]"),
    ("dream", "derived", None, None, 3 * 24 * 3600,
     "Sen [p95 1 d]"),
    ("proactive", "derived", None, None, 35 * 24 * 3600,
     "Proaktivni osloveni [p95 17,6 d]"),
    ("study_note", "derived", None, None, 8 * 24 * 3600,
     "Studijni poznamka [p95 4 d]"),
    ("fact_correction", "derived", None, None, 32 * 24 * 3600,
     "Overeni faktu (G5K) [p95 16 d]"),
    ("night_summary", "derived", None, None, 3 * 24 * 3600,
     "Nocni souhrn [p95 1 d]"),
    ("book_read", "derived", None, None, 3 * 24 * 3600,
     "Prectena kapitola [p95 0,6 d]"),
    ("writing_section", "derived", None, None, 4 * 24 * 3600,
     "Kapitola vlastniho dila [p95 2,1 d]"),
    ("agent_action", "derived", None, None, 4 * 24 * 3600,
     "Provedena agentni akce [p95 2 d]"),
    ("teddy_dialog", "derived", None, None, 3 * 24 * 3600,
     "Dialog s Kolacem [p95 0,2 d]"),
]

# HANS_SCHEDULE_DERIVED_V1 — kde ma kazda odvozena rutina svou stopu.
# VYHRADNE cteci SELECT MAX(...) bez parametru zvenci (zadny vstup uzivatele).
# Tabulky lezi v teze DB jako `hans_schedule` (data/hans_diary.db).
_DERIVED = {
    "stance_contradict":
        "SELECT MAX(ts) FROM stance_history WHERE event='contradict'",
    "kolac_memory":      "SELECT MAX(ts) FROM kolac_memory",
    "stance_new":        "SELECT MAX(first_seen) FROM stances",
    "lesson_learned":    "SELECT MAX(ts) FROM diary WHERE event_type='lesson_learned'",
    "artwork":           "SELECT MAX(ts) FROM diary WHERE event_type='artwork'",
    "dream":             "SELECT MAX(ts) FROM diary WHERE event_type='dream'",
    "proactive":         "SELECT MAX(ts) FROM diary WHERE event_type='proactive'",
    "study_note":        "SELECT MAX(ts) FROM diary WHERE event_type='study_note'",
    "fact_correction":   "SELECT MAX(ts) FROM diary WHERE event_type='fact_correction'",
    "night_summary":     "SELECT MAX(ts) FROM diary WHERE event_type='night_summary'",
    "book_read":         "SELECT MAX(ts) FROM diary WHERE event_type='book_read'",
    "writing_section":   "SELECT MAX(ts) FROM diary WHERE event_type='writing_section'",
    "agent_action":      "SELECT MAX(ts) FROM diary WHERE event_type='agent_action'",
    "teddy_dialog":      "SELECT MAX(ts) FROM diary WHERE event_type='teddy_dialog'",
}


def _fresh_since(r: dict) -> float:
    """Od kdy měřit čerstvost: od posledního ÚSPĚCHU. Když žádný neznáme
    (nová / vždy skipující rutina), měř od INSTALACE — `installed_ts`, protože
    `updated_ts` obnovuje i skip, takže by se díra vrátila záložní větví
    (HANS_SCHEDULE_INSTALLED_TS_V1)."""
    return ((r.get("last_ok_ts") or 0)
            or (r.get("installed_ts") or 0)
            or r["updated_ts"])


class ScheduleStore:
    def __init__(self, db_path: str):
        self._path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._path) as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS hans_schedule (
                    name             TEXT PRIMARY KEY,
                    kind             TEXT NOT NULL DEFAULT 'periodic',
                    period_s         REAL,
                    hour             INTEGER,
                    expected_gap_s   REAL NOT NULL,
                    last_run_ts      REAL NOT NULL DEFAULT 0,
                    last_ok_ts       REAL NOT NULL DEFAULT 0,
                    installed_ts     REAL NOT NULL DEFAULT 0,
                    last_run_ok      INTEGER NOT NULL DEFAULT 1,
                    last_skip_reason TEXT NOT NULL DEFAULT '',
                    enabled          INTEGER NOT NULL DEFAULT 1,
                    note             TEXT NOT NULL DEFAULT '',
                    updated_ts       REAL NOT NULL DEFAULT 0
                )
            """)
            # HANS_SCHEDULE_LAST_OK_V1 (18.8.) — migrace pro DB založené dřív.
            # Backfill: kde poslední běh DOPADL dobře, je poslední úspěch = ten
            # běh; kde skončil skipem, poslední úspěch NEZNÁME → 0 = „nikdy",
            # což `is_stale` měří od instalace (updated_ts). Radši ať to jednou
            # zbytečně vykřikne, než aby to dál mlčelo.
            _cols = {r[1] for r in db.execute(
                "PRAGMA table_info(hans_schedule)").fetchall()}
            # HANS_SCHEDULE_INSTALLED_TS_V1 (18.8.) — `updated_ts` se NEDÁ použít
            # jako „od kdy měřit", protože ho `mark()` obnovuje i při SKIPU.
            # Rutina, která nikdy neuspěla (dnes `study_tick`), by tak i po
            # opravě zůstala věčně svěží — díra by přežila v záložní větvi.
            # `installed_ts` se zapíše jednou a už se nikdy nemění.
            if _cols and "installed_ts" not in _cols:
                db.execute("ALTER TABLE hans_schedule "
                           "ADD COLUMN installed_ts REAL NOT NULL DEFAULT 0")
                # Neznáme skutečný čas instalace (updated_ts je přepsaný) →
                # start hodin = TEĎ. Konzervativní: první poplach přijde nejdřív
                # za expected_gap, ne zpětně.
                db.execute("UPDATE hans_schedule SET installed_ts=? "
                           "WHERE installed_ts=0", (time.time(),))
            if _cols and "last_ok_ts" not in _cols:
                db.execute("ALTER TABLE hans_schedule "
                           "ADD COLUMN last_ok_ts REAL NOT NULL DEFAULT 0")
                db.execute("UPDATE hans_schedule SET last_ok_ts=last_run_ts "
                           "WHERE last_run_ok=1")
                _log.info("hans_schedule: migrace last_ok_ts hotová "
                          "(čerstvost se nově měří od posledního ÚSPĚCHU)")
            # HANS_SCHEDULE_STUDY_GAP_V1 — seed existující řádek nepřepíše
            # (INSERT OR IGNORE), takže starý práh 4 h se musí opravit adresně.
            # Jen tahle jedna rutina a jen z původní hodnoty → ruční ladění
            # (kdyby někdo práh měnil) zůstane nedotčené.
            db.execute("UPDATE hans_schedule SET expected_gap_s=? "
                       "WHERE name='study_tick' AND expected_gap_s=?",
                       (48 * 3600, 4 * 3600))
            # HANS_SCHEDULE_CURIOSITY_GAP_V1 — totez adresne pro curiosity_tick
            # (seed je INSERT OR IGNORE); jen z puvodni hodnoty 4 h.
            db.execute("UPDATE hans_schedule SET expected_gap_s=? "
                       "WHERE name='curiosity_tick' AND expected_gap_s=?",
                       (12 * 3600, 4 * 3600))
            # HANS_STUDY_UNIFY_PERIOD_V1 (18.8.) — totéž pro `period_s`: seed je
            # INSERT OR IGNORE, takže existující řádek si drží 1800 = popis
            # třicetiminutového rytmu, který studium nemá (běží v nočním okně
            # nebo na brain_up catchup). Hodnotu NIKDO nečte (je informativní,
            # viz ř. 16; `is_stale` jede z expected_gap_s) → čistě kosmetika,
            # ale ať tabulka nepopisuje neexistující rytmus.
            # Podmínka na 1800 chrání případné ruční ladění.
            db.execute("UPDATE hans_schedule SET period_s=NULL "
                       "WHERE name='study_tick' AND period_s=?", (30 * 60,))
            # Seed idempotentně (INSERT OR IGNORE — nepřepisuje ruční změny).
            now = time.time()
            for name, kind, ps, hr, gap, note in _SEED:
                db.execute("""
                    INSERT OR IGNORE INTO hans_schedule
                    (name, kind, period_s, hour, expected_gap_s,
                     last_run_ts, last_ok_ts, installed_ts, last_run_ok,
                     last_skip_reason, enabled, note, updated_ts)
                    VALUES (?,?,?,?,?,0,0,?,1,'',1,?,?)
                """, (name, kind, ps, hr, gap, now, note, now))
            db.commit()

    # ── zápis (odvozené rutiny) ──────────────────────────────────────────────
    def refresh_derived(self) -> int:
        """HANS_SCHEDULE_DERIVED_V1 — dopočítej čerstvost rutin, které se
        nehlásí samy: `last_ok_ts` = `MAX(ts)` nad jejich stopou v datech.

        Vrací počet obnovených rutin. Nikdy nevyhazuje výjimku a nikdy
        neposouvá čas ZPĚT (`MAX(sloupec, ?)`), aby chybějící/prázdná
        tabulka nezpůsobila falešný poplach.

        ⚠️ Čerstvost se plní do `last_ok_ts`, protože `_fresh_since` měří od
        posledního ÚSPĚCHU — do `last_run_ts` by to rutinu nechalo vypadat
        jako nikdy neúspěšnou (HANS_SCHEDULE_LAST_OK_V1).
        """
        obnoveno = 0
        try:
            with sqlite3.connect(self._path, timeout=5.0) as db:
                for name, sql in _DERIVED.items():
                    try:
                        row = db.execute(sql).fetchone()
                    except Exception as e:
                        # Chybějící tabulka není důvod shodit celý audit.
                        _log.debug("refresh_derived(%s): %s", name, e)
                        continue
                    ts = float(row[0]) if row and row[0] else 0.0
                    if ts <= 0:
                        continue
                    db.execute(
                        "UPDATE hans_schedule SET "
                        "last_run_ts=MAX(last_run_ts, ?), "
                        "last_ok_ts=MAX(last_ok_ts, ?), updated_ts=? "
                        "WHERE name=? AND kind='derived'",
                        (ts, ts, time.time(), name))
                    obnoveno += 1
                db.commit()
        except Exception as e:
            _log.warning("refresh_derived selhalo: %s", e)
        return obnoveno

    # ── zápis (autonomní subsystémy) ─────────────────────────────────────────
    def mark(self, name: str, ok: bool = True,
             skip_reason: str = "") -> None:
        """Zapiš, že rutina teď proběhla.
        ok=False + skip_reason='brain_down' = rutina si všimla důvodu skip
        a NEbude to počítat jako úspěšný běh (last_run_ok=0).
        POKUD subsystém běžel jen částečně / degradovaně, dej ok=True s
        reason='' — audit tě nechá být, dokud freshness drží.

        HANS_SCHEDULE_LAST_OK_V1 (18.8.): `last_ok_ts` se posouvá JEN při ok=True.
        Dřív obnovoval hodiny čerstvosti i skip, takže rutina, která se každou
        minutu hlásí jako `deferred`, vypadala věčně svěží — doloženo 18.8.:
        studium 5 h nic nenastudovalo, `stale_list` byl prázdný a `/zdravi`
        mlčelo. Sebe-audit chytal „přestalo běhat", ne „běží a pokaždé selže",
        což je ta ČASTĚJŠÍ porucha ([[robustness-silent-failure-audit]]).
        """
        now = time.time()
        try:
            with sqlite3.connect(self._path, timeout=3.0) as db:
                cur = db.execute(
                    "UPDATE hans_schedule SET last_run_ts=?, last_run_ok=?, "
                    "last_skip_reason=?, updated_ts=?, "
                    "last_ok_ts=CASE WHEN ? THEN ? ELSE last_ok_ts END "
                    "WHERE name=?",
                    (now, 1 if ok else 0, skip_reason[:120], now,
                     1 if ok else 0, now, name))
                if cur.rowcount == 0:
                    _log.debug("hans_schedule.mark: neznámá rutina '%s' "
                               "(seed ji neobsahuje) — ignoruji", name)
                db.commit()
        except Exception as e:
            _log.warning("hans_schedule.mark(%s) selhalo: %s", name, e)

    # ── čtení (dashboard / audit / /zdravi) ──────────────────────────────────
    def get(self, name: str) -> Optional[dict]:
        try:
            with sqlite3.connect(
                    "file:%s?mode=ro" % self._path, uri=True, timeout=3.0) as db:
                db.row_factory = sqlite3.Row
                r = db.execute(
                    "SELECT * FROM hans_schedule WHERE name=?", (name,)
                ).fetchone()
                return dict(r) if r else None
        except Exception:
            return None

    def all(self) -> list[dict]:
        try:
            with sqlite3.connect(
                    "file:%s?mode=ro" % self._path, uri=True, timeout=3.0) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM hans_schedule ORDER BY name"
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception:
            return []

    def is_stale(self, name: str, now: Optional[float] = None) -> bool:
        """True = rutina si zaslouží pozornost. Nikdy neběžela → True
        až po expected_gap_s od NAINSTALOVÁNÍ (updated_ts)."""
        r = self.get(name)
        if not r or not r["enabled"]:
            return False
        n = now if now is not None else time.time()
        last = _fresh_since(r)
        return (n - last) > r["expected_gap_s"]

    def stale_list(self, now: Optional[float] = None) -> list[dict]:
        """Vrátí seznam ZASTARALÝCH rutin s late_s (kolik po termínu).
        Používá hans_health / /zdravi / behaviorální sebe-audit."""
        n = now if now is not None else time.time()
        out = []
        for r in self.all():
            if not r["enabled"]:
                continue
            last = _fresh_since(r)
            late = n - last - r["expected_gap_s"]
            if late > 0:
                out.append({
                    "name": r["name"],
                    "late_s": late,
                    "last_run_ts": r["last_run_ts"],
                    "last_ok_ts": r.get("last_ok_ts", 0),
                    # Rozliš „vůbec se nespouští" od „spouští se a pokaždé
                    # selže" — jiná diagnóza, jiná oprava.
                    "tried_recently": bool(
                        r["last_run_ts"]
                        and (n - r["last_run_ts"]) < r["expected_gap_s"]),
                    "expected_gap_s": r["expected_gap_s"],
                    "last_skip_reason": r["last_skip_reason"],
                    "note": r["note"],
                })
        out.sort(key=lambda x: -x["late_s"])
        return out

    def stale_report(self, now: Optional[float] = None) -> str:
        """Human-readable — pro /zdravi kartu."""
        st = self.stale_list(now)
        if not st:
            return "Rozvrh: všechny rutiny běží podle plánu."
        lines = []
        for x in st:
            hrs = x["late_s"] / 3600
            reason = f" (posl. skip: {x['last_skip_reason']})" \
                if x["last_skip_reason"] else ""
            # HANS_SCHEDULE_LAST_OK_V1 — „běží, ale pokaždé selže" je jiná
            # porucha než „nespouští se"; hlášení to musí říct rovnou.
            kind = " — spouští se, ale nedaří se" if x.get("tried_recently") \
                else ""
            lines.append(f"  • {x['name']} — {hrs:.1f}h bez úspěchu"
                         f" (max gap {x['expected_gap_s']/3600:.1f}h)"
                         f"{kind}{reason}")
        return "Rozvrh — zaostávající rutiny:\n" + "\n".join(lines)


# ── Module-level shortcut ────────────────────────────────────────────────────
# Instrumentaci autonomních ticků chceme mít jednoduchou (JEDEN řádek per
# místo), bez rozvláčného passování store instancí. Singleton store proti
# defaultní DB path (`data/hans_diary.db`); lazy init, thread-safe.

import os as _os
import threading as _th

_SINGLETON: Optional[ScheduleStore] = None
_SINGLETON_LOCK = _th.Lock()
_DEFAULT_DB = _os.path.join(_os.path.dirname(_os.path.dirname(
    _os.path.abspath(__file__))), "data", "hans_diary.db")


def _default_store() -> ScheduleStore:
    global _SINGLETON
    if _SINGLETON is None:
        with _SINGLETON_LOCK:
            if _SINGLETON is None:
                _SINGLETON = ScheduleStore(_DEFAULT_DB)
    return _SINGLETON


def mark(name: str, ok: bool = True, skip_reason: str = "") -> None:
    """Zápis „rutina teď proběhla" proti defaultní DB. Bezpečné — tiché při chybě."""
    try:
        _default_store().mark(name, ok=ok, skip_reason=skip_reason)
    except Exception as _e:
        _log.debug("hans_schedule.mark shortcut failed: %s", _e)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    st = ScheduleStore("data/hans_diary.db")
    if len(sys.argv) > 1 and sys.argv[1] == "mark":
        st.mark(sys.argv[2], ok=True)
        print("marked:", sys.argv[2])
    print(st.stale_report())
    print()
    for r in st.all():
        print(f"  {r['name']:24s} last={r['last_run_ts']:.0f} "
              f"ok={r['last_run_ok']} gap={r['expected_gap_s']/3600:.1f}h "
              f"enabled={r['enabled']}")
