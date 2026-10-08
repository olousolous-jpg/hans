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

def is_recent_activity_query(text: str) -> bool:
    """Ptá se uživatel „co jsi se dnes dozvěděl / co sis zapsal / jaké
    zajímavosti dnes / co jsi dnes dělal"? Deterministický gate."""
    f = _hr._fold(text or "")
    if _hr._RECENT_NOT_RE.search(f):   # HANS_RECENT_ACTIVITY_FORMS_V1
        return False
    return bool(_hr._RECENT_QUERY_RE.search(f))


def _okno_aktivity(text: str, days: int):
    """HANS_RECENT_ACTIVITY_YESTERDAY_V1 — z dotazu urci ČASOVÉ OKNO.

    Vrací (od, do, popis). `do=None` = bez horní meze (dosavadní chování).
    ⚠️ Horní mez je to podstatné: bez ní vrátí dotaz na VČEREJŠEK i dnešní
    záznamy — tedy přesně tu záměnu, kvůli které to vzniklo."""
    import datetime as _dt
    f = _hr._fold(text or "")
    if re.search(r"\bv[čc]era\b", f):
        dnes = _dt.datetime.now().replace(hour=0, minute=0, second=0,
                                          microsecond=0)
        vcera = dnes - _dt.timedelta(days=1)
        # ⚠️ „VČERA V NOCI" NENÍ kalendářní včerejšek. Noc z 10. na 11. patří
        # kalendářně z větší části na 11., ale mluvčí jí myslí „ta, co právě
        # skončila". Doloženo daty: studium v tu noc má značku 11. 9. 03:04.
        # Kalendářní okno by ji vyloučilo a Hans by o té noci neřekl NIC.
        if re.search(r"\bnoc", f):
            return ((vcera + _dt.timedelta(hours=18)).timestamp(),
                    (dnes + _dt.timedelta(hours=9)).timestamp(),
                    "Záznamy z noci na %s" % dnes.strftime("%d.%m.%Y"))
        return (vcera.timestamp(), dnes.timestamp(),
                "Záznamy z %s" % vcera.strftime("%d.%m.%Y"))
    return (time.time() - days * 86400.0, None, "")


def recent_activity_answer(db_path: str, days: int = 1,
                           max_items_per_type: int = 3,
                           text: str = "") -> Optional[str]:
    """HANS_RECENT_ACTIVITY_V1 — deterministický recall Hansovy vlastní
    aktivity za posledních N dní (default 1 = dnešek). Vrátí grounded blok
    z deníku (study_note, book_reflection, reading_takeaway, web_read,
    movie_opinion, introspection, spontaneous, art_generated) — Hans z toho
    LLM vytvoří lidskou odpověď, ale je grounded ve faktech.

    Účel: opravit false-negative anti-konfab („nemám záznam") na dotaz na
    dnešní aktivitu, když Hans REÁLNĚ dnes něco dělal a to je v deníku.
    """
    since, _do, _popis = _hr._okno_aktivity(text, days)
    # kategorie k výpisu (label → event_type, kolik z každého)
    cats = [
        ("Studoval jsem", "study_note", max_items_per_type),
        ("Četl jsem", "web_read", max_items_per_type),
        ("Zaujalo mě ze čtení", "reading_takeaway", max_items_per_type),
        ("Zapsal jsem k filmu/pořadu", "movie_opinion", max_items_per_type),
        ("Zapsal jsem ke knize", "book_reflection", max_items_per_type),
        ("Mě napadlo (spontaneous)", "spontaneous", max_items_per_type),
        ("Uvažoval jsem (introspection)", "introspection", max_items_per_type),
    ]
    lines = []
    total = 0
    conn = None
    try:
        conn = _hr._ro(db_path)
        conn.row_factory = sqlite3.Row
        for label, etype, lim in cats:
            # HANS_SPONTANEOUS_TEMPLATE_MARK_V1/V2 (27.8.; V2 kotví na začátek
            # pole — `%"template"%` kdekoli by tiše zahodilo článek,
            # který o šablonách jen píše) — „Mě napadlo"
            # nesmí být šablona. Filtr je psaný obecně (platí na kterýkoli typ
            # označený jako šablona), ne jen na `spontaneous`.
            rows = conn.execute(
                "SELECT ts, title, note, data FROM diary "
                "WHERE event_type=? AND ts >= ? "
                # HANS_RECENT_ACTIVITY_YESTERDAY_V1 — HORNI MEZ
                "AND (? < 0 OR ts < ?) "
                "AND coalesce(note, data, '') != '' "
                "AND coalesce(data,'') NOT LIKE '{\"template\":%' "
                "ORDER BY ts DESC LIMIT ?",
                (etype, since, (-1 if _do is None else _do),
                 (0 if _do is None else _do), lim)).fetchall()
            if not rows:
                continue
            lines.append(f"{label}:")
            for r in rows:
                _content = (r["note"] or r["data"] or "").strip()
                _title = (r["title"] or "").strip()
                _snip = (_content[:180] + ("…" if len(_content) > 180 else ""))
                if _title:
                    lines.append(f"  • [{_title}] {_snip}")
                else:
                    lines.append(f"  • {_snip}")
                total += 1
    except Exception as e:
        _hr._log.warning("recent_activity_answer: %s", e)
        return None
    finally:
        if conn:
            conn.close()
    if total == 0:
        return None  # Hans dnes reálně nic nedělal → pusť anti-konfab
    return ("SKUTEČNÉ zápisky z tvého deníku (%s)" % (_popis or "za dnešek") + " (odpověz JEN z nich; "
            "shrň lidsky, nevymýšlej nic, co v nich není):\n\n"
            + "\n".join(lines))


