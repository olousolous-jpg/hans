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


# ── HANS_WIKI_TITLE_MATCH_V1 (17.7.) — title-similarity helpery ─────────────
_TITLE_STOPWORDS = frozenset({
    "a", "i", "o", "u", "v", "ve", "z", "ze", "k", "ke", "s", "se", "do",
    "na", "po", "za", "od", "je", "jsou", "byl", "byla", "být", "pro", "si",
    "the", "and", "of", "in", "on", "at", "to", "for", "by", "with", "an",
})


def _title_tokens(text: str) -> list:
    """Rozseká text na obsahová slova (bez diakritiky, bez interpunkce, bez
    stopwordů, min 3 znaky). Používá se pro title similarity match."""
    import unicodedata
    if not text:
        return []
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t.lower())
    return [w for w in t.split()
            if len(w) >= 3 and w not in _TITLE_STOPWORDS]


def _token_match(a: str, b: str) -> bool:
    """Fuzzy match dvou tokenů — přesná shoda nebo prefix ≥4 znaky (chytí
    české skloňování: „architektonické" ↔ „architektura")."""
    if a == b:
        return True
    n = min(len(a), len(b))
    if n < 4:
        return False
    for k in range(n, 3, -1):
        if a[:k] == b[:k]:
            return True
    return False


