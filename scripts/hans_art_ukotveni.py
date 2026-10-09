"""Funkce přesunuté z `scripts/hans_art.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_ha.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Optional
import os
import re
import time
import urllib.request

def _en_name(name: str, lang: str = "cs") -> str:
    """HANS_ART_EN_TITLE_V1 — ANGLICKÝ/franšízový název přes Wiki langlink
    („Mimoni"→„Minions", „Cardiffský hrad"→„Cardiff Castle"). SDXL nerozumí
    českým jménům → u fikce i reálií pak namaluje ROZPOZNATELNOU věc, ne
    generickou scénu. '' když langlink není nebo je stejný."""
    n = (name or "").strip()
    if not n or lang == "en":
        return ""
    try:
        from scripts.hans_study import _en_title
        en = (_en_title(n, lang=lang) or "").strip()
        # rozcestník = nejednoznačné → k ničemu
        if not en or re.search(r"\(disambiguation\)|rozcestník|may refer to",
                               en, re.I):
            return ""
        # strhni Wiki disambiguační příponu „(film)"/„(character)" — není součást jména
        en = re.sub(r"\s*\([^)]*\)\s*$", "", en).strip()
        if en and en.lower() != n.lower():
            return en
    except Exception as e:
        _ha._log.debug("art: _en_name(%s) selhal: %s", n, e)
    return ""


def _ground_via_wikipedia(config: dict, db_path: str, subject: str) -> str:
    """HANS_ART_SUBJECT_GROUNDING_V2 — když námět není v Hansově čtení (C1 miss),
    dohledej ho na Wikipedii a ROVNOU ulož do entity store (příště už zná; pomůže
    i chatu). Vrací „Titul: definiční věta" nebo '' (nenalezeno/nevhodné)."""
    low = (subject or "").strip().lower()
    # jen konkrétní pojmenované věci — ne konverzační seed / dlouhé fráze /
    # POPISNÉ SCÉNY (4+ slov = „klidná krajina s řekou za soumraku" NENÍ entita,
    # jinak se resolvne na náhodného malíře krajin a pollutuje store).
    if (not subject or len(subject) > 60 or len(subject.split()) > 3
            or low.startswith(("náš rozhovor", "dojem", "nas rozhovor"))):
        return ""
    try:
        from scripts.web_reader import WebReader
        from scripts.hans_entities import EntityStore, _first_sentence
        lang = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
        art = WebReader(config).wikipedia_article(subject, lang=lang,
                                                  max_chars=1500)
        if not art or not (art.get("text") or "").strip():
            return ""
        # ulož do entity store (čistý glos ze zdroje = anti-konfab)
        try:
            EntityStore(config, db_path).capture_from_reading(
                art["title"], art["text"], url=art.get("url", ""),
                lang=art.get("lang", lang))
        except Exception:
            pass
        gloss = _first_sentence(art["text"])
        # rozcestník („Bauhaus má více významů") = k ničemu → ber jako miss
        if re.search(r"(m[aá] více význam|může (být|označovat|odkazovat)|"
                     r"rozcestník|may refer to|several meanings)", gloss, re.I):
            _ha._log.debug("art: '%s' → rozcestník, přeskakuji", subject)
            return ""
        if gloss.strip():
            name = art["title"]
            _en = _ha._en_name(art.get("page_title", name),   # HANS_ART_EN_TITLE_V1
                           lang=art.get("lang", lang))
            if _en:
                name = "%s (English name to use in the image prompt: %s)" % (name, _en)
            _ha._log.info("art: namet '%s' dohledan na Wikipedii → '%s'%s "
                      "(ulozeno do entity store)", subject, art["title"],
                      f" [EN: {_en}]" if _en else "")
            return "%s: %s" % (name, gloss.strip())
    except Exception as e:
        _ha._log.debug("art: wiki grounding selhal: %s", e)
    return ""


# HANS_ART_SCENE_NO_GROUND_V1 — pozná KOMPOZIČNÍ scénu (víc prvků, „v pozadí",
# koordinace „a"), kterou NELZE scvrknout na jednu entitu. Doložený případ:
# „alej sakur a v pozadí japonskou svatyni" → loose match překlepu „svatini" na
# entitu „Svatba" přebil celý prompt → svatba místo sakur. Scéna jde RAW do
# scene-prompt LLM (ten víceprvkový popis zvládne). Diakritika volitelná.
def _looks_like_scene(s: str) -> bool:
    sl = " " + (s or "").lower() + " "
    if "pozad" in sl or "popred" in sl or "popřed" in sl:  # v pozadí / v popředí
        return True
    content = [w for w in sl.split() if len(w) >= 4]
    if " a " in sl and len(content) >= 3:                   # koordinace ≥3 prvků
        return True
    return False


