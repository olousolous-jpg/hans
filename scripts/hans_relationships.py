"""
Vztahové karty — Hansova paměť o lidech kolem něj.

Každá enrollnutá osoba (klíč v known_faces.pkl) má kartu:
  - kdo to je (role, rodinné vazby)
  - kdy ji Hans naposled viděl, kolikrát celkem
  - krátká charakteristika (Hansův osobní pohled, updatovaná
    týdenní reflexí přes hans-czech:latest)

Karty se synchronizují do RAGu (kolekce hans_identita) — 3 samostatné
dokumenty, re-upload jen při změně charakteristiky.

Použití:
    rels = Relationships(config)
    rels.seed_if_empty()                  # první spuštění
    rels.record_sighting("alice")         # při rozpoznání tváře
    card = rels.get("bob")                # čtení karty
    rels.update_characterization("carol", "Carol dnes...")
"""

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

_log = logging.getLogger("hans_relationships")

# Seed — kdo bydlí v domě (role + rodinné vazby)
# Klíče musí odpovídat klíčům v known_faces.pkl.
# PORTABILITY: seed vztahových karet jde z config.json `relationship_seed`
# (gitignored — žádná reálná jména/rodinné vazby v repu). Prázdný default =
# pro čistou instalaci se nic nenaseeduje (karty vzniknou enrollmentem/během
# provozu). Aplikuje se jen na PRÁZDNOU tabulku (seed_if_empty).
_SEED: dict = {}


@dataclass
class RelationshipCard:
    person_id: str
    display_name: str
    role: str
    family_links: dict
    first_seen_ts: Optional[float]
    last_seen_ts: Optional[float]
    sightings_count: int
    characterization: str
    updated_at: float
    last_reflection_at: Optional[float] = None  # REL_REFLECTION_TS_V1
    deactivated_at: Optional[float] = None  # NULL = aktivní