def _title_similarity(query: str, title: str) -> float:
    """Kolik query tokens má odpovídající token v titulu (0.0–1.0). Substring
    match query v titulu = auto 1.0 (např. „Icon of the Seas" ↔ „Icon of the
    Seas (loď)")."""
    if not query or not title:
        return 0.0
    if query.strip().lower() in title.lower():
        return 1.0
    q = _title_tokens(query)
    t = _title_tokens(title)
    if not q:
        return 0.0
    matched = 0
    for qt in q:
        for tt in t:
            if _token_match(qt, tt):
                matched += 1
                break
    return matched / len(q)

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
    "pocasi":    "https://www.yr.no/en/forecast/daily-table/1-72088/Czech%20Republic/Prague/Prague",
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
    # HANS_DEFERRED_SUMMARY_V1 — mozek byl mimo → summary se NEVYROBILA.
    # raw_text se PODRŽÍ, poznatek se doplní v catchup, až Ollama naběhne.
    # NIKDY nevydávat raw_text za Hansův poznatek.
    pending: bool = False

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
                summary  = summary or "",       # HANS_DEFERRED_SUMMARY_V1
                pending  = summary is None,
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
        # HANS_WEBREAD_DEEPER_V1 (20.7.): hluboké čtení = hlubší poznatek.
        # Dřív 2 věty (identita + 1 detail) z 6k zn článku → recall mělký.
        # Teď 3-5 vět: identita + 2-3 konkrétní zajímavosti. Config
        # `curiosity.read_summary_sentences` (default 5) laditelné.
        _sent = int(self.config.get("curiosity", {}).get(
            "read_summary_sentences", 5))
        summary = self._summarize(
            text  = text,
            query = art["title"],
            style = ("Nejdřív JEDNOU větou stručně uveď, KDO nebo CO to je "
                     "(podle úvodu článku, např. „… je/byl …“). Pak ve "
                     "2 až 4 větách napiš KONKRÉTNÍ věci, které tě z článku "
                     "nejvíc zaujaly — fakta, souvislosti, detaily, ne obecné "
                     "dojmy. Drž se faktů z textu, nic si nepřidávej."),
            max_text = max_chars,
            max_sentences = _sent,
            num_predict = int(self.config.get("curiosity", {}).get(
                "read_summary_num_predict", 260)),
        )
        return ReadResult(
            source   = "wikipedia",
            title    = art["title"],
            url      = art["url"],
            raw_text = text,
            summary  = summary or "",       # HANS_DEFERRED_SUMMARY_V1
            pending  = summary is None,
            topic    = "wikipedia",
        )

    def _wikipedia_search(self, query: str, lang: str = "cs") -> Optional[str]:
        """HANS_WIKI_TITLE_MATCH_V1 (17.7.) — vrátí NÁZEV STRÁNKY, která reálně
        odpovídá tématu, ne první srsearch hit slepě.

        Dřívější `srlimit=1` bralo top hit i když byl mimo („Kreativní coding a"
        → JavaFX; „Architektonické vlivy" → Sursockovo muzeum). Study to zapsalo
        jako platný material → konfabulace přes nesouvisející zdroj.

        Nová strategie:
          1. `prefixsearch` — striktní, chytí kanonický titul (WCAG → WCAG).
          2. `srsearch` (top 5) + title-similarity gate (min score
             `curiosity.wiki_title_min_score`, default 0.5). Bere hit s
             nejvyšší shodou; pod threshold → None (raději žádný článek než
             irrelevantní).
        """
        api = f"https://{lang}.wikipedia.org/w/api.php"
        # 1) prefixsearch — přesné začátky titulů
        try:
            r = self._sess.get(api, params={
                "action": "query", "list": "prefixsearch",
                "pssearch": query, "format": "json",
                "pslimit": 3, "psnamespace": 0,
            }, timeout=self._timeout)
            pfx = r.json().get("query", {}).get("prefixsearch", [])
            if pfx:
                return pfx[0]["title"]
        except Exception as e:
            _log.debug("Wikipedia prefixsearch error: %s", e)
        # 2) srsearch s title-similarity filtrem
        try:
            r = self._sess.get(api, params={
                "action": "query", "list": "search",
                "srsearch": query, "format": "json",
                "srlimit": 5, "srnamespace": 0,
            }, timeout=self._timeout)
            hits = r.json().get("query", {}).get("search", [])
            if not hits:
                return None
            # threshold 0.6 = min 60% query tokens musí být v titulu. Nastaveno
            # po ostrém testu (0.5 pouštělo „Technologie 3D rekonstrukce" →
            # „Pozemské technologie ve Hvězdné bráně" jen na shodu „technologie").
            min_score = float((self.config.get("curiosity", {}) or {})
                              .get("wiki_title_min_score", 0.6))
            best_title = None
            best_score = 0.0
            for h in hits:
                t = h.get("title") or ""
                s = _title_similarity(query, t)
                if s > best_score:
                    best_score = s
                    best_title = t
            if best_title and best_score >= min_score:
                return best_title
            _log.debug("Wikipedia srsearch: best %r score=%.2f < %.2f — skip",
                       best_title, best_score, min_score)
        except Exception as e:
            _log.debug("Wikipedia srsearch error: %s", e)
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
            summary  = summary or "",       # HANS_DEFERRED_SUMMARY_V1
            pending  = summary is None,
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
                summary  = summary or "",       # HANS_DEFERRED_SUMMARY_V1
                pending  = summary is None,
                topic    = topic,
            )
        except Exception as e:
            _log.warning("URL fetch error (%s): %s", url, e)
            return None

    # ── Sumarizace přes Ollama ────────────────────────────────────────────────

    def _summarize(self, text: str, query: str, style: str,
                   max_text: int = 1500, max_sentences: int = 2,
                   num_predict: int = 100) -> Optional[str]:
        """
        Pošle text na Ollama a vrátí sumarizaci v Hansově stylu.
        HANS_DEFERRED_SUMMARY_V1 — když mozek NEODPOVÍ, vrátí None
        (dřív vracel první 2 věty RAW textu → ty se ukládaly jako Hansův
        poznatek = tichá kontaminace paměti; volající to teď odloží jako
        `pending` a doplní v catchup). NIKDY nefabrikovat shrnutí z raw textu.
        max_text = kolik znaků textu se pošle modelu (CURIOSITY_DEEP_V1 —
        u hloubkového čtení 6k+, jinak výchozí 1500 = jen úvod).

        HANS_WEBREAD_DEEPER_V1 (20.7.): délka výstupu je parametrická
        (max_sentences/num_predict). Hluboké čtení (`wikipedia_read`, 6k zn
        vstupu) dřív zaškrceno na 2 věty / num_predict 100 → mělký poznatek,
        z něhož pak čerpá recall/RAG. Deep read teď 3-5 vět; lehké čtení
        (lead, fetch_url) zůstává 2 věty.
        """
        user_prompt = (
            f"Přečetl sis text o tématu '{query}'.\n"
            f"{style}\n"
            f"Odpovídej česky, max {max_sentences} věty, bez uvozovek.\n\n"
            f"Text:\n{text[:max_text]}"
        )
        # OLLAMA_CLIENT_PATCH_WEBREADER
        options = {"num_predict": num_predict}
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

        # HANS_DEFERRED_SUMMARY_V1 — mozek mimo (nebo herní mód) → NEfabrikuj.
        # Volající odloží raw_text jako pending a doplní v catchup.
        return None