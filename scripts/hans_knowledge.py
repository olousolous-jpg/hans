"""
HansKnowledge — uploaduje texty do OpenWebUI Knowledge kolekcí přes API.

Workflow OpenWebUI 0.8.x:
  1) POST /api/v1/files/  (multipart, soubor)         → file_id
  2) POST /api/v1/knowledge/{kid}/file/add  {file_id} → soubor v kolekci

Pro update existujícího dokumentu:
  3) POST /api/v1/knowledge/{kid}/file/remove  → smazat starý
  4) opakovat 1+2

Modul je thread-safe a fault-tolerant — pokud upload selže, hodí varování
ale nevyhodí výjimku ven. Volající (hooks v hans_idle) tedy nemusí chytat.

Mapping: každý záznam se identifikuje stringem `doc_id` který je unikátní
v rámci kolekce. Modul si v paměti drží `doc_id -> file_id` mapování,
aby uměl správně mazat starší verze. Tato mapa se persistuje do interní
SQLite (data/hans_knowledge.db) aby přežila restart.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field  # G1_RAG_QUERY_V1
from pathlib import Path
from typing import Optional

import requests


# ─── G1_RAG_QUERY_V1 — RAG retrieval result ──────────────────────────────
@dataclass
class RAGResult:
    """Výsledek RAG dotazu nad Knowledge kolekcí.

    chunks: list of dict {text, distance, name} — syrové shody (řazené,
            nejlepší první). Pro rozhodovací logiku (G.3) — kolik shod,
            jak dobré.
    text:   složený text top shod — připravený k vložení do kontextu.
    top_score: distance nejlepší shody (nižší = lepší). None když prázdné.
    collection_key: ze které kolekce.

    POZOR škála bge-m3: relevantní ~0.64-0.69, šum se PŘEKRÝVÁ. Nepoužívej
    top_score jako jediný absolutní práh — kombinuj s počtem shod / kontextem.
    """
    chunks: list = field(default_factory=list)
    text: str = ""
    top_score: Optional[float] = None
    collection_key: str = ""

    @property
    def found(self) -> bool:
        """True když dotaz vrátil aspoň jednu shodu."""
        return bool(self.chunks)

    def best_within(self, max_distance: float) -> bool:
        """True když nejlepší shoda je pod prahem (= dost relevantní)."""
        return self.top_score is not None and self.top_score <= max_distance

_log = logging.getLogger(__name__)

_KNOWLEDGE_DB = "data/hans_knowledge.db"


class HansKnowledge:
    """Upload textů do OpenWebUI Knowledge kolekcí."""

    def __init__(self, config: dict):
        kn = config.get("knowledge", {}) or {}
        ow = config.get("openwebui_direct", {}) or {}
        self._enabled: bool = bool(kn.get("enabled", False))
        self._base_url: str = (kn.get("base_url")
                               or ow.get("base_url", "")).rstrip("/")
        self._token: str = ow.get("api_token", "")
        self._collections: dict[str, str] = kn.get("collections", {}) or {}
        self._timeout: int = int(kn.get("timeout", 30))
        self._lock = threading.Lock()

        # Persistentní mapa doc_id → file_id (pro update/delete)
        Path(_KNOWLEDGE_DB).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(_KNOWLEDGE_DB, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                collection_key TEXT NOT NULL,
                doc_id         TEXT NOT NULL,
                file_id        TEXT NOT NULL,
                uploaded_at    REAL NOT NULL,
                PRIMARY KEY (collection_key, doc_id)
            )
        """)
        self._db.commit()

        if not self._enabled:
            _log.info("HansKnowledge: vypnuto (knowledge.enabled=false)")
        elif not self._collections:
            _log.warning("HansKnowledge: žádné kolekce v configu — "
                         "spusť knowledge_setup.py")
        elif not self._token:
            _log.warning("HansKnowledge: chybí api_token")

    # ── Public API ───────────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled and bool(self._token) and bool(self._collections)

    def upload(
        self,
        collection_key: str,
        doc_id: str,
        title: str,
        text: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """Nahraje text do kolekce. Pokud doc_id už existuje, nahradí ho.

        Args:
            collection_key: jméno kolekce (např. 'hans_filmy')
            doc_id:  unikátní string v rámci kolekce (např. 'movie_42')
            title:   lidsky čitelný název dokumentu
            text:    obsah který se zaindexuje
            metadata: volitelný dict (datum, kategorie...) — uloží se
                     jako prefix v textu, OpenWebUI ho zaindexuje s textem

        Returns:
            True při úspěchu, False při chybě (vždy se zaloguje warning).
        """
        if not self.enabled:
            return False
        if not text or not text.strip():
            return False
        if collection_key not in self._collections:
            _log.warning("upload: neznámá kolekce '%s'", collection_key)
            return False

        kid = self._collections[collection_key]
        with self._lock:
            try:
                # Pokud existuje stará verze, smaž ji
                old_file_id = self._get_file_id(collection_key, doc_id)
                if old_file_id:
                    self._remove_from_collection(kid, old_file_id)

                # Vytvoř text s metadaty
                full_text = self._format_doc(title, text, metadata)

                # Upload souboru
                file_id = self._upload_file(doc_id, full_text)
                if not file_id:
                    return False

                # Drobná pauza — OpenWebUI potřebuje čas na embedding
                # než se soubor dá přidat do kolekce
                time.sleep(0.5)

                # Přidat do kolekce (s retry)
                added = False
                for attempt in range(3):
                    if self._add_to_collection(kid, file_id):
                        added = True
                        break
                    time.sleep(1.0 + attempt)
                if not added:
                    _log.warning("file/add selhal po 3 pokusech: %s",
                                 doc_id)
                    return False

                # Zapamatuj si mapping
                self._save_file_id(collection_key, doc_id, file_id)
                _log.info("Knowledge upload OK: %s/%s (%d znaků)",
                          collection_key, doc_id, len(text))
                return True
            except Exception as e:
                _log.warning("Knowledge upload selhal (%s/%s): %s",
                             collection_key, doc_id, e)
                return False

    def query(
        self,
        collection_key: str,
        text: str,
        k: int = 5,
        max_distance: Optional[float] = None,
    ) -> "RAGResult":
        """G1_RAG_QUERY_V1 — Přečte relevantní chunky z kolekce.

        Args:
            collection_key: jméno kolekce (např. 'hans_denik')
            text: dotaz (přirozený jazyk)
            k: kolik nejlepších shod vrátit
            max_distance: volitelný tvrdý filtr — zahodí shody s distance
                          NAD tuto hodnotu. None = vrátí vše (default).
                          KONZERVATIVNÍ použití: volající (G.3) si filtruje
                          sám podle kontextu, query() jen poctivě měří.

        Returns:
            RAGResult — vždy (prázdný při chybě/nenalezení, nikdy nevyhodí).
        """
        if not self.enabled:
            return RAGResult(collection_key=collection_key)
        if not text or not text.strip():
            return RAGResult(collection_key=collection_key)
        if collection_key not in self._collections:
            _log.warning("query: neznámá kolekce '%s'", collection_key)
            return RAGResult(collection_key=collection_key)

        kid = self._collections[collection_key]
        # LOG_CIRCUIT_V1 — potlač spam z mrtvého OpenWebUI (PC v noci vypnutý)
        from scripts._log_circuit import for_url as _breaker_for, is_conn_error
        _url = f"{self._base_url}/api/v1/retrieval/query/collection"
        br = _breaker_for(_url)
        try:
            r = requests.post(
                _url,
                headers=self._headers_json(),
                json={"collection_names": [kid], "query": text, "k": int(k)},
                timeout=self._timeout,
            )
            if r.status_code != 200:
                _log.warning("query HTTP %d: %s", r.status_code, r.text[:200])
                return RAGResult(collection_key=collection_key)
            data = r.json()
            br.note_success(_log)
        except Exception as e:
            if is_conn_error(e):
                if br.should_log(e):
                    _log.warning("query selhal (%s): %s", collection_key, e)
            else:
                _log.warning("query selhal (%s): %s", collection_key, e)
            return RAGResult(collection_key=collection_key)

        # ChromaDB tvar: documents/distances/metadatas jsou listy listů,
        # index [0] = první (a jediná) kolekce z collection_names.
        docs = (data.get('documents') or [[]])[0] or []
        dists = (data.get('distances') or [[]])[0] or []
        metas = (data.get('metadatas') or [[]])[0] or []

        chunks = []
        for i, doc in enumerate(docs):
            dist = dists[i] if i < len(dists) else None
            meta = metas[i] if i < len(metas) else {}
            if max_distance is not None and dist is not None \
                    and dist > max_distance:
                continue  # tvrdý filtr (volitelný)
            chunks.append({
                'text': doc,
                'distance': dist,
                'name': (meta or {}).get('name', ''),
                # HANS_PROVENANCE_V1 — přesná provenience z metadata, když
                # ji upload uložil (jinak volající použije fallback dle kolekce).
                'provenance': (meta or {}).get('provenance'),
            })

        # Řazení: nejlepší (nejnižší distance) první. None distance dozadu.
        chunks.sort(key=lambda c: (c['distance'] is None,
                                   c['distance'] if c['distance'] is not None
                                   else 9e9))

        top_score = chunks[0]['distance'] if chunks else None
        composed = '\n\n'.join(c['text'] for c in chunks if c['text'])

        return RAGResult(
            chunks=chunks,
            text=composed,
            top_score=top_score,
            collection_key=collection_key,
        )

    def delete(self, collection_key: str, doc_id: str) -> bool:
        """Smaže dokument z kolekce."""
        if not self.enabled:
            return False
        if collection_key not in self._collections:
            return False
        kid = self._collections[collection_key]
        with self._lock:
            try:
                file_id = self._get_file_id(collection_key, doc_id)
                if not file_id:
                    return False  # už neexistuje
                self._remove_from_collection(kid, file_id)
                self._db.execute(
                    "DELETE FROM uploads WHERE collection_key=? AND doc_id=?",
                    (collection_key, doc_id))
                self._db.commit()
                return True
            except Exception as e:
                _log.warning("Knowledge delete selhal: %s", e)
                return False

    def stop(self):
        try:
            self._db.close()
        except Exception:
            pass

    # ── Internal HTTP helpers ────────────────────────────────────────────────

    def _headers_json(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _headers_auth(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def _upload_file(self, doc_id: str, text: str) -> Optional[str]:
        """POST /api/v1/files/  (multipart) → file_id"""
        # Bezpečný název souboru — jen alfanum + _
        safe_name = "".join(c if c.isalnum() or c in "_-" else "_"
                            for c in doc_id)[:80] or "doc"
        # Použij temp soubor
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".txt",
            prefix=f"{safe_name}_", delete=False,
        ) as tf:
            tf.write(text)
            tmp_path = tf.name
        try:
            with open(tmp_path, "rb") as f:
                files = {"file": (f"{safe_name}.txt", f, "text/plain")}
                r = requests.post(
                    f"{self._base_url}/api/v1/files/",
                    headers=self._headers_auth(),
                    files=files,
                    timeout=self._timeout,
                )
            if r.status_code != 200:
                _log.warning("file upload HTTP %d: %s",
                             r.status_code, r.text[:200])
                return None
            return r.json().get("id")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _add_to_collection(self, kid: str, file_id: str) -> bool:
        """POST /api/v1/knowledge/{kid}/file/add"""
        r = requests.post(
            f"{self._base_url}/api/v1/knowledge/{kid}/file/add",
            headers=self._headers_json(),
            json={"file_id": file_id},
            timeout=self._timeout,
        )
        if r.status_code != 200:
            _log.warning("file/add HTTP %d: %s",
                         r.status_code, r.text[:200])
            return False
        return True

    def _remove_from_collection(self, kid: str, file_id: str) -> bool:
        """POST /api/v1/knowledge/{kid}/file/remove"""
        try:
            r = requests.post(
                f"{self._base_url}/api/v1/knowledge/{kid}/file/remove",
                headers=self._headers_json(),
                json={"file_id": file_id},
                timeout=self._timeout,
            )
            if r.status_code != 200:
                _log.debug("file/remove HTTP %d: %s",
                           r.status_code, r.text[:200])
                return False
            return True
        except Exception as e:
            _log.debug("file/remove error: %s", e)
            return False

    # ── Internal DB helpers ──────────────────────────────────────────────────

    def _get_file_id(self, collection_key: str, doc_id: str) -> Optional[str]:
        row = self._db.execute(
            "SELECT file_id FROM uploads "
            "WHERE collection_key=? AND doc_id=?",
            (collection_key, doc_id),
        ).fetchone()
        return row[0] if row else None

    def _save_file_id(self, collection_key: str, doc_id: str, file_id: str):
        self._db.execute(
            "INSERT OR REPLACE INTO uploads "
            "(collection_key, doc_id, file_id, uploaded_at) "
            "VALUES (?,?,?,?)",
            (collection_key, doc_id, file_id, time.time()),
        )
        self._db.commit()

    # ── Formátování dokumentu ────────────────────────────────────────────────

    @staticmethod
    def _format_doc(title: str, text: str,
                    metadata: Optional[dict]) -> str:
        """Sestaví finální text souboru — title + meta + obsah."""
        parts = [f"# {title}", ""]
        if metadata:
            for k, v in metadata.items():
                parts.append(f"{k}: {v}")
            parts.append("")
        parts.append(text.strip())
        return "\n".join(parts)
