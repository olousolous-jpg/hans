"""Funkce přesunuté z `scripts/hans_recall.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hr.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Optional
import re
import sqlite3
import time

# ── HANS_SELF_STATE_V1 (5.8.) — grounded blok „jak se mám a co jsem dnes dělal"
def _popis_dila(data_json: str) -> str:
    """HANS_SELF_STATE_WORKS_V1 (30. 9.) — co dílo JE, ne jen jeho téma.
    Test 30. 9.: s řádkem „napsal jsem dílo: Dílo: <téma>“ Hans o webu se
    6 stránkami a skladbou tvrdil „první fáze vývoje nástroje“, „interaktivní
    vizualizace“, cizímu „esej“ a předstíral přehrání skladby."""
    import json as _json
    import os as _os
    try:
        d = _json.loads(data_json or "{}")
    except Exception:
        return ""
    tema = str(d.get("topic") or "").strip()
    cesta = str(d.get("path") or "")
    if not tema:
        return ""
    if d.get("target") != "coder" or not cesta:
        return "„%s“" % tema
    adr = _os.path.dirname(cesta)
    bits = []
    try:
        stranky = [f for f in _os.listdir(adr) if f.endswith(".html")]
        if stranky:
            bits.append("%d %s" % (len(stranky), "stránka" if len(stranky) == 1
                                   else "stránky" if len(stranky) < 5 else "stránek"))
        if _os.path.isdir(_os.path.join(adr, "images")) and _os.listdir(_os.path.join(adr, "images")):
            bits.append("s obrázky")
        _dj = _os.path.join(adr, "dilo.json")
        if _os.path.exists(_dj):
            _h = (_json.load(open(_dj, encoding="utf-8")) or {}).get("hudba")
            if isinstance(_h, dict) and (_h.get("popis") or _h.get("styl")):
                bits.append("se skladbou na úvodní stránce (%s)"
                            % (_h.get("popis") or _h["styl"]))
    except Exception:
        pass
    return "webové stránky na téma „%s“%s — hotové, uložené u mě" % (
        tema, (" (" + ", ".join(bits) + ")") if bits else "")


def _prvni_veta(text: str, max_chars: int = 110) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    m = re.match(r"(.+?[.!?])(\s|$)", s)
    s = m.group(1) if m else s
    return s if len(s) <= max_chars else s[:max_chars].rsplit(" ", 1)[0] + "…"


def _self_state_trvale(conn) -> list:
    """HANS_SELF_STATE_LASTING_V1 (1. 10.) — co Hans VYTVOŘIL a co STUDUJE,
    bez ohledu na dnešek. Blok self_state nesl jen dnešní záznamy, takže web
    hotový 29. 9. v něm o den později chyběl a model na „je ta stránka
    hotová?“ odpověděl v jednom rozhovoru „v rané fázi“, „hotová“
    i „rozpracovaná“ a cizímu „teprve ji chystám“; další studium si vymýšlel
    („Jára Cimrman“), ačkoli fronta programů je v DB (/tazatel 1. 10., 9×)."""
    out = []
    try:
        r = conn.execute("SELECT ts, data FROM diary WHERE event_type='work_artifact' "
                         "ORDER BY ts DESC LIMIT 1").fetchone()
        if r and _hr._popis_dila(r["data"] or ""):
            import datetime as _dtm
            _d = _dtm.datetime.fromtimestamp(r["ts"])
            out.append("moje poslední dílo ze studia: %s (dokončil jsem ho %d. %d.)"
                       % (_hr._popis_dila(r["data"] or ""), _d.day, _d.month))
    except Exception as e:
        _hr._log.debug("self_state trvale (dilo): %s", e)
    try:
        import json as _json
        rows = conn.execute("SELECT title, kind, status, current_index, outline "
                            "FROM writing_project ORDER BY id DESC").fetchall()
        akt = [x for x in rows if x["status"] == "active"]
        hot = [x for x in rows if x["status"] == "completed"]
        if akt:
            n = len(_json.loads(akt[0]["outline"] or "[]"))
            out.append("rozepsané dílo: %s „%s“ (píšu sekci %d z %d)"
                       % (akt[0]["kind"] or "esej", akt[0]["title"],
                          min((akt[0]["current_index"] or 0) + 1, n or 1), n))
        if hot:
            out.append("dokončená psaná díla: %d, poslední %s „%s“"
                       % (len(hot), hot[0]["kind"] or "esej", hot[0]["title"]))
    except Exception as e:
        _hr._log.debug("self_state trvale (psani): %s", e)
    try:
        import json as _json
        akt = conn.execute("SELECT topic, current_index, curriculum FROM study_program "
                           "WHERE status='active' ORDER BY id ASC").fetchall()
        cek = conn.execute("SELECT topic FROM study_program WHERE status='pending' "
                           "ORDER BY id ASC").fetchall()
        if akt:
            n = len(_json.loads(akt[0]["curriculum"] or "[]"))
            # HANS_SELF_STATE_MORE_V1 — i KDY studium začalo (/tazatel 2. 10.: „od 21. srpna“, správně 17. 9.)
            _zac = ""
            try:
                _st = conn.execute("SELECT started_ts FROM study_program WHERE status='active' "
                                   "ORDER BY id ASC LIMIT 1").fetchone()
                if _st and _st[0]:
                    import datetime as _dtz
                    _z = _dtz.datetime.fromtimestamp(_st[0])
                    _zac = ", začal jsem %d. %d." % (_z.day, _z.month)
            except Exception:
                pass
            out.append("teď studuji: „%s“ (podtéma %d z %d%s)"
                       % (akt[0]["topic"], min((akt[0]["current_index"] or 0) + 1, n or 1), n, _zac))
        dalsi = [x["topic"] for x in list(akt[1:]) + list(cek)]
        if dalsi:
            out.append("další studium v pořadí: " + ", ".join("„%s“" % d for d in dalsi[:3]))
    except Exception as e:
        _hr._log.debug("self_state trvale (studium): %s", e)
    # HANS_SELF_STATE_MORE_V1 (2. 10.) — poslední obraz a poslední dočtená kniha.
    # /tazatel 2. 10.: o obraze „Sen“ (1. 10. 23:24, muž na stezce do lesa)
    # tvrdil „14 h 27 min od pátku“ a popsal zříceninu s notami; jako poslední
    # dočtenou knihu jmenoval rozečtený dokument místo dočtené knihy.
    try:
        import datetime as _dto
        r = conn.execute("SELECT ts, title, note FROM diary WHERE event_type='artwork' "
                         "ORDER BY ts DESC LIMIT 1").fetchone()
        if r and r["title"]:
            _d = _dto.datetime.fromtimestamp(r["ts"])
            _co = re.split(r"(?<=[.!?])\s", (r["note"] or "").strip(), 1)[0][:140]
            out.append("můj poslední obraz: „%s“ (namaloval jsem ho %d. %d. v %s)%s"
                       % (r["title"], _d.day, _d.month, _d.strftime("%H:%M"),
                          (" — " + _co) if _co else ""))
    except Exception as e:
        _hr._log.debug("self_state trvale (obraz): %s", e)
    # HANS_SELF_STATE_READING_V1 (4. 10.) — rozečtená kniha: /tazatel B1 Hans
    # tvrdil „čtu Zápisky z deníku Anny Frankové“ (nikdy nečetl), skutečná
    # četba byla jiná a v přehledu chyběla.
    try:
        r = conn.execute("SELECT ts, title FROM diary WHERE event_type='book_read' "
                         "AND ts > ? ORDER BY ts DESC LIMIT 1",
                         (time.time() - 3 * 86400,)).fetchone()
        if r and r["title"]:
            _cte = "teď čtu: %s" % re.sub(r"\s+—\s+kap\.\s*(\d+)$", r" (kapitola \1)",
                                          r["title"].strip())
            # HANS_SELF_STATE_AUTHOR_V1 (6. 10.) — bez autora v přehledu si ho
            # model na otázku „kdo to napsal“ vymyslel (/tazatel: jiné jméno
            # v každém pokusu, 3 ze 4) — a autor v knihovně celou dobu je.
            try:
                _kn = re.sub(r"\s+—\s+kap\..*$", "", r["title"].strip())
                _a = conn.execute("SELECT author, total_chapters FROM hans_library "
                                  "WHERE book_title = ? LIMIT 1", (_kn,)).fetchone()
                if _a and (_a[0] or "").strip():
                    _cte += " — autor: %s" % _a[0].strip()
                    if _a[1]:
                        _cte += ", kniha má %d kapitol" % int(_a[1])
            except Exception as _ae:
                _hr._log.debug("self_state trvale (autor): %s", _ae)
            out.append(_cte)
    except Exception as e:
        _hr._log.debug("self_state trvale (cteni): %s", e)
    try:
        import datetime as _dtk
        r = conn.execute("SELECT ts, title FROM diary WHERE event_type='book_finished' "
                         "ORDER BY ts DESC LIMIT 1").fetchone()
        if r and r["title"]:
            _d = _dtk.datetime.fromtimestamp(r["ts"])
            out.append("poslední dočtená kniha: %s (dočetl jsem ji %d. %d.)"
                       % (re.sub(r"^Do[čc]etl:\s*", "", r["title"]), _d.day, _d.month))
    except Exception as e:
        _hr._log.debug("self_state trvale (kniha): %s", e)
    return out


def predmet_vlastniho_dila(text: str, db_path: str) -> str:
    """HANS_A1_OWN_WORK_IN_PROMPT_V1 — vrátí slovo z otázky, které stojí
    v `lasting_facts` („bach“), když se otázka ptá na Hansův vlastní čin.
    '' = ne. Změřeno na 565 přepisech F1 (~50 dní): 15 má 2. osobu + sloveso
    vlastního činu, a jen 2 z nich mají předmět v přehledu děl (Bach na webu,
    tvorba webových stránek) — „co tě zaujalo na Heideggerovi“ nebo „kde jsi
    četl o Gulagu“ zůstávají pod A1 (opora by byla v paměti, ne v promptu)."""
    import unicodedata as _ud
    t = text or ""
    if not (_hr._VLASTNI_CIN.search(t) and _hr._DRUHA_OS.search(t)):
        return ""
    fold = lambda s: "".join(c for c in _ud.normalize("NFD", (s or "").lower())
                             if _ud.category(c) != "Mn")
    lf = fold(" ".join(_hr.lasting_facts(db_path)))
    if not lf:
        return ""
    lf_slova = set(re.findall(r"\w{4,}", lf))
    for w in re.findall(r"\w{4,}", fold(t)):
        if w in _hr._VLASTNI_STOP or w.startswith(_hr._VLASTNI_OBECNE):
            continue
        k = w[:4] if len(w) <= 6 else w[:5]
        if any(x.startswith(k) for x in lf_slova):
            return w
    return ""


def detail_vlastniho_dila(text: str, db_path: str) -> list:
    """Řádky navíc do promptu, když se otázka týká Hansova psaní / malby."""
    out = []
    t = text or ""
    psani, malba = bool(_hr._DOTAZ_PSANI.search(t)), bool(_hr._DOTAZ_MALBA.search(t))
    if not (psani or malba):
        return out
    conn = None
    try:
        import json as _json
        conn = _hr._ro(db_path)
        conn.row_factory = sqlite3.Row
        if psani:
            r = conn.execute("SELECT id, title, current_index, outline FROM writing_project "
                             "WHERE status='active' ORDER BY id DESC LIMIT 1").fetchone()
            if r:
                osn = _json.loads(r["outline"] or "[]")
                hot = int(r["current_index"] or 0)
                body = "; ".join("%d) %s%s" % (i + 1, str(o)[:160],
                                               " [napsáno]" if i < hot else "")
                                 for i, o in enumerate(osn))
                out.append("osnova mé rozepsané eseje „%s“ (o obsahu mluv JEN podle ní, "
                           "jiné příklady ani teorie v ní nejsou): %s" % (r["title"], body))
        if malba:
            r = conn.execute("SELECT title, note FROM diary WHERE event_type='artwork' "
                             "ORDER BY ts DESC LIMIT 1").fetchone()
            if r and r["title"]:
                out.append("můj poslední obraz „%s“ — co na něm je: %s"
                           % (r["title"], re.sub(r"\s+", " ", (r["note"] or "").strip())[:400]))
            # HANS_OWN_WORK_DETAIL_V2 (4. 10.) — i STARŠÍ obrazy: /tazatel zapřel
            # Lendla s Agassim i Göringa, které o pár tahů dřív sám vyjmenoval
            import datetime as _dta
            _vid, _dal = {((r["title"] if r else "") or "").strip().lower()}, []
            for x in conn.execute("SELECT ts, title FROM diary WHERE event_type='artwork' "
                                  "ORDER BY ts DESC LIMIT 20").fetchall()[1:]:
                _t = re.sub(r"\s+", " ", (x["title"] or "").strip())[:90]
                if _t and _t.lower() not in _vid:
                    _vid.add(_t.lower())
                    _d = _dta.datetime.fromtimestamp(x["ts"])
                    _dal.append("„%s“ (%d. %d.)" % (_t, _d.day, _d.month))
                if len(_dal) >= 8:
                    break
            if _dal:
                out.append("mé dřívější obrazy (od nejnovějšího): " + "; ".join(_dal))
            out.append("technika mých obrazů: DIGITÁLNÍ obrazy, které vytvářím na počítači "
                       "generativním modelem — žádné plátno, olej, štětce ani rozměry v centimetrech")
    except Exception as e:
        _hr._log.debug("detail_vlastniho_dila: %s", e)
    finally:
        if conn is not None:
            conn.close()
    return out


def _radek_zprav() -> str:
    """HANS_SELF_STATE_NEWS_V1 (4. 10.) — /tazatel 4. 10.: „nemám přístup ke
    sledování aktuálních zpráv“ (2×), protože výčet schopností se k otázce
    o sobě nevkládá. Řádek ze SKUTEČNÉHO stavu sběru (poslední běh)."""
    try:
        import os as _os
        import datetime as _dtz
        p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                          "data", "hans_zpravy.db")
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=3)
        try:
            ts = c.execute("SELECT MAX(ts) FROM sbery").fetchone()[0]
        finally:
            c.close()
        if not ts:
            return ""
        d = _dtz.datetime.fromtimestamp(ts)
        return ("zprávy: každou hodinu sbírám titulky z českých i zahraničních médií "
                "(naposledy %d. %d. v %s); CO v nich je, vím jen z výpisu zpráv — titulky "
                "ani odkazy si nevymýšlej, nabídni, že zprávy vypíšeš (i s odkazy)"
                % (d.day, d.month, d.strftime("%H:%M")))
    except Exception:
        return ""


def je_dotaz_na_vlastni_dilo(text: str, db_path: str) -> bool:
    """HANS_OWN_WORK_A1_SKIP_V1 (4. 10.) — ptá se věta na Hansův VLASTNÍ obraz
    nebo psaní? (pak opora je v promptu z `detail_vlastniho_dila` a brzda A1
    ani dohledání na Wikipedii nemají běžet). /tazatel 4. 10.: „jaké postavy
    v eseji rozebíráte?“ → „nemám spolehlivý záznam“, „jakou technikou jsi to
    dělal?“ → Wikipedie „Sen“ (stav ve spánku), Lendl s Agassim zapřen.
    Podmínka: slovo o psaní/malbě A ZÁROVEŇ 2. osoba nebo slovo z názvu
    vlastního obrazu/eseje (jinak „co víš o obrazu Mona Lisa“ zůstává pod A1)."""
    import unicodedata as _ud
    t = text or ""
    if not (_hr._DOTAZ_PSANI.search(t) or _hr._DOTAZ_MALBA.search(t)):
        return False
    if _hr._DRUHA_OS.search(t) or re.search(r"\bsv(?:[ůu]j|[ée]|ou|[ée]m|[ée]ho|ými?)\b", t, re.I):
        return True
    fold = lambda s: "".join(c for c in _ud.normalize("NFD", (s or "").lower())
                             if _ud.category(c) != "Mn")
    conn = None
    try:
        conn = _hr._ro(db_path)
        nazvy = [r[0] for r in conn.execute(
            "SELECT title FROM diary WHERE event_type='artwork' ORDER BY ts DESC LIMIT 20")]
        nazvy += [r[0] for r in conn.execute(
            "SELECT title FROM writing_project ORDER BY id DESC LIMIT 3")]
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()
    slova = {w[:5] for n in nazvy for w in re.findall(r"\w{5,}", fold(n))}
    return any(w[:5] in slova for w in re.findall(r"\w{5,}", fold(t))
               if not w.startswith(_hr._VLASTNI_OBECNE))


def lasting_facts(db_path: str) -> list:
    """HANS_SELF_STATE_LASTING_V1 — trvalé řádky pro chatový prompt (díla, studium)."""
    conn = None
    try:
        conn = _hr._ro(db_path)
        conn.row_factory = sqlite3.Row
        return _hr._self_state_trvale(conn) + [x for x in (_hr._radek_zprav(),) if x]
    except Exception as e:
        _hr._log.debug("lasting_facts: %s", e)
        return []
    finally:
        if conn is not None:
            conn.close()


def self_state_facts(db_path: str, max_items: int = 6,
                     mood: str = "", mood_reason: str = "",
                     runtime: dict = None) -> str:
    """Stručný VÝČET dnešní Hansovy činnosti z deníku (fakta, ne vyprávění).

    Proč: na „jak se máš?" / „co jsi dnes dělal?" model dosud odpovídal z ničeho
    a plodil vatu („Službu plním, a to je pro mne dostatečné") nebo komoleniny
    („zkoumal jsem historii zeleného, pana"). `recent_activity_answer` sice
    existuje, ale visí na frázovém detektoru a na dotaz typu „jak se máš" se
    nepřipojí. Tenhle blok je krátký a dává se do promptu jako FAKTA, ze
    kterých má persona čerpat — Hans pak řekne, co doopravdy dělal.

    Vrací "" když dnes není co hlásit (pak ať model nemluví o ničem).
    """
    import datetime as _dt
    start = _dt.datetime.now().replace(hour=0, minute=0, second=0,
                                       microsecond=0).timestamp()
    # (label, event_type) — pořadí = důležitost pro vyprávění o dni
    cats = [("studoval jsem", "study_note"),
            ("vytvořil jsem dílo", "work_artifact"),   # HANS_SELF_STATE_WORKS_V1
            ("napsal jsem esej", "work_created"),
            ("namaloval jsem", "artwork"),
            # HANS_SELF_STATE_ARTICLE_LABEL_V1 (1. 10.) — web_read jsou ČLÁNKY
            # (Wikipedie); z „četl jsem: Falešná kočička (film, 1926)“ model 3×
            # udělal přečtenou KNIHU („dočetl jsem Falešnou kočičku“). Knihy
            # mají vlastní řádek „zapsal jsem si ke knize“.
            ("četl jsem článek o", "web_read"),
            ("zapsal jsem si ke knize", "book_reflection"),
            ("napadlo mě", "synthesis_idea"),
            ("uvědomil jsem si o sobě", "self_critique"),
            ("hovořil jsem s Koláčem", "teddy_dialog")]
    out = []
    conn = None
    try:
        conn = _hr._ro(db_path)
        conn.row_factory = sqlite3.Row
        for label, etype in cats:
            rows = conn.execute(
                "SELECT title, note, data FROM diary WHERE event_type=? "
                "AND ts >= ? ORDER BY id DESC LIMIT 2", (etype, start)).fetchall()
            if not rows:
                continue
            det = []
            for r in rows:
                t = (r["title"] or "").strip()
                if etype == "work_artifact":
                    t = _hr._popis_dila(r["data"]) or t
                # HANS_SELF_STATE_IDEA_TEXT_V1 (1. 10.) — titulek nápadu jsou
                # semínka „A × B × C“; z „Hrabě Monte Christo (film) × …“ model
                # cizímu řekl „momentálně přehrávám Hraběte Monte Christo“.
                # Titulek sebekritiky je jen jméno persony („uvědomil jsem si
                # o sobě: Hans“). Obojí → první věta obsahu.
                elif etype == "synthesis_idea" and (r["data"] or "").strip():
                    t = "„%s“" % _hr._prvni_veta(r["data"])
                elif etype == "self_critique" and (r["note"] or "").strip():
                    t = _hr._prvni_veta(r["note"])
                if not t:
                    t = ((r["note"] or r["data"] or "").strip().split("\n")[0])[:60]
                if t:
                    det.append(t[:200] if etype == "work_artifact" else
                               t[:120] if etype in ("synthesis_idea", "self_critique")
                               else t[:70])
            if det:
                out.append("%s: %s" % (label, "; ".join(det)))
            if len(out) >= max_items:
                break
        trvale = _hr._self_state_trvale(conn)            # HANS_SELF_STATE_LASTING_V1
    except Exception as e:
        _hr._log.debug("self_state_facts: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _tiche:
                _hr.log_once(  # HANS_NO_SILENT_CTX_V1
                    _hr._log, "self_state_facts(ř. 1735)",
                    "self_state_facts: blok kontextu selhal (ř. 1735): %s", _tiche)
    head = []
    # HANS_SELF_STATE_AWAKE_V1 (7.8.) — PROVOZNÍ STAV jako první fakt.
    # Bez něj si model režim vymýšlel: 7.8. 10:52 tvrdil „Jsem v režimu
    # spánku, sleduji pouze bezpečnostní kamery", ačkoli spánek skončil
    # v 09:00 a hlídání bylo vypnuté. Stav je deterministicky zjistitelný —
    # tak ať ho čte, místo aby vyprávěl.
    if runtime:
        _st = []
        if runtime.get("sleeping") is not None:
            _st.append("spím (noční režim)" if runtime["sleeping"]
                       else "jsem vzhůru, v běžném provozu")
        if runtime.get("vision") is not None:
            _st.append("kamerou vidím" if runtime["vision"]
                       else "kameru mám vypnutou")
        # HANS_SELF_STATE_ASKER_VISIBLE_V1 (15. 9.) — plni handler JEN u otazky
        # na videni; stala zminka by byla semenko (HANS_SELF_STATE_NO_OFF_MODES_V1).
        if runtime.get("asker_visible") is True:
            _st.append("toho, kdo se m\u011b te\u010f pt\u00e1, vid\u00edm p\u0159ed kamerou")
        elif runtime.get("asker_visible") is False:
            _st.append("toho, kdo se m\u011b te\u010f pt\u00e1, p\u0159ed kamerou nevid\u00edm "
                       "\u2014 mluv\u00edme spolu jen p\u0159es zpr\u00e1vy")
        # HANS_SELF_STATE_NO_OFF_MODES_V1 (20.8.) — VYPNUTÝ hlídací režim se
        # NEZMIŇUJE. Doloženo 20.8.: na „co jsi dělal v noci?" Hans odpověděl
        # „byl jsem v režimu hlídání domu", ačkoli tenhle blok měl v promptu
        # a stálo v něm „hlídací režim je vypnutý" — tedy si protiřečil
        # s vlastním podkladem.
        # A/B změřeno: v izolaci model negaci zvládne (3/3 „byl vypnutý"),
        # v plném ~14 KB promptu ji překlopí. Zmínka je semínko, šum kolem
        # spouštěč — a semínko jde odstranit. Bez ní odpoví „nemám o tom
        # informace", což je pravda; obsah noci nese zbytek bloku (studium,
        # četba). Táž logika jako HANS_NUMERALS_AS_DIGITS_V1: odebrat důvod,
        # proč model improvizuje, místo hlídání výsledku.
        # ⚠️ Netýká se ostatních režimů: „kameru mám vypnutou" i „spím" se
        # uvádět MUSÍ — tam je výchozí očekávání OPAČNÉ (že vidí a bdí),
        # takže vynechání by vyrobilo chybu na druhou stranu.
        if runtime.get("guard"):
            _st.append("hlídací režim je zapnutý")
        if _st:
            head.append("teď: " + ", ".join(_st))
    if mood:
        head.append("nálada: %s%s" % (
            mood, (" (důvod: %s)" % mood_reason) if mood_reason else ""))
    if not out and not head and not trvale:
        return ""
    # Instrukce s TVAREM odpovědi: samotná fakta nestačila — persona je jen
    # olízla a vrátila vatu („Službu plním, a to je pro mne dostatečné").
    # Model potřebuje říct, KOLIK a CO má z bloku použít.
    return ("FAKTA O MĚ A O MÉM DNEŠKU — čerpej z NICH, nic si nepřidávej "
            "(co tu není, dnes nebylo):\n"
            + ("- " + "\n- ".join(head + out) if (head or out) else "")
            + (("\nCO JSEM VYTVOŘIL A CO STUDUJI (platí trvale, ne jen dnes — o stavu "
                "svých děl a studia mluv JEN podle tohohle):\n- " + "\n- ".join(trvale))
               if trvale else "")
            + "\n\nKdyž se ptá, jak se mám nebo co jsem dělal: odpověz 2–4 větami, "
              "řekni jak se cítím a PROČ, a jmenuj DVĚ KONKRÉTNÍ věci z dneška "
              "(téma studia, název díla, co jsem četl). Žádné obecné fráze "
              "typu „plním službu\" — ty nic neříkají."
              # HANS_SELF_STATE_AWAKE_V1 — bez téhle věty model řádek „teď:"
              # přečetl, ale stejně dodal vlastní verzi režimu.
              "\nO SVÉM REŽIMU (spánek, kamera, hlídání) mluv POUZE podle "
              "řádku „teď:\" výše. Nikdy netvrď, že něco přepínáš nebo "
              "jsi přepnul — sám to udělat neumíš, děje se to na povel.")


# ── HANS_DAY_AT_HOME_V1 (7.8.) — „co se dnes dělo v domě?" ───────────────────
# Nález C5: dotaz na DNEŠEK zpětně vracel AKTUÁLNÍ stav („na TV hraje X,
# vidím tu Y") — správná odpověď na jinou otázku. Hans neměl kam takový dotaz
# poslat: `night_summary` je až noční a je o něm samém, ne o dění v domě.
#
# ⚠️ Fakta se sbírají TADY, aby existoval JEDEN zdroj pravdy — `_write_night_
# summary` v `hans_routine` dělal totéž vlastním SQL. Druhá kopie by se časem
# rozešla (viz pravidlo „protáhni existující mechanismus" v CLAUDE.md).
def day_facts(db_path: str, date_str: Optional[str] = None) -> dict:
    """Fakta o jednom dni z deníku. Čistě SQL, žádný LLM, deferral-safe.

    Vrací dict s klíči: date, n_events, n_dialogs, types, people (se
    začátkem/koncem přítomnosti), reads, takeaways, films, moments.
    """
    import datetime as _dt
    day = date_str or _dt.datetime.now().strftime("%Y-%m-%d")
    out = {"date": day, "n_events": 0, "n_dialogs": 0, "types": [],
           "people": [], "reads": [], "takeaways": [], "films": [],
           "moments": []}
    conn = None
    try:
        conn = _hr._ro(db_path)
        D = "date(ts,'unixepoch','localtime')=?"

        def q(sql):
            try:
                return conn.execute(sql, (day,)).fetchall()
            except Exception:
                return []

        out["n_events"] = (q(f"SELECT COUNT(*) FROM diary WHERE {D}") or [[0]])[0][0]
        out["n_dialogs"] = (q("SELECT COUNT(*) FROM diary WHERE "
                              f"event_type='teddy_dialog' AND {D}") or [[0]])[0][0]
        out["types"] = [(r[0], r[1]) for r in q(
            f"SELECT event_type, COUNT(*) FROM diary WHERE {D} "
            "GROUP BY event_type ORDER BY COUNT(*) DESC LIMIT 5")]
        # Osoby VČETNĚ času — „co se dělo" je hlavně kdo tu byl a kdy.
        out["people"] = [(r[0], r[1], r[2]) for r in q(
            "SELECT title, MIN(ts), MAX(ts) FROM diary WHERE "
            f"event_type='person_seen' AND {D} AND title NOT IN "
            "('','Unknown','?','unknown_person') GROUP BY title ORDER BY MIN(ts)")]
        out["reads"] = [r[0] for r in q(
            f"SELECT DISTINCT title FROM diary WHERE event_type='web_read' AND {D} "
            "AND title<>'' ORDER BY ts DESC LIMIT 4")]
        out["takeaways"] = [r[0] for r in q(
            "SELECT coalesce(data,note) FROM diary WHERE "
            f"event_type='reading_takeaway' AND {D} AND coalesce(data,note)<>'' "
            "ORDER BY ts DESC LIMIT 2")]
        out["films"] = [r[0] for r in q(
            "SELECT DISTINCT title FROM diary WHERE event_type IN "
            f"('kodi_playing','movie_opinion') AND {D} AND title<>'' "
            "ORDER BY ts DESC LIMIT 3")]
        out["moments"] = [r[0] for r in q(
            "SELECT coalesce(NULLIF(note,''),data) FROM diary WHERE "
            f"COALESCE(importance,0)>=6 AND {D} AND "
            "coalesce(NULLIF(note,''),data)<>'' AND event_type NOT IN "
            "('human_chat','night_summary') ORDER BY importance DESC, ts DESC LIMIT 3")]
    except Exception as e:
        _hr._log.debug("day_facts: %s", e)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
    return out


def day_fact_lines(f: dict, config: dict = None,
                   asker: str = None) -> list:
    """`day_facts` → české věty pro LLM grounding i pro deterministický výpis.

    Jména se zobrazují přes `cz_names.display_name` — jinak by v textu byla
    tak, jak jsou klíče v konfiguraci (malá písmena, bez diakritiky).

    HANS_DAY_FACTS_PRIVACY_V1 (9. 9.) — `asker` je NEPOVINNÝ a bez něj se
    chování NEMĚNÍ. Je to schválně: druhý volající je noční souhrn
    (`hans_routine._write_night_summary`), který jména dostávat MUSÍ.
    Gate se tedy zapíná jen tam, kde odpověď čte člověk — dnes `/dnes`.
    """
    import time as _t

    def _nm(n):
        # HANS_DAY_AT_HOME_GENDER_V1 (7.8.) — jméno + ROD. Bez rodu hlasový
        # krok skloňoval ženská jména jako mužská („pan" u ženy);
        # týž fix jako HANS_SELF_INSIGHT_GENDER_V1 u vhledů.
        try:
            from scripts import cz_names
            disp = cz_names.display_name(n, config) or n
            g = cz_names.person_gender(n, config)
            if g == "žena":
                return "paní " + disp
            if g == "muž":
                return "pan " + disp
            return disp
        except Exception:
            return n

    def _hm(ts):
        return _t.strftime("%H:%M", _t.localtime(ts))

    lines = []
    # ── HANS_DAY_FACTS_PRIVACY_V1 (9. 9.) — ČTVRTÝ zdroj úniku domácnosti ──
    # 8. 9. se únik zavíral na TŘECH místech (prompt, surroundings, who_home)
    # a tohle čtvrté se minulo, protože `day_facts`/`day_fact_lines` tazatele
    # VŮBEC NEDOSTALY — `_cmd_dnes(handler, name, args)` ho přitom v parametru
    # má. Doloženo naživo 9. 9.: cizí „co se dnes delo doma?" →
    # „Dnes ráno, od 09:29 do 10:31, navštívila nás paní <jméno>." (i s časy).
    # Predikát je SDÍLENÝ (`cz_names.is_known_person`), ne nový.
    _cizi = False
    if asker:
        try:
            from scripts import cz_names as _czn
            _cizi = not _czn.is_known_person(asker, config)
        except Exception:
            _cizi = False          # fail-open: chyba predikátu nesmí umlčet dům
    if _cizi:
        # ⚠️ Odmítá se OBOJE — výčet i „nikoho jsem neviděl". Ta druhá věta
        # totiž cizímu prozradí, že je dům PRÁZDNÝ, což je při use-case
        # `/hlidej` (dovolená, prázdný dům) horší než jmenný výčet.
        # A říká se to VÝSLOVNĚ, nemlčí se: HANS_VISION_NOT_DENIED_V1 doložil,
        # že z vynechané věty si model domyslí popření vlastního zraku.
        if f.get("people") or f.get("n_events"):
            lines.append(_hr._PRIVACY_REFUSAL)
    elif f.get("people"):
        parts = []
        for name, t0, t1 in f["people"]:
            parts.append("%s (%s–%s)" % (_nm(name), _hm(t0), _hm(t1))
                         if t1 - t0 > 300 else "%s (%s)" % (_nm(name), _hm(t0)))
        lines.append("V domě jsem dnes viděl: " + ", ".join(parts) + ".")
    elif f.get("n_events"):
        # Jen když se ten den VŮBEC něco dělo. Na úplně prázdném dni musí
        # zůstat prázdný seznam, jinak by `_write_night_summary` považoval
        # fakta za neprázdná a nechal model psát reflexi o ničem (dřív spadl
        # na statistiku) — regrese chycená testem na dni bez záznamů.
        lines.append("Dnes jsem v domě nikoho neviděl.")
    if f.get("films"):
        lines.append("Na televizi běželo: " + ", ".join(f["films"]) + ".")
    if f.get("moments"):
        lines.append("Výrazné chvíle dne: "
                     + " / ".join(m.strip()[:180] for m in f["moments"]))
    if f.get("reads"):
        lines.append("Sám jsem četl: " + ", ".join(f["reads"]) + ".")
    if f.get("takeaways"):
        lines.append("Z četby mě zaujalo: "
                     + " / ".join(t.strip()[:180] for t in f["takeaways"]))
    if f.get("n_dialogs"):
        lines.append("Rozhovorů s Koláčem: %d." % f["n_dialogs"])
    return lines

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