def _reorder_object_first(text: str) -> str:
    """Přeskládá „co ses o tom divadle dozvěděl" → „co ses dozvěděl o tom
    divadle". Když vzor nesedí, vrací text beze změny."""
    t = text or ""
    try:
        return _hr._OBJ_FIRST_RE.sub(
            lambda m: "co %s %s %s %s" % (m.group(1), m.group(4),
                                          m.group(2), m.group(3)), t)
    except Exception:
        return t


def _kc_match(text: str):
    """Shoda `_KNOWLEDGE_CHECK_RE`, která není vztažnou větou. None = není dotaz."""
    t = text or ""
    m = _hr._KNOWLEDGE_CHECK_RE.search(t)
    if m and _hr._KC_VZTAZNA.search(t[:m.start()]):
        return None
    return m


def is_knowledge_check_query(text: str) -> bool:
    """Ptá se uživatel „znáš X?" / „co víš o X?" / „máš záznam o X?"? Levný gate.
    Regex je unicode-safe → volám na ORIGINÁLU (bez _fold), ať `_extract_topic`
    dostane originální text s diakritikou."""
    return bool(_hr._kc_match(_hr._reorder_object_first(text)))


def _extract_knowledge_topic(text: str) -> Optional[str]:
    """Vytáhne X z „znáš X?" — capture group regexu. Očištěno o pomocná slova."""
    m = _hr._kc_match(text or "")
    if not m:
        return None
    x = m.group(1).strip(" .,?!;:'\"")
    # Odstranit prefix „ten/tu/to/ta/serial/film/kniha" (pomocná slova bez informace)
    # HANS_KNOWLEDGE_CHECK_V2 — skloněné tvary media-typu („o filmU/seriálU/
    # knizE X"), jinak „filmu Proud krve" nesedne na paměť → falešná nabídka
    # studia u filmu, který Hans zná.
    # HANS_KNOWLEDGE_TOPIC_ANAFORA_V1 (21. 9.) — UKAZOVACÍ ZÁJMENO NENÍ TÉMA.
    # Doloženo rozhovorem 21. 9. (A/15): „a odkud znas TEN PROJEKT?" (myšlen
    # Hansův vlastní projekt) → téma 'ten projekt' → dohledání na Wikipedii →
    # Hans odpověděl obecnou definicí z hesla „Řízení projektů" a připsal jí
    # zdroj. To je tvrzení o světě s doloženým zdrojem, tedy nejdražší druh
    # chyby — a přitom otázka mířila na něco, o čem se právě mluvilo.
    #
    # Zájmeno je ANAFORA: ukazuje do hovoru, ne na pojem. Řeší se TÝMŽ
    # způsobem, jakým funkce už odstraňuje „film/seriál/kniha" — jen o řádek
    # dřív. Táž třída jako zájmena v `_STOPWORDS` (15. 7., „četl jsi JI?").
    #
    # ⚠️ ZMĚŘENO na 1 565 skutečných zprávách, a měření OBRÁTILO první návrh:
    # téma se zájmenem vznikne jen 3× a pokaždé je to LEGITIMNÍ „co vis o tom
    # vraku u sicilie?". Plošná abstinence by zabila jediné reálné výskyty.
    # Proto se zájmeno jen ODŘÍZNE (dotaz 'vraku u sicilie' je navíc lepší)
    # a abstinuje se, teprve když po něm nezbude nic než holý kvalifikátor
    # („projekt", „film", „kniha") — tedy když ve větě žádné téma nebylo.
    # ⚠️ `(?:\s+|$)` a ne `\s+`: zájmeno i kvalifikátor stojí často NA KONCI
    # („odkud znas TU KNIHU?", „a odkud znas TOHLE?"). S požadavkem na mezeru
    # za slovem vzor na konci věty nesepne a zbude téma 'knihu' / 'tohle' —
    # týž tvar chyby jako `kamer` × „kameře" (20. 9.).
    # HANS_KNOWLEDGE_TOPIC_FILLER_V1 (7. 10.) — VÝPLŇ PŘED TÉMATEM A HOLÉ ZÁJMENO.
    # /tazatel 7. 10.: „znas neco o starych hradech?“ → téma „neco o starych
    # hradech“ → „nemám žádné záznamy“, ačkoli hrady Hans studoval; „…znas je?“
    # → téma „je“. Výplň se odřízne, osobní zájmeno tématem není (None = běžná
    # cesta, kde předmět doplní vlákno). 📏 2 369 reálných vět: 0 změn.
    x = re.sub(r"^(?:n[ěe]co(?:\s+m[áa]lo)?|n[ěe]jak\w*|cokoli\w*|p[áa]r\s+v[ěe]c[íi]|"
               r"v[íi]ce?|trochu)\s+(?:o|z|ze)\s+", "", x, flags=re.I)
    if re.fullmatch(r"(?:je|ho|ji|jej|jich|n[ěe]m|n[íi]|nich|n[ěe]j|jim|mu)", x, flags=re.I):
        return None
    x = re.sub(r"^(?:ten|ta|to|tu|toho|tom|tomu|t[ée]|ty|ti|t[ěe]ch|t[íi]m|"
               r"tohle|tenhle|tahle|tamten|tamta|onen|ona)(?:\s+|$)",
               "", x, flags=re.I)
    x = re.sub(r"^(?:seri[áa]l\w*|film\w*|kn[ií]\w+|posta?v\w*|typ\w*)(?:\s+|$)",
               "", x, flags=re.I)
    x = x.strip()
    # Zbyl holý kvalifikátor bez jména („projekt", „knihu") → ve větě žádné
    # téma nebylo. Kontroluje se jak výčtem, tak týmž vzorem jako výš —
    # výčet nemá všechny pády.
    if x and " " not in x and (
            x.lower() in _hr._TOPIC_QUALIFIERS
            or re.fullmatch(r"(?:seri[áa]l\w*|film\w*|kn[ií]\w+|posta?v\w*"
                            r"|typ\w*)", x, flags=re.I)):
        return None
    # HANS_KNOWLEDGE_TOPIC_LEN_V1 (1. 10.) — téma delší než 6 slov není téma,
    # ale kus souvětí („třeba jiné díla s obdobnou atmosférou jako ta Falešná
    # kočička“) → bypass „nemám záznamy o ‚…‘“, ačkoli film Hans ten den četl
    # (/tazatel 1. 10.). Změřeno na 1 700 větách: skutečná témata 1–4 slova,
    # 9 a 10 slov jen 2× a obě nesmysl.
    if x and len(x.split()) > 6:
        return None
    return x or None


