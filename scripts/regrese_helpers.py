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
