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

def is_source_query(text: str) -> bool:
    """Ptá se uživatel „odkud to víš / kde jsi to četl / máš k tomu zdroj / odkaz"?
    Tolerantní k i/y a překlepům + bez diakritiky. NEsmí trefit obecnou zvědavost
    („co je zdroj X"). Rozšířeno o reálné formulace uživatele („mas odkaz na
    clanek, kde se o tom pisr?" — chat 17.7. 11:59)."""
    t = (text or "").lower()
    # HANS_SOURCE_META_MEMORY_V1 — metaotázka o paměti jde běžnou cestou
    if _hr._META_PAMET.search(text or ""):
        return False
    # Klasické + reálné formulace dotazu na provenienci Hansova tvrzení.
    pats = (
        # „odkud to (víš/máš)"
        r"\bodkud\s+(to|tohle|to\s+m[áa][šs]|to\s+v[íi][šs])",
        # „kde jsi to (četl/našel/vzal/slyšel)" i bez „jsi"
        r"\bkde\s+(jsi|si)?\s*(to|tohle|toto)?\s*(?:vzal|na[šs]el|na[čc]etl|[čc]etl|sly[šs]el|na[šs]el)",
        # „kde to najdu / kde se to dočtu / kde se o tom píše"
        r"\bkde\s+(to|se\s+(to|o\s+tom))\s+(?:najdu|na[čc]tu|do[čc]t[eě][šs]?|d[ao]ct[eěií]?|p[íi][šs]e)",
        # „na základě čeho / z čeho to víš/máš / podle čeho"
        # HANS_SOURCE_QUERY_V3 (30.8.) — ZÚŽENO. „na základě čeho" a „podle
        # čeho" stály samy o sobě, takže chytaly i otázku na ROZHODOVACÍ
        # KRITÉRIUM, ne na zdroj tvrzení. Doloženo simulovaným rozhovorem
        # 30.8.: „…podle čeho poznáte, že je zrovna tohle téma důležitější…"
        # → deterministický bypass odpověděl odkazem na Wikipedii k heslu
        # „Zločin", tedy úplně mimo, za 0 s a bez LLM.
        # ⚠️ Změřeno na 1337 reálných replikách: oba vzory nechytily NIKDY NIC
        # (0 výskytů), takže zúžení nemůže vzít žádný doložený zásah — a všech
        # 29 skutečných dotazů na zdroj prochází dál jinými vzory.
        # Rozhoduje SLOVESO za tázacím obratem: „víš/tvrdíš/soudíš" = zdroj,
        # „poznáš/vybíráš/rozhoduješ" = kritérium, na to se nesahá.
        # HANS_SOURCE_QUERY_V3B (30.8.) — VYKÁNÍ. Zúžení V3 mělo slovesa jen
        # v tykání, takže „na základě čeho to TVRDÍTE?" propadlo a Hans na
        # dotaz po zdroji odpověděl volným povídáním (doloženo dlouhým
        # ověřovacím rozhovorem, tah 15). Regresi jsem vyrobil sám tím
        # zúžením — cizí člověk Hansovi vyká, takže to není okrajový tvar.
        r"\bna\s+z[áa]klad[ěe]\s+[čc]eho\s+(to\s+)?"
        r"(v[íi][šs]|v[íi]te|tvrd[íi]([šs]|te)|soud[íi]([šs]|te)|"
        r"usuzuje([šs]|te)|mysl[íi]([šs]|te)|[řr][íi]k[áa]([šs]|te)|"
        r"p[íi][šs]e([šs]|te)|jsi|jste)",
        r"\bz\s+[čc]eho\s+(to|tohle)?\s*(v[íi][šs]|m[áa][šs])",
        r"\bpodle\s+[čc]eho\s+(to\s+)?"
        r"(v[íi][šs]|v[íi]te|tvrd[íi]([šs]|te)|soud[íi]([šs]|te)|"
        r"usuzuje([šs]|te)|mysl[íi]([šs]|te)|[řr][íi]k[áa]([šs]|te)|tak|to)",
        # „máš (k tomu) zdroj / odkaz / článek / důkaz"
        r"\bm[áa][šs]\s+(k\s+tomu\s+)?(zdroj|odkaz|[čc]l[áa]nek|d[ůu]kaz|citaci|pramen)",
        r"\bjak[ýy]\s+(m[áa][šs])?\s*(zdroj|odkaz|pramen)",
        # „dej mi / ukaž mi / pošli mi (odkaz / zdroj / článek)"
        r"\b(dej|ukaz|uk[áa][žz]|po[šs]li|hoď|hoď mi)\s+(mi\s+)?(odkaz|zdroj|[čc]l[áa]nek|pramen)",
        # „(můžeš / můžeš mi) ukázat/dát/poslat (zdroj/odkaz/článek)"
        r"\b(m[ůu][žz]e[šs]|dok[áa][žz]e[šs])\s+(m[eě]?\s+|mi\s+)?(uk[áa]zat|d[áa]t|posl[aá]t|pou[žz][ií]t)\s+.*?(zdroj|odkaz|[čc]l[áa]nek|pramen)",
        # „uveď/uved zdroj"
        r"\buve[ďdt]\s+(zdroj|odkaz|pramen)",
        # „proč si to myslíš"
        r"\bpro[čc]\s+si\s+(to|tohle)?\s*mysl[íi][šs]",
        # samotný „důkaz?"
        r"\bd[ůu]kaz\b",
        # samostatné „zdroj?" / „odkaz." / „a článek?" — krátký standalone dotaz
        r"^\s*(a\s+)?(zdroj|odkaz|[čc]l[áa]nek|pramen)(\s+pros[íi]m)?\s*[\.!\?]*\s*$",
        # HANS_SOURCE_QUERY_V2 (5.8.) — doplněno po měření na 305 reálných
        # zprávách: vzory chytily 7 dotazů, ale ze 7 ZKUŠEBNÍCH formulací
        # propadly 4. Doložený reálný miss: „muze me ukazat zdroj odkud jsi
        # cerpal?" (chat 17.7.) — obsahuje „zdroj" i „odkud", ale ani jeden
        # vzor nesedl, protože „odkud" nebylo následováno „to".
        # ⚠️ Zkoušeno nahradit mini modelem na Pi (bod 2 z nápadu) — ZAMÍTNUTO:
        # 9/15, propadly přesně tyhle formulace a u „odkud jsi čerpal?" model
        # místo klasifikace ZAČAL ODPOVÍDAT („z encyklopedie"). Regex je tu
        # měřeně lepší; detail v backlogu.
        # HANS_SOURCE_IS_SENSOR_V2 (4.9.) — PŘÍTOMNÝ ČAS. V2 doplnil jen
        # minulé „čerpal" (to byl doložený miss z 17.7.), takže „odkud
        # ČERPÁŠ informace o počasí?" propadlo dál a router z něj udělal
        # hlášení o počasí. Změřeno na 1226 reálných replikách: tyhle dva
        # vzory přidají PŘESNĚ 1 zásah — právě tu větu, žádný falešný.
        r"\b[čc]erp(al|[áa][šs]|[áa]te)",     # „odkud jsi čerpal / čerpáš / čerpáte"
        r"\bodkud\s+(bere[šs]|berete)",       # „odkud berete informace o…"
        r"\bodkud\s+(jsi|si|m[áa][šs]|jste)",  # „odkud jsi to vzal/čerpal"
        r"\bz\s+[čc]eho\s+(jsi|si)\s+(to\s+)?(vzal|m[áa][šs]|[čc]erpal)",
        r"\bkde\s+(ses|jsi\s+se)\s+(to\s+)?dozv[ěe]d[ěe]l",
        r"\bm[ůu][žz]e[šs]\s+(to\s+)?dolo[žz]it",
        r"\bjak\s+v[íi][šs]\s*,?\s*[žz]e",
        r"\bm[áa][šs]\s+(na\s+to\s+)?(n[ěe]jak[ýy]\s+)?(zdroj|odkaz|pramen)",
        # HANS_SOURCE_BARE_ODKUD_V1 (14. 9.) — holé „odkud VÍŠ/VÍTE/MÁTE" bez „to".
        # 4. 9. odloženo s poznámkou „měřit zvlášť". Změřeno na 1 179 reálných
        # větách: přidá PŘESNĚ 3 zásahy, všechny doložené chyby z 13. 9. —
        # 2× „odkud vis o F. L. Vekovi?" (→ falešné „nemám záznam") a „odkud vis,
        # ze tu nekdo je?" (→ „to nesdělím"). „odkud máš/jsi" už kryje vzor výš.
        r"\bodkud\s+(v[íi][šs]|v[íi]te|m[áa]te)\b",
    )
    return any(re.search(p, t) for p in pats)