def _topic_core_prefixes(topic: str) -> list:
    """HANS_RECALL_STEM_V2 — jádrová slova tématu (bez kvalifikátorů) oříznutá
    na KMEN (declension-safe). 'jazyku dadština' → ['dadš']; 'hradech' → 'hrad'.
    Ořez -3 znaky (česká koncovka mění poslední 1–3 znaky kmene), podlaha 4
    (kmen 'hrad' má 4 znaky) — kratší by v textu náhodně splýval, proto se
    4znakové prefixy hledají jen v titulu (viz `_topic_in_memory`)."""
    import re as _re
    words = [w for w in _re.split(r"[^0-9a-zá-žA-ZÁ-Ž]+", (topic or "").lower())
             if len(w) >= 4 and w not in _hr._TOPIC_QUALIFIERS]
    return [w[:max(4, len(w) - 3)] for w in words]


def _topic_in_memory(db_path: str, topic: str) -> bool:
    """True když topic MÁ nějaký záznam v deníku / entities. Declension-safe
    (HANS_RECALL_DECLENSION_V1): matchuje na PREFIX každého jádrového slova
    ('jazyku dadština'/'dadštině' → 'dadšt'). U víceslovných témat musí najít
    VŠECHNA jádrová slova (AND) → 'žirafí polévka' nedá false-positive jen
    protože 'polévka' někde je."""
    if not topic or len(topic) < 3:
        return False
    prefixes = _hr._topic_core_prefixes(topic)
    if not prefixes:
        return False
    conn = None
    # HANS_RECALL_NODIA_DB_V1 (13.8.) — SQL LIKE NESKLÁDÁ DIAKRITIKU, takže
    # dotaz bez háčků minul zápisek s háčky: „co vis o historii opevneni?" →
    # jádrový prefix 'opevne' × uložené 'opevnění' → Hans ZAPŘEL tři vlastní
    # zápisky („nic jsem si o tom nezapsal ani nečetl", doloženo 13.8. 16:31,
    # ačkoli má study_note „Historie opevnění"). Uživatel píše z telefonu bez
    # diakritiky, deník ji má — přesně na to `_fold` odjakživa je, jen se tady
    # nevolalo (klasické „komponenta existuje, ale nikdo ji nevolá").
    # POŘADÍ: nejdřív prostá shoda (rychlá), teprve při neúspěchu složená
    # (volá python funkci nad řádky) → dotazy S diakritikou nic nestojí navíc.
    _TYPES = ("'web_read','study_note','book_read','book_reflection',"
              "'movie_opinion','kodi_playing','reading_takeaway'")
    # HANS_RECALL_NODIA_SPLIT_V1 (13.8.) — `UNION` nutil SQLite vyhodnotit OBĚ
    # strany, i když malá tabulka `entities` odpověděla hned: změřeno 1557 ms
    # vs 124 ms pro tytéž dotazy spuštěné ZVLÁŠŤ s předčasným koncem
    # (entities 2 ms → diary 122 ms). Rozděleno = 12× rychleji v běžném případě,
    # kdy se téma najde. Případ „nenajde" zůstává ~1,6 s (plný sken je nutný).
    # `%(f)s` = obalová funkce nad sloupcem: prázdná pro prostou shodu,
    # `nodia` pro shodu bez diakritiky.
    # HANS_RECALL_STEM_V2 — postav JEDEN dotaz, který vyžaduje VŠECHNA jádrová
    # slova v TÉMŽE řádku (AND). Dřív se každé slovo hledalo zvlášť napříč
    # deníkem → „historie fotbaloveho mistrovstvi" našlo 3 slova ve 3 různých
    # záznamech = falešně „mám záznam" (měřeno: 4 z 8 negativů). Krátký prefix
    # (≤4 zn) jen v TITULU — v dlouhém textu poznámky by 4 znaky splynuly.
    def _clauses(fn):
        """(SQL fragment 'A AND B AND …', args) pro obal `fn` ('' / 'nodia')."""
        col_t = ("%s(lower(title))" % fn) if fn else "lower(title)"
        col_n = ("%s(lower(note))" % fn) if fn else "lower(note)"
        parts, args = [], []
        for p in prefixes:
            needle = "%" + (_hr._fold(p) if fn else p) + "%"
            if len(p) <= 4:                      # krátký → jen titul
                parts.append("(%s LIKE ?)" % col_t)
                args.append(needle)
            else:                                # delší → titul i poznámka
                parts.append("(%s LIKE ? OR %s LIKE ?)" % (col_t, col_n))
                args += [needle, needle]
        return " AND ".join(parts), args

    def _ent_clause(fn):
        """entities má jen `name` → všechny prefixy v jednom jménu (AND)."""
        col = ("%s(lower(name))" % fn) if fn else "lower(name)"
        parts, args = [], []
        for p in prefixes:
            parts.append("(%s LIKE ?)" % col)
            args.append("%" + (_hr._fold(p) if fn else p) + "%")
        return " AND ".join(parts), args

    try:
        conn = _hr._ro(db_path)
        try:
            conn.create_function("nodia", 1, _hr._fold)
            _has_nodia = True
        except Exception:
            _has_nodia = False      # starší sqlite → zůstane jen prostá shoda

        def _found(fn: str) -> bool:
            ec, ea = _ent_clause(fn)                 # levné entities napřed
            if conn.execute("SELECT 1 FROM entities WHERE %s LIMIT 1" % ec,
                            ea).fetchone():
                return True
            dc, da = _clauses(fn)
            return bool(conn.execute(
                "SELECT 1 FROM diary WHERE %s AND event_type IN (%s) LIMIT 1"
                % (dc, _TYPES), da).fetchone())

        if _found(""):                              # rychlá prostá shoda
            return True
        if _has_nodia and _found("nodia"):          # až pak dražší bez háčků
            return True
        return False
    except Exception:
        return False
    finally:
        if conn:
            conn.close()