def _wiki_character_appearance(config: dict, name: str) -> str:
    """Vytáhne z Wiki článku VĚTY o vzhledu postavy (oblečení, vlasy, rysy).
    Fiktivní postavy bez vlastního článku (Manka→Rumcajs) sdílí pasáž, která
    obvykle popisuje víc postav — vezmi ji, scene-LLM zdůrazní zadanou."""
    try:
        from scripts.web_reader import WebReader
        a = WebReader(config).wikipedia_article(name)
        if not a or not a.get("text"):
            return ""
        sents = re.split(r"(?<=[.!?])\s+", a["text"])
        # appearance věty PŘEDNOST (vzhled), doplň větami se jménem subjektu
        # (role/kontext — pomůže sub-postavám bez vlastního vzhledu: Cipísek).
        appear = [s.strip() for s in sents if _ha._APPEAR_KW.search(s)]
        named = [s.strip() for s in sents
                 if re.search(re.escape(name), s, re.I) and s.strip() not in appear]
        out = appear[:4] + named[:2]
        return " ".join(out)[:700]
    except Exception as e:
        _ha._log.debug("art: _wiki_character_appearance: %s", e)
        return ""


def _wiki_place_appearance(config: dict, name: str, max_chars: int = 700) -> str:
    """Vytáhne z Wiki článku věty o PODOBĚ místa (silueta, hmota, materiál).

    Tři věci, které se při stavbě ukázaly jako nutné (změřeno na 8 místech):
    (1) `max_chars=40000` — výchozích 12 000 znaků článek uřízne JEŠTĚ PŘED
        popisnou sekcí (ta stojí až za historií); u Kosti i Kolosea se do
        výřezu nevešla vůbec.
    (2) konec sekce = nadpis STEJNÉ nebo VYŠŠÍ úrovně. „Fyzický popis" má
        hned pod sebou podsekci → naivní „do dalšího ==" vrátilo 2 znaky.
    (3) ŽÁDNÝ fallback na klíčová slova mimo popisnou sekci. Vyzkoušeno
        a zahozeno: tahal do obrazového promptu majitele hradu („Zajícové
        z Hazmburka") a koloniální dějiny Zambie. Radši nic než historie —
        beze změny se chová jako dosud.
    """
    try:
        from scripts.web_reader import WebReader
        a = WebReader(config).wikipedia_article(name, max_chars=40000)
        txt = (a or {}).get("text") or ""
        if not txt:
            return ""
        out = _ha._place_appearance_from_text(txt, max_chars)
        if out:
            _ha._log.info("art: podoba místa '%s' — %d zn.", name, len(out))
        return out
    except Exception as e:
        _ha._log.debug("art: _wiki_place_appearance: %s", e)
        return ""


def _place_appearance_from_text(txt: str, max_chars: int = 700) -> str:
    """HANS_ART_PLACE_PURE_V1 — čistá půlka `_wiki_place_appearance` (bez sítě),
    aby šla tvrdit v regresní sadě. Tři pravidla, která tu drží, se dají snadno
    „zjednodušit" a rozbít TIŠE, proto mají každé svůj případ v regresi."""
    if not txt:
        return ""
    try:
        for pat in _ha._PLACE_SEC_PRIO:
            m = re.search(r"(=+)\s*(" + pat + r"[^=\n]{0,30}?)\s*=+", txt, re.I)
            if not m:
                continue
            uroven = len(m.group(1))
            konec = len(txt)
            for h in _ha._PLACE_HEAD.finditer(txt, m.end()):
                if len(h.group(1)) <= uroven:
                    konec = h.start()
                    break
            body = _ha._PLACE_HEAD.sub(" ", txt[m.end():konec]).strip()
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
            vis = [s for s in sents
                   if _ha._PLACE_VIS.search(s) and not _ha._PLACE_HIST.search(s)
                   and not _ha._PLACE_LIDE.search(s)]   # HANS_ART_PLACE_NO_PEOPLE_V1
            out, n = [], 0
            for s in vis:
                if n + len(s) > max_chars:
                    break
                out.append(s)
                n += len(s) + 1
                if len(out) >= 4:
                    break
            if out:
                _ha._log.info("art: podoba místa ze sekce '%s' (%d vět)",
                          m.group(2).strip(), len(out))
                return " ".join(out)
        return ""
    except Exception as e:
        _ha._log.debug("art: _place_appearance_from_text: %s", e)
        return ""