def _find_entity_in_text(db_path: str, text: str,
                         vyzaduj_velke: bool = False) -> Optional[tuple]:
    """Najdi entity.name, která je zmíněna v textu (case insensitive, whole word).
    Vrací (name, source_url) nebo None. Preferuje delší jméno (specifičtější).

    HANS_SOURCE_PROPER_MENTION_V1 (11. 9.) — `vyzaduj_velke=True` uzná shodu
    jen tam, kde je jméno psané VELKÝM písmenem. Ve store je 95 jednoslovných
    obecných pojmů („Příležitost", „Secese", „Vývoj"…), takže běžné slovo
    v běžné řeči se jinak chytne jako ZDROJ — a tvrzení o PROVENIENCI je to
    poslední, co smí být vymyšlené.
    ⛔ Zapínat JEN nad Hansovými replikami (píše korektně), NIKDY nad dotazem
    uživatele. ⚠️ Původní zdůvodnění znělo „uživatel píše malá písmena“ —
    to podle něj samotného (11. 9.) platí JEN OBČAS, někdy píše i s velkými.
    Závěr se tím nemění, ale důvod je jiný a silnější: na straně uživatele
    není velké písmeno SPOLEHLIVÝ signál ani v jednom směru, takže se z něj
    nesmí dělat podmínka. Kdyby se to chtělo zapnout i tam, potřebuje to
    nejdřív měření na jeho skutečných větách, ne domněnku o jeho stylu.
    📏 Změřeno na 25 reálných dotazech na zdroj: nálezů 12 → 10, odmítnuty
    právě dvě falešné."""
    if not text:
        return None
    conn = None
    try:
        conn = _hr._ro(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, source FROM entities WHERE source IS NOT NULL "
            "AND source != '' AND length(name) >= 4").fetchall()
    except Exception:
        return None
    finally:
        if conn:
            conn.close()

    # HANS_ENTITY_WORDBOUND_V1 (30.8.) — DOCSTRING LHAL: slibuje „whole word",
    # ale porovnávalo se prostým `in`, tedy substringem kdekoli uvnitř slova.
    # Doloženo dlouhým rozhovorem 30.8.: na „odkud to víš?" Hans odpověděl
    # „O tématu 'Sedm' jsem se dočetl na Wikipedii" — entita „Sedm" se trefila
    # do slova „v posledních SEDMI dnech" v jeho vlastní předchozí replice.
    # Tvrzení o PROVENIENCI je přitom to poslední, co smí být vymyšlené.
    #
    # Hranice slova + nejvýš 3 znaky navíc: české skloňování přidá 1–3
    # („gotika/gotiky", „vývoj/vývojem"), kdežto ODVOZENINA víc a je to jiné
    # slovo („duch" → „duchovních"). ⚠️ Prahu 3 se nedotýkat bez měření —
    # simulace na 400 reálných replikách: ze 122 nálezů se změnily 4 a všechny
    # 4 byly falešné, žádná legitimní shoda („hrad Trosky", „Licence to Kill")
    # nezmizela.
    #
    # Číslovky ven úplně: „Sedm" jako pojem sedne na kterékoli počítání dnů
    # a jako zdroj tvrzení nedává smysl v žádném kontextu.
    def _psano_velkym(txt: str, jmeno: str) -> bool:
        """Je jméno v textu aspoň jednou psané velkým písmenem?
        Fail-open: při chybě True (radši nález než ticho)."""
        try:
            for m in re.finditer(re.escape(jmeno), txt, re.IGNORECASE):
                if txt[m.start():m.start() + len(jmeno)][:1].isupper():
                    return True
        except Exception:
            return True
        return False

    t_lower = text.lower()
    best = None
    for r in rows:
        name = r["name"]
        nl = name.lower()
        if nl in _hr._CISLOVKY:
            continue
        _shoda = re.search(r"(?<![\w])" + re.escape(nl) + r"\w{0,3}(?![\w])",
                           t_lower)
        # HANS_SOURCE_ENTITY_FOLD_V1 (14. 9.) — BEZ DIAKRITIKY, ale JEN nad
        # dotazem UŽIVATELE (píše z většiny bez háčků). Doloženo 13.–14. 9.:
        # „odkud vis o F. L. Vekovi?" nenašlo entitu „F. L. Věk", ačkoli ji
        # store MÁ i s odkazem → „nemám uložený článek". Změřeno: dotazy na
        # zdroj +2 (obě tahle věta), 0 jiných.
        # ⛔ NIKDY nad Hansovou replikou (`vyzaduj_velke`): tam by bez diakritiky
        # sedla 5× potvrzení malování („maluji obraz na téma ,Gustav Husak'")
        # a Hans by tvrdil, že o námětu obrazu ČETL — falešná provenience.
        if not _shoda and not vyzaduj_velke:
            _fn = _hr._fold(nl)
            if _fn != nl:
                _shoda = re.search(r"(?<![\w])" + re.escape(_fn) + r"\w{0,3}(?![\w])",
                                   _hr._fold(t_lower))
        if _shoda:
            # HANS_SOURCE_PROPER_MENTION_V1 — v replice jen vlastní jméno
            if vyzaduj_velke and not _psano_velkym(text, name):
                continue
            if best is None or len(name) > len(best[0]):
                best = (name, r["source"])
    return best