def reading_recall_answer(db_path: str, question: str = "") -> Optional[str]:
    """HANS_READING_RECALL_V1 — dotaz „co víš o X?" → dohledej Hansovo VLASTNÍ
    čtení o X (web_read/reading_takeaway/study_note, declension-safe) a vrať
    GROUNDED blok s tím, co si přečetl. None = nic → normální tok. Deterministické,
    žádný LLM. Řeší „přečteno ale nezapamatováno": ruční odkaz z chatu jde do
    web_read, ale RAG ho na tenkém souhrnu semanticky nedohledá; tady se najde
    přímo z deníku (declension-safe AND na jádrových slovech)."""
    # HANS_KNOWLEDGE_WORDORDER_V1 — i tady, jinak brána
    # pustí dotaz dál, ale hledání téma nenajde.
    question = _hr._reorder_object_first(question)
    if not question or not _hr.is_knowledge_check_query(question):
        return None
    prefixes = _hr._topic_core_prefixes(_hr._extract_knowledge_topic(question) or "")
    if not prefixes:
        return None
    conn = None
    try:
        # HANS_READING_RECALL_WORD_START_V1 (8. 10.) — dřív `LIKE %prefix%` kdekoli
        # v textu a tři NEJNOVĚJŠÍ shody: „hradu Kost“ → „na-hrad-it“ + „kost-i“
        # v článku o veganské stravě, a model si k tomu vymyslel „Kostiště“
        # (čtení „Hrad Kost“ v deníku je). Teď musí každé slovo tématu ZAČÍNAT
        # slovo titulu nebo textu, shoda v titulu jde první a jednoslovné téma
        # se bere jen z titulu (čtyřpísmenný kmen v těle textu je šum).
        # 📏 52 reálných dotazů na znalost: 9 beze změny, 24 jiný výběr, 5 šum →
        # nic („Merkuru“ → Sfinx, Strážci Galaxie), 7 nově nalezeno („vrak
        # u Sicílie“, „vyšetřování ztráty třídní knihy“).
        vzory = [re.compile(r"(?<![a-z0-9])" + re.escape(_hr._fold(p).lower()))
                 for p in prefixes]
        conn = _hr._ro(db_path)
        nalez = []
        for ts, title, body in conn.execute(
                "SELECT ts, coalesce(title,''), coalesce(NULLIF(note,''), data, '') "
                "FROM diary WHERE event_type IN ('web_read','reading_takeaway','study_note')"):
            ft = _hr._fold(title).lower()
            v_titulu = all(v.search(ft) for v in vzory)
            if not v_titulu:
                if len(vzory) < 2:
                    # téma uložené při ručním čtení jako úvodní [značka] poznámky
                    # (HANS_READ_TOPIC_V1) platí jako titul — kvůli tomu čtení vzniklo
                    _zn = re.match(r"\s*\[([^\]]{1,40})\]", body or "")
                    if not (_zn and vzory[0].search(_hr._fold(_zn.group(1)).lower())):
                        continue
                    nalez.append((0, -float(ts or 0), body))
                    continue
                fb = _hr._fold(body).lower()
                if not all(v.search(ft) or v.search(fb) for v in vzory):
                    continue
            nalez.append((0 if v_titulu else 1, -float(ts or 0), body))
        nalez.sort(key=lambda r: r[:2])
        rows, _videno = [], set()
        for _a, _b, body in nalez:
            _k = (body or "")[:80]
            if _k in _videno:
                continue
            _videno.add(_k)
            rows.append((body,))
            if len(rows) >= 3:
                break
        conn.close()
        conn = None
        bits = []
        for (body,) in rows:
            b = re.sub(r"^\[[^\]]{1,30}\]\s*", "", (body or "").strip())  # ořízni [topic]
            if b and len(b) > 15:
                bits.append(b[:400])
        if not bits:
            return None
        return ("\n\nZ TVÉ ČTENÁŘSKÉ PAMĚTI (co sis o tom sám přečetl a zapsal "
                "— odpověz z tohohle, ne z domýšlení):\n"
                + "\n".join("• " + x for x in bits[:3]) + "\n")
    except Exception:
        if conn:
            conn.close()
        return None


