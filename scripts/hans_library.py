"""
Hans Library — Hans čte knihy přes víc dní.

Knihy z Project Gutenberg (public domain). Hans přečte jednu
"kapitolu" denně, pamatuje si kde skončil, a občas ze čtení
cituje nebo komentuje v dialogu s Kolačem.

Podporuje:
  - Automatické stahování z Gutenberg
  - Rozdělení na kapitoly (nebo bloky po N znacích)
  - Sledování pozice (bookmark)
  - LLM sumarizace přečteného
  - Kontext pro dialog ("Hans právě čte Babičku od Němcové, kapitola 3")

Použití:
    lib = HansLibrary(config, "data/hans_diary.db")
    
    # Denně jednou:
    chapter = lib.read_next_chapter()
    if chapter:
        summary = lib.summarize_chapter(chapter, ollama_url, model)
    
    # Pro dialog kontext:
    ctx = lib.get_reading_context()
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

_log = logging.getLogger("hans_library")

# České knihy na Project Gutenberg (public domain)
_DEFAULT_BOOKS = [
    {
        "id": "babicka",
        "title": "Babicka",
        "author": "Bozena Nemcova",
        "url": "https://www.gutenberg.org/cache/epub/langlinks/cs",
        "lang": "cs",
        "gutenberg_id": None,  # Doplní se manuálně
    },
]

# Záložní knihy — pokud Gutenberg nemá česky, použij anglické klasiky
# které Hans jako anglický majordomus zná
_FALLBACK_BOOKS = [
    # HANS_LIBRARY_CURATED_V1 — tags řídí výběr dle Hansových koníčků/postojů
    {"id": "importance_of_being_earnest", "title": "The Importance of Being Earnest",
     "author": "Oscar Wilde", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/844/pg844.txt",
     "tags": ["společnost", "humor", "literatura"]},
    {"id": "sherlock_adventures", "title": "The Adventures of Sherlock Holmes",
     "author": "Arthur Conan Doyle", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/1661/pg1661.txt",
     "tags": ["detektivky", "krimi", "dedukce"]},
    {"id": "hound_baskervilles", "title": "The Hound of the Baskervilles",
     "author": "Arthur Conan Doyle", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/2852/pg2852.txt",
     "tags": ["detektivky", "krimi", "dedukce"]},
    {"id": "pride_and_prejudice", "title": "Pride and Prejudice",
     "author": "Jane Austen", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/1342/pg1342.txt",
     "tags": ["společnost", "literatura", "romance"]},
    {"id": "picture_of_dorian_gray", "title": "The Picture of Dorian Gray",
     "author": "Oscar Wilde", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/174/pg174.txt",
     "tags": ["literatura", "morálka", "filozofie"]},
    {"id": "ivanhoe", "title": "Ivanhoe", "author": "Walter Scott", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/82/pg82.txt",
     "tags": ["historie", "hrady", "rytíři", "architektura", "památky"]},
    {"id": "meditations", "title": "Meditations", "author": "Marcus Aurelius",
     "lang": "en", "url": "https://www.gutenberg.org/cache/epub/2680/pg2680.txt",
     "tags": ["filozofie", "existence", "úvahy", "smysl"]},
    {"id": "around_the_world", "title": "Around the World in Eighty Days",
     "author": "Jules Verne", "lang": "en",
     "url": "https://www.gutenberg.org/cache/epub/103/pg103.txt",
     "tags": ["dobrodružství", "cestování"]},
]

# Velikost "kapitoly" pokud kniha nemá jasné dělení
_DEFAULT_CHUNK_SIZE = 3000  # znaků = ~500 slov = ~2 min čtení


@dataclass
class Chapter:
    book_id: str
    book_title: str
    chapter_num: int
    title: str
    text: str
    summary: str = ""


def _cosine(a, b) -> float:
    import math
    s = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return s / (na * nb) if na and nb else 0.0


class HansLibrary:
    """Spravuje Hansovu knihovnu a čtení."""

    def __init__(self, config: dict, diary_db_path: str, diary_writer=None):
        # DIARY_WRITER_PATCH_LIBRARY
        self._diary_writer = diary_writer
        self.config = config
        self._diary_path = diary_db_path
        self._books_dir = Path("data/library")
        self._books_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

        cfg = config.get("hans_library", {})
        self._enabled = bool(cfg.get("enabled", True))
        self._chunk_size = int(cfg.get("chunk_size", _DEFAULT_CHUNK_SIZE))
        self._custom_urls = cfg.get("custom_books", [])

        _log.info("HansLibrary ready (dir=%s)", self._books_dir)

    def _init_db(self):
        db = sqlite3.connect(self._diary_path)
        db.execute("""
            CREATE TABLE IF NOT EXISTS hans_library (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id     TEXT NOT NULL,
                book_title  TEXT NOT NULL,
                author      TEXT DEFAULT '',
                total_chapters INTEGER DEFAULT 0,
                current_chapter INTEGER DEFAULT 0,
                started_at  REAL NOT NULL,
                finished_at REAL,
                status      TEXT DEFAULT 'reading',
                last_summary TEXT DEFAULT ''
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS hans_library_chapters (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                book_id     TEXT NOT NULL,
                chapter_num INTEGER NOT NULL,
                title       TEXT DEFAULT '',
                text_hash   TEXT DEFAULT '',
                summary     TEXT DEFAULT '',
                read_at     REAL
            )
        """)
        # BOOK_COMPLETION_DEFERRED_V1 — perzistentní flag pro odloženou completion
        # reflexi: Ollama down v okamžiku dočtení nesmí ztratit reflexi (retry až
        # do úspěchu). Viz pravidlo ollama-deferred-processing. Idempotentní ALTER.
        _cols = [r[1] for r in db.execute("PRAGMA table_info(hans_library)").fetchall()]
        if "completion_reflected" not in _cols:
            db.execute("ALTER TABLE hans_library ADD COLUMN completion_reflected INTEGER DEFAULT 0")
            # Jednorázově (jen při zavedení sloupce): existující dočtené knihy ber
            # jako vyřízené — feature je dopředná, neretroaktivuj backlog. Knihy
            # dočtené PO deployi mají default 0 → projdou completion reflexí.
            db.execute("UPDATE hans_library SET completion_reflected=1 WHERE status='finished'")
        # HANS_BOOK_MENTIONS_V1 — wishlist knihy ze zmínek v chatu nesou zdrojovou
        # URL (Gutenberg) + jazyk, aby je reader uměl stáhnout. Idempotentní ALTER.
        if "url" not in _cols:
            db.execute("ALTER TABLE hans_library ADD COLUMN url TEXT DEFAULT ''")
        if "source_lang" not in _cols:
            db.execute("ALTER TABLE hans_library ADD COLUMN source_lang TEXT DEFAULT ''")
        db.commit()
        db.close()

    # ── Public API ───────────────────────────────────────────────────────────

    def get_current_book(self) -> Optional[dict]:
        """Vrátí aktuálně čtenou knihu."""
        db = sqlite3.connect(self._diary_path)
        row = db.execute(
            "SELECT book_id, book_title, author, total_chapters, "
            "current_chapter, last_summary FROM hans_library "
            "WHERE status='reading' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        db.close()
        if row:
            return {
                "book_id": row[0], "title": row[1], "author": row[2],
                "total": row[3], "current": row[4], "summary": row[5],
            }
        return None

    def read_next_chapter(self) -> Optional[Chapter]:
        """Přečte další kapitolu aktuální knihy. Pokud žádná → začne novou."""
        if not self._enabled:
            return None

        book = self.get_current_book()
        if not book:
            # Začni novou knihu
            book = self._start_new_book()
            if not book:
                return None

        book_id = book["book_id"]
        next_ch = book["current"] + 1

        # Načti text kapitoly
        chapters = self._load_chapters(book_id)
        if not chapters or next_ch > len(chapters):
            # Kniha dočtena
            self._finish_book(book_id)
            return None

        ch = chapters[next_ch - 1]  # 0-indexed

        # Zaznamenej přečtení
        db = sqlite3.connect(self._diary_path)
        db.execute(
            "UPDATE hans_library SET current_chapter=? WHERE book_id=? AND status='reading'",
            (next_ch, book_id))
        db.execute(
            "INSERT OR REPLACE INTO hans_library_chapters "
            "(book_id, chapter_num, title, text_hash, read_at) VALUES (?,?,?,?,?)",
            (book_id, next_ch, ch.title, hashlib.md5(ch.text.encode()).hexdigest(),
             time.time()))
        db.commit()
        db.close()

        # BOOK_GROUNDING_V2 — sentence-clip ~1500 místo syrového [:500] uprostřed slova
        _book_note = (getattr(ch, "summary", "") or getattr(ch, "text", "") or "").strip()
        if len(_book_note) > 1500:
            _cut = _book_note[:1500]
            _b = max(_cut.rfind(". "), _cut.rfind("! "), _cut.rfind("? "))
            _book_note = _cut[:_b + 1] if _b > 750 else _cut
        self._diary_write("book_read",
                          f"{book['title']} — kap. {next_ch}",
                          _book_note)

        _log.info("Read chapter %d/%d of '%s': %s",
                  next_ch, book["total"], book["title"], ch.title)
        return ch

    def get_reading_context(self) -> str:
        """Pro LLM dialog kontext — co Hans právě čte."""
        book = self.get_current_book()
        if not book:
            return ""
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        ctx = (f"{_pn(self.config)} cte knihu '{book['title']}' od {book['author']} "
               f"(kapitola {book['current']}/{book['total']}).")
        _refl = self._latest_reflection(book['title'])  # HANS_READING_CONTEXT_V1
        if _refl:
            ctx += f" Naposledy ho zaujalo: {_refl[:200]}"
        return ctx

    def get_quote_for_dialog(self) -> str:
        """Vrátí krátký kontext pro dialog — Hans může zmínit co čte."""
        book = self.get_current_book()
        if not book:
            return ""
        _refl = self._latest_reflection(book['title'])  # HANS_READING_CONTEXT_V1
        if not _refl:
            return ""
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        return (f"{_pn(self.config)} nedavno cetl {book['title']} ({book['author']}) "
                f"a zaujalo ho: {_refl[:200]}")

    def _latest_reflection(self, book_title: str) -> str:
        """HANS_READING_CONTEXT_V1 — poslední Hansova reflexe k této knize
        (book_reflection.data; last_summary je mrtvý). Read-only."""
        if not book_title:
            return ""
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path,
                                  uri=True, timeout=2.0)
            row = con.execute(
                "SELECT data FROM diary WHERE event_type='book_reflection' "
                "AND title LIKE ? AND data IS NOT NULL AND data!='' "
                "ORDER BY ts DESC LIMIT 1", (book_title + "%",)).fetchone()
            con.close()
            return row[0].strip() if row and row[0] else ""
        except Exception:
            return ""

    # ── Správa knih ──────────────────────────────────────────────────────────

    def _wishlist_candidates(self) -> list:
        """HANS_BOOK_MENTIONS_V1 — knihy z wishlistu, které mají zdrojovou URL
        (ze zmínek v chatu přes Gutenberg). Vrací katalogové dicty (low priority)."""
        out = []
        try:
            db = sqlite3.connect(self._diary_path)
            rows = db.execute(
                "SELECT book_id, book_title, author, url, source_lang "
                "FROM hans_library WHERE status='wishlist' AND COALESCE(url,'')<>''"
            ).fetchall()
            db.close()
            for bid, title, author, url, lang in rows:
                out.append({"id": bid, "title": title, "author": author or "",
                            "url": url, "lang": lang or "", "tags": [],
                            "wishlist": True})
        except Exception as e:
            _log.debug("wishlist_candidates failed: %s", e)
        return out

    def _start_new_book(self) -> Optional[dict]:
        """Vybere a stáhne novou knihu."""
        # Zkus custom URL z configu
        books = self._custom_urls + _FALLBACK_BOOKS

        # Vyber knihu kterou ještě nečetl
        db = sqlite3.connect(self._diary_path)
        read_ids = {r[0] for r in db.execute(
            "SELECT book_id FROM hans_library").fetchall()}
        db.close()

        # HANS_BOOK_MENTIONS_V1 — wishlist knihy (s URL) jsou samostatný zdroj
        # kandidátů (nízká priorita), ne filtrované read_ids (být v tabulce = BÝT
        # na wishlistu). Katalog se dál filtruje na nepřečtené.
        wishlist = self._wishlist_candidates()
        available = [b for b in books if b["id"] not in read_ids] + wishlist
        if not available:
            # Všechny přečteny a žádný wishlist — recykluj katalog
            available = books

        if not available:
            _log.warning("No books available")
            return None

        chosen = self._pick_book_by_tendencies(available)  # HANS_LIBRARY_CURATED_V1

        # Stáhni knihu
        text = self._download_book(chosen)
        if not text:
            return None

        # Rozděl na kapitoly
        chapters = self._split_into_chapters(text, chosen.get("id", "unknown"))
        if not chapters:
            return None

        # Ulož metadata
        book_id = chosen["id"]
        db = sqlite3.connect(self._diary_path)
        if chosen.get("wishlist"):
            # wishlist řádek už existuje → překlop na 'reading' (ne duplicitní INSERT)
            db.execute(
                "UPDATE hans_library SET status='reading', total_chapters=?, "
                "current_chapter=0, started_at=? WHERE book_id=? AND status='wishlist'",
                (len(chapters), time.time(), book_id))
        else:
            db.execute(
                "INSERT INTO hans_library "
                "(book_id, book_title, author, total_chapters, current_chapter, "
                "started_at, status) VALUES (?,?,?,?,0,?,?)",
                (book_id, chosen["title"], chosen.get("author", ""),
                 len(chapters), time.time(), "reading"))
        db.commit()
        db.close()

        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        self._diary_write("book_started",
                          f"Zacal cist: {chosen['title']}",
                          f"{_pn(self.config)} zacal cist '{chosen['title']}' "
                          f"od {chosen.get('author', '?')} ({len(chapters)} kapitol).")

        _log.info("Started book '%s' by %s (%d chapters)",
                  chosen["title"], chosen.get("author", "?"), len(chapters))

        return {
            "book_id": book_id, "title": chosen["title"],
            "author": chosen.get("author", ""), "total": len(chapters),
            "current": 0, "summary": "",
        }

    def _gather_interests_text(self) -> str:
        """HANS_LIBRARY_CURATED_V1 — Hansovy zájmy jako text (koníčky + postoje),
        read-only. Slouží jen k výběru knihy (low-stakes → bez durable gate)."""
        parts = []
        try:
            con = sqlite3.connect("file:%s?mode=ro" % self._diary_path,
                                  uri=True, timeout=2.0)
            for q in ("SELECT name FROM hobbies WHERE status='active'",
                      "SELECT claim FROM stances WHERE status='active'"):
                try:
                    parts += [r[0] for r in con.execute(q).fetchall() if r and r[0]]
                except Exception:
                    pass
            con.close()
        except Exception:
            pass
        return " ".join(parts).lower()

    def _embed(self, text: str):
        """bge-m3 embedding přes Ollama /api/embed. None při selhání."""
        if not text:
            return None
        import urllib.request
        url = self.config.get("openwebui_chat", {}).get(
            "base_url", "http://127.0.0.1:11434")
        model = (self.config.get("hans_library", {}).get("embed_model")
                 or "bge-m3:latest")
        try:
            req = urllib.request.Request(
                url + "/api/embed",
                data=json.dumps({"model": model, "input": text[:2000]}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                emb = json.load(r).get("embeddings") or []
            return emb[0] if emb else None
        except Exception as e:
            _log.debug("embed failed: %s", e)
            return None

    def _semantic_scores(self, available: list, interests: str):
        """HANS_LIBRARY_SEMANTIC_V1 — bge-m3 kosinová podobnost knih (titul+tagy)
        vs Hansovy zájmy. Vrací [(book, sim)] nebo None když embed nedostupný."""
        iv = self._embed(interests)
        if iv is None:
            return None
        out = []
        for b in available:
            desc = (b.get("title", "") + ". "
                    + ", ".join(b.get("tags", []))).strip(" .")
            bv = self._embed(desc) if desc else None
            out.append((b, _cosine(iv, bv) if bv else 0.0))
        return out

    def _pick_book_by_tendencies(self, available: list) -> dict:
        """HANS_LIBRARY_CURATED_V1 + HANS_LIBRARY_SEMANTIC_V1 — vyber knihu nejblíž
        Hansovým zájmům SÉMANTICKY (bge-m3), fallback doslovný překryv tagů.
        Nahrané knihy zlevněny (nízká priorita). ~25 % explorace. Fallback [0]."""
        import random
        interests = self._gather_interests_text()
        if not interests:
            return available[0]
        # #2 sémantický match (fallback na doslovný překryv tagů)
        scored = self._semantic_scores(available, interests)
        mode = "sémanticky"
        if scored is None:
            mode = "dle tagů"
            scored = [(b, float(sum(1 for t in b.get("tags", []) if t.lower() in interests)))
                      for b in available]
        # nahrané knihy + wishlist (ze zmínek v chatu) = nízká priorita (dampener)
        _hl = self.config.get("hans_library", {})
        uw = float(_hl.get("user_book_weight", 0.7))
        ww = float(_hl.get("wishlist_weight", 0.7))  # HANS_BOOK_MENTIONS_V1
        def _damp(b):
            f = 1.0
            if b.get("user_upload"):
                f *= uw
            if b.get("wishlist"):
                f *= ww
            return f
        scored = [(b, s * _damp(b)) for b, s in scored]
        scored.sort(key=lambda x: -x[1])
        best, best_score = scored[0]
        if best_score <= 0 or random.random() < 0.25:
            choice = random.choice(available)
            _log.info("Book pick: explorace → '%s'", choice.get("title"))
            return choice
        _log.info("Book pick: %s (skóre %.3f) → '%s'", mode, best_score, best.get("title"))
        return best

    def _finish_book(self, book_id: str):
        # HANS_BOOK_COMPLETION_V1 — titul + souhrny kapitol zachyť PŘED změnou
        # statusu (get_current_book pak vrací None) pro completion reflexi.
        book = self.get_current_book()
        title = book["title"] if book else book_id
        db = sqlite3.connect(self._diary_path)
        summaries = [r[0] for r in db.execute(
            "SELECT summary FROM hans_library_chapters WHERE book_id=? "
            "AND summary IS NOT NULL AND summary!='' ORDER BY chapter_num",
            (book_id,)).fetchall()]
        db.execute(
            "UPDATE hans_library SET status='finished', finished_at=? "
            "WHERE book_id=? AND status='reading'",
            (time.time(), book_id))
        db.commit()
        db.close()

        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        self._diary_write("book_finished",
                          f"Docetl: {title}",
                          f"{_pn(self.config)} docetl knihu '{title}'.")
        self._just_finished = {"title": title, "summaries": summaries}
        _log.info("Finished book '%s' (%d souhrnu kapitol)", title, len(summaries))

    def pop_just_finished(self):
        """HANS_BOOK_COMPLETION_V1 — vrátí {title, summaries} právě dočtené knihy
        (a vynuluje), jinak None. (DEPRECATED — nahrazeno perzistentním
        get_pending_completion, BOOK_COMPLETION_DEFERRED_V1.)"""
        jf = getattr(self, "_just_finished", None)
        self._just_finished = None
        return jf

    def get_pending_completion(self) -> Optional[dict]:
        """BOOK_COMPLETION_DEFERRED_V1 — nejstarší dočtená kniha BEZ completion
        reflexe (perzistentní, přežije restart i výpadek Ollamy → retry až do
        úspěchu). Vrací {book_id, title} nebo None."""
        db = sqlite3.connect(self._diary_path)
        try:
            row = db.execute(
                "SELECT book_id, book_title FROM hans_library "
                "WHERE status='finished' AND COALESCE(completion_reflected,0)=0 "
                "ORDER BY finished_at LIMIT 1").fetchone()
        finally:
            db.close()
        return {"book_id": row[0], "title": row[1]} if row else None

    def mark_completion_reflected(self, book_id: str):
        """BOOK_COMPLETION_DEFERRED_V1 — completion reflexe uspěla → přestaň
        retrigerovat. Volat AŽ po úspěšné reflexi (ne před)."""
        db = sqlite3.connect(self._diary_path)
        try:
            db.execute("UPDATE hans_library SET completion_reflected=1 WHERE book_id=?",
                       (book_id,))
            db.commit()
        finally:
            db.close()

    def _download_book(self, book_info: dict) -> Optional[str]:
        """Stáhne knihu z URL, cachuje lokálně."""
        book_id = book_info["id"]
        cache_path = self._books_dir / f"{book_id}.txt"

        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8", errors="ignore")

        url = book_info.get("url", "")
        if not url:
            return None

        try:
            _log.info("Downloading book '%s' from %s", book_info["title"], url)
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            text = r.text

            # Odstraň Gutenberg header/footer
            text = self._strip_gutenberg_boilerplate(text)

            cache_path.write_text(text, encoding="utf-8")
            _log.info("Book cached: %s (%d chars)", cache_path.name, len(text))
            return text

        except Exception as e:
            _log.error("Download failed for '%s': %s", book_info["title"], e)
            return None

    @staticmethod
    def _strip_gutenberg_boilerplate(text: str) -> str:
        """Odstraní Gutenberg header a footer."""
        # Start marker
        for marker in ["*** START OF", "***START OF"]:
            idx = text.find(marker)
            if idx >= 0:
                end_of_line = text.find("\n", idx)
                if end_of_line >= 0:
                    text = text[end_of_line + 1:]
                break

        # End marker
        for marker in ["*** END OF", "***END OF", "End of the Project"]:
            idx = text.find(marker)
            if idx >= 0:
                text = text[:idx]
                break

        return text.strip()

    def _split_into_chapters(self, text: str, book_id: str) -> list[Chapter]:
        """Rozděl text na kapitoly."""
        # Zkus najít chapter markers
        chapter_pattern = re.compile(
            r'^(CHAPTER|Chapter|KAPITOLA|Kapitola|HLAVA)\s+[\dIVXLCDM]+',
            re.MULTILINE
        )
        matches = list(chapter_pattern.finditer(text))

        chapters = []
        if len(matches) >= 3:
            # Kniha má jasné kapitoly
            for i, match in enumerate(matches):
                start = match.start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                ch_text = text[start:end].strip()
                ch_title = match.group(0).strip()
                chapters.append(Chapter(
                    book_id=book_id, book_title="",
                    chapter_num=i + 1, title=ch_title,
                    text=ch_text[:self._chunk_size * 3],  # limit
                ))
        else:
            # Rozděl na bloky po chunk_size znaků
            pos = 0
            ch_num = 1
            while pos < len(text):
                chunk = text[pos:pos + self._chunk_size]
                # Zkus najít konec odstavce (ne uprostřed věty)
                last_para = chunk.rfind("\n\n")
                if last_para > self._chunk_size // 2:
                    chunk = chunk[:last_para]
                chapters.append(Chapter(
                    book_id=book_id, book_title="",
                    chapter_num=ch_num,
                    title=f"Cast {ch_num}",
                    text=chunk.strip(),
                ))
                pos += len(chunk)
                ch_num += 1

        # Ulož kapitoly na disk
        ch_dir = self._books_dir / book_id
        ch_dir.mkdir(exist_ok=True)
        for ch in chapters:
            ch_path = ch_dir / f"ch{ch.chapter_num:03d}.txt"
            ch_path.write_text(ch.text, encoding="utf-8")

        _log.info("Split '%s' into %d chapters", book_id, len(chapters))
        return chapters

    def _load_chapters(self, book_id: str) -> list[Chapter]:
        """Načti kapitoly z disku."""
        ch_dir = self._books_dir / book_id
        if not ch_dir.exists():
            return []
        chapters = []
        for ch_path in sorted(ch_dir.glob("ch*.txt")):
            num = int(ch_path.stem.replace("ch", ""))
            chapters.append(Chapter(
                book_id=book_id, book_title="",
                chapter_num=num,
                title=f"Kapitola {num}",
                text=ch_path.read_text(encoding="utf-8"),
            ))
        return chapters

    # ── DB helper ────────────────────────────────────────────────────────────

    def _diary_write(self, event_type: str, title: str, note: str = ""):
        # Předej callback pokud existuje — jinak přímý SQL
        if self._diary_writer:
            try:
                self._diary_writer(event_type, title, note=note)
                return
            except Exception as _de:
                _log.warning("Diary writer (library) failed: %s", _de)
                self._diary_writer = None
        try:
            db = sqlite3.connect(self._diary_path)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note) "
                "VALUES (?,?,?,?)",
                (time.time(), event_type, title, note))
            db.commit()
            db.close()
        except Exception as _de:
            _log.warning("Diary write failed: %s", _de)