def _je_vycet(text: str) -> bool:
    return bool(_hr._VYCET_PAT.search(text or ""))


def _je_vypis_cetby(text: str) -> bool:
    """Je to vypis toho, co Hans CETL (a tedy legitimni referent)?"""
    return bool(_hr._CETBA_PAT.search(text or ""))


def _last_hans_topics(db_path: str, limit: int = 3,
                      person: Optional[str] = None,
                      okno_s: float = 3600.0) -> list:
    """Extrahuj potenciální témata z NĚKOLIKA posledních Hansových replik.
    Vrací list stringů (celý text Hansovy repliky) — volající pak matchuje entity.

    HANS_SOURCE_REFERENT_SCOPE_V1 (5.9.) — TŘI MEZE. Navazuje na
    `HANS_ENTITY_WORDBOUND_V1` (30.8.), který opravil, KTERÁ slova smí
    matchovat; tohle omezuje, KTERÝ TEXT se vůbec prohledává. Bez toho je
    tvrzení o PROVENIENCI vyrobené z náhodné věty — a to je konfabulace
    v nejcitlivějším místě.

    1. `person` — jen repliky TÉŽE osobě. Dřív se bralo globální pořadí
       deníku, takže „odkud to víš?" mohlo sáhnout po replice, kterou Hans
       dal NĚKOMU JINÉMU. Doložený nález #8: odpověď „O tématu Surrealismus
       jsem se dočetl na Wikipedii", přičemž Surrealismus padl v hovoru
       s JINÝM MLUVČÍM a v tomhle hovoru vůbec ne.
    2. `okno_s` — referent anaforické otázky musí být ČERSTVÝ. Změřeno na
       30 reálných dotazech na zdroj: medián mezery 1,0 min, 90. percentil
       8,1 min, maximum 46,7 min → hodina má rezervu 100 %.
    3. `_je_vycet` — výpis se přeskočí (viz komentář výše).

    ⚠️ `person=None` = beze změny (žádný filtr osoby) — kdyby funkci volal
    někdo, kdo mluvčího nezná, chová se jako dosud.
    Selhání kterékoli meze vede nejhůř k POCTIVÉMU PŘIZNÁNÍ („konkrétní zdroj
    nabídnout nemohu"), nikdy k vymyšlenému zdroji. Fail-safe je abstinence.
    """
    conn = None
    try:
        conn = _hr._ro(db_path)
        if okno_s and okno_s > 0:
            rows = conn.execute(
                "SELECT note FROM diary WHERE event_type='human_chat' "
                "AND ts >= ? ORDER BY ts DESC LIMIT ?",
                (time.time() - float(okno_s), limit * 8)).fetchall()
        else:
            rows = conn.execute(
                "SELECT note FROM diary WHERE event_type='human_chat' "
                "ORDER BY ts DESC LIMIT ?", (limit * 8,)).fetchall()
    except Exception:
        return []
    finally:
        if conn:
            conn.close()
    _kdo = (person or "").strip().lower()
    out = []
    for (note,) in rows:
        # note = "<osoba>: ...\nHans: ..." — vytáhni jen Hansovu část
        if not note:
            continue
        if _kdo and note.partition(":")[0].strip().lower() != _kdo:
            continue                      # replika NĚKOMU JINÉMU
        idx = note.find("Hans:")
        if idx >= 0:
            _h = note[idx + 5:].strip()
            if _hr._je_vycet(_h) and not _hr._je_vypis_cetby(_h):
                continue                  # výpis není tvrzení
                                          # (HANS_SOURCE_READING_LIST_V1:
                                          # výpis ČETBY je výjimka)
            out.append(_h)
        if len(out) >= limit:
            break
        if len(out) >= limit:
            break
    return out