def _ground_subject(config: dict, db_path: str, subject: str) -> str:
    """HANS_ART_SUBJECT_GROUNDING_V1/V2 — zjisti, KOHO/CO malovat.
    Kaskáda: (1) C1 entity store (Hansovo čtení) → (2) Wikipedia fallback
    (a ulož do store). SDXL tak dostane informovaný popis místo holého jména
    („pan Sorge" → „Erich Robert Sorge: německý církevní hudebník a skladatel").
    Bez shody vrací syrový námět."""
    s = (subject or "").strip()
    if not s:
        return s
    # HANS_ART_SCENE_NO_GROUND_V1 — víceprvkovou scénu NEscvrkávej na entitu
    if _ha._looks_like_scene(s):
        return s
    s_clean = _ha._HONORIFIC.sub("", s).strip() or s
    try:
        from scripts.hans_entities import EntityStore
        es = EntityStore(config, db_path)
        # HANS_ART_COMMON_NOUN_V1 (5.8.) — OBECNÉ podstatné jméno se NEUKOTVUJE
        # na pojmenovanou entitu. Entity store je od TOHO, aby poznal JMÉNA
        # („pan Sorge", „Bud Spencer"); u obecného slova je shoda skoro vždy
        # náhoda. Doloženo 5.8.: „namaluj kočku" → entita „Kockums" (švédská
        # loděnice) → prompt „Saab Kockums shipyard in Malmö" → obraz PŘÍSTAVU
        # S JEŘÁBY. Prahem to opravit NELZE: „kocku" je regulérní prefix
        # „kockums", takže žádné pravidlo o délce prefixu to od českého
        # skloňování („hrad"→„hradu") neodliší. Rozlišovač, který k dispozici
        # JE: uživatel píše jména s velkým písmenem, obecná slova malým.
        # Bez velkého písmene se grounding přeskočí ÚPLNĚ (i Wikipedia — ta
        # dělá tutéž chybu) a FLUX beztak ví, jak vypadá kočka.
        # Souvisí HANS_ENTITY_SURNAME_PERSON_ONLY_V1 (táž třída, jiná větev).
        if not _ha._is_proper_name(s_clean, config):   # HANS_ART_NAME_CLASSIFY_V1
            # Obecné slovo → NEGROUNDOVAT VŮBEC (ani Wikipedií: „kočku" tam
            # padne na „Kockums", „psa" na „Psaní levou rukou" — týž prefix
            # problém). Syrový námět převede do angličtiny až scene-prompt,
            # což je přesně jeho práce, a FLUX ví, jak vypadá kočka.
            _ha._log.info("art: '%s' je obecné slovo — grounding přeskočen", s_clean)
            return s
        ent = es.resolve(s, loose=True) or es.resolve(s_clean, loose=True)
        gloss = (ent.get("gloss") if ent else "") or ""
        if gloss.strip():
            name = ent.get("name", s_clean)
            _wl = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
            _en = _ha._en_name(name, lang=_wl)          # HANS_ART_EN_TITLE_V1
            if _en:
                name = "%s (English name to use in the image prompt: %s)" % (name, _en)
            _ha._log.info("art: namet '%s' ukotven na entitu '%s'%s", s,
                      ent.get("name", s_clean), f" [EN: {_en}]" if _en else "")
            _base = "%s: %s" % (name, gloss.strip())
            # HANS_ART_CHAR_APPEARANCE_V1 — fiktivní postava (Rumcajs…), kterou
            # FLUX nezná: gloss je definice bez vzhledu → vytáhni z Wiki článku
            # VZHLED (červený klobouk, vousy…), ať má FLUX co malovat.
            if _ha._IS_CHARACTER.search(gloss):
                _app = _ha._wiki_character_appearance(config, ent.get("name", s_clean))
                if _app:
                    _ha._log.info("art: postava '%s' — vzhled z Wiki (%d zn)",
                              s_clean, len(_app))
                    _base = ("Vzhled postavy %s (ZDŮRAZNI ho v obraze): %s\n%s"
                             % (name, _app, _base))
            # HANS_ART_PLACE_APPEARANCE_V1 — místo (hrad, zřícenina, jezero):
            # gloss řekne, CO to je, ne JAK to vypadá. Brána je `etype` z entity
            # storu, ne regex nad prózou — užší a nelže.
            elif (ent.get("etype") or "") == "místo":
                _app = _ha._wiki_place_appearance(config, ent.get("name", s_clean))
                if _app:
                    _ha._log.info("art: místo '%s' — podoba z Wiki (%d zn)",
                              s_clean, len(_app))
                    _base = ("Podoba místa %s (ZDŮRAZNI ji v obraze): %s\n%s"
                             % (name, _app, _base))
            return _base
        # C1 miss → Wikipedia fallback (dohledej + ulož do store)
        enriched = _ha._ground_via_wikipedia(config, db_path, s_clean)
        if enriched:
            return enriched
    except Exception as e:
        _ha._log.debug("art: grounding námětu selhal: %s", e)
    return s


