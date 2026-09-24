"""regrese_helpers.py — tenké obálky pro regresní sadu.

Některé kontroly potřebují METODU instance (např. `AgentRouter._hint_match`),
ale sada volá funkce podle jména „modul.funkce". Aby v datovém souboru
nemusela být logika, bydlí ta obálka tady — a NIC nepočítá sama, jen
zavolá skutečný kód, aby se testovalo to, co běží v provozu.
"""
from __future__ import annotations

import json
from functools import lru_cache


# ── REGRESE_CONFIG_IO_V1 (13. 9.) — SLOUCENY config, ne jen verejny ────────
# Sada cetla `config.json` NAPRIMO na sesti mistech. Od rozdeleni configu
# 8. 9. (HANS_CONFIG_SPLIT_V1) tam ale `known_persons` NEJSOU — sedi
# v `config.private.json`. Vysledek: predikaty, ktere potrebuji jmena
# domacnosti, dostaly prazdno a testy hlasily FALESNE SELHANI.
# Doloženo 13. 9.: `_asks_person_presence("je <jmeno> doma?")` vraci
# False s verejnym configem a True se sloucenym → pravidlo
# HANS_PRESENCE_ASK_V1 vypadalo rozbite, pritom fungovalo.
# ⚠️ Tataz past chytla tyz den dvakrat i mimo sadu (fix_addressee,
# _resolve_person). V testech VZDY `config_io.load()`.
@lru_cache(maxsize=1)
def _cfg() -> dict:
    """Sloučený config (veřejný + privátní). Fallback na veřejný, kdyby
    `config_io` nebyl k dispozici — sada nesmí spadnout kvůli importu."""
    try:
        from scripts import config_io
        return config_io.load()
    except Exception:
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)


@lru_cache(maxsize=1)
def _router():
    from scripts.hans_agent import AgentRouter
    return AgentRouter(_cfg())


def hint_match(text: str) -> bool:
    """Trefila věta některou agentní nápovědu? (brána PŘED LLM routerem)"""
    return bool(_router()._hint_match(text))


def pravidlo(aid: str, text: str, args: dict | None = None):
    """HANS_AGENT_RULES_TABLE_V1 — co udělá tabulka pravidel se zvolenou akcí?

    Vrací nové id akce, nebo None (= potlačeno). Volá se PŘÍMO
    `_uplatni_pravidla`, tedy bez routeru i bez provádění akce — test tak
    tvrdí ROZHODNUTÍ, ne text odpovědi.
    """
    dec = {"action": aid, "args": args or {}, "confidence": 0.95}
    r = _router()
    r._is_small_talk = lambda m: False   # LLM klasifikátor mimo hru
    return r._uplatni_pravidla(aid, text, dec, None)


def poradi_bloku(varianta: str) -> str:
    """HANS_PROMPT_BLOCKS_TABLE_V1 — v jakém pořadí se skládá system prompt?
    Vrací názvy bloků oddělené '|' (hodnota bloku = jeho jméno)."""
    from scripts.openwebui_direct_handler import _PROMPT_BLOKY, slozit_prompt
    return slozit_prompt({n: n + "|" for n, _ in _PROMPT_BLOKY}, varianta)


def posledni_blok(varianta: str) -> str:
    """Poslední blok varianty — u plného promptu MUSÍ být adresát (`current`)."""
    return poradi_bloku(varianta).rstrip("|").split("|")[-1]


def tiche_vychody_groundingu() -> int:
    """HANS_GROUNDING_OUTCOME_LOG_V1 — kolik východů obchází logovaný setter?

    Musí být 0: každý výsledek groundingu má téct přes
    `_vysledek_groundingu`, jinak se v logu ztratí, KTERÁ cesta odpověď
    rozhodla (a přesně to zdržovalo ladění 20.8.). Jediné povolené přiřazení
    je uvnitř samotného setteru.
    """
    import re
    from pathlib import Path
    src = Path("scripts/openwebui_direct_handler.py").read_text(encoding="utf-8")
    i = src.index("def _vysledek_groundingu(")
    j = src.index("def _build_grounding(", i)
    mimo = src[:i] + src[j:]
    return len(re.findall(r"self\._grounding_outcome\s*=", mimo))


def nezname_rutiny() -> str:
    """HANS_SCHEDULE_NIGHT_STEPS_V1 — hlásí se někde rutina, kterou rozvrh nezná?

    `hans_schedule.mark()` neznámý název **tiše zahodí** (jen debug hláška),
    takže překlep nebo zapomenutý seed = rutina se tváří, že běží, a nikdy se
    nezapíše. Tahle kontrola je statická: posbírá názvy ze VŠECH `mark(...)`
    volání v kódu a porovná je se seedem. Vrací názvy navíc (prázdno = OK).
    """
    import re
    from pathlib import Path
    from scripts.hans_schedule import _SEED
    znama = {s[0] for s in _SEED}
    volane = set()
    for f in Path("scripts").glob("*.py"):
        if f.name == "hans_schedule.py":
            continue
        for m in re.finditer(r"mark\(\s*['\"](\w+)['\"]", f.read_text(encoding="utf-8")):
            volane.add(m.group(1))
    return ",".join(sorted(volane - znama))