def knowledge_check_answer(db_path: str, user_text: str) -> Optional[str]:
    """HANS_KNOWLEDGE_CHECK_V1 — grounding blok „PAMĚŤ NEOBSAHUJE X" pro
    dotaz „znáš X?". None = X JE v paměti (nech normální recall/RAG cestu)
    nebo detektor selhal (dotaz není typu 'znáš X?').

    Anti-konfab silnější než system prompt klauzule (G4B position: grounding
    sedí těsně před user query, přebíjí conversation history i persona)."""
    # HANS_KNOWLEDGE_WORDORDER_V1 — i tady, jinak brána
    # pustí dotaz dál, ale hledání téma nenajde.
    user_text = _hr._reorder_object_first(user_text)
    topic = _hr._extract_knowledge_topic(user_text)
    if not topic:
        return None
    if _hr._topic_in_memory(db_path, topic) or _hr._hlavni_slovo_v_pameti(db_path, topic):
        # X JE v paměti — nech film_knowledge_answer / recall / RAG odpovědět
        return None
    return (
        "\n\nDŮLEŽITÉ FAKTUM O TVÉ PAMĚTI: v tvých vlastních záznamech "
        "(deník, entity, čtená paměť) NENÍ žádný záznam o \"%s\". Nic "
        "konkrétního jsi si o tom nezapsal ani nepamatuješ z vlastní "
        "zkušenosti.\n\n"
        "PRAVIDLA PRO ODPOVĚĎ:\n"
        "1. NIKDY neříkej „mám v paměti záznamy o %s\" ani „nedávno "
        "jsem si to pročetl\" — byla by to lež (PAMĚŤ NEOBSAHUJE).\n"
        "2. Pokud tě to napadá z obecné znalosti (z tréninku): odpověz "
        "poctivě „V paměti to nemám zapsané, ale obecně vím, že %s "
        "je...\" — jasně rozliš OBECNOU ZNALOST od PAMĚTI.\n"
        "3. Když nevíš ani obecně: „O tomto pojmu nic konkrétního nevím, "
        "pane.\"\n\n"
        "Klíč: rozlišuj OBECNÁ ZNALOST (z tréninku) vs. PAMĚŤ (co jsi "
        "sám prožil/četl/zapsal). Nesměšuj je." % (topic, topic, topic))


