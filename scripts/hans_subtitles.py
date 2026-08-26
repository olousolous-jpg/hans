"""HANS_SUBTITLES_V1 — dohledání titulků na OpenSubtitles.

Pořadí zdrojů (změřeno 26.8.): otisk souboru → název s určením dílu → nic.

⚠️ IDENTIFIKACE MUSÍ JÍT PŘES OTISK, NE PŘES NÁZEV. Dotaz podle názvu bez
určení dílu vrátil na testovacím dokumentu 10 000 „nálezů" a byly to nesmysly
(*A History of Violence*, *Dexter S07E07 – Chemistry*). Naivní kód by vzal
první a přilepil k pořadu titulky z něčeho úplně jiného. `total_count` sám
o sobě neznamená NIC — rozhoduje příznak `moviehash_match`.

Limity: účet zdarma 20 stažení/den, bez účtu 5. **Hledání se do limitu
nepočítá**, jen stahování.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


def moviehash(path: str) -> str:
    """OSDb hash: velikost + prvních a posledních 64 kB, součet 64bit slov."""
    bs = 65536
    size = os.path.getsize(path)
    if size < bs * 2:
        raise ValueError("soubor je na otisk příliš malý")
    h = size
    with open(path, "rb") as f:
        for _ in range(bs // 8):
            h = (h + struct.unpack("<q", f.read(8))[0]) & 0xFFFFFFFFFFFFFFFF
        f.seek(max(0, size - bs), 0)
        for _ in range(bs // 8):
            h = (h + struct.unpack("<q", f.read(8))[0]) & 0xFFFFFFFFFFFFFFFF
    return "%016x" % h


class OpenSubtitles:
    def __init__(self, config: dict):
        c = (config or {}).get("subtitles", {}) or {}
        self.cfg = c
        self.base = c.get("api_base", "https://api.opensubtitles.com/api/v1").rstrip("/")
        self.h = {"Api-Key": c.get("api_key", ""),
                  "User-Agent": c.get("user_agent", "Hans v1.0"),
                  "Accept": "application/json"}
        self._token = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and self.cfg.get("api_key"))

    def _call(self, path, data=None, auth=False, timeout=30):
        h = dict(self.h)
        if data is not None:
            h["Content-Type"] = "application/json"
        if auth:
            h["Authorization"] = "Bearer " + self._login()
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None, headers=h)
        try:
            r = urllib.request.urlopen(req, timeout=timeout)
            return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"OpenSubtitles {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None

    def _login(self) -> str:
        if self._token:
            return self._token
        d = self._call("/login", {"username": self.cfg.get("username", ""),
                                  "password": self.cfg.get("password", "")})
        self._token = d.get("token")
        if not self._token:
            raise RuntimeError("OpenSubtitles: přihlášení nevrátilo token")
        return self._token

    def by_hash(self, film_hash: str, lang: str) -> list[dict]:
        """Jediná spolehlivá identifikace — bereme JEN potvrzenou shodu otisku."""
        d = self._call(f"/subtitles?moviehash={film_hash}&languages={lang}")
        return [x for x in (d.get("data") or [])
                if (x.get("attributes") or {}).get("moviehash_match")]

    def by_title(self, query: str, lang: str, season=None, episode=None) -> list[dict]:
        """Záchranná cesta. ⚠️ Bez určení dílu vrací nesmysly — proto se
        u seriálů BEZ season/episode raději nehledá vůbec."""
        q = urllib.request.quote(query)
        url = f"/subtitles?query={q}&languages={lang}"
        if season is not None:
            url += f"&season_number={int(season)}"
        if episode is not None:
            url += f"&episode_number={int(episode)}"
        elif season is None:
            log.info("titulky podle názvu bez určení dílu — výsledek je nespolehlivý")
        d = self._call(url)
        return (d.get("data") or [])[:5]

    @staticmethod
    def file_id(item: dict) -> int | None:
        files = (item.get("attributes") or {}).get("files") or []
        return files[0].get("file_id") if files else None

    def download(self, file_id: int, out_path: str) -> dict:
        """⚠️ Utratí jedno z denních stažení."""
        d = self._call("/download", {"file_id": int(file_id)}, auth=True)
        link = d.get("link")
        if not link:
            raise RuntimeError("OpenSubtitles nevrátil odkaz ke stažení")
        raw = urllib.request.urlopen(urllib.request.Request(
            link, headers={"User-Agent": self.h["User-Agent"]}), timeout=60).read()
        with open(out_path, "wb") as f:
            f.write(raw)
        zbyva = d.get("remaining")
        log.info("titulky staženy (%d B), zbývá dnes stažení: %s", len(raw), zbyva)
        return {"path": out_path, "remaining": zbyva}
