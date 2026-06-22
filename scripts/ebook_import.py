"""
EBOOK_IMPORT_V1 — uživatel nahraje vlastní ebooky, Hans je zařadí ke čtení.

Uživatel hodí soubory (.epub / .mobi / .azw / .azw3 / .pdb / .txt) do
`data/user_books/`. Při startu (volá se z hans_idle PŘED HansLibrary) se:
  1. vytáhne čistý text (EPUB = zip+bs4, MOBI = mobi lib + bs4, TXT přímo),
  2. zapíše do cache `data/library/<book_id>.txt` (= místo, kam píše _download_book,
     takže existující reader ho vezme bez stahování),
  3. zaregistruje do `config["hans_library"]["custom_books"]` V PAMĚTI (ne do
     config.json — drop folder je SSOT, re-sken při každém bootu, idempotentní).

Priorita NÍZKÁ: tag 'user_upload' nezapadá do Hansových zájmů → tendency ruleta
(_pick_book_by_tendencies) ho vybírá zřídka (hlavně přes ~25 % exploraci). Hans si
dál vybírá vlastní knihy podle cesty k Severce; nahrané jsou jen doplněk.

Deferral-safe: extrakce v try/except, selhání 1 souboru neshodí ostatní ani start.
"""

import glob
import json
import logging
import os
import re

_log = logging.getLogger("ebook_import")

USER_DIR = os.path.join("data", "user_books")
LIB_DIR = os.path.join("data", "library")
_EXTS = (".epub", ".mobi", ".azw", ".azw3", ".azw4", ".prc", ".pdb", ".txt")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return ("user_" + s)[:60] or "user_book"


def _clean_title(filename: str) -> str:
    """Z názvu souboru udělá čitelný titul."""
    base = os.path.basename(filename)
    base = re.sub(r"\.(epub|mobi|azw3?|azw4|prc|pdb|txt)$", "", base, flags=re.I)
    base = re.sub(r"\.(epub|mobi|azw3?|pdb|prc)$", "", base, flags=re.I)  # dvojitá přípona .pdb.mobi
    base = base.replace("_", " ").replace("-", " – ")
    base = re.sub(r"\s+", " ", base).strip()
    return base[:120] or "Nahraná kniha"


# ── extrakce textu dle formátu ──────────────────────────────────────────────
def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.extract()
    return soup.get_text("\n")


def _extract_epub(path: str) -> str:
    """EPUB = ZIP s XHTML. Čte spine z OPF (pořadí kapitol), fallback sorted html."""
    import zipfile
    from bs4 import BeautifulSoup
    parts = []
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        order = []
        # zkus OPF spine pro správné pořadí
        opf = next((n for n in names if n.lower().endswith(".opf")), None)
        if opf:
            try:
                soup = BeautifulSoup(z.read(opf), "lxml-xml")
                base = os.path.dirname(opf)
                idmap = {it.get("id"): it.get("href") for it in soup.find_all("item")}
                for ref in soup.find_all("itemref"):
                    href = idmap.get(ref.get("idref"))
                    if href:
                        full = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
                        if full in names:
                            order.append(full)
            except Exception as e:
                _log.debug("epub opf parse: %s", e)
        if not order:
            order = sorted(n for n in names if n.lower().endswith((".xhtml", ".html", ".htm")))
        for n in order:
            try:
                parts.append(_html_to_text(z.read(n).decode("utf-8", "ignore")))
            except Exception:
                pass
    return "\n\n".join(parts)


def _extract_mobi(path: str) -> str:
    """MOBI/AZW/PDB přes `mobi` lib → HTML → text. Uklidí temp dir."""
    import shutil
    import mobi
    tmp, _fp = mobi.extract(path)
    try:
        htmls = sorted(set(
            glob.glob(os.path.join(tmp, "**", "*.html"), recursive=True)
            + glob.glob(os.path.join(tmp, "**", "*.htm"), recursive=True)
            + glob.glob(os.path.join(tmp, "**", "*.xhtml"), recursive=True)))
        parts = []
        for h in htmls:
            try:
                parts.append(_html_to_text(open(h, encoding="utf-8", errors="ignore").read()))
            except Exception:
                pass
        return "\n\n".join(parts)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".txt":
        return open(path, encoding="utf-8", errors="ignore").read()
    if ext == ".epub":
        return _extract_epub(path)
    # .mobi/.azw/.azw3/.azw4/.prc/.pdb → mobi lib
    return _extract_mobi(path)