def blok_o_sobe(varianta: str) -> str:
    """HANS_SELF_STATE_NO_OFF_MODES_V1 — blok „FAKTA O MĚ" pro daný stav.
    `guard_on` / `guard_off` — vypnuté hlídání se zmiňovat NESMÍ (model si
    ho v plném promptu překlopil do kladu a tvrdil, že v noci hlídal)."""
    from scripts.hans_recall import self_state_facts
    return self_state_facts("data/hans_diary.db", mood="content",
                            runtime={"guard": varianta == "guard_on",
                                     "sleeping": False}) or ""


def agent_kontext_ma_fazi() -> bool:
    """HANS_AGENT_CTX_PHASE_FIX_V1 — dostane agentní router řádek „Situace: …"?

    `phase_label` je @property; volání se závorkami házelo TypeError, který
    spolkl `except` pod tím, a řádek z kontextu TIŠE mizel. Není to kosmetika:
    bez něj kontext začíná větou o televizi a router se jí chytí (změřeno:
    add_note 3/3 se Situací × report_now_playing 3/3 bez ní).
    """
    class _Rutina:
        @property
        def phase_label(self):
            return "ráno"

    class _Idle:
        _routine = _Rutina()
        kodi = None

    class _H:
        _hans_idle = _Idle()

        class conv_store:
            @staticmethod
            def get_history(n, **kw):
                return []

            @staticmethod
            def get_history_scoped(n, ch):
                return []

    return "Situace: ráno." in (_router()._context(_H(), "kdokoliv") or "")

def zdroje_odpoved(veta: str) -> str:
    """HANS_SOURCES_TOPIC_V1 — co /zdroje reálně odpoví na danou větu.

    Volá skutečný `_cmd_zdroje` nad živým deníkem (read-only), aby se
    testovalo chování, ne jen regex. Handler stačí atrapa s configem —
    příkaz z něj bere jen cestu k DB.
    """
    from scripts.chat_commands import _cmd_zdroje

    class _H:
        def __init__(self):
            self.config = _cfg()          # REGRESE_CONFIG_IO_V1

    return _cmd_zdroje(_H(), "Uživatel", veta) or ""

def stejny_navrh(titul_a: str, titul_b: str) -> bool:
    """HANS_AGENT_ECHO_HASH_V1 — považuje anti-echo dva tituly za TÝŽ návrh?

    Klíč, pod kterým se pamatuje odmítnutí, musí přežít jinou velikost písmen
    i mezery — jinak se odmítnutý film vrátí (doloženo 20.8., „Projekt A").
    """
    from scripts.hans_agent import _args_hash
    return (_args_hash("kodi_play_film", {"titul": titul_a})
            == _args_hash("kodi_play_film", {"titul": titul_b}))


def popis_dilu(ep: dict) -> str:
    """HANS_KODI_EPISODES_V1 — jednotný lidský popis dílu seriálu."""
    from scripts.kodi_client import KodiClient
    return KodiClient.episode_label(ep)


def cetba_bez_duplicit() -> bool:
    """HANS_READING_DEDUP_V1 — nemá výpis /cetl dvakrát tentýž titul?

    Jede nad živým deníkem: obsah se mění, ale tvrzení „žádný titul dvakrát"
    platí vždycky. Doloženo 20.–21.8., kdy se ze čtyř řádků staly dva tituly.
    """
    from scripts.hans_recall import reading_answer
    out = reading_answer("data/hans_diary.db", "co jsi dnes cetl?") or ""
    tituly = []
    for radek in out.split("\n"):
        radek = radek.strip()
        if not radek.startswith("–"):
            continue
        # „– 21. srpna (četba): TITUL"
        _, _, zbytek = radek.partition(":")
        t = (zbytek or radek).strip().lower()
        if t:
            tituly.append(t)
    return len(tituly) == len(set(tituly))


def obsazeni_az_za_prepisem() -> bool:
    """HANS_KODI_CAST_FACT_V2 — stojí blok obsazení AŽ ZA přepisem dotazu?

    Strukturální test, protože chyba nebyla ve funkci, ale v jejím POŘADÍ:
    blok potřebuje název filmu, který doplní až F1 rewriter. Nad ním dostával
    holou větu („kdo tam hraje?") a mlčel. Táž třída jako A1 brzda.
    """
    src = open("scripts/openwebui_direct_handler.py", encoding="utf-8").read()
    i_prepis = src.find("_q_for_retrieval = _rw.strip()")
    i_blok = src.find("self._kodi_cast_fact(")
    return i_prepis != -1 and i_blok != -1 and i_blok > i_prepis


def veta_se_zmenila(puvodni: str, opravena: str) -> bool:
    """HANS_CMD_LLM_ROUTE_TYPO_V1 — rozhodlo by se o štítku znovu?

    True = oprava větu opravdu změnila (druhé kolo routeru má proběhnout),
    False = liší se jen diakritikou/interpunkcí a druhé kolo se přeskočí.
    """
    from scripts.chat_commands import _norm_veta
    return _norm_veta(puvodni) != _norm_veta(opravena)


def kotva_v_kazdem_stupni(dotaz: str, kotva: str) -> bool:
    """HANS_CONVINDEX_ANCHOR_V1 — drží se předmět dotazu ve VŠECH stupních?

    Doloženo 21.8. („hrad Kost"): žebřík ubíral nejkratší slovo, takže
    jediné specifické slovo vypadlo první a zůstal balast — a Hans dostal
    cizí zápisky pod hlavičkou „tohle máš ve svých zápiscích".
    """
    from scripts.hans_convindex import relax_attempts
    kroky, _ = relax_attempts(dotaz)
    return bool(kroky) and all(kotva in k for k in kroky)