def sensor_source_answer(db_path: str, user_text: str,
                         asker: str = "") -> Optional[str]:
    """HANS_SENSOR_SOURCE_SHARED_V1 (6.9.) — pochazi tvrzeni z CIDLA, ne ze cteni?

    Vraci hotovou odpoved (kamera / ČHMÚ / čidla v pokoji), nebo None.

    Blok byl doted uvnitr `sources_answer`, takze se k nemu chatovy prikaz
    /zdroje nedostal. Dolozeno 6. 9.: po rozsireni vzoru (HANS_SOURCES_VYKANI_V1)
    zacal prikaz brat i "odkud cerpas informace o pocasi?" a odpovedel
    VYPISEM PRECTENYCH CLANKU — tedy presne tou chybou, kterou
    HANS_SOURCE_IS_SENSOR_V2 uz 4. 9. opravil na druhe ceste.

    ⛔ NEDELAT z toho druhy predikat v chat_commands: pak by existovaly
    dve pravdy o tom, co je udaj z cidla, a rozesly by se. Proto je to
    JEDNA funkce, kterou volaji obe cesty.
    """
    oslov = _hr._cz_address(asker) if asker else "pane"
    # HANS_SOURCE_IS_SENSOR_V1 (20.8.) — NE KAŽDÉ TVRZENÍ POCHÁZÍ ZE ČTENÍ.
    # Doloženo 20.8.: „a odkud to víš, že tu je Jana?" → „nemám uložený
    # konkrétní článek s odkazem" — což je nesmysl, přítomnost člověka Hans
    # nemá z Wikipedie, ale z KAMERY. Celá tahle funkce mlčky předpokládá,
    # že zdroj = přečtený článek; u živého stavu je zdrojem ČIDLO.
    # (Chyba vznikla tím, že HANS_PROVENANCE_NOT_LIST_V1 z téhož dne správně
    # zrušil výpis zdrojů, ale odpověď pak spadla sem.)
    # Predikát je sdílený s agentem — jedna pravda o tom, co je dotaz na
    # přítomnost osoby.
    # ⚠️ NESTAČÍ zeptat se `_asks_person_presence` — ta řeší „je X doma?",
    # kdežto tady jde o dotaz na PŮVOD tvrzení o přítomnosti. A nestačí ani
    # samotné jméno: „odkud víš, že Jana ráda vaří?" je tvrzení ZE ZÁPISKŮ,
    # ne z čidla. Rozhoduje tedy DVOJICE: známá osoba + slovo o přítomnosti.
    try:
        import re as _re
        from scripts.cz_names import find_known_person
        # HANS_RECALL_CONFIG_IO_V1 (13. 9.) — SLOUCENY config.
        # `find_known_person` potrebuje `known_persons`, ktere od rozdeleni
        # configu 8. 9. (HANS_CONFIG_SPLIT_V1) sedi v `config.private.json`.
        # Cteni `config.json` napřimo tedy vratilo config BEZ JMEN → tahle
        # cidlova vetev od 8. 9. NESEPLA a na „odkud vis, ze tu je <osoba>?"
        # Hans misto „vidim to kamerou" vypsal nesouvisejici zapisek.
        # Doloženo regresni sadou 13. 9. (pripad „pritomnost -> cidlo“).
        from scripts.config_io import load as _cio_load
        _cfg = _cio_load()
        _pritomnost = _re.compile(
            r"\b(tu|tady|doma|p[řr][íi]toms?n|v\s+pokoji|v\s+m[íi]stnosti|"
            r"vid[íi][šs]|vid[íi]te)\b", _re.IGNORECASE)
        _o_pritomnosti = bool(
            find_known_person(user_text or "", _cfg)
            and _pritomnost.search(user_text or ""))
        # HANS_SOURCE_BARE_ODKUD_V1 (14. 9.) — přítomnost BEZ JMÉNA: „že tu někdo
        # je", „že tu jsem", „že nikdo není doma". Podmětem je neurčitá osoba
        # nebo tazatel sám, takže `find_known_person` nic nenajde a dotaz
        # propadl na výpis četby — doloženo 13. 9.: „a odkud to vis, ze tu jsem?"
        # → „Četl jsem tohle: Pán prstenů…". Zdrojem je i tady kamera.
        # Změřeno na 1 179 větách: sedne jen na 2 doložené; „že Jana ráda vaří"
        # (bez slova o přítomnosti) dál NE.
        if not _o_pritomnosti and _re.search(
                r"\b[žz]e\s+(tu|tady|doma|v\s+pokoji)\s+(n[ěe]kdo|nikdo|jsem|jsme|nejsem|nejsme)\b"
                r"|\b[žz]e\s+(n[ěe]kdo|nikdo)\s+(tu|tady|doma|v\s+pokoji)\b"
                r"|\b[žz]e\s+(jsem|jsme|nejsem|nejsme)\s+(tu|tady|doma|v\s+pokoji)\b"
                r"|\b(n[ěe]kdo|nikdo)\s+(je|nen[íi])\s+(tu|tady|doma|v\s+pokoji)\b",
                user_text or "", _re.IGNORECASE):
            _o_pritomnosti = True
        # Holé doptání („a odkud to víš?") jméno NEOBSAHUJE — předmětem je
        # POSLEDNÍ Hansova replika. Když ta hlásila, koho vidí, je zdrojem
        # kamera. Funkce si poslední repliky stejně tahá (fallback níž),
        # tak se použije týž zdroj místo nového mechanismu.
        # ⚠️ Anaforická větev jen u SKUTEČNĚ HOLÉHO doptání. Regresní sada
        # chytila, že jinak přebije i otázku s vlastním předmětem: „odkud víš,
        # že Jana ráda vaří?" dostalo odpověď „vidím to kamerou“ jen proto, že
        # poslední replika náhodou hlásila, koho Hans vidí.
        _hole = (len((user_text or "").split()) <= 6
                 and _re.search(r"\bto\b", user_text or "", _re.IGNORECASE))
        if not _o_pritomnosti and _hole:
            _hlaseni = _re.compile(
                r"(vid[íi]m\s+(tu|tady)|nikoho\s+nevid[íi]m|"
                r"zahl[ée]dl\s+jsem|je\s+doma|jsou\s+doma)", _re.IGNORECASE)
            for _r in _hr._last_hans_topics(db_path, limit=2, person=asker):
                if _hlaseni.search(_r or ""):
                    _o_pritomnosti = True
                    break
        if _o_pritomnosti:
            return ("To nemám ze zápisků, %s — vidím to kamerou. "
                    "Hlásím, koho právě rozpoznávám v místnosti." % oslov)
        # HANS_SOURCE_IS_SENSOR_V2 (4.9.) — TÁŽ TŘÍDA, DALŠÍ DVA ZDROJE.
        # V1 pokryl jen přítomnost osob (kamera), takže „odkud máš informace
        # o počasí?" pořád dostalo *„nemám v paměti uložený konkrétní článek…
        # jen obecná znalost"* — a to je NEPRAVDA: počasí Hans bere z ČHMÚ
        # (`weather_chmu.WeatherCHMU`) a teplotu/vlhkost v místnosti měří
        # vlastními čidly (`data/surroundings.db`). Falešné popření vlastního
        # zdroje je horší než mlčení: uživatel z něj usoudí, že si Hans počasí
        # vymýšlí.
        # ⚠️ Pořadí je významné — obě větve stojí AŽ ZA přítomností osob,
        # aby „odkud víš, že tu je Jana?" dál odpovídalo kamerou.
        _venku = _re.compile(
            r"\bpo[čc]as[íi]|\bvenku\b|za\s+oknem|\bp[řr][ée]dpov[ěe]|"
            r"\bpredpoved|\bpr[šs][íi]\b|\bsn[ěe][žz][íi]\b", _re.IGNORECASE)
        _uvnitr = _re.compile(
            r"(v\s+pokoji|v\s+m[íi]stnosti|uvnit[řr]|\btady\b|\btu\b)"
            r".{0,40}(teplot|vlhkost|stup[ňn]|dusno)"
            r"|(teplot|vlhkost|stup[ňn]|dusno).{0,40}"
            r"(v\s+pokoji|v\s+m[íi]stnosti|uvnit[řr])", _re.IGNORECASE)
        if _venku.search(user_text or ""):
            return ("To nemám ze zápisků, %s — počasí beru z Českého "
                    "hydrometeorologického ústavu, pokaždé znovu. "
                    "Uložený článek k tomu tedy nemám a mít nemusím." % oslov)
        if _uvnitr.search(user_text or ""):
            return ("To nemám ze zápisků, %s — teplotu a vlhkost v místnosti "
                    "měřím vlastními čidly. Je to údaj z měření, ne z četby."
                    % oslov)
    except Exception:
        pass
    return None