class Relationships:
    """CRUD nad tabulkou `relationships` v hans_diary.db."""

    def __init__(self, config: dict):
        cfg = config.get("relationships", {})
        self._enabled = cfg.get("enabled", True)
        self._diary_path = config.get("diary_db", "data/hans_diary.db")
        # PORTABILITY: seed z configu (gitignored), fallback prázdný _SEED
        self._seed = config.get("relationship_seed", {}) or _SEED
        # Throttle: max jeden sighting bump na osobu za N sekund
        self._sighting_throttle_s = float(cfg.get("sighting_throttle_s", 30.0))
        self._last_bump = {}  # person_id -> ts
        self._lock = threading.Lock()

        self._ensure_schema()
        _log.info("Relationships ready (enabled=%s, throttle=%.0fs)",
                  self._enabled, self._sighting_throttle_s)

    # ---------- schema ----------

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._diary_path)

    def _ensure_schema(self) -> None:
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS relationships (
                    person_id        TEXT PRIMARY KEY,
                    display_name     TEXT NOT NULL,
                    role             TEXT,
                    family_links     TEXT,          -- JSON
                    first_seen_ts    REAL,
                    last_seen_ts     REAL,
                    sightings_count  INTEGER DEFAULT 0,
                    characterization TEXT DEFAULT '',
                    updated_at       REAL,
                    deactivated_at   REAL           -- NULL = aktivní
                )
            """)
            # REL_REFLECTION_TS_V1 — samostatny timestamp reflexe (idempotentni)
            try:
                db.execute("ALTER TABLE relationships ADD COLUMN last_reflection_at REAL")
            except sqlite3.OperationalError:
                pass  # sloupec uz existuje
            # Migrace pro existující DB: přidat deactivated_at sloupec
            cols = [r[1] for r in db.execute(
                "PRAGMA table_info(relationships)").fetchall()]
            if "deactivated_at" not in cols:
                db.execute("ALTER TABLE relationships "
                           "ADD COLUMN deactivated_at REAL")
                _log.info("Schema migration: added deactivated_at column")
            db.commit()

    # ---------- seed ----------

    def seed_if_empty(self) -> int:
        """Naplní tabulku ze seedu (config), pokud je prázdná. Vrací počet."""
        with self._connect() as db:
            cur = db.execute("SELECT COUNT(*) FROM relationships")
            if cur.fetchone()[0] > 0:
                return 0

            now = time.time()
            inserted = 0
            for pid, data in self._seed.items():
                db.execute("""
                    INSERT INTO relationships
                        (person_id, display_name, role, family_links,
                         first_seen_ts, last_seen_ts, sightings_count,
                         characterization, updated_at)
                    VALUES (?, ?, ?, ?, NULL, NULL, 0, '', ?)
                """, (
                    pid,
                    data["display_name"],
                    data["role"],
                    json.dumps(data["family_links"], ensure_ascii=False),
                    now,
                ))
                inserted += 1
            db.commit()
            _log.info("Seed: vložil jsem %d vztahových karet", inserted)
            return inserted

    # ---------- read ----------

    def get(self, person_id: str,
            include_inactive: bool = False) -> Optional[RelationshipCard]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            if include_inactive:
                row = db.execute(
                    "SELECT * FROM relationships WHERE person_id=?",
                    (person_id,)
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT * FROM relationships "
                    "WHERE person_id=? AND deactivated_at IS NULL",
                    (person_id,)
                ).fetchone()
            if not row:
                return None
            return self._row_to_card(row)

    def all_cards(self, include_inactive: bool = False) -> list:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            if include_inactive:
                rows = db.execute(
                    "SELECT * FROM relationships ORDER BY person_id"
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM relationships "
                    "WHERE deactivated_at IS NULL ORDER BY person_id"
                ).fetchall()
            return [self._row_to_card(r) for r in rows]

    @staticmethod
    def _row_to_card(row) -> RelationshipCard:
        # Safe access: existing DB before migration nemá deactivated_at
        try:
            deact = row["deactivated_at"]
        except (KeyError, IndexError):
            deact = None
        return RelationshipCard(
            person_id=row["person_id"],
            display_name=row["display_name"],
            role=row["role"] or "",
            family_links=json.loads(row["family_links"] or "{}"),
            first_seen_ts=row["first_seen_ts"],
            last_seen_ts=row["last_seen_ts"],
            sightings_count=row["sightings_count"] or 0,
            characterization=row["characterization"] or "",
            updated_at=row["updated_at"] or 0.0,
            last_reflection_at=row["last_reflection_at"],  # REL_REFLECTION_TS_V1
            deactivated_at=deact,
        )

    # ---------- write ----------

    def record_sighting(self, person_id: str) -> bool:
        """Zaznamenej, že Hans právě někoho viděl.

        Vrací True, pokud se opravdu zapsalo (po throttle).
        """
        if not self._enabled:
            return False

        now = time.time()
        with self._lock:
            last = self._last_bump.get(person_id, 0.0)
            if now - last < self._sighting_throttle_s:
                return False
            self._last_bump[person_id] = now

        with self._connect() as db:
            cur = db.execute(
                "SELECT first_seen_ts, sightings_count FROM relationships "
                "WHERE person_id=?", (person_id,)
            ).fetchone()
            if cur is None:
                _log.warning("record_sighting: neznámá osoba %r — přeskakuju",
                             person_id)
                return False
            first_ts, count = cur
            new_first = first_ts if first_ts else now
            db.execute("""
                UPDATE relationships
                SET first_seen_ts=?, last_seen_ts=?,
                    sightings_count=?, updated_at=?
                WHERE person_id=?
            """, (new_first, now, (count or 0) + 1, now, person_id))
            db.commit()
        return True

    def update_characterization(self, person_id: str, text: str) -> bool:
        """Aktualizuj Hansovu charakteristiku osoby (volá týdenní reflexe)."""
        if not self._enabled:
            return False
        now = time.time()
        with self._connect() as db:
            cur = db.execute(
                "UPDATE relationships SET characterization=?, updated_at=?, "
                "last_reflection_at=? WHERE person_id=?",  # REL_REFLECTION_TS_V1
                (text, now, now, person_id)
            )
            db.commit()
            if cur.rowcount == 0:
                _log.warning("update_characterization: neznámá osoba %r",
                             person_id)
                return False
        _log.info("Charakteristika '%s' aktualizována (%d znaků)",
                  person_id, len(text))
        return True

    # SOFT_DELETE_PATCH

    def deactivate(self, person_id: str) -> bool:
        """Soft-delete: označí kartu jako neaktivní. Data se neztratí.

        Vrací True pokud karta existovala a byla úspěšně deaktivována,
        False pokud osoba neexistuje nebo už byla deaktivovaná.
        """
        now = time.time()
        with self._connect() as db:
            cur = db.execute(
                "UPDATE relationships SET deactivated_at=?, updated_at=? "
                "WHERE person_id=? AND deactivated_at IS NULL",
                (now, now, person_id)
            )
            db.commit()
            if cur.rowcount == 0:
                _log.warning("deactivate: %r neexistuje nebo už deaktivovaná",
                             person_id)
                return False
        _log.info("Karta '%s' deaktivována", person_id)
        return True

    def reactivate(self, person_id: str) -> bool:
        """Vrátit kartu ze soft-delete. Vrací True pokud existovala
        a byla reaktivována, False pokud osoba neexistuje nebo už byla
        aktivní."""
        now = time.time()
        with self._connect() as db:
            cur = db.execute(
                "UPDATE relationships SET deactivated_at=NULL, updated_at=? "
                "WHERE person_id=? AND deactivated_at IS NOT NULL",
                (now, person_id)
            )
            db.commit()
            if cur.rowcount == 0:
                return False
        _log.info("Karta '%s' reaktivována", person_id)
        return True

    def seed_one(self, person_id: str, display_name: str,
                 role: str = "", family_links: Optional[dict] = None) -> bool:
        """Vytvoří kostru karty pro nově enrollovanou osobu.

        Idempotent: pokud karta existuje a je aktivní, jen vrátí True.
        Pokud existuje a je deaktivovaná, automaticky ji reaktivuje.
        Pokud neexistuje, vytvoří novou.

        Charakteristika zůstává prázdná — doplní ji večerní reflexe
        až bude dost setkání.
        """
        now = time.time()
        family_json = json.dumps(family_links or {}, ensure_ascii=False)
        with self._connect() as db:
            row = db.execute(
                "SELECT deactivated_at FROM relationships WHERE person_id=?",
                (person_id,)
            ).fetchone()
            if row is None:
                # Nová osoba
                db.execute("""
                    INSERT INTO relationships
                        (person_id, display_name, role, family_links,
                         first_seen_ts, last_seen_ts, sightings_count,
                         characterization, updated_at, deactivated_at)
                    VALUES (?, ?, ?, ?, NULL, NULL, 0, '', ?, NULL)
                """, (person_id, display_name, role, family_json, now))
                db.commit()
                _log.info("seed_one: vytvořena nová karta '%s' (%s)",
                          person_id, display_name)
                return True
            elif row[0] is not None:
                # Existuje, ale je deaktivovaná → reactivate
                db.execute(
                    "UPDATE relationships "
                    "SET deactivated_at=NULL, updated_at=? "
                    "WHERE person_id=?",
                    (now, person_id)
                )
                db.commit()
                _log.info("seed_one: karta '%s' reaktivována "
                          "(původní data zachována)", person_id)
                return True
            else:
                # Aktivní karta už existuje — no-op
                _log.debug("seed_one: '%s' už aktivně existuje", person_id)
                return True



# ═══════════════════════════════════════════════════════════════════════
# RelationshipReflection — týdenní LLM reflexe vztahových karet
# (Dříve v samostatném souboru hans_relationship_reflection.py)
# RELATIONSHIPS_MERGED_V1
# ═══════════════════════════════════════════════════════════════════════

_log_refl = logging.getLogger("hans_relationship_reflection")

# Event typy, ve kterých budeme hledat zmínky o osobě (kromě person_seen)
# Hansovy VLASTNÍ texty, ve kterých se může zmínit o osobě.
# DŮLEŽITÉ: žádný z těchto eventů není konverzace S osobou — všechny jsou
# Hansovy vnitřní úvahy (s fiktivním Koláčem nebo nad případem).
_MENTION_EVENT_TYPES = (
    "teddy_dialog",
    "dialog_reflection",
    "case_thought",
    "case_resolution",
    "film_suggestion_accepted",  # KODI_FILM_SUGGEST_V1
)

# Skutečné rozhovory mezi osobou a Hansem (chat okno, voice listener).
# Kvalitativně jiná kategorie — obsahuje slova, která osoba reálně řekla.
_HUMAN_CHAT_EVENT = "human_chat"

# Pro vstup [MOJE VNITŘNÍ ÚVAHY] do promptu musí mít osoba dost
# reálných dat, jinak LLM tyto úvahy promítne na osobu jako fakta.
# "Dost dat" = aspoň 1 chat NEBO 5+ setkání.
MIN_SIGHTINGS_FOR_INNER_THOUGHTS = 5


class RelationshipReflection:

    def __init__(self, config: dict, diary_path: str, synthesis,
                 knowledge=None):
        cfg = config.get("relationships", {})
        self._enabled = bool(cfg.get("enabled", True))
        self._interval_days = int(cfg.get("reflection_interval_days", 3))
        self._lookback_days = int(cfg.get("reflection_lookback_days",
                                          self._interval_days * 2))
        self._min_sightings = int(cfg.get("reflection_min_sightings", 3))
        self._rag_collection = cfg.get("rag_collection", "hans_identita")

        self._diary_path = diary_path
        self._synthesis = synthesis
        self._knowledge = knowledge
        self._rels = Relationships(config)

        rag_status = "off"
        if self._knowledge is not None:
            try:
                rag_status = "on" if self._knowledge.enabled else "off (disabled)"
            except Exception:
                rag_status = "off (error)"

        _log_refl.info(
            "RelationshipReflection ready (enabled=%s, interval=%dd, "
            "lookback=%dd, min_sightings=%d, rag=%s/%s)",
            self._enabled, self._interval_days,
            self._lookback_days, self._min_sightings,
            rag_status, self._rag_collection)

    # ── Public ─────────────────────────────────────────────────────────────

    def reflect_due_persons(self) -> int:
        """Zreflektuje všechny osoby, kde uplynul interval. Vrací počet
        skutečně updatovaných karet."""
        if not self._enabled:
            return 0
        if self._synthesis is None:
            _log_refl.warning("synthesis je None — přeskakuju reflexi")
            return 0

        now = time.time()
        threshold = self._interval_days * 86400.0
        cards = self._rels.all_cards()
        updated = 0

        for card in cards:
            age = now - (card.last_reflection_at or 0.0)  # REL_REFLECTION_TS_V1
            if age < threshold:
                _log_refl.debug("%s: ještě %dh do reflexe",
                                card.person_id, int((threshold - age) / 3600))
                continue

            ok = self._reflect_one(card)
            if ok:
                updated += 1

        if updated:
            _log_refl.info("Reflexe hotová — aktualizováno %d karet", updated)
        return updated

    # ── Internal ───────────────────────────────────────────────────────────

    def _reflect_one(self, card) -> bool:
        person_id = card.person_id
        observations = self._collect_observations(person_id)

        # Minimum: aspoň 3 sighting eventy NEBO aspoň 1 zmínka v dialogu
        sightings = len(observations.get("person_seen", []))
        mentions = sum(len(v) for k, v in observations.items()
                       if k != "person_seen")

        if sightings < self._min_sightings and mentions == 0:
            _log_refl.info("%s: málo dat (sightings=%d, mentions=%d) — skip",
                           person_id, sightings, mentions)
            return False

        prompt_input = self._build_prompt_input(card, observations)

        try:
            text = self._synthesis.synthesize(
                topic=card.display_name,
                facts=prompt_input,
                style="relationship_reflection",
                max_tokens=300,
                max_chars=800,
                facts_max_chars=4000,
            )
        except Exception as e:
            _log_refl.error("LLM volání selhalo pro %s: %s", person_id, e)
            return False

        if not text or not text.strip():
            _log_refl.warning("%s: prázdný výstup z LLM — skip", person_id)
            return False

        text = text.strip()
        ok = self._rels.update_characterization(person_id, text)
        if not ok:
            return False

        # Diary event — pro pozdější synthesis hooks / debug
        self._write_diary(person_id, card.display_name, text)
        _log_refl.info("%s: characterization updatován (%d znaků)",
                       person_id, len(text))

        # RAG upload do hans_identita (s aktuální kartou po updatu)
        updated_card = self._rels.get(person_id)
        if updated_card is not None:
            self._upload_to_rag(updated_card)

        return True

    def _collect_observations(self, person_id: str) -> dict:
        """Vrátí {event_type: [(ts, title, note), ...]} pro daný person_id
        za posledních lookback_days dní."""
        out: dict = {}
        cutoff_ts = time.time() - self._lookback_days * 86400.0

        try:
            db = sqlite3.connect(self._diary_path)

            # Vlastní person_seen — match na title (= person_id)
            rows = db.execute(
                "SELECT ts, title, note FROM diary "
                "WHERE event_type='person_seen' AND title=? AND ts>=? "
                "ORDER BY ts ASC",
                (person_id, cutoff_ts)
            ).fetchall()
            if rows:
                out["person_seen"] = [
                    (r[0], r[1] or "", r[2] or "") for r in rows
                ]

            # Zmínky v dialozích / case zápisech — fulltext na title nebo note.
            # Klíčové slovo = person_id (lowercase) i display_name.
            card = self._rels.get(person_id)
            keywords = {person_id.lower()}
            if card and card.display_name:
                keywords.add(card.display_name.lower())

            for evt in _MENTION_EVENT_TYPES:
                rows = db.execute(
                    "SELECT ts, title, note FROM diary "
                    "WHERE event_type=? AND ts>=? "
                    "ORDER BY ts ASC",
                    (evt, cutoff_ts)
                ).fetchall()
                hits = []
                for ts, title, note in rows:
                    blob = ((title or "") + " " + (note or "")).lower()
                    if any(kw in blob for kw in keywords):
                        hits.append((ts, title or "", note or ""))
                if hits:
                    out[evt] = hits

            # human_chat — title je přímo person_id, žádný fulltext potřeba
            rows = db.execute(
                "SELECT ts, title, note FROM diary "
                "WHERE event_type=? AND title=? AND ts>=? "
                "ORDER BY ts ASC",
                (_HUMAN_CHAT_EVENT, person_id, cutoff_ts)
            ).fetchall()
            if rows:
                out[_HUMAN_CHAT_EVENT] = [
                    (r[0], r[1] or "", r[2] or "") for r in rows
                ]

            db.close()
        except Exception as e:
            _log_refl.error("Sběr observations selhal pro %s: %s", person_id, e)
        return out

    def _build_prompt_input(self, card, observations: dict) -> str:
        """Sestaví textový vstup pro LLM."""
        lines = []

        # 1. Základní info
        lines.append("[ZÁKLADNÍ INFO]")
        lines.append(f"Jméno: {card.display_name}")
        if card.role:
            lines.append(f"Role: {card.role}")
        if card.family_links:
            links_str = ", ".join(
                f"{k}={v}" for k, v in card.family_links.items()
            )
            lines.append(f"Rodinné vazby: {links_str}")
        lines.append("")

        # 2. Předchozí poznámka
        lines.append("[PŘEDCHOZÍ POZNÁMKA]")
        if card.characterization:
            lines.append(card.characterization)
        else:
            lines.append("(zatím prázdné — píšeš poprvé)")
        lines.append("")

        # 3. Pozorování
        lines.append("[POZOROVÁNÍ — co o osobě vím ze sledování kamerou]")
        lines.append(f"(za posledních {self._lookback_days} dní)")

        # Sightings — agregát + 5 příkladů s časem
        sightings = observations.get("person_seen", [])
        if sightings:
            lines.append(f"Setkání: {len(sightings)}× za období.")
            # Rozložení po hodinách dne
            hours = [datetime.fromtimestamp(s[0]).hour for s in sightings]
            morning = sum(1 for h in hours if 5 <= h < 12)
            afternoon = sum(1 for h in hours if 12 <= h < 17)
            evening = sum(1 for h in hours if 17 <= h < 22)
            night = sum(1 for h in hours if h >= 22 or h < 5)
            parts = []
            if morning: parts.append(f"ráno {morning}×")
            if afternoon: parts.append(f"odpoledne {afternoon}×")
            if evening: parts.append(f"večer {evening}×")
            if night: parts.append(f"v noci {night}×")
            if parts:
                lines.append("Doba: " + ", ".join(parts) + ".")

        # MOJE VNITŘNÍ ÚVAHY — zařadíme JEN POKUD má osoba dost reálných dat
        # (chat NEBO 5+ setkání). Bez kotvy reality si LLM tyto úvahy
        # promítne na osobu jako její vlastnosti (problém ranné verze).
        n_chats = len(observations.get(_HUMAN_CHAT_EVENT, []))
        n_sightings = len(observations.get("person_seen", []))
        include_inner = (n_chats >= 1
                         or n_sightings >= MIN_SIGHTINGS_FOR_INNER_THOUGHTS)

        if include_inner:
            mention_lines = []
            for evt, hits in observations.items():
                if evt == "person_seen" or evt == _HUMAN_CHAT_EVENT:
                    continue
                for ts, title, note in hits[-6:]:
                    snippet = (note or title)[:200].strip().replace("\n", " ")
                    if snippet:
                        mention_lines.append(f"  • {snippet}")

            if mention_lines:
                lines.append("")
                lines.append("[MOJE VNITŘNÍ ÚVAHY — moje vlastní myšlenky o této osobě,")
                lines.append(" ne věci, které řekla nebo udělala]")
                lines.extend(mention_lines)

        # NAŠE ROZHOVORY — skutečné chat/voice exchanges s touto osobou.
        # Plný text obou stran, max 8 nejnovějších, kvůli kontextu delší
        # snippet než u vnitřních úvah.
        chat_hits = observations.get(_HUMAN_CHAT_EVENT, [])
        if chat_hits:
            lines.append("")
            lines.append("[NAŠE ROZHOVORY — co spolu skutečně mluvíme]")
            lines.append(f"(celkem {len(chat_hits)} výměn za období)")
            for ts, title, note in chat_hits[-8:]:
                snippet = (note or "")[:500].strip()
                if snippet:
                    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                    lines.append(f"  • [{when}]")
                    for line in snippet.split("\n"):
                        if line.strip():
                            lines.append(f"    {line.strip()}")

        return "\n".join(lines)

    def _upload_to_rag(self, card) -> bool:
        """Uploadne vztahovou kartu do RAG kolekce hans_identita.

        doc_id je stabilní (relationship_<person_id>), takže další upload
        nahradí předchozí verzi. Pokud RAG není dostupný, jen warning.
        """
        if self._knowledge is None:
            return False
        try:
            if not self._knowledge.enabled:
                _log_refl.debug("RAG upload skipped — knowledge disabled")
                return False
        except Exception as e:
            _log_refl.warning("RAG check enabled() failed: %s", e)
            return False

        doc_id = f"relationship_{card.person_id}"
        title = f"Vztah — {card.display_name}"
        text = self._build_rag_document(card)
        metadata = {
            "typ": "vztahová karta",
            "osoba": card.person_id,
            "jmeno": card.display_name,
            "role": card.role or "",
        }

        try:
            ok = self._knowledge.upload(
                collection_key=self._rag_collection,
                doc_id=doc_id,
                title=title,
                text=text,
                metadata=metadata,
            )
            if ok:
                _log_refl.info("RAG: %s/%s nahráno (%d znaků)",
                               self._rag_collection, doc_id, len(text))
            else:
                _log_refl.warning("RAG: upload %s vrátil False", doc_id)
            return ok
        except Exception as e:
            _log_refl.error("RAG upload selhal pro %s: %s", doc_id, e)
            return False

    def _build_rag_document(self, card) -> str:
        """Sestaví markdown dokument pro RAG kolekci.

        Pořadí: nadpis → characterization (důležitý obsah) →
        rodinné vazby → metadata.

        Rationale: RAG chunker dokument rozseká po ~512 tokenech, a pro
        krátké karty může characterization skončit v jiném chunku než
        metadata. Důležitý obsah (charakter) musí být v prvním chunku,
        aby retrieval na dotaz 'co víš o X' vrátil charakteristiku, ne
        jen suchá metadata.
        """
        lines = [f"# {card.display_name}", ""]

        # 1. Charakteristika — NEJDŮLEŽITĚJŠÍ, nahoru
        if card.characterization:
            lines.append("## Moje poznámky")
            lines.append("")
            lines.append(card.characterization)
            lines.append("")
        else:
            lines.append("_(Zatím o této osobě nemám hlubší poznámky.)_")
            lines.append("")

        # 2. Rodinné vazby — důležité kontextové info
        if card.family_links:
            link_labels = {
                "spouse": "manžel/ka",
                "children": "děti",
                "parents": "rodiče",
                "siblings": "sourozenci",
            }
            lines.append("## Rodina")
            lines.append("")
            for k, v in card.family_links.items():
                label = link_labels.get(k, k)
                if isinstance(v, list):
                    v_str = ", ".join(v)
                else:
                    v_str = str(v)
                lines.append(f"- {label.capitalize()}: {v_str}")
            lines.append("")

        # 3. Metadata — role + aktivita (na konec, ne nejdůležitější)
        meta_lines = []
        if card.role:
            meta_lines.append(f"- Role v domě: {card.role}")
        if card.sightings_count:
            meta_lines.append(f"- Setkání celkem: {card.sightings_count}")
        if card.last_seen_ts:
            last = datetime.fromtimestamp(card.last_seen_ts).strftime(
                "%Y-%m-%d %H:%M")
            meta_lines.append(f"- Naposled viděn: {last}")
        if meta_lines:
            lines.append("## Údaje")
            lines.append("")
            lines.extend(meta_lines)
            lines.append("")

        if card.updated_at:
            upd = datetime.fromtimestamp(card.updated_at).strftime("%Y-%m-%d")
            lines.append(f"_Záznam naposled aktualizován: {upd}_")

        return "\n".join(lines)

    def _write_diary(self, person_id: str, display_name: str, text: str):
        try:
            with sqlite3.connect(self._diary_path) as db:
                db.execute(
                    "INSERT INTO diary (ts, event_type, title, note) "
                    "VALUES (?,?,?,?)",
                    (time.time(), "characterization_update",
                     display_name, text)
                )
                db.commit()
        except Exception as e:
            _log_refl.error("Zápis characterization_update selhal: %s", e)


# --- self-test ---
if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    _project_root = Path(__file__).resolve().parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))
    cfg_path = _project_root / "config.json"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if not Path(cfg.get("diary_db", "")).is_absolute():
        cfg["diary_db"] = str(
            _project_root / cfg.get(
                "diary_db", "data/hans_diary.db"))

    # 1) Relationships seed test
    rels = Relationships(cfg)
    n = rels.seed_if_empty()
    print(f"Seed vložil: {n}")
    for card in rels.all_cards():
        print(f"  {card.person_id:8s}  role={card.role!r:20s}  "
              f"sightings={card.sightings_count}  "
              f"links={card.family_links}")

    # 2) RelationshipReflection debug — vynucený interval 0
    if "--reflect" in sys.argv:
        from scripts.hans_synthesis import HansSynthesis
        syn = HansSynthesis(cfg)
        refl = RelationshipReflection(cfg, cfg["diary_db"], syn)
        refl._interval_days = 0
        m = refl.reflect_due_persons()
        print(f"Reflexe spustila pro {m} osob.")