def _hlavni_slovo_v_pameti(db_path: str, topic: str) -> bool:
    """HANS_KNOWLEDGE_HEAD_NOUN_V1 (7. 10.) — víceslovné téma, které jako celek
    v paměti není, ale jeho POSLEDNÍ slovo ano („starých hradech“ → hrady).

    `_topic_in_memory` žádá všechna slova v témže záznamu (záměr z 13. 8., brání
    falešnému „mám záznam“). Pro ZAPŘENÍ je to ale moc přísné: přívlastek
    („staré“, „gotické“) v zápisku být nemusí a Hans pak tvrdí „nic jsem si
    o tom nezapsal ani nečetl“ o tématu, které studoval — a dohledá místo toho
    náhodné heslo. Tady se nic netvrdí, jen se NEzapře: věta jde běžnou cestou.
    📏 2 613 vět: 22 zapření, 6 z nich takhle přejde na běžnou cestu."""
    w = [x for x in re.findall(r"\w+", topic or "") if len(x) >= 5]
    if len((topic or "").split()) < 2 or not w:
        return False
    try:
        return bool(_hr._topic_in_memory(db_path, w[-1]))
    except Exception:
        return False


def knowledge_check_bypass(db_path: str, user_text: str,
                           asker: Optional[str] = None) -> Optional[str]:
    """HANS_KNOWLEDGE_CHECK_V1 BYPASS (18.7.) — deterministická odpověď na
    „znáš X?" když X NENÍ v Hansově paměti. Analogicky `sources_answer`
    (bypass mimo LLM), protože grounding block nezabral — hans-czech persona
    finetune si vždy vyfabuluje „mám v paměti záznamy".

    Vrátí string nebo None. None = X JE v paměti nebo dotaz není typu 'znáš X?'
    → nech normální recall/RAG cestu.

    Text šetří obecnou znalost (bypass nemá LLM) — přiznává „nemám v paměti"
    a nabízí uživateli, že se to může Hans naučit (studium, čtení, atd.).
    """
    topic = _hr._extract_knowledge_topic(user_text)
    if not topic:
        return None
    if _hr._topic_in_memory(db_path, topic) or _hr._hlavni_slovo_v_pameti(db_path, topic):
        return None  # nech normální cestu, X JE v paměti
    oslov = _hr._cz_address(asker) if asker else "pane"  # HANS_NAME_INFLECTION_V1
    # Kompaktní honestní odpověď + nabídka pokud chce ať Hans si to zapíše
    return ("V paměti nemám žádné vlastní záznamy o '%s', %s. "
            "Nic jsem si o tom nezapsal ani nečetl (obecně to znám možná "
            "z tréninku, ale nechci to vydávat za vlastní paměť). "
            "Kdybyste chtěl, mohu si to zařadit do studia — stačí říct "
            "'nastuduj %s'." % (topic, oslov, topic))

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