def sources_answer(db_path: str, user_text: str,
                   asker: Optional[str] = None) -> Optional[str]:
    """HANS_SOURCE_QUERY_V1 — DETERMINISTICKÁ odpověď (bypass LLM).

    Hans-czech (persona finetune) neposlouchá grounding — odmítá sdílet URL
    i když je má doslova v promptu (17.7. doložený případ „Icon of the Seas").
    Malý model je natrénovaný na personu „nemám externí zdroje" silněji než
    kterákoliv system-prompt instrukce. Řešení: pro dotaz na zdroj obejdi LLM
    a vygeneruj odpověď sám. Vzor: `commitments_answer`, `film_knowledge_answer`.

    Vrací string (Hansovým hlasem) nebo None (nic k nabídnutí → propadne do LLM).
    """
    oslov = _hr._cz_address(asker) if asker else "pane"  # HANS_NAME_INFLECTION_V1
    # HANS_SENSOR_SOURCE_SHARED_V1 — cidlo ma prednost pred zapisky.
    _sens = _hr.sensor_source_answer(db_path, user_text, asker)
    if _sens:
        return _sens


    hit = _hr._find_entity_in_text(db_path, user_text)
    if not hit:
        # HANS_SOURCE_ENTITY_RESOLVE_V1 (11. 9.) — doslovné hledání neuzná
        # skloněné víceslovné téma: „o ceskem raji" nenašlo „Český ráj",
        # a Hans tvrdil, že zdroj nemá, ačkoli ho má. `EntityStore.resolve`
        # to umí (token po tokenu, bez diakritiky). Jen ZÁLOHA za doslovným
        # hledáním, takže se stávající chování nemění.
        try:
            from scripts.config_io import load as _cio_load
            from scripts.hans_entities import EntityStore as _ES
            _e = _ES(_cio_load(), db_path).resolve(user_text)
            _src = (_e.get("source") or "").strip() if _e else ""
            if _src:
                hit = (_e.get("name"), _src)
        except Exception:
            pass
    if not hit:
        # fallback z posledních Hansových replik (user řekl jen „a odkud to víš")
        for hans_reply in _hr._last_hans_topics(db_path, limit=3, person=asker):
            # HANS_SOURCE_PROPER_MENTION_V1 — nad REPLIKOU jen vlastní jméno
            hit = _hr._find_entity_in_text(db_path, hans_reply,
                                       vyzaduj_velke=True)
            if hit:
                break

    if hit:
        name, url = hit
        return ("Ano, %s. O tématu '%s' jsem se dočetl na Wikipedii. "
                "Zde je odkaz: %s" % (oslov, name, url))

    # ── HANS_SOURCE_FRESH_LOOKUP_V1 (21. 9.) — ČERSTVÉ DOHLEDÁNÍ JE ZDROJ ──
    # Doloženo testovacím rozhovorem 21. 9. (tahy 5→6): Hans řekl „právě jsem
    # se podíval (Wikipedie — heslo Icon of the Seas)" a na navazující otázku
    # „na základě čeho to tvrdíte?" odpověděl, že zdroj nemá. Tazatel to vzal
    # jako přiznání lži.
    #
    # PŘÍČINA není v téhle funkci, ale v dělbě práce: `lookup_now` ZÁMĚRNĚ
    # nezapisuje do paměti nic (`instant-lookup-verify-loop` — provizorně
    # hned, do deníku a entit až po nočním ověření), takže mezi dohledáním
    # a nocí neexistuje entita s URL, po které tahle funkce sahá. Popření
    # bylo doslova vzato pravdivé („v PAMĚTI to uloženo nemám") a jako
    # odpověď přesto nepravdivé.
    #
    # Je to TÁŽ TŘÍDA jako `HANS_SOURCE_IS_SENSOR_V2` (4. 9.): další druh
    # zdroje, o kterém popření nevědělo. Proto stejné řešení — vlastní větev
    # PŘED přiznáním, ne rozšiřování hledání v entitách.
    #
    # ⚠️ Čerstvost se drží TÝMŽ oknem jako `_last_hans_topics` (1 h) a jen
    # pro TÉHOŽ tazatele — referent anaforické otázky musí být z TOHOTO
    # hovoru (HANS_SOURCE_REFERENT_SCOPE_V1). Bez obou mezí by Hans nabídl
    # odkaz k tématu, o kterém s tímhle člověkem vůbec nemluvil.
    # ⚠️ Odpověď MUSÍ říct, že to ještě není ověřené — jinak by se provizorní
    # nález tvářil jako uložená znalost a obešel by tím noční ověření.
    # HANS_SOURCE_FRESH_REFERENT_V1 (22. 9.) — CERSTVE DOHLEDANI MUSI BYT
    # REFERENT OTAZKY, ne proste posledni dohledani v okne.
    # Odhaleno ZIVYM testem 22. 9.: Hans vypsal ctyri polozky dnesni cetby,
    # uzivatel se zeptal "a odkud to mas?" a dostal odkaz na hrad Svojanov,
    # ktery si dohledal o dvacet minut driv v UPLNE JINE souvislosti.
    # Okno 1 h a "tyz tazatel" (HANS_SOURCE_REFERENT_SCOPE_V1) na to nestaci:
    # obe meze byly splneny, jen to nebylo to, na co se ptal.
    # Podminka je proto TATAZ, jakou pouziva vetev cetby niz: tema dohledani
    # se musi objevit v dotazu NEBO v tom, co Hans prave rekl. Kdyz ne,
    # propadne se dal — a odpovi bud cetba, nebo poctive priznani.
    try:
        _cerstve = _hr._cerstve_dohledani(db_path, asker)
        if _cerstve:
            _tema, _url = _cerstve
            _tt = _hr._zdroj_slova(_tema, minlen=4)
            _sedi = not _tt
            if _tt:
                # ⚠️ SEBEPOTVRZUJICI VSTUP — Hansovy vlastni odpovedi
                # o provenienci se jako dukaz referentu POUZIT NESMI.
                # Doloženo zivym testem 22. 9.: prvni (chybna) odpoved
                # o Svojanove skoncila v historii, a pri druhem dotazu
                # uz kontrola nasla "Svojanov" prave v ni — takze si
                # chybu potvrdila sama. Uzivatel se pritom pta znovu
                # PRAVE PROTO, ze prvni odpoved nesedela.
                # [[severka-input-is-self-confirming]]
                _moje = ("Dohledal jsem to b\u011bhem na\u0161eho hovoru",
                         "to m\u00e1m ze sv\u00e9 \u010detby")
                # POSLEDNI REPLIKA se bere z ULOZISTE KONVERZACE, ne
                # z `_last_hans_topics`: ta ma vlastni filtr vyctu
                # ("vypis neni tvrzeni"), takze prave tu repliku, na kterou
                # se uzivatel ptá, casto vynecha. Zmereno 22. 9.: po
                # dohledani hradu vratila tri STARSI repliky a cerstvou ne,
                # takze kontrola referentu selhala a odpovedela cetba.
                _kde = [user_text]
                try:
                    from scripts.conversation_store import ConversationStore
                    from scripts.config_io import load as _cio
                    _h = ConversationStore(_cio()).get_history(asker) or []
                    for _m2 in reversed(_h):
                        _txt2 = (_m2.get("content") or "") if isinstance(_m2, dict) else ""
                        if not _txt2:
                            continue
                        if (_m2.get("role") or "") != "assistant":
                            continue
                        if any(_m in _txt2 for _m in _moje):
                            continue      # nesmi se potvrzovat vlastni odpovedi
                        _kde.append(_txt2)
                        break
                except Exception:
                    _kde += [_r for _r in _hr._last_hans_topics(
                        db_path, limit=3, person=asker)
                        if not any(_m in _r for _m in _moje)]
                for _txt in _kde:
                    _ct = _hr._zdroj_slova(_txt, minlen=4)
                    if any(a[:5] == b[:5] for a in _tt for b in _ct):
                        _sedi = True
                        break
            if _sedi:
                return ("Ano, %s — to není ze zápisků. Dohledal jsem to během "
                        "našeho hovoru k tématu '%s': %s Ještě si to musím "
                        "ověřit, zatím to beru jako provizorní."
                        % (oslov, _tema, _url))
            _hr._log.info("HANS_SOURCE_FRESH_REFERENT_V1: čerstvé dohledání "
                      "'%.40s' není referent otázky — jdu dál", _tema)
    except Exception:
        pass

    # ── HANS_SOURCE_READING_URL_V1 (22. 9.) — ZDROJ U DENIKOVE CETBY ──
    # Dolozeno pametovou sadou 22. 9. pod SKUTECNYM jmenem (test persona na to
    # nestaci, do deniku nezapisuje): Hans vypsal ctyri polozky dnesni cetby
    # a na "a odkud to mas?" i na "ten clanek o gulagu - kde jsi ho cetl?"
    # odpovedel, ze zdroj nabidnout nemuze. Pritom tentyz clanek ma v deniku
    # `source_url` zapsane tyz den v 08:45:10.
    #
    # PRICINA: vetve vys hledaji ENTITU (vlastni jmeno). Titul clanku
    # ("Hlad, nemoci i nasili dozorcu, popsali archeologove podminky
    # v gulagu") zadna entita neni, takze retez propadl az k priznani.
    # Je to TATAZ TRIDA jako HANS_SOURCE_IS_SENSOR_V2 a _FRESH_LOOKUP_V1:
    # dalsi druh zdroje, o kterem popreni nevedelo -> vlastni vetev pred
    # priznanim, NE rozsirovani hledani v entitach.
    #
    # 📏 ZMERENO PRED NASAZENIM: na 25 realnych dotazech na zdroj z historie
    # chatu vraci vetev 0 nalezu (nevymysli si), na trech dolozenych vetach
    # a na skutecnem vypisu cetby 12/12 spravne.
    # ⚠️ Cetnost te tridy je 25 dotazu z 1 569 chatu a DVAKRAT je v historii
    # videt, ze se uzivatel musel zeptat znovu jinak ("ptam se jinak. odkud
    # vis o ...", "zeptam se jinak, kde jsi cetl o ..."). Drivejsi zaver
    # DROBNOSTI_17_09 o "0 vyskytech" merilo prilis uzke fraze.
    try:
        _cet = _hr._zdroj_z_cetby(db_path, user_text)
        if not _cet:
            # anaforicke "a odkud to mas?" — referent je v tom, co Hans PRAVE
            # rekl. Stejne mezE jako vys (tyz tazatel, okno 1 h) — jinak by
            # se zdroj vyrobil z cizi repliky.
            for _r in _hr._last_hans_topics(db_path, limit=2, person=asker):
                _cet = _hr._zdroj_z_cetby(db_path, _r)
                if _cet:
                    break
        if _cet:
            if len(_cet) == 1:
                _t, _u = _cet[0]
                return ("Ano, %s — to mám ze své četby: %s\n%s"
                        % (oslov, _t, _u))
            return (("Ano, %s — to mám ze své četby:\n" % oslov)
                    + "\n".join("\u2013 %s\n  %s" % (_t, _u) for _t, _u in _cet))
    except Exception:
        pass

    # nic konkrétního — poctivé přiznání (bez konfabulace)
    return ("K tomu, o čem jsme mluvili, nemám v paměti uložený konkrétní "
            "článek s odkazem, %s. Zůstává mi jen obecná znalost, kterou "
            "jsem si osvojil — konkrétní zdroj Vám k tomu nabídnout nemohu, "
            "nechci si nic vymýšlet." % oslov)