def tv_paint_subject(config: dict, db_path: str, now_playing: dict) -> tuple:
    """HANS_ART_TV_GROUNDING_V1 — nejlepší malovací NÁMĚT z běžícího pořadu.
    Dřív se malovalo jen z názvu → u pořadu bez popisku vznikla odpojená scéna
    („Vyprávěj, přijímačky" → černobílá cesta lesem). Kaskáda:
      (1) PLOT z Kodi (přímý popis děje) → nejlepší,
      (2) plot chybí → DOHLEDEJ popis na Wikipedii (i EN přes langlink),
      (3) ani to → jen název (a přiznat to volajícímu).
    Vrací (subject_pro_malbu, source_label). source_label = odkud popis je."""
    np = now_playing or {}
    title = (np.get("title") or np.get("label") or "").strip()
    show  = (np.get("showtitle") or "").strip()
    plot  = (np.get("plot") or np.get("plotoutline") or "").strip()
    # zobrazovaný název: „Seriál – epizoda" u dílu, jinak název
    disp = ("%s – %s" % (show, title)) if (show and title and
            show.lower() != title.lower()) else (title or show)
    if plot and len(plot) >= 40:
        return ("%s: %s" % (disp, plot[:400]), "z popisu pořadu")
    # plot chybí → internet (Wikipedia); zkus seriál i epizodní název
    for q in (show, title):
        q = (q or "").strip()
        if not q:
            continue
        g = _ha._ground_via_wikipedia(config, db_path, q)
        if g:
            _ha._log.info("art TV: popisek chyběl → dohledáno na Wikipedii pro '%s'", q)
            return (g, "z internetu (popisek u pořadu chyběl)")
    return (disp or title or show or "televizní obrazovka", "jen podle názvu")


def _style_from_study(config: dict, style: str) -> str:
    """HANS_ART_STYLE_V4 — popis stylu z HANSOVÝCH studijních poznámek (RAG
    hans_cetba/hans_identita — Hans studoval Design). Osobnější než Wikipedie.
    Vrátí chunk (≤300 zn) jen když se styl v textu opravdu vyskytuje, jinak ''."""
    try:
        from scripts.hans_knowledge import HansKnowledge
        from scripts.hans_entities import _norm
        kn = HansKnowledge(config)
        key = _norm(style)
        first = (key.split() or [""])[0]
        for col in ("hans_cetba", "hans_identita"):
            try:
                res = kn.query(col, style, 3, 0.8)
            except Exception:
                continue
            for ch in (getattr(res, "chunks", None) or []):
                t = (ch.get("text") or "").strip()
                if t and first and first[:6] in _norm(t):
                    _ha._log.info("art: styl '%s' ukotven z Hansova studia (%s)",
                              style, col)
                    return t[:300]
    except Exception as e:
        _ha._log.debug("art: style-from-study selhal: %s", e)
    return ""


def _ground_style(config: dict, db_path: str, style: str) -> str:
    """HANS_ART_STYLE_V4 — ukotvi umělecký styl (umělec/směr). Kaskáda:
    (1) C1 entity store → (2) Hansovy studijní poznámky (RAG) → (3) Wikipedia
    (uloží do store). Vrací krátký český popis stylu, fallback syrový styl."""
    s = (style or "").strip()
    if not s:
        return s
    try:
        from scripts.hans_entities import EntityStore
        ent = EntityStore(config, db_path).resolve(s, loose=True)
        if ent and (ent.get("gloss") or "").strip():
            return "%s: %s" % (ent["name"], ent["gloss"].strip())
    except Exception:
        pass
    note = _ha._style_from_study(config, s)
    if note:
        return note
    wiki = _ha._ground_via_wikipedia(config, db_path, s)
    if wiki:
        return wiki
    # rozcestník / nenalezeno → zkus stylové upřesnění (Bauhaus → škola/směr)
    for hint in (" (výtvarná škola)", " umělecký směr", " umělecký sloh",
                 " (umění)"):
        wiki = _ha._ground_via_wikipedia(config, db_path, s + hint)
        if wiki:
            return wiki
    return s


