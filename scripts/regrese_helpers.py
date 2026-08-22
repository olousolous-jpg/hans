"""regrese_helpers.py — tenké obálky pro regresní sadu.

Některé kontroly potřebují METODU instance (např. `AgentRouter._hint_match`),
ale sada volá funkce podle jména „modul.funkce". Aby v datovém souboru
nemusela být logika, bydlí ta obálka tady — a NIC nepočítá sama, jen
zavolá skutečný kód, aby se testovalo to, co běží v provozu.
"""
from __future__ import annotations

import json
from functools import lru_cache


@lru_cache(maxsize=1)
def _router():
    from scripts.hans_agent import AgentRouter
    with open("config.json", encoding="utf-8") as f:
        return AgentRouter(json.load(f))


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
            with open("config.json", encoding="utf-8") as f:
                self.config = json.load(f)

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
    `except` spolkl, přišla o výsledky celá RAG kolekce. Vrací prázdný
    řetězec, když je soubor čistý; jinak „jméno:řádek".
    """
    import re
    src = open(soubor, encoding="utf-8").read()
    radky = src.splitlines()
    nalezy = []
    for a in set(re.findall(r"^    (_[A-Z][A-Z0-9_]*)\s*=", src, re.M)):
        for m in re.finditer(r"(?<![.\w])%s\b" % a, src):
            i = src[:m.start()].count("\n")
            radek = radky[i]
            if re.match(r"^    %s\s*=" % a, radek) or radek.lstrip().startswith("#"):
                continue
            nalezy.append("%s:%d" % (a, i + 1))
    return ", ".join(sorted(nalezy))


def dedup_osloveni(text: str, jmeno: str) -> str:
    """G4D_ADDRESS_KNOWN_VOCATIVE_V1 — dedup oslovení s reálným configem.

    Doloženo 22.8.: dřív se mazalo jakékoli slovo končící na -o/-e před
    čárkou („nesedělo", „toho", „ticho"), protože regex měl IGNORECASE.
    """
    import json
    from scripts.conversation_store import dedup_address_g4d
    with open("config.json", encoding="utf-8") as fh:
        cfg = json.load(fh)
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
