"""Funkce přesunuté z `scripts/hans_recall.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hr.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Optional
from datetime import datetime
import re

def first_memory_answer(db_path: str) -> str:
    """Nejstarší záznam v deníku — MIN(ts), deterministicky. Žádný LLM."""
    conn = None
    try:
        conn = _hr._ro(db_path)
        row = conn.execute(
            "SELECT ts, event_type, title, note FROM diary "
            "ORDER BY ts ASC LIMIT 1").fetchone()
        if not row:
            return "Můj deník je zatím prázdný, pane — nemám žádné vzpomínky."
        total = conn.execute("SELECT COUNT(*) FROM diary").fetchone()[0]
        ts, etype, title, note = row
        when = _hr._cz_when(ts)
        detail = ""
        if note:
            detail = f" — poznamenal jsem si tehdy: „{str(note).strip()[:120]}“"
        elif title:
            detail = f" — týkal se: {str(title).strip()[:80]}"
        return (f"Podíval jsem se do svého deníku, pane. Můj úplně nejstarší "
                f"záznam vznikl {when} (typ „{etype}“){detail}. Od té doby "
                f"mám zapsáno {total} záznamů. Nic staršího si nepamatuji — "
                f"dřívější vzpomínky nemám.")
    except Exception as e:
        _hr._log.warning("first_memory_answer selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _extract_topic(question: str) -> str:
    """Vytáhni téma z dotazu na čtení ('četl jsi o hradech?' → 'hradech').
    '' když dotaz téma nemá (obecné 'co jsi četl')."""
    q = (question or "").strip()
    m = _hr._SUBJECT_PAT.search(q)          # nejdřív explicitní předmět „o knize X“
    if not m:
        m = _hr._TOPIC_PAT.search(q)
    if not m:
        # slash tvar: „/cetl o hradech" → args = „o hradech"
        m = re.match(r"^o\s+(.{2,60}?)\s*\??$", q, re.IGNORECASE)
    if not m:
        return _hr._vytcene_tema(q)
    words = [w for w in re.findall(r"[\wěščřžýáíéúůďťňó-]+", m.group(1))
             if w.lower() not in _hr._STOPWORDS]
    # HANS_READING_TOPIC_SENTENCE_BOUND_V1 — reálné čtenářské téma je krátké
    # (1-4 slova: „hradech", „Sherlock Holmes"). Delší = pahýl z ukecané věty,
    # ne téma → radši prázdno = obecný výpis „co jsem četl", ne bogus „o ‚…'".
    if len(words) > 4:
        return _hr._vytcene_tema(q)
    # HANS_TOPIC_CONNECTIVES_V1 (14. 9.) — tema slozene jen ze spojek
    # a predlozek neni tema. Dolozeno: „cetl jsi tu knihu nebo jen neco o ni?"
    # -> „nebo jen o" a Hans odpovedel, ze o „nebo jen o" zaznam nema.
    # ⚠️ Spojky se NEPRIDAVAJI do _STOPWORDS: „Na zapadni fronte klid" je titul.
    # Zmereno na 1 170 realnych vetach: meni se jen dolozena veta.
    if not any(len(w) >= 3 for w in words if w.lower() not in _hr._TEMA_SPOJKY):
        return _hr._vytcene_tema(q)
    return " ".join(words).strip() or _hr._vytcene_tema(q)


# HANS_READING_FRONTED_TOPIC_V1 (4.9.) — VYTCENE TEMA JAKO ZALOHA.
# Doloženo 4.9. v BEZICIM Hansovi: „a rukodelna prace, cetl jsi o tom neco?"
# -> `_extract_topic` vrati '' (tema stoji PRED carkou, ve vete uz je jen
# anaforicke „o tom", a „tom"/„neco" jsou stopwordy) -> `/cetl` vysypal ctyri
# posledni cetby, ani jednu o remesle.
# Predikat je SDILENY (`hans_intent.vytcene_tema`) s branou na `/zajmy`
# v `chat_commands._thread_guard` — je to tataz trida vety, tak at se
# nerozejdou. Cisla mereni jsou u predikatu.
# ⚠️ Jen ZALOHA: kdyz tema najde puvodni cesta, vytceni se neptame.
def _vytcene_tema(question: str) -> str:
    try:
        from scripts.hans_intent import vytcene_tema
        return vytcene_tema(question)
    except Exception:
        return ""


def _topic_stems(topic: str) -> list[str]:
    """Hrubé pahýly pro LIKE — poslední 1-3 znaky pryč (české skloňování).
    Bere jen NEJDELŠÍ (nejspecifičtější) slovo tématu — shoda na obecném
    slově víceslovného tématu („kvantová" z „kvantová chromodynamika")
    by dala falešné „mám o tom záznam". Radši poctivé „nemám záznam"."""
    words = sorted((w for w in topic.split() if len(w) >= 3),
                   key=len, reverse=True)
    if not words:
        return []
    w = words[0]
    out = []
    for cut in (0, 1, 2, 3):
        stem = w[: len(w) - cut] if cut else w
        if len(stem) >= 3 and stem.lower() not in (s.lower() for s in out):
            out.append(stem)
    return out


# ── HANS_TOPIC_ENTITY_AWARE_V1 (21.8.) — ZEPTEJ SE, CO TO TÉMA JE ──────────
# Dosud se téma hledalo jako ŘETĚZEC: uřízni pár znaků a hledej podřetězec.
# Jenže „Václav Svoboda" není řetězec, je to OSOBA — a Hans to ví, entity
# store drží typované záznamy z jeho čtení (`etype='osoba'`). Jen se ho nikdo
# neptal, tak se místo toho vymýšlela pravidla o počtu uříznutých znaků.
# U osoby se proto nehledá pahýl, ale ŽÁDÁ SE CELÉ JMÉNO (křestní i příjmení),
# skloňování řeší táž funkce jako u entit. Tím zmizí kolize „Svoboda" ×
# „svobodou projevu" u kořene, ne záplatou.
def tema_entita(topic: str):
    """Známá entita pro dané téma (dict), nebo None. Nikdy nevyhodí výjimku —
    bez entity store se prostě hledá po staru."""
    t = (topic or "").strip()
    if not t:
        return None
    try:
        from scripts.hans_entities import EntityStore
        from scripts.config_io import load as _cio_load   # HANS_RECALL_CONFIG_IO_V1
        cfg = _cio_load()
        es = EntityStore(cfg, cfg.get("diary_db") or "data/hans_diary.db")
        return es.resolve(t)
    except Exception:
        return None


def jmeno_entity(ent) -> str:
    """Kanonické jméno bez závorkového upřesnění („Václav Svoboda (politik
    KSČ)" → „Václav Svoboda"). Prázdno, když entita není osoba ani postava."""
    if not ent or (ent.get("etype") not in ("osoba", "postava")):
        return ""
    try:
        from scripts.hans_entities import _PAREN
        return _PAREN.sub("", ent.get("name") or "").strip()
    except Exception:
        return (ent.get("name") or "").strip()


def osoba_sedi(text: str, jmeno: str) -> bool:
    """Je v textu CELÉ jméno osoby (každé jeho slovo), i skloňované?"""
    if not jmeno or not text:
        return False
    try:
        from scripts.hans_entities import _tokens, _tok_match
    except Exception:
        return jmeno.lower() in (text or "").lower()
    t_slova = _tokens(text)
    for w in _tokens(jmeno):
        if len(w) < 3:
            continue
        if not any(w == x or _tok_match(w, x) for x in t_slova):
            return False
    return True


def _vsechna_slova_sedi(text: str, topic: str) -> bool:
    """HANS_READING_TOPIC_ALLWORDS_V1 (21.8.) — u VÍCESLOVNÉHO tématu musí
    v záznamu sedět KAŽDÉ slovo, ne jen to nejdelší.

    Doloženo 21.8.: „četl jsi o Václavu Svobodovi?" vrátilo „Meditations —
    kap. 12", protože pahýl „Svobodo" (uříznuté dva znaky) sedl na
    „svobodou projevu" — a hledání se u prvního pahýlu se shodou zastaví,
    takže se k pahýlu „Svobod" a skutečnému článku nikdy nedostane.
    Příjmení Svoboda JE běžné slovo, takže řezáním se ta kolize odstranit
    nedá; odstraní ji až požadavek, aby sedělo i „Václav".
    Jednoslovné téma zůstává beze změny (není co křížit).
    """
    slova = [w for w in (topic or "").split() if len(w) >= 3]
    if len(slova) < 2:
        return True
    for w in slova:
        varianty = []
        for cut in (0, 1, 2, 3):
            v = w[:len(w) - cut] if cut else w
            if len(v) >= 3 and v not in varianty:
                varianty.append(v)
        if not any(re.search(r"(?i)\b" + re.escape(v), text) for v in varianty):
            return False
    return True


def _dedup_cteni(rows, delsi_vyhrava: bool = False):
    """HANS_READING_DEDUP_V1 — jeden titul = jeden řádek výpisu.

    `rows` jsou (ts, event_type, title, snip) seřazené od nejnovějšího.
    Klíč je normalizovaný TITUL (ne dvojice s typem): týž článek bývá
    zapsaný pod několika typy a uživateli je to jedno — vidí dvakrát totéž.
    `delsi_vyhrava` u tématického dotazu ponechá nejobsáhlejší úryvek,
    protože tam je hodnota v poznámce, ne v názvu.
    """
    nej = {}
    poradi = []
    kapitoly = {}          # HANS_READING_CHAPTERS_V1 — čísla kapitol na klíč
    for r in rows:
        t = (r[2] or "").strip().lower()
        if not t:
            poradi.append(r)          # bez názvu nelze slučovat
            continue
        # HANS_READING_CHAPTERS_V1 (3.9.) — kniha čtená po kapitolách zabrala
        # celý výpis: „Já robot — kap. 41 / 40 / 39 / 38" jsou čtyři různé
        # tituly, takže je dedup na titulu neslučoval. Klíč je proto kniha BEZ
        # kapitoly; rozsah se vrátí do názvu níž, ať se neztratí, kolik toho
        # přečetl.
        m = _hr._KAP_PAT.search(t)
        klic = t[:m.start()].strip(" -—–") if m else t
        if m:
            try:
                kapitoly.setdefault(klic, []).append(int(m.group(1)))
            except ValueError:
                pass
        stav = nej.get(klic)
        if stav is None:
            nej[klic] = r
            poradi.append(("__klic__", klic))
        elif delsi_vyhrava and len(str(r[3] or "")) > len(str(stav[3] or "")):
            # ponech novější datum, ale obsažnější úryvek
            nej[klic] = (stav[0], stav[1], stav[2], r[3])
    out = []
    for x in poradi:
        if isinstance(x, tuple) and len(x) == 2 and x[0] == "__klic__":
            r = nej[x[1]]
            ks = sorted(set(kapitoly.get(x[1], [])))
            if len(ks) > 1:            # sloučeno víc kapitol → ukaž rozsah
                titul = _hr._KAP_PAT.sub("", str(r[2] or "")).strip(" -—–")
                r = (r[0], r[1], "%s — kap. %d–%d" % (titul, ks[0], ks[-1]),
                     r[3])
            out.append(r)
        else:
            out.append(x)
    return out


def _je_z_posledni_noci(ts: float) -> bool:
    """Patri sen k noci, ktera prave skoncila (nebo zacina)?

    ⚠️ Neni to „dnesni datum". Sny se zapisuji ve dvou vlnach — zmereno
    na 303 zaznamech: **188 ve 22 hodin a 94 o pulnoci** — takze sen z 22:01
    vcerejsiho dne je „dnes v noci", kdezto podle kalendare je vcerejsi.
    Hranice je proto 18:00 predchoziho dne, ne pulnoc.
    """
    ted = datetime.now()
    dnes0 = ted.replace(hour=0, minute=0, second=0, microsecond=0)
    hranice = dnes0.timestamp() - (24 - _hr._DREAM_DEN_ZACATEK_H) * 3600
    return float(ts) >= hranice


def _pta_se_na_dnesek(question: str) -> bool:
    """Ptal se vylozene na DNESNI noc? (pak je „nic" poctiva odpoved)"""
    q = _hr._fold(question or "").lower()
    return bool(re.search(r"\b(dnes\w*|dneska|v noci|tuhle noc|te noci|"
                          r"minulou noc|posledni noc)\b", q))


def dream_answer(db_path: str, question: str = "",
                 limit: int = 3, asker: Optional[str] = None) -> str:
    """Co se Hansovi zdalo — z deniku, deterministicky.

    S tematem v dotazu -> hledani mezi sny; bez tematu -> posledni sen.
    """
    oslov = _hr._cz_address(asker) if asker else "pane"
    topic = _hr._extract_topic(question)
    conn = None
    try:
        conn = _hr._ro(db_path)
        if topic:
            rows = []
            videno = set()
            for stem in _hr._topic_stems(topic):
                for r in conn.execute(
                        "SELECT ts, COALESCE(NULLIF(note,''),data) "
                        "FROM diary WHERE event_type='dream' "
                        "AND COALESCE(NULLIF(note,''),data) LIKE ? "
                        "ORDER BY ts DESC LIMIT ?", ("%%%s%%" % stem, limit * 3)):
                    # LIKE je jen LEVNÝ PŘEDVÝBĚR — sám o sobě chytá i pahýl
                    # UVNITŘ jiného slova. Doloženo při stavbě: téma „hradech"
                    # (pahýl „hrad") vytáhlo sen o ZA-HRAD-Ě a Hans na dotaz po
                    # hradech odpověděl „ano, zdálo se mi" a vypsal zahradu.
                    # Falešné potvrzení je horší než žádný nález, proto se
                    # shoda ověří ještě na ZAČÁTKU SLOVA.
                    if not re.search(r"(?<![\w])" + re.escape(stem),
                                     r[1] or "", re.IGNORECASE):
                        continue
                    if r[0] in videno:
                        continue
                    videno.add(r[0]); rows.append(r)
            rows.sort(key=lambda r: -r[0])
            rows = rows[:limit]
            if not rows:
                # ⚠️ Formulace je schválně o HLEDÁNÍ, ne o neexistenci.
                # `_topic_stems` je hrubý pahýl a české střídání kmene mu
                # uteče: doloženo „o vlacích" × sen o „vlacích" zapsaný jako
                # „vlaky" (k/c). Tvrdit za takového stavu „nic se mi nezdálo"
                # by bylo falešné zapření — což je tatáž třída chyby, kterou
                # tenhle příkaz opravuje. Radši přiznat mez hledání.
                return ("Sny si zapisuji, %s, ale o „%s“ jsem v nich "
                        "nic nenašel. Nebudu si vymýšlet — zkuste to prosím "
                        "říct jinak, hledám podle slov." % (oslov, topic))
            lines = ["– %s: %s" % (_hr._cz_date(ts), _hr._zkrat_sen(txt))
                     for ts, txt in rows]
            return ("Ano, %s — o „%s“ se mi zdálo:\n%s"
                    % (oslov, topic, "\n".join(lines)))
        row = conn.execute(
            "SELECT ts, COALESCE(NULLIF(note,''),data) FROM diary "
            "WHERE event_type='dream' ORDER BY ts DESC LIMIT 1").fetchone()
        if not row or not (row[1] or "").strip():
            return "Žádný sen zatím zapsaný nemám, %s." % oslov
        ts, txt = row[0], row[1]
        if _hr._je_z_posledni_noci(ts):
            return "Dnes v noci se mi zdálo tohle, %s:\n%s" % (oslov, _hr._zkrat_sen(txt))
        if _hr._pta_se_na_dnesek(question):
            # Nevydavat starsi sen za dnesni — to je tatáž trida chyby jako
            # falesny narok na pamet.
            return ("Dnes v noci se mi nic nezdálo, %s — aspoň nic, co bych si "
                    "byl zapsal. Poslední sen mám z %s:\n%s"
                    % (oslov, _hr._cz_date(ts), _hr._zkrat_sen(txt)))
        return ("Poslední sen mám v deníku z %s, %s:\n%s"
                % (_hr._cz_date(ts), oslov, _hr._zkrat_sen(txt)))
    except Exception as e:
        _hr._log.debug("dream_answer: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _zkrat_sen(txt: str, limit: int = 600) -> str:
    t = " ".join(str(txt or "").split())
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def reading_answer(db_path: str, question: str = "",
                   limit: int = 4, asker: Optional[str] = None) -> str:
    """Co/kdy jsem četl — reálné čtecí eventy z deníku, deterministicky.
    S tématem v dotazu → hledání; bez → poslední čtení.

    HANS_READING_ASKER_V1 (5.9.) — oslovení podle TAZATELE. Dosud tu byly
    ctyri natvrdo psane „pane", takze vypis cetby oslovoval „pane" i zenu.
    `_cmd_cetl` jmeno mel a jen ho nepredaval. Vzor je tentyz jako o par set
    radku vys (`oslov = _cz_address(asker) if asker else "pane"`), aby v tomhle
    souboru nebyly dve pravdy o osloveni.
    ⚠️ „pane" zustava JEN jako fallback pro volani BEZ tazatele (skripty,
    regresni behoun) — tam neni z ceho vokativ odvodit."""
    oslov = _hr._cz_address(asker) if asker else "pane"   # HANS_READING_ASKER_V1
    topic = _hr._extract_topic(question)
    conn = None
    try:
        conn = _hr._ro(db_path)
        qmarks = ",".join("?" * len(_hr._READ_TYPES))
        if topic:
            # HANS_TOPIC_ENTITY_AWARE_V1 — je téma ZNÁMÁ OSOBA? Pak se
            # nehledá pahýl, ale celé jméno (viz komentář u tema_entita).
            _osoba = _hr.jmeno_entity(_hr.tema_entita(topic))
            if _osoba:
                _hr._log.info("HANS_TOPIC_ENTITY_AWARE_V1: téma %r je osoba %r "
                          "→ vyžaduji celé jméno", topic[:40], _osoba)
            # hledej podle tématu (title i note, hrubé stemy na skloňování)
            rows = []
            for stem in _hr._topic_stems(topic):
                like = f"%{stem}%"
                cand = conn.execute(
                    f"SELECT ts, event_type, title, "
                    f"substr(COALESCE(NULLIF(data,''),note),1,160) "
                    f"FROM diary WHERE event_type IN ({qmarks}) "
                    f"AND (title LIKE ? OR note LIKE ? OR data LIKE ?) "
                    f"ORDER BY ts DESC LIMIT ?",
                    (*_hr._READ_TYPES, like, like, like, limit * 4)).fetchall()
                # LIKE nemá hranice slov („hradech" chytá i „Vinohradech")
                # → post-filtr: stem musí začínat na hranici slova
                _wb = re.compile(r"(?i)\b" + re.escape(stem))
                rows = []
                for r in cand:
                    _txt = " ".join(str(x) for x in r[2:] if x)
                    if not _wb.search(_txt):
                        continue
                    if _osoba:
                        # u osoby rozhoduje jméno, ne pahýl tématu
                        if not _hr.osoba_sedi(_txt, _osoba):
                            continue
                    # HANS_READING_TOPIC_ALLWORDS_V1 — víceslovné téma musí
                    # sednout celé, jinak stačí náhodná shoda na jednom slově.
                    elif not _hr._vsechna_slova_sedi(_txt, topic):
                        continue
                    rows.append(r)
                if rows:
                    break
            if not rows:
                return (f"Prošel jsem svůj deník, {oslov} — o „{topic}“ v něm "
                        f"žádný záznam čtení nemám. Nebudu si vymýšlet; "
                        f"jestli chcete, mohu si o tom něco přečíst.")
            rows = _hr._dedup_cteni(rows, delsi_vyhrava=True)[:limit]
            # HANS_READING_KODI_SPLIT_V1 — tady se NEFILTRUJE. Na cileny
            # dotaz („cetl jsi o Jakubove zebriku?") je vylouceni FALESNE
            # ZAPRENI — presne trida chyby, kterou recall resi od 15.7.
            # Zaznam tedy zustava, jen rekne, odkud se vzal.
            _kodi = _hr._kodi_tituly(conn)
            lines = []
            for ts, etype, title, snip in rows:
                t = (title or "").strip() or "(bez názvu)"
                line = f"– {_hr._cz_date(ts)}: {t}"
                if _hr._je_k_filmu(etype, title, _kodi):
                    line += " (k filmu)"
                if snip:
                    line += f" — {str(snip).strip()}"
                lines.append(line)
            return (f"Ano, {oslov} — tohle mám o „{topic}“ ve svém deníku "
                    f"skutečně zapsáno:\n" + "\n".join(lines))
        # bez tématu → poslední čtení
        # HANS_READING_DEDUP_V1 (21.8.) — týž titul má v deníku i několik
        # záznamů (web_read + reading_takeaway, opakované čtení), takže se
        # z LIMITu ukrajovala místa a výpis „posledních čtyř" ukázal jen dvě
        # věci dvakrát (doloženo 20.8. uživatelem i 21.8.: „Pride and
        # Prejudice — kap. 46" 2×, „Design" 2×). Načti víc a ořízni AŽ po
        # sloučení. Táž zásada jako HANS_SOURCES_DEDUP_V2 u /zdroje.
        rows = conn.execute(
            f"SELECT ts, event_type, title, "
            f"substr(COALESCE(NULLIF(data,''),note),1,120) "
            f"FROM diary WHERE event_type IN ({qmarks}) "
            f"ORDER BY ts DESC LIMIT ?",
            (*_hr._READ_TYPES, limit * 8)).fetchall()
        # HANS_READING_KODI_SPLIT_V1 — filmy ven JESTE PRED dedupem i orezem,
        # jinak by ukrajovaly mista z LIMITu presne jako duplicity, ktere
        # resil HANS_READING_DEDUP_V1 (proto je nasobitel 5 → 8).
        _kodi = _hr._kodi_tituly(conn)          # JEDNOU, ne v kazde iteraci
        rows = [r for r in rows if not _hr._je_k_filmu(r[1], r[2], _kodi)]
        rows = _hr._dedup_cteni(rows)[:limit]
        if not rows:
            return (f"V deníku zatím žádné čtení zapsané nemám, {oslov}.")
        lines = []
        for ts, etype, title, snip in rows:
            t = (title or "").strip() or "(bez názvu)"
            kind = {"book_read": "kniha", "study_note": "studium",
                    "book_completion_reflection": "dočtená kniha"}.get(
                        etype, "četba")
            lines.append(f"– {_hr._cz_date(ts)} ({kind}): {t}")
        return (f"Podle mého deníku jsem naposledy četl toto, {oslov}:\n"
                + "\n".join(lines))
    except Exception as e:
        _hr._log.warning("reading_answer selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