def zebrik_relaxace(dotaz: str) -> str:
    """Stupně relaxace jako jeden řetězec (ať se dá tvrdit `obsahuje`)."""
    from scripts.hans_convindex import relax_attempts
    kroky, uzky = relax_attempts(dotaz)
    return " | ".join(list(kroky) + list(uzky))


def hola_jmena_atributu(soubor: str) -> str:
    """HANS_CHATLOG_NOT_FACT_V2 — atribut třídy volaný jako holé jméno.

    Doloženo 22.8.: `_CHATLOG_RE.search(...)` uvnitř metody = NameError,
    takže filtr chatlogů z 19.8. NIKDY neběžel — a protože ho okolní
    `except` spolkl, přišla o výsledky celá RAG kolekce.

    Čte se AST, ne regex: první verze hledala „odsazené přiřazení VELKÝM
    JMÉNEM", což je i modulová konstanta uvnitř `try:` — na celém repu z toho
    bylo 13 falešných nálezů. AST rozliší tělo třídy od modulu a lokální
    proměnnou od atributu; přes celé `scripts/` hlásí nula, a proti kódu
    před opravou hlásí přesně `_CHATLOG_RE`.

    Vrací prázdný řetězec, když je soubor čistý; jinak „jméno:řádek".
    """
    import ast
    try:
        strom = ast.parse(open(soubor, encoding="utf-8").read())
    except SyntaxError as e:
        return "nešlo rozparsovat: %s" % e
    modul = {t.id for u in strom.body if isinstance(u, ast.Assign)
             for t in u.targets if isinstance(t, ast.Name)}
    for u in strom.body:            # konstanty uvnitř try/if na úrovni modulu
        if isinstance(u, (ast.Try, ast.If, ast.For, ast.While)):
            for v in ast.walk(u):
                if isinstance(v, ast.Assign):
                    for t in v.targets:
                        if isinstance(t, ast.Name):
                            modul.add(t.id)
    nalezy = []
    for tr in ast.walk(strom):
        if not isinstance(tr, ast.ClassDef):
            continue
        atrib = {t.id for u in tr.body if isinstance(u, ast.Assign)
                 for t in u.targets if isinstance(t, ast.Name)} - modul
        if not atrib:
            continue
        for fn in tr.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            lokal = {t.id for u in ast.walk(fn) if isinstance(u, ast.Assign)
                     for t in u.targets if isinstance(t, ast.Name)}
            lokal |= {a.arg for a in fn.args.args}
            for n in ast.walk(fn):
                if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                        and n.id in atrib and n.id not in lokal):
                    nalezy.append("%s:%d" % (n.id, n.lineno))
    return ", ".join(sorted(set(nalezy)))


def hola_jmena_atributu_repo() -> str:
    """Táž kontrola přes CELÉ `scripts/` — třída chyby, ne jeden soubor."""
    import glob
    spatne = []
    for f in sorted(glob.glob("scripts/*.py")):
        r = hola_jmena_atributu(f)
        if r:
            spatne.append("%s → %s" % (f, r))
    return "OK" if not spatne else "; ".join(spatne)


# ── KORPUSOVÉ KONTROLY ──────────────────────────────────────────────────────
# Ručně vybraná věta dokazuje, že oprava drží NA NÍ. Neřekne ale, jestli se
# hranice neposunula jinde — a právě posun hranice dělá většinu chyb (vzor
# sedí na osm formulací a na devátou už ne). Tyhle kontroly proto pouštějí
# funkci přes STOVKY skutečných vět z deníku a hovorů a hlídají ČÍSLO.
# Vracejí „OK", nebo popis překročení (běhoun ho vypíše celý).
# Korpus je gitignorovaný (data/), takže bez něj kontrola tiše projde —
# jinak by sada spadla každému, kdo si repo jen naklonuje.

def _repliky_uzivatele(limit: int = 500) -> list:
    """Poslední repliky ČLOVĚKA z deníkových `human_chat` (ne Hansovy)."""
    import os
    import sqlite3
    db = "data/hans_diary.db"
    if not os.path.exists(db):
        return []
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='human_chat' "
            "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        conn.close()
    except Exception:
        return []
    out = []
    for (n,) in rows:
        for line in (n or "").splitlines():
            if ":" in line:
                kdo, _, txt = line.partition(":")
                if kdo.strip().lower() != "hans" and txt.strip():
                    out.append(txt.strip())
                    break
    return out


def korpus_brana_navrhu(strop: int = 25) -> str:
    """HANS_DEEPEN_FEEDBACK_GATE_V1 — kolik běžných replik projde ke
    klasifikátoru návrhu prohloubení. Dnes 17 z 500; před opravou procházelo
    všechno kromě otázek. Když číslo vyskočí, brána se zase rozvolnila."""
    from scripts.hans_study import je_reakce_na_navrh
    vety = _repliky_uzivatele()
    if not vety:
        return "OK"
    prosly = [v for v in vety if je_reakce_na_navrh(v)]
    if len(prosly) <= strop:
        return "OK"
    return "PŘEKROČENO: %d z %d replik (strop %d), např. %r" % (
        len(prosly), len(vety), strop, prosly[0][:60])


