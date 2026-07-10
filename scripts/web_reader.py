"""
Web Reader
Jednoduchý čtecí modul pro Hanse — stahuje a sumarizuje obsah z webu.

Zdroje:
  - Wikipedia API  (bez klíče, CZ + EN)
  - RSS feeds      (ČT24, Novinky, BBC Czech)
  - Přímá URL      (libovolná stránka)

Sumarizace probíhá přes lokální Ollama (stejný model jako chat).
Výstup jde do Hansova deníku jako "přečtená věc".
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

_log = __import__("scripts.logger", fromlist=["get_logger"]).get_logger("web_reader")

# HANS_STUDY_DEEP_V1 — generické odkazy bez studijní hodnoty (vynech z pododkazů)
_LINK_NOISE = {
    "zeměpisné souřadnice", "geografické souřadnice", "souřadnicový systém",
    "rozcestník", "spojené království", "česko", "anglicky", "latina",
    "iso 3166", "wikidata",
}

# RSS zdroje — Hans je čte podle nálady / zájmů
RSS_FEEDS = {
    "zpravy":    "https://ct24.ceskatelevize.cz/rss/hlavni-zpravy",
    "veda":      "https://ct24.ceskatelevize.cz/rss/veda",
    "kultura":   "https://ct24.ceskatelevize.cz/rss/kultura",
    "pocasi":    "https://www.yr.no/en/forecast/daily-table/2-3068799/Czech%20Republic/Pardubice%20Region/Pardubice",
}

WIKIPEDIA_API = "https://cs.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_API_EN = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
WIKIPEDIA_SEARCH = "https://cs.wikipedia.org/w/api.php"


@dataclass
class ReadResult:
    source: str          # "wikipedia", "rss", "url"
    title: str
    url: str
    raw_text: str        # surový text (může být dlouhý)
    summary: str         # sumarizovaný výstup pro Hanse
    topic: str           # co to spustilo ("kodi", "object", "interest", "news")
    fetched_at: float = 0.0

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = time.time()


class WebReader:
    """
    Stahuje a sumarizuje webový obsah pro Hanse.
    Používá Ollama pro sumarizaci — stejný endpoint jako chat.
    """

    def __init__(self, config: dict):
        self.config     = config
        self._ollama    = config.get("openwebui_chat", {}).get(
                            "base_url", "http://127.0.0.1:11434")
        self._model     = config.get("hans_dialog", {}).get(
                            "ollama_model",
                            config.get("openwebui_chat", {}).get(
                                "model_name", "jobautomation/OpenEuroLLM-Czech:latest"))
        self._timeout   = 15
        from scripts.hans_persona import persona_core  # PERSONA_REFACTOR_1_4
        self._persona   = persona_core(config)
        self._sess      = requests.Session()
        self._sess.headers.update({
            "User-Agent": "HansBot/1.0 (home assistant; educational use)"
        })

    # ── Wikipedia ─────────────────────────────────────────────────────────────

    def wikipedia(self, query: str, lang: str = "cs") -> Optional[ReadResult]:
        """
        Hledá na Wikipedii a vrátí sumarizovaný výsledek.
        Nejprve zkusí CZ, pak EN.
        """
        # Hledej přes search API
        title = self._wikipedia_search(query, lang)
        if not title:
            if lang == "cs":
                return self.wikipedia(query, lang="en")
            return None

        api = WIKIPEDIA_API if lang == "cs" else WIKIPEDIA_API_EN
        try:
            r = self._sess.get(api.format(title=title), timeout=self._timeout)
            if r.status_code != 200:
                return None
            data    = r.json()
            extract = data.get("extract", "")
            page_url = data.get("content_urls", {}).get("desktop", {}).get("page", "")
            if not extract or len(extract) < 100:
                return None

            summary = self._summarize(
                text  = extract[:2000],
                query = query,
                style = "Napiš 1-2 věty co tě zaujalo, jako by sis dělal poznámku."
            )
            return ReadResult(
                source   = "wikipedia",
                title    = data.get("title", title),
                url      = page_url,
                raw_text = extract,
                summary  = summary,
                topic    = "wikipedia",
            )
        except Exception as e:
            _log.warning("Wikipedia fetch error: %s", e)
            return None

    def wikipedia_read(self, query: str, lang: str = "cs",
                       max_chars: int | None = None) -> Optional["ReadResult"]:
        """CURIOSITY_DEEP_V1 — zvídavé čtení z CELÉHO článku (ne jen lead).
        Lehčí sourozenec `wikipedia()`: vrací stejný ReadResult, ale poznámka
        vzniká z většího těla článku (anti-mělkost — Hansova každodenní znalost
        nestojí jen na úvodu). cs→en fallback řeší wikipedia_article uvnitř."""
        if max_chars is None:
            max_chars = int(
                self.config.get("curiosity", {}).get("read_max_chars", 6000))
        art = self.wikipedia_article(query, lang=lang, max_chars=max_chars)
        if not art or not (art.get("text") or "").strip():
            return None
        text = art["text"]
        # HANS_READING_GROUNDING_V1 — poznámka nejdřív UKOTVÍ, kdo/co téma je
        # (z úvodu článku), pak teprve detail. Bez identity-kotvy zůstal v RAG
        # jen náhodný detail → na pozdější „co je X?“ Hans konfabuloval (fantom
        # „AJ II × protein“). Předáváme VYŘEŠENÝ titul článku, ne surový dotaz.
        summary = self._summarize(
            text  = text,
            query = art["title"],
            style = ("Nejdřív JEDNOU větou stručně uveď, KDO nebo CO to je "
                     "(podle úvodu článku, např. „… je/byl …“). Pak JEDNOU "
                     "větou napiš, co tě z článku nejvíc zaujalo. Drž se faktů "
                     "z textu, nic si nepřidávej."),
            max_text = max_chars,
        )
        return ReadResult(
            source   = "wikipedia",
            title    = art["title"],
            url      = art["url"],
            raw_text = text,
            summary  = summary,
            topic    = "wikipedia",
        )

    def _wikipedia_search(self, query: str, lang: str = "cs") -> Optional[str]:
        """Vrátí název nejlepší stránky pro dotaz."""
        api = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            r = self._sess.get(api, params={
                "action": "query", "list": "search",
                "srsearch": query, "format": "json",
                "srlimit": 1, "srnamespace": 0,
            }, timeout=self._timeout)
            hits = r.json().get("query", {}).get("search", [])
            if hits:
                return hits[0]["title"]
        except Exception as e:
            _log.debug("Wikipedia search error: %s", e)
        return None

    # ── Wikipedia hloubkové čtení (HANS_STUDY_DEEP_V1) ──────────────────────────

    def wikipedia_article(self, query: str, lang: str = "cs",
                          max_chars: int = 12000) -> Optional[dict]:
        """Najde nejlepší stránku a vrátí PLNÝ plaintext článku (ne jen lead).
        cs→en fallback. Vrací {page_title, title, url, text, lang} nebo None.
        Bez LLM — jen stažení (sumarizaci/poznámku dělá volající)."""
        title = self._wikipedia_search(query, lang)
        if not title:
            if lang == "cs":
                return self.wikipedia_article(query, lang="en", max_chars=max_chars)
            return None
        extract = self._wiki_extract(title, lang, intro_only=False)
        if not extract or len(extract) < 120:
            if lang == "cs":
                return self.wikipedia_article(query, lang="en", max_chars=max_chars)
            return None
        url = (f"https://{lang}.wikipedia.org/wiki/"
               + requests.utils.quote(title.replace(" ", "_")))
        return {
            "page_title": title,
            "title": title,
            "url": url,
            "text": extract[:max_chars],
            "lang": lang,
        }

    def wikipedia_image(self, page_title: str, lang: str = "cs") -> Optional[str]:
        """HANS_ART_PERSON_LIKENESS_V3 — URL hlavního obrázku článku (lead image
        = obvykle portrét osoby). Original, fallback thumbnail 1024. None když
        článek obrázek nemá."""
        api = f"https://{lang}.wikipedia.org/w/api.php"
        for piprop, extra in (("original", {}),
                              ("thumbnail", {"pithumbsize": 1024})):
            try:
                params = {"action": "query", "titles": page_title,
                          "prop": "pageimages", "piprop": piprop,
                          "redirects": 1, "format": "json",
                          "formatversion": 2}
                params.update(extra)
                r = self._sess.get(api, params=params, timeout=self._timeout)
                pages = r.json().get("query", {}).get("pages", [])
                if pages and isinstance(pages, list):
                    node = pages[0].get(piprop) or {}
                    src = node.get("source")
                    if src:
                        return src
            except Exception as e:
                _log.debug("wikipedia_image error (%s/%s): %s",
                           page_title, piprop, e)
        return None

    def wikipedia_lead_links(self, page_title: str, lang: str = "cs",
                             limit: int = 6) -> list[str]:
        """Odkazy z ÚVODNÍ sekce článku v POŘADÍ VÝSKYTU (= nejcentrálnější
        pojmy první, ne abecedně). Parsuje HTML lead sekce — `prop=links` by
        vrátil odkazy abecedně. Jen články (ne File:/Help:/kotvy/rozcestníky)."""
        import urllib.parse as _up
        api = f"https://{lang}.wikipedia.org/w/api.php"
        try:
            r = self._sess.get(api, params={
                "action": "parse", "page": page_title, "prop": "text",
                "section": "0", "format": "json", "redirects": 1,
            }, timeout=self._timeout)
            html = r.json().get("parse", {}).get("text", {}).get("*", "")
        except Exception as e:
            _log.debug("wikipedia_lead_links error (%s): %s", page_title, e)
            return []
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        out = []
        seen = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("/wiki/"):
                continue
            raw = href[len("/wiki/"):]
            if ":" in raw:           # File:, Help:, Wikipedie: …
                continue
            t = _up.unquote(raw.split("#", 1)[0]).replace("_", " ").strip()
            tl = t.lower()
            if not t or tl in seen:
                continue
            if any(t.startswith(p) for p in ("Seznam ", "List of ")):
                continue
            if tl in _LINK_NOISE:    # generické nesouvisející odkazy
                continue
            out.append(t)
            seen.add(tl)
            if len(out) >= limit:
                break
        return out

    def wikipedia_intro(self, page_title: str, lang: str = "cs",
                        max_chars: int = 3000) -> str:
        """Úvodní (lead) plaintext konkrétní stránky podle PŘESNÉHO názvu."""
        extract = self._wiki_extract(page_title, lang, intro_only=True)
        return (extract or "")[:max_chars]

    def _wiki_extract(self, page_title: str, lang: str = "cs",
                      intro_only: bool = False) -> str:
        """prop=extracts plaintext daného názvu. intro_only → jen lead."""
        api = f"https://{lang}.wikipedia.org/w/api.php"
        params = {
            "action": "query", "prop": "extracts", "explaintext": 1,
            "redirects": 1, "titles": page_title, "format": "json",
            "formatversion": 2,
        }
        if intro_only:
            params["exintro"] = 1
        try:
            r = self._sess.get(api, params=params, timeout=self._timeout)
            pages = r.json().get("query", {}).get("pages", [])
            if pages and isinstance(pages, list):
                return (pages[0].get("extract") or "").strip()
        except Exception as e:
            _log.debug("_wiki_extract error (%s): %s", page_title, e)
        return ""

    # ── RSS ───────────────────────────────────────────────────────────────────

    def rss_headlines(self, feed_key: str = "zpravy",
                      max_items: int = 5) -> list[dict]:
        """
        Stáhne RSS feed a vrátí seznam {title, description, link}.
        """
        url = RSS_FEEDS.get(feed_key)
        if not url:
            return []
        try:
            r = self._sess.get(url, timeout=self._timeout)
            soup = BeautifulSoup(r.content, "xml")
            items = []
            for item in soup.find_all("item")[:max_items]:
                items.append({
                    "title":       item.find("title").text.strip() if item.find("title") else "",
                    "description": item.find("description").text.strip() if item.find("description") else "",
                    "link":        item.find("link").text.strip() if item.find("link") else "",
                })
            return items
        except Exception as e:
            _log.warning("RSS fetch error (%s): %s", feed_key, e)
            return []

    def rss_summary(self, feed_key: str = "zpravy") -> Optional[ReadResult]:
        """
        Stáhne RSS, vybere nejzajímavější položku a sumarizuje.
        """
        items = self.rss_headlines(feed_key, max_items=8)
        if not items:
            return None

        # Poskládej text pro LLM
        headlines = "\n".join(f"- {it['title']}: {it['description'][:120]}"
                              for it in items if it["title"])
        summary = self._summarize(
            text  = headlines,
            query = f"zprávy ({feed_key})",
            style = (
                "Vybral sis jednu zprávu která tě jako formálního anglického majordomuse "
                "zaujala. Napiš jednu větu co to bylo a proč."
            )
        )
        return ReadResult(
            source   = "rss",
            title    = f"Zprávy: {feed_key}",
            url      = RSS_FEEDS.get(feed_key, ""),
            raw_text = headlines,
            summary  = summary,
            topic    = "news",
        )

    # ── Přímá URL ─────────────────────────────────────────────────────────────

    def fetch_url(self, url: str, topic: str = "url") -> Optional[ReadResult]:
        """
        Stáhne libovolnou URL, extrahuje čitelný text, sumarizuje.
        """
        try:
            r = self._sess.get(url, timeout=self._timeout)
            soup = BeautifulSoup(r.content, "html.parser")

            # Odstraň skripty, styly, nav
            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()

            # Extrahuj odstavce
            paragraphs = [p.get_text(" ", strip=True)
                          for p in soup.find_all("p")
                          if len(p.get_text(strip=True)) > 60]
            text = "\n".join(paragraphs[:20])
            if not text:
                text = soup.get_text(" ", strip=True)[:3000]

            title = (soup.find("title").text.strip()
                     if soup.find("title") else url)

            summary = self._summarize(
                text  = text[:2500],
                query = url,
                style = "Shrň v 1-2 větách co bylo na stránce zajímavého."
            )
            return ReadResult(
                source   = "url",
                title    = title,
                url      = url,
                raw_text = text,
                summary  = summary,
                topic    = topic,
            )
        except Exception as e:
            _log.warning("URL fetch error (%s): %s", url, e)
            return None

    # ── Sumarizace přes Ollama ────────────────────────────────────────────────

    def _summarize(self, text: str, query: str, style: str,
                   max_text: int = 1500) -> str:
        """
        Pošle text na Ollama a vrátí sumarizaci v Hansově stylu.
        Fallback: první 2 věty raw textu.
        max_text = kolik znaků textu se pošle modelu (CURIOSITY_DEEP_V1 —
        u hloubkového čtení 6k+, jinak výchozí 1500 = jen úvod).
        """
        user_prompt = (
            f"Přečetl sis text o tématu '{query}'.\n"
            f"{style}\n"
            f"Odpovídej česky, max 2 věty, bez uvozovek.\n\n"
            f"Text:\n{text[:max_text]}"
        )
        # OLLAMA_CLIENT_PATCH_WEBREADER
        options = {"num_predict": 100}
        if max_text > 2000:  # CURIOSITY_DEEP_V1 — větší vstup potřebuje širší ctx
            options["num_ctx"] = int(
                self.config.get("curiosity", {}).get("read_num_ctx", 8192))
        from scripts.ollama_client import ollama_chat
        try:
            result = ollama_chat(
                self._model,
                [
                    {"role": "system", "content": self._persona},
                    {"role": "user",   "content": user_prompt},
                ],
                ollama_url=self._ollama,
                options=options,
            )
            if result:
                return result
        except Exception as e:
            _log.warning("Summarize error: %s", e)

        # Fallback — první dvě věty raw textu
        sentences = re.split(r"(?<=[.!?])\s+", text.strip())
        return " ".join(sentences[:2])