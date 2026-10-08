"""Funkce přesunuté z `scripts/hans_recall.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_hr.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations


def _family_sentence(pid: str, links: dict, config: dict) -> str:
    """Rodinné vazby jako ŠTÍTKY s dvojtečkou („Rodiče: Standa a Jana“).

    ⚠️ Záměrně NE větná vazba: čeština by chtěla 2. pád („dcera Standy a Jany“)
    a `cz_names` umí jen vokativ a akuzativ. Vymýšlet další skloňování kvůli
    jedné větě se nevyplatí — štítek je gramaticky bezpečný v každém pádu.
    """
    if not links:
        return ""
    try:
        from scripts.cz_names import display_name as _dn
    except Exception:
        return ""
    def _join(ids):
        return " a ".join(_dn(i, config) or i for i in ids if i)
    out = []
    if links.get("parents"):
        out.append("rodiče: %s" % _join(links["parents"]))
    if links.get("spouse"):
        out.append("partner: %s" % (_dn(links["spouse"], config) or links["spouse"]))
    if links.get("children"):
        out.append("děti: %s" % _join(links["children"]))
    return "; ".join(out)


def person_card(db_path: str, query: str, config: dict,
                asker: str = "") -> str:
    """Deterministická odpověď na „kdo je X / co víš o X“. "" = nevím (pak ať
    odpoví běžná cesta; NIC se nedomýšlí).

    Pořadí: (1) `relationships` — domácnost zná Hans nejlíp a má o ní vlastní
    pozorování; (2) `entities` — lidé z jeho čtení (Bud Spencer). Přísné
    `resolve` (bez `loose`, etype='osoba'), aby „co víš o hradech“ netrefilo
    člověka.
    """
    q = (query or "").strip()
    if not q:
        return ""
    # (1) DOMÁCNOST
    try:
        from scripts.cz_names import (find_known_person, display_name,
                                      is_known_person)
        pid = find_known_person(q, config)
        # HANS_HOUSEHOLD_PRIVACY_V1 — o ČLENU DOMÁCNOSTI jen se známou osobou.
        # Encyklopedické osoby (větev 2) zůstávají volné — Bud Spencer je
        # veřejný fakt, ne soukromí domu.
        if pid and asker and not is_known_person(asker, config):
            _hr._log.info("person_card: %r není známá osoba → soukromí domácnosti",
                      asker)
            return _hr._PRIVACY_REFUSAL
        if pid:
            from scripts.hans_relationships import Relationships
            card = Relationships(config).get(pid)
            if card:
                nm = card.display_name or display_name(pid, config) or pid
                head = nm
                if card.role:
                    head += " — " + card.role
                head += "."
                fam = _hr._family_sentence(pid, card.family_links or {}, config)
                if fam:
                    head += " (%s)" % fam
                ch = (card.characterization or "").strip()
                # HANS_PRAVA_V1 (27. 9.) — Hansovy postřehy o JINÉ osobě jen
                # s oprávněním; jinak zůstane jméno, role a rodina.
                try:
                    from scripts.hans_prava import muze as _pm
                    if ch and not _pm(config, asker or "", "karta_osoby", o_kom=pid):
                        ch = ""
                except Exception:
                    pass
                if ch:
                    head += " " + (ch[:400] + ("…" if len(ch) > 400 else ""))
                return head
    except Exception as e:
        _hr._log.debug("person_card: domácnost (%s)", e)
    # (2) ENCYKLOPEDIE (Hansovo čtení)
    try:
        from scripts.hans_entities import EntityStore
        ent = EntityStore(config).resolve(q, etype="osoba")
        if ent:
            # ⚠️ NE `fact_block()` — ta věta („Ověřený fakt o „X“ (z mého
            # čtení, zdroj: …)“) je GROUNDING PRO MODEL, ne odpověď člověku.
            # Když ji vrátíme přímo (a to se stane vždy, když hlasový krok
            # neprojde kontrolou), uživatel čte vnitřek stroje. Doloženo
            # živě 18.8. Skládáme proto vlastní, čitelnou podobu.
            g = (ent.get("gloss") or "").strip()
            nm = ent.get("name") or "?"
            if not g:
                return "%s — mám o něm záznam, ale bez bližšího popisu." % nm
            src = (ent.get("source") or "").strip()
            out = g if g.lower().startswith(nm.lower()[:6]) else "%s: %s" % (nm, g)
            if src:
                out += " (z mého čtení, zdroj: %s)" % src
            return out
    except Exception as e:
        _hr._log.debug("person_card: entity (%s)", e)
    return ""


def person_card_voiced(db_path: str, query: str, config: dict,
                       asker: str = "") -> str:
    """HANS_PERSON_CARD_VOICE_V1 (18.8.) — táž fakta, ale Hansovým hlasem.

    Bez tohohle kroku dostal uživatel do chatu SYROVOU KARTU
    („Jana — paní domu. (partner: Standa; děti: Klára) …“), případně rovnou
    vnitřní grounding řetězec z `fact_block` („Ověřený fakt o „X“ (z mého
    čtení, zdroj: …)“). To je podklad PRO MODEL, ne odpověď člověku —
    doloženo živým dialogem 18.8.

    Kontrakt je stejný jako u `/dnes` (HANS_DAY_AT_HOME_V1): fakta vzniknou
    DETERMINISTICKY, hlas je smí jen přeformulovat. Když mozek není
    (herní mód / PC dole), vrátí se karta holá — radši strohé než žádné.
    """
    card = _hr.person_card(db_path, query, config, asker=asker)
    if not card or card == _hr._PRIVACY_REFUSAL:
        return card   # odmítnutí jde jak je, model ho nepřebásňuje
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return card
    except Exception:
        pass
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_core
        try:
            core = persona_core(config, with_address=False)
        except Exception:
            core = ""
        model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + (
            "Někdo se tě ptá na konkrétního člověka. Odpověz souvisle "
            "(2-4 věty, tvým hlasem). "
            # Doloženo živě 18.8.: na „kdo je Bud Spencer?" model odpověděl
            # „Jmenuji se Carlo Pedersoli… zemřel jsem“ — vzal „první osobu“
            # jako pokyn mluvit ZA TU OSOBU. Proto se to říká výslovně.
            "⚠️ O té osobě mluv ve TŘETÍ osobě („je“, „byl“) — první osoba "
            "patří jen tobě, Hansovi. NIKDY nemluv jako ona. "
            # HANS_DAY_AT_HOME_EXACT_V1 (7.8.) — týž hlasový krok jinde komolil
            # čísla; tady z „1929“ udělal „roku devětadvacátého“.
            "LETOPOČTY, DATA a ČÍSLA opiš PŘESNĚ číslicemi tak, jak jsou ve "
            "faktech (1929, 2016) — nepřepisuj je slovy. "
            "Vyjdi POUZE z faktů níže — "
            "co v nich není, nevíš, a nic si nepřimýšlej: žádné domněnky o "
            "povaze, zvycích ani vztazích navíc. Jména, role a rodinné vazby "
            "opiš PŘESNĚ tak, jak jsou uvedené. Když je mezi fakty zdroj "
            "(odkaz), zmiň, odkud to máš; když tam žádný zdroj NENÍ, o zdroji "
            # Doloženo živě 18.8.: u domácnosti model připsal
            # „(zdroj neuváděn)“ — pro uživatele je to šum o vnitřku.
            "nepiš NIC, ani že chybí. Žádný nadpis, žádné odrážky, "
            "žádné uvozovky kolem celé odpovědi.")
        out = ollama_generate(
            model, "FAKTA O OSOBĚ:\n" + card + "\n\nOdpověz na dotaz: " + (query or ""),
            system=system, config=config, timeout=60)
        txt = (out or "").strip().strip('"')
        # HANS_PERSON_CARD_VOICE_V1 — DETERMINISTICKÁ KONTROLA MÍSTO DŮVĚRY.
        # Instrukce „čísla opiš číslicemi" nestačila: doloženo 18.8., z „1929"
        # a „2016" udělal hans-czech „roku devětadvacátého" a „šestnáctého".
        # To není sloh, to je posunutý FAKT. Prompt už nezesiluji (vzor
        # [[prompt-debt-tool-calling]]) — radši ověřím výsledek: když se z faktů
        # ztratí letopočet, hlasovou verzi nepřijmu a vrátím kartu.
        import re as _re
        years = set(_re.findall(r"\b(1[89]\d\d|20\d\d)\b", card))
        if years and not years.issubset(set(
                _re.findall(r"\b(1[89]\d\d|20\d\d)\b", txt))):
            _hr._log.info("person_card_voiced: hlas ztratil letopočet %s → karta",
                      sorted(years - set(_re.findall(
                          r"\b(1[89]\d\d|20\d\d)\b", txt))))
            return card
        # Krátká odpověď = model se nechytil → radši fakta než pahýl.
        if len(txt) >= 40:
            return txt[:1200]
    except Exception as e:
        _hr._log.warning("person_card_voiced: hlas selhal (%s) — vracím kartu", e)
    return card


def household_card(db_path: str, config: dict, asker: str = "") -> str:
    """HANS_HOUSEHOLD_CARD_V1 (18.8.) — SLOŽENÍ DOMÁCNOSTI, ne kdo je vidět.

    Doloženo dialogem 18.8.: na „kdo v tomhle domě žije" Hans odpověděl
    „pan Standa, Stando a slečna Klára" — do výčtu se vplížilo OSLOVENÍ a jeden
    člen domácnosti CHYBĚL, protože odpověď skládal model z hlavy. Seznam
    přitom leží v `relationships`. Bez LLM; "" když store nic nedá.
    """
    # HANS_HOUSEHOLD_PRIVACY_V1 — složení domácnosti není veřejná informace.
    try:
        from scripts.cz_names import is_known_person
        if asker and not is_known_person(asker, config):
            _hr._log.info("household_card: %r není známá osoba → odmítám", asker)
            return _hr._PRIVACY_REFUSAL
    except Exception:
        pass
    try:
        from scripts.hans_relationships import Relationships
    except Exception as e:
        _hr._log.debug("household_card: import (%s)", e)
        return ""
    try:
        cards = [c for c in (Relationships(config).all_cards() or [])
                 if (c.display_name or "").strip()]
    except Exception as e:
        _hr._log.debug("household_card: store (%s)", e)
        return ""
    if not cards:
        return ""
    # Pořadí: pán/paní domu první, pak zbytek — ne náhodné z DB.
    def _rank(c):
        r = (c.role or "").lower()
        return (0 if "pán" in r else 1 if "paní" in r else 2, c.person_id)
    # ⚠️ ZÁMĚRNĚ `display_name`, NE `formal_name`: ten sáhne po plném jméně
    # z configu („Johana"), jenže doma se jí říká Jana — a kontrola
    # úplnosti v hlasovém kroku porovnává právě `display_name`, takže by si
    # věta s kontrolou protiřečila. Role („paní domu") titul stejně nese.
    parts = []
    for c in sorted(cards, key=_rank):
        nm = c.display_name
        parts.append("%s (%s)" % (nm, c.role) if c.role else nm)
    if len(parts) == 1:
        return "V domě žije %s." % parts[0]
    return "V domě žijí %s a %s." % (", ".join(parts[:-1]), parts[-1])


def household_card_voiced(db_path: str, config: dict, asker: str = "") -> str:
    """Totéž Hansovým hlasem, ale s KONTROLOU ÚPLNOSTI: když ve vyslovené
    verzi chybí něčí jméno, nepřijme se. Právě vynechaný člen domácnosti byl
    ta chyba, kvůli které tohle vzniklo — hezčí věta za cenu ztraceného
    člověka nestojí."""
    card = _hr.household_card(db_path, config, asker=asker)
    if not card or card == _hr._PRIVACY_REFUSAL:
        return card   # odmítnutí se NEvyslovuje modelem, jde jak je
    try:
        from scripts.hans_relationships import Relationships
        names = [(c.display_name or "").strip()
                 for c in (Relationships(config).all_cards() or [])
                 if (c.display_name or "").strip()]
    except Exception:
        names = []
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return card
    except Exception:
        pass
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_core
        try:
            core = persona_core(config, with_address=False)
        except Exception:
            core = ""
        model = (config.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + (
            "Pán domu se ptá, KDO V DOMĚ ŽIJE. Odpověz jednou až dvěma větami "
            "svým hlasem. Vyjmenuj VŠECHNY osoby z faktů níže i s jejich rolí — "
            "nikoho nevynechej, nikoho nepřidávej a role neměň. Nepleť do "
            "výčtu oslovení toho, s kým mluvíš. Žádné odrážky.")
        out = ollama_generate(model, "FAKTA:\n" + card + "\n\nOdpověz.",
                              system=system, config=config, timeout=60)
        txt = (out or "").strip().strip('"')
        if txt and all(n in txt for n in names) and len(txt) >= 20:
            return txt[:600]
        if txt:
            _hr._log.info("household_card_voiced: hlas vynechal jméno → karta")
    except Exception as e:
        _hr._log.warning("household_card_voiced: %s", e)
    return card


def asks_about_person(query: str, config: dict) -> bool:
    """True = věta jmenuje známou osobu A ptá se na ni. Obě podmínky musí
    platit současně — viz komentář u `_PERSON_ASK_PAT`."""
    q = (query or "").strip()
    if not q or not _hr._PERSON_ASK_PAT.search(q):
        return False
    try:
        from scripts.cz_names import find_known_person
        if find_known_person(q, config):
            return True
    except Exception:
        pass
    # osoba z Hansova čtení (Bud Spencer) — rozhodne až `person_card`
    return True


def datetime_answer(query: str) -> str:
    """Deterministická odpověď na dotaz po datu / čase. "" = není to on.

    Datum i čas se vrací ROVNOU SLOVY (`cz_numbers`), aby to sedělo i pro
    hlasový výstup — model si číslice rozepsat neumí.
    ⚠️ Omezeno na KRÁTKÉ dotazy: „kolik je hodin práce před námi?" má být
    normální hovor, ne výpis hodin.
    """
    q = (query or "").strip()
    if not q or len(q.split()) > 7:
        return ""
    want_date = bool(_hr._DATE_ASK.search(q))
    want_time = bool(_hr._TIME_ASK.search(q))
    if not (want_date or want_time):
        return ""
    import datetime as _dt
    now = _dt.datetime.now()
    try:
        from scripts.cz_numbers import normalize as _n
        d_words = _n(f"{now.day}.{now.month}.{now.year}").strip()
    except Exception:
        d_words = ""
    den = _hr._DNY_CZ[now.weekday()]
    # čas taky slovy — `cz_numbers` to umí („09:31" → „devět hodin třicet jedna
    # minut"); číslice by hlasová syntéza přečetla špatně, což je celý důvod,
    # proč ten modul vznikl.
    try:
        from scripts.cz_numbers import normalize as _n2
        cas = _n2(now.strftime("%H:%M")).strip() or now.strftime("%H:%M")
    except Exception:
        cas = now.strftime("%H:%M")
    if want_date and not want_time:
        return ("Dnes je %s %s." % (den, d_words) if d_words
                else "Dnes je %s %d.%d.%d." % (den, now.day, now.month, now.year))
    if want_time and not want_date:
        return "Je %s." % cas
    return ("Dnes je %s %s, %s." % (den, d_words or "", cas)).replace("  ", " ")


def asks_book_recommendation(query: str) -> bool:
    """Ptá se věta na DOPORUČENÍ KNIHY? (film sem NEpatří)"""
    q = (query or "").strip()
    if not q or len(q.split()) > 12:
        return False
    if __import__("re").search(r"\bfilm|seri[áa]l|po[řr]ad\b", q, __import__("re").IGNORECASE):
        return False
    return bool(_hr._BOOK_ASK.search(q))


def book_recommendation(db_path: str, config: dict = None) -> str:
    """Doporučení z knih, které Hans DOČETL, i s tím, co si o nich zapsal.
    "" když nic nedočetl — pak ať odpoví běžná cesta, nic se nevyrábí."""
    import sqlite3
    try:
        with sqlite3.connect("file:%s?mode=ro" % db_path, uri=True,
                             timeout=3.0) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT title, ts FROM diary WHERE event_type='book_finished' "
                "ORDER BY ts DESC LIMIT 8").fetchall()
            if not rows:
                return ""
            import random
            pick = random.choice([dict(r) for r in rows])
            titul = (pick.get("title") or "").replace("Docetl:", "").strip()
            if not titul:
                return ""
            # vlastní reflexe k té knize (proč ho zaujala) — ne obsah z internetu
            refl = db.execute(
                "SELECT COALESCE(data, note) AS t FROM diary "
                "WHERE event_type='book_completion_reflection' "
                "AND COALESCE(title,'') LIKE ? ORDER BY ts DESC LIMIT 1",
                ("%" + titul + "%",)).fetchone()
    except Exception as e:
        _hr._log.debug("book_recommendation: %s", e)
        return ""
    out = "Z toho, co jsem dočetl, bych doporučil „%s“." % titul
    t = (refl["t"] if refl else "") or ""
    if t:
        t = " ".join(t.split())
        out += " " + (t[:260] + ("…" if len(t) > 260 else ""))
    return out


def asker_state_answer(query: str, asker: str, present_names, config: dict) -> str:
    """Deterministická odpověď na „vidíte mě?" / „kdo jsem já?". "" = není to on."""
    q = (query or "").strip()
    if not q or len(q.split()) > 8:
        return ""
    try:
        from scripts.cz_names import is_known_person, display_name
    except Exception:
        return ""
    known = bool(asker) and is_known_person(asker, config)
    disp = (display_name(asker, config) if known else (asker or "")).strip()

    if _hr._SEES_ME.search(q):
        # „vidíš mě RÁD?" je otázka na vztah, ne na kameru (chyceno vlastním
        # protipříkladem při testu — vzor jinak odpověděl výpisem z kamery).
        if __import__("re").search(r"\br[áa]d[aoy]?\b", q, __import__("re").IGNORECASE):
            return ""
        # HANS_CAMERA_STRANGER_ASKER_V1 (27. 9.) — HANS_CAMERA_STRANGER_V1 platil
        # jen v agentovi; tahle deterministická cesta ho neznala. Doloženo testem
        # nováčka: cizí „vidíš mě přes kameru?“ → „kamera je prázdná“, a kdyby
        # v místnosti někdo byl, dostal by „v místnosti vidím <jména domácnosti>“.
        if not known:
            return _hr._CAMERA_REFUSAL
        names = [n for n in (present_names or [])
                 if n and n not in ("Unknown", "?", "")]
        me = [n for n in names
              if disp and n.strip().lower() == disp.strip().lower()
              or (asker and n.strip().lower() == asker.strip().lower())]
        if me:
            return "Ano, vidím vás, pane."
        if names:
            try:
                from scripts.cz_names import accusative as _acc
                vid = ", ".join(_acc(n, config) or n for n in names)
            except Exception:
                vid = ", ".join(names)
            return "Vás teď nevidím, pane — v místnosti vidím %s." % vid
        return "Teď tu nikoho nevidím, pane — kamera je prázdná."

    if _hr._WHO_AM_I.search(q):
        if not known:
            # Žádné domýšlení identity a ŽÁDNÉ údaje o domácnosti
            # (táž hranice jako HANS_HOUSEHOLD_PRIVACY_V1).
            return ("Neznám vás, pane — ve svých záznamech vás nemám. "
                    "Rád se to dozvím, představíte-li se.")
        try:
            from scripts.hans_relationships import Relationships
            card = Relationships(config).get(str(asker).strip().lower())
        except Exception:
            card = None
        if card and card.role:
            return "Jste %s, %s." % (card.display_name or disp, card.role)
        return "Jste %s — znám vás ze svých záznamů." % (disp or asker)
    return ""

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
