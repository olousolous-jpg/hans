"""Funkce přesunuté z `scripts/hans_recall.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hr.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Optional
from datetime import datetime
import re
import time

def _resolve_person(question: str, config: dict,
                    asker: Optional[str]) -> Optional[str]:
    """Koho se dotaz týká: 'mě' → tazatel; jinak zkus person_name_forms."""
    low = (question or "").lower()
    if re.search(r"\bm[ěe]\b|\bmne\b", low):
        return asker
    forms_map = (config or {}).get("person_name_forms", {}) or {}
    words = set(re.findall(r"[a-zěščřžýáíéúůďťňó]+", low))
    for pid, forms in forms_map.items():
        if words & set(f.lower() for f in forms):
            return pid
    return asker


def films_watched_answer(db_path: str, question: str = "",
                         limit: int = 5) -> str:
    """Jaký film/pořad jsem viděl/sledoval — z deníku (kodi_playing),
    deterministicky. „dnes" v dotazu → dnešní; jinak posledních pár. Žádný LLM.
    Řeší, aby Hans neabstoval na „jaký film jsi viděl", když to v deníku má."""
    conn = None
    try:
        conn = _hr._ro(db_path)
        q = (question or "").lower()
        today = "dnes" in q or "dneska" in q
        # HANS_FILM_DAY_RANGE_V1 (23. 9.) — okno znalo JEN slovo „dnes“, takže
        # „co jsi včera viděl za filmy?“ vrátilo výpis posledních (tedy
        # DNEŠNÍCH) filmů. Doloženo sadou A 23. 9. Protažen hotový parser
        # `resolve_time_range` (včera, předevčírem, den v týdnu, datum,
        # tento/minulý týden) — týž, který používá recall rozhovorů.
        # 📏 Reálně 0× z 1 584 výměn; je to pokrytí tvaru, ne častá vada.
        _rng = None
        if not today:
            try:
                _rng = _hr.resolve_time_range(q)
            except Exception:
                _rng = None
        if _rng:
            _lbl = (_rng[2] or "").strip()
            _lbl = (_lbl[:1].upper() + _lbl[1:]) if _lbl else "V tu dobu"
            rows = conn.execute(
                "SELECT ts, title FROM diary WHERE event_type='kodi_playing' "
                "AND ts >= ? AND ts < ? ORDER BY ts DESC",
                (_rng[0], _rng[1])).fetchall()
            _seen, _tit = set(), []
            for _ts, _t in rows:
                _t = (_t or "").strip()
                if _t and _t.lower() not in _seen:
                    _seen.add(_t.lower())
                    _tit.append(_t)
                if len(_tit) >= limit * 2:
                    break
            if not _tit:
                return ("%s jsem podle deníku žádný film ani pořad "
                        "nesledoval, pane. Nebudu si nic vymýšlet." % _lbl)
            return "%s jsem u obrazovky zaznamenal: %s." % (
                _lbl, "; ".join("„%s“" % t for t in _tit))
        if today:
            midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            rows = conn.execute(
                "SELECT ts, title FROM diary WHERE event_type='kodi_playing' "
                "AND ts >= ? ORDER BY ts DESC", (midnight,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, title FROM diary WHERE event_type='kodi_playing' "
                "ORDER BY ts DESC LIMIT ?", (limit * 5,)).fetchall()
        if not rows:
            return ("Nemám záznam o žádném filmu či pořadu, který bych "
                    + ("dnes " if today else "") +
                    "sledoval, pane. Nebudu si nic vymýšlet.")
        seen, titles = set(), []
        for ts, title in rows:
            t = (title or "").strip()
            if t and t.lower() not in seen:
                seen.add(t.lower())
                titles.append((ts, t))
            if len(titles) >= limit:
                break
        if today:
            names = "; ".join("„%s“" % t for _, t in titles)
            return "Dnes jsem u obrazovky zaznamenal: %s." % names
        last_ts, last = titles[0]
        out = "Naposledy jsem sledoval „%s“ (%s)." % (last, _hr._cz_when(last_ts))
        if len(titles) > 1:
            out += " Předtím: %s." % "; ".join("„%s“" % t for _, t in titles[1:])
        return out
    except Exception:
        return ("K filmům teď nemám přístup do deníku, pane.")
    finally:
        if conn:
            conn.close()


def artwork_answer(db_path: str, question: str = "", limit: int = 5) -> str:
    """HANS_ARTWORK_RECALL_V1 (30.8.) — CO JSEM NAMALOVAL, z deníku, bez LLM.

    PROČ: na „namaloval jsi neco novyho?" Hans odpověděl *„Vzhledem k mé roli
    nemám možnost vytvářet obrazy samostatně, malování nastane pouze na základě
    vašeho pokynu"* a na „ukaz mi to" dokonce *„jsem textový asistent"*.
    Obojí je DOLOŽENĚ NEPRAVDA — `artwork` má 89 záznamů za 30 dní, poslední
    „Sen" vznikl v noci bez jakéhokoli pokynu.

    Táž třída jako „Proud krve" (14.7.): **zapírá, co ví**. Tehdy se to opravilo
    pro filmy (`film_knowledge_answer`) a CLAUDE.md si nechala otevřené „další
    zapíraná kategorie, až se najde reálný případ". Tohle je ten případ, takže
    se PROTAHUJE hotový vzor (`films_watched_answer`), nestaví nový mechanismus.

    ⚠️ Nestačilo by doručit `hans_capabilities`: „umím malovat" je odpověď na
    JINOU otázku. Tady se ptá na PROVEDENOU PRÁCI → musí přijít z dat.
    """
    conn = None
    try:
        conn = _hr._ro(db_path)
        q = (question or "").lower()
        dnes = "dnes" in q or "dneska" in q
        # HANS_WORK_RECALL_IN_ARTWORK_V1 (15. 9.) — "vytvoril jste k tomu dilo?"
        # sedne na vzor /obrazy (_ART_MINULE zna "vytvoril"), jenze DILO neni
        # obraz: `work_artifact` (stranka k dostudovanemu tematu) sem nechodil.
        # Doloženo 15. 9.: na dotaz na dilo k hudbe Hans ukazal obraz a pak
        # tvrdil, ze skladby nevytvoril, ackoli "Dilo: hudba" existuje.
        # Rozhoduje slovo v dotazu: dilo/skladba/web bez slova o malovani.
        if (re.search(r"\bd[\u00edi]l(?:o|a|u|e|em)\b|skladb|\bweb|str[\u00e1a]nk", q)
                and not re.search(r"obraz|malov|namal|kresl", q)):
            import json as _json_d
            _temata = []
            for _ts, _tit, _dat in conn.execute(
                    "SELECT ts, title, data FROM diary WHERE event_type='work_artifact' "
                    "ORDER BY ts DESC LIMIT 40").fetchall():
                try:
                    _tp = (_json_d.loads(_dat or "{}") or {}).get("topic") or ""
                except Exception:
                    _tp = ""
                _tp = _tp or re.sub(r"^D\u00edlo:\s*", "", _tit or "")
                if _tp and _tp.lower() not in [x[1].lower() for x in _temata]:
                    _temata.append((_ts, _tp))
                if len(_temata) >= 3:
                    break
            if _temata:
                # HANS_DILO_TENSE_V1 (2. 10.) — dřív „webovou stránku, kterou
                # SESTAVÍM, když téma dostuduji“ i o HOTOVÉM díle → tazatel
                # (B21) to právem četl jako slib a Hans se zamotal.
                _out = ("Naposledy jsem vytvo\u0159il d\u00edlo k t\u00e9matu \u201e%s\u201c (%s) \u2014 "
                        "webovou str\u00e1nku; takovou stavím v\u017edy, kdy\u017e t\u00e9ma dostuduji."
                        % (_temata[0][1], _hr._cz_when(_temata[0][0])))
                if len(_temata) > 1:
                    _out += " P\u0159edt\u00edm k t\u00e9mat\u016fm: %s." % ", ".join(
                        "\u201e%s\u201c" % _t for _, _t in _temata[1:])
                _obr = conn.execute(
                    "SELECT title FROM diary WHERE event_type='artwork' "
                    "ORDER BY ts DESC LIMIT 1").fetchone()
                if _obr and _obr[0]:
                    _out += " Obrazy maluji zvl\u00e1\u0161\u0165 \u2014 naposledy \u201e%s\u201c." % _obr[0]
                return _out
        if dnes:
            midnight = datetime.now().replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            rows = conn.execute(
                "SELECT ts, title, note FROM diary WHERE event_type='artwork' "
                "AND ts >= ? ORDER BY ts DESC", (midnight,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT ts, title, note FROM diary WHERE event_type='artwork' "
                "ORDER BY ts DESC LIMIT ?", (limit * 6,)).fetchall()
        if not rows:
            return ("Nemám záznam o žádném obrazu, který bych "
                    + ("dnes " if dnes else "") +
                    "namaloval, pane. Nebudu si nic vymýšlet.")
        videno, dila = set(), []
        for ts, title, note in rows:
            t = (title or "").strip()
            if t and t.lower() not in videno:
                videno.add(t.lower())
                dila.append((ts, t, (note or "").strip()))
            if len(dila) >= limit:
                break
        if dnes:
            return "Dnes jsem namaloval: %s." % "; ".join(
                "„%s“" % t for _, t, _ in dila)
        ts0, t0, note0 = dila[0]
        out = "Naposledy jsem maloval „%s“ (%s)." % (t0, _hr._cz_when(ts0))
        if note0:
            # vlastní poznámka k obrazu — Hansův komentář, ne domněnka
            out += " Poznamenal jsem si k tomu: %s" % note0.rstrip(".") + "."
        if len(dila) > 1:
            out += " Předtím: %s." % "; ".join("„%s“" % t for _, t, _ in dila[1:])
        # HANS_ARTWORK_WHERE_V1 (30.8.) — KDE je obraz k vidění. Bez tohohle
        # Hans na navazující „ukaz mi to" odpověděl „nemám možnost zobrazovat
        # obrazy jen tak ze záznamu", což je NEPRAVDA: obrazy leží v
        # `data/hans_art/` a jsou na nástěnce web adminu. Věta se dostane do
        # historie hovoru, takže na ni model může navázat.
        out += " Obrazy mám na nástěnce (Co Hans namaloval)."
        return out
    except Exception:
        return "K obrazům teď nemám přístup do deníku, pane."
    finally:
        if conn:
            conn.close()


def last_seen_answer(db_path: str, config: dict, question: str,
                     asker: Optional[str]) -> str:
    """Kdy jsem osobu naposledy viděl — přímo z person_seen. Žádný LLM."""
    person = _hr._resolve_person(question, config, asker)
    if not person:
        return "Nevím jistě, koho máte na mysli, pane."
    # HANS_LAST_SEEN_NAME_V1 — `person` je KLÍČ z `person_name_forms` („jana"),
    # ne jméno k vyslovení. Bez skloňování z toho lezlo „Naposledy jsem osobu
    # jana viděl" (malé písmeno, 1. pád). Tvary drží config (known_persons.acc),
    # `cz_names.acc` je jen přečte — nic se tu nevymýšlí.
    from scripts.cz_names import acc as _cz_acc
    who = "vás" if person == asker else _cz_acc(person, config)
    conn = None
    try:
        conn = _hr._ro(db_path)
        rows = conn.execute(
            "SELECT ts FROM diary WHERE event_type='person_seen' "
            "AND lower(title) LIKE ? ORDER BY ts DESC LIMIT 40",
            (f"%{person.lower()}%",)).fetchall()
        if not rows:
            return (f"V deníku nemám žádný záznam, že bych {who} viděl, pane.")
        last = rows[0][0]
        # předchozí NÁVŠTĚVA = starší záznam oddělený > 1 h mezerou
        prev = None
        for (ts,) in rows[1:]:
            if last - ts > 3600:
                prev = ts
                break
        gap_min = (time.time() - last) / 60.0
        if gap_min < 15:
            out = f"Vidím {who} právě teď, pane"
        elif gap_min < 90:
            out = (f"Naposledy jsem {who} viděl před "
                   f"{int(round(gap_min))} minutami")
        else:
            out = f"Naposledy jsem {who} viděl {_hr._cz_when(last)}"
        if prev:
            out += f"; předtím {_hr._cz_when(prev)}"
        return out + ". Tak to mám zapsáno v deníku."
    except Exception as e:
        _hr._log.warning("last_seen_answer selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _looks_like_film_query(question: str) -> bool:
    """Vypadá dotaz jako na film / „co víš o X“? (levný gate před DB reverzní
    shodou — ať se titulový slovník netahá na každou zprávu)."""
    return bool(_hr._FILM_CTX.search(_hr._fold(question or "")))


def _nazor_prvni_veta(data: str) -> Optional[str]:
    """HANS_FILM_OPINION_ANAFORA_V1 — první věta vlastního názoru na film,
    nebo None (krátká, přiznání neznalosti, jméno z domácnosti).
    Vytaženo z `films_liked_answer`, aby obě odpovědi měly TÝŽ filtr."""
    veta = (data or "").strip().split("\n")[0].strip()
    m = re.search(r"^(.{20,180}?[.!?])(\s|$)", veta)
    if m:
        veta = m.group(1).strip()
    elif len(veta) > 180:
        veta = veta[:180].rstrip() + "\u2026"
    if len(veta) < 20:
        return None
    if any(_n in veta.lower() for _n in _hr._NAZOR_NEZNA):
        return None
    if _hr._jmenuje_domacnost(veta):
        return None
    return veta


def film_list_titles(text: str) -> list:
    """Tituly z Hansova výpisu filmů; [] když text výpisem není."""
    t = (text or "").strip()
    if not _hr._VYPIS_FILMU_PAT.search(t):
        return []
    return [x.strip() for x in re.findall(r"„([^“]{1,120})“", t) if x.strip()]


def nazor_k_filmu(db_path: str, title: str) -> Optional[str]:
    """HANS_FILM_RECOMMEND_V1 — první věta Hansova vlastního názoru na PŘESNĚ
    tento titul (`movie_opinion`), jinak None. `title = ?`, ne `lower()` —
    SQLite `lower()` nemění ne-ASCII („Čelisti“)."""
    t = (title or "").strip()
    if not t:
        return None
    conn = None
    try:
        conn = _hr._ro(db_path)
        for (data,) in conn.execute(
                "SELECT data FROM diary WHERE event_type='movie_opinion' "
                "AND title = ? AND data IS NOT NULL AND trim(data) != '' "
                "ORDER BY ts DESC LIMIT 5", (t,)).fetchall():
            v = _hr._nazor_prvni_veta(data)
            if v:
                return v
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return None


def films_liked_among(db_path: str, titles: list, limit: int = 2) -> str:
    """HANS_FILM_OPINION_ANAFORA_V1 (23. 9.) — „a který se ti z nich líbil
    nejvíc?“ po výpisu filmů. Názor se bere JEN k filmům z toho výpisu.
    Doloženo sadou A 23. 9.: anafora nenesla slovo „film“, šla přes
    self_state a Hans si estetiku filmu vymyslel. K žádnému názor → přizná."""
    if not titles:
        return ""
    conn = None
    ven = []
    try:
        conn = _hr._ro(db_path)
        for t in titles:
            tl = t.lower()
            rows = conn.execute(
                "SELECT title, data FROM diary WHERE event_type='movie_opinion' "
                "AND data IS NOT NULL AND trim(data) != '' AND title IS NOT NULL "
                "AND (lower(title) = ? OR instr(lower(title), ?) > 0 "
                "OR instr(?, lower(title)) > 0) ORDER BY ts DESC LIMIT 5",
                (tl, tl, tl)).fetchall()
            for _tt, data in rows:
                # krátký titul (≤ 3 znaky) jen při přesné shodě
                if len((_tt or "").strip()) <= 3 and (_tt or "").lower() != tl:
                    continue
                v = _hr._nazor_prvni_veta(data)
                if v:
                    ven.append((t, v))
                    break
            if len(ven) >= max(1, limit):
                break
    except Exception:
        return ""
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    if not ven:
        return ("K žádnému z těch filmů nemám zapsaný vlastní názor, pane, "
                "takže si nebudu vymýšlet, který se mi líbil nejvíc.")
    if len(ven) == 1:
        return "Z nich mě nejvíc zaujal „%s“. %s" % ven[0]
    return ("Názor mám zapsaný k těmhle z nich:\n"
            + "\n".join("\u2013 %s \u2014 %s" % (t, v) for t, v in ven))


def book_from_thread(db_path: str, texts: list) -> Optional[dict]:
    """HANS_BOOK_ORIGIN_V1 — kniha z `hans_library`, o které vlákno mluví.
    Shoda = slovo titulu (≥ 5 znaků) se ve vlákně objeví jako začátek
    slova (bez diakritiky). Nejnověji založená kniha vyhrává."""
    txt = " " + _hr._fold(" ".join(str(x) for x in (texts or []))).lower()
    if not txt.strip():
        return None
    conn = None
    try:
        conn = _hr._ro(db_path)
        rows = conn.execute(
            "SELECT book_id, book_title, author, COALESCE(url,'') FROM "
            "hans_library WHERE status IN ('reading','finished') "
            "ORDER BY started_at DESC").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    for bid, title, author, url in rows:
        slova = [w for w in re.findall(r"\w+", _hr._fold(title or "").lower())
                 if len(w) >= 5]
        if any(re.search(r"(?<!\w)" + re.escape(w[:6]), txt) for w in slova):
            return {"id": bid or "", "title": title or "", "author": author or "",
                    "url": url or ""}
    return None


def book_origin_answer(book: dict) -> str:
    """HANS_BOOK_ORIGIN_V1 (23. 9.) — odkud Hans knihu MÁ, z `hans_library`.
    Doloženo sadou A: „kde jsi ji vzal?“ (o čtené knize) → výpis webových
    zdrojů. Knihy jsou buď nahrané uživatelem (`user_*`), nebo z Project
    Gutenberg. 🔒 Bez jména — kdo knihu nahrál, se neříká (smí to slyšet
    i cizí)."""
    t = book.get("title") or "tu knihu"
    bid = book.get("id") or ""
    url = book.get("url") or ""
    if bid.startswith("user_"):
        return ("Knihu „%s“ mi do knihovny nahrál někdo z domácnosti, pane — "
                "sám jsem si ji nesháněl." % t)
    if not url:
        try:
            from scripts.hans_library import _FALLBACK_BOOKS
            url = next((b.get("url") or "" for b in _FALLBACK_BOOKS
                        if b.get("id") == bid), "")
        except Exception:
            url = ""
    if "gutenberg" in url:
        return ("Knihu „%s“ jsem si stáhl z Project Gutenberg, kde je volně "
                "dostupná, pane: %s" % (t, url))
    return ("Knihu „%s“ mám ve své knihovně, ale odkud přesně pochází, "
            "zapsané nemám, pane." % t)


def _PRUBEH_OBSAHOVA_SLOVA(question: str) -> bool:
    """Nese otázka o průběhu čtení JMÉNO (slovo ≥ 4 mimo rámec dotazu)?"""
    return any(len(w) >= 4 and w not in _hr._PRUBEH_RAMEC
               for w in re.findall(r"\w+", _hr._fold(question or "").lower()))


def _kniha_z_otazky(db_path: str, question: str) -> Optional[dict]:
    """HANS_BOOK_PROGRESS_ANSWER_V1 — kniha z `hans_library` jmenovaná
    v otázce: slovo otázky (≥ 4 znaky) je ZAČÁTKEM slova titulu (≥ 5).
    „le guin" → „Le Guinova Ursula – …" (autor je v titulu)."""
    slova_q = [w for w in re.findall(r"\w+", _hr._fold(question or "").lower())
               if len(w) >= 4]
    if not slova_q:
        return None
    conn = None
    try:
        conn = _hr._ro(db_path)
        rows = conn.execute(
            "SELECT book_id, book_title, author, COALESCE(url,'') FROM "
            "hans_library WHERE status IN ('reading','finished') "
            "ORDER BY started_at DESC").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    for bid, title, author, url in rows:
        slova_t = [w for w in re.findall(r"\w+", _hr._fold(title or "").lower())
                   if len(w) >= 5]
        # oboustranně: „guin" → „guinova", „doriana" → „dorian"
        if any(t.startswith(q) or (len(q) >= 5 and q.startswith(t))
               for q in slova_q for t in slova_t):
            return {"id": bid or "", "title": title or "", "author": author or "",
                    "url": url or ""}
    return None


def book_progress_answer(db_path: str, question: str,
                         thread_texts: Optional[list] = None) -> str:
    """HANS_BOOK_PROGRESS_ANSWER_V1 (23. 9.) — ODKDY a JAK DLOUHO knihu čtu,
    na které jsem kapitole a jestli POPRVÉ. Deterministicky z `hans_library`
    a deníku (`book_read`), bez LLM.

    Doloženo 23. 9.: „tu knihu od le guin čteš poprvé?" → „četl jsem ji již
    dříve" a „jak dlouho už ji čteš?" → „dva dny" (skutečně poprvé, 6 dní).
    Popisek v podkladu (`HANS_BOOK_PROGRESS_LABEL_V1`) model PŘEHLÍŽEL a LLM
    router `/cetl` zamítl — jenže ani `/cetl` průběh neuměl. Rozhodnutí se
    proto nedává modelu: odpověď je z dat. 📏 Reálně 1× z 2 342 vět.
    Kniha: z otázky → z vlákna → jediná rozečtená. Nenajde-li se, vrací ''
    (volající jde dosavadní cestou)."""
    kn = _hr._kniha_z_otazky(db_path, question)
    # Jmenuje-li otázka něco, co v knihovně NENÍ („čteš Doriana Graye?"
    # a Dorian tam není), NESMÍ se odpovědět o jiné knize z vlákna.
    if not kn and _hr._PRUBEH_OBSAHOVA_SLOVA(question):
        return ""
    if not kn and thread_texts:
        kn = _hr.book_from_thread(db_path, thread_texts)
    conn = None
    try:
        conn = _hr._ro(db_path)
        if not kn:
            rows = conn.execute(
                "SELECT book_id, book_title FROM hans_library "
                "WHERE status='reading'").fetchall()
            if len(rows) != 1:
                return ""
            kn = {"id": rows[0][0], "title": rows[0][1]}
        r = conn.execute(
            "SELECT started_at, finished_at, status, current_chapter, "
            "total_chapters FROM hans_library WHERE book_id=? "
            "ORDER BY started_at DESC LIMIT 1", (kn["id"],)).fetchone()
        if not r:
            return ""
        pocet_cteni = conn.execute(
            "SELECT COUNT(*) FROM hans_library WHERE book_id=?",
            (kn["id"],)).fetchone()[0]
        kap = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT title), MAX(ts) FROM diary "
            "WHERE event_type='book_read' AND title LIKE ?",
            (kn["title"] + " \u2014 kap.%",)).fetchone()
    except Exception:
        return ""
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    start, fin, status, cur, tot = r
    if not start:
        return ""
    d = lambda x: datetime.fromtimestamp(x).strftime("%-d. %-m.")
    poprve = pocet_cteni <= 1 and (not kap or kap[0] == kap[1])
    konec = fin if (status == "finished" and fin) else time.time()
    dni = max(0, int((konec - start) // 86400))
    dni_s = ("necelý den" if dni == 0 else "%d %s" % (
        dni, "den" if dni == 1 else ("dny" if dni < 5 else "dní")))
    t = kn.get("title") or "tu knihu"
    if status == "finished" and fin:
        out = ("„%s“ jsem dočetl %s, pane — četl jsem ji od %s, tedy %s."
               % (t, d(fin), d(start), dni_s))
    else:
        out = "„%s“ čtu od %s, pane, tedy %s" % (t, d(start), dni_s)
        if cur:
            _ts = str(tot or "")
            # „ze" před sto…/sedm…/sedmnáct… (ze 113, ze 7, ze 17)
            _pr = ("ze" if (_ts[:1] == "7" or _ts[:2] == "17"
                            or (len(_ts) == 3 and _ts[:1] == "1")) else "z")
            out += " — teď jsem u kapitoly %s%s" % (
                cur, (" %s %s" % (_pr, tot)) if tot else "")
            if kap and kap[2]:
                out += " (naposledy %s)" % d(kap[2])
        out += "."
    out += (" Podle deníku ji čtu poprvé." if poprve and status != "finished"
            else " Četl jsem ji poprvé." if poprve
            else " Podle deníku jsem ji četl víckrát.")
    return out


def films_liked_answer(db_path: str, limit: int = 3) -> Optional[str]:
    """HANS_FILM_OPINION_ANSWER_V1 (22. 9.) — na dotaz \u201ekter\u00fd film se ti
    l\u00edbil?\u201c odpov\u011bz z VLASTN\u00cdCH n\u00e1zor\u016f (`movie_opinion`), ne v\u00fdpisem
    sledovan\u00fdch (`kodi_playing`).

    Doloženo pam\u011b\u0165ovou sadou 22. 9.: na \u201ea jak\u00fd film se ti l\u00edbil?\u201c vr\u00e1til
    Hans seznam naposledy sledovan\u00fdch \u2014 co\u017e na ot\u00e1zku po OBLIB\u011a neodpov\u00edd\u00e1.
    Je to t\u0159et\u00ed v\u00fdskyt t\u0159\u00eddy \u201ev\u00fdpis m\u00edsto odpov\u011bdi\u201c (po `/sen`
    a `/anomalie`, `HANS_LIST_NOT_CLAIM_V1` 21. 9.).
    📌 Data pro to existuj\u00ed: `movie_opinion` m\u00e1 2 205 z\u00e1znam\u016f s obsahem
    ve sloupci `data` \u2014 jen k nim \u017e\u00e1dn\u00e1 odpov\u011b\u010f nesahala.
    ⚠️ Obsah je v `data`, ne v `note` \u2014 t\u00e1\u017e past jako u `reading_takeaway`.

    Vrac\u00ed CS v\u011btu nebo None (\u017e\u00e1dn\u00fd n\u00e1zor \u2192 vol\u00e1 se dosavadn\u00ed cesta).
    """
    conn = None
    try:
        conn = _hr._ro(db_path)
        rows = conn.execute(
            "SELECT title, data FROM diary WHERE event_type='movie_opinion' "
            "AND data IS NOT NULL AND trim(data) != '' AND title IS NOT NULL "
            "AND trim(title) != '' ORDER BY ts DESC LIMIT 60").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
    # ⚠️ Tytez film mivá v deniku VIC TITULU ("Krouzek sebevrahu" x
    # "Suicide Circle: Krouzek sebevrahu"), takze pouha shoda klicu nestaci
    # a odpoved by tentyz film nabidla dvakrat. Dedup proto i na OBSAZENI.
    # ⛔ A vyrazuji se zaznamy, kde Hans priznava, ze film NEZNA — na otazku
    # "co te zaujalo" je priznani neznalosti spatna odpoved (na to ma jine
    # cesty). Doloženo pri stavbe: treti polozka znela "Pripustim, ze o filmu
    # vim jen velmi malo".
    _NEZNA = ("v\u00edm jen velmi m\u00e1lo", "v\u00edm jen m\u00e1lo", "nezn\u00e1m",
              "ne\u010detl jsem", "nevid\u011bl jsem", "nem\u00e1m z\u00e1znam")
    videl, ven = set(), []
    for title, data in rows:
        t = (title or "").strip()
        klic = t.lower()
        if not t or klic in videl:
            continue
        if any(klic in _v or _v in klic for _v in videl):
            continue
        # jen PRVNI veta nazoru — cely odstavec by z odpovedi udelal esej.
        # HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) — veta, ktera jmenuje clena
        # domacnosti, se vynecha (odpoved jde deterministicky i cizimu).
        # Filtr je sdileny s `films_liked_among` (HANS_FILM_OPINION_ANAFORA_V1).
        veta = _hr._nazor_prvni_veta(data)
        if not veta:
            continue
        videl.add(klic)
        ven.append((t, veta))
        if len(ven) >= max(1, limit):
            break
    if not ven:
        return None
    if len(ven) == 1:
        return "Z film\u016f, co jsem vid\u011bl, m\u011b zaujal %s. %s" % (ven[0][0], ven[0][1])
    hlava = "Z film\u016f, co jsem vid\u011bl, m\u011b zaujaly tyhle:"
    telo = "\n".join("\u2013 %s \u2014 %s" % (t, v) for t, v in ven)
    return hlava + "\n" + telo


def _jmenuje_domacnost(text: str) -> bool:
    """HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) — jmenuje text clena domacnosti?
    Sdileny predikat `cz_names.find_known_person` (pady, diakritika), ne novy.
    ⚠️ Porovnava na prefix, takze obcas chytne i nevinne slovo (anglicke
    "old", "sarkofagy") — na strane soukromi prijatelna chyba: poznamka
    se jen vynecha. Selhani = False (dosavadni chovani)."""
    try:
        from scripts.cz_names import find_known_person
        return bool(find_known_person(text or ""))
    except Exception:
        return False


def film_knowledge_answer(db_path: str, question: str = "",
                          asker: str = "") -> Optional[str]:
    """HANS_FILM_RECALL_V1 — když dotaz zmiňuje FILM podle názvu, dohledej v
    deníku Hansovy VLASTNÍ záznamy o tom filmu (movie_opinion = názor/děj,
    kodi_playing = kdy viděl) a vrať GROUNDED blok. None = žádný známý titul
    v dotazu → normální tok. Deterministické, žádný LLM."""
    if not question or not _hr._looks_like_film_query(question):
        return None
    conn = None
    try:
        conn = _hr._ro(db_path)
        q_fold = _hr._fold(question).lower()
        # slovník titulů z deníku (distinct, filmové event types)
        rows = conn.execute(
            "SELECT DISTINCT title FROM diary WHERE event_type IN "
            "('movie_opinion','kodi_playing','movie_browsed','film_suggestion') "
            "AND title IS NOT NULL AND length(title) >= 4").fetchall()
        # reverzní shoda na hranici slov; jen distinktivní tituly
        best = None
        for (title,) in rows:
            tf = _hr._fold(title).lower().strip()
            if len(tf) < 4:
                continue
            multiword = " " in tf
            if not multiword and len(tf) < 6:
                # krátké jednoslovné (Hra, Past…) → riziko falešné shody.
                # HANS_FILM_SHORT_TITLE_V1 (25. 9.) — VÝJIMKA, když věta titul
                # výslovně označí: „film Duna“, „o filmu Duna“, „Duna“ v uvozovkách.
                # Změřeno na 1 675 reálných větách (98 filmových dotazů): 1 změna
                # („pusť film ring“ → Ring, správně), 0 falešných.
                if not re.search(r"\bfilm\w*\s+[„\"']?" + re.escape(tf) + r"\b"
                                 r"|[„\"']" + re.escape(tf) + r"[“\"']", q_fold):
                    continue
            if re.search(r"\b" + re.escape(tf) + r"\b", q_fold):
                if best is None or len(tf) > len(_hr._fold(best).lower()):
                    best = title
        if not best:
            return None
        # Hansovy vlastní názory/poznámky (obsah bývá v `data`, fallback `note`)
        ops = conn.execute(
            "SELECT ts, COALESCE(NULLIF(data,''), note) FROM diary "
            "WHERE event_type='movie_opinion' AND title=? "
            "AND COALESCE(NULLIF(data,''), note) IS NOT NULL "
            "ORDER BY ts DESC LIMIT 4", (best,)).fetchall()
        # kolikrát/kdy viděl
        # HANS_FILM_FIRST_SEEN_V1 (16. 9.) — i MIN(ts): na dotaz \u201ekdy jsi ho
        # videl poprve?\u201c blok dosud nabizel jen \u201enaposledy\u201c a model si prvni
        # zhlednuti VYMYSLEL (16. 9.: \u201epred peti lety, v roce 2021\u201c, pritom
        # nejstarsi zaznam v deniku je z dubna 2026).
        seen = conn.execute(
            "SELECT COUNT(*), MAX(ts), MIN(ts) FROM diary "
            "WHERE event_type='kodi_playing' "
            "AND title=?", (best,)).fetchone()
        notes = [str(n).strip() for _, n in ops if n and str(n).strip()]
        # HANS_FILM_KODI_FACTS_V1 (27. 9.) — údaje PŘEHRÁVAČE do podkladu.
        # Doloženo 26. 9.: „co víte o filmu Duna?“ → blok nesl jen „viděl jsi to
        # 3×“ a model doplnil „1984, David Lynch“, ačkoli záznam přehrávání
        # měl rok 2021 i režii Villeneuve. Změřeno: z 1 493 titulů má záznam
        # rok u 611, režii u 506, děj u 959; 305 titulů nemá žádný názor, takže
        # jim podklad nesl jen počet zhlédnutí.
        fakta = []
        try:
            _kr = conn.execute(
                "SELECT COALESCE(NULLIF(note,''), data) FROM diary "
                "WHERE event_type='kodi_playing' AND title=? "
                "ORDER BY ts DESC LIMIT 1", (best,)).fetchone()
            for _cast in str((_kr or [""])[0] or "").split(" | "):
                _m = re.match(r"\s*(rok|žánr|režie|děj)[:\s]\s*(.+)", _cast)
                if not _m:
                    continue
                _k, _v = _m.group(1), _m.group(2).strip()
                if _k == "děj" and len(_v) > 400:
                    _cut = max(_v.rfind(". ", 0, 400), _v.rfind("! ", 0, 400),
                               _v.rfind("? ", 0, 400))
                    _v = _v[:_cut + 1] if _cut > 100 else _v[:400].rstrip() + "…"
                fakta.append("%s %s" % (_k, _v) if _k == "rok" else "%s: %s" % (_k, _v))
        except Exception:
            fakta = []
        # HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) — CIZIMU tazateli nedavej
        # poznamky, ktere jmenuji cleny domacnosti. Doloženo sadou B 23. 9.:
        # blok s takovou vetou lezel v promptu ciziho (ven neprosla).
        # Zmereno: 65 z 2 224 nazoru na filmy jmenuje nekoho z domacnosti,
        # 59 z 1 180 titulu se to tyka. Ctvrty zdroj tridy
        # HANS_PROMPT_HOUSEHOLD_PRIVACY_V1/V2 — tentyz predikat tazatele.
        # Prazdny `asker` chovani NEMENI (jako V1: interni cesty bez mluvciho).
        if asker and notes:
            try:
                from scripts.cz_names import is_known_person as _ikp
                if not _ikp(asker):
                    notes = [n for n in notes if not _hr._jmenuje_domacnost(n)]
            except Exception:
                pass
        if not notes and not (seen and seen[0]):
            return None  # titul se objevil, ale nic konkrétního → nech projít dál
        parts = [f"SKUTEČNÝ ZÁZNAM o „{best}“ z TVÉHO deníku (odpověz JEN z něj, "
                 f"nic si nedomýšlej; na co tu není, přiznej „to si nevybavuji“):"]
        if notes:
            parts.append("Tvé dřívější poznámky a názory:")
            parts.extend(f"- {n}" for n in notes)
        if fakta:   # HANS_FILM_KODI_FACTS_V1
            parts.append("Údaje přehrávače o tom, co jsi viděl: " + "; ".join(fakta))
        if seen and seen[0]:
            kdy = _hr._cz_when(seen[1], slovy=True) if seen[1] else "dříve"
            krat = "jednou" if seen[0] == 1 else f"{seen[0]}×"
            # HANS_FILM_FIRST_SEEN_V1 — \u201epoprve\u201c jen kdyz se od \u201enaposledy\u201c
            # lisi o vic nez den. Zmereno: z 483 vicekrat videnych titulu je to
            # 205; u zbylych 278 jde o reprisu v tyz den, kde by to byl sum.
            _prvni = ""
            try:
                if (len(seen) > 2 and seen[2] and seen[1]
                        and (float(seen[1]) - float(seen[2])) > 86400.0):
                    _prvni = ", poprvé %s" % _hr._cz_when(seen[2])
            except Exception:
                _prvni = ""
            parts.append(f"(V záznamu přehrávání: viděl jsi to {krat}{_prvni}, "
                         f"naposledy {kdy}.)")
        return "\n\n" + "\n".join(parts)
    except Exception as e:
        _hr._log.warning("film_knowledge_answer selhal: %s", e)
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