def _norm(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── EBOOK_AUTOTAG_V1 — z obsahu knihy odvoď témata (aby Hans věděl, o čem je) ─
_TAG_SYSTEM = (
    "Z úryvku knihy urči 3 až 6 STRUČNÝCH českých tagů jejího žánru a hlavních témat "
    "(jednoslovných nebo dvouslovných), oddělených čárkami. Vrať POUZE tagy, nic "
    "jiného, žádný úvod. Např.: fantasy, dobrodružství, dospívání, magie."
)


def _derive_tags(config: dict, text: str) -> list:
    """qwen z úvodu knihy odvodí 3-6 českých tematických tagů. [] při selhání."""
    if not text or len(text) < 200:
        return []
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return []
    model = (config.get("models", {}) or {}).get("analysis") or "qwen2.5:7b"
    snippet = text[:2500]
    try:
        raw = ollama_generate(model, "Úryvek knihy:\n%s\n\nTagy:" % snippet,
                              system=_TAG_SYSTEM, config=config,
                              timeout=90, keep_alive=0)
    except Exception as e:
        _log.warning("ebook_import: auto-tag LLM failed: %s", e)
        return []
    if not raw:
        return []
    tags = [t.strip().lower() for t in re.split(r"[,;\n]", raw) if t.strip()]
    tags = [t for t in tags if 2 <= len(t) <= 30][:6]
    return tags


# ── hlavní entry ────────────────────────────────────────────────────────────
def import_user_books(config: dict, user_dir: str = USER_DIR,
                      lib_dir: str = LIB_DIR) -> list:
    """Naskenuje user_dir, vytáhne text do cache a zaregistruje knihy do
    config['hans_library']['custom_books'] (v PAMĚTI). Vrací list nových titulů.
    Idempotentní: už zaregistrované (book_id v custom_books) přeskočí."""
    if not os.path.isdir(user_dir):
        return []
    os.makedirs(lib_dir, exist_ok=True)
    libcfg = config.setdefault("hans_library", {})
    custom = libcfg.setdefault("custom_books", [])
    known_ids = {b.get("id") for b in custom if isinstance(b, dict)}

    files = sorted(f for f in glob.glob(os.path.join(user_dir, "*"))
                   if f.lower().endswith(_EXTS) and os.path.isfile(f))
    added = []
    for f in files:
        bid = _slug(os.path.basename(f))
        if bid in known_ids:
            continue  # už zaregistrovaná
        cache = os.path.join(lib_dir, bid + ".txt")
        tags_path = os.path.join(lib_dir, bid + ".tags.json")
        try:
            text = None
            if not os.path.exists(cache) or os.path.getsize(cache) < 500:
                _log.info("ebook_import: extrahuji %s", os.path.basename(f))
                text = _norm(_extract_text(f))
                if len(text) < 500:
                    _log.warning("ebook_import: %s → málo textu (%d zn), přeskakuji",
                                 os.path.basename(f), len(text))
                    continue
                with open(cache, "w", encoding="utf-8") as wf:
                    wf.write(text)
            # EBOOK_AUTOTAG_V1 — odvoď témata (sidecar cache → deriv jen jednou)
            tags = []
            if os.path.exists(tags_path):
                try:
                    tags = json.load(open(tags_path, encoding="utf-8"))
                except Exception:
                    tags = []
            if not tags:
                if text is None:
                    try:
                        text = open(cache, encoding="utf-8", errors="ignore").read()
                    except Exception:
                        text = ""
                tags = _derive_tags(config, text)
                if tags:
                    try:
                        json.dump(tags, open(tags_path, "w", encoding="utf-8"),
                                  ensure_ascii=False)
                    except Exception:
                        pass
                    _log.info("ebook_import: tagy „%s\": %s", _clean_title(f), ", ".join(tags))
            entry = {
                "id": bid,
                "title": _clean_title(f),
                "author": "nahráno uživatelem",
                "lang": "cs",
                "url": "",                       # cache existuje → _download_book ji vrátí
                "tags": tags or [],              # AUTO-TAG: reálná témata (sémantika #2 je matchuje)
                "user_upload": True,             # příznak → nízká priorita (dampener ve výběru)
            }
            custom.append(entry)
            known_ids.add(bid)
            added.append(entry["title"])
            _log.info("ebook_import: zaregistrováno „%s\" (id=%s)", entry["title"], bid)
        except Exception as e:
            _log.warning("ebook_import: %s selhalo: %s", os.path.basename(f), e)
    if added:
        _log.info("ebook_import: přidáno %d nahraných knih: %s", len(added), ", ".join(added))
    return added


# ── ruční test: python3 -m scripts.ebook_import ─────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cfg = json.load(open("config.json"))
    res = import_user_books(cfg)
    print("Naimportováno:", res or "(nic nového)")
    print("custom_books teď:", [b["title"] for b in cfg["hans_library"]["custom_books"]])
