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

def _conv_keywords(query: str) -> list:
    """Obsahová slova z dotazu (bez stopwords, ≥4 znaky), stemovaná na prefix
    kvůli českému skloňování (sobotní→sobot, oběd→oběd, recept→recep)."""
    kws = []
    for w in re.findall(r"[a-zA-Zá-žÁ-Ž]{4,}", (query or "").lower()):
        if w in _hr._CONV_STOP:
            continue
        stem = w[:5]
        if stem not in kws:
            kws.append(stem)
    return kws


def _kw_groups(query: str) -> list:
    """Query klíčová slova → shluky pro shodu (zpráva odpovídá shluku, obsahuje-li
    kterýkoli stem). Rozšíří o synonyma."""
    groups = []
    for kw in _hr._conv_keywords(query):
        grp = {kw}
        for cl in _hr._SYN_CLUSTERS:
            if kw in cl:
                grp |= cl
        groups.append(grp)
    return groups


def _day_bounds(d) -> tuple:
    start = datetime(d.year, d.month, d.day).timestamp()
    return (start, start + 86400)


def _pick_year(day: int, month: int, year: Optional[int], dt_now) -> Optional[int]:
    """Rok z dotazu, nebo NEJBLIŽŠÍ MINULÝ výskyt toho dne (27. dubna v červenci
    2026 → 2026; 27. prosince v červenci 2026 → 2025)."""
    if year:
        return year
    for y in (dt_now.year, dt_now.year - 1):
        try:
            if datetime(y, month, day).date() <= dt_now.date():
                return y
        except ValueError:
            return None
    return None


def resolve_time_range(query: str, now: Optional[float] = None):
    """Časová reference v dotazu → (start_ts, end_ts, popisek), jinak None.
    Umí: dnes / včera / předevčírem / den v týdnu (nejbližší minulý) /
    konkrétní datum slovem i číslem / tento a minulý týden."""
    now = now or time.time()
    q = (query or "").lower()
    dt_now = datetime.fromtimestamp(now)

    # 1) konkrétní datum slovem: „27. dubna 2026", „27 dubna"
    m = re.search(r"\b(\d{1,2})\.?\s*([a-zá-ž]{4,10})(?:\s+(\d{4}))?", q)
    if m and m.group(2) in _hr._MES_WORD:
        day, mon = int(m.group(1)), _hr._MES_WORD[m.group(2)]
        year = _hr._pick_year(day, mon, int(m.group(3)) if m.group(3) else None, dt_now)
        if year:
            try:
                d = datetime(year, mon, day)
                lo, hi = _hr._day_bounds(d)
                return (lo, hi, _hr._cz_when(lo, with_weekday=True).split(" v ")[0])
            except ValueError:
                pass

    # 2) konkrétní datum číslem: „27.4.2026", „27. 4.", „27/4"
    # (obě tečky povinné — jinak by „verze 2.5" byla 2. května)
    m = (re.search(r"\b(\d{1,2})\s*\.\s*(\d{1,2})\s*\.(?:\s*(\d{4}))?", q)
         or re.search(r"\b(\d{1,2})\s*/\s*(\d{1,2})(?:\s*/\s*(\d{4}))?", q))
    if m:
        day, mon = int(m.group(1)), int(m.group(2))
        if 1 <= day <= 31 and 1 <= mon <= 12:
            year = _hr._pick_year(day, mon,
                              int(m.group(3)) if m.group(3) else None, dt_now)
            if year:
                try:
                    d = datetime(year, mon, day)
                    lo, hi = _hr._day_bounds(d)
                    return (lo, hi, _hr._cz_when(lo, with_weekday=True).split(" v ")[0])
                except ValueError:
                    pass

    # 3) týdenní rozsahy
    if re.search(r"\bminul\w*\s+t[ýy]d", q):
        mon_this = dt_now.date().toordinal() - dt_now.weekday()
        lo = datetime.fromordinal(mon_this - 7).timestamp()
        return (lo, lo + 7 * 86400, "minulý týden")
    if re.search(r"\bt[ée]nhle\s+t[ýy]d|\btento\s+t[ýy]d|\btento\s+t[ýy]den", q):
        mon_this = dt_now.date().toordinal() - dt_now.weekday()
        lo = datetime.fromordinal(mon_this).timestamp()
        return (lo, now, "tento týden")

    # 4) relativní dny
    target = None
    if re.search(r"\bp[řr]edev[čc][íi]r", q):
        target = dt_now.date().toordinal() - 2
    elif re.search(r"\bv[čc]er", q):
        target = dt_now.date().toordinal() - 1
    elif re.search(r"\bdnes|\bdneska", q):
        target = dt_now.date().toordinal()
    else:
        for name, wd in _hr._DNY.items():
            if re.search(r"\b" + name + r"\b", q):
                # nejbližší minulý (nebo dnešní) výskyt daného dne v týdnu
                back = (dt_now.weekday() - wd) % 7
                target = dt_now.date().toordinal() - back
                break
    if target is None:
        return None
    lo, hi = _hr._day_bounds(datetime.fromordinal(target))
    return (lo, hi, _hr._cz_when(lo, with_weekday=True).split(" v ")[0])