def korpus_dedup_jen_vokativy() -> str:
    """G4D_ADDRESS_KNOWN_VOCATIVE_V1 — dedup oslovení smí ubrat JEN vokativ.

    Kontroluje se vlastnost, ne počet: projde všechny Hansovy uložené
    odpovědi a hlásí první úsek, který zmizel a vokativ známé osoby to
    nebyl. Doloženo 22.8.: mizelo „nesedělo", „toho", „ticho"."""
    import glob
    import json
    import os
    from scripts.conversation_store import (dedup_address_g4d, _ADDRESS_RE_G4D,
                                            _fold_g4d, _vokativy_g4d)
    if not os.path.exists("config.json"):
        return "OK"
    cfg = _cfg()                      # REGRESE_CONFIG_IO_V1
    for f in sorted(glob.glob("data/conversations/*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        partner = os.path.basename(f)[:-5]
        povol = _vokativy_g4d(partner, cfg)
        for m in data.get("messages", []):
            if m.get("role") != "assistant":
                continue
            t = m.get("content") or ""
            out = dedup_address_g4d(t, partner, cfg)
            if out == t:
                continue
            for mm in list(_ADDRESS_RE_G4D.finditer(t))[1:]:
                usek = _fold_g4d(mm.group(0).strip(" ,"))
                if usek not in povol and mm.group(0) not in out:
                    return "smazáno mimo vokativ: %r" % usek
    return "OK"


def korpus_kotva_balast() -> str:
    """HANS_CONVINDEX_ANCHOR_SENTENCE_V1 — kotva se nesmí brát ze začátku
    další věty. Kotva se z relaxace NEUBÍRÁ, takže balast jako „Rád" nebo
    „Myslím" by v dotazu zůstal viset ve všech stupních a hledání by nenašlo
    nic (falešná absence, přesně to, proti čemu relaxace vznikla).
    Před opravou: 20 z 500 replik."""
    from scripts.hans_convindex import kotvy_ve_vete
    vety = _repliky_uzivatele()
    if not vety:
        return "OK"
    for v in vety:
        for i, w in kotvy_ve_vete(v):
            pred = v[:v.find(w)].rstrip()
            if pred and pred[-1] in ".!?…:;":
                return "kotva ze začátku věty: %r v %r" % (w, v[:60])
    return "OK"


def dedup_osloveni(text: str, jmeno: str) -> str:
    """G4D_ADDRESS_KNOWN_VOCATIVE_V1 — dedup oslovení s reálným configem.

    Doloženo 22.8.: dřív se mazalo jakékoli slovo končící na -o/-e před
    čárkou („nesedělo", „toho", „ticho"), protože regex měl IGNORECASE.
    """
    import json
    from scripts.conversation_store import dedup_address_g4d
    cfg = _cfg()                      # REGRESE_CONFIG_IO_V1
    return dedup_address_g4d(text, jmeno, cfg)


def fronta_prezije_restart(text: str) -> str:
    """HANS_MATRIX_DEFERRED_FILE_V1 — odložená zpráva přežije restart mostu.

    Doloženo 22.8.: fronta byla seznam v paměti, Hans se v 08:35 restartoval
    a návrh prohloubení z 00:30 uživateli nikdy nedorazil. „Restart" se tu
    simuluje tím, že se čte ZE SOUBORU, ne z objektu, který zprávu uložil.
    Vrací text doručené zprávy, nebo prázdno.
    """
    import tempfile
    import os
    from scripts.hans_matrix import _dq_pridej, _dq_vyber
    cesta = os.path.join(tempfile.mkdtemp(), "odlozene.jsonl")
    _dq_pridej(text, "!room:matrix.org", cesta)
    rows = _dq_vyber(cesta)
    return rows[0]["text"] if rows else ""


def guard_zahodil(odpoved: str, podklad: str) -> int:
    """GROUNDING_GUARD_ACTIVE_V2 — kolik vět odpovědi nemá v podkladu oporu."""
    from scripts.grounding_guard import check
    _, zahozene = check(odpoved, podklad)
    return len(zahozene)


def vlastni_dilo(titul: str, dila: list) -> bool:
    """BOOK_MENTIONS_OWN_WORKS_V1 — obálka: sada posílá seznam, funkce chce set.
    Díla se ZÁMĚRNĚ předávají jako argument, ne čtou z DB — test nesmí být
    závislý na tom, co má Hans zrovna rozepsané."""
    from scripts.hans_book_mentions import _je_vlastni_dilo, _norm_title
    return _je_vlastni_dilo(_norm_title(titul), {_norm_title(d) for d in dila})


def thread_guard(cid: str, veta: str) -> str:
    """HANS_THREAD_NO_LIST_V1 — obálka: `_thread_guard` je metoda modulu
    a bere config + turns, které sada nemá. Config se načte, turns jsou
    prázdné ZÁMĚRNĚ: guard, který tohle řeší, je deterministický a na
    vlákně nestojí (jinak by test měřil něco jiného než rozhodnutí)."""
    import json
    from scripts.chat_commands import _thread_guard
    cfg = _cfg()                      # REGRESE_CONFIG_IO_V1
    return _thread_guard(cid, veta, cfg, turns=[])


def entita_v_textu(text: str) -> str:
    """HANS_ENTITY_WORDBOUND_V1 — obálka: funkce vrací dvojici (jméno, URL)
    a bere cestu k DB. Sada porovnává jen JMÉNO; prázdný řetězec = nenalezeno,
    ať se v případech nemusí psát None. DB je reálná — entity store je vstup,
    který tenhle test ověřuje (ne fixture)."""
    from scripts.hans_recall import _find_entity_in_text
    hit = _find_entity_in_text("data/hans_diary.db", text)
    return hit[0] if hit else ""


def wiki_pokryti_ok(query: str, title: str) -> bool:
    """HANS_WIKI_COVERAGE_V1 — projde titul prahem zpětného pokrytí?
    Práh se čte z configu (default 0.4), ať test měří TOTÉŽ co běžící kód."""
    import json
    from scripts.web_reader import _title_coverage
    cfg = _cfg()                      # REGRESE_CONFIG_IO_V1
    prah = float((cfg.get("curiosity", {}) or {}).get("wiki_title_min_coverage", 0.4))
    return _title_coverage(query, title) >= prah


def kniha_po_doporuceni(predchozi: str, veta: str) -> str:
    """HANS_BOOK_RECOMMEND_FOLLOWUP_V1 (14. 9.) — dostane navazujici veta po
    doporuceni cetby skutecnou knihovnu? Vraci "navaz" | "blok" | "nic".
    Kazde volani ma cistou instanci, aby se pripady neovlivnovaly stavem."""
    from scripts.openwebui_direct_handler import OpenWebUIDirectHandler as _H
    h = _H.__new__(_H)
    h.config = _cfg()
    if predchozi:
        h._knihovna_fact(predchozi, "regrese")
    r = h._knihovna_fact(veta, "regrese")
    if not r:
        return "nic"
    return "navaz" if r.startswith("\n\nUZIVATEL NAVAZUJE") else "blok"


def orez_pozdravu(text: str, role: str = "assistant") -> str:
    """HANS_CONV_GREETING_DIALOG_V2 (14. 9.) — co z repliky uvidi prompt,
    kdyz je v okne historie JEDINA (nejnovejsi). Vola skutecnou metodu
    `ConversationStore._orez_pozdravy`, bez zapisu do uloziste."""
    from scripts.conversation_store import ConversationStore
    cs = ConversationStore.__new__(ConversationStore)
    return cs._orez_pozdravy([{"role": role, "content": text}])[0]["content"]


def zajmy_na_hanse(veta: str) -> bool:
    """HANS_ZAJMY_O_HANSOVI_V2 — mireji veta na HANSOVY zajmy (ne tazatele)?"""
    from scripts.chat_commands import _ZAJMY_NA_HANSE
    return bool(_ZAJMY_NA_HANSE.search(veta))


def den_v_tydnu(veta: str) -> str:
    """HANS_WEEKDAY_FIX_V1 — co z vety udela oprava dne v tydnu, kdyz je
    pondeli 14. 9. 2026 (pevne datum, aby sada nezavisela na dni behu)."""
    from datetime import datetime
    from scripts.cz_names import fix_weekday
    return fix_weekday(veta, datetime(2026, 9, 14, 10, 30))[0]


def zdroj_entita(text: str, replika: bool):
    """HANS_SOURCE_ENTITY_FOLD_V1 — jmeno entity, kterou `_find_entity_in_text`
    v textu najde (nad replikou s `vyzaduj_velke`), nebo None. Cte ostry denik."""
    from scripts.hans_recall import _find_entity_in_text
    h = _find_entity_in_text("data/hans_diary.db", text, vyzaduj_velke=replika)
    return h[0] if h else None


def osloveni_jednou(text: str) -> str:
    """HANS_ADDRESSEE_ONCE_V1 — co z odpovedi udela `fix_addressee`, kdyz
    se ptala test persona `zkouška` (osloveni jen jednou za odpoved)."""
    from scripts.cz_names import fix_addressee
    return fix_addressee(text, "zkouška", _cfg())[0]


def thread_guard_po(cid: str, veta: str, predchozi: str) -> str:
    """HANS_KALENDAR_NOT_ELLIPSIS_V1 — `_thread_guard` s vlaknem, ve kterem
    Hans naposledy odpovedel `predchozi`."""
    from scripts.chat_commands import _thread_guard
    return _thread_guard(cid, veta, _cfg(), turns=[("user", "x"), ("assistant", predchozi)])


def dilo_odpoved_zacina(dotaz: str, zacatek: str) -> bool:
    """HANS_WORK_RECALL_IN_ARTWORK_V1 — zacina odpoved /obrazy na `zacatek`?
    Cte ostry denik (dila i obrazy tam jsou trvale)."""
    from scripts.hans_recall import artwork_answer
    return artwork_answer("data/hans_diary.db", dotaz).startswith(zacatek)


def _handler_bez_initu():
    from scripts.openwebui_direct_handler import OpenWebUIDirectHandler
    h = OpenWebUIDirectHandler.__new__(OpenWebUIDirectHandler)
    h.config = _cfg()
    return h


def entita_je_tazatel(tazatel: str, jmeno_entity: str) -> bool:
    """HANS_ENTITY_NOT_ASKER_V1."""
    h = _handler_bez_initu()
    h._tazatel_ted = tazatel
    return h._entita_je_tazatel({"name": jmeno_entity})


def obraz_fakt_je(veta: str) -> bool:
    """HANS_ARTWORK_CONTENT_GROUNDED_V1 — dostane veta blok s popisem obrazu?"""
    return bool(_handler_bez_initu()._obraz_fact(veta))


def kniha_navazuje(veta: str) -> bool:
    """HANS_BOOK_FOLLOWUP_DATIVE_V1 — navazuje veta chvili po doporuceni?"""
    import time as _t
    h = _handler_bez_initu()
    h._kniha_posledni = {"x": _t.time()}
    return h._kniha_navazuje(veta, "x", _t.time())


def self_state_vidi(videt) -> bool:
    """HANS_SELF_STATE_ASKER_VISIBLE_V1 — rika blok o sobe, ze tazatele nevidi?"""
    from scripts.hans_recall import self_state_facts
    return "nevid\u00edm" in self_state_facts("data/hans_diary.db", runtime={"asker_visible": videt})


def dny_pocasi(veta: str) -> str:
    """HANS_WEATHER_DAYS_V1 — dny z vety pri pevnem dnesku (utery 15. 9. 2026)."""
    from datetime import datetime
    from scripts.hans_agent import _dny_z_vety
    return ",".join(d.isoformat() for d in _dny_z_vety(veta, datetime(2026, 9, 15, 10, 0)))


def proc_tema(veta: str) -> str:
    """HANS_STUDY_WHY_TOPIC_V1 — tema studia z "proc zrovna X" (cte ostry denik)."""
    from scripts.chat_commands import _puvod_tema_z_proc
    return _puvod_tema_z_proc(veta, "data/hans_diary.db")


def relax_sum(query: str, relaxovano: bool) -> str:
    """HANS_KNOWLEDGE_RELAX_NOISE_V1 — tituly, ktere filtr ponecha (umele radky)."""
    from scripts.hans_convindex import _bez_sumu_relaxace
    rows = [
        (0, "web_read", "", "Stopařův průvodce po Galaxii",
         "Zásadní jest informace o výpočtu odpovědi na základní otázku Života, "
         "Vesmíru a vůbec, která dle Hlubiny Myšlení zněla 42."),
        (0, "book_read", "", "Já robot — kap. 94",
         "Průzor už nebyl vyplněn modří oblohy. Odpověď na tu otázku leží ve vesmíru."),
    ]
    return "|".join(r[3] for r in _bez_sumu_relaxace(query, rows, relaxovano))


# ── HANS_SOURCE_READING_URL_V1 (22. 9.) ────────────────────────────────────
# Sama `_zdroj_z_cetby` cte ZIVY denik, takze se jako regresni pripad nehodi
# (bylo by to datovy pripad jako `korpus_kotva_balast` — pri rotaci okna by
# zhasl). Testuje se proto DETERMINISTICKA cast: odfiltrovani slov, kterymi
# se otazka na zdroj PTA. Prave ta chybela a "odkud cerpas informace
# o pocasi?" se trefilo do titulu "Pravo na informace".
def zdroj_slova(veta: str) -> str:
    """Významová slova dotazu na zdroj, bez rámcových — seřazená, čárkou."""
    from scripts.hans_recall import _zdroj_slova
    return ",".join(sorted(_zdroj_slova(veta, minlen=4, bez_ramce=True)))


# ── HANS_FILM_OPINION_PRIVACY_V1 (23. 9.) ────────────────────────────────────
# Docasna DB se dvema nazory na tyz film: jeden jmenuje clena domacnosti
# (jmeno se bere za behu z configu, v datech sady zadne neni), druhy ne.
def film_nazor_soukromi(cesta: str, tazatel: str) -> str:
    """cesta: 'znalost' (film_knowledge_answer) | 'obliba' (films_liked_answer)
    tazatel: 'cizi' | 'znamy'. Vrati 'se_jmenem' / 'bez_jmena' / 'nic'."""
    import os, sqlite3, tempfile, time as _t
    from scripts import hans_recall
    kp = _cfg().get("known_persons") or {}
    if not kp:
        return "chybi_known_persons"
    klic, rec = next(iter(kp.items()))
    jm = str((rec or {}).get("nom") or klic)
    znamy = jm
    d = tempfile.mkdtemp()
    db = os.path.join(d, "t.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, event_type TEXT,"
              " title TEXT, data TEXT, note TEXT, importance INTEGER,"
              " provenance TEXT, source_url TEXT)")
    now = _t.time()
    c.execute("INSERT INTO diary (ts,event_type,title,data) VALUES (?,?,?,?)",
              (now - 10, "movie_opinion", "Regresni Zkusebni Snimek",
               "Tento film jsem sledoval s %s a bylo to prijemne odpoledne." % jm))
    c.execute("INSERT INTO diary (ts,event_type,title,data) VALUES (?,?,?,?)",
              (now - 20, "movie_opinion", "Regresni Zkusebni Snimek",
               "Kamera pracuje se svetlem velmi citlive a pribeh ma dobre tempo."))
    c.commit(); c.close()
    try:
        if cesta == "obliba":
            r = hans_recall.films_liked_answer(db) or ""
        else:
            r = hans_recall.film_knowledge_answer(
                db, "co vite o filmu Regresni Zkusebni Snimek?",
                asker=("Neznamy Host" if tazatel == "cizi" else znamy)) or ""
    finally:
        try:
            os.remove(db); os.rmdir(d)
        except Exception:
            pass
    if not r:
        return "nic"
    return "se_jmenem" if jm.lower() in r.lower() else "bez_jmena"


# ── HANS_FILM_ASKER_PREFIX_V1 (23. 9.) ───────────────────────────────────────
def film_prefix_kolize(veta: str) -> str:
    """Docasna DB s filmem „Zkouska“ (jako v produkci). Vrati titul, ktery
    film_knowledge_answer trefil, nebo '' — jmeno tazatele nesmi byt titul."""
    import os, re as _re, sqlite3, tempfile, time as _t
    from scripts import hans_recall
    d = tempfile.mkdtemp(); db = os.path.join(d, "t.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE diary (id INTEGER PRIMARY KEY, ts REAL, event_type TEXT,"
              " title TEXT, data TEXT, note TEXT, importance INTEGER,"
              " provenance TEXT, source_url TEXT)")
    for tit in ("Zkouška", "Doctor Strange"):
        c.execute("INSERT INTO diary (ts,event_type,title,data) VALUES (?,?,?,?)",
                  (_t.time(), "movie_opinion", tit, "Zajimavy film s dobrou kamerou a pribehem."))
    c.commit(); c.close()
    try:
        from scripts.openwebui_direct_handler import OpenWebUIDirectHandler as _H
        r = hans_recall.film_knowledge_answer(db, _H._ASKER_PFX.sub("", veta)) or ""
    finally:
        try: os.remove(db); os.rmdir(d)
        except Exception: pass
    m = _re.search(r"\u201e([^\u201c]+)\u201c", r)
    return m.group(1) if m else ""



# ── HANS_CAMERA_STRANGER_V1 (23. 9.) ─────────────────────────────────────────
def kamera_cizimu(cesta: str, tazatel: str) -> str:
    """Rekne Hans NEZNAMEMU, co vidi kamerou? Vrati 'odmita' / 'rika'.
    cesta: 'prompt' (surroundings_db, prazdna mistnost + predmety)
           'agent'  (report_who_is_home)"""
    import types
    cfg = _cfg()
    kp = cfg.get("known_persons") or {}
    znamy = next(iter(kp), "") if kp else ""
    jm = "Neznamy Host" if tazatel == "cizi" else znamy
    if cesta == "prompt":
        from scripts.surroundings_db import SurroundingsDB
        s = SurroundingsDB(cfg)
        s.get_recent_objects = lambda max_age_s=0: [
            {"class_name": "chair", "seen_count": 3}]
        s.get_persons = lambda: []
        t = s.build_llm_context(visible_persons=[], pan_angle=0.0,
                                asker_known=(tazatel != "cizi"))
        s.close()
        rika = ("Nikdo neni" in t) or ("V místnosti vidím" in t)
        return "rika" if rika else "odmita"
    from scripts import hans_agent
    h = types.SimpleNamespace(config=cfg,
                              _agent_inst=types.SimpleNamespace(_raw_name=jm),
                              _hans_idle=types.SimpleNamespace(_present_names=[]))
    r = hans_agent._run_who_home(h, {}) or ""
    return "odmita" if "sděluji jen" in r else "rika"


def film_nabizi_kameru(tazatel: str) -> bool:
    """HANS_CAMERA_STRANGER_V1 — nabidne /film u holeho 'videl' kameru?"""
    from scripts.chat_commands import _cmd_film
    kp = _cfg().get("known_persons") or {}
    jm = "Neznamy Host" if tazatel == "cizi" else (next(iter(kp), "") if kp else "")
    return "kamerou" in (_cmd_film(None, jm, "co jste dnes viděl?") or "")


# ── HANS_RELAX_BOOK_TITLE_V1 (23. 9.) ────────────────────────────────────────
def relax_kniha(dotaz: str) -> int:
    """Kolik kapitol projde relaxacnim filtrem? Umele radky (ts, zdroj,
    partner, titul, text) — nezavisle na zivem indexu."""
    from scripts.hans_convindex import _bez_sumu_relaxace
    rows = [(1.0, "book_read", "", "Le Guinova Ursula – Zememori 1 – Carodej Zememori — kap. 51",
             "Ged sel k mori a premyslel o svem stinu."),
            (2.0, "book_read", "", "Já robot – Asimov Isaac — kap. 94",
             "Odpoved na otazku vesmiru byla ztracena.")]
    return len(_bez_sumu_relaxace(dotaz, rows, True))


# ── HANS_RELAX_BOOK_ANCHOR_V1 (23. 9.) ───────────────────────────────────────
def kniha_kotva(dotaz: str, slovo: str) -> bool:
    """Drzi relaxacni zebrik `slovo` ve VSECH stupnich? (bez indexu)"""
    from scripts.hans_convindex import relax_attempts
    kroky, _ = relax_attempts(dotaz)
    return bool(kroky) and all(slovo in k for k in kroky)


# ── HANS_FILM_DAY_RANGE_V1 / HANS_BOOK_ORIGIN_V1 (23. 9.) ────────────────────
def film_okno_prazdne(dotaz: str) -> str:
    """Odpoved `films_watched_answer` nad PRAZDNYM denikem (docasna DB)."""
    import os, sqlite3, tempfile
    from scripts.hans_recall import films_watched_answer
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        c = sqlite3.connect(p)
        c.execute("CREATE TABLE diary (ts REAL, event_type TEXT, title TEXT)")
        c.commit(); c.close()
        return films_watched_answer(p, dotaz)
    finally:
        os.unlink(p)


def puvod_knihy(veta: str) -> bool:
    """Sepne vzor na ZISKANI veci (`/zdroje` → puvod knihy)?"""
    from scripts.chat_commands import _PUVOD_KNIHY_PAT
    return bool(_PUVOD_KNIHY_PAT.search(veta or ""))


# ── HANS_BOOK_PROGRESS_ANSWER_V1 / HANS_AGENT_SOCIAL_GUARD_THREAD_V1 (23. 9.) ─
def prubeh_cteni(veta: str) -> bool:
    """Sepne vzor na PRUBEH cteni (surovy i bez diakritiky)?"""
    from scripts.chat_commands import _PRUBEH_CTENI_PAT, _fold_diacritics
    return bool(_PRUBEH_CTENI_PAT.search(veta or "")
                or _PRUBEH_CTENI_PAT.search(_fold_diacritics(veta or "")))


def prubeh_jmenuje(veta: str) -> bool:
    """Nese otazka o prubehu cteni jmeno knihy (mimo ramec dotazu)?"""
    from scripts.hans_recall import _PRUBEH_OBSAHOVA_SLOVA
    return _PRUBEH_OBSAHOVA_SLOVA(veta)


def kolac_navazuje(veta: str, predchozi: str) -> bool:
    """Neplati SOCIAL_GUARD pro report_kolac_status po dane predchozi replice?"""
    from scripts.config_io import load
    from scripts.hans_agent import AgentRouter

    class _St:
        def get_history(self, n):
            return [{"role": "assistant", "content": predchozi}]
        get_history_scoped = lambda self, n, c: self.get_history(n)

    class _H:
        conv_store = _St()
    ar = AgentRouter.__new__(AgentRouter)
    ar.config = load()
    ar._speaker = "test"
    return ar._navazuje_na_kolace("report_kolac_status", veta, _H())


# ── HANS_MOOD_CAMERA_STRANGER_V1 + spol. (24. 9.) ───────────────────────────
def _tazatel_jmeno(tazatel: str) -> str:
    kp = _cfg().get("known_persons") or {}
    return "Neznamy Host" if tazatel == "cizi" else (next(iter(kp), "") if kp else "")


def nalada_cizimu(duvod: str, tazatel: str) -> str:
    """Dostane se duvod nalady (a samota) do promptu? 'rika' / 'mlci'."""
    import time as _t
    from scripts.hans_mood import HansMood
    m = HansMood(_cfg())
    m._state.shift_reason = duvod
    m._state.alone_since = _t.time() - 5 * 3600
    t = m.get_prompt_addition(_tazatel_jmeno(tazatel),
                              asker_cizi=(tazatel == "cizi"))
    return "rika" if ("konkrétní důvod" in t or "Jsi sám" in t) else "mlci"


def kritika_cizimu(tazatel: str) -> str:
    """HANS_STRANGER_NO_INSPECT_V1 — pusti /kritika (LLM cesta) k tazateli?"""
    from scripts import chat_commands as cc
    return "odmita" if cc._cizi_nesmi("kritika", "", _tazatel_jmeno(tazatel)) else "rika"


def souhrn_deniku_cizimu(tazatel: str) -> str:
    """HANS_CHAT_SUMMARY_STRANGER_V1 — prida /rozhovory zapisy dne?"""
    from scripts import hans_recall as hr
    puvodni = hr._day_notes
    hr._day_notes = lambda conn, lo, hi, limit=4: ["– 1. ledna: zapis"]
    try:
        t = hr.chat_summary("data/hans_diary.db", _tazatel_jmeno(tazatel),
                            "o čem jsme mluvili 1. ledna 2026?", config=_cfg())
    finally:
        hr._day_notes = puvodni
    return "rika" if "Zapsal jsem si tehdy" in t else "mlci"


# ── HANS_MOOD_HIDDEN_NEUTRAL_V1 + HANS_PLACE_STRANGER_V1 (24. 9.) ───────────
def nalada_zakladni(duvod: str, tazatel: str) -> str:
    """Jaka nalada jde do promptu pri 'worried' s danym duvodem? 'worried'/'content'."""
    from scripts.hans_mood import HansMood, MOOD_PROMPTS
    m = HansMood(_cfg())
    m._state.mood = "worried"
    m._state.shift_reason = duvod
    t = m.get_prompt_addition(_tazatel_jmeno(tazatel), asker_cizi=(tazatel == "cizi"))
    return "worried" if t.startswith(MOOD_PROMPTS["worried"]) else "content"


def misto_cizimu(tazatel: str) -> str:
    """HANS_PLACE_STRANGER_V1 — pusti holy /misto k tazateli?"""
    from scripts import chat_commands as cc
    return "odmita" if cc._cizi_nesmi("misto", "", _tazatel_jmeno(tazatel)) else "rika"


# ── HANS_KODI_SEEN_BEFORE_V1 (24. 9.) ────────────────────────────────────────
def videno_pred(sezeni: list, stari_dni: float) -> str:
    """Umela historie: sezeni = [(pred_kolika_dny_start, minut)] filmu 'X'.
    Vrati 'hlasi' / 'mlci' pro spusteni tehoz filmu ted."""
    import sqlite3 as _s, time as _t
    from scripts.kodi_monitor import naposledy_videno
    c = _s.connect(":memory:")
    c.execute("CREATE TABLE kodi_sessions (media_type TEXT, title TEXT, "
              "started_at REAL, updated_at REAL)")
    now = _t.time()
    for dni, minut in sezeni:
        s = now - float(dni) * 86400
        c.execute("INSERT INTO kodi_sessions VALUES ('movie','X',?,?)",
                  (s, s + float(minut) * 60))
    return "hlasi" if naposledy_videno(c, "X", now, max_days=stari_dni) else "mlci"