def _zdroj_slova(text: str, minlen: int = 5, bez_ramce: bool = False) -> set:
    """HANS_SOURCE_READING_URL_V1 — vyznamova slova bez diakritiky."""
    import unicodedata as _u
    _t = "".join(c for c in _u.normalize("NFD", (text or "").lower())
                 if not _u.combining(c))
    _s = {w for w in re.findall(r"[0-9a-z]+", _t)
          if len(w) >= minlen and w not in _hr._STOPWORDS}
    return {w for w in _s if w not in _hr._ZDROJ_RAMEC} if bez_ramce else _s


def _zdroj_z_cetby(db_path: str, text: str, okno_dnu: int = 14,
                   limit: int = 80, pokryti: float = 0.5) -> list:
    """HANS_SOURCE_READING_URL_V1 — zdroje precteneho, na co se text pta.

    Vraci seznam (titul, url), nejvys 3, nejnovejsi napred. Fail-safe je
    prazdny seznam: radsi poctive priznani nez odkaz k necemu jinemu.

    DVA REZIMY, podle toho, co muze nabidnout CIL:
      (a) dlouhy text (Hansova replika s vypisem cetby) — musi pokryt
          vetsinu vyznamovych slov titulu,
      (b) kratka otazka, kde uzivatel jmenuje tema — titul nema jak
          pokryt, takze staci, kdyz se trefi VSECHNA slova dotazu.
    Porovnava se na PREFIX peti znaku, aby sedelo sklonovani (tyz pristup
    jako entity store).
    """
    try:
        _c = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        _rows = _c.execute(
            "SELECT title, source_url, ts FROM diary WHERE source_url "
            "IS NOT NULL AND source_url != '' AND ts > ? "
            "ORDER BY ts DESC LIMIT ?",
            (time.time() - okno_dnu * 86400, limit)).fetchall()
        _c.close()
    except Exception:
        return []
    _cil = _hr._zdroj_slova(text, minlen=4, bez_ramce=True)
    if not _cil:
        return []
    _ven = []
    for _title, _url, _ts in _rows:
        _tt = _hr._zdroj_slova(_title)
        if not _tt:
            continue
        _sh = {a for a in _tt if any(a[:5] == b[:5] for b in _cil)}
        if not _sh:
            continue
        _sh_cil = {b for b in _cil if any(a[:5] == b[:5] for a in _tt)}
        if (len(_sh) / len(_tt) >= pokryti
                or (len(_cil) <= 3 and len(_sh_cil) == len(_cil))):
            _ven.append((_ts, _title, _url))
    _ven.sort(reverse=True)
    _vid, _out = set(), []
    for _ts, _t, _u in _ven:
        if _u in _vid:
            continue
        _vid.add(_u)
        _out.append((_t, _u))
    return _out[:3]