def _resolve_day_reference(query: str, now: float):
    """Zpětně kompatibilní obal (start_ts, end_ts) — bez popisku."""
    r = _hr.resolve_time_range(query, now)
    return (r[0], r[1]) if r else None


def conversation_recall(db_path: str, query: str, days: int = 30,
                        min_age_hours: float = 0.3, limit: int = 4,
                        person: Optional[str] = None) -> list:
    """Recall PŘEDCHOZÍHO rozhovoru (deterministicky). Když dotaz nese ČASOVOU
    referenci („v pátek"/„včera"), zúží se na TEN den (nezávisle na doslovných
    slovech) a v něm seřadí dle shody (se synonymy). Jinak čistě dle klíčových
    slov přes celé okno. Vynechá právě proběhlou výměnu (min_age_hours). Vrací
    [(kdy, note)] nebo []."""
    now = time.time()
    groups = _hr._kw_groups(query)
    day_ref = _hr._resolve_day_reference(query, now)
    # Cizí rozhovory se nevynášejí — každý dostane jen své (title = osoba).
    who = (person or "").strip().lower()
    p_sql = " AND lower(title)=?" if who else ""
    try:
        conn = _hr._ro(db_path)
        if day_ref:
            lo, hi = day_ref
            hi = min(hi, now - min_age_hours * 3600)
            args = [lo, hi] + ([who] if who else [])
            rows = conn.execute(
                "SELECT ts, note FROM diary WHERE event_type='human_chat' AND "
                "ts>=? AND ts<=?" + p_sql + " ORDER BY ts DESC",
                tuple(args)).fetchall()
        else:
            args = [now - days * 86400, now - min_age_hours * 3600] + (
                [who] if who else [])
            rows = conn.execute(
                "SELECT ts, note FROM diary WHERE event_type='human_chat' AND "
                "ts>=? AND ts<=?" + p_sql + " ORDER BY ts DESC",
                tuple(args)).fetchall()
        conn.close()
    except Exception:
        return []

    def _score(note):
        low = (note or "").lower()
        return sum(1 for g in groups if any(s in low for s in g))

    if day_ref:
        # den je kotva → vezmi všechny, seřaď dle shody (i skóre 0 projde, ale
        # nejrelevantnější první); prázdný den → nic
        scored = [( _score(n), ts, n) for ts, n in rows if (n or "").strip()]
        scored.sort(key=lambda x: (-x[0], -x[1]))
    else:
        if not groups:
            return []
        need = max(1, (len(groups) + 1) // 2)
        scored = [(s, ts, n) for ts, n in rows
                  for s in (_score(n),) if s >= need]
        scored.sort(key=lambda x: (-x[0], -x[1]))
    return [(_hr._cz_when(ts), (note or "").strip()) for _s, ts, note in scored[:limit]]


def _split_exchange(note: str, person: str) -> tuple:
    """'jmeno: dotaz\\nHans: odpověď' → (dotaz, odpověď). Robustní vůči tvaru."""
    txt = (note or "").strip()
    m = re.split(r"\n(?=\w+:)", txt, maxsplit=1)
    user = m[0].strip()
    reply = m[1].strip() if len(m) > 1 else ""
    user = re.sub(r"^\s*%s\s*:\s*" % re.escape(person or ""), "", user,
                  flags=re.IGNORECASE)
    user = re.sub(r"^\s*\w+\s*:\s*", "", user) if ":" in user[:20] else user
    reply = re.sub(r"^\s*\w+\s*:\s*", "", reply)
    return (user.strip(), reply.strip())


def _day_notes(conn, lo: float, hi: float, limit: int = 4) -> list:
    """Co si Hans ten den zapsal (mimo vjemový šum) — pro poctivý fallback."""
    rows = conn.execute(
        "SELECT ts, event_type, title, "
        "substr(COALESCE(NULLIF(data,''),note),1,110) "
        "FROM diary WHERE ts>=? AND ts<? ORDER BY ts ASC", (lo, hi)).fetchall()
    out = []
    for ts, etype, title, snip in rows:
        if etype in _hr._DIARY_NOISE:
            continue
        txt = (str(snip or "").strip() or str(title or "").strip())
        if txt.startswith("{"):     # HANS_DAY_NOTES_NO_JSON_V1: syrovy JSON (sablona nalady) neni zapis
            txt = str(title or "").strip()
        if not txt:
            continue
        out.append("– %s: %s" % (_hr._cz_date(ts), txt))
        if len(out) >= limit:
            break
    return out


def _summarize_topics(config: Optional[dict], lines: list) -> Optional[str]:
    """Kondenzace SKUTEČNÝCH replik na témata (materiál injektovaný → nízké
    riziko konfabulace). LLM dole / herní mód → None a volající vypíše seznam."""
    if not config or not lines:
        return None
    try:
        from scripts.ollama_client import ollama_generate
    except Exception:
        return None
    model = ((config.get("evening_reflection", {}) or {}).get("model")
             or "jobautomation/OpenEuroLLM-Czech:latest")
    body = "\n".join("- %s" % l for l in lines[:80])
    try:
        out = ollama_generate(
            model, "Repliky:\n%s\n\nO čem se bavili?" % body,
            system=_hr._TOPIC_SUM_SYSTEM, config=config, timeout=90, keep_alive=0,
            options={"temperature": 0.2, "num_predict": 120, "num_ctx": 8192})
    except Exception as e:
        _hr._log.warning("_summarize_topics selhal: %s", e)
        return None
    out = (out or "").strip()
    return out.split("\n")[0].strip() if out else None


def _extract_conv_topic(query: str) -> str:
    """Téma z dotazu na konkrétní rozhovor („připomeň rozhovor o Maradonovi"
    → „Maradonovi"; „pošli detail o rychlem obedu na sobotu 10.7. 14:18"
    → „rychlem obedu"). '' když dotaz téma nemá (obecné „o čem jsme se bavili").
    Časové údaje se odřežou — nejsou téma, jen zpřesnění."""
    q = (query or "").strip()
    if re.search(r"o\s+[čc]em\b", q, re.IGNORECASE):
        return ""    # „o čem jsme se bavili" = obecný sumář, ne téma
    m = _hr._TOPIC_ASK.search(q)
    if not m:
        b = _hr._TOPIC_BARE.search(q)
        return b.group(1) if b else ""
    topic = m.group(1).strip()
    # odřež datum/čas/den z konce i zbytky předložek
    prev = None
    while topic and topic != prev:
        prev = topic
        topic = _hr._TOPIC_TAIL.sub(" ", topic).strip()
        topic = re.sub(r"\s+(na|z|ze|v|ve|o)$", "", topic).strip()
    topic = re.sub(r"^(ten|ta|to|toho|tom)\s+", "", topic,
                   flags=re.IGNORECASE).strip()
    return "" if len(topic) < 3 else topic


def _is_echo_exchange(user: str, reply: str) -> bool:
    """Ozvěna, ne zdroj — vyžádané převyprávění / abstinence / zdvořilost."""
    return bool(_hr._NOISE_REPLY.search(reply or "")
                or _hr._NOISE_USER.search((user or "").strip()))


def topic_conversation(db_path: str, person: Optional[str], topic: str,
                       limit: int = 3, days: int = 120) -> str:
    """Doslovné vybavení konkrétního rozhovoru na dané téma (obě strany).
    Deterministické hledání v human_chat té osoby. Nic nenalezeno → přizná to."""
    who = (person or "").strip().lower()
    # Skóruj podle VŠECH obsahových slov tématu, ne jen podle nejdelšího —
    # „rychlem obedu“ musí trefit výměnu, kde je OBOJÍ (jinak se chytne
    # náhodná zmínka „rychle“). Diakritika folded (uživatel píše bez háčků),
    # koncovky uříznuté (skloňování: „obedu“ → „obed“).
    toks = [w for w in re.split(r"[^\wá-žÁ-Ž]+", topic.lower()) if len(w) >= 4]
    pref = [_hr._fold(w)[: max(4, len(w) - 2)] for w in toks]
    if not who or not pref:
        return ""
    conn = None
    try:
        conn = _hr._ro(db_path)
        now = time.time()
        rows = conn.execute(
            "SELECT ts, note FROM diary WHERE event_type='human_chat' "
            "AND lower(title)=? AND ts>=? ORDER BY ts DESC",
            (who, now - days * 86400)).fetchall()
        scored = []
        for ts, n in rows:
            fn = _hr._fold(n or "").lower()
            score = sum(1 for p in pref if p in fn)
            if not score:
                continue
            u, r = _hr._split_exchange(n, who)
            if _hr._is_echo_exchange(u, r):
                continue    # ozvěna (vyžádané převyprávění / abstinence)
            scored.append((score, ts, n))
        best = max((s for s, _t, _n in scored), default=0)
        hits = [(ts, n) for s, ts, n in scored if s == best]
        if not hits:
            return ("O „%s“ nemám s vámi žádný rozhovor zapsaný. Nebudu si ho "
                    "vymýšlet." % topic)
        found = len(hits)
        # PŮVODNÍ výměna má přednost před pozdějšími (v nich už Hans o tématu
        # jen mluví — a případně si domýšlí; origin nese skutečný obsah).
        hits = sorted(hits, key=lambda x: x[0])[:limit]
        # Přiber bezprostřední POKRAČOVÁNÍ (do 10 min) — „posli postup přípravy“
        # je samostatná výměna, ale nese vlastní jádro odpovědi (celý recept).
        by_ts = dict(hits)
        for ts0, _n0 in list(hits):
            for ts, n in rows:
                if ts0 < ts <= ts0 + 600 and ts not in by_ts:
                    u2, r2 = _hr._split_exchange(n, who)
                    if not _hr._is_echo_exchange(u2, r2):
                        by_ts[ts] = n
        hits = sorted(by_ts.items())[: limit + 2]
        parts = []
        for ts, note in hits:
            u, r = _hr._split_exchange(note, who)
            # Odpověď dáváme CELOU (recept/postup se nesmí utnout uprostřed) —
            # je to doslovný zápis, ne převyprávění.
            blk = "[%s]\nVy: „%s“" % (_hr._cz_when(ts), u[:300])
            if r:
                blk += "\nJá: „%s“" % (r if len(r) <= 1400
                                       else r[:1400] + " …(zkráceno)")
            parts.append(blk)
        out = ("Tady je, co o „%s“ máme v deníku doslova zapsáno:\n\n%s"
               % (topic, "\n\n".join(parts)))
        if found > len(hits):
            out += "\n\n(K tomu tématu mám ještě %d starších výměn.)" % (
                found - len(hits))
        # Navázání: uživatel může rovnou upřesnit („zjisti o tom víc“).
        out += ("\n\nChcete-li, mohu si o tom zjistit víc — stačí říct "
                "„zjisti víc o %s“." % topic)
        return out
    except Exception as e:
        _hr._log.warning("topic_conversation selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def chat_summary(db_path: str, person: Optional[str], query: str = "",
                 now: Optional[float] = None, max_lines: int = 12,
                 config: Optional[dict] = None,
                 detail_max: int = 6) -> str:
    """Sumář toho, o čem se daná osoba s Hansem bavila. S časovou referencí
    v dotazu („v pátek", „27. dubna 2026", „minulý týden") → jen to období;
    bez ní → poslední den, kdy spolu mluvili. Žádný LLM, nulová konfabulace."""
    now = now or time.time()
    rng = _hr.resolve_time_range(query, now)
    conn = None
    try:
        conn = _hr._ro(db_path)
        who = (person or "").strip().lower()
        if not who:
            return ("Nevím jistě, s kým mluvím — sumář rozhovorů proto "
                    "nesestavím.")

        if rng:
            lo, hi, label = rng
        else:
            row = conn.execute(
                "SELECT MAX(ts) FROM diary WHERE event_type='human_chat' "
                "AND lower(title)=?", (who,)).fetchone()
            if not row or not row[0]:
                return ("V deníku nemám zapsaný žádný náš rozhovor. "
                        "Nebudu si nic vymýšlet.")
            lo, hi = _hr._day_bounds(datetime.fromtimestamp(row[0]))
            label = _hr._cz_when(lo, with_weekday=True).split(" v ")[0]

        rows = conn.execute(
            "SELECT ts, note FROM diary WHERE event_type='human_chat' "
            "AND lower(title)=? AND ts>=? AND ts<? ORDER BY ts ASC",
            (who, lo, hi)).fetchall()

        if not rows:
            # HANS_CHAT_SUMMARY_STRANGER_V1 (24. 9.) — cizimu zapisy dne ne:
            # je v nich denni shrnuti s pocty z kamery (person_seen) a cizi
            # cinnosti. Doloženo testem 24. 9.
            try:
                from scripts.cz_names import is_known_person as _ikp_cs
                _znamy_cs = _ikp_cs(who, config)
            except Exception:
                _znamy_cs = False
            extra = _hr._day_notes(conn, lo, hi) if _znamy_cs else []
            out = ("%s jsme spolu podle deníku vůbec nemluvili — žádný náš "
                   "rozhovor z té doby zapsaný nemám a nebudu si ho vymýšlet."
                   % label.capitalize())
            if extra:
                out += "\nZapsal jsem si tehdy jen tohle:\n" + "\n".join(extra)
            return out

        parsed = []
        for ts, note in rows:
            u, r = _hr._split_exchange(note, who)
            if u:
                parsed.append((ts, u.replace("\n", " ").strip(),
                               (r or "").replace("\n", " ").strip()))
        if not parsed:
            return ("%s mám sice rozhovor zapsaný, ale bez čitelného obsahu."
                    % label.capitalize())

        # Výpis je DOSLOVNÝ (obě strany z deníku) — nic se negeneruje znovu.
        multi_day = len({_hr._cz_date(ts) for ts, _u, _r in parsed}) > 1
        lines = []
        for ts, u, r in parsed:
            when = datetime.fromtimestamp(ts).strftime("%H:%M")
            prefix = ("%s %s" % (_hr._cz_date(ts), when)) if multi_day else when
            blk = "– %s\n   Vy: „%s“" % (prefix, u[:160])
            if r:
                blk += "\n   Já: „%s“" % r[:220]
            lines.append(blk)

        total = len(lines)
        head = ("%s jsme spolu vedli %d %s."
                % (label.capitalize(), total,
                   "výměnu" if total == 1 else
                   ("výměny" if total < 5 else "výměn")))

        # Delší období → nejdřív TÉMATA (kondenzace skutečných replik),
        # podrobnosti až na vyžádání („připomeň rozhovor o X“).
        if total > detail_max:
            topics = _hr._summarize_topics(config, [u for _ts, u, _r in parsed])
            if topics:
                return ("%s %s\nChcete-li si některý připomenout doslova, "
                        "řekněte třeba „připomeň rozhovor o …“ — vypíšu ho, "
                        "jak je zapsán."
                        % (head, topics))

        shown = lines[:max_lines]
        out = (head + " Tady je doslovný zápis z deníku:\n" + "\n".join(shown))
        if total > max_lines:
            out += ("\n(… a dalších %d výměn. Konkrétní si vyžádejte: "
                    "„připomeň rozhovor o …“.)" % (total - max_lines))
        return out
    except Exception as e:
        _hr._log.warning("chat_summary selhal: %s", e)
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def is_recall_query(text: str) -> bool:
    """Ptá se uživatel na dřívější rozhovor? (pamatuješ / mluvili jsme / říkal jsi)"""
    # Tolerantní k i/y a překlepům („bavily", „pripomenou") — striktní vzor by
    # dotaz pustil do volné generace a Hans by si rozhovor VYMYSLEL.
    t = (text or "").lower()
    return bool(re.search(
        r"pamatuj|vzpom[íi]n|p[řr]ipome[ňnt]|mluvil[iy]\s+jsme|"
        r"bavil[iy]\s+jsme|[řr][íi]kal\s+jsi|co\s+jsme|jsme\s+se\s+bavil[iy]|"
        r"zm[íi]nil\s+jsi|navrh\w*\s+jsi|[řr]ekl\s+jsi", t))


def is_memory_meta_query(text: str) -> bool:
    """HANS_SOURCE_META_MEMORY_V1 — ptá se věta na SPOLEHLIVOST Hansovy paměti?

    Sdílený predikát: používá ho `is_source_query` (aby nespustil šablonu
    o zdrojích) i agentní guard v `hans_agent` (aby metaotázku neunesl na
    hlášení, co dělá Koláč). Jedna pravda místo dvou vzorů, které se rozejdou.
    """
    return bool(_hr._META_PAMET.search(text or ""))

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