# ── HANS_ART_PERSON_LIKENESS_V3 — podoba osoby (img2img z reálné fotky) ──────
def _download_ref_image(url: str) -> Optional[str]:
    """Stáhni obrázek na /tmp. Vrací cestu nebo None."""
    try:
        ext = os.path.splitext(url.split("?")[0])[1].lower()
        if ext not in (".jpg", ".jpeg", ".png"):
            ext = ".jpg"
        path = os.path.join("/tmp", "hans_ref_%d%s" % (int(time.time()), ext))
        req = urllib.request.Request(
            url, headers={"User-Agent": "HansBot/1.0 (home assistant)"})
        with urllib.request.urlopen(req, timeout=25) as r, open(path, "wb") as f:
            f.write(r.read())
        return path
    except Exception as e:
        _ha._log.debug("art: download ref selhal: %s", e)
        return None


def _resolve_entity(config: dict, db_path: str, subject: str):
    """Entity dict pro námět (name/etype/gloss/source/source_title) z C1 store;
    když chybí, dohledá na Wikipedii (uloží) a resolvuje znovu. None = nic."""
    s = (subject or "").strip()
    if not s:
        return None
    s_clean = _ha._HONORIFIC.sub("", s).strip() or s
    try:
        from scripts.hans_entities import EntityStore
        es = EntityStore(config, db_path)
        ent = es.resolve(s, loose=True) or es.resolve(s_clean, loose=True)
        if ent:
            return ent
        if _ha._ground_via_wikipedia(config, db_path, s_clean):
            return es.resolve(s, loose=True) or es.resolve(s_clean, loose=True)
    except Exception as e:
        _ha._log.debug("art: resolve entity selhal: %s", e)
    return None


def _fetch_person_ref(config: dict, ent: dict, db: str = "") -> Optional[str]:
    """Stáhni portrét osoby z Wikipedie (dle source URL entity), zmenši pro SDXL.
    Vrací lokální cestu nebo None (osoba bez obrázku → fallback na text)."""
    # HANS_ENTITY_IMAGE_V1 (29. 9.) — obrázek se k entitě UKLÁDÁ (data/
    # entity_images/) a příště se nestahuje; dřív šel do /tmp a byl smazán.
    if db:
        try:
            from scripts.hans_entity_images import ensure_image
            _ulozeny = ensure_image(config, db, ent)
            if _ulozeny:
                return _ha._resize_to_temp(_ulozeny)
        except Exception as _eie:
            _ha._log.debug("art: uložený obrázek entity: %s", _eie)
    title = ent.get("source_title") or ent.get("name") or ""
    if not title:
        return None
    src = ent.get("source") or ""
    m = re.search(r"https?://([a-z]{2})\.wikipedia", src)
    lang = m.group(1) if m else (config.get("curiosity", {}) or {}).get(
        "wiki_lang", "cs")
    try:
        from scripts.web_reader import WebReader
        url = WebReader(config).wikipedia_image(title, lang=lang)
        if not url:
            return None
        raw = _ha._download_ref_image(url)
        if not raw:
            return None
        tmp = _ha._resize_to_temp(raw)
        try:
            os.remove(raw)
        except Exception:
            pass
        return tmp
    except Exception as e:
        _ha._log.debug("art: fetch person ref selhal: %s", e)
        return None


def _subject_beyond_name(subject: str, person_name: str) -> bool:
    """HANS_ART_PORTRAIT_IMG2IMG_V1 — nese námět kromě JMÉNA ještě něco (akci,
    místo, rekvizitu)? „Bud Spencer" → False (portrét), „Radecký na motorce"
    → True (scéna). Rozhoduje o img2img × PuLID, viz `paint_person_from_photo`.

    Porovnává se na složeninách bez diakritiky, prefixově (skloňování
    „Radeckého") a s podobnostním prahem na PŘEKLEPY („TerRence" vs uložené
    „Terence" — jinak by překlep vypadal jako obsah navíc a portrét by se
    poslal na scénu)."""
    def _fold(s):
        import unicodedata
        s = unicodedata.normalize("NFKD", (s or "").lower())
        s = "".join(c for c in s if not unicodedata.combining(c))
        return re.sub(r"[^\w\s]", " ", s).split()

    from difflib import SequenceMatcher as _SM
    name_toks = _fold(person_name)
    rest = []
    for t in _fold(subject):
        if len(t) < 3 or t in _ha._PERSON_FILLER:
            continue
        if t in name_toks:
            continue
        if any(t[:4] == n[:4] and min(len(t), len(n)) >= 4 for n in name_toks):
            continue
        if any(_SM(None, t, n).ratio() >= 0.8 for n in name_toks):
            continue
        rest.append(t)
    return bool(rest)