def _cerstve_dohledani(db_path: str, asker: Optional[str] = None,
                       okno_s: float = 3600.0):
    """HANS_SOURCE_FRESH_LOOKUP_V1 — poslední dohledání TOHOTO tazatele
    z čekárny `unverified_findings`. Vrací (téma, url) nebo None.

    Fail-safe je abstinence: když chybí url, téma nebo tazatel, vrací None
    a volající spadne do dosavadního poctivého přiznání.
    """
    if not asker:
        return None
    conn = None
    try:
        conn = _hr._ro(db_path)
        row = conn.execute(
            "SELECT topic, url FROM unverified_findings "
            "WHERE asker = ? AND ts >= ? AND COALESCE(url,'') <> '' "
            "ORDER BY ts DESC LIMIT 1",
            (asker, time.time() - float(okno_s))).fetchone()
        if row and row[0] and row[1]:
            return str(row[0]), str(row[1])
    except Exception:
        return None
    finally:
        try:
            if conn:
                conn.close()
        except Exception:
            pass
    return None


def sources_reply(db_path: str, user_text: str = "", limit: int = 5,
                  asker: Optional[str] = None) -> str:
    """HANS_SOURCE_QUERY_V1 — grounding blok pro dotaz „odkud to víš".

    STRATEGIE (17.7. — přepracováno na FAKTA, ne instrukce; malý model neuměl
    dvoustupňovou inferenci „najdi téma v seznamu → vytáhni URL"):
      1. Zkus najít KONKRÉTNÍ entitu zmíněnou v user promptu → hotová URL.
      2. Fallback na entitu z předchozí Hansovy repliky (dohledá téma o kterém
         právě mluvil, i když user řekne jen „a odkud to víš").
      3. Když nic nenajde → čestně přiznat obecnou znalost.
    """
    # 1) entita v samotném dotazu ("ukaž mi zdroj o Icon of the Seas")
    hit = _hr._find_entity_in_text(db_path, user_text)
    if not hit:
        # 2) entita v Hansově předchozí odpovědi (user: „a odkud to víš?")
        for hans_reply in _hr._last_hans_topics(db_path, limit=3, person=asker):
            # HANS_SOURCE_PROPER_MENTION_V1 — nad REPLIKOU jen vlastní jméno
            hit = _hr._find_entity_in_text(db_path, hans_reply,
                                       vyzaduj_velke=True)
            if hit:
                break

    if hit:
        name, url = hit
        return (
            "\n\nUZIVATEL SE PTA NA ZDROJ. Toto jsou TVA FAKTA (ne instrukce, "
            "ne 'externi zdroje' - jsou to zaznamy z tve pameti):\n\n"
            "O tematu \"%s\" mas v pameti clanek na URL: %s\n\n"
            "Odpovez uzivateli PRESNE tuto URL. Priklad odpovedi: 'Cetl jsem "
            "o tom na Wikipedii. Odkaz: %s'. NEtvrd 'nemam pristup k externim "
            "zdrojum' - mas ho tady, prave ted, v tomto promptu."
        ) % (name, url, url)

    # 3) nic konkrétního — poctivé přiznání
    return (
        "\n\nUZIVATEL SE PTA NA ZDROJ. K tematu, o kterem jste mluvili, "
        "NEMAS v pameti zadny konkretni ulozeny clanek s URL. Odpovez "
        "cestne: 'To vim z obecne znalosti, konkretni clanek jsem k tomu "
        "necetl'. NIKDY nevymyslej URL, nazvy clanku, ani citace.")

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_recall as _hr  # noqa: E402