def _ref_ma_tvar(path: str) -> bool:
    """HANS_ART_POSTAVA_PULID_V1 — je na referenčním obrázku lidská tvář?
    PuLID bere z reference jen obličej; kreslená postava nebo figurka ho nemá.
    Haar (změřeno 6. 10. na uložených obrázcích entit: osoby 17/21, ostatní
    4/38). Chyba detekce = False → malba podle textu jako dřív."""
    try:
        import cv2
        im = cv2.imread(path)
        if im is None:
            return False
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY)
        m = max(24, int(min(g.shape) * 0.12))
        c = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        return len(c.detectMultiScale(g, 1.1, 6, minSize=(m, m))) > 0
    except Exception as e:
        _ha._log.debug("art: detekce tváře na referenci selhala: %s", e)
        return False


def _is_proper_name(subject: str, config: dict) -> bool:
    """Je námět vlastní jméno (→ má smysl groundovat)?"""
    s = (subject or "").strip()
    if not s:
        return False
    _cap = any(w[:1].isupper() for w in s.split() if w)   # fallback heuristika
    if s in _ha._name_cls_cache:
        return _ha._name_cls_cache[s]
    try:
        from scripts.hans_intent import _ask_classifier
        out = _ask_classifier(config, _ha._NAME_CLS_SYSTEM, s)
    except Exception:
        out = None
    if out is None:
        return _cap
    low = (out or "").strip().lower()
    if low.startswith("jmeno") or low.startswith("jméno"):
        res = True
    elif low.startswith("obecne") or low.startswith("obecné"):
        res = False
    else:
        return _cap                       # nejednoznačné → heuristika
    if len(_ha._name_cls_cache) < 256:
        _ha._name_cls_cache[s] = res
    return res


def _name_shaped(subject: str) -> bool:
    """HANS_ART_PERSON_WIKI_LOOKUP_V1 — vypadá námět jako JMÉNO (a stojí tedy za
    to zkusit, jestli to není osoba)? Krátký a s velkým písmenem. Drží Wikipedia
    dotaz od běžných námětů („kočka na zdi"), ať se nedělá zbytečný request
    navíc — `_ground_subject` si stejně sáhne na Wikipedii sám."""
    s = (subject or "").strip()
    if not s:
        return False
    toks = s.split()
    if not (1 <= len(toks) <= 4):
        return False
    # aspoň jeden token začíná velkým písmenem (a nejde o celou větu)
    return any(t[:1].isupper() for t in toks)


def _wiki_capture_person(config: dict, db_path: str, subject: str):
    """Dohledej námět na Wikipedii a ulož jako entitu (etype určí `_classify`).
    Vrací NÁZEV nalezeného článku (nebo None) — ne jen True: Wikipedia opraví
    i překlep („TerRence Hill" → „Terence Hill") a resolvovat se pak musí podle
    OPRAVENÉHO titulu, jinak by překlep entitu minul (token-prefix match potřebuje
    shodné první 4 znaky, a „terr" ≠ „tere"). Sdílí mechaniku s `_ground_subject`;
    tady jde o to, aby cesta k PODOBĚ měla stejné vstupy jako grounding."""
    try:
        from scripts.web_reader import WebReader
        from scripts.hans_entities import EntityStore
        lang = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
        art = WebReader(config).wikipedia_article(subject, lang=lang,
                                                  max_chars=1500)
        if not art or not (art.get("text") or "").strip():
            return None
        EntityStore(config, db_path).capture_from_reading(
            art["title"], art["text"], url=art.get("url", ""),
            lang=art.get("lang", lang))
        _ha._log.info("art: osobu '%s' jsem neznal → dohledal na Wikipedii '%s'",
                  subject, art["title"])
        return art["title"]
    except Exception as e:
        _ha._log.debug("art: wiki lookup osoby selhal: %s", e)
        return None

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_art as _ha  # noqa: E402
