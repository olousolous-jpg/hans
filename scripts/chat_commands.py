#!/usr/bin/env python3
"""Chat commands — slash (/denik) i natural language ("připrav deník").

Použití:
    from scripts.chat_commands import parse_command, dispatch

    cmd = parse_command("/denik")               # ("denik", "")
    cmd = parse_command("Hansi, připrav deník")  # ("denik", "")
    cmd = parse_command("zapomeň naši historii") # ("zapomen", "")
    cmd = parse_command("ahoj")                  # None

    if cmd:
        reply = dispatch(cmd, handler, name=user_name)
"""
from __future__ import annotations

import logging
import re
import threading
# HANS_CHAT_IMPORT_TIME_V1 (4.9.) — `time` v modulovém scope CHYBĚL, ačkoli ho
# tělo souboru používá. Ostrý běh /hledani spadl na „name 'time' is not
# defined“ a prověrka ukázala, že tatáž mina leží i v `_cmd_rozvrh` (ř. 663
# volá time.time(), funkce žádný import nemá — `import time` níž je v JINÉ
# funkci, `_cmd_interest`, a lokální import se ven nepropíše).
# ⚠️ Offline testy to minuly: bez přihlašovacích údajů se běh k té větvi
# nedostal a vracel se dřív. Chytil to až běh v ŽIVÉM Hansovi.
# Lokální `import time as _t` na jiných místech tím nejsou dotčené.
import time
from typing import Callable, Optional

_log = logging.getLogger("chat_commands")


def _current_channel() -> Optional[str]:
    """HANS_CHAT_CHANNEL_AWARE_V1 — čte aktuální kanál (web/telegram/voice/
    popup) nastavený send_chat_message v thread-local. None = mimo chat
    vlákno (dispatch → celá historie, zpětná kompat)."""
    try:
        from scripts.openwebui_direct_handler import get_current_channel
        return get_current_channel()
    except Exception:
        return None


# CHAT_COMMANDS_MARKER

# HANS_CAP_SUMMARY_V1 — původ routingu (slash × NL × LLM) v thread-local.
# Slash a LLM-route vracejí OBĚ prázdné args → z args samotných je nerozliším.
# Původ ale rozhoduje, jestli /schopnosti dá plný výpis (slash) nebo vřelé
# shrnutí (přirozený dotaz). Stejný vzor jako _current_channel výše.
_route_tls = threading.local()


def _set_route_origin(origin: Optional[str]) -> None:
    _route_tls.origin = origin


def _route_origin() -> Optional[str]:
    return getattr(_route_tls, "origin", None)


# HANS_VIDEL_KOHO_V1 (9. 9.) — SUROVA VETA k prikazu. LLM router predava args
# zamerne PRAZDNE (fail-closed proti mutujicim podprikazum), takze prikaz uz
# nepozna, NA CO se clovek ptal. `/videl` to dosud resil tim, ze spadl na
# tazatele — u otazky „KOHO jste videl" je to vzdy spatne, protoze ta zadny
# podmet nema.
# ⚠️ Zamerne na TEMZE `threading.local` jako `origin`, NE na instanci handleru
# ani routeru: 4. 9. (HANS_WEATHER_RAW_MSG_FIX_V1) cetlo `_run_weather` surovou
# vetu z HANDLERU, jenze ta zila na instanci ROUTERU — zaloha byla v produkci
# MRTVA a nikdo si toho nevsiml, protoze staticky to vypadalo spravne.
def _set_route_msg(msg: Optional[str]) -> None:
    _route_tls.msg = msg


def _route_msg() -> str:
    return getattr(_route_tls, "msg", "") or ""


# ── Registr commands ───────────────────────────────────────────────────

_COMMANDS: dict[str, dict] = {}


def register(command_id: str, *,
             slash_aliases: list[str],
             nl_patterns: list[str],
             handler: Callable,
             help_text: str = ""):
    """Zaregistruj command. slash_aliases: ['denik','reflexe'] - matche /denik /reflexe.
    nl_patterns: regex patterny (case-insensitive) pro natural language."""
    _COMMANDS[command_id] = {
        "slash":    [s.lower().lstrip("/") for s in slash_aliases],
        "nl":       [re.compile(p, re.IGNORECASE) for p in nl_patterns],
        # NL bez diakritiky — uživatelé často píšou „kalendar"/„udalosti".
        # Fold i vzor i vstup → matchne s háčky i bez nich.
        "nl_fold":  [re.compile(_fold_diacritics(p), re.IGNORECASE)
                     for p in nl_patterns],
        "handler":  handler,
        "help":     help_text,
    }


def _fold_diacritics(s: str) -> str:
    """Odstraní diakritiku (á→a, ř→r, ž→z…). Bezpečné i pro regex vzory
    (mění jen písmena, ne strukturu)."""
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


# ── Parser ─────────────────────────────────────────────────────────────

_HYPOTETICKY = re.compile(r"\bkdyb\w*\b", re.IGNORECASE)


def parse_command(message: str) -> Optional[tuple[str, str]]:
    """Pokus se rozpoznat command. Vrátí (command_id, args) nebo None.
    Slash má prioritu. NL detekce běží jen pokud message nezačíná /."""
    msg = message.strip()
    _set_route_origin(None)  # HANS_CAP_SUMMARY_V1 — nezdědit původ z minula
    _set_route_msg(msg)      # HANS_VIDEL_KOHO_V1 — a ani větu z minula
    if not msg:
        return None

    # Slash commands
    if msg.startswith("/"):
        parts = msg[1:].split(maxsplit=1)
        if not parts:
            return None
        slash_name = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        for cmd_id, spec in _COMMANDS.items():
            if slash_name in spec["slash"]:
                _set_route_origin("slash")
                return (cmd_id, args)
        return None  # neznámý slash → ne-command

    # HANS_BARE_ALIAS_V1 (28.8.) — HOLÝ NÁZEV PŘÍKAZU BEZ LOMÍTKA.
    # Nález uživatele: „preloz" spustilo MALOVÁNÍ. Vzory pro /preloz čekaly za
    # slovem ještě „to"/„ten", takže holý tvar propadl do volného hovoru a co
    # se stalo místo toho, určil kontext vlákna (zbytek rozmluvy o Troskách).
    # ⚠️ Audit 28.8. ukázal, že tuhle díru mělo 53 z 56 příkazů. Ruční vzor ke
    # každému je ale ŠPATNÁ oprava: většina aliasů jsou buď anglické
    # identifikátory, které česky nikdo nenapíše, nebo naopak běžná slova
    # („film", „stop", „dnes", „nápad", „seznam") — a ta by pak unášela normální
    # hovor, což je horší porucha než ta původní.
    # Proto JEDNO pravidlo: zpráva, která je CELÁ jen názvem příkazu, je ten
    # příkaz. Kotvy na začátek i konec drží riziko nízko — aby se to spustilo,
    # musí uživatel napsat to slovo a nic jiného, což je fakticky povel.
    # Nové příkazy tím dostanou totéž chování samy, bez dalšího vzoru.
    # ⛔ Tyhle NE. Holý tvar smí spustit jen to, co se dá vzít zpět.
    # `/vypnipc` volá `systemctl poweroff` BEZ POTVRZENÍ (ověřeno 28.8. čtením
    # obsluhy — potvrzovací krok je v agentní cestě, ne tady), `zapomen` sahá
    # na paměť. Destruktivní byly i předtím, ale přes lomítko; sundat jim ho
    # kvůli pohodlí by bylo špatně. U nich zůstává `/prikaz` povinné.
    _BEZ_HOLEHO_TVARU = {"vypnipc", "zapomen", "enroll", "herni", "sleep", "experiment"}
    holy = _fold_diacritics(msg.lower()).strip().rstrip("!?.,").strip()
    if holy and " " not in holy and holy not in _BEZ_HOLEHO_TVARU:
        for cmd_id, spec in _COMMANDS.items():
            if holy in [_fold_diacritics(a) for a in spec["slash"]]:
                _set_route_origin("bare")
                return (cmd_id, "")

    # HANS_NL_ROUTE_HYPOTHETICAL_V1 (26.8.) — DLOUHÁ HYPOTETICKÁ VĚTA NENÍ POVEL.
    # Doloženo: „kdybys měl namalovat obraz, který by vystihoval dnešní den…"
    # spustilo SKUTEČNÉ malování; „kdyby ses měl rozhodnout, jestli pamatovat,
    # nebo zapomínat" skončilo výpisem studijního programu.
    # Regexové NL vzory totiž nemají žádnou délkovou brzdu, kdežto LLM router
    # ano (`_LLM_ROUTE_MAX_WORDS`, „delší věta = vyprávění, ne žádost o výpis").
    # Tady se týž princip uplatní i na ně — ale JEN v kombinaci s „kdyby":
    # krátká zdvořilá žádost („kdybys mohl namalovat kočku") projít MUSÍ.
    # Slash je nedotčený: explicitní příkaz je vždycky příkaz.
    if (_HYPOTETICKY.search(msg)
            and len(msg.split()) > _LLM_ROUTE_MAX_WORDS):
        _log.debug("NL routing přeskočen: dlouhá hypotetická věta")
        return None

    # Natural language (diakritika i bez ní)
    msg_fold = _fold_diacritics(msg)
    for cmd_id, spec in _COMMANDS.items():
        for pat in spec["nl"]:
            if pat.search(msg):
                _set_route_origin("nl")
                return (_cetl_nebo_zpravy(cmd_id, msg_fold), msg)
        for pat in spec.get("nl_fold", []):
            if pat.search(msg_fold):
                _set_route_origin("nl")
                return (_cetl_nebo_zpravy(cmd_id, msg_fold), msg)
    return None


# HANS_CETL_ZPRAVY_V1 (6. 10.) — „četl jsi dnes nějaké zprávy?“ sedne na vzor
# četby dřív než na vzor zpráv (pořadí registrace) → výpis četby místo zpráv
# (/tazatel 6. 10.). Předmětem čtení jsou zprávy/noviny v téže části věty →
# příkaz zpráv. Na 1 920 větách změní 1 (tu z tazatele).
_CETL_ZPRAVY = re.compile(
    r"\b(?:pre)?c(?:etl\w*|tes|tete)\b[^.?!,]{0,30}\b(?:zpravy|noviny|zpravodajstvi)\b",
    re.IGNORECASE)


def _cetl_nebo_zpravy(cmd_id: str, msg_fold: str) -> str:
    if cmd_id == "cetl" and "zpravy" in _COMMANDS and _CETL_ZPRAVY.search(msg_fold or ""):
        _log.info("HANS_CETL_ZPRAVY_V1: čtení zpráv → /zpravy místo /cetl")
        return "zpravy"
    return cmd_id


# ── Dispatcher ─────────────────────────────────────────────────────────

def _oslov_dle_tazatele(text, handler, name):
    """HANS_CMD_ADDRESSEE_V1 (5.9.) — SROVNEJ OSLOVENÍ I U PŘÍKAZOVÝCH ODPOVĚDÍ.

    `cz_names.fix_addressee` (a v ní `HANS_ADDRESSEE_ZENA_PANE_V1` ze 4. 9.)
    slibuje v docstringu, že běží „po KAŽDÉ odpovědi". NEBĚŽÍ: volá se až na
    konci `send_chat_message`, a PŘED tím řádkem je v té metodě **16 returnů**;
    Matrix navíc volá `dispatch` napřímo (`bridge_commands`), takže se do té
    metody vůbec nedostane [[two-command-paths-agent-vs-bridge]].
    Doloženo živě 5. 9.: ŽENA → `/verify` → „Nemám co ověřovat, PANE."
    V `chat_commands.py` je **46 příkazů** se šablonou obsahující „pane".

    ⛔ NEPŘEPISOVAT ty šablony po jedné — 4. 9. bylo výslovně rozhodnuto, že
    jedno místo je levnější, robustnější a pokryje i texty z modelu. Tohle to
    rozhodnutí NEruší, naopak ho konečně platí i tady.

    Proč zrovna `dispatch`, a ne obal kolem `send_chat_message`: teče přes něj
    web i Matrix, a hlavně je to **PŘED zápisem** do `conv_store` a deníku.
    Obal až nad návratem by opravil, co uživatel vidí, ale do historie by se
    uložil původní tvar — a ta historie je modelu few-shotem
    [[conv-history-is-few-shot]], takže by se „pane" ženě učil dál.

    Bezpečnost: `fix_addressee` sahá jen na OSLOVOVACÍ pozici, takže výpisy
    typu „jméno: zájem" (1. pád na začátku řádku) zůstávají — ověřeno
    regresí. Je idempotentní, takže druhé proběhnutí v ocasu nic nezmění.
    """
    if not text or not isinstance(text, str) or not name:
        return text
    try:
        from scripts.cz_names import fix_addressee
        out, _n = fix_addressee(text, name, getattr(handler, "config", None))
        return out
    except Exception as e:
        _log.debug("HANS_CMD_ADDRESSEE_V1 preskocen: %s", e)
        return text


# HANS_STRANGER_NO_MUTATE_V1 (24. 9.) — cizí nesmí měnit Hansovo chování ani
# stav (pokyn uživatele: „cizí nemá mít možnost upravovat chování Hanse").
# Jedno místo pro web i Matrix. Dvě skupiny:
#  - VŽDY jen známým: příkazy, které samy o sobě mění stav, spouštějí práci
#    nebo zasahují do domu;
#  - PODLE ARGUMENTU: holý tvar je jen výpis, mutuje až podpříkaz. Hlídá se
#    jen u slash/holého tvaru; NL předává celou větu a obsluhy podpříkazy
#    porovnávají přesně, LLM router předává args prázdné (fail-closed).
# Prázdné jméno se za známé NEpočítá.
_JEN_ZNAMYM = frozenset({
    "zapis", "work", "denik", "dialog", "zaptej", "enroll", "sleep", "herni",
    "severka", "hlidej", "preloz", "vypnipc", "vpnprepni", "router",
    "experiment", "stop", "pauza", "hledani", "nalez", "brief", "vytvor",
    "zrusmalbu",                                  # HANS_PAINT_CANCEL_V1
    "sleva",                                      # HANS_LETAKY_V1 — seznam nákupů domácnosti
    # HANS_STRANGER_NO_INSPECT_V1 (24. 9.) — sebekritika vznika z rozhovoru
    # s domacnosti a nese jejich jmena (i v 7. pade, ktery privacy vzor
    # nom/acc/voc nechyti). Doloženo: LLM router ji poslal cizimu.
    "kritika",
    # HANS_PLACE_STRANGER_V1 (24. 9., pokyn uzivatele) — rozlozeni domu cizimu
    # ne; VEDOMA ZMENA: driv byl holy vypis /misto pro cizi otevreny.
    "misto",
    # HANS_STRANGER_INSIGHTS_V1 (24. 9.) — vhledy nesou jmena domacnosti
    # a pocty zaznamu z kamery, nitky tema rozhovoru podle osob. Doloženo
    # tazatelem: LLM router poslal cizimu /vhledy. Audit vsech 14 ctecich
    # prikazu otevrenych cizim: ostatni ciste.
    "vhledy", "nitky",
})
_JEN_ZNAMYM_CTENI = frozenset({"kritika", "misto", "vhledy", "nitky", "sleva"})  # HANS_STRANGER_READ_MSG_V1
_CTENI_BEZ_ARG = {  # příkaz → argumenty, které jsou jen výpis
    "seznam": (), "kalendar": (), "studium": ("programy",),
    "dilo": ("vse",), "napad": ("vse",), "dashboard": (),
    "avatar": ("stav",), "zdravi": (), "nastroj": (), "prohloubit": (),
    "anomalie": (), "interest": (),
}


_BEZ_SVOLENI = "K tomu ode mne nemáte svolení. Snad mi to prominete."  # HANS_PRAVA_V1


# HANS_STRANGER_HOUSEHOLD_V1 (30. 9., pokyn uživatele: „cizí by z chodu
# domácnosti neměl dostávat informace“) — čtecí příkazy o DOMĚ, ne o Hansovi.
# Doloženo auditem 30. 9. (26 příkazů pod testovací personou): cizí dostal
# nákupní seznam, co běží na TV (a nabídku pustit film) a stav techniky
# v domě (PC, Kodi, restarty, čidla). Hansovo vlastní (četba, studium,
# dílo, sny, obrazy, zájmy, nápady) zůstává otevřené — rozhodnutí uživatele.
# Na rozdíl od `_JEN_ZNAMYM` se týká JEN cizího: známý bez práva „akce“
# seznam dál přečte (mutace seznamu hlídá větev níž jako dosud).
_CHOD_DOMACNOSTI = frozenset({"hraje", "seznam", "zdravi"})


def _cizi_nesmi(cmd_id: str, args, name) -> str:
    """Vrátí odmítnutí pro cizího u mutujícího příkazu, jinak ''."""
    if cmd_id in _CHOD_DOMACNOSTI:
        try:
            from scripts.cz_names import is_known_person as _ikp_d
            _zn = bool(name) and _ikp_d(name)
        except Exception:
            _zn = False
        if not _zn:
            _log.info("HANS_STRANGER_HOUSEHOLD_V1: %s od neznámého (%s) odmítnuto",
                      cmd_id, name)
            return "O tom mluvím jen se svou domácností."
    if cmd_id in _JEN_ZNAMYM:
        pass
    elif cmd_id == "smer":
        # `/smer` mění i z NL: oznamovací věta se stane směrem
        # (_smer_is_custom) — brána rozhoduje stejně jako obsluha.
        a = str(args or "").strip()
        if not a or a.lower() in ("stav", "status"):
            return ""
        if _route_origin() not in ("slash", "bare") and not _smer_is_custom(a):
            return ""
    elif cmd_id in _CTENI_BEZ_ARG and _route_origin() in ("slash", "bare"):
        a = _fold_diacritics(str(args or "")).strip().lower()
        if not a or a.split()[0] in _CTENI_BEZ_ARG[cmd_id]:
            return ""
    else:
        return ""
    try:
        from scripts.cz_names import is_known_person as _ikp
        if name and _ikp(name):
            # HANS_PRAVA_V1 (27. 9.) — známému bez oprávnění k akcím
            # (čtecí příkazy mají vlastní kategorie, sem nepatří).
            if cmd_id not in _JEN_ZNAMYM_CTENI:
                from scripts.cz_names import _load_config as _lc
                from scripts.hans_prava import muze as _pm
                if not _pm(_lc(), name, "akce"):
                    return _BEZ_SVOLENI
            return ""
    except Exception:
        pass
    _log.info("HANS_STRANGER_NO_MUTATE_V1: %s od neznámého (%s) odmítnuto",
              cmd_id, name)
    # HANS_STRANGER_READ_MSG_V1 (24. 9.) — u příkazů, které nic nemění, je to
    # otázka, ne úkon: „udělat“ by na „kde jsi?“ znělo nesmyslně.
    if cmd_id in _JEN_ZNAMYM_CTENI:
        return "O tom mluvím jen se svou domácností."
    return "Tohle mohu udělat jen pro svou domácnost."


# HANS_UNKNOWN_SLASH_V1 (9. 10.) — NEZNÁMÝ PŘÍKAZ S LOMÍTKEM.
# `parse_command` na něj vrací None a zpráva propadla do volného hovoru, kde si
# model odpověď vymyslel. Doloženo 9. 10.: „/sleva ted“ (příkaz ještě nebyl
# nasazen) → „…namaloval portrét… Zobrazím ho na nástěnce“. V deníku rozhovorů
# 5 výskytů, z toho 3 překlepy (/studoum, /studiun, /napaf) → nabídnout nejbližší.
# Cesta jako „/home/user/soubor“ příkaz není (za jménem musí být mezera nebo konec).
NEZNAMY_PRIKAZ = "neznamy_prikaz"
_NEZNAMY_SLASH = re.compile(r"^\s*/([^\W\d_][\w-]{1,24})(?=\s|$)")


def neznamy_prikaz(message: str) -> Optional[str]:
    """Odpověď na zprávu začínající neznámým `/slovem`, jinak None."""
    m = _NEZNAMY_SLASH.match(message or "")
    if not m:
        return None
    jm = m.group(1).lower()
    hlavni = {}
    for spec in _COMMANDS.values():
        for a in spec["slash"]:
            hlavni[_fold_diacritics(a)] = spec["slash"][0]
    if _fold_diacritics(jm) in hlavni:
        return None
    import difflib
    blizke = []
    for b in difflib.get_close_matches(_fold_diacritics(jm), list(hlavni), n=3, cutoff=0.72):
        if hlavni[b] not in blizke:
            blizke.append(hlavni[b])
    if blizke:
        return ("Příkaz /%s neznám. Nejblíž je %s. Všechny příkazy ukáže /help; "
                "jinak to stačí říct obyčejnou větou."
                % (jm, " nebo ".join("/" + b for b in blizke[:2])))
    return ("Příkaz /%s neznám. Co umím, ukáže /help; jinak to stačí říct obyčejnou větou."
            % jm)


def dispatch(command: tuple[str, str], handler, name: Optional[str]) -> str:
    """Spustí command. handler = openwebui_direct_handler instance.
    Vrátí text odpovědi pro chat."""
    cmd_id, args = command
    spec = _COMMANDS.get(cmd_id)
    if cmd_id == NEZNAMY_PRIKAZ:                  # HANS_UNKNOWN_SLASH_V1
        _log.info("HANS_UNKNOWN_SLASH_V1: neznámý příkaz (%.40s)", args)
        return neznamy_prikaz(args) or ""
    if not spec:
        return f"⚠ Neznámý příkaz: {cmd_id}"
    _odmitnuti = _cizi_nesmi(cmd_id, args, name)
    if _odmitnuti:
        return _odmitnuti
    try:
        return _oslov_dle_tazatele(
            spec["handler"](handler, name, args), handler, name)
    except Exception as e:
        _log.error("dispatch %s failed: %s", cmd_id, e)
        return f"⚠ Příkaz {cmd_id} selhal: {e}"


def list_commands() -> list[dict]:
    """Vrátí seznam dostupných commands pro /help."""
    return [
        {"id": cid, "slash": spec["slash"][0], "help": spec["help"]}
        for cid, spec in _COMMANDS.items()
    ]


NL_RUNTIME = chr(10)  # G5C: nový řádek jako runtime znak

# ── Command implementations ────────────────────────────────────────────

# G5C_VERIFY_COMMAND_V1 ─────────────────────────────────────────────────
def _g5c_llm(handler, system, user, num_predict=200):
    """Zavolá LLM stejným vzorem jako web_reader._summarize (ollama_chat)."""
    try:
        from scripts.ollama_client import ollama_chat
        cfg = getattr(handler, "config", {}) or {}
        ow = cfg.get("openwebui_chat", {}) or {}
        model = (cfg.get("models", {}).get("utility")
                 or cfg.get("models", {}).get("dialog")
                 or getattr(handler, "model_name", None)
                 or "hans-czech:latest")
        url = ow.get("base_url", "http://127.0.0.1:11434")
        # G5I_VERIFY_DETERMINISTIC_V1 — temperature 0.0: verify musí být
        # reprodukovatelné (extrakce entity i porovnání). Bez ní ollama
        # default ~0.7 → flip-flop na identickém vstupu (R.U.R. apod.).
        out = ollama_chat(
            model,
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            ollama_url=url,
            options={"num_predict": num_predict, "temperature": 0.0},
        )
        return (out or "").strip()
    except Exception as e:
        _log.error("G5C LLM selhal: %s", e)
        return ""


from scripts.chat_cmd_zaklad import _cmd_verify   # ROZDELENI_PRIKAZU_V1 — přesunuto



from scripts.chat_cmd_pamet import _cmd_denik   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_zaklad import _cmd_dialog   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_pamet import _cmd_zapomen   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_zaklad import _cmd_info   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_zaklad import _cmd_help   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_zaklad import _cmd_zaptej   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_zaklad import _cmd_enroll   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ── Registrace ─────────────────────────────────────────────────────────

from scripts.chat_cmd_zaklad import _cmd_ooda   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "ooda",
    slash_aliases=["ooda"],
    # HANS_NL_ROUTE_HYPOTHETICAL_V1 (26.8.) — NL vzory ODEBRÁNY. `/ooda` je
    # interní diagnostika (help sám říká „akci nevykoná") a vzor
    # `\bjak.{0,15}rozhod` se trefil doprostřed věty „jak se vlastně
    # rozhoduješ, čemu se budeš věnovat" → uživateli vypadlo
    # „OODA skóre: movie:2 thought:1 read:4…". Diagnostika patří za slash.
    nl_patterns=[],
    handler=_cmd_ooda,
    help_text="Diagnostika OODA — co by Hans teď vybral (akci nevykoná)",
)

from scripts.chat_cmd_zaklad import _cmd_seznam   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ─── zapsání poznámky — DETERMINISTICKÁ cesta (HANS_NOTE_WRITE_PATH_V1) ─────
# PROČ VZNIKLA (20.8.): „zapiš si, že …" dosud stálo a padalo s agentním
# routerem, a ten je na téhle třídě nespolehlivý — doloženo: na tutéž větu
# vrátil offline `add_note` 12/12, ale živě `report_now_playing` (conf 0.95).
# Když akce nevznikla, odpověď obstaral volný hovor a PROHLÁSIL zápis, který
# se nestal („Zápis v deníku byl aktualizován", `agent_action` = 0 řádků).
#
# ⚠️ ZVAŽOVANÁ ALTERNATIVA ZAMÍTNUTA MĚŘENÍM: post-guard, který takové tvrzení
# odhalí podle slov, by hlásil hlavně plané poplachy — v deníku je 24 vět typu
# „zaznamenal jsem", z toho 18 bez agentní akce, a **většina je legitimní**
# („Zaznamenal jsem přehrávání filmu", „…poslední aktivitu paní domu").
# Jazyk to nerozliší; proto se místo brzdy dělá to tvrzení PRAVDIVÝM.
#
# Rozkaz je jednoznačný, takže se NEPTÁ na potvrzení (agent se ptá proto, že
# HÁDÁ; regex nehádá) a volá TÝŽ kód jako agentní akce — vzor
# HANS_UNIFY_ACTIONS_V1: „regexy zůstávají jako rychlá, na mozku nezávislá
# cesta", jedna pravda o zápisu, ne druhá implementace vedle.
_NOTE_IMP = re.compile(r"\b(zapi[šs]|poznamenej|zaznamenej|pozna[čc])\b\s*", re.I)
# Výplň mezi slovesem a obsahem („si prosím, že …", „hlavně to, že …").
# ⚠️ Hranice slova je nutná: bez ní „sis vymyslel" ztratí „si" a zbude
# „s vymyslel" (chyceno testem, ne až v provozu).
_NOTE_FILLER = re.compile(
    r"^\s*(?:(?:si|prosím|prosim|hlavně|hlavne|to|že|ze)\b|[,:;–-])\s*", re.I)


def _note_text(msg: str):
    """Vytáhne z rozkazu obsah poznámky. None = není co zapsat."""
    m = _NOTE_IMP.search(msg or "")
    if not m:
        return None
    t = msg[m.end():]
    prev = None
    while prev != t:
        prev = t
        t = _NOTE_FILLER.sub("", t)
    t = t.strip(" ,.:;–-")
    return t or None


from scripts.chat_cmd_zaklad import _cmd_zapis   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "zapis",
    slash_aliases=["zapis", "zapiš", "poznamka", "poznámka"],
    nl_patterns=[
        r"\bzapi[šs]\s+si\b", r"\bsi\s+zapi[šs]\b",
        r"\bpoznamenej\s+si\b", r"\bsi\s+poznamenej\b",
        r"\bzaznamenej\s+si\b", r"\bsi\s+zaznamenej\b",
    ],
    handler=_cmd_zapis,
    help_text='Zapsání poznámky: zapiš si, že …',
)


register(
    "seznam",
    slash_aliases=["seznam", "poznamky", "poznámky", "todo"],
    nl_patterns=[
        r"\bco.{0,8}m[áa]m.{0,12}seznam",
        r"\buka[žz].{0,12}seznam",
        r"\bm[ůu]j\s+seznam",
        r"\bseznam.{0,12}pozn[áa]mek",
        r"\bco.{0,8}jsem.{0,8}(si\s+)?poznamenal",
    ],
    handler=_cmd_seznam,
    help_text="Výpis poznámek/úkolů (/seznam, /seznam hotovo N, /seznam smaz N)",
)

from scripts.chat_cmd_dum import _cmd_kalendar   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "kalendar",
    slash_aliases=["kalendar", "kalendář", "kalendar", "calendar"],
    nl_patterns=[
        r"kalend[áa]ř?",
        r"\bco.{0,8}m[áa]m.{0,12}(dnes|z[ií]tra|tento t[ýy]den|tenhle t[ýy]den)",
        r"\bmoje?\s+ud[áa]losti",
        # HANS_CALENDAR_ANCHOR_V1 (16. 9.) — drive holy kmen `napl[áa]novan`,
        # ktery se chytil kdekoli ve vete: „to se zda byt dobre naplanovane“
        # nebo „peclive naplanovan harmonogram byl i v jinych letech“ skoncily
        # vypisem kalendare misto odpovedi. Ukotveno na 1./2. osobu + „neco“
        # (ne na otaznik — ten uzivatele vynechavaji). Koncovka `-v[áa]n` je
        # tu schvalne: `napl[áa]novan` nesedlo na „naplánován/naplánováno“
        # a zachranoval to jen slozeny dvojnik `nl_fold`.
        r"\b(m[áa]m|nem[áa]m|m[áa][šs]|nem[áa][šs]|m[áa]te|nem[áa]te)\b"
        r"[^?.!]{0,12}\bn[ěe]co\b[^?.!]{0,16}\bnapl[áa]nov[áa]n\w*",  # co mám naplánováno / nemám něco naplánovaného
        r"\bschůzk|\bschuzk",
    ],
    handler=_cmd_kalendar,
    help_text="Nadcházející události z Proton kalendáře (/kalendar, /kalendar sync)",
)


from scripts.chat_cmd_dum import _cmd_rozvrh   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "rozvrh",
    slash_aliases=["rozvrh", "schedule"],
    nl_patterns=[
        r"m[ůu]j\s+rozvrh",
        r"tv[ůu]j\s+rozvrh",
        r"hans[ůu]v\s+(rozvrh|kalend[áa]ř)",
        r"tv[ůu]j\s+kalend[áa]ř",   # „tvůj kalendář" = Hansův (ne Proton)
        r"\brutin[yaou]?\b",
        r"co\s+d[ěe]l[áa]š\s+(v\s+noci|automaticky|rutinn[ěe])",
    ],
    handler=_cmd_rozvrh,
    help_text="Můj rozvrh autonomních rutin (kdy naposled tikly, zaostávají-li)",
)

from scripts.chat_cmd_studium import _cmd_work   # ROZDELENI_PRIKAZU_V1 — přesunuto


# HANS_ZAJMY_JEN_TAZATEL_V1 — kdy se výpis zájmů smí týkat CELÉ domácnosti.
# Musí to být řečeno výslovně; jinak dostane tazatel svoje. Vzor je úzký
# schválně — širší by spolkl i „co detektivky, čteš je?" a únik by se vrátil.
_PTA_SE_NA_VSECHNY = re.compile(
    r"\b(v[šs]ichni|v[šs]ech|kdo\s+v[šs]echno|lid[ií]\s+(doma|v\s+dom\w*)|"
    r"cel[áa]\s+dom[áa]cnost|kdo\s+co\s+m[áa]\s+r[áa]d|u\s+n[áa]s\s+doma|"
    r"ka[žz]d[ýy]\b)", re.IGNORECASE)


from scripts.chat_cmd_studium import _cmd_interest   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "interest",
    slash_aliases=["interest", "zajem", "zájem"],
    nl_patterns=[],
    handler=_cmd_interest,
    help_text="Zapíše Hansův zájem do deníku (/interest <téma>) nebo vypíše naučené (/interest)",
)


register(
    "work",
    # HANS_AUTHORSHIP_V1 — „dilo"/„dílo" patří autorskému projektu (_cmd_dilo),
    # ne tomuhle staršímu ad-hoc /work; jinak by ho /work stínil (registrován dřív).
    slash_aliases=["work", "esej"],
    nl_patterns=[
        r"\bnapi\w+.{0,15}esej",
        r"\bnapi\w+.{0,15}pr\u00e1ci",
    ],
    handler=_cmd_work,
    help_text="Hans napíše esej z nastudované četby na zadané téma",
)

register(
    "denik",
    slash_aliases=["denik", "deník", "reflexe", "shrnuti"],
    nl_patterns=[
        r"\bpřiprav.{0,20}den[íi]k",
        r"\bzapis.{0,20}dnes",
        r"\bshrnut[íi].{0,20}dne",
        r"\bzapis(?!ky\s+z\s+den)(?!k[uy]\s+z\s+den).{0,20}den[íi]k",   # HANS_DENIK_NOT_BOOK_V1: ne kniha „Zápisky z deníku …“
        r"\bden[íi]k.{0,20}dnes",
    ],
    handler=_cmd_denik,
    help_text="Spustí večerní reflexi (uloží shrnutí dne do deníku a RAGu)",
)

register(
    "dialog",
    slash_aliases=["dialog", "kolac", "koláč"],
    nl_patterns=[
        r"\bzavolej.{0,20}kol[aá]č",
        r"\bpromluv.{0,20}kol[aá]č",
        r"\bdialog.{0,20}kol[aá]č",
    ],
    handler=_cmd_dialog,
    help_text="Vyvolá rozhovor Hanse s panem Koláčem",
)

register(
    "zapomen",
    slash_aliases=["zapomen", "zapomeň", "vymaz", "reset"],
    nl_patterns=[
        r"\bzapomeň.{0,30}(naš|histor|hovor|rozhov)",
        r"\bvymaž.{0,20}histor",
        r"\bzač[ěn][i]?.{0,20}znovu",
    ],
    handler=_cmd_zapomen,
    help_text="Smaže historii našich hovorů",
)

register(
    "info",
    slash_aliases=["info", "stav"],
    nl_patterns=[
        r"\bjak[ýé].{0,10}\bstav",   # HANS_INFO_STAV_WORD_V1 (4. 10.): ne „jaké POSTAVy“
        r"\bco.{0,5}ví[šs].{0,10}o\s*sob",
    ],
    handler=_cmd_info,
    help_text="Zobrazí aktuální stav (kolik zpráv v paměti atd.)",
)

register(
    "help",
    slash_aliases=["help", "pomoc", "napoveda", "nápověda"],
    nl_patterns=[
        # „co umíš" → schopnosti (capabilities); help drží jen dotaz na příkazy
        r"\bjak[éý].{0,10}p[řr][íi]kaz",
        r"\bseznam\s+p[řr][íi]kaz",
    ],
    handler=_cmd_help,
    help_text="Seznam příkazů",
)
register(
    "zaptej",
    slash_aliases=["zaptej", "otazka", "otázka", "zeptej"],
    nl_patterns=[
        r"\bvyvolej.{0,15}ot[áa]zk",
        r"\bzaptej\s+se",
        r"\bpolož[íi].{0,10}ot[áa]zk",
        r"\bzv[íi]davost",
    ],
    handler=_cmd_zaptej,
    help_text="Hans si položí otázku a hledá odpověď (curiosity)",
)
register(
    "enroll",
    slash_aliases=["enroll", "video_enroll", "trenuj"],
    nl_patterns=[
        r"\bspust[íi]\s+video\s+enroll",
        r"\btrenu[jí]\s+m[ěe]",
    ],
    handler=_cmd_enroll,
    help_text="Spustí video enrollment (zachytí 30s video tváří)",
)


# G5C_VERIFY_COMMAND_V1 — registrace /verify
register(
    "verify",
    slash_aliases=["verify", "over", "overit"],
    nl_patterns=[],
    handler=_cmd_verify,
    help_text="Ověří faktická tvrzení proti Wikipedii (/verify <text> nebo poslední odpověď)",
)


from scripts.chat_cmd_dum import _cmd_sleep   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "sleep",
    slash_aliases=["sleep"],
    nl_patterns=[
        r"\bb[ěe]ž\s+spát",
        r"\bjdi\s+spat",
        r"\bvzbu[ďd]\s+se",
        r"\bprobu[ďd]\s+se",
    ],
    handler=_cmd_sleep,
    help_text="Toggle spánkového režimu (manuální override).",
)


# ─── /herni — herní mód: uvolni VRAM pro hru na PC (OLLAMA_GAME_MODE_V1) ──────
_HERNI_ON  = {"zap", "zapni", "on", "1", "ano", "start"}
_HERNI_OFF = {"vyp", "vypni", "off", "0", "ne", "stop", "konec"}


from scripts.chat_cmd_dum import _cmd_herni   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "herni",
    slash_aliases=["herni", "herní", "hra", "game", "hrani", "hraní"],
    nl_patterns=[
        r"\bjdu\s+hrát",
        r"\bspou[št]t[íě]m\s+hru",
        r"\bhern[íi]\s+m[óo]d",
    ],
    handler=_cmd_herni,
    help_text="Herní mód — uvolní grafiku pro hru na PC (/herni vyp = zpět).",
)


# ─── /severka — sebereflexe identity (HANS_SEVERKA_V1, Fáze 3c) ──────────
_SEVERKA_APPROVE = {"schválit", "schvalit", "approve", "ano", "ok", "souhlasím", "souhlasim"}
_SEVERKA_REJECT  = {"zamítnout", "zamitnout", "reject", "ne", "nesouhlasím", "nesouhlasim"}
_SEVERKA_HISTORY = {"historie", "history", "log"}
_SEVERKA_ROLLBACK = {"rollback", "vrať", "vrat", "zpět", "zpet"}
_SEVERKA_RUN = {"teď", "ted", "run", "check", "spusť", "spust", "zkontroluj"}


from scripts.chat_cmd_studium import _cmd_severka   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "severka",
    slash_aliases=["severka"],
    nl_patterns=[],
    handler=_cmd_severka,
    help_text="Sebereflexe identity: /severka [stav|schválit|zamítnout|historie|rollback <id>|teď]",
)


from scripts.chat_cmd_studium import _cmd_uzamceni   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "uzamceni",
    slash_aliases=["uzamceni", "uzamčení"],
    nl_patterns=[],
    handler=_cmd_uzamceni,
    help_text="Týdenní míry uzamčení: opakování témat, ozvěna persony (/uzamceni)",
)


from scripts.chat_cmd_tvorba import _cmd_art   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "art",
    slash_aliases=["art", "obraz"],
    nl_patterns=[],
    handler=_cmd_art,
    help_text="Hans namaluje obraz k aktuální knize: /art [název knihy]",
)


from scripts.chat_cmd_tvorba import _cmd_namaluj   # ROZDELENI_PRIKAZU_V1 — přesunuto


_INSTR_TOKENS = {
    # instrukční slovesa/příslovce, které samy o sobě NENESOU námět
    "zkus", "zkusme", "zkusit", "jeste", "ještě", "znovu", "znova",
    "vypad", "vypada", "vypadá", "nedokon", "nedokoncena", "nedokončená",
    "myslel", "prosim", "prosím", "jinak", "lepe", "lépe", "hur", "hůř",
    "dalsi", "další", "opakuj", "opakovat", "jednou", "jeste",
    # meta slova o obrazu (neurčují námět)
    "obraz", "obrazek", "obrázek", "obrazku", "malba", "kresba",
    # imperativy tvorby
    "namaluj", "nakresli", "vytvor", "vytvoř", "prekresli", "překresli",
    "premaluj", "přemaluj", "udelej", "udělej", "kresba", "malovat",
    # obecné meta výrazy o tématu
    "tema", "téma", "temat", "témat", "veci", "věci", "vec", "věc",
}


def _is_instruction_only(s: str, ref_pronouns: set) -> bool:
    """HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 (20.7.) — True když v textu
    po odstranění pronoun + instrukčních slov nezbývá ŽÁDNÝ obsahový token
    (žádné podstatné jméno, žádný konkrétní subjekt).

    Vzor: „to znova prosim" → toks {to, znova, prosim} → všechny v ref+INSTR
    → True. „kočka na zdi" → {kočka, na, zdi} → „kočka" a „zdi" mimo → False.

    Krátká spojka („na/v/u/s/a/i") se počítá jako neobsahová — nezachrání
    „to na X" pokud X samo v INSTR/ref.
    """
    import re as _re
    _STOP = {"na", "v", "u", "s", "z", "o", "a", "i", "k", "do", "od",
             "pro", "ze", "za", "před", "po", "při", "kde"}
    toks = _re.findall(r"\w+", (s or "").lower())
    for t in toks:
        if t in ref_pronouns or t in _INSTR_TOKENS or t in _STOP:
            continue
        # zbývá aspoň jeden obsahový token — subject má co malovat
        return False
    return True


def _distill_paint_subject(config, name, handler, subj: str):
    """HANS_ART_SUBJECT_DISTILL_V1 — z messy požadavku + kontextu rozhovoru
    destiluj JEDEN výtvarný námět (2-6 slov, česky). Řeší odkazy („tu kočku"
    → kočka, „o čem jsme se bavili" → téma), ořeže instrukce („zkus znovu",
    „vypadá nedokončeně"). Fallback = původní subj (LLM dole/podezřelý výstup).

    HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 (20.7.) — když ani po destilaci
    není konkrétní námět (jen pronoun/instrukce zůstalo), vrátí **None** →
    caller místo poslání do SDXL poprosí uživatele o upřesnění.
    """
    import re as _re2
    toks = set(_re2.findall(r"\w+", subj.lower()))
    _ref = {"to", "ho", "ji", "tu", "ten", "tenhle", "tohle", "toho",
            "tuhle", "tamtu", "mě", "mne", "mně", "mnou", "me", "sebe",
            # odkazy na PŘEDCHOZÍ téma („o tom", „o něm") → destiluj z kontextu
            "tom", "tomhle", "tomto", "něm", "nem", "něj", "nej", "nich"}
    # POZOR: NEspouštět destilaci jen podle DÉLKY — explicitní víceslovný námět
    # („velký mimoň a spousta malých") se pak s těžkým kontextem přebil na téma
    # z předchozího rozhovoru („Les Camerounais"). Destiluj JEN u skutečných
    # odkazů (zájmena) nebo instrukčního šumu — jinak zadání RESPEKTUJ.
    messy = (subj.startswith(("náš rozhovor:", "dojem z"))
             or bool(toks & _ref)
             or any(k in subj.lower() for k in (
                 "zkus", "jeste", "ještě", "znovu", "vypad", "nedokon",
                 "myslel jsem", "o mně", "o mne")))
    if not messy:
        # HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 — messy check nemusel chytit
        # (např. „obraz znova" — bez „znovu"/pronoun v setu), ale sám subj
        # je jen instrukce → radši refuse než rovnou do SDXL beze změny.
        if _is_instruction_only(subj, _ref):
            _log.info("art subject: subj instruction-only bez messy %r → refuse", subj[:60])
            return None
        return subj
    try:
        conv = getattr(handler, "conv_store", None)
        # HANS_CHAT_CHANNEL_AWARE_V1 — destilaci krmi JEN tímto kanálem
        _ch = _current_channel()
        if conv is None:
            hist = []
        elif _ch:
            hist = conv.get_history_scoped(name, _ch)
        else:
            hist = conv.get_history(name)
        ctx = "\n".join(
            "%s: %s" % ("Uživatel" if m.get("role") == "user" else "Hans",
                        (m.get("content") or "")[:150])
            for m in (hist or [])[-6:])
        from scripts.ollama_client import ollama_generate
        model = ((config.get("dialog", {}) or {}).get("model")
                 or "hans-czech:latest")
        _who = ("Ten, kdo píše, se jmenuje %s. „mě/o mně\" = tato osoba "
                "(portrét či scéna o ní), NE obecný pojem „uživatel\". "
                % name) if name else ""
        system = (
            "Jsi extraktor výtvarného NÁMĚTU pro malbu. Z posledního "
            "požadavku uživatele a kontextu rozhovoru urči JEDEN konkrétní "
            "námět obrazu (CO má být namalováno), česky, 2 až 6 slov. Rozřeš "
            "odkazy: „tu kočku\" → kočka; „to/o čem jsme se bavili\" → to "
            "téma z kontextu; „dnešní počasí\" → konkrétní počasí z kontextu. "
            + _who +
            "DŮLEŽITÉ: když požadavek UŽ pojmenovává konkrétní věc k namalování "
            "(„velký mimoň a spousta malých\", „západ slunce nad mořem\"), vrať "
            "PŘESNĚ TU VĚC (jen zkrať) — kontext použij POUZE k rozřešení "
            "zájmen (to/tom/ho/toho); NIKDY jím NEPŘEBÍJEJ jasně zadaný námět. "
            "IGNORUJ instrukce jako „zkus znovu\", „vypadá nedokončeně\" — to "
            "NENÍ námět. NEPŘIDÁVEJ nic navíc. Vrať POUZE námět, jedním "
            "krátkým slovním spojením.")
        prompt = "%s\n\nPožadavek: %s\n\nNÁMĚT:" % (ctx, subj)
        raw = ollama_generate(model, prompt, system=system, config=config,
                              timeout=25, keep_alive=-1,
                              options={"temperature": 0.1, "num_predict": 30})
        if not raw:
            # HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 — LLM mlčí + subj sám
            # je jen instrukce („to znova prosim") → nepropouštět jako námět.
            if _is_instruction_only(subj, _ref):
                _log.info("art subject: LLM mlčí + subj instruction-only %r → refuse", subj[:60])
                return None
            return subj
        out = raw.strip().splitlines()[0]
        out = _re2.sub(r"(?i)^\s*n[áa]m[ěe]t\s*:?\s*", "", out)
        out = out.strip(" \"'„“”?.!:•-")
        if out and 1 <= len(out.split()) <= 8 and 2 <= len(out) <= 60 \
                and not out.lower().startswith(("nevím", "nemám", "promiň")):
            # HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 — LLM vrátil „To téma
            # znova" / „obraz znova" (guard by ho pustil) — pořád jen
            # instrukce, žádný obsahový námět → refuse.
            if _is_instruction_only(out, _ref):
                _log.info("art subject: destilace vrátila jen instrukci %r → refuse", out)
                return None
            _log.info("art subject destilován: %r → %r", subj[:50], out)
            return out
    except Exception as _e:
        _log.debug("distill subject: %s", _e)
    # Fallback: pokud subj sám je jen instrukce, radši odmítni než malovat nesmysl.
    if _is_instruction_only(subj, _ref):
        return None
    return subj


# HANS_ART_NOT_PAST_QUESTION_V1 — minulý čas sloves malování. Otázka
# „namaloval JSI něco?" je dotaz na hotové dílo, ne příkaz malovat.
# HANS_ART_EXPLAIN_NOT_REQUEST_V1 (12. 9.) — VYSVETLUJICI veta nesmi spustit RENDER.
# Doloheno 11. 9. 14:06: "tu je vysvetleni k te hromade malovani. upravuji
# tvuj kod abys ses kazdym namalovanym obrazem zlepsoval" — slovo
# "namalovanym" sedlo na `.*\bnama[kl]\w*` a Hans zacal malovat (FLUX drzi
# GPU ~5 min). Tataz trida jako u `detect_intent` v bridge_commands.
# Zmereno na 824 realnych vetach: 0 zmen = zadna regrese.
_ART_VYSVETL = (r"(?:\bpro[cč]\b|\bjen\s+(?:abys|vysv[eě]tl|vysvetl)"
        r"|\bnev[ií]m\s+jestli\b|\bnevim\s+jestli\b"
        r"|\bupravuji\b|\btu\s+je\s+vysv[eě]tl)")

_ART_MINULE = (r"(?:namaloval|namalovala|nakreslil|nakreslila"
               r"|vytvo[řr]il|vytvo[řr]ila)\w*")
# HANS_ART_CONTRACTED_PAST_V1 (27. 9.) — stažené „namalovals / nakreslils /
# namalovalas“ = „namaloval jsi“ (jsi je v koncovce -s), takže výjimka pro
# minulý čas s „jsi/jste“ je minula. Doloženo testem: „namalovals uz nekdy
# neco takovyho?“ spustilo FLUX s námětem „uz nekdy neco takovyho“ a chat byl
# minuty zablokovaný („Zrovna maluji“). V 1 646 reálných replikách 0×, ale
# cena výskytu je nejvyšší ze všech falešných spuštění (viz NOT_PAST níž).
# HANS_ART_REQUEST_FORM_V1 (27. 9.) — malovat JEN na tvar, který je ŽÁDOST:
# rozkaz (namaluj/nakresli/namalujte…), budoucí 2. os. (namaluješ/nakreslíš?)
# nebo infinitiv po žádosti („můžeš / zkus / chci / mohl bys … namalovat“).
# Doloženo 27. 9. dvakrát za večer: „namalovals…?“ a „co Vás vedlo k tomu,
# abyste se ho pokusil nakreslit?“ → FLUX + chat blokovaný pro všechny.
# Výjimky pro minulý čas (NOT_PAST, CONTRACTED_PAST) každá zachytila jeden
# tvar; tohle obrací logiku. 📏 172 reálných povelů /namaluj: ztráta 0
# (vč. „zkusíme namalovat…“, „nezkusíš namalovat…?“, překlepů namalij/namakuj).
_ART_ZADOST = (
    r"(?:\b(?:nama\w{0,3}j\w*|nakresl(?:i|ete|[íi][šs]|[íi]te)|namaluje[šs]|namalujete"
    r"|p[řr]ekresli\w*|p[řr]emaluj\w*)\b"
    r"|\b(?:m[ůu][žz]e[šs]|m[ůu][žz]ete|mohl[ai]?|um[íi][šs]|um[íi]te|zkus|zkuste"
    r"|zkus[íi]me|zkus[íi][šs]|nezkus[íi][šs]|zkus[íi]te|chci|cht[ěe]la?|chce[šs]|chcete"
    r"|bys|byste|pros[íi]m|pot[řr]ebuju|myslela?|poj[ďd]|poj[ďd]me)\b"
    r"(?:\W+\w+){0,4}?\W+(?:namal\w*|nakresl\w*|nakres\w*))")
_ART_MINULE_STAZENE = (r"\b(?:namaloval|namalovala|nakreslil|nakreslila"
                       r"|vytvo[řr]il|vytvo[řr]ila)s\b")


register(
    "namaluj",
    slash_aliases=["namaluj", "nakresli"],
    # HANS_ART_CMD_TYPO_V1 (5.9.) — tolerance PREKLEPU v kmeni slova.
    # Doloheny dva realne pripady, oba propadly do volneho hovoru nebo na cizi
    # prikaz: „zkus jeste jednou NAMALOVAR obrazek s Richardem sorge" (-> None,
    # pak router -> report_person = karta o Sorgeovi misto obrazu) a „NAMAKUJ
    # obraz o filmu co jsi dnes videl" (-> /film, protoze slovo „film" ve vete
    # prebilo zkomolene sloveso).
    # `nama[kl]\w*` pokryje namaluj/namaloval/namalovat/namalovar/namakuj,
    # `nakresl\w*` i nakreslit/nakresleny. Precedens `ja[kmn]` u wellbeing.
    # ⚠️ ZMERENO na 1 359 realnych zpravach: rozdily PRESNE 2 a jsou to obe
    # doloheme chyby — zadna jina veta se tim neunese.
    # ── HANS_ART_NOT_PAST_QUESTION_V1 (9. 9.) ───────────────────────────
    # Vzory byly `\bnama[kl]\w*` / `\bnakresl\w*`, tedy COKOLI od „namal"
    # nebo „nakresl" — včetně MINULÉHO ČASU. Doloženo simulovaným rozhovorem
    # 9. 9.: „namaloval jsi neco?" (dotaz!) spustilo malování s námětem
    # „jsi neco" (zbytek věty po odstranění slovesa).
    # 💸 Cena je ze všech falešných spuštění NEJVYŠŠÍ: FLUX drží GPU ~5 min,
    # `avatar_render` odsune hans-czech i qwen2.5 z VRAM a chat je po tu dobu
    # zablokovaný („Zrovna maluji") — v tom testu to znehodnotilo 8 z 10 tahů
    # druhé sady. Plus vznikne odpadní obraz.
    # ⚠️ Signál k odmítnutí PŘITOM UŽ EXISTOVAL a nepoužil se:
    # `hans_art` loguje „'jsi neco' je obecné slovo — grounding přeskočen",
    # tedy VÍ, že námět je nesmysl, a maluje dál.
    # 📏 Změřeno PŘED zásahem: 73 z 758 replik jde na `/namaluj` a po zúžení
    # jich jde pořád 73 (0 změn v korpusu); cílová sada 13/13.
    # Dotazy v minulém čase spadnou na `/obrazy` — příkaz, který na to je.
    nl_patterns=[r"^(?!.*\b(?:jsi|jste)\b.*\b" + _ART_MINULE + r")"
                 r"(?!.*\b" + _ART_MINULE + r".*\b(?:jsi|jste)\b)"
                 r"(?!.*" + _ART_VYSVETL + r")"          # HANS_ART_EXPLAIN_NOT_REQUEST_V1
                 r"(?!.*" + _ART_MINULE_STAZENE + r")"   # HANS_ART_CONTRACTED_PAST_V1
                 r"(?=.*" + _ART_ZADOST + r")"            # HANS_ART_REQUEST_FORM_V1
                 r".*\bnama[kl]\w*",
                 r"^(?!.*\b(?:jsi|jste)\b.*\b" + _ART_MINULE + r")"
                 r"(?!.*\b" + _ART_MINULE + r".*\b(?:jsi|jste)\b)"
                 r"(?!.*" + _ART_VYSVETL + r")"          # HANS_ART_EXPLAIN_NOT_REQUEST_V1
                 r"(?!.*" + _ART_MINULE_STAZENE + r")"   # HANS_ART_CONTRACTED_PAST_V1
                 r"(?=.*" + _ART_ZADOST + r")"            # HANS_ART_REQUEST_FORM_V1
                 r".*\bnakresl\w*",
                 r"vytvoř\s+obr",
                 r"\bp[řr]ekresli", r"\bp[řr]emaluj",
                 r"\boprav\s+(ten\s+|ten[hz]le\s+)?(obraz|obr[áa]zek)"],
    handler=_cmd_namaluj,
    help_text="Hans namaluje/překreslí obraz: namaluj <téma> (i namaluj to jinak)",
)


_KOLIK_RE = re.compile(r"\bkolik\b", re.IGNORECASE)


def _pocet_obrazu(handler) -> str:
    """HANS_COUNT_ANSWER_V1 (13. 9.) — na „kolik“ odpovez CISLEM, ne vypisem.

    Dolozeno 12. i 13. 9.: spocitatelna otazka dostala bud falesnou abstinenci
    („neumim rici“, pritom jich bylo 303), nebo vypis peti del misto poctu.
    Vzor `/obrazy` slovo „kolik“ uz obsahuje — chybela jen tahle vetev.
    """
    try:
        import sqlite3 as _s3
        _c = _s3.connect(_recall_db(handler))
        _n = _c.execute(
            "SELECT COUNT(*) FROM diary WHERE event_type='artwork'").fetchone()[0]
        _posl = _c.execute(
            "SELECT title FROM diary WHERE event_type='artwork' "
            "AND title IS NOT NULL AND title<>'' ORDER BY id DESC LIMIT 1").fetchone()
        _c.close()
    except Exception:
        return ""
    if not _n:
        return ""
    # cesky tvar podle poctu: 1 obraz / 2-4 obrazy / 5+ obrazu
    _tv = "obraz" if _n == 1 else ("obrazy" if 2 <= _n <= 4 else "obraz\u016f")
    _out = "Zat\u00edm jsem namaloval %d %s." % (_n, _tv)
    if _posl and _posl[0]:
        _out += " Naposledy \u201e%s\u201c." % _posl[0]
    return _out


def _pocet_filmu(handler) -> str:
    """HANS_COUNT_FILMS_BOOKS_V1 (14. 9.) — „kolik filmu jsi videl?“ → CISLO.

    Dolozeno 13. 8.: otazka nesedla na zadny prikaz, propadla do volneho
    hovoru a Hans odpovedel nesmyslem o „prime otazce“. Pocita se ze
    stejneho zdroje jako `films_watched_answer` (denik `kodi_playing`), jen
    rozdelene podle typu — „filmy“ nejsou dily serialu ani poradu z TV.
    """
    _tv = lambda n, a, b, c: a if n == 1 else (b if 2 <= n <= 4 else c)
    try:
        import sqlite3 as _s3
        _c = _s3.connect(_recall_db(handler))
        _r = dict(_c.execute(
            "SELECT CASE WHEN note LIKE 'Typ: movie%' THEN 'film' "
            "WHEN note LIKE 'Typ: episode%' THEN 'dil' "
            "WHEN note LIKE 'Typ: channel%' THEN 'tv' ELSE 'jine' END, "
            "COUNT(DISTINCT title) FROM diary WHERE event_type='kodi_playing' "
            "AND title IS NOT NULL AND title<>'' GROUP BY 1").fetchall())
        _posl = _c.execute(
            "SELECT title FROM diary WHERE event_type='kodi_playing' "
            "AND note LIKE 'Typ: movie%' AND title IS NOT NULL AND title<>'' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        _c.close()
    except Exception:
        return ""
    _f = int(_r.get("film") or 0)
    if not _f:
        return ""
    _out = "Zat\u00edm jsem vid\u011bl %d %s" % (_f, _tv(_f, "film", "filmy", "film\u016f"))
    _dalsi = []
    _d = int(_r.get("dil") or 0)
    if _d:
        _dalsi.append("%d %s seri\u00e1l\u016f" % (_d, _tv(_d, "d\u00edl", "d\u00edly", "d\u00edl\u016f")))
    _p = int(_r.get("tv") or 0)
    if _p:
        _dalsi.append("%d %s v televizi" % (_p, _tv(_p, "po\u0159ad", "po\u0159ady", "po\u0159ad\u016f")))
    if _dalsi:
        _out += ", k tomu " + " a ".join(_dalsi)
    _out += "."
    if _posl and _posl[0]:
        _out += " Naposledy film \u201e%s\u201c." % _posl[0]
    return _out


def _pocet_knih(handler) -> str:
    """HANS_COUNT_FILMS_BOOKS_V1 (14. 9.) — „kolik knih jsi precetl?“ → CISLO.
    Zdroj je knihovna `hans_library` — tataz, ze ktere se doporucuje cetba
    (HANS_BOOK_RECOMMEND_GROUNDED_V1), aby cislo a doporuceni nesly proti sobe."""
    _tv = lambda n, a, b, c: a if n == 1 else (b if 2 <= n <= 4 else c)
    try:
        import sqlite3 as _s3
        _c = _s3.connect(_recall_db(handler))
        _n = _c.execute(
            "SELECT COUNT(*) FROM hans_library WHERE status='finished'").fetchone()[0]
        _ctu = _c.execute(
            "SELECT book_title FROM hans_library WHERE status='reading' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        _c.close()
    except Exception:
        return ""
    if not _n and not _ctu:
        return ""
    _out = "Do\u010detl jsem %d %s" % (_n, _tv(_n, "knihu", "knihy", "knih"))
    if _ctu and _ctu[0]:
        _out += " a pr\u00e1v\u011b \u010dtu \u201e%s\u201c" % _ctu[0]
    return _out + "."


from scripts.chat_cmd_tvorba import _cmd_obrazy   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "obrazy",
    slash_aliases=["obrazy", "namaloval", "galerie"],
    # HANS_ARTWORK_RECALL_V1 (30.8.) — DOTAZ NA HOTOVÉ DÍLO, ne pokyn malovat.
    # Doloženo: „namaloval jsi neco novyho?" propadlo do volného hovoru a Hans
    # odpověděl „nemám možnost vytvářet obrazy samostatně" — přitom má 89 obrazů
    # za 30 dní. Vzory jsou v MINULÉM čase a schválně NEobsahují „namaluj",
    # aby nekradly routing příkazu k malování (`\bnamaluj` na „namaloval"
    # nesedne — liší se od šestého znaku, ověřeno).
    nl_patterns=[
        r"namaloval\s+(jsi|si)\b",
        r"co\s+jsi\s+(dnes\w*\s+|v[čc]era\s+|naposledy\s+)?namaloval",
        # HANS_ART_NOT_PAST_QUESTION_V1 — vykání a „nakreslil/vytvořil".
        # Vzory výš znaly jen TYKÁNÍ a jen „namaloval"
        # [[test-both-grammatical-persons]].
        r"\b" + _ART_MINULE + r"\s+(jsi|jste)\b",
        _ART_MINULE_STAZENE,                     # HANS_ART_CONTRACTED_PAST_V1
        r"\b(co|jak[ée]|kolik)\b.*\b(jsi|jste)\b.*\b" + _ART_MINULE,
        # HANS_OBRAZY_NOT_TECHNIQUE_V1 (4. 10.) — otázka na techniku/rozměr
        # obrazu není žádost o výpis (/tazatel: „olejomalba 60×80“) → chat
        # s HANS_OWN_WORK_DETAIL_V1
        r"(?!.*(?:olejomal|pl[áa]tn|rozm[ěe]r|jak\s+velk|technik|[čc][íi]m\s+(?:jsi|jste)\s+maloval))(posledn[íi]|nov[ýy])\s+obraz\b",
        # HANS_COUNT_ANSWER_V1 (13. 9.) — „kolik obrazu mas?“ nema sloveso
        # v minulem case, takze na vzor s `(jsi|jste)` + minuly tvar nesedlo
        # a propadlo do volneho hovoru → falesna abstinence (12. 9.).
        r"\bkolik\b[^?.!]{0,20}\b(obraz\w*|d[ěe]l)\b",
        r"jak[ýy]\s+obraz\s+jsi",
        r"kreslil\s+(jsi|si)\b",
        # HANS_ARTWORK_SHOW_V1 (30.8.) — „ukaž mi ten obraz" je dotaz, ne pokyn
        # malovat. Holé „ukaž mi to" tu ZÁMĚRNĚ není: bez předmětu může mířit
        # na cokoli (rozvrh, deník, nález) a únos by byl horší než dnešní stav.
        # HANS_ARTWORK_ASK_WIDEN_V1 (12. 9.) — mezi slovesem a podstatnym jmenem
        # smi byt 0-3 libovolna slova. Dosud tam smelo stat jen
        # `mi` + `ten/ty/svuj`, takze "ukaz mi NEJAKY obraz" nesedlo
        # a veta spadla do volneho hovoru, kde LLM tvrdil, ze Hans
        # obrazy posilat neumi. Doplnena slovesa posli/poslat/videt.
        # Zmereno: cil 10/10, 0 vet ukradenych na 758 realnych,
        # 9 hranicnich pripadu nekrade `/namaluj`.
        r"(?!.*(?:olejomal|pl[áa]tn|rozm[ěe]r|jak\s+velk|technik|[čc][íi]m\s+(?:jsi|jste)\s+maloval))(uka[žz]\w*|po[šs]l\w*|poslat|vid[ěe]t|uvid[ěe]t)"
        r"(\s+\w+){0,3}\s+(obraz|obr[áa]z|galeri)",
        r"m[ůu][žz]u\s+(to\s+)?vid[ěe]t\s+(ten\s+)?obraz",
        # HANS_ARTWORK_UNPREFIXED_V1 (16. 9.) — NEPREDPONOVE tvary.
        # `_ART_MINULE` zna jen „namaloval/nakreslil/vytvoril“, takze
        # „co jsi uz MALOVAL?“ propadlo do volneho hovoru — a tam Hans
        # bud zapre, nebo si obrazy vymysli (oboji doloženo 15. a 16. 9.).
        # `kreslil\s+(jsi|si)` uz vyse je, slo tedy o asymetrii.
        # `malova[lit]` snese i preklep „malovai“ (tyz pristup jako
        # `nama[kl]\w*` u `/namaluj`, HANS_ART_CMD_TYPO_V1).
        # ⚠️ Oba vzory jsou UKOTVENE NA 2. OSOBU, takze rozkaz „namaluj“
        # ani dotaz na schopnost („muzes malovat?“) nekradou.
        # Zmereno pres `parse_command`: cil 6/6, kontroly 8/8 beze zmeny,
        # korpus 758 -> 3 zmenena smerovani, vsechna spravne.
        r"(?<![a-zá-ž])(malova[lit]\w*|kreslil\w*|tvo[řr]il\w*)\s+(jsi|jste|sis|si)(?![a-zá-ž])",
        # HANS_ARTWORK_ADVERB_WIDEN_V1 (22. 9.) — mezi zajmenem a slovesem
        # smel stat jen UZAVRENY seznam prislovci (uz/vcera/dnes), takze
        # "co jsi POSLEDNI DOBOU maloval?" regexova vrstva minula
        # (`parse_command` -> None) a vetu prevzal LLM router, ktery zvolil
        # `/dilo` — tedy vypis PSANI na dotaz o OBRAZECH. Doloženo
        # pametovou sadou 22. 9.; se slovem "obrazy" odpovidal spravne.
        # ⛔ Brana `/dilo` to zachytit NEMUZE: rozlisuje "vlastni zaznamy
        # x cizi dilo" a tahle veta O JEHO vlastni cinnosti JE, jen o jine.
        # Spravne misto je proto tady — prikaz predbehne agenta
        # [[command-preempts-agent-same-message]].
        # 📏 Zmereno na 2 327 vetach (1 569 realnych chatu + korpus 758):
        # PRIRUSTEK 0 — realne tvary uz prochazely, takze je to pokryti
        # dalsich formulaci za nulovou cenu, ne oprava casteho selhani.
        # Az tri libovolna slova, liny kvantifikator (at se neprotahne
        # pres pulku souveti).
        r"co\s+(jsi|jste|sis|si)\s+(\w+\s+){0,3}?(malova[lit]\w*|kreslil\w*)",
    ],
    handler=_cmd_obrazy,
    help_text="Co jsem namaloval (přímo z deníku artwork): /obrazy [dnes]",
)


# ─── /schopnosti — co Hans reálně umí (HANS_CAPABILITY_AWARENESS_V1) ─────────
# HANS_CAP_HOWTO_V1 (26.8.) — „kam/kde/jak to funguje" u KONKRÉTNÍ schopnosti.
# Doloženo: „kam mi pošleš ten snímek?" → Hans nejdřív nabídl hlídání zapnout,
# po opravě agenta odpověděl abstinencí — a přitom odpověď („na Matrix") je
# v `hans_capabilities` celou dobu. Nebyla to neznalost, ale NEDORUČENÍ.
# ⚠️ Fail-open jako `/vycet`: když se žádná schopnost netrefí, vrací prázdno
# a dotaz propadne do běžného hovoru. Vrátit CIZÍ schopnost je horší než nic.
_HOWTO_PAT = re.compile(r"\b(kam|kde|jak|jakto)\b", re.IGNORECASE)


from scripts.chat_cmd_pamet import _cmd_jakto   # ROZDELENI_PRIKAZU_V1 — přesunuto


# ⛔ NEREGISTROVAT jako příkaz s vzorem `\b(kam|kde|jak)\b…` — vyzkoušeno
# 26.8. a KRADE to routing: „jak jde studium?" šlo na `jakto` místo na
# `studium`, a protože handler vrátil prázdno, deterministická odpověď
# /studium se ztratila úplně. „jak" je moc běžné slovo.
# Doručuje se proto GROUNDINGEM (`openwebui_direct_handler`), který routing
# nesahá. `_cmd_jakto` zůstává jako pomocná funkce.


from scripts.chat_cmd_zaklad import _cmd_schopnosti   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "schopnosti",
    slash_aliases=["schopnosti", "umis", "umíš", "capabilities"],
    nl_patterns=[r"co\s+(v[šs]echno\s+)?um[ií][šs]", r"co\s+dok[aá][žz]e[šs]"],
    handler=_cmd_schopnosti,
    help_text="Přehled toho, co Hans umí: /schopnosti",
)


from scripts.chat_cmd_dum import _cmd_blink   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "blink",
    slash_aliases=["blink", "mrkni"],
    # jen jednoznačné „mrkni okem/očima / zamrkej" — NE holé „mrkni" (kolize s
    # „mrkni na to" = podívej se)
    nl_patterns=[r"\bzamrkej\b", r"\bmrkni\s+(oč|ok|na\s+m[ěe])"],
    handler=_cmd_blink,
    help_text="Hans mrkne očima: /blink",
)


from scripts.chat_cmd_dum import _cmd_hraje   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "hraje",
    slash_aliases=["hraje", "prehrava", "přehrává"],
    # HANS_HRAJE_WORDORDER_V1 (7.8.) — „teď" smí stát PŘED i ZA slovesem.
    # Doloženo: „co TEĎ běží v tv?" minulo (volitelné „teď" bylo jen ZA
    # slovesem) → propadlo na LLM router → vyhrál `rozvrh` a Hans vypsal
    # seznam autonomních rutin. „co hraje" to mělo správně už předtím —
    # nekonzistence uvnitř jednoho bloku.
    nl_patterns=[
        r"co\s+(te[ďd]\s+)?hraj[eí]",
        r"hraje\s+(te[ďd]\s+)?n[ěe]jak",
        r"co\s+se\s+(te[ďd]\s+)?p[řr]ehr[aá]v[aá]",
        r"p[řr]ehr[aá]v[aá]\s+se\s+(te[ďd]\s+)?n[ěe]co",
        r"co\s+(te[ďd]\s+)?b[ěe][žz][ií]\s+(te[ďd]\s+)?(v\s+)?(televiz|tv|kodi)",
        r"co\s+(te[ďd]\s+)?d[aá]vaj[ií]\s+(te[ďd]\s+)?(v\s+)?(televiz|tv)",
    ],
    handler=_cmd_hraje,
    help_text="Co se právě přehrává (živě z Kodi): co hraje?",
)


# ─── /nitky — rozjeté nitky per osoba (HANS_THREADS_V1, frontier #4) ──────
_NITKY_CLOSE = {"zavři", "zavri", "close", "uzavři", "uzavri"}
_NITKY_ALL = {"vše", "vse", "all", "vsechny", "všechny"}


from scripts.chat_cmd_pamet import _cmd_nitky   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "nitky",
    slash_aliases=["nitky", "threads"],
    nl_patterns=[],
    handler=_cmd_nitky,
    help_text="Rozjeté nitky per osoba: /nitky [vše|zavři <id>]",
)


# ─── /zajmy — per-osoba zájmy (HANS_PERSON_INTERESTS_V1, frontier #4) ─────
# HANS_ZAJMY_O_HANSOVI_V2 (14. 9.) — mezi „co vás/tě“ a „zajímá“ smí být až 3 slova.
# V1 znala jen „vlastně“; živě ověřeno: „a co vas ted nejvic zajima?“ → zájmy TAZATELE.
_ZAJMY_NA_HANSE = re.compile(
    r"\b(?:tv[\u016fu]j|tvoje|tvoji|tv[\u00e1a]|tv[\u00e9e]|va[\u0161s]e|va[\u0161s]i|va[\u0161s]ich)\b[^?.!]{0,24}"
    r"\b(?:z[\u00e1a]j(?:em|my|m[\u016fu])|kon[\u00edi][\u010dc]\w*|bav[\u00edi])"
    # HANS_ZAJMY_NA_TEMA_V1 (6. 10.) — „co te NA TOM zajima“ se pta na
    # pojmenovane tema, ne na vycet konicku → mezera nesmi nest „na“;
    # a „tebe“ je 2. osoba stejne jako „te“.
    r"|\bco\s+(?:t[\u011be]|tebe|v[\u00e1a]s)\s+(?:(?!na\b)\w+\s+){0,3}zaj[\u00edi]m\w*",  # HANS_ZAJMY_O_HANSOVI_V2
    re.IGNORECASE)


# HANS_ZAJMY_ASKER_ONLY_V1 (15. 9.) — /zajmy chodi JEN pres LLM router
# (nl_patterns=[]) a router si ho vzal i bez slova o zajmech ("proc zrovna
# fotbal") a na vetu o HANSOVI ve 2. osobe ("Co vas na studiu fotbalu nejvic
# bavi a proc?"). Hans pak tazateli odpovedel, ze o JEHO zajmech nic nevi.
_ZAJEM_SLOVO = re.compile(
    r"z[\u00e1a]j(?:em|my|m[\u016fu]|mu|m[e\u011b]|[\u00edi]m)|zaj[\u00edi]m|"
    r"kon[\u00edi][\u010dc]|bav[\u00edi]|bavil|hobb|\br[\u00e1a]d[aoy]?\b|obl[\u00edi]b",
    re.IGNORECASE)
_DRUHA_OSOBA = re.compile(
    r"\b(?:t[\u011be]|tebe|tob[\u011be]|ti|tv[\u016fu]j|tvoje|tvoji|tv[\u00e1a]|tv[\u00e9e]|"
    r"v[\u00e1a]s|v[\u00e1a]m|va[\u0161s]e|va[\u0161s]i|va[\u0161s]eho|v[\u00e1a][\u0161s])\b",
    re.IGNORECASE)
# HANS_ZAJMY_VERB_2ND_V1 (1. 10.) — 2. osoba bývá jen ve SLOVESE („mas rad
# klasicke skladatele?“, „mel jsi rad nejakou knihu?“, „zajimas se…?“,
# „Čtete rád…?“) → _DRUHA_OSOBA (zájmena) ji míjela, router dal /zajmy
# a Hans odmítl „to patří jiné osobě“ (/tazatel 1. 10. 3×, 30. 9. 1×).
# Změřeno na 15 skutečných volbách /zajmy z logu (~50 dní): vypadne 7 otázek
# na Hanse; „co myslis ze ME zajima?“ drží výjimka 1. osoby.
_DRUHA_OSOBA_SLOVESO = re.compile(
    r"\b(?:jsi|jste|m[\u00e1a][\u0161s]|m[\u00e1a]te|"
    r"\w{2,}(?:[\u00e1a][\u0161s]|[\u00edi][\u0161s]|e[\u0161s]|[\u00e1a\u00edie]te))\b",
    re.IGNORECASE)
_PRVNI_OSOBA_OBJEKT = re.compile(r"\b(?:m[\u011be]|mne|mi|m[\u016fu]j|moje|moji)\b",
                                 re.IGNORECASE)
# HANS_KALENDAR_NOT_ELLIPSIS_V1 (15. 9.) — kratka elipsa po predpovedi pocasi.
_ELIPSA_KRATKA = re.compile(r"^\s*a\s+(?:\S+\s*){1,3}\??\s*$", re.IGNORECASE)
_KALENDAR_SLOVO = re.compile(
    r"kalend|napl[\u00e1a]n|ud[\u00e1a]lost|sch[\u016fu]z|term[\u00edi]n|program",
    re.IGNORECASE)
_POCASI_REPLIKA = re.compile(
    r"\u00b0C|p[\u0159r]edpov[\u011be]|po[\u010dc]as[\u00edi]|p[\u0159r]eh[\u00e1a][\u0148n]k|"
    r"sr[\u00e1a][\u017ez]k|d[e\u011b][\u0161s]t|sn[\u011be][\u017ez]",
    re.IGNORECASE)


def _hansovy_konicky(db: str) -> str:
    """HANS_ZAJMY_O_HANSOVI_V1 (13. 9.) — Hansovy VLASTNI konicky z `hobbies`.

    `person_interests` jsou zajmy LIDI; Hans v te tabulce neni a nikdy nebude.
    Jeho vlastni zaujeti drzi `hobbies` (evidence_count = kolikrat se k tomu
    vratil). Vraci '' kdyz tabulka nic nema → volajici se chova jako dosud.
    """
    try:
        import sqlite3 as _s3
        _c = _s3.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        _r = _c.execute(
            "SELECT name, evidence_count FROM hobbies "
            "WHERE COALESCE(status,'') <> 'dropped' "
            "ORDER BY sila DESC, evidence_count DESC LIMIT 6").fetchall()
        _c.close()
    except Exception:
        return ""
    _r = [(n, e) for n, e in _r if n]
    if not _r:
        return ""
    return ("Nejv\u00edc m\u011b posledn\u00ed dobou zam\u011bstn\u00e1v\u00e1: "
            + ", ".join("%s" % n for n, _ in _r[:4])
            + ". Nejd\u00e9le se vrac\u00edm k t\u00e9matu \u201e%s\u201c." % _r[0][0])


from scripts.chat_cmd_pamet import _cmd_zajmy   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "zajmy",
    slash_aliases=["zajmy", "zájmy", "interests"],
    nl_patterns=[],
    handler=_cmd_zajmy,
    help_text="Co koho zajímá: /zajmy [jméno]",
)


# ─── /studium — studijní program z koníčku (HANS_STUDY_V1, #1 odbornost) ──
_STUDY_NOW = {"teď", "ted", "now", "session", "studuj"}
# HANS_STUDY_ORIGIN_V1 (26.8.) — „vybral sis to sám, nebo jsem ti to zadal já?"
# Doloženo 26.8.: dotaz na původ studia se zaroutoval jako `volny_hovor`
# (nonfactual) → bez groundingu → Hans si vymyslel, že to plyne „z deníku z 26.
# srpna v 06:15" (což byl web_read zpráv z ČT24; Cimrman je program z 30.7.).
# Persona smí vyprávět o domě, ale NESMÍ si vymýšlet, CO MÁ ZAPSÁNO.
# Odpověď je deterministická z DB, bez LLM — vzor HANS_STUDY_RECALL_V1, který
# vznikl na tutéž třídu chyby („copak jsi studoval" → vymyšlený report).
_STUDY_ORIGIN = {"původ", "puvod", "kdo", "odkud", "proč", "proc", "zadal"}
# HANS_STUDY_NUDGE_V1 (4.8.) — ruční popostrčení, když se studium zaseklo na
# pod-tématu, ke kterému encyklopedie nemá článek. Automatika ho přeskočí až po
# `max_subtopic_failures` NOCÍCH (default 3) — tohle je zkratka pro uživatele.
_STUDY_SKIP = {"přeskoč", "preskoc", "přeskoc", "preskoč", "skip", "dál", "dal",
               "další", "dalsi", "jeď dál", "jed dal"}


def _notify_user(handler, msg: str) -> bool:
    """HANS_STUDY_NUDGE_V1 — ohlas výsledek úlohy běžící na pozadí.

    Příkazy typu `/studium teď` startují vlákno a hned se vrátí; bez tohohle
    uživatel nikdy nezjistí, že session skončila `noread` (přesně to zamlčelo
    zaseknuté studium 4.8.). Posílá se přes Notifier (Matrix) — `send_proactive`
    respektuje tiché okno, takže v noci to počká do rána."""
    try:
        tg = getattr(handler, "telegram", None)   # = Notifier (historický název)
        if tg is None or not getattr(tg, "enabled", True):
            return False
        _send = getattr(tg, "send_proactive", None) or getattr(tg, "send", None)
        if not _send:
            return False
        _send(msg)
        return True
    except Exception as _e:
        _log.debug("notify_user: %s", _e)
        return False


def _datum_cz(ts) -> str:
    """HANS_STUDY_ORIGIN_V1 — „30.7.2026" z unixového času; hlásí se, když neví."""
    try:
        import datetime as _d
        return _d.datetime.fromtimestamp(float(ts)).strftime("%-d.%-m.%Y")
    except Exception:
        return "neznámo kdy"


_ORIGIN_PAT = re.compile(
    r"(vybral|zvolil|urcil|určil)\s+(sis|jsi\s+si|sis\s+to|si)\b"
    # obrácený slovosled je v češtině stejně běžný: „to SIS VYBRAL ty sám"
    r"|\b(sis|jsi\s+si)\s+(to\s+)?(vybral|zvolil|ur[cč]il)\b"
    r"|\bsám\s+(sis|jsi)\b|\bsam\s+(sis|jsi)\b"
    r"|\b(zadal|ulozil|uložil|rekl|řekl)\s+(jsem|ti|mi)\b"
    r"|\bkdo\s+(ti|vám|vam)\s+(to\s+)?(zadal|ur[cč]il|vybral)\b"
    r"|\bod[kK]ud\s+se\s+vzalo\b"
    # HANS_SOURCES_BODY_V1 (9. 9.) — „odkud máš" bylo PŘÍLIŠ ŠIROKÉ a kradlo
    # dotaz na ZDROJ: „odkud máš informace o hradu X?" končilo u `/studium`
    # (tedy odpovědí „kdo mi to téma zadal"), místo u `/zdroje`. Vykací tvar
    # „odkud MÁTE informace" přitom nechytal nikdo a padal na LLM.
    # `_ORIGIN_PAT` má odpovídat na PŮVOD TÉMATU, takže si o téma říká
    # výslovně. 📏 Změřeno PŘED zásahem na 758 replikách: 0 změn v routingu,
    # a 5 z 5 vět o původu tématu zůstalo u `/studium`.
    r"|\bod[kK]ud\s+(m[áa][sš]|m[áa]te)\s+(to\s+|ten\s+|tohle\s+|tenhle\s+)?"
    r"(t[ée]ma|n[áa]m[ěe]t|zad[áa]n[íi])\b", re.IGNORECASE)


# HANS_STUDY_WHY_TOPIC_V1 (15. 9.) — "proc zrovna fotbal" na HANSOVO studium.
# Doloženo 15. 9.: po "studuji historii fotbalu" se tazatel zeptal "proc zrovna
# fotbal" a Hans odpovedel "Fotbal nebyl tema meho studia" — vyrok si vymyslel
# na ceste bez faktu (factual_nofacts). `_studium_puvod` pritom deterministicky
# vi, kdo tema vybral. Rozhoduje, zda text ZA "proc zrovna" je tema studijniho
# programu v DB; samotne "proc zrovna" nestaci (korpus 837 vet: 4x, vsechny
# o svete — rok 2021, po roce 1968, tohle, ono — a zadna tema nesedne).
_PROC_TEMA_PAT = re.compile(
    r"\bpro[čc]\s+(?:zrovna|pr[áa]v[ěe]|studuje(?:š|s|te)|"
    r"ses\s+rozhodl\w*\s+pro|jste\s+se\s+rozhodl\w*\s+pro|sis\s+vybral|"
    r"jste\s+si\s+vybral)\s+(.{2,40}?)\s*[?.!]*\s*$", re.IGNORECASE)


def _puvod_tema_z_proc(text: str, db: str) -> str:
    """Tema studijniho programu, na ktere se "proc zrovna X" pta, jinak ''."""
    m = _PROC_TEMA_PAT.search((text or "").strip())
    if not m:
        return ""
    import sqlite3 as _sq
    import unicodedata as _ud
    _f = lambda s: "".join(c for c in _ud.normalize("NFKD", (s or "").lower())
                           if not _ud.combining(c))
    slova = [w for w in re.findall(r"[a-z0-9]+", _f(m.group(1))) if len(w) >= 4]
    if not slova:
        return ""
    try:
        con = _sq.connect(db)
        temata = [r[0] for r in con.execute("SELECT topic FROM study_program ORDER BY id DESC")]
        con.close()
    except Exception:
        return ""
    for t in temata:
        for b in [w for w in re.findall(r"[a-z0-9]+", _f(t)) if len(w) >= 4]:
            for a in slova:
                k = 0
                while k < min(len(a), len(b)) and a[k] == b[k]:
                    k += 1
                if k >= 4 and k >= 0.7 * min(len(a), len(b)):
                    return t
    return ""


def _je_dotaz_na_puvod(text: str) -> bool:
    """HANS_STUDY_ORIGIN_V1 — ptá se věta, KDO téma vybral?"""
    return bool(_ORIGIN_PAT.search(text or ""))


def _studium_puvod(store, db: str, args: str) -> str:
    """HANS_STUDY_ORIGIN_V1 — kdo zvolil téma: uživatel, nebo Hans sám?

    Deterministicky z DB. `add_study_topic → accepted` = zadal uživatel (a KDY);
    když takový záznam není, vybral si program Hans sám (`ensure_program`
    z durable koníčků) a platí datum `started_ts`.
    ⚠️ Absence záznamu je tu ZÁMĚRNĚ brána jako „vybral jsem si sám" — tak to
    dnes v systému opravdu funguje. Kdyby přibyla další cesta k založení
    programu, tahle úvaha se musí přepsat.
    """
    import json as _json
    import sqlite3 as _sq
    prog = None
    try:
        con = _sq.connect(db)
        con.row_factory = _sq.Row
        radky = con.execute(
            "SELECT topic, topic_norm, status, started_ts FROM study_program "
            "ORDER BY id").fetchall()
        hledane = _norm_veta(args)
        if hledane:
            for r in radky:            # shoda na jádrových slovech, ne přesná
                t = _norm_veta(r["topic"])
                # HANS_STUDY_ORIGIN_SCOPE_V1 — po celých slovech (kmen 5 zn.):
                # podřetězec dával „proc“ ⊂ „proces“ → Norimberský proces.
                _tt = t.split()
                if hledane == t or any(
                        w == x or (len(w) >= 5 and len(x) >= 5 and w[:5] == x[:5])
                        for w in hledane.split() if len(w) > 3
                        and w not in _STUDY_ORIGIN for x in _tt):
                    prog = r
                    break
        if prog is None:
            prog = (store.get_active_program() or
                    (dict(radky[-1]) if radky else None))
        if prog is None:
            con.close()
            return "Zatím jsem nezačal žádný studijní program, pane."
        tema = prog["topic"]
        zadano = con.execute(
            "SELECT ts, data FROM diary WHERE event_type='agent_action' "
            "AND title LIKE 'add_study_topic%' ORDER BY id DESC").fetchall()
        con.close()
    except Exception as e:
        _log.warning("HANS_STUDY_ORIGIN_V1: %s", e)
        return "K původu tématu se mi teď nepodařilo dostat, pane."

    tn = _norm_veta(tema)
    for r in zadano:
        try:
            d = _json.loads(r["data"] or "{}")
        except Exception:
            continue
        if d.get("outcome") != "accepted":
            continue
        t = _norm_veta((d.get("args") or {}).get("tema") or "")
        if not t or (t not in tn and tn not in t):
            continue
        # ⚠️ `accepted` znamená NÁVRH + VAŠE SCHVÁLENÍ. Z dat se NEDÁ poznat,
        # jestli téma původně padlo z vaší věty, nebo si ho vymyslel Hans —
        # obojí projde toutéž cestou (router → návrh → potvrzení). Proto se
        # tvrdí jen to, co je doložené, a nedomýšlí se původce.
        kdo = d.get("person") or ""
        return ("Téma „%s\" máme v deníku jako schválené, pane — návrh padl %s "
                "a byl přijat%s. Kdo s ním přišel první, ze záznamu nepoznám."
                % (tema, _datum_cz(r["ts"]), (" (" + kdo + ")") if kdo else ""))
    return ("Téma „%s\" jsem si zvolil sám, pane — založil jsem si ho %s "
            "z trvalých zájmů. V deníku nemám žádný záznam, že byste ho "
            "schvaloval." % (tema, _datum_cz(prog["started_ts"])))


# ── /vycet — výčtový dotaz jako SELECT (HANS_FACTS_ENUM_V1, 26.8.) ──────────
# Doloženo živě: „jaké hrady vlastně znáš?" → Hans vyjmenoval Windsor, Tower of
# London, Sychrov, Pernštejn a Karlštejn. ŽÁDNÝ z nich nemá v datech — je to
# výčtová konfabulace na RAG cestě, kterou guard vidí, ale nehlídá.
# `entity_facts` přitom umí odpovědět deterministicky (Cardiffský hrad, Kost).
# ⚠️ Formulace je ZÁMĚRNĚ SKROMNÁ: korpus NENÍ úplný (Hans četl o věcech, které
# se entitou nestaly), takže se tvrdí jen „v ověřených faktech mám tyto",
# nikdy „tohle je všechno, co znám".
# `(?:\w+\s+){0,2}` = vsuvka („jaké hrady VLASTNĚ znáš"). Bez ní vzor nesedl.
_VYCET_PAT = re.compile(
    # `a` v třídě je nutné: bez diakritiky se píše „jakA města" a extrakce
    # slova pak spadla na celé souvětí (routing to přežil, ten diakritiku
    # odstraňuje — extrakce ne).
    # HANS_VYCET_NOT_RELATIVE_V1 (6. 10.) — tri zuzeni, zmereno na 2 055
    # vetach (6 shod → 4, nove zadna): (a) carka pred „ktera“ = vztazna
    # veta („mista, ktera jste studoval“), ne vyctovy dotaz; (b) pomocne
    # sloveso neni hledana kategorie; (c) „jake X mas na mysli“ se pta
    # na vyznam, ne na vycet.
    r"(?<!,\s)\b(jak[éeáa]|kter[éeáa])\s+"
    r"(?!js(?:te|i|me|em)\b|by(?:ste|ch|s)\b)"
    r"([a-zá-žA-ZÁ-Ž]{4,})\w*\s+(?:\w+\s+){0,2}"
    r"(zn[áa][sš]|m[áa][sš]|v[íi][sš]|pamatuje[sš]|studoval)"
    r"(?!\s+na\s+mysli)", re.IGNORECASE)


def _vycet_dotaz(text: str) -> str:
    """Vrátí hledané slovo („hrady"), nebo prázdno."""
    m = _VYCET_PAT.search(text or "")
    return m.group(2) if m else ""


from scripts.chat_cmd_pamet import _cmd_vycet   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "vycet",
    slash_aliases=["vycet", "výčet", "cojeto"],
    nl_patterns=[_VYCET_PAT.pattern],
    handler=_cmd_vycet,
    help_text="Co mám doloženo ve faktech: jaké hrady znáš? jaké filmy znáš?",
)


from scripts.chat_cmd_studium import _cmd_studium   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "studium",
    slash_aliases=["studium", "study", "učení", "uceni"],
    # HANS_STUDY_RECALL_V1 — recall otázky na studium jdou na grounded /studium
    # (jinak je zodpoví LLM konfabulací; doloženo „copak jsi studoval" → vymyšlený
    # report o Českém ráji, který nebyl nastudovaný).
    nl_patterns=[
        r"co(?:pak)?\s+(?:jsi|jsi|si)\s+(?:na)?studoval",
        r"co\s+(?:(?:te[dď]|pr[aá]v[eě])\s+)?studuje[sš]",
        r"co\s+ses\s+(?:na)?u[cč]il",
        r"jak\s+.{0,12}(?:tv[eé]\s+)?studium",
        r"na\s+[cč]em\s+.{0,6}studuje[sš]",
        # HANS_STUDY_ORIGIN_V1 — kdo téma vybral (jinak to zodpoví LLM
        # konfabulací; doloženo 26.8. vymyšlenou citací vlastního deníku).
        # ⚠️ JEDEN ZDROJ PRAVDY: tentýž vzor, jakým se rozhoduje uvnitř příkazu.
        # Dvě kopie se hned rozešly — psal jsem je zvlášť a slovosled „to SIS
        # VYBRAL" byl opravený jen v jedné, takže dotaz k příkazu vůbec nedošel.
        # HANS_STUDY_ORIGIN_SCOPE_V1 (1. 10.) — jen když ZA slovesem výběru
        # (= v pozici předmětu; lookahead se vyhodnocuje od místa shody) stojí
        # studium/téma/program nebo „to/tohle/sám“. Na 1 711 větách (korpus, konverzace,
        # přepisy) sedl holý vzor 2× a OBA mimo studium („proc sis vybral
        # pravy bacha?“, „…tu konkretni knihu na cteni?“) → Hans odpověděl
        # o „Norimberském procesu“. Případ z 26. 8. („vybral sis to sám?“) drží „to/sám“.
        r"(?=.*(?:studi|t[eé]m|program|nau[cč]|\bto\b|tohle|toto|\bs[aá]m\b))(?:"
        + _ORIGIN_PAT.pattern + r")",
    ],
    handler=_cmd_studium,
    help_text="Studijní program: /studium [programy|teď|přeskoč]",
)


# ─── /smer — vlastní směr / aspirace (HANS_DIRECTION_V1) ─────────────────────
_SMER_NL = [
    r"jak[ýy]\s+m[áa][sš]\s+sm[eě]r",
    r"kam\s+sm[eě][rř]uje[sš]",
    r"co\s+chce[sš]\s+d[eě]lat\s+d[aá]l",
    r"(?:tv[uů]j|m[uů]j)\s+sm[eě]r",
    r"k\s+[cč]emu\s+sm[eě][rř]uje[sš]",
]


def _smer_is_custom(sub: str) -> bool:
    """HANS_DIRECTION_NL_ARG_GUARD_V1 (6.8.) — je `sub` opravdu ZADÁNÍ vlastního
    směru, nebo jen otázka, kterou sem poslala NL shoda?

    `parse_command` u NL vrací celou větu jako args (u `/vytvor`/`/namaluj` je
    to správně — vzory jsou rozkazy), jenže vzory `/smer` jsou OTÁZKY. Věta
    „v úvaze kam směřuješ říkáš…? " se tak uložila jako nový směr a PŘEBILA
    ten skutečný (doloženo 6.8., směr z 2.8. skončil jako superseded).
    Zadání směru proto musí být oznamovací věta, která sama nespustila zdejší
    NL vzor. Slash s takovým textem propadne na výpis — to je u otázky
    „kam směřuješ?" i tak správná odpověď.
    """
    s = (sub or "").strip()
    if not s or s.endswith("?"):
        return False
    fold = _fold_diacritics(s)
    for p in _SMER_NL:
        if re.search(p, s, re.IGNORECASE) or re.search(p, fold, re.IGNORECASE):
            return False
    return True


from scripts.chat_cmd_studium import _cmd_smer   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "smer",
    slash_aliases=["smer", "směr", "smetr", "direction", "aspirace"],
    nl_patterns=_SMER_NL,
    handler=_cmd_smer,
    help_text="Vlastní směr/aspirace: /smer [schválit|ne|teď|<vlastní text>]",
)


from scripts.chat_cmd_tvorba import _cmd_dilo   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "dilo",
    slash_aliases=["dilo", "dílo", "psani", "psaní", "kniha_moje"],
    nl_patterns=[],
    handler=_cmd_dilo,
    help_text="Autorský projekt: /dilo [vše|teď]",
)


from scripts.chat_cmd_tvorba import _cmd_napad   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "napad",
    slash_aliases=["napad", "nápad", "napady", "nápady", "synteze", "syntéza"],
    nl_patterns=[],
    handler=_cmd_napad,
    help_text="Vlastní nápady / synteze: /napad [vše|teď]",
)


from scripts.chat_cmd_tvorba import _cmd_kritika   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "kritika",
    slash_aliases=["kritika", "sebekritika", "sebereflexe"],
    nl_patterns=[],
    handler=_cmd_kritika,
    help_text="Sebekritika vlastního projevu: /kritika [teď]",
)


from scripts.chat_cmd_tvorba import _cmd_dashboard   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "dashboard",
    slash_aliases=["dashboard", "nastenka", "nástěnka"],
    nl_patterns=[],
    handler=_cmd_dashboard,
    help_text="Hansův návrh vlastní nástěnky: /dashboard [teď]",
)


# AVATAR_CMD_V1 — ruční inspekce/refresh vizuálního descriptoru (fáze 2 avatara).
_AVATAR_GEN = {"gen", "generuj", "nový", "novy", "znovu", "teď", "ted", "refresh"}


from scripts.chat_cmd_tvorba import _cmd_avatar   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "avatar",
    slash_aliases=["avatar", "podoba", "tvar"],
    nl_patterns=[],
    handler=_cmd_avatar,
    help_text="Vizuální podoba (descriptor): /avatar [stav|gen]. Render obrázku = fáze 3 (TBD).",
)


# ─── /misto — model místa „Kde jsem" (HANS_PLACE_V1, frontier #4) ─────────
_MISTO_SUBS = {
    "mistnost": "room", "místnost": "room", "pokoj": "room",
    "okno": "window", "okna": "window",
    "dvere": "door", "dveře": "door",
    "vedle": "neighbor", "soused": "neighbor", "sousedni": "neighbor",
    "rozlozeni": "layout", "rozložení": "layout", "layout": "layout",
    "pozn": "note", "poznamka": "note", "poznámka": "note",
}
_MISTO_DEL = {"smaz", "smaž", "odeber", "zrus", "zruš", "del"}
_MISTO_CAT_LABEL = {
    "room": "Místnost", "window": "Okno", "door": "Dveře",
    "neighbor": "Vedle", "layout": "Rozložení", "note": "Pozn.",
    "mental_map": "Z fotek",
}


from scripts.chat_cmd_dum import _cmd_misto   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "misto",
    slash_aliases=["misto", "místo", "kdejsem", "domov"],
    nl_patterns=[],
    handler=_cmd_misto,
    help_text="Model domova (kde jsem): /misto [mistnost|okno|dvere|vedle|rozlozeni|pozn <text> | smaz <id>]",
)


# ─── HANS_RECALL_SHORTCIRCUIT_V1 — vnitřní paměťové dotazy PŘÍMO Z DAT ────────
# (#1 anti-konfabulačního pořadí) — „první vzpomínka" / „co jsi četl" /
# „kdy jsi mě viděl" se NEposílají do LLM: odpověď je deterministická šablona
# z deníku (vzor HANS_LIVE_PLAYBACK_QUERY_V1). Nulová konfabulace.

def _recall_db(handler) -> str:
    cfg = getattr(handler, "config", {}) or {}
    return (cfg.get("diary_db")
            or (cfg.get("hans_idle", {}) or {}).get("diary_db")
            or "data/hans_diary.db")


from scripts.chat_cmd_pamet import _cmd_vzpominka   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "vzpominka",
    slash_aliases=["vzpominka", "vzpomínka"],
    nl_patterns=[
        r"(prvn[íi]|nejstarš[íi])\s+(tvoje?\s+|tv[áa]\s+)?vzpom[íi]nk",
        r"vzpom[íi]nk\w*\s+(m[áa]š\s+)?(jako\s+)?(úplně\s+)?prvn[íi]",
        r"co\s+si\s+pamatuje[šs]\s+(jako\s+|ze\s+všeho\s+)?(úplně\s+)?"
        r"(prvn[íi]|nejd[řr][íi]v)",
        r"nejstarš[íi]\s+z[áa]znam",
        # HANS_MEMORY_SPAN_V1 (30.8.) — DOTAZ NA ROZSAH PAMĚTI je totéž jako
        # dotaz na první vzpomínku: `first_memory_answer` vrací OBOJÍ (nejstarší
        # záznam + „od té doby mám zapsáno N záznamů"), jen se k ní tyhle
        # formulace nedostaly.
        # Doloženo 30.8.: „kolik toho v deníku máte a za jak dlouhou dobu?" →
        # Hans odpověděl, že deník *„začal při mém spuštění před několika dny…
        # informace z posledních přibližně čtyř dnů"*. Skutečnost: 25. 4. 2026
        # a 66 tisíc záznamů. Domýšlel si čísla o vlastní historii.
        #
        # ⚠️ ČÁST TĚCHTO VZORŮ NAVRHOVALA UŽ CLAUDE.md 8.7. („jak dávno si
        # pamatuješ", „úplně první") — návrh ale zůstal NEPOSTAVENÝ a seděl
        # v sekci nápadů. Změřeno 30.8.: pět formulací propadalo do LLM.
        # HANS_MEMORY_SPAN_V2 (1.9.) — DOBA SLUŽBY je totéž co rozsah paměti.
        # Doloženo dlouhým rozhovorem: „Jak dlouho už v tomto domě takto
        # sloužíte?" → Hans si VYMYSLEL „první zaznamenaný incident 28. prosince
        # 2019", a o dva tahy později na „vzpomínáte si na svůj první den?"
        # odpověděl správně (25. 4. 2026, 67 608 záznamů). Odpověď tedy existuje
        # a je deterministická — jen se k ní tahle formulace nedostala.
        # ⚠️ Vzory V1 mířily na DENÍK („vedeš deník", „od kdy existuješ"), ne na
        # SLUŽBU. A byly skoro celé v tykání — cizí člověk přitom vyká, takže
        # sada níž má obě osoby. [[test-both-grammatical-persons]]
        # ⚠️ Změřeno na 1547 reálných uživatelských replikách: **0 falešných
        # poplachů**, všech 6 cílových formulací chyceno.
        r"jak\s+dlouho\s+(u[žz]\s+)?.{0,30}?slou[žz][íi](te|[šs])",
        r"jak\s+dlouho\s+(u[žz]\s+)?(tu|tady|zde)\s+(jsi|jste|slou|p[ůu]sob|fun)",
        r"jak\s+dlouho\s+(u[žz]\s+)?existuj(e[šs]|ete)",
        r"jak\s+dlouho\s+(u[žz]\s+)?(jsi|jste)\s+(tu|tady|zde|v\s+t)",
        r"od\s+kdy\s+(tu|tady|zde)\s+(jsi|jste)",
        r"jak\s+d[áa]vno\s+si\s+pamatuje[šs]",
        # HANS_MEMORY_SPAN_V3 (19. 9.) — vzor byl jen v TYKANI, ackoli
        # komentar u V2 se odvolava na obe osoby (to plati pro SLUZBU,
        # ne pro DENIK). Doloženo testem cizim clovekem: "Jak dlouho si
        # deník vedete?" propadlo do LLM a Hans odpovedel "od dětství"
        # (skutecnost: 25. 4. 2026, 74 544 zaznamu). Druha dira byl
        # SLOVOSLED — veta mela predmet pred slovesem ("si deník vedete").
        # Zmereno: 5/5 cilovych formulaci chyceno, 0 falesnych poplachu
        # na 758 realnych replikach. [[test-both-grammatical-persons]]
        r"jak\s+dlouho\s+(u[žz]\s+)?(si\s+)?"
        r"(vede[šs]|vedete|p[íi][šs]e[šs]|p[íi][šs]ete|m[áa][šs]|m[áa]te)"
        r"\s+(ten\s+)?den[íi]k",
        r"jak\s+dlouho\s+(u[žz]\s+)?(si\s+)?(ten\s+)?den[íi]k\s+"
        r"(vede[šs]|vedete|p[íi][šs]e[šs]|p[íi][šs]ete)",
        r"od\s+kdy\s+(si\s+)?(vede[šs]|p[íi][šs]e[šs]|m[áa][šs]|existuje[šs])",
        r"kolik\s+(toho\s+)?(m[áa][šs]|m[áa]te)\s+.{0,20}?(den[íi]k|zapsan|z[áa]znam)",
        # ⚠️ Tolerance musí být ŠIROKÁ: doložená věta zněla „…k tomu deníku:
        # kolik toho v něm vlastně máte a za jak dlouhou dobu…" — mezi „deníku"
        # a „za jak dlouho" je 38 znaků. S tolerancí 15 propadla i po opravě.
        r"(den[íi]k\w*|z[áa]znam\w*)[\s\S]{0,70}?za\s+jak\s+dlouh",
        r"kolik\s+toho\s+.{0,25}?(m[áa][šs]|m[áa]te)\b",
        r"jak\s+dlouho\s+(u[žz]\s+)?existuje[šs]",
    ],
    handler=_cmd_vzpominka,
    help_text="Má první/nejstarší vzpomínka (přímo z deníku, žádný odhad)",
)


# HANS_BOOK_PROGRESS_ANSWER_V1 (23. 9.) — dotaz na PRŮBĚH čtení (poprvé,
# jak dlouho, od kdy, kolikátá kapitola). Rozhoduje VZOR, ne LLM router —
# ten „tu knihu od le guin čteš poprvé?" zamítl jako dotaz na svět.
_PRUBEH_CTENI_PAT = re.compile(
    r"\b(?:[čc]te[šs]|[čc]tete)\b[^.?!]{0,40}\b(?:poprv|znovu|opakovan)"
    r"|\b(?:poprv[ée]|znovu)\b[^.?!]{0,30}\b(?:[čc]te[šs]|[čc]tete)\b"
    r"|\bjak\s+dlouho\b[^.?!]{0,30}\b(?:[čc]te[šs]|[čc]tete)\b"
    r"|\bod\s+kdy\b[^.?!]{0,30}\b(?:[čc]te[šs]|[čc]tete)\b"
    r"|\b(?:kolik[áa]t\w*|kter[ée]|jak[ée])\s+kapitol\w*\s+(?:u[žz]\s+)?(?:jsi|jste|[čc]te)"
    r"|\b(?:na|u)\s+(?:kolik[áa]t|kter)[ée]\s+kapitol"
    r"|\b[čc]etla?\s+(?:jsi|jste)\s+(?:ji|ho|tu\s+knihu)\s+(?:u[žz]\s+)?"
    r"(?:n[ěe]kdy\s+)?(?:d[řr][íi]v|p[řr]edt[íi]m)", re.I)


from scripts.chat_cmd_pamet import _cmd_cetl   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "cetl",
    slash_aliases=["cetl", "četl", "cteni", "čtení"],
    nl_patterns=[
        # HANS_BOOK_PROGRESS_ANSWER_V1 — vzor na průběh čtení (viz výš).
        _PRUBEH_CTENI_PAT.pattern,
        # HANS_COUNT_FILMS_BOOKS_V1 (14. 9.) — pocet prectenych knih (tykani
        # i vykani, bez diakritiky). Na 2 248 realnych vetach 0 zasahu.
        r"\bkolik\s+(?:\w+\s+){0,3}kn[i\u00ed](?:h|\u017eek|zek)\w*[^.?!]{0,30}(?:[\u010dc]etl|p[\u0159r]e[\u010dc]ten)",
        r"\bkolik\s+(?:jsi|jste|sis)\s+(?:u[\u017ez]\s+)?(?:p[\u0159r]e)?[\u010dc]etl\w*\s+(?:\w+\s+)?kn[i\u00ed]",
        r"\bkolik\s+(?:m[\u00e1a][\u0161s]|m[\u00e1a]te)\s+(?:u[\u017ez]\s+)?p[\u0159r]e[\u010dc]ten\w*\s+kn[i\u00ed]",
        r"\bco\s+(jsi|sis)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
        r"(pře)?[čc]etl",
        r"\bcos?\s+(dnes\w*\s+|včera\s+|naposledy\s+)?[čc]etl",
        r"\bkdy\s+(jsi|sis)\s+[čc]etla?\b",
        # HANS_VYKANI_DLOUHA_OTAZKA_V1 (9. 9.) — vzor znal JEN TYKÁNÍ
        # (`jsi|sis`), vykací „četl JSTE" v něm nebylo. Doloženo rozhovorem:
        # „Četl jste někdy něco od Isaaca Asimova…?" nedošlo ani k regexu,
        # ani k routeru → Hans řekl „v paměti jsem o tom nic neměl" a šel na
        # Wikipedii, PŘESTOŽE má 179 deníkových záznamů o té knize a byl
        # zrovna na kapitole 90–91. ⚠️ Recall vadný NENÍ — `reading_answer`
        # na tutéž větu odpoví správně; vada byla čistě v ROUTINGU.
        # 📌 Třetí případ téže asymetrie za jediný den.
        # [[test-both-grammatical-persons]] · [[corpus-has-no-foreign-speakers]]
        r"\b(pře)?[čc]etla?\s+(jsi|sis|jste)\s+(něco|neco|někdy|nekdy|už|uz)?\s*o?\b.{2,}\?",
        r"\bco\s+(pr[áa]vě\s+|te[ďd]\s+)?[čc]te[šs]\b",
        # HANS_CETL_VYKANI_V1 (23. 9.) — vzory vyse znaly jen TYKANI:
        # "co jsi cetl" / "co ctes" prosly, "co jste cetl" / "co ctete"
        # / "jake knihy jste cetl" spadly do LLM (sada B 23. 9.: 88 s
        # a bez knihy, kterou Hans tutez noc cetl). Mezi zajmenem
        # a slovesem az tri slova ("v posledni dobe") — vzor jako
        # HANS_ARTWORK_ADVERB_WIDEN_V1. Zmereno na 2 342 realnych vetach:
        # +1 spravne ("co jsis dnes precetl"), 0 ztrat; kontroly 11/11.
        # [[test-both-grammatical-persons]]
        r"\bco\s+(?:jste|jsis)\s+(?:\w+\s+){0,3}?(?:pře|pre)?[čc]etl",
        r"\bco\s+(?:pr[áa]vě\s+|te[ďd]\s+)?[čc]tete\b",
        r"\bjak[éeýy]\s+(?:kn[ií]\w*|[čc]l[áa]nk\w*|texty|[čc]etb\w*)"
        r"(?:\s+(?:nebo|a)\s+\w+)?\s+(?:jsi|sis|jste)\s+(?:\w+\s+){0,3}?"
        r"(?:pře|pre)?[čc]etl",
    ],
    handler=_cmd_cetl,
    help_text="Co/kdy jsem četl (přímo z deníku): co jsi četl? četl jsi o X?",
)


from scripts.chat_cmd_tvorba import _cmd_sen   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "sen",
    slash_aliases=["sen", "sny", "snil"],
    # ⚠️ O tom, jestli se dotaz k obsluze vůbec dostane, rozhodují TYHLE vzory,
    # ne větvení uvnitř — proto jsou obě osoby (ti/vám) u KAŽDÉHO tvaru
    # [[test-both-grammatical-persons]].
    # ⛔ „zdálo se MI" schválně chybí: to o svém snu mluví uživatel, ne Hans.
    nl_patterns=[
        r"\bzd[áa]lo\s+se\s+(ti|v[áa]m)\b",
        r"\bnezd[áa]lo\s+se\s+(ti|v[áa]m)\b",
        r"\bsnilo\s+se\s+(ti|v[áa]m)\b",
        r"\bo\s+[čc]em\s+(jsi|jste)\s+snila?\b",
        r"\bm[ěe]l(a)?\s+(jsi|jste)\s+.{0,20}\bsen\b",
        # ── HANS_DREAM_NOT_ASPIRATION_V1 (21. 9.) — `sen` JE HOMONYMUM ──────
        # Nocni sen x TOUHA. Doloženo třikrát, a ve VŠECH TŘECH OSOBÁCH řeči:
        #   „váš sen je ukotvený v konkrétní realitě"        (cizi, 20. 9.)
        #   „co je ted tvuj sen, tvoje velka aspirace?"      (domaci, 21. 9.)
        #   „váš sen – myslím tím spíš vaši aspiraci – je…"  (cizi, 21. 9.)
        # Na vsechny tri Hans vypsal nocni sen o ledovych vlockach.
        # A neni to jen vec testu — v korpusu je SKUTECNA zprava
        # „popiš mi svůj sen do budoucna, o čem přemýšlíš".
        #
        # 🪤 PRVNI OPRAVA (tehoz dne rano) ZAKAZOVALA JEN SPONU za frazi
        # („sen JE…") a padla hned v prvnim rozhovoru: v A/13 stoji za „sen"
        # CARKA, v B/9 POMLCKA. Zakazovat dalsi a dalsi interpunkci je zavod,
        # ktery se nevyhraje — rozhoduje NAOPAK pritomnost dukazu.
        #
        # 🔑 Nocni vyznam nese skoro vzdy EPIZODICKOU STOPU (zdalo, snilo,
        # noc, spanek) nebo KVALIFIKATOR (posledni/dnesni sen). Holy tvar
        # „tvuj sen" bez obojiho je statisticky touha → radsi nic.
        # ⛔ Podminka „veta musi byt otazka" NESTACI — zabila by holy
        # kvalifikovany tvar z help_textu („tvůj poslední sen").
        # Zmereno na kontrolnim seznamu 8 vet: chybne verdikty 3 → 0;
        # na 1 566 skutecnych zpravach ma tenhle vzor 0 vyskytu, takze
        # zuzeni nestoji nic.
        r"\b(tv[ůu]j|v[áa][šs])\s+(posledn[íi]|dne[šs]n[íi])\s+sen\b",
        r"^(?=.*\b(zd[áa]l\w*|snilo|noc\w*|ve\s+sp[áa]nku|probudil))"
        r".*\b(tv[ůu]j|v[áa][šs])\s+sen\b",
        r"\bco\s+se\s+(ti|v[áa]m)\s+zd[áa]lo\b",
        r"\bjak[ýy]\s+(jsi|jste)\s+m[ěe]l(a)?\s+sen\b",
    ],
    handler=_cmd_sen,
    help_text="Co se mi zdálo (přímo z deníku): zdálo se ti něco? tvůj poslední sen",
)


# ─── /zdroje — odkud Hans čerpal (HANS_SOURCES_V1) ─────────────────────────
# HANS_SOURCES_BODY_V1 (9. 9.) — tázací tvary, které NEJSOU téma.
_ZDROJ_STOP = {"cem", "čem", "kom", "sobe", "sobě", "tom", "tobe", "tobě",
               "nich", "ni", "ní", "nem", "něm", "tomhle", "tomto", "nem"}
_ZDROJ_TEMA_PAT = re.compile(
    r"\b(?:o|k|ke)\s+([\w ěščřžýáíéúůďťňó-]{2,45}?)\s*[?.!]?$", re.IGNORECASE)
# HANS_SOURCES_TOPIC_OBJECT_V1 (7. 10.) — téma i jako PŘEDMĚT („odkud znáš X“),
# s iniciálami („o F. L. <Příjmení>“) a v PRVNÍ klauzi, když věta pokračuje
# („…? četl jsi to někde?“). Bez tématu /zdroje vypíše jen poslední čtení —
# na věc čtenou před měsícem tedy neodpoví.
# 📏 36 reálných i testovacích vět na /zdroje: +3 témata, všechna správná.
_ZDROJ_STOP2 = {"to", "ho", "ji", "je", "jej", "tohle", "toto", "tuhle", "ten",
                "ta", "ty", "tu", "tuto", "tenhle", "me", "mě", "mne", "nas",
                "nás", "vsechno", "všechno"}
_ZDROJ_TEMA_KL = re.compile(
    r"\b(?:o|k|ke)\s+([\w ěščřžýáíéúůďťňó.-]{2,45}?)\s*$", re.IGNORECASE)
_ZDROJ_OBJ_KL = re.compile(
    r"\bodkud\s+(?:zn[áa][šs]|zn[áa]te)\s+([\w ěščřžýáíéúůďťňó.-]{2,45}?)\s*$",
    re.IGNORECASE)


# HANS_VIDEL_KOHO_V2 — věta MÍŘÍ NA TAZATELE („kdy jsi MĚ viděl").
# ⚠️ JEN 4. PÁD. Dativ „mi"/„nám" tu ZÁMĚRNĚ NENÍ: je to zdvořilostní obrat
# („Řekněte MI prosím…", „Povězte NÁM…"), který s předmětem vidění nemá nic
# společného. S ním v sadě odpovídal Hans na „Řekněte mi prosím, vidíte teď
# někoho?" větou „nemám záznam, že bych VÁS viděl" — tedy přesně ta vada,
# kterou tohle má opravit.
# 📌 Odhalil to až ŽIVÝ test: v simulaci jsem použil větu BEZ zdvořilostní
# předložky a vyšlo 10/10. Na skutečných větách z přepisu dal starý tvar
# 7/10, nový 10/10. Simulace je jen tak dobrá jako její vstupy.
_NA_TAZATELE = re.compile(r"\b(m[ěe]|mne|n[áa]s)\b", re.IGNORECASE)


def _tema_ze_zdrojoveho_dotazu(raw: str) -> str:
    """Téma z dotazu na zdroj — „…o hradu Trosky?" → „hradu trosky".

    ⚠️ ZÁMĚRNĚ JEN TADY, ne v `hans_recall._extract_topic`. Ta funkce je
    SDÍLENÁ (používá ji i vybavování četby) a táž úprava v ní by na korpusu
    758 replik vytáhla 108 nových „témat", z velké části šum — „o čem jsme
    se bavili" → „cem jsme se bavili", „co o sobě dokážeš říci" → „sobe
    dokayes rici". Uvnitř `/zdroje` je kontext známý (věta prošla vzory na
    dotaz po zdroji), takže stačí úzké pravidlo.
    📏 Změřeno na produkční cestě PŘED zásahem: +7 témat, 0 chybných,
    0 změněných stávajících.
    """
    try:
        m = _ZDROJ_TEMA_PAT.search((raw or "").strip())
        if not m:
            # HANS_SOURCES_TOPIC_OBJECT_V1 — první klauze věty
            for _kl in re.split(r"[?!]+|\.(?=\s+[a-zěščřžýáíéúůďťňó])|,",
                                (raw or "").strip()):
                _kl = _kl.strip().rstrip(".")
                if not _kl:
                    continue
                for _p in (_ZDROJ_TEMA_KL, _ZDROJ_OBJ_KL):
                    _m = _p.search(_kl)
                    if not _m:
                        continue
                    _sl = _m.group(1).split()
                    if (_sl and len(_sl) <= 4 and not any(
                            w.lower().strip(",.?!") in (_ZDROJ_STOP | _ZDROJ_STOP2)
                            for w in _sl)):
                        return " ".join(_sl).lower()
                break
            return ""
        slova = m.group(1).split()
        # ⚠️ Strop 4 slov je kvůli anglickým souslovím („Icon of the Seas");
        # delší úsek už bývá zbytek věty i se slovesem, ne téma.
        if not slova or len(slova) > 4:
            return ""
        if any(w.lower().strip(",.?!") in _ZDROJ_STOP for w in slova):
            return ""
        return " ".join(slova).lower()
    except Exception:
        return ""


def _zdroje_vsechna_slova(cx, q: str):
    """HANS_SOURCES_ALL_WORDS_V1 — čtení, ve kterých jsou VŠECHNA slova tématu.
    None = téma není víceslovné (volající hledá po staru); jinak seznam
    (ts, titul, url) — i prázdný."""
    from scripts.hans_recall import _fold
    slova = [_fold(w).lower() for w in re.findall(r"\w+", q or "") if len(w) >= 4]
    if len(slova) < 2:
        return None
    vzory = [re.compile(r"(?<![a-z0-9])" + re.escape(w[:max(4, len(w) - 3)]))
             for w in slova]
    nalez = []
    for ts, title, url, text in cx.execute(
            "SELECT ts, title, source_url, COALESCE(NULLIF(note,''), data, '') "
            "FROM diary WHERE event_type IN ('web_read','reading_takeaway','study_note')"):
        ft = _fold(title or "").lower()
        v_titulu = all(v.search(ft) for v in vzory)
        if v_titulu or all(v.search(ft) or v.search(_fold(text or "").lower()) for v in vzory):
            nalez.append((0 if v_titulu else 1, -float(ts or 0), ts, title, url))
    nalez.sort()
    return [(ts, title, url) for _a, _b, ts, title, url in nalez[:12]]


# HANS_BOOK_ORIGIN_V1 — sloveso ZÍSKÁNÍ věci (ne čerpání informace).
_PUVOD_KNIHY_PAT = re.compile(
    r"\b(vzal|vzala|na[šs]el|na[šs]la|sehnal|sehnala|dostal|dostala)\b"
    r"|\bodkud\s+(ji|ho|jej)\s+m", re.I)


from scripts.chat_cmd_pamet import _cmd_zdroje   # ROZDELENI_PRIKAZU_V1 — přesunuto


_HLAVNI_ZPRAVA_PAT = re.compile(r"\bhlavn[ií]\s+zpr[aá]v(a|u|ou|[eě])\b", re.I)


def dotaz_hlavni_zprava(veta: str) -> bool:
    """HANS_HLAVNI_ZPRAVA_V1 — ptá se věta na HLAVNÍ ZPRÁVU (jednotné číslo)?
    „Hlavní zprávy“ v množném čísle jsou přehled dne. Vzory /zpravy sednou dřív
    než vlastní příkaz, proto se rozhoduje až uvnitř `_cmd_zpravy`."""
    return bool(_HLAVNI_ZPRAVA_PAT.search(veta or ""))


from scripts.chat_cmd_zpravy import _cmd_zpravy   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "zpravy",
    slash_aliases=["zpravy", "zprávy", "novinky"],
    # HANS_ZPRAVY_CHAT_V1 — ZÁMĚRNĚ úzké: holé „co je nového?“ je pozdrav
    # Hansovi, ne dotaz na zprávy. Musí zaznít zprávy/noviny/svět/píšou.
    nl_patterns=[
        r"\b(?:ve|v) zpr[áa]v[áa]ch\b",
        r"\b(?:ve|v) novin[áa]ch\b",
        r"\bco (?:se )?p[íi][šs]ou\b",
        r"\bp[íi][šs]ou\s+(?:o|v|ve|na)\b",
        r"\b(?:co|jak[ée]) (?:je |jsou )?(?:nov[ée]ho|novinky)\s+(?:ve|ze|v)\s+sv[ěe]t",
        r"\b(?:nejnov[ěe]j[šs][íi]|aktu[áa]ln[íi]|dne[šs]n[íi]|hlavn[íi]) zpr[áa]v",
        r"\b(?:n[ěe]jak[ée]|jak[ée]) zpr[áa]vy\b",
        r"\bzpr[áa]vy (?:o|ohledn[ěe]|ze sv[ěe]ta)\b",   # ne „zprávy z Matrixu“
        # HANS_ZPRAVY_CO_SE_DEJE_V1 (6. 10.) — /tazatel: „co se děje v poslední
        # době zajímavého ve světě?“ šlo do volného hovoru a model si zprávy
        # vymyslel (soud v Německu kvůli AI souhrnům — v titulcích není).
        r"\bco\s+se\s+(?:\w+\s+){0,5}?d[ěe]je\b[^.?!]{0,40}\bve?\s+sv[ěe]t[ěe]\b",
        # HANS_ZPRAVY_SLEDUJES_V1 (4. 10.) — „sledujete vůbec zprávy? co jste
        # v nich dnes zaznamenal?“ šlo do volného hovoru a model zprávy i odkaz
        # VYMYSLEL. 1 336 vět deníku: 0 shod (nic neukradne).
        r"\bsleduj\w*\s+(?:v[ůu]bec\s+|n[ěe]jak[ée]\s+)?zpr[áa]vy\b",
        r"\bzpr[áa]v\w*\b[^.?!]{0,40}\b(?:zaznamenal|zachytil|zaujal[oa]?|[čc]etl|vid[ěe]l|sly[šs]el)\w*",
        r"\b(?:zaznamenal|zachytil|[čc]etl|sly[šs]el)\w*\b[^.?!]{0,30}\bve?\s+zpr[áa]v",
        # HANS_ZPRAVY_DENI_MISTO_V1 (7. 10.) — „co se DNES děje v <zemi>?“ šlo do
        # volného hovoru (shoda se zprávami pod prahem podkladu) a model vypsal
        # VYMYŠLENÉ titulky ve tvaru přehledu zpráv. Příkaz na tutéž větu vrací
        # skutečné zprávy s odkazy, na neznámé místo „nic jsem nenašel“.
        # Úzké ZÁMĚRNĚ: musí zaznít časové slovo A místo za „v/ve/na“; věta
        # o tazateli (ti, vám) a místa domácnosti, dny a TV nesednou.
        # 📏 1 356 reálných vět + 1 262 vět tazatele: 0 shod (nic neukradne).
        (r"^(?![^?!]*\b(?:ti|tob[ěe]|tebe|v[áa]m|v[áa]s)\b)"
         r"(?=[^?!]*\b(?:dnes\w*|te[ďd]|aktu[áa]ln\w*|pr[áa]v[ěe]|v[čc]era|nyn[íi]|moment[áa]ln\w*)\b)"
         r"[^?!]*\bco\s+(?:se\s+)?(?:\w+\s+){0,3}?"
         r"(?:d[ěe]je|stalo|ud[áa]lo|d[ěe]lo|je\s+(?:\w+\s+)?nov[ée]ho)\s+(?:\w+\s+){0,2}?(?:v|ve|na)\s+"
         r"(?!dom|byt|pokoj|kuchyn|ob[ýy]v|lo[žz]n|zahrad|m[ée]\b|moj|na[šs]|va[šs]|tv[éeoůu]|posledn|noci|pond|"
         r"[úu]ter|st[řr]ed|[čc]tvrt|p[áa]t|sobot|ned[ěe]l|t[ýy]d|v[íi]kend|pr[áa]c|[šs]kol|televiz|tv\b|kodi|"
         r"film|tom|t[ée]\b|tomhle|hlav|studi|den[íi]k|sv[ěe]t|zpr[áa]v|novin|okol|m[íi]stnost|kamer|pam[ěe]t|"
         r"syst[ée]m|po[čc][íi]ta|pc\b)\w{3,}"),
        # HANS_ZPRAVY_SVET_NE_DENIK_V1 — „co se dneska událo ve světě“
        r"\bco\s+se\s+(?:\w+\s+){0,2}(?:stalo|d[ěe]je|d[ěe]lo|ud[áa]lo)\w*\s+(?:\w+\s+){0,2}ve?\s+sv[ěe]t",
    ],
    handler=_cmd_zpravy,
    help_text="Co je ve zprávách — /zpravy [téma]",
)


# HANS_ZPRAVY_ODKAZY_V1 (4. 10.) — „pošli mi odkaz na Novinky, kde se o tom
# píše“ po výpisu /zpravy. 3. 10. (Matrix) taková věta nikam nevedla a model
# tvrdil, že „nemá přístup k internetu pro získání URL“ — odkazy přitom má.
# Vzor HANS_DEMAGOG_FOLLOWUP_V1: rozhoduje POSLEDNÍ Hansova replika.
_ZP_PATICKA = "Sbírám je každou hodinu z českých i zahraničních médií"
_ZP_ODKAZ_PAT = re.compile(
    r"\b(odkaz\w*|link\w*|url|adres\w*|zdroj\w*|[čc]l[áa]n(?:ek|ku|ky|k\w*)|"
    r"p[íi][šs]e\s+(?:se\s+)?o\s+tom|kde\s+(?:se\s+)?(?:to|o\s+tom)\s+p[íi][šs]\w*|"
    r"cel[ýy]\s+text|p[řr]e[čc][íi]st)\b", re.I)


def thread_zpravy(message: str, turns):
    """Argument pro /odkazy_zprav, když věta žádá odkaz po výpisu zpráv; jinak None."""
    msg = (message or "").strip()
    if not msg or len(msg) > 300 or not _ZP_ODKAZ_PAT.search(msg):
        return None
    for role, text in reversed(list(turns or [])):
        if role != "assistant":
            continue
        text = str(text or "")
        if _ZP_PATICKA not in text:
            return None
        urls = re.findall(r"https?://\S+", text)
        return "%s\x1f%s" % (" ".join(urls), msg)
    return None


from scripts.chat_cmd_zpravy import _cmd_odkazy_zprav   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "odkazy_zprav",
    slash_aliases=["odkazy_zprav"],
    nl_patterns=[],            # jen z vlákna (thread_zpravy), samo nesepne
    handler=_cmd_odkazy_zprav,
    help_text="Odkazy na články k poslednímu výpisu zpráv",
)


_DEMAGOG_STRANKA: dict = {}   # (osoba, mluvčí/dotaz) → (posun, čas) — HANS_DEMAGOG_V1

# HANS_DEMAGOG_FOLLOWUP_V1 (3. 10.) — navazující otázka po výpisu z Demagogu.
# /tazatel 3. 10.: „a co ostatní výroky?“, „výroky pana Fialy o migraci?“,
# „jaké další politiky ověřovali?“ nenesly slovo „Demagog“ → příkaz nesepnul
# a model ZKOPÍROVAL TVAR výpisu z historie se smyšlenými verdikty (3×).
# Vzor HANS_FILM_OPINION_ANAFORA_V1: rozhoduje POSLEDNÍ Hansova replika.
_DM_POKRACOVANI = re.compile(
    r"\b(dal[šs]\w*|je[šs]t[ěe]|ostatn\w*|v[íi]c|jin[éeýa]\w*|v[ýy]rok\w*|"
    r"ov[ěe][řr]\w*|politi\w*|t[ée]ma\w*|pokra[čc]\w*)\b", re.I)


def posledni_vypis_demagog(turns):
    """Mluvčí z POSLEDNÍ Hansovy repliky, je-li výpisem z Demagogu:
    "" = výpis bez mluvčího (téma), None = poslední replika výpis není."""
    for role, text in reversed(list(turns or [])):
        if role != "assistant":
            continue
        text = str(text or "")
        if "demagog.cz/vyrok/" not in text and "Demagog.cz" not in text:
            return None
        m = re.match(r"\s*([^\n—:]{3,60}?) — (?:Demagog\.cz od|k tomuhle tématu má Demagog)", text)
        return m.group(1).strip() if m else ""
    return None


def thread_demagog(message: str, turns):
    """Argument pro /demagog, když věta navazuje na výpis z Demagogu; jinak None."""
    msg = (message or "").strip()
    if not msg or len(msg) > 400 or not _DM_POKRACOVANI.search(msg):
        return None
    mluvci = posledni_vypis_demagog(turns)
    if mluvci is None:
        return None
    return ("%s %s" % (mluvci, msg)).strip() if mluvci else msg


from scripts.chat_cmd_zpravy import _cmd_demagog   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "demagog",
    slash_aliases=["demagog", "overeno", "overene"],
    # HANS_DEMAGOG_V1 — ZÁMĚRNĚ úzké: slovo „demagog“, nebo dotaz na OVĚŘENÍ
    # výroku/politika. Obecné „je pravda, co řekl X?“ ne (X bývá člen
    # domácnosti). Tykání i vykání: ověřil/ověřoval/ověřujete…
    nl_patterns=[
        r"\bdemagog",
        r"\bov[eě][řr](il[ia]?|ovali?|uje|ujete|ujete|ili)\b.{0,40}\b(v[ýy]rok\w*|politik\w*)",
        # HANS_DEMAGOG_FOLLOWUP_V1 — „ověřené výroky“ (tazatel 3. 10., B8)
        r"\bov[eě][řr]en[éeý]\w*\s+v[ýy]rok",
        r"\bfact[- ]?check\w*",
    ],
    handler=_cmd_demagog,
    help_text="Ověřené výroky politiků z Demagog.cz — /demagog <jméno nebo téma>",
)


from scripts.chat_cmd_zpravy import _cmd_hlavnizprava   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "hlavnizprava",
    slash_aliases=["hlavnizprava", "hlavni"],
    # ZÁMĚRNĚ jen jednotné číslo: „jaké jsou hlavní zprávy“ je přehled dne (/zpravy).
    nl_patterns=[
        r"\bhlavn[ií]\s+zpr[aá]v(a|u|ou|[eě])\b",
    ],
    handler=_cmd_hlavnizprava,
    help_text="Hlavní zpráva posledních dvou dnů, jak jsem ji vybral — /hlavnizprava",
)


from scripts.chat_cmd_dum import _cmd_sleva   # HANS_LETAKY_V1
register(
    "sleva",
    slash_aliases=["sleva", "slevy", "letaky"],
    nl_patterns=[
        r"\bco\b.{0,30}\bv\s+akci\b",
        r"\bjak[eé]\s+(?:jsou|m[aá]me)\b.{0,20}\bslevy\b",
        r"\bslevy\s+z\s+let[aá]k",
        r"\b(?:p[rř]idej|hl[ií]dej|sleduj|odeber|sma[zž])\b.{0,60}\bslev(?:u|y)?\b",
        r"\bseznam\s+(?:hl[ií]dan[yý]ch\s+)?slev\b",
    ],
    handler=_cmd_sleva,
    help_text="Slevy z letáků podle hlídaného seznamu — /sleva (přidej …, odeber …, teď)",
)


register(
    "zdroje",
    slash_aliases=["zdroje", "odkazy", "literatura", "zdroj", "odkaz"],
    # HANS_SOURCES_VYKANI_V1 (6.9.) — vzory znaly JEN TYKANI a jen uzky
    # okruh formulaci. Zmereno: tykani 6/6, VYKANI 0/6 — a prave cizi clovek
    # Hansovi vyka. [[test-both-grammatical-persons]]
    # Dolozeno 6. 9.: "dokazal byste mi ukazat, odkud presne jste to cetl,
    # tedy nejaky odkaz nebo nazev?" -> prikaz se nespustil, odpovedel LLM
    # frazi "nemam pristup k externim zdrojum, moje znalosti pochazeji
    # z trenninku". To je NEPRAVDA: v deniku je 473 radku se `source_url`
    # (467 z nich web_read), takze Hans ty odkazy realne MA.
    #
    # ⚠️ Klauzule v promptu uz zuzena JE (HANS_SOURCE_QUERY_V1, 17. 7.) a
    # dokonce vyslovne rika "Neodbyvej frazi 'nemam pristup k externim
    # zdrojum'". Model ji presto pouzil — proto se to resi BRANOU, ne dalsi
    # vetou v promptu.
    #
    # 📏 ZMERENO na 1362 realnych replikach: +9 nove zachycenych,
    # 0 ukradenych jinemu prikazu, 0 ztracenych.
    # ⛔ Vzory na odkaz zamerne vyzaduji 2. osobu (das/date/mas/mate/posles),
    # aby neunesly opacny smer — kdyz uzivatel odkaz SAM POSILA
    # ("posilam ti odkaz", "precti si tenhle odkaz"), patri to do cteni webu,
    # ne do vypisu zdroju [[read-and-remember-links]].
    # Samostatny vzor na "odkaz(y) na clanek" tu BYL a je ZAMERNE PRYC:
    # chytal prave "posilam ti odkaz na clanek", a pritom je nadbytecny —
    # vsechny realne dotazy ("mas odkaz na clanek…", "muzes poslat odkazy
    # na clanky…") uz pokryvaji vzory s 2. osobou. Overeno na korpusu.
    nl_patterns=[
        # HANS_SOURCES_BODY_V1 (9. 9.) — „odkud máš/máte INFORMACE o X".
        # Tykací tvar kradlo `/studium` (`_ORIGIN_PAT`), vykací nechytal
        # NIKDO a padal na LLM. Obě osoby schválně v jednom vzoru
        # [[test-both-grammatical-persons]].
        r"\bod[kK]ud\s+(m[áa][sš]|m[áa]te)\s+(ty\s+|tyhle\s+)?"
        r"(informace|inform\w+|[úu]daje|poznatky)\b",
        r"\bodkud\s+(\w+\s+){0,2}(jsi|si|to|jste)\b.{0,18}"
        r"(čerpal|cerpal|m[áa][šs]|m[áa]te|vz[áa]l|v[íi][šs]|v[íi]te|[čc]etl|[čc]etla|dozv[ěe])",
        # HANS_VYKANI_DLOUHA_OTAZKA_V1 (9. 9.) — vzor chtel „cerpate" HNED
        # za „odkud". Doloženo rozhovorem: „…odkud PŘESNĚ TYTO INFORMACE
        # čerpáte?" propadlo, LLM router zvolil `/rozhovory` a druhá brána
        # to zamítla („ptá se na svět") — takže Hans řekl, že zdroj nemá,
        # ačkoli ho v deníku s odkazem MÁ. Povoleno až 4 slova mezi.
        r"\bodkud\s+(\w+\s+){0,4}[čc]erp[áa]([šs]|te)\b",
        # HANS_SOURCES_ODKUD_ZNAS_V1 (7. 10.) — „odkud znáš/víš X“ bez
        # „to/jsi“ propadlo: router zvolil /cetl, druhá brána ho zamítla
        # („ptá se na svět“) a Hans řekl, že odkaz nemá, ačkoli článek
        # s odkazem četl týž den. 📏 2 369 reálných vět: +3 („odkud víš
        # o <osobnosti>?“ 2×, dřív abstinence; „odkud víš, že tu někdo je?“).
        r"\bodkud\s+((to|ho|ji|je|jej|tohle|o\s+tom)\s+)?"
        r"(zn[áa][šs]|zn[áa]te|v[íi][šs]|v[íi]te)\b",
        r"\b(z\s+)?[čc]eho\s+(jsi|si|jste)\s+.{0,10}(čerpal|cerpal|vych[áa]zel)",
        r"\b(z\s+)?[čc]eho\s+(studuje[šs]|studujete)\b",
        r"\b(d[áa][šs]|d[áa]te|m[áa][šs]|m[áa]te|po[šs]le[šs]|po[šs]lete)"
        r"\s+(mi\s+)?(n[ěe]jak[ýéya]\s+)?odkaz",
        r"\bm[uů][žz]e([šs]|te)\s+(mi\s+)?(poslat|uk[áa]zat|d[áa]t)\s+.{0,14}odkaz",
        # HANS_SOURCES_PRONOUN_V1 (22. 9.) — zajmeno bylo jen "to", takze
        # "kde jste HO cetl?" propadlo az do volneho hovoru a Hans si
        # vymyslel cas zaznamu. Doloženo zivym testem 22. 9.
        # 📏 Zmereno na 1 569 realnych vetach: PRIRUSTEK 0 — nic to
        # nekrade a nic neztraci; je to pokryti tvaru, ktery se zatim
        # nevyskytl, za nulovou cenu.
        r"\bkde\s+(jsi|si|jste)\s+((to|ho|je|ji|jej|tohle|tenhle)\s+)?"
        r"(četl|cetl|na[šs]el|na[šs]la|vzal|vzala|dozv[ěe]d[ěe]l)",
        r"\bjak[ýy]\s+(je\s+)?(ten\s+)?zdroj",
        r"\b(uka[žz]|uka[žz]te)\s+(mi\s+)?(sv[ée]\s+)?zdroj",
        r"\bjak[ée]\s+(m[áa][šs]|m[áa]te)\s+.{0,12}zdroj",
        r"\bposli\s+(mi\s+)?odkaz|\bpo[šs]li\s+(mi\s+)?odkaz",
    ],
    handler=_cmd_zdroje,
    help_text="Odkazy na to, co jsem četl (přímo z deníku): odkud jsi čerpal?",
)


def _ukazka_casu(fakta: list) -> str:
    """HANS_EXACT_SAMPLE_FROM_DATA_V1 — ukázka formátu času VZATÁ Z FAKT.
    Prázdný řetězec, když ve faktech žádný čas není (pak není co ukazovat
    ani co opsat)."""
    try:
        m = re.search(r"\d{1,2}:\d{2}(?:\s*[–-]\s*\d{1,2}:\d{2})?",
                      " ".join(str(x) for x in (fakta or [])))
        if m:
            return ("\nČASY opiš ZNAK PO ZNAKU přesně tak, jak jsou ve "
                    "faktech (tedy „%s\", ne slovy). " % m.group(0))
    except Exception:
        pass
    return "\n"


def _hlas_nad_fakty(cfg, fakta: list, pokyn: str, uvod: str,
                    min_len: int = 60, timeout: int = 90) -> str:
    """HANS_HLAS_NAD_FAKTY_V1 (9. 9.) — pust HOTOVÁ fakta Hansovým hlasem.

    Vzniklo z `/dnes`, kde tenhle krok žil natvrdo. Nález uživatele 9. 9.:
    `/videl` vracel SYROVÝ faktový řádek („V domě jsem dnes viděl: paní
    <Jméno> (09:29–10:52).") — čísla i špatný pád, ne Hansova řeč.
    Sdílené, aby se obě cesty nerozešly.

    Vrací '' když mozek není nebo výstup nestojí za to — volající pak vrátí
    svůj deterministický text (deferral-safe, vzor `_night_reflection`).
    ⚠️ `min_len` je parametr schválně: `/dnes` píše odstavec, `/videl` větu.
    """
    if not fakta:
        return ""
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(cfg):
            return ""
    except Exception:
        pass
    try:
        from scripts.ollama_client import ollama_generate
        from scripts.hans_persona import persona_core
        try:
            core = persona_core(cfg, with_address=False)
        except Exception:
            core = ""
        model = (cfg.get("models", {}) or {}).get("dialog", "hans-czech:latest")
        system = (core + "\n\n" if core else "") + pokyn + (
            # HANS_DAY_AT_HOME_EXACT_V1 (7.8.) — hlasový krok komolil PŘESNÁ
            # data: „10:03–10:15" přepsal na „mezi desátou minutou třetí
            # a čtvrtou minutou" a počet 23 na „dvacet čtyři krát". Persona
            # smí formulovat, ale ne přepočítávat.
            # HANS_DAY_AT_HOME_EXACT_V1 (7. 8.) — hlasovy krok komolil PRESNA
            # data: „10:03\u201310:15" prepsal na „mezi desatou minutou treti
            # a ctvrtou minutou" a pocet 23 na „dvacet ctyri krat". Persona
            # smi formulovat, ale ne prepocitavat.
            # ⚠️ ZNENI JE ZAMERNE DOSLOVNE TAKOVE, JAKE BYLO ZMERENE.
            # 9. 9. jsem ho pri vytahovani do sdilene funkce prepsal „lip"
            # a rozbil: bez teto ukazky psal model casy SLOVY (0/3 bezu),
            # a nepomohl ani tvar „HH:MM" ani ukazka vzata z fakt.
            # ⛔ NEPREFORMULOVAT. Kdyby ukazka zase zacala unikat do odpovedi
            # jako vymysleny udaj, resi to `_ma_osoby` (rubrika o lidech se
            # neda, kdyz o nich fakta nic nemaji) a `HANS_HLAS_CAS_GUARD_V1`,
            # ne dalsi prepis teto vety.
            "\nČASY A ČÍSLA opiš PŘESNĚ tak, jak jsou ve faktech (např. "
            "„10:03\u201310:15\", „23\") — nepřepisuj je slovy ani "
            "nezaokrouhluj. Oslovení „pan/paní\" u jmen zachovej, jak je "
            "uvedeno. Vyjdi POUZE z faktů níže; co v nich není, se nestalo "
            "— nic si nepřimýšlej. Žádný nadpis, žádné uvozovky, "
            "žádné odrážky.")
        out = ollama_generate(
            model, uvod + NL_RUNTIME.join(fakta) + "\n\nShrň to pánovi.",
            system=system, config=cfg, timeout=timeout)
        txt = (out or "").strip().strip('"')
        # HANS_HLAS_CAS_GUARD_V1 (9. 9.) — POKYNEM TO NEJDE. Model časy
        # přepisuje slovy („od devíti hodin dvaceti devíti minut") a změřeno
        # je, že nepomůže ani ukázka formátu („HH:MM", 3/3 slovy), ani ukázka
        # vzatá z fakt (0/3 číselně). `HANS_DAY_AT_HOME_EXACT_V1` (7. 8.) to
        # řešil ukázkou v promptu, jenže ta se 9. 9. propsala do odpovědi jako
        # VYMYŠLENÝ ÚDAJ. Prompt je tedy na obě strany slepá ulička.
        # Řešení je OVĚŘENÍ, ne pokyn: když fakta čas obsahovala a hlasový
        # výstup ani jeden nemá, výstup se ZAHODÍ a volající vrátí svou
        # deterministickou větu. Horší formulace je lepší než zkomolený údaj
        # — a tahle pojistka by chytila i původní vadu ze 7. 8.
        if txt and len(txt) >= min_len:
            _fakta_txt = " ".join(str(x) for x in fakta)
            if re.search(r"\d{1,2}:\d{2}", _fakta_txt) and \
                    not re.search(r"\d{1,2}:\d{2}", txt):
                _log.info("hlas: výstup ZAHOZEN — fakta měla čas HH:MM, "
                          "odpověď žádný (model ho přepsal slovy)")
                return ""
            return txt[:1200]
    except Exception as e:
        _log.warning("hlasový krok selhal (%s) — vracím fakta", e)
    return ""


from scripts.chat_cmd_pamet import _cmd_videl   # ROZDELENI_PRIKAZU_V1 — přesunuto


def _PRIVACY_REFUSAL_TXT() -> str:
    """Sdilene odmitnuti z `hans_recall` — nekopirovat text, at se nerozejde."""
    try:
        from scripts.hans_recall import _PRIVACY_REFUSAL
        return _PRIVACY_REFUSAL
    except Exception:
        return ""


register(
    "videl",
    slash_aliases=["videl", "viděl"],
    nl_patterns=[
        # jen mě/nás — obecné „kdy jsi X viděl" (film, věc) patří LLM,
        # špatný deterministický únos by byl horší než žádný. Jiné osoby
        # jdou přes /videl <jméno> (resolve person_name_forms v handleru).
        r"\bkdy\s+(jsi|si)\s+(m[ěe]|n[áa]s)\s+(naposledy\s+)?vid[ěe]l",
        r"\bvid[ěe]l\s+(jsi|si)\s+m[ěe]\s+(dnes|včera|naposledy)",
    ],
    handler=_cmd_videl,
    help_text="Kdy jsem koho naposledy viděl (přímo z deníku person_seen)",
)


from scripts.chat_cmd_pamet import _cmd_dnes   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "dnes",
    slash_aliases=["dnes", "dnesek", "den"],
    nl_patterns=[
        # HANS_ZPRAVY_SVET_NE_DENIK_V1 (4. 10.) — „co se dneska událo ve světě“
        # (hlas 3. 10.) je dotaz na zprávy, ne na deník domu
        r"co\s+se\s+(dnes|dneska|d[ňn]es)\w*\s+(d[ěe]lo|stalo|ud[áa]lo)(?!.*\b(?:ve?\s+sv[ěe]t|v\s+[čc]esk|ve?\s+zpr[áa]v|v\s+republi))",
        r"co\s+se\s+(d[ěe]lo|stalo|ud[áa]lo)\s+(dnes|dneska)(?!.*\b(?:ve?\s+sv[ěe]t|v\s+[čc]esk|ve?\s+zpr[áa]v|v\s+republi))",
        r"co\s+(bylo|se\s+d[ěe]lo)\s+(dnes\s+)?(doma|v\s+dom[ěe])",
        r"jak[ýy]\s+byl\s+(dnes(n[íi])?)?\s*den",
        r"shr[nň]\s+(mi\s+)?(dnes(ek|n[íi]\s+den)?)",
    ],
    handler=_cmd_dnes,
    help_text="Co se dnes dělo v domě (z deníku): co se dnes dělo?",
)


from scripts.chat_cmd_dum import _cmd_rezim   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "rezim",
    slash_aliases=["rezim", "režim", "spis", "spíš"],
    nl_patterns=[
        r"\b(jsi|js|nejsi)\s+(te[ďd]\s+)?(v\s+)?(re[žz]imu\s+)?sp[áa]nk",
        r"\bsp[íi][sš]\s*\?",
        r"\bnesp[íi][sš]\b",
        r"\b(jsi|nejsi)\s+vzh[uů]ru",
        r"\bne?m[ěe]l\s+bys?\s+b[ýy]t\s+(v\s+)?(re[žz]imu\s+)?sp[áa]nk",
        r"\bv\s+jak[ée]m\s+jsi\s+re[žz]imu",
        r"\bhl[íi]d[áa][sš]\s+(te[ďd]\s+)?(d[uů]m|dom)",
    ],
    handler=_cmd_rezim,
    help_text="V jakém jsem režimu (spánek, hlídání) — přímo ze stavu",
)


# HANS_FILM_OPINION_ANSWER_V1 (22. 9.) — DOTAZ NA OBLIBU NENI DOTAZ NA VYPIS.
# Uzky zamerne: vylucuje DOPORUCENI ("doporucil bys mi film?") i dotaz na
# CIZI vkus ("myslis, ze by ji bavil ten film o Bondovi?") — obe tridy se
# v korpusu vyskytuji a ani jedna sem nepatri.
# 📏 Zmereno na 2 327 vetach (1 569 realnych chatu + korpus 758): sedne na
# JEDNU realnou vetu ("jaky je tvuj nejoblibenejsi film?"), kontroly 9/9.
_FILM_OBLIBA_PAT = re.compile(
    r"(?:\b(jak[ýy]|kter[ýýy]|kter[éey])\b[^.?!]{0,30}\b(film|ser[ií][aá]l)\w*"
    r"[^.?!]{0,30}\b(l[ií]bil|zaujal|bavil)\b"
    r"|\bnejobl[ií]ben[eě]j[sš][ií]\b[^.?!]{0,12}\b(film|ser[ií][aá]l)"
    r"|\b(film|ser[ií][aá]l)\w*[^.?!]{0,20}\bse\s+(ti|v[aá]m)\s+(l[ií]bil|zaujal)\b)",
    re.I)
_FILM_TRETI_PAT = re.compile(r"\b(by|mysl[ií][sš])\b[^.?!]{0,30}\bbavil\b", re.I)


# HANS_FILM_RECOMMEND_V1 (24. 9.) — žádost o DOPORUČENÍ filmu. Dřív žádný vzor:
# věta šla do volného hovoru (Hans si jednou vymyslel „The Avengers z roku
# 1950“) nebo k agentovi, kde film vybíral model. Změřeno na 2 354 reálných
# větách: 5 shod, všech 5 skutečné žádosti, 0 falešných. Obliba („jaký film se
# ti líbil“) i cizí vkus („bavil by ji ten film?“) jsou jiné třídy.
_DOPORUC_FILM_PAT = re.compile(
    r"\bdoporu[cč]\w*\b[^?.!]{0,40}\bfilm|\bfilm\w*\b[^?.!]{0,30}\bdoporu[cč]"
    r"|\btip\w* na (?:nejak\w* )?film"
    r"|\b(?:co|jaky film)\b[^?.!]{0,20}\b(?:bych|bychom|bysme|mam|mame|mel|mela|meli)\b"
    r"[^?.!]{0,15}\b(?:pustit|podivat|koukat|videt)\b[^?.!]{0,15}(?:film|vecer|dnes)")


def _je_zadost_o_doporuceni_filmu(veta: str) -> bool:
    v = _fold_diacritics(veta or "").lower()
    # „doporučuješ TEN film, co jsme viděli?“ = názor na konkrétní film
    if re.search(r"\bt(?:en|enhle|ento|ahle|ohle)\s+film", v):
        return False
    return bool(_DOPORUC_FILM_PAT.search(v))


def _doporuc_film(handler, name, znamy: bool) -> str:
    """HANS_FILM_RECOMMEND_V1 — film z knihovny TOUŽ logikou jako proaktivní
    nabídka (`hans_idle._pick_next_film`: žánry a oblíbené filmy tazatele,
    občas opakované zhlédnutí) + Hansův vlastní názor, jinak první věta děje.
    '' = nelze (Kodi/idle nedostupné) → volající pokračuje dál."""
    hi = getattr(handler, "_hans_idle", None)
    if hi is None or getattr(hi, "kodi", None) is None:
        return ""
    try:
        m = hi._pick_next_film([name] if (name and znamy) else [], hi._fcfg())
    except Exception as e:
        _log.debug("doporuceni filmu: vyber selhal: %s", e)
        return ""
    if not m or not (m.get("title") or "").strip():
        return ""
    t = m["title"].strip()
    _pop = [str(m["year"])] if m.get("year") else []
    _pop += [g for g in (m.get("genre") or [])[:2] if g]
    out = "Z naší knihovny bych navrhl „%s“%s." % (
        t, (" (%s)" % ", ".join(_pop)) if _pop else "")
    try:
        from scripts.hans_recall import nazor_k_filmu
        _n = nazor_k_filmu(_recall_db(handler), t)
    except Exception:
        _n = None
    if _n:
        out += " " + _n
    else:
        try:
            from scripts.hans_entities import _first_sentence
            _d = _first_sentence((m.get("plot") or "").strip(), 240)
        except Exception:
            _d = ""
        if _d:
            out += " " + _d
    if znamy:
        out += " Když řeknete „pusť %s“, pustím ho." % t
    _log.info("HANS_FILM_RECOMMEND_V1: doporučuji %r (%s)", t, name)
    return out


# HANS_FILM_SIMILAR_V1 (25. 9.) — „něco podobného jako X“ → filmy TÉHOŽ REŽISÉRA,
# které knihovna nemá (Wikidata přes IMDb ID z Kodi, `hans_film_podobny`).
# Zadání uživatele: jen na požádání; známému i nabídka z Webshare ve stejném
# výpisu jako `HANS_KODI_WEBSHARE_NABIDKA_V1`. Na 1 729 reálných větách vzor
# nesedl ani jednou (0 falešných, ale i 0 dosavadních žádostí).
_PODOBNY_FILM_PAT = re.compile(
    r"\b(?:film\w*|neco\w*|nejak\w*|tip\w*)\b[^?.!]{0,25}\bpodobn\w*"
    r"|\bpodobn\w*\s+(?:film\w*|neco\w*)"
    r"|\bod\s+(?:stejn\w+|t\w+\s+sam\w+)\s+rezis[eé]r")


def _je_zadost_o_podobny_film(veta: str) -> bool:
    return bool(_PODOBNY_FILM_PAT.search(_fold_diacritics(veta or "").lower()))


def _podobny_film(handler, name, znamy: bool, veta: str) -> str:
    """HANS_FILM_SIMILAR_V1 — tip na filmy téhož režiséra mimo knihovnu."""
    from scripts import hans_film_podobny as fp
    hi = getattr(handler, "_hans_idle", None)
    kodi = getattr(hi, "kodi", None) if hi is not None else None
    if kodi is None:
        return "Knihovnu filmů teď nevidím, zkuste to prosím za chvíli."
    dotaz = fp.nazev_z_vety(veta)
    try:
        filmy = fp.knihovna(kodi)
        film = fp.najdi_v_knihovne(filmy, dotaz) if dotaz else None
        if dotaz and film is None:
            q, cs = fp.qid_podle_nazvu(dotaz)
            if q:
                film = {"_qid": q, "title": cs}
        if not dotaz:
            np = kodi.get_now_playing()
            if np and (np.get("type") in (None, "", "movie")):
                film = np
        if film is None:
            if dotaz:
                return ("Film „%s“ jsem nenašel ani v knihovně, ani na Wikidatech. "
                        "Zkuste mi napsat jeho přesný název." % dotaz)
            return "Podobný jako který film? Napište mi prosím jeho název."
        rez, tipy = fp.podobne(film, filmy)
    except fp.Nedostupne as e:
        _log.info("HANS_FILM_SIMILAR_V1: Wikidata nedostupná: %s", e)
        return "Na Wikidata se teď nedostanu, zkuste to prosím za chvíli."
    nazev = (film.get("title") or "").strip() or dotaz
    rok = film.get("year") or ""
    hlava = "„%s“%s" % (nazev, (" (%s)" % rok) if rok else "")
    if not rez:
        return ("U filmu %s nemám na Wikidatech režiséra, takže podobný podle "
                "něj nenajdu." % hlava)
    if not tipy:
        return ("%s — režie %s. Další jeho známé filmy už v knihovně máme."
                % (hlava, rez))
    radky = "\n".join("%d. %s%s" % (i, t["nazev"], (" (%s)" % t["rok"]) if t["rok"] else "")
                      for i, t in enumerate(tipy, 1))
    out = ("%s — režie %s. Další filmy stejného režiséra, které v knihovně "
           "nemáme:\n%s" % (hlava, rez, radky))
    _log.info("HANS_FILM_SIMILAR_V1: %r → %s", nazev, [t["nazev"] for t in tipy])
    # Webshare jen známému (stažení je mutující akce, `HANS_STRANGER_NO_MUTATE_V1`)
    # a jen když je nastavený; chyba Webshare nesmí shodit samotný tip.
    if not znamy:
        return out
    try:
        cfg = getattr(handler, "config", {}) or {}
        wc = (cfg.get("webshare", {}) or {})
        from scripts import hans_webshare as _ws
        if wc.get("enabled", True) and _ws.nastaveno(cfg):
            for t in tipy[:2]:
                nalezy = _ws.hledej(cfg, t["nazev"], limit=int(wc.get("limit", 25)))
                if nalezy:
                    zapamatuj_nalezy(name or "", t["nazev"], nalezy)
                    return (out + "\n\nNa Webshare jsem k „%s“ našel tohle "
                            "(kvalitu odhaduji z názvu souboru):\n%s"
                            "\n\nStáhnu který? Stačí /hledani stahni <číslo>."
                            % (t["nazev"], _ws.vypis(nalezy, int(wc.get("vypis_kolik", 8)))))
            out += "\n\nNa Webshare jsem je nenašel."
    except Exception as _we:
        _log.debug("HANS_FILM_SIMILAR_V1 webshare: %s", _we)
    return out


def _je_dotaz_na_oblibu_filmu(veta: str) -> bool:
    """HANS_FILM_OPINION_ANSWER_V1 — pta se na JEHO oblibu, ne na doporuceni?"""
    v = str(veta or "")
    if not _FILM_OBLIBA_PAT.search(v):
        return False
    if _FILM_TRETI_PAT.search(v) or re.search(r"doporu[čc]", v, re.I):
        return False
    return True


# HANS_FILM_OPINION_ANAFORA_V1 (23. 9.) — „a který se ti z nich líbil
# nejvíc?“ po výpisu filmů. Věta slovo „film“ nenese, takže `/film` nesepne
# a šla přes self_state (Hans si estetiku vymyslel). Rozhoduje VLÁKNO:
# přesměruje se, jen když předchozí Hansova replika JE výpis filmů
# (`film_list_titles`) — „z nich“ o knihách zůstane beze změny.
_ANAFORA_OBLIBY_PAT = re.compile(
    r"\b(l[ií]bil\w*|bavil\w*|zaujal\w*)\b", re.I)
_ANAFORA_ODKAZ_PAT = re.compile(
    r"\bz\s+(nich|t[ěe]ch|toho)\b|\bkter[ýyáaée]\b|\bnejv[íi]c\b|"
    r"\bnejl[ée]p\b", re.I)


def je_anafora_obliby(veta: str) -> bool:
    v = str(veta or "")
    if len(v) > 120 or not _ANAFORA_OBLIBY_PAT.search(v):
        return False
    if not _ANAFORA_ODKAZ_PAT.search(v):
        return False
    # jiný druh díla ve větě = jiné téma, ne anafora na výpis filmů
    if re.search(r"knih|obraz|hudb|p[íi]s[ní]|skladb|[čc]l[áa]n", v, re.I):
        return False
    return not (_FILM_TRETI_PAT.search(v) or re.search(r"doporu[čc]", v, re.I))


def posledni_vypis_filmu(turns) -> list:
    """Tituly z POSLEDNÍ Hansovy repliky, je-li výpisem filmů; jinak []."""
    try:
        from scripts.hans_recall import film_list_titles
        for role, text in reversed(list(turns or [])):
            if role == "assistant":
                return film_list_titles(text)
    except Exception:
        pass
    return []


def thread_film_opinion(message: str, turns) -> bool:
    """HANS_FILM_OPINION_ANAFORA_V1 — patří věta na /film díky vláknu?"""
    return bool(je_anafora_obliby(message) and posledni_vypis_filmu(turns))


# HANS_VIDEL_UPRESNI_V1 (23. 9.) — hole "co jsi/jste (dnes) videl?" je
# viceznacne: film, nebo co Hans zahledl kamerou? Navrh uzivatele: zeptat
# se, ne hadat. Odpoved "film" jde na /film, "kamerou" k modelu, ktery
# odpovi ze ziveho stavu (overeno zive 23. 9.). 📏 V 2 342 realnych vetach
# 0 vyskytu — realne dotazy na film nesou slovo "film"; je to pojistka.
_VIDEL_HOLY_PAT = re.compile(
    r"^\W*(?:a\s+|tak\s+)?co\s+(?:jsi|sis|jste)\s+"
    r"(?:(?:dnes\w*|v[čc]era|naposledy|te[ďd])\s+)?vid[ěe]l\w*\s*\??\W*$",
    re.I)


from scripts.chat_cmd_dum import _cmd_film   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "film",
    slash_aliases=["film", "filmy"],
    nl_patterns=[
        # HANS_FILM_SIMILAR_V1 (25. 9.) — „něco podobného jako X“. SMĚROVÁNÍ chce
        # filmový kontext: holé „něco podobného“ poslalo na /film i „zažil jsi
        # někdy něco podobného?“. Uvnitř /film pak stačí široký predikát.
        r"\bfilm\w*\b[^?.!]{0,30}\bpodobn\w*|\bpodobn\w*\b[^?.!]{0,30}\bfilm",
        r"\b(?:pod[ií]vat|koukat|kouknout)\b[^?.!]{0,30}\bpodobn\w*|\bpodobn\w*\b[^?.!]{0,30}\b(?:pod[ií]vat|koukat|kouknout)\b",
        r"\bod\s+(?:stejn\w+|t\w+\s+sam\w+)\s+re[zž]is[eé]r",
        # HANS_FILM_RECOMMEND_V1 (24. 9.) — žádost o doporučení filmu
        r"\bdoporu[cč]\w*\b[^?.!]{0,40}\bfilm",
        r"\bfilm\w*\b[^?.!]{0,30}\bdoporu[cč]",
        r"\btip\w* na (?:n[eě]jak\w* )?film",
        r"\b(?:co|jak[yý] film)\b[^?.!]{0,20}\b(?:bych|bychom|bysme|m[aá]m|m[aá]me|m[eě]l|m[eě]la|m[eě]li)\b[^?.!]{0,15}\b(?:pustit|pod[ií]vat|koukat|vid[eě]t)\b[^?.!]{0,15}(?:film|ve[cč]er|dnes)",
        # HANS_COUNT_FILMS_BOOKS_V1 (14. 9.) — pocet videnych filmu. SLOVESO je
        # povinne: bez nej sedl vzor i na „kolik stoji ten film v kine?“.
        # Na 2 248 realnych vetach chyti jen doložený dotaz z 13. 8.
        r"\bkolik\s+(?:\w+\s+){0,3}film\w*[^.?!]{0,30}\b(?:vid[\u011be]l|koukal|sledoval|zhl[\u00e9e]dl)",
        r"\bkolik\s+(?:jsi|jste|sis)\s+(?:u[\u017ez]\s+)?(?:vid[\u011be]l|koukal|sledoval|zhl[\u00e9e]dl)\w*\s+(?:\w+\s+)?film",
        r"posledn[ií].{0,10}film",
        # HANS_FILM_OPINION_ANSWER_V1 (22. 9.) — "jaky je tvuj nejoblibenejsi
        # film?" nesedlo na ZADNY vzor a padalo do volneho hovoru, kde si Hans
        # oblibeny film VYMYSLEL (doloženo 22. 9.). Handler na to ma vlastni
        # vetev z `movie_opinion`, takze se to sem pusti zamerne.
        r"\bnejobl[ií]ben[eě]j[sš][ií]\b[^.?!]{0,12}\b(film|ser[ií][aá]l)",
        # HANS_FILM_QUERY_BOUNDARY_V1 (2.9.) — `\b` je tu NUTNA, ne kosmetika:
        # bez ni „jak[ýy]" matchne uvnitr slova „NEjaky", takze dotazovy vzor
        # spolknul ZADOST O SPUSTENI. Doloženo rozhovorem: „pust mi nejaky
        # film" → vypis, co se naposledy hralo. Tim se navic nikdy nedostane
        # ke slovu agentni `kodi_play_film` (a jeho HANS_KODI_NO_TITLE_V1).
        # Zmereno na 1028 realnych vetach: 7 zasahu → 1, a tou jedinou
        # zbylou je „jaky film jsi videl naposled?" (spravne). Sest, ktere
        # odpadly, jsou zadosti o spusteni („muzes pustit na kodi nejaky
        # film?"), dotazy na bezici prehravani („je pusteny nejaky film?")
        # a vypraveni — ani jedna neni dotaz na to, co Hans videl.
        r"\bjak[ýy].{0,10}film",
        # HANS_VIDEL_UPRESNI_V1 (23. 9.) — + vykani (`jste`); "videl"
        # s kamerovym dovetkem ("kamerou", "venku", "z okna") uz neni film
        # (driv i "co jsi videl kamerou?" vratilo vypis filmu).
        r"co\s+(jsi|sis|jste)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
        r"(vid[ěe]l(?!\w*\s+(?:kamer|venku|z\s+okna|za\s+oknem|na\s+ulic))"
        r"|koukal|sledoval|d[íi]val)",
        r"co\s+jsem?\s+(dnes\w*\s+)?(vid[ěe]l|koukal|sledoval)",
        r"\bfilm\w*\s+(jsi|sis)\s+(vid|koukal|sledoval)",
    ],
    handler=_cmd_film,
    help_text="Jaký film/pořad jsem viděl (přímo z deníku kodi_playing)",
)


# „s <jménem>“ / „se <S…/Z…/Š…/Ž…>“ a jméno v 7. pádě (-ou/-em/-ím). Holé „se“
# je zvratné („proč se <jméno> musela…“) — změřeno na 1 613 větách: bez
# omezení 4 falešné nálezy, všechny zvratné.
_PARTNER_PAT = re.compile(
    r"\b(?:s\s+|se\s+(?=[sszšžSZŠŽ]))([^\W\d_]{2,}(?:ou|em|ím|ým))\b",
    re.IGNORECASE)


def _partner_rozhovoru(q: str, cfg: dict, tazatel) -> str:
    """HANS_PRAVA_V1 — klíč JINÉ známé osoby za předložkou „s/se“, jinak ''."""
    try:
        from scripts.cz_names import find_known_person
    except Exception:
        return ""
    ja = str(tazatel or "").strip().lower()
    for m in _PARTNER_PAT.finditer(q or ""):
        k = find_known_person(m.group(1), cfg)
        if k and k != ja:
            return k
    return ""


from scripts.chat_cmd_pamet import _cmd_rozhovory   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "rozhovory",
    slash_aliases=["rozhovory", "rozhovor", "souhrn", "sumar", "sumář"],
    nl_patterns=[
        # Uživatel píše s překlepy a i/y („bavily", „pripomenou") → vzory musí
        # být tolerantní; jinak dotaz propadne do LLM a to si vymyslí rozhovor,
        # který se nikdy nestal (doložený případ 13.7. — smyšlený Vietnam).
        # „o čem jsme (se) bavili/mluvili/povídali (v pátek / 27. dubna)"
        r"o\s+[čc]em\s+jsme\b",
        r"co\s+jsme\s+(spolu\s+)?(prob[íi]r|[řr]e[šs]il|probir)",
        # „shrň náš rozhovor", „shrň o čem jsme mluvili"
        r"(shr[ňn]|shrnout|sum[áa]rizuj)\s+\w*\s*(n[áa][šs]\s+)?"
        r"(rozhovor|konverzac|chat)",
        # „vytáhni/vytáhneš vzpomínku z 27. dubna", „vzpomínky z minulého týdne"
        r"(vyt[áa]h\w*|uk[áa][žz]\w*|najdi)\s+\w*\s*vzpom[íi]nk",
        r"vzpom[íi]nk\w*\s+z\s+(\d|[a-zá-ž]{4,})",
        # „na co jsem se tě ptal (v pátek)"
        r"na\s+co\s+jsem\s+se\s+t[ěe]\s+ptal",
        # detail na vyžádání: „připomeň (mi) rozhovor o Maradonovi"
        r"(p[řr]ipome[ňnt]\w*|vzpome[ňn]\w*|zopakuj)\s+.{0,25}"
        r"(rozhovor|konverzac|bavil|mluvil|pov[íi]dal)",
        r"(rozhovor|konverzac\w*)\s+o\s+\w{3,}",
        r"(bavil|mluvil|pov[íi]dal)[iy]\s+jsme\s+(se\s+)?o\s+\w{3,}(?![^.?!]*\s[-–]\s)",  # HANS_ROZHOVORY_NOT_PREAMBLE_V1: „Mluvili jsme o X – <jiná otázka>“ je úvod, ne dotaz
        # „pošli detail o rychlém obědě…", „ukaž ten recept", „vypiš záznam o…"
        # Bez tohohle Hans odpověď VYGENERUJE ZNOVU (doložený případ 13.7. —
        # do receptu si přidal koriandr, který v původním zápisu nebyl).
        r"(po[šs]l\w*|uka[žz]\w*|zopakuj\w*|vypi[šs]\w*|dej\s+mi)\s+"
        r".{0,25}(detail|recept|postup|z[áa]znam|z[áa]pis)",
        r"co\s+jsi\s+(mi\s+)?(psal|napsal|poslal|[řr][íi]kal|navrhl|"
        r"doporu[čc]il)",
        r"\b(ten|tu|to)\s+(recept|postup|n[áa]vrh)\b",
    ],
    handler=_cmd_rozhovory,
    help_text="O čem jsme se bavili (z deníku): /rozhovory [v pátek | "
              "27. dubna 2026 | minulý týden]; detail: „připomeň rozhovor o X“",
)


# ─── /hlidej — hlídací režim (HANS_GUARD_V1) ─────────────────────────────────
# Prázdný dům: Hans střeží místnost a při POHYBU / NÁHLÉ ZMĚNĚ SVĚTLA pošle
# snímek na Matrix. Obchází noční spánek vidění (framy tečou vždy) a drží
# kameru v místnosti (jinak by v noci koukala do stropu).

def _guard_camera_down(handler) -> None:
    """Zapnuto během spánku → vrať kameru z stropu do místnosti."""
    try:
        hi = getattr(handler, "_hans_idle", None)
        routine = getattr(hi, "_routine", None) if hi else None
        servo = getattr(routine, "_servo", None) if routine else None
        if routine is not None and getattr(routine, "_sleeping", False) \
                and servo is not None and hasattr(servo, "manual_tilt"):
            servo.manual_tilt(0)
            _log.info("/hlidej: Hans spí → kamera vrácena do místnosti")
    except Exception as e:
        _log.warning("/hlidej: návrat kamery selhal: %s", e)


from scripts.chat_cmd_dum import _cmd_hlidej   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "hlidej",
    slash_aliases=["hlidej", "hlídej", "guard", "hlidani", "hlídání"],
    nl_patterns=[
        r"(hl[íi]dej|str[ěe][žz]|dohl[íi]žej)\s+(dům|dum|byt|m[íi]stnost|to\b)",
        r"zapni\s+(hl[íi]d[áa]n[íi]|str[áa][žz])",
        r"vypni\s+(hl[íi]d[áa]n[íi]|str[áa][žz])",
    ],
    handler=_cmd_hlidej,
    help_text="Hlídací režim: /hlidej [stop|stav] — při pohybu/změně světla "
              "pošlu snímek na Matrix",
)


# ─── /preloz — česká stopa k cizojazyčnému dokumentu (HANS_TRANSLATE_V1) ─────
# Zadání uživatele 26.8.: pustí dokument, zjistí že není česky, PAUZNE ho
# a řekne Hansovi ať ho přeloží. Hans si z Kodi zjistí, co běží, připraví
# soubor a ozve se. Uživatel si ho pustí sám (ovládat přehrávání nechce).

def cfg_of(handler):
    return getattr(handler, "config", {}) or {}


from scripts.chat_cmd_dum import _cmd_preloz   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "preloz",
    slash_aliases=["preloz", "přelož", "preklad", "překlad", "translate"],
    nl_patterns=[
        # HANS_PRELOZ_HOLY_TVAR_V1 (28.8.) — nález uživatele: na holé „preloz"
        # se Hans pustil MALOVAT (vzory čekaly za slovem ještě „to"/„ten").
        # Holý tvar dnes řeší obecné pravidlo HANS_BARE_ALIAS_V1 v
        # `parse_command` — zvláštní vzor na něj tu ZÁMĚRNĚ NENÍ, ať totéž
        # nedělají dva mechanismy, které se můžou rozejít.
        # Zůstává jen infinitiv („zkus to přeložit", „můžeš to přeložit"). Vzor je široký
        # a chytí i větu, kde překlad Kodi nemyslíš — nevadí: obsluha se napřed
        # ptá Kodi, co běží, a bez přehrávání odpoví „nic neběží", nic nespustí.
        r"p[řr]elo[žz]it\b",
        r"p[řr]elo[žz]\s+(to|ten|tenhle|tenhleten|mi|nam|n[áa]m)\b",
        r"p[řr]elo[žz]\s+(ten\s+)?(dokument|film|po[řr]ad|dokument[áa]rn)",
        r"(ud[ěe]l[aáeě]\w*|p[řr]iprav\w*)\s+(mi\s+)?[čc]esk[ou]\w*\s+(stopu|dabing|verzi)",
        r"jak\s+(to\s+)?jde\s+(ten\s+)?p[řr]eklad",
        # 28.8.: „uz mas hotovy preklad?" LLM router poslal na /dilo, takže
        # uživatel dostal odpověď o něčem jiném, zatímco překlad běžel.
        r"hotov\w*\s+p[řr]eklad|p[řr]eklad\w*\s+(u[žz]\s+)?hotov",
        # DOTAZ na hotové překlady — musí být i TADY, ne jen v obsluze:
        # nl_patterns rozhodují, jestli se k obsluze vůbec dojde.
        r"co\s+(jsi|u[žz]|v[šs]echno)\s+[\w\s]{0,25}?p[řr]elo[žz]",
        r"kter[ée]\s+[\w\s]{0,25}?p[řr]elo[žz]",
        r"seznam\s+p[řr]eklad|p[řr]elo[žz]en[éy]\s+(dokument|po[řr]ad|film)",
    ],
    handler=_cmd_preloz,
    help_text="Připrav českou stopu k tomu, co běží v Kodi: "
              "/preloz [stav|seznam]",
)

# ─── /vypnipc — ruční vypnutí PC (HANS_PC_SHUTDOWN_CMD_V1) ───────────────────
# Protějšek /wol. Vypínání samo je hotové (HANS_PC_NIGHT_SHUTDOWN: S3 suspend
# je na téhle desce rozbitý → čistý poweroff přes SSH + ranní WOL); tady se
# jen dává na povel. Ověření pingem, ať Hans netvrdí „vypnuto“ naslepo.

def _pc_ping(config: dict, timeout: int = 2) -> bool:
    import subprocess
    ip = (str(config.get("wol_pc_ip", "") or "")
          or str((config.get("pc_remote", {}) or {}).get("host", "") or ""))
    if not ip:
        return False
    try:
        return subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                              capture_output=True,
                              timeout=timeout + 2).returncode == 0
    except Exception:
        return False


from scripts.chat_cmd_dum import _cmd_vypnipc   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_dum import _cmd_vypnipc_slash   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "vypnipc",
    slash_aliases=["vypnipc", "vypnipc", "shutdown", "pcoff", "vypnout"],
    # HANS_UNIFY_ACTIONS_V1 — přirozená řeč („vypni pc") ZÁMĚRNĚ nemá regex:
    # vypnutí PC je destruktivní → padá k agentní akci pc_shutdown, která se
    # NAPŘED zeptá a vypíše, co na PC běží. Explicitní /vypnipc slash zůstává
    # okamžitý (výslovný povel = výslovný záměr). Ztráta při mozku dole = žádná
    # (PC dole ⇒ není co vypínat), slash funguje vždy.
    nl_patterns=[],
    handler=_cmd_vypnipc_slash,
    help_text="Vypnu počítač, až dodělá práci (/vypnipc hned = okamžitě)",
)


# ─── /router, /vpnprepni — HANS_ROUTER_V1 (23. 9.) ──────────────────────────
# Stav domácí sítě a přepnutí VPN serveru. JEN pro známé osoby: cizímu
# nepatří ani stav sítě, natož zásah do ní (rozhodnutí uživatele 23. 9.).
# Přirozená řeč jde přes nl_patterns, NE přes LLM router — nová agentní akce
# by změnila rozhodování routeru nad všemi větami (viz
# [[action-description-is-router-change]]); tady stačí úzké vzory.
def _router_smi(name) -> bool:
    try:
        from scripts.cz_names import is_known_person as _ikp
        return bool(name) and _ikp(name)
    except Exception:
        return False


from scripts.chat_cmd_dum import _cmd_router   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_dum import _cmd_vpnprepni   # ROZDELENI_PRIKAZU_V1 — přesunuto


_ROUTER_VEC = r"(?:router\w*|internet\w*|vpn\w*|wifi\w*|wi-fi\w*)"
_ROUTER_ZLE = (r"(?:nejde|nefunguje|nefunguj[eí]|vypad[áa]v[áa]|pad[áa]|"
               r"zlob[íi]|nejede)")
register(
    "router",
    slash_aliases=["router", "sit", "síť", "vpn"],
    nl_patterns=[
        r"\b(?:stav|zkontroluj|zkontrolujte|jak\s+je\s+na\s+tom|jak\s+jde|"
        r"co\s+d[ěe]l[áa])\b[^?.!]{0,20}\b" + _ROUTER_VEC + r"\b",
        r"\b" + _ROUTER_ZLE + r"\b[^?.!]{0,15}\b" + _ROUTER_VEC + r"\b",
        r"\b" + _ROUTER_VEC + r"\b[^?.!]{0,15}\b" + _ROUTER_ZLE + r"\b",
    ],
    handler=_cmd_router,
    help_text="Stav routeru, internetu a VPN (jen pro domácnost)",
)
register(
    "vpnprepni",
    slash_aliases=["vpnprepni", "vpnpřepni", "prepnivpn", "přepnivpn"],
    nl_patterns=[
        r"\b(?:p[řr]epni|p[řr]epn[ěe]te|zm[ěe][nň]|zm[ěe][nň]te|vym[ěe][nň]|"
        r"vym[ěe][nň]te)\b[^?.!]{0,20}\b(?:vpn\w*|server\w*)\b",
    ],
    handler=_cmd_vpnprepni,
    help_text="Přepnu VPN na další server (jen pro domácnost)",
)


from scripts.chat_cmd_dum import _cmd_zdravi   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "zdravi",
    slash_aliases=["zdravi", "zdraví", "health"],
    nl_patterns=[
        r"jak\s+(ti|se\s+ti|se\s+m[áa]š?).{0,15}(zdrav|syst[ée]m|slu[žz]b)",
        r"(zdrav|stav).{0,10}(syst[ée]m|slu[žz]eb|z[áa]vislost)",
        r"(funguje|jede|b[ěe][žz][íi]).{0,12}(ollama|comfyui|mozek|zrak)",
        r"jsi\s+v\s+po[řr][áa]dku",
    ],
    handler=_cmd_zdravi,
    help_text="Zdraví závislostí (Ollama/ComfyUI/Kodi/STT/PC/disk); /zdravi vylec = self-heal",
)


from scripts.chat_cmd_studium import _cmd_nalez   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "nalez",
    slash_aliases=["nalez", "nález", "nalezy", "nálezy", "zkousky", "zkoušky"],
    nl_patterns=[
        r"co\s+(u\s+tebe\s+)?na[šs]el\s+kol[áa][čc]",
        r"(nálezy|nalezy)\s+ze\s+zkou[šs]en[íi]",
        r"jak\s+jsi\s+(dopadl|obst[áa]l)\s+ve\s+zkou[šs]",
    ],
    handler=_cmd_nalez,
    help_text="Nálezy ze zkoušení Koláčem; /nalez N = trvalá otázka, /nalez N ne = zamítnout",
)


from scripts.chat_cmd_studium import _cmd_nastroj   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "nastroj",
    slash_aliases=["nastroj", "nástroj", "tool"],
    nl_patterns=[
        r"najdi\s+(mi\s+)?(vhodn|n[ěe]jak).{0,15}(model|n[áa]stroj|llm)",
        r"jak[ýy]\s+(model|n[áa]stroj|llm).{0,20}(pro|na)\s+",
    ],
    handler=_cmd_nastroj,
    help_text="Najdi LLM nástroj pro dílo: /nastroj <téma>; schválit/zamítnout N",
)


from scripts.chat_cmd_tvorba import _cmd_brief   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "brief",
    slash_aliases=["brief", "zadani", "zadání"],
    nl_patterns=[
        r"(p[řr]iprav|udělej).{0,15}(brief|zad[áa]n[íi]|prompt)",
    ],
    handler=_cmd_brief,
    help_text="Destiluj studium do promptu pro dílo: /brief <téma> [coder|esej|obraz]",
)


from scripts.chat_cmd_tvorba import _cmd_vytvor   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "vytvor",
    slash_aliases=["vytvor", "vytvoř", "vyrob", "artefakt"],
    nl_patterns=[
        r"vytvo[řr].{0,20}(z\s+toho|co\s+ses|nastudova|dílo|artefakt)",
    ],
    handler=_cmd_vytvor,
    help_text="Vyrob artefakt z nastudovaného: /vytvor <téma> [coder|obraz]",
)


from scripts.chat_cmd_tvorba import _cmd_prohloubit   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "prohloubit",
    slash_aliases=["prohloubit", "prohloub", "deepen"],
    nl_patterns=[],
    handler=_cmd_prohloubit,
    help_text="Návrh prohloubení studia: /prohloubit [schválit|ne|<vlastní kritika>]",
)


from scripts.chat_cmd_studium import _cmd_vhledy   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "vhledy",
    slash_aliases=["vhledy", "insights"],
    nl_patterns=[
        r"\bco\s+sis?\s+vši?ml?\s+(u\s+sebe|na\s+sob[ěe])",
        r"\btv[ée]\s+vhledy?\b",
        r"\bm[áa]š?\s+n[ěe]jak[éy]\s+vhled",
    ],
    handler=_cmd_vhledy,
    help_text="Co si Hans všiml ve vlastních datech (offline/herní mód); /vhledy teď = spusť rozbor hned",
)


from scripts.chat_cmd_studium import _cmd_experiment   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "experiment",
    slash_aliases=["experiment", "footgun"],
    nl_patterns=[],
    handler=_cmd_experiment,
    help_text="Experiment: zapnu si herní mód na N minut (default 5), pak auto-resume",
)


from scripts.chat_cmd_studium import _cmd_anomalie   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "anomalie",
    slash_aliases=["anomalie", "anomaly", "odchylky"],
    # HANS_LIST_NOT_CLAIM_V1 (21. 9.) — dve vady najednou, obe zmerene:
    #  (a) vzor sepnul na RELATIVNI VETE uprostred souveti: „všiml jste si
    #      něčeho v sobě samém, co se změnilo?" → vypis tydennich odchylek
    #      Hansova PROVOZU. Proto se vzor kotvi na zacatek vety/otazky.
    #  (b) ve vzorech CHYBELO SAMO SLOVO „anomálie"/„odchylka", takze
    #      „jsou nejake anomalie?" i „ukaz prosim anomalie a napady"
    #      propadly do volneho hovoru. Popisny vzor byl, pojmenovani ne.
    # Zmereno na 1 564 skutecnych zpravach: zuzeni nestoji ANI JEDNU
    # (oba vzory tam dnes maji 0 vyskytu), slovo nove chyta 1 realnou zpravu,
    # a na kontrolnim seznamu 11 vet klesnou chybne verdikty 7 → 0.
    nl_patterns=[r"(?:^|[?!.]\s*)co\b.{0,12}\bje\s+jinak",
                 r"(?:^|[?!.]\s*)co\b.{0,12}\bse\b.{0,12}\bzm[ěe]nilo",
                 # HANS_ANOMALIE_NOUN_ONLY_V1 (6. 10.) — hole „odchyl“
                 # chytalo i sloveso („to se trochu odchyluje od zamereni“
                 # → vypis tydennich odchylek). Jen podstatne jmeno.
                 r"\banom[áa]li", r"\bodchyl(?:k|ek)"],
    handler=_cmd_anomalie,
    help_text="Týdenní odchylky ve tvém chování (algoritmicky) — /anomalie teď = spusť detekci",
)


# ── HANS_CMD_LLM_ROUTE_V1 (5.8.) — příkaz z volné řeči ───────────────────────
# Problém: `nl_patterns` pokryjí jen formulace, které někdo vypsal. „jak jde
# studium?" ve vzorech je, „pokročil jsi v tom učení?" ne — a uživatel pak
# dostane obecné žvanění místo grounded výpisu. Regexy zůstávají PRVNÍ (jsou
# zdarma); model se ptá, teprve když minou.
#
# ⚠️ JEN ČTECÍ PŘÍKAZY. Model může hádat, a hádání nesmí smazat historii
# (`zapomen`), zapnout hlídání (`hlidej`) ani spustit enrollment. Allowlist je
# proto VÝČET, ne odvození z registru: nový příkaz se NEZAŘADÍ sám (fail-closed).
# Args se předávají PRÁZDNÉ → i u příkazů, které mají mutující podpříkazy
# (`/studium přeskoč`, `/smer schválit`), se spustí jen holý výpis.
#
# Model: hans-czech na PC (viz HANS_INTENT_PC_V1). Změřeno 29/30 při 20
# štítcích, medián 1,4 s; VŠECH 8 negativů správně (běžný hovor se neunáší).
# Malý model na Pi tutéž úlohu nezvládl (11/16) — detail v backlogu.
_LLM_ROUTE_CMDS = [
    ("studium",    "co studuje, jak pokračuje jeho učení"),
    ("cetl",       "co a kdy četl (knihy, články)"),
    # ⚠️ `film` a `hraje` tu ZÁMĚRNĚ NEJSOU (HANS_CMD_LLM_ROUTE_V2, 5.8.):
    # sedí sémanticky hned vedle AKCÍ agenta („pusť film Kruh" → kodi_play_film,
    # „děje se něco doma?" → report_home_status). Doloženo naživo hodinu po
    # nasazení V1: „pust film kruh" → /film = výpis, CO Hans viděl, místo aby
    # se film pustil; „deje se neco doma?" → /hraje. Routing běží PŘED agentem,
    # takže mu takové věty ukradne. Obojí má vlastní nl_patterns i agentní
    # akci — z LLM allowlistu se tím neztrácí nic podstatného.
    ("napad",      "jeho vlastní nápady a postřehy (synteze)"),
    ("kritika",    "co u sebe chce zlepšit (sebekritika)"),
    ("dilo",       "jeho autorské dílo na pokračování"),
    ("smer",       "jeho vlastní směr a aspirace"),
    ("vhledy",     "co si všiml ve vlastních datech"),
    ("anomalie",   "odchylky v jeho chování"),
    # HANS_SOFT_MEMORY_V1 — popis zúžen: model sem posílal i „máš oblíbenou
    # vzpomínku?", což je vztahový dotaz do volného registru, ne výpis.
    ("vzpominka",  "jeho ÚPLNĚ PRVNÍ (nejstarší) vzpomínka — NE oblíbená ani hezká"),
    ("videl",      "kdy koho naposledy viděl"),
    # HANS_ROUTE_SELF_ACTIVITY_V1 — dřív jen „o čem jsme se spolu bavili":
    # model pod to schoval i „co jsi dnes dělal?" a Hans vysypal přepis chatu.
    # HANS_STUDY_CONTENT_ROUTE_V1 (19.8.) — dotaz na obsah tématu router
    # posílal SEM a Hans vypsal shrnutí chatu místo obsahu studia (19.8.).
    # Rozhoduje PŘEDMĚT: o čem jsme MLUVILI × co víš O TÉ VĚCI.
    ("rozhovory",  "OBSAH našich dřívějších ROZHOVORŮ (o čem jsme spolu "
                   "mluvili, co jsem říkal). NE dotaz na obsah nějakého TÉMATU "
                   '„co ses o tom dozvěděl“, „co je zajímavého na X“ — to je '
                   "věcná otázka, ne vzpomínka na chat"),
    ("seznam",     "seznam poznámek a úkolů"),
    ("kalendar",   "nadcházející události z kalendáře"),
    # HANS_HRAJE_WORDORDER_V1 — dřív „…kdy co naposledy BĚŽELO": sloveso
    # kolidovalo s „co teď BĚŽÍ v tv" a router posílal dotaz na televizi sem.
    ("rozvrh",     "jeho vlastní rozvrh autonomních rutin a jejich poslední tik"),
    ("zdravi",     "zdraví systému: Ollama, Kodi, PC, disk"),
    ("nalez",      "nálezy ze zkoušení Koláčem (potvrdit/zamítnout)"),
    # HANS_PERSON_ASK_PAT_V1 — popis byl tak obecný („co koho zajímá"), že si
    # k sobě přitáhl i „co o ní víš" → Hans místo odpovědi vysypal výpis zájmů.
    # ⚠️ V popisu ZÁMĚRNĚ ŽÁDNÉ konkrétní jméno: první verze uváděla příklad
    # „jaké má Jana zájmy“ a router si podle toho jména přitáhl i otázku
    # „myslíš, že by Jana měla radost z kávovaru?“ (regrese 18.8., moje).
    ("zajmy",      "VÝSLOVNĚ zájmy a koníčky osoby — musí v dotazu zaznít "
                   "„zájmy“, „koníčky“, „co koho zajímá“. NE obecný dotaz na "
                   "osobu („co víš o X“, „kdo je X“) a NE otázka na názor "
                   "(„myslíš, že by se X líbilo…“)"),
    # HANS_CMD_LLM_ROUTE_V4 — dřív „…s osobou": model to četl jako
    # „věta zmiňující osobu" a posílal sem dotazy na Koláče.
    ("nitky",      "nedokončená témata, která zbývá dotáhnout"),
    ("schopnosti", "co všechno umí"),
]

_LLM_ROUTE_SYSTEM = (
    "Uživatel mluví s domácím asistentem. Rozhodni, který VÝPIS jeho věta "
    "vyžaduje.\nMožnosti:\n"
    + "\n".join("  %s = %s" % (c, d) for c, d in _LLM_ROUTE_CMDS)
    + "\n  zadny = věta nevyžaduje žádný výpis (běžný hovor, názor, zdvořilost, "
      "dotaz na svět, žádost o obraz)\n\n"
      "Příklady:\n„jak jde studium?\" -> studium\n„co jsi četl?\" -> cetl\n"
      "„jsi hodný\" -> zadny\n„kdo byl Napoleon?\" -> zadny\n"
      # HANS_CMD_LLM_ROUTE_V2 — POKYNY K AKCI nejsou žádost o výpis. Bez těchto
      # příkladů model posílal „pusť film X" na /film (= co Hans viděl).
      "„namaluj kočku\" -> zadny\n„pusť film Kruh\" -> zadny\n"
      # HANS_ROUTE_SELF_ACTIVITY_V1 — otázka na Hansův DEN patří chatu, který
      # má grounded blok „FAKTA O MĚ A O MÉM DNEŠKU" (HANS_SELF_STATE_V1).
      # Bez těchto příkladů to model schovával pod `rozhovory` (= přepis chatu).
      "„co jsi dnes dělal?\" -> zadny\n„jak se dnes máš?\" -> zadny\n"
      # HANS_PERSON_ASK_PAT_V1 — dotaz NA OSOBU si k sobě přitahoval /zajmy
      # (jediná „osobní" volba v katalogu). Odpovídá na něj karta z
      # `relationships`/`entities` až za routerem, takže sem patří „zadny“.
      "„co o ní víš?\" -> zadny\n„na Janu jsi zapomněl, co o ní víš\" -> zadny\n"
      "„kdo je Klára?\" -> zadny\n„co víš o Janě?\" -> zadny\n"
      # Otázka na NÁZOR jen zmiňuje osobu — patří do volného hovoru.
      "„myslíš, že by Jana měla radost z kávovaru?\" -> zadny\n"
      "„co by na to řekla Klára?\" -> zadny\n"
      "„co jsi dělal celý den?\" -> zadny\n"
      # HANS_STUDY_CONTENT_ROUTE_V1 — dotaz na OBSAH tématu (i navazující).
      "„a co ses o tom divadle dozvěděl?\" -> zadny\n"
      "„co je na tom zajímavého?\" -> zadny\n"
      "„přehraj ten seriál\" -> zadny\n„co běží v televizi?\" -> zadny\n"
      "„děje se něco doma?\" -> zadny\n„zapni hlídání\" -> zadny\n"
      # HANS_CMD_LLM_ROUTE_V5 (12.8.) — ŽÁDOST O ZALOŽENÍ není žádost o výpis.
      # Doloženo 12.8. 09:29: „připomeň mi, že mám provést měření dnes v 17:00"
      # → model poslal na /kalendar (= VÝPIS událostí), Hans odpověděl, že
      # kalendář není napojený, a připomínka se nikam neuložila. Kalendář je
      # navíc JEN KE ČTENÍ (Proton ICS), takže tam žádost o zápis nemá co dělat
      # — patří agentní akci, která ji umí založit.
      "„připomeň mi zítra v 8 zavolat doktorovi\" -> zadny\n"
      "„připomeň mi to v 17:00\" -> zadny\n"
      "„poznamenej si, že mám koupit mléko\" -> zadny\n"
      "„nezapomeň mi připomenout schůzku\" -> zadny\n\n"
      "Odpověz JEDNÍM slovem ze seznamu."
)

_llm_route_cache: dict = {}

# HANS_CMD_LLM_ROUTE_CUE_V1 — co musí věta (bez diakritiky) nést, aby model
# smel zvolit tenhle výpis
_ROUTE_CUE = {
    "rozvrh": re.compile(r"rozvrh|rutin", re.I),
    "rozhovory": re.compile(r"mluv|bavil|bavi[lt]|povid|rozhovor|rikal|rekl|slibil|"
                            r"psal|chat|debat|konverz", re.I),
    # /tazatel 4. 10.: „co máš v plánu na zítřek?“ (Hansův plán) → kalendář
    # domácnosti; za ~50 dní jediná LLM volba /kalendar a chybná
    "kalendar": re.compile(r"kalend|udalost|schuz|termin|narozen|svat[ek]|akce|akci|"
                           r"navstev|\bmam\b|\bmame\b|\bmi\b|\bnas\b|\bnam\b|\bmuj\b", re.I),
    # HANS_VZPOMINKA_CUE_V1 (6. 10.) — za 15 dni logu 4 volby routeru, 3 mimo
    # („kdy ses potkal s hudbou poprve?“, „ktery z nich je nejstarsi?“)
    # → nejstarsi zaznam deniku. Vypis jen kdyz veta mluvi o pameti ci vzniku.
    "vzpominka": re.compile(r"vzpomin|pamatuj|pamet|zapamat|existuj|vznik|"
                            r"narodi|zaznam|denik", re.I),
}

# HANS_STUDIUM_NOT_WHY_V1 (6. 10.) — opak pojistky tematu: /studium je vypis
# STAVU programu. Router si ho bral i na otazku po duvodu, dojmu a volbe
# („proc jsi zacal studovat…“, „co te na tom studiu zajima?“, „sam si
# vybiras, co studovat?“). Z 20 voleb za 15 dni logu 7 mimo; vzor zamitne 5
# z nich a zadnou z 8 spravnych. Na 853 vetach lidi 1 shoda (o studentech).
_ROUTE_ANTICUE = {
    "studium": re.compile(r"\bproc\b|\bco\s+(?:te|tebe|vas)\b[^?.!]{0,30}(?:zajima|bavi)|"
                          r"zaskoc|prekvap|\bvybir|\bvybral", re.I),
}


def _route_cache_key(msg: str, turns=None) -> str:
    """HANS_CMD_LLM_ROUTE_CACHE_V1 — klíč cache = věta + PŘEDMĚT z vlákna.

    Bez předmětu by se první rozhodnutí o dané větě zafixovalo napořád
    a `HANS_THREAD_V1` by nemělo jak ho změnit (táž věta, jiný kontext,
    jiný správný štítek). Když vlákno nic nenese, klíč je jen text —
    chování beze změny.
    """
    if not turns:
        return msg
    try:
        from scripts.hans_thread import extract_subject, last_assistant_text
        subj = extract_subject(last_assistant_text(turns))
    except Exception:
        subj = ""
    return "%s\x00%s" % (msg, subj) if subj else msg
_LLM_ROUTE_MAX_WORDS = 14      # delší věta = vyprávění, ne žádost o výpis


# HANS_CMD_LLM_ROUTE_V3 (5.8.) — DRUHÁ BRÁNA na „vnitřní" štítky.
# Doloženo naživo: „co si myslíš o cimrmanově filosofii externismu?" → /napad
# (Hans vysypal syntézu o Mauna Loa a ponorce Titan místo odpovědi).
# Měřením potvrzeno u 6 z 9 dotazů na svět: unesly je napad, cetl, kritika,
# vhledy, smer. Vzorec: tyhle štítky se týkají Hansova VNITŘNÍHO ŽIVOTA
# a otázky na ně mají TOTOŽNÝ TVAR jako otázky na svět — liší se jen
# předmětem („co bys zlepšil U SEBE" × „NA TOM OBRAZU"). Negativní příklady
# v promptu nepomohly: „co si myslíš o dešti? -> zadny" tam bylo A PŘESTO
# model poslal filosofii na /napad.
# Řešení: u rizikových štítků se DOPTÁ druhá otázka „ptá se na TVOJE vlastní
# záznamy, nebo na něco jiného?" — jiný kontrast, který model zvládá (15/16).
# Bezpečné štítky (seznam, kalendář, zdraví…) branou NEPROCHÁZEJÍ: jsou
# konkrétní a druhá brána by je jen zdržela (a „co mám na seznamu?" by
# dokonce zamítla — seznam je uživatelův, ne Hansův).
# HANS_ROUTE_RISKY_ROZHOVORY_V1 (20.8.) — `rozhovory` PATŘÍ do rizikových.
# Doloženo 19.8. v hovoru s cizím člověkem: Hans se zeptal „Co byste si rád
# pustil na televizi?", host odpověděl „třeba ty Strážce Galaxie, o kterých
# jste mluvil" → router to poslal na /rozhovory a místo puštění filmu přišlo
# shrnutí 8 výměn. Vazba „o kterých jste mluvil" zní jako dotaz na společnou
# historii, ale věta JMENUJE FILM — je to odpověď na Hansovu vlastní otázku.
# ⚠️ Ověřeno měřením, že to nic nerozbije: legitimní formulace („připomeň
# rozhovor o X", „o čem jsme se bavili?", „vzpomínáš, co jsme řešili?") chytí
# REGEX v `parse_command` DŘÍV a k routeru se vůbec nedostanou; druhá brána
# je tedy uvidí jen u vět, které vzory minuly. Na problémovou větu vrací
# `_asks_own_records` False (= zamítnout), na dotazy na společnou historii True.
# ⛔ `rozvrh` sem ZÁMĚRNĚ NEPŘIDÁVÁM: na doložené větě („čemu jste se dnes
# věnoval nejvíce?") vrací brána True, takže by ho to nespravilo — ten nález
# potřebuje jinou opravu a nemá se schovat pod tuhle.
# HANS_ROUTE_RISKY_ANOMALIE_V1 (1.9.) — `anomalie` PATŘÍ do rizikových.
# Doloženo rozhovorem: „Zaznamenal jste dnes něco neobvyklého V MÍSTNOSTI?"
# → `/anomalie`, jenže ten výpis je o HANSOVĚ VLASTNÍM provozu („počet
# rozhovorů se snížil", „výpadky mozku"), ne o dění v pokoji. Předmět nesedí.
# Je to učebnicový případ, na který je druhá brána stavěná: štítek o vnitřním
# životě, jehož otázka má TOTOŽNÝ TVAR jako otázka na okolí — „všiml sis něčeho
# divného?" u sebe × v místnosti.
# ⚠️ `HANS_THREAD_NO_LIST_V1` to nechytí a chytit nemá: změřeno, že „zaznamenal
# jste…", „všiml sis něčeho divného?" i „stalo se dnes něco neobvyklého?" mají
# `_je_navazujici_dotaz` = False — nejsou to navazující věty, ale samostatné
# otázky. Rozšiřovat kvůli tomu predikát navazování by byl špatný nástroj.
# HANS_ROUTE_RISKY_DILO_V1 (18. 9.) — `dilo` PATRI do rizikovych.
# Dolozeno 15. 9.: "A ta knizka - co je to za dilo? Co se tam ctete?"
# (dotaz na KNIHU, kterou Hans cte) -> /dilo, tedy vypis JEHO autorskeho
# projektu vcetne vnitrni cesty k souborum. Slovo "dilo" si stitek pritahne,
# protoze v katalogu je jedinou volbou, kde to slovo je.
# ⛔ Popis stitku se ZAMERNE nemeni — to je zasah do rozhodovaciho prostoru
#    celeho routeru [[action-description-is-router-change]] a zadal by
#    premereni vsech voleb. Rozhoduje STRUKTURA, jako u `rozhovory`
#    (20. 8.) a `anomalie` (1. 9.): tytez dve otazky maji TOTOZNY TVAR
#    a lisi se jen predmetem — "co pises TY" x "co je to za dilo TAMTA kniha".
# ⛔ Povolit `TYPO_V2` prepis stitku (rewriter na te vete spravne /cetl
#    navrhl a pravidlo ho zahodilo) NELZE — to je vedome, zmerene
#    rozhodnuti z 1. 9. (rewriter meni 13 ze 14 replik, prah podobnosti
#    vyzkousen a zamitnut).
# ZMERENO 18. 9., 3 behy na vetu, produkcni cestou:
#    dolozena veta      /dilo 2 ze 3 behu -> None 3/3
#    6 legitimnich      /dilo 3/3        -> /dilo 3/3 (nic se neztratilo)
#    11 realnych voleb  beze zmeny v obou ramenech
#    brana samotna: dotaz na knihu False 3/3, dotazy na dilo True 3/3
# Cetnost vady: 0 realnych vet (ta jedna v logu je veta testovaciho tazatele),
# proto se to opravuje jednim radkem a ne novou vrstvou.
_LLM_ROUTE_RISKY = frozenset({"napad", "kritika", "vhledy", "smer", "cetl",
                              "rozhovory", "anomalie", "dilo"})

_OWN_RECORDS_SYSTEM = (
    "Uživatel se ptá domácího asistenta. Rozhodni, jestli se ptá na "
    "ASISTENTOVY VLASTNÍ ZÁZNAMY (co ON sám dělal, studoval, četl, co JEHO "
    "napadlo, co si všiml NA SOBĚ, kam ON směřuje), nebo na NĚCO JINÉHO "
    "(vnější téma, cizí dílo, názor na věc, obecná otázka).\n\n"
    "Příklady:\n„napadlo tě něco zajímavého?\" -> vlastni\n"
    "„co bys u sebe zlepšil?\" -> vlastni\n„pokročil jsi v učení?\" -> vlastni\n"
    "„všiml sis něčeho na sobě?\" -> vlastni\n"
    "„co si myslíš o Kantovi?\" -> jine\n„co bys zlepšil na tom obrazu?\" -> jine\n"
    "„co soudíš o té knize?\" -> jine\n„kam to podle tebe spěje?\" -> jine\n\n"
    "Odpověz JEDNÍM slovem: vlastni nebo jine."
)


def _asks_own_records(message: str, config: dict) -> bool:
    """Ptá se na Hansovy VLASTNÍ záznamy? Selhání → True (nezhoršuj dostupnost
    routingu, když model neodpoví — riziko nese až samotné routování)."""
    try:
        from scripts.hans_intent import _ask_classifier
        out = _ask_classifier(config, _OWN_RECORDS_SYSTEM, message)
    except Exception:
        return True
    if out is None:
        return True
    return not (out or "").strip().lower().startswith("jine")


# HANS_SOFT_MEMORY_V1 — rozlišení faktického a měkkého dotazu na vzpomínku.
_MEM_FACTUAL_PAT = re.compile(
    r"(prvn[íi]|nejstarš[íi]|nejd[řr][íi]v|nejd[řr][íi]v[ěe]jš[íi]|"
    r"úpln[ěe]\s+prvn[íi]|jak\s+dávno)", re.I)
_MEM_SOFT_PAT = re.compile(
    r"(oblíben|nejoblíben|hezk|nejhezč|krásn|nejkrásn|mil[áou]|dojemn|"
    r"nejrad[šs]|nejlep[šs]|nejsiln|vtipn|smutn|zábavn|radostn|"
    r"d[ůu]ležit|cenn)", re.I)


def _soft_memory_ask(msg: str) -> bool:
    """True = dotaz na vzpomínku VZTAHOVÝ (oblíbená, hezká, nejsilnější),
    ne faktický (první / nejstarší / jak dávno)."""
    m = msg or ""
    if _MEM_FACTUAL_PAT.search(m):
        return False
    return bool(_MEM_SOFT_PAT.search(m))


# HANS_CAP_WISH_NOT_LIST_V1 (19.8.) — otázka na TOUHU/MEZERU není žádost
# o výčet. Doloženo testem očima cizího člověka: „co byste chtěl umět, co
# zatím NEumíte?" dostalo týž hotový blok jako „co všechno umíte?" (2×
# během 20 výměn). Router pro to nemá jak: v katalogu je jediný štítek
# `schopnosti = co všechno umí`, takže si k sobě přitáhne i jeho OPAK.
# Rozhoduje se to proto tady, deterministicky — další věta do promptu je
# prompt debt ([[prompt-debt-tool-calling]]).
# ⚠️ Pořadí slov musí sedět v obou směrech: čeština staví „co BYS CHTĚL"
# i „co byste CHTĚL"; jednosměrný vzor by minul přesně tu doloženou větu.
_CAP_WISH_PAT = re.compile(
    r"((bys|byste)\s+(si\s+)?cht[ěe]l|cht[ěe]l[aoy]?\s+(bys|byste)|"
    r"(bys|byste)\s+(si\s+)?p[řr]á[lt]|r[áa]d\s+bys(te)?|"
    r"tou[žz][íi][šs]|tou[žz][íi]te|co\s+(ti|v[áa]m)\s+chyb[íi]|"
    r"neum[íi]|nedok[áa][žz]e|nezvl[áa]d)", re.I)


def _capability_wish_ask(msg: str) -> bool:
    """True = věta se ptá, co by Hans CHTĚL umět nebo co NEumí — a na to
    je výčet schopností špatná odpověď (je to jeho opak)."""
    return bool(_CAP_WISH_PAT.search(msg or ""))


# ── HANS_THREAD_NO_LIST_V1 (30.8.) — VÝPIS NEPATŘÍ NA NAVAZUJÍCÍ OTÁZKU ─────
# Doloženo simulovaným rozhovorem 30.8. (styl uživatele, krátké věty):
#   „kolik jsi jich uz udelal"  (o překladech) → /seznam  = POZNÁMKY, „prázdný"
#   „co ti na nem vadilo"                      → /kritika = 10 ponaučení
#   „kolik ti to jeste zabere"                 → /rozvrh  = 14 rutin
#
# ⚠️ PŘÍČINA NENÍ TAM, KDE JSEM ji nejdřív hledal. Regexy (`parse_command`)
# vracejí u všech tří None — rozhoduje LLM router. A rozřešení odkazu
# (`HANS_THREAD_V1`) se na ně vůbec nespustí: `has_own_subject` bere za
# předmět i sloveso („zabere"), takže věta „vlastní předmět má", a
# `_ANAPHORA_RE` obsahuje jen KOREKČNÍ fráze („myslel jsem"), ne zájmena.
#
# Proto zásah DETERMINISTICKÝ a jen ODEBÍRAJÍCÍ, ve stejném místě a tvaru
# jako `HANS_SOFT_MEMORY_V1` a `HANS_CAP_WISH_NOT_LIST_V1` výš — žádná nová
# vrstva. Věta se zájmenem odkazujícím zpět nemá dostat VÝPIS; spadne do
# volného hovoru, který historii má a odpoví v kontextu.
#
# ⚠️ POJISTKA: když věta sama zmiňuje téma příkazu („ukaž mi ten rozvrh",
# „co mám v seznamu"), guard NEZASAHUJE — jinak by zabil legitimní dotaz.
# HANS_THREAD_PRONOUN_MU_V1 (1.9.) — chybějící dativ „mu". Doloženo 30.8.:
# „co jsi mu rikal" po replice o Koláčovi se nepoznalo jako navazující, takže
# se k vláknu vůbec nedostalo. ⚠️ Přidáno JEN „mu": změřeno na 1547 reálných
# uživatelských replikách — 1 výskyt, který už navazující je → 0 změn, tedy
# nulové riziko. Ostatní kandidáti MĚŘENÍM PROPADLI a nepřidávají se:
#   „ne" (53 výskytů) je záporka, ne zájmeno · „te" (26) míří na TAZATELE
#   („vylepšil jsem tě"), ne na předchozí téma · „nich" (8) by udělalo
#   5 změn bez jediného doloženého případu.
_ZPETNE_ZAJMENO = re.compile(
    r"\b(to|tom|tim|toho|tomu|jich|jim|mu|nem|nej|nim|ni|ho|ji|jej|jeho)\b")
# HANS_THREAD_NO_LIST_V3 (30.8.) — dvě další formy téže chyby z dlouhého
# rozhovoru, obě „otázka dostala výpis":
#   „poznas sam, kdyz je neco spatne?"  → /anomalie (výpis odchylek)
#   „a co studium, na cem jsi ted"      → /nitky    (výpis 18 nitek)
# První je dotaz na SCHOPNOST (Hans má o sobě vrstvu, ze které umí odpovědět),
# druhá nese TÁZACÍ zájmeno ukazující do kontextu („na čem"), které se do
# `_ZPETNE_ZAJMENO` nehodí — to jsou zájmena odkazovací.
_SCHOPNOST_DOTAZ = re.compile(
    r"\b(pozn[áa][šs]|um[íi][šs]|dok[áa][žz]e[šs]|zvl[áa]dne[šs]|dovede[šs]|"
    r"pozn[áa]te|um[íi]te|dok[áa][žz]ete|jsi\s+schopen)\b", re.IGNORECASE)
_TAZACI_ZAJMENO = re.compile(
    r"\b(na\s+[čc]em|o\s+[čc]em|s\s+[čc][íi]m|k\s+[čc]emu)\b", re.IGNORECASE)
# `anomalie` přibylo až s V3: výpis odchylek je na dotaz „poznáš to sám?"
# odpověď na jinou otázku. Legitimní „jaké máš anomálie?" chrání pojistka
# `_zminuje_vlastni_tema`, která na slovo z aliasů příkazu guard vypne.
# ⛔ `schopnosti` sem NEPATŘÍ — vyzkoušeno a VRÁCENO 1.9.
# Nález z rozhovoru („měříš to sám, nebo to odhaduješ?" → celý souhrn „co umím")
# je pravý, ale tudy se opravit nedá: guard zamítá větu bez slova, kterým se
# příkaz volá — a **„co umíš?" ani „co všechno dokážeš?" ho neobsahují**, takže
# by se zamítly taky. Změřeno: pojistka `_zminuje_vlastni_tema` chytí jen
# „jaké máš schopnosti?". Rozbilo by to hlavní způsob, jak se na to ptát.
# Správná cesta je predikát na METAOTÁZKU O ZDROJI ÚDAJE (třída
# `is_memory_meta_query`, jen pro čidla), ne rozšíření výpisového seznamu.
# HANS_THREAD_NO_LIST_V4 (1.9.) — `vzpominka` přibyla.
# Doloženo dlouhým rozhovorem: po výpisu sebekritiky přišlo „kdy sis to
# uvedomil?" a Hans odpověděl NEJSTARŠÍM ZÁZNAMEM DENÍKU (25. 4. 2026) —
# navazující otázka na konkrétní věc dostala výpis o úplně jiné.
# ⚠️ Změřeno, že legitimní dotazy NEZMIZÍ: „jaká je tvoje nejstarší vzpomínka?",
# „co si pamatuješ jako první?", „jak dlouho už tu jsi?" i dnešní
# `HANS_MEMORY_SPAN_V2` („jak dlouho v tomto domě sloužíte?") mají
# `_je_navazujici_dotaz` = False, takže projdou. Zamítne se jen věta se
# zpětným zájmenem, která na něco navazuje.
# ⛔ Pozor na rozdíl proti `schopnosti`, které se sem týž den zkusily přidat
#    a VRÁTILY: tam „co umíš?" navazující JE, takže by se zamítlo. Tady ne.
# HANS_SMER_NO_LIST_V1 (3.9.) — `smer` je výpis (emoji, odrážky, tvůrčí
# záměry), a na úvahovou otázku typu „co bys chtěl dělat, kdybys mohl cokoli?"
# je to špatná odpověď. `is_reflective_ask` takovou větu UŽ pozná, jen ji
# guard nemohl zamítnout, protože příkaz tady chyběl. Legitimní dotaz chrání
# `_zminuje_vlastni_tema` — změřeno na 6 tvarech („jaký máš směr?",
# „kam směřuješ?", „/smer" …), všechny projdou.
# ⛔ Precedent `schopnosti` (zkoušeno a vráceno) sem nesedí: tam guard rozbil
# hlavní cestu, protože „co umíš?" slovo příkazu neobsahuje.
_VYPISOVE_CMDS = {"seznam", "nitky", "rozvrh", "kritika", "anomalie",
                  "vzpominka", "smer"}


_NALEZ_SLOVA = re.compile(
    r"\b(na[šs]el|nalez|n[áa]lez|zkou[šs]|obst[áa]l|dopadl|vytkl|pochyb)",
    re.IGNORECASE)
_KOLAC_SLOVO = re.compile(r"\bkol[áa][čc]\w*\b", re.IGNORECASE)


def _je_kolac_bez_nalezu(msg: str) -> bool:
    """HANS_NALEZ_NOT_KOLAC_TALK_V1 — věta je o Koláčovi, ale ne o jeho nálezech."""
    m = msg or ""
    return bool(_KOLAC_SLOVO.search(m)) and not _NALEZ_SLOVA.search(m)


_ELIPSA = re.compile(
    r"^(?:a|no\s+a|tak\s+a|a\s+tak)\s+(?:jak\w*|kdy|pro[cč]|kdo|kolik|kde|co)\b", re.I)


def _je_navazujici_dotaz(msg: str) -> bool:
    """Krátká věta se zájmenem, které ukazuje na předchozí repliku."""
    try:
        from scripts.hans_thread import _fold
        f = _fold(msg or "")
    except Exception:
        f = (msg or "").lower()
    if _TAZACI_ZAJMENO.search(f) or _SCHOPNOST_DOTAZ.search(f):
        return True          # HANS_THREAD_NO_LIST_V3 — bez délkového limitu
    # HANS_THREAD_ELLIPSIS_V1 (27. 9.) — eliptická otázka bez zájmena: „a jaký
    # byl?“, „a jak dopadl?“, „a proč?“. Doloženo testem: „a jaky byl“ (o filmu)
    # → LLM router /vzpominka = nejstarší záznam deníku. Změřeno na 1 645 reálných
    # replikách: 10 vět nově navazujících („a co jsi zjistil?“, „a kdo se dívá?“),
    # žádná nechce výpis z `_VYPISOVE_CMDS` → nic se neztratí.
    if len((msg or "").split()) <= 4 and _ELIPSA.search(f):
        return True
    return len((msg or "").split()) <= 9 and bool(_ZPETNE_ZAJMENO.search(f))


# HANS_READING_IMPRESSION_V1 (3.9.) — dotaz na DOJEM z četby, ne na výpis.
# Doloženo testem: „Když jste o tom četl, dozvěděl jste se něco, co vás
# překvapilo?" → /cetl vypsal čtyři tituly místo odpovědi.
# ⚠️ Proč to NEJDE přes `_thread_guard`: (a) věta má 12 slov, takže ji
# `_je_navazujici_dotaz` odmítne (limit 9), (b) `is_reflective_ask` ji nezná,
# (c) — a to je důvod, který backlog neznal — `_zminuje_vlastni_tema` vrací
# True, protože věta slovo „četl" OBSAHUJE, takže by ji guard propustil
# i kdyby `cetl` do `_VYPISOVE_CMDS` přibyl. Tudy cesta nevede vůbec.
_DOJEM_PAT = re.compile(
    r"(dozv[ěe]d[ěe]l[ao]?\s+(ses|jsi\s+se|jste\s+se)"
    r"|co\s+(t[ěe]|v[áa]s)\b[^?]{0,14}\b(p[řr]ekvapilo|zaujalo|oslovilo|bavilo)"
    r"|(p[řr]ekvapilo|zaujalo|oslovilo)\s+(t[ěe]|v[áa]s)"
    r"|co\s+sis\s+z\s+(toho|n[ěe][hj]o)\s+odnesl)", re.I)


def _je_dotaz_na_dojem(msg: str) -> bool:
    """Ptá se na PROŽITEK z četby/sledování (co tě překvapilo, zaujalo)?

    Změřeno na 1327 reálných uživatelských replikách: 1 shoda („co tě nejvíce
    zaujalo na práci Heideggera?") — a ta je správná. Šest tvarů legitimní
    žádosti o výpis („co jsi četl?", „četl jsi tu knihu?") predikát nebere.
    """
    m = msg or ""
    if _DOJEM_PAT.search(m):
        return True
    try:
        from scripts.hans_thread import _fold
        return bool(_DOJEM_PAT.search(_fold(m)))
    except Exception:
        return False


def _zminuje_vlastni_tema(cid: str, msg: str) -> bool:
    """Nese věta slovo, kterým se ten příkaz volá? Pak ho míní doopravdy."""
    try:
        from scripts.hans_thread import _fold
        f = _fold(msg or "")
        aliasy = set()
        spec = _COMMANDS.get(cid) or {}
        for a in (spec.get("slash_aliases") or [cid]):
            aliasy.add(_fold(a))
        return any(a and a[:5] in f for a in aliasy)
    except Exception:
        return False


def _thread_guard(cid: str, msg: str, config: dict, turns=None) -> str:
    """HANS_CMD_LLM_ROUTE_V4 — oprav štítek podle DETERMINISTICKÝCH signálů.

    Aplikuje se na čerstvý i cachovaný výsledek, ať je rozhodnutí stejné.
    Dnes řeší jediný, ale doložený případ: dotaz na rozhovor s TŘETÍ STRANOU
    (Koláč) model posílá na `nitky` (2× z 50 vět, 6.8.). Tam pro něj není
    nic — kdežto `rozhovory` má A4 (`HANS_THREAD_V1`), který hledá
    v `teddy_dialog` přes `hans_convindex`.
    """
    # HANS_SOFT_MEMORY_V1 — „máš oblíbenou vzpomínku?" NENÍ dotaz na MIN(ts).
    # Router pod `vzpominka` schová každou větu se slovem vzpomínka, protože
    # jiný štítek pro paměť nemá. Faktický dotaz („první/nejstarší") chodí
    # na short-circuit přes vlastní nl_patterns, takže se tu o nic nepřijde;
    # měkký/vztahový dotaz posíláme do volného registru (= žádný výpis).
    if cid == "vzpominka" and _soft_memory_ask(msg):
        _log.info("HANS_SOFT_MEMORY_V1: '%.40s' → /vzpominka ZAMÍTNUTO "
                  "(měkký dotaz, ne nejstarší záznam)", msg)
        return ""
    # HANS_CAP_WISH_NOT_LIST_V1 — „co bys chtěl umět / co zatím neumíš" je
    # otázka na MEZERU, ne na výčet. Volný hovor má v promptu blok `direction`
    # (Hansův vlastní odvozený směr) i `cap`, takže odpoví z toho, co o sobě
    # skutečně ví — kdežto výčet schopností odpovídá na jinou otázku.
    if cid == "schopnosti" and _capability_wish_ask(msg):
        _log.info("HANS_CAP_WISH_NOT_LIST_V1: '%.40s' → /schopnosti ZAMÍTNUTO "
                  "(ptá se, co NEumí nebo co by chtěl umět)", msg)
        return ""
    # HANS_NALEZ_NOT_KOLAC_TALK_V1 (30.8.) — „CO ŘÍKAL KOLÁČ" NENÍ „CO NAŠEL".
    # Doloženo simulovaným rozhovorem: „co rikal kolac" → `/nalez`, tedy výpis
    # věcí, kde Koláč Hansovi našel CHYBU („vymýšlel si — Jeden svět nestačí").
    # Změřeno na routeru: na `nalez` posílá i „co dela kolac?" a „jak se ma
    # kolac" — všechny čtyři zkušební věty.
    #
    # ⚠️ `KOLAC_STATUS_GUARD_V1` v `hans_agent` tuhle třídu už řeší, ale jen
    # mezi AGENTNÍMI akcemi; na chatový příkaz `/nalez` nedosáhne. Proto guard
    # tady — jen ODEBERE štítek, takže dotaz spadne dál (agent má vlastní akci
    # `report_kolac_status`, nebo odpoví hovor).
    #
    # Rozlišuje SLOVO O NÁLEZU: „našel / nález / zkoušel / obstál / vytkl" →
    # výpis je správně. Bez něj je to dotaz na Koláče jako společníka.
    # Změřeno: 45 reálných replik zmiňuje Koláče bez slova o nálezu
    # („Co víš o Koláčovi?", „Kolik dní trvají Koláčovy případy?") — všem
    # dosud hrozilo, že dostanou seznam Hansových pochybení.
    if cid == "nalez" and _je_kolac_bez_nalezu(msg):
        _log.info("HANS_NALEZ_NOT_KOLAC_TALK_V1: '%.40s' → /nalez ZAMÍTNUTO "
                  "(ptá se na Koláče, ne na jeho nálezy)", msg)
        return ""
    # HANS_VIDEL_NOT_KOLAC_V1 (11. 9.) — TÝŽ VZOR PRO `/videl`.
    # Doloženo živě: „kdy jsi naposledy videl Kolace?“ → Hans vrátil
    # „O lidech z tohoto domu mluvím jen s těmi, koho znám.“ Koláč je
    # přitom MEDVĚD, ne člen domácnosti — a k agentovi, který má správnou
    # odpověď (`report_kolac_status`), se dotaz vůbec nedostal:
    #     HANS_CMD_LLM_ROUTE_V1: '...videl Kolace?' → /videl
    # `KOLAC_STATUS_GUARD_V1` v `hans_agent` tuhle třídu řeší, ale jen mezi
    # agentními akcemi; na chatový příkaz nedosáhne.
    # ⚠️ Podmínka „a ŽÁDNÁ ZNÁMÁ OSOBA ve větě“ je nutná: dotaz
    # „kdy jsi viděl <osobu> s Koláčem?“ je legitimní dotaz na
    # člověka a štítek si ponechat MÁ.
    if cid == "videl" and _KOLAC_SLOVO.search(msg or ""):
        _osoba = ""
        try:
            from scripts.cz_names import find_known_person
            _osoba = find_known_person(msg or "", config or {})
        except Exception:
            _osoba = ""
        if not _osoba:
            _log.info("HANS_VIDEL_NOT_KOLAC_V1: '%.40s' → /videl ZAMÍTNUTO "
                      "(ptá se na Koláče, ne na člověka)", msg)
            return ""
    # HANS_THREAD_NO_LIST_V2 (30.8.) — výpis nedostane ani ÚVAHOVÁ otázka.
    # Doloženo dlouhým ověřovacím rozhovorem, tah 19: „kdybys mohl neco zmenit
    # na svem uspořádání, co by to bylo?" → /kritika, tedy výpis deseti
    # ponaučení místo odpovědi. V1 to minula kvůli limitu 9 slov (věta má 10) —
    # a zvedat ten limit by guard rozšířilo naslepo. Místo toho se použije
    # HOTOVÝ predikát `is_reflective_ask`, který na tuhle třídu už existuje;
    # dvě opravy z téhož dne se tím spojí místo aby si konkurovaly.
    _uvaha_ask = False
    try:
        from scripts.hans_intent import is_reflective_ask as _ira
        _uvaha_ask = _ira(msg)
    except Exception:
        pass
    # HANS_READING_IMPRESSION_V1 (3.9.) — dotaz na DOJEM z četby/sledování
    # dostane odpověď, ne výpis. Musí to být VLASTNÍ větev před podmínkou níž:
    # `_zminuje_vlastni_tema` by ji propustilo, protože věta slovo „četl"
    # obsahuje — jenže tam je jako KONTEXT („když jste o tom četl…"), ne jako
    # předmět žádosti.
    if cid in ("cetl", "videl") and _je_dotaz_na_dojem(msg):
        _log.info("HANS_READING_IMPRESSION_V1: '%.40s' → /%s ZAMÍTNUTO "
                  "(ptá se na dojem, ne na výpis)", msg, cid)
        return ""
    # HANS_ZAJMY_NOT_TOPIC_V1 (4.9.) — VYPIS ZAJMU NEUMI FILTROVAT PODLE TEMATU.
    # Doloženo 4.9. v BEZICIM Hansovi (2 vety, obe reprodukovany):
    #   „a co detektivky, ty ctes?"             -> vypis zajmu tazatelky
    #   „co detektivky, mas nejakou oblibenou?" -> tentyz vypis
    # a treti tvar je OZNAMENI, ne otazka:
    #   „Me samotnou hodne bavi priroda…"       -> tentyz vypis
    # `/zajmy` je klicovany OSOBOU (SELECT ... WHERE person=?), takze na dotaz
    # POJMENOVANEM TEMATEM odpovedet neumi — a na oznameni uz vubec.
    # Guard proto sedi AZ ZA volbou stitku (tady, vedle HANS_CMD_LLM_ROUTE_V4),
    # ne pred ni: rozlisovac neni vlastnost vety, ale dvojice (veta, stitek).
    # ⛔ NEZKOUSET ZNOVU dva kandidaty zmerene 4.9. a ZAMITNUTE:
    #   (a) `hans_intent.is_about_self` — myli se v 5 z 8 dolozenych vet,
    #   (b) „ano/ne sonda za carkou" — na 1231 realnych replikach se tyka 56
    #       a vetsina je LEGITIMNI (uvodni pozdrav vyrobi carku).
    # Oba predikaty niz jsou SDILENE (`hans_intent`) a zmerene na realnych
    # zpravach: `vytcene_tema` 6 shod z 1298, `je_fakt_o_mluvcim` 4 z 1294.
    # HANS_ZAJMY_ASKER_ONLY_V1 — zmereno 15. 9. na 837 realnych vetach: slovo
    # zajmu + 2. osoba ma 7 vet; pravidlo by z /zajmy vyradilo 6 a vsech 6 se pta
    # na Hanse nebo na hovor. Jedina ponechana ("co je vlastne tvuj hlavni zajem?")
    # projde pojistkou _ZAJMY_NA_HANSE a handler vypise Hansovy konicky.
    if cid == "zajmy":
        if not _ZAJEM_SLOVO.search(msg or ""):
            _log.info("HANS_ZAJMY_ASKER_ONLY_V1: '%.40s' → /zajmy ZAMÍTNUTO "
                      "(v dotazu nezaznělo nic o zájmech)", msg)
            return ""
        if ((_DRUHA_OSOBA.search(msg or "")
             or (_DRUHA_OSOBA_SLOVESO.search(msg or "")          # HANS_ZAJMY_VERB_2ND_V1
                 and not _PRVNI_OSOBA_OBJEKT.search(msg or "")))
                and not _ZAJMY_NA_HANSE.search(msg or "")):
            _log.info("HANS_ZAJMY_ASKER_ONLY_V1: '%.40s' → /zajmy ZAMÍTNUTO "
                      "(ptá se na Hanse, ne na zájmy osoby)", msg)
            return ""
    # HANS_KALENDAR_NOT_ELLIPSIS_V1 (15. 9.) — "a o vikendu?" po predpovedi
    # pocasi router poslal na /kalendar. Elipsa dedi predmet z PREDCHOZI repliky.
    # Realne kalendarni vety (9 z 837) maji vyslovne slovo a chodi pres regexy.
    if (cid == "kalendar" and turns
            and _ELIPSA_KRATKA.match(msg or "")
            and not _KALENDAR_SLOVO.search(msg or "")):
        try:
            from scripts.hans_thread import last_assistant_text
            _pred = last_assistant_text(turns) or ""
        except Exception:
            _pred = ""
        if _POCASI_REPLIKA.search(_pred):
            _log.info("HANS_KALENDAR_NOT_ELLIPSIS_V1: '%.40s' → /kalendar "
                      "ZAMÍTNUTO (navazuje na předpověď počasí)", msg)
            return ""
    if cid == "zajmy":
        try:
            from scripts.hans_intent import vytcene_tema, je_fakt_o_mluvcim
            _tema = vytcene_tema(msg)
            if _tema or je_fakt_o_mluvcim(msg):
                _log.info("HANS_ZAJMY_NOT_TOPIC_V1: '%.40s' → /zajmy ZAMÍTNUTO "
                          "(%s — výpis zájmů to nefiltruje)", msg,
                          ("téma '%s'" % _tema) if _tema
                          else "fakt o mluvčím")
                return ""
        except Exception as _zne:
            _log.debug("zajmy topic gate: %s", _zne)
    if (cid in _VYPISOVE_CMDS and (_je_navazujici_dotaz(msg) or _uvaha_ask)
            and not _zminuje_vlastni_tema(cid, msg)):
        _log.info("HANS_THREAD_NO_LIST_V1: '%.40s' → /%s ZAMÍTNUTO "
                  "(navazující otázka, výpis nedává odpověď)", msg, cid)
        return ""
    if cid not in ("nitky", "rozhovory"):
        return cid
    try:
        from scripts.hans_thread import third_party_scope
        if third_party_scope(msg, config, turns=turns):
            if cid != "rozhovory":
                _log.info("HANS_CMD_LLM_ROUTE_V4: '%.40s' /%s → /rozhovory "
                          "(dotaz na třetí stranu)", msg, cid)
            return "rozhovory"
    except Exception:
        pass
    return cid


def _norm_veta(s: str) -> str:
    """HANS_CMD_LLM_ROUTE_TYPO_V1 — tvar věty pro POROVNÁNÍ (ne pro hledání):
    bez diakritiky, malá písmena, bez interpunkce a přebytečných mezer.
    Rozhoduje, jestli oprava vůbec něco změnila — když ne, druhé kolo se
    přeskočí a nestojí nic."""
    import unicodedata as _ud
    t = _ud.normalize("NFKD", (s or "").lower())
    t = "".join(c for c in t if not _ud.combining(c))
    return " ".join(__import__("re").findall(r"[a-z0-9]+", t))


def resolve_command_llm(message: str, config: dict, turns=None):
    """Vrátí (command_id, "") když věta žádá o některý ČTECÍ výpis, jinak None.

    Volá se AŽ když `parse_command` (slash + regexy) minul. Fail-safe: model
    nedostupný / neznámý štítek / herní mód → None = beze změny chování.
    `turns` = vlákno rozhovoru (HANS_THREAD_V1) pro deterministické brzdy."""
    msg = (message or "").strip()
    if not msg or msg.startswith("/"):
        return None
    if len(msg.split()) > _LLM_ROUTE_MAX_WORDS:
        return None
    # HANS_CMD_LLM_ROUTE_V4 — KOREKCE nikdy nežádá výpis; je to oprava
    # předchozí odpovědi. Doloženo 6.8.: „to nebyla kritika, myslel jsem co
    # jsis odnesl ze studia" → /studium (uživatel přitom právě říkal, že se
    # NEptá na výpis). PŘED cache schválně — korekce se nemá ani zapamatovat.
    try:
        from scripts.hans_thread import is_correction
        if is_correction(msg):
            _log.info("HANS_CMD_LLM_ROUTE_V4: '%.40s' → routing přeskočen "
                      "(korekce)", msg)
            return None
    except Exception:
        pass
    # HANS_CMD_LLM_ROUTE_IMPERATIVE_V2 (20.8.) — ROZKAZ NIKDY NEŽÁDÁ VÝPIS.
    # Dvojče pravidla o korekci hned nad tímhle. Doloženo 20.8.: „zapiš si,
    # že sis vymyslel to divadlo" → model zvolil /dilo (slovo „vymyslel"
    # + „divadlo" ho stáhlo k tvorbě) a žádost o poznámku se ukradla agentovi;
    # Hans pak vypsal seznam esejí. Táž věta o pár slov delší přitom prošla —
    # jen proto, že překročila limit 14 slov a router se neptal. Na takové
    # náhodě nemá stát, jestli se poznámka zapíše.
    # ⚠️ Řešeno DETERMINISTICKY, ne dalším příkladem v promptu: zkoušel jsem
    # obecné pravidlo v promptu a měření ukázalo drift (tázací věty začaly
    # sahat po štítcích: „na čem teď pracuješ?" zadny → nitky), přičemž cílový
    # případ stejně neopravilo. Prompt vrácen do původního stavu.
    # Slovník sloves je AGENTŮV (`_ACTION_VERBS`) — jedna pravda o tom, co je
    # povel, ne druhý seznam vedle něj.
    # **Změřeno na 948 reálných zprávách: pravidlo se týká 37 z nich a všech
    # 37 jsou skutečné povely** („pusť X", „vypni pc", „připomeň", „zjisti",
    # „zapiš si") — ani jedna žádost o výpis. Otázky se vylučují rovnou:
    # výpis se žádá otázkou, povel otazník nemá.
    if not msg.endswith("?"):
        try:
            from scripts.hans_agent import _ACTION_VERBS, _norm
            if set(_norm(msg).split()) & {_norm(v) for v in _ACTION_VERBS}:
                _log.info("HANS_CMD_LLM_ROUTE_IMPERATIVE_V2: '%.40s' → routing "
                          "přeskočen (rozkaz, ne žádost o výpis)", msg)
                return None
        except Exception as _ive:
            _log.debug("imperative gate: %s", _ive)
    # HANS_CMD_LLM_ROUTE_CAPABILITY_V1 (20.8.) — „UMÍŠ X?" NENÍ ŽÁDOST O VÝPIS X.
    # Doloženo v hovoru 20.8.: „umíš si vlastně zapisovat poznámky?" → router
    # zvolil /seznam a Hans odpověděl „Seznam je prázdný, pane." Uživatel se
    # přitom neptal, CO tam má, ale JESTLI to umí.
    # Je to TŘETÍ případ téže třídy za den (schopnosti, rozhovory, seznam),
    # tak je pravidlo obecné a ne pro další jeden štítek.
    # Predikát je AGENTŮV (`_asks_capability`) — týž, kterým se potlačují akce;
    # jedna pravda o tom, co je dotaz na schopnost.
    # ⚠️ DVĚ VÝJIMKY, obě vynucené měřením na 961 reálných zprávách:
    #   • „umíš mi ŘÍCT, co mám na seznamu?" = zdvořilá ŽÁDOST → výpis projde
    #     (HANS_CAP_QUESTION_SPEECH_VERB_V1),
    #   • „co všechno umíš?" = žádost o VÝČET schopností → /schopnosti projde
    #     (HANS_CAP_LIST_REQUEST_V1); bez ní by pravidlo zabilo funkční featuru.
    # Po výjimkách se pravidlo týká 11 z 961 zpráv a všech 11 jsou skutečné
    # dotazy na schopnost.
    try:
        from scripts.hans_agent import _asks_capability, asks_capability_list
        if _asks_capability(msg, {}) and not asks_capability_list(msg):
            _log.info("HANS_CMD_LLM_ROUTE_CAPABILITY_V1: '%.40s' → routing "
                      "přeskočen (dotaz na schopnost, ne žádost o výpis)", msg)
            return None
    except Exception as _cqe:
        _log.debug("capability gate: %s", _cqe)
    cfg = (config or {}).get("intent", {}) or {}
    if not cfg.get("use_llm", False) or not cfg.get("cmd_route", True):
        return None
    _ckey = _route_cache_key(msg, turns)
    if _ckey in _llm_route_cache:
        cid = _thread_guard(_llm_route_cache[_ckey], msg, config, turns)
        if cid:
            _set_route_origin("llm")  # HANS_CAP_SUMMARY_V1
            # HANS_VIDEL_KOHO_V1 — DRUHA navratova cesta routeru (z cache).
            # Prvni patch ji minul; presne ten vzorec, na ktery CLAUDE.md
            # upozornuje: „spocitej, kolik cest je pred tvym hrdlem".
            _set_route_msg(msg)
            return (cid, "")
        return None
    try:
        from scripts.hans_intent import _ask_classifier
        out = _ask_classifier(config, _LLM_ROUTE_SYSTEM, msg)
    except Exception as e:
        _log.debug("cmd route: %s", e)
        return None
    if out is None:
        return None
    tok = (out or "").strip().lower().strip('".,!?').split()
    tok = tok[0] if tok else ""
    valid = {c for c, _ in _LLM_ROUTE_CMDS}
    cid = tok if tok in valid else ""
    # HANS_CMD_LLM_ROUTE_TYPO_V1 (21.8.) — POTVRĎ ŠTÍTEK NA OPRAVENÉ VĚTĚ.
    # Doloženo 20.8.: „ja vypadal normalitacni proces?" (překlep + chybějící
    # slovo) → /anomalie, tedy výpis o sledování osob místo odpovědi;
    # táž otázka napsaná správně routuje na nic. Přidat `anomalie` mezi
    # RISKY nejde — ZMĚŘENO, že druhá brána pravý dotaz „jaké byly poslední
    # anomálie?" od překlepu NEROZLIŠÍ (u obou „neptá se na své záznamy"),
    # takže by to zabilo funkční featuru.
    # Proto: větu nechá opravit F1 (existující rewriter) a zeptá se znovu.
    # Změřeno: „ja vypadal normalitacni proces?" → „Jak probíhal normalizační
    # proces?" → None ✓, „jake byly posledni anomalie?" → anomalie ✓.
    # ⚠️ Potvrzení smí štítek jen ODEBRAT nebo ZMĚNIT, nikdy PŘIDAT tam, kde
    # router mlčel — špatně opravený překlep by jinak vyrobil výpis.
    # Cena: LLM volání navíc JEN když router po výpisu sáhl; když je věta už
    # napsaná čistě, druhé kolo se přeskočí (porovnání je zadarmo).
    if cid:
        try:
            from scripts.hans_rewriter import (rewrite_for_retrieval as _rw,
                                               is_enabled as _rw_on)
            if _rw_on(config):
                _cista = (_rw(config, msg, history=[], name=None) or "").strip()
                if _cista and _norm_veta(_cista) != _norm_veta(msg):
                    _out2 = _ask_classifier(config, _LLM_ROUTE_SYSTEM, _cista)
                    _t2 = (_out2 or "").strip().lower().strip('".,!?').split()
                    _t2 = _t2[0] if _t2 else ""
                    _cid2 = _t2 if _t2 in valid else ""
                    # HANS_CMD_LLM_ROUTE_TYPO_V2 (1.9.) — potvrzení smí štítek
                    # už jen ODEBRAT, ne PŘEPSAT NA JINÝ.
                    # Doloženo dlouhým rozhovorem: „kdy sis to uvedomil?"
                    # → /vzpominka, jenže rewriter z toho udělal „Kdy jsi si
                    # toho byl/a vědom/a?" a štítek se přepsal na /vhledy →
                    # místo odpovědi přišel výpis vhledů. „uvedomil" přitom
                    # NENÍ překlep.
                    # ⚠️ Měřeno na 14 reálných replikách: rewriter mění 13 z nich
                    # a většinou nejde o opravu překlepu, ale o PŘEFORMULOVÁNÍ —
                    # „zkus to namalovat jeste jednou" → „Jak znovu vytvořit
                    # obraz?" (podobnost 0,26), a „osoby, které byly SOUZENÉ"
                    # → „byly ZASNOUBENÉ" je dokonce věcná chyba.
                    # ⛔ Prahem podobnosti to oddělit NELZE — rozložení je
                    # spojité (0,26–0,93) a pravý překlep „schipnost" (0,78)
                    # leží mezi přeformulováními. Vyzkoušeno, zamítnuto.
                    # ✅ Odebrání ale zůstává bezpečné a doložený případ z 20.8.
                    # („ja vypadal normalitacni proces?" → /anomalie → po opravě
                    # žádný příkaz) je právě odebrání, takže funguje dál.
                    if _cid2 != cid and not _cid2:
                        _log.info("HANS_CMD_LLM_ROUTE_TYPO_V2: '%.40s' → /%s "
                                  "ODEBRÁNO (po opravě '%.40s' žádný příkaz)",
                                  msg, cid, _cista)
                        cid = ""
                    elif _cid2 != cid and _cid2:
                        _log.info("HANS_CMD_LLM_ROUTE_TYPO_V2: '%.40s' → /%s "
                                  "PONECHÁN (oprava '%.40s' chtěla /%s — přepis "
                                  "na jiný štítek se neprovádí)",
                                  msg, cid, _cista, _cid2)
        except Exception as _te:
            _log.debug("route typo confirm: %s", _te)
    if cid and cid not in _COMMANDS:      # registr je pravda, ne můj výčet
        _log.warning("cmd route: '%s' není v registru — ignoruji", cid)
        cid = ""
    cid = _thread_guard(cid, msg, config, turns)
    if len(_llm_route_cache) < 256:
        _llm_route_cache[_ckey] = cid
    # HANS_ROUTE_TP_OWN_RECORDS_V1 (1.9.) — dialog s TŘETÍ STRANOU je z definice
    # Hansův vlastní záznam, takže se druhá brána nemá co ptát.
    # Doloženo rozhovorem 1. 9.: „Mohl byste mi říci, o čem jste dnes rozmlouval
    # s Koláčem?" → správná odpověď; navazující „A co jste mu na to odpověděl?"
    # → `/rozhovory` ZAMÍTNUTO („ptá se na svět") → Hans odpověděl abstinencí,
    # ačkoli ten dialog v deníku MÁ. Klasifikátor větu čte jako dotaz na TÉMA
    # (Norimberský proces = svět), ne na to, co Hans sám řekl.
    # ⛔ Neřeší se dalším příkladem v promptu — to je [[prompt-debt-tool-calling]]
    #    a stejná past, jakou popisuje komentář V3 výš („negativní příklady
    #    v promptu nepomohly"). Rozhoduje STRUKTURA: ví-li vlákno o třetí straně,
    #    je předmět jasný.
    # ⚠️ Doložený případ z 20.8., kvůli kterému je `rozhovory` rizikový („ty
    #    Strážce Galaxie, o kterých jste mluvil"), tím NETRPÍ: tam žádná třetí
    #    strana ve vlákně není, scope vyjde prázdný a brána běží dál.
    _tp_scope = ""
    if cid and cid in _LLM_ROUTE_RISKY:
        try:
            from scripts.hans_thread import third_party_scope
            _tp_scope = third_party_scope(msg, config, turns=turns) or ""
        except Exception:
            _tp_scope = ""
        if _tp_scope:
            _log.info("HANS_ROUTE_TP_OWN_RECORDS_V1: '%.40s' → /%s PROCHÁZÍ "
                      "(vlákno nese třetí stranu: %s)", msg, cid, _tp_scope)
    if (cid and cid in _LLM_ROUTE_RISKY and not _tp_scope
            and not _asks_own_records(msg, config)):
        _log.info("HANS_CMD_LLM_ROUTE_V3: '%.40s' → /%s ZAMÍTNUTO "
                  "(ptá se na svět, ne na Hansovy záznamy)", msg, cid)
        _llm_route_cache[_ckey] = ""
        return None
    # HANS_CMD_LLM_ROUTE_CUE_V1 (4. 10.) — pevná pojistka ZA modelem (změna
    # popisu v promptu přehodila 9 ze 117 jiných voleb → zamítnuto měřením).
    # Z logu ~50 dní: /rozvrh 4× a všechny chybně („co chceš dělat zítra“,
    # „snídani sis vzal?“, „odpočíváš?“), /rozhovory u „co jsi dělal ráno?“,
    # „zapamatuješ si mě?“. Výpis jen když věta nese jeho téma.
    _cue = _ROUTE_CUE.get(cid) if cid else None
    if _cue is not None and not _cue.search(_fold_diacritics(msg)):
        _log.info("HANS_CMD_LLM_ROUTE_CUE_V1: '%.40s' → /%s ZAMÍTNUTO "
                  "(věta nenese téma výpisu)", msg, cid)
        _llm_route_cache[_ckey] = ""
        return None
    _anti = _ROUTE_ANTICUE.get(cid) if cid else None
    if _anti is not None and _anti.search(_fold_diacritics(msg)):
        _log.info("HANS_STUDIUM_NOT_WHY_V1: '%.40s' → /%s ZAMÍTNUTO "
                  "(ptá se na důvod nebo dojem, ne na stav)", msg, cid)
        _llm_route_cache[_ckey] = ""
        return None
    if cid:
        _set_route_origin("llm")  # HANS_CAP_SUMMARY_V1
        _set_route_msg(msg)       # HANS_VIDEL_KOHO_V1 — args jsou prázdné
        _log.info("HANS_CMD_LLM_ROUTE_V1: '%.40s' → /%s", msg, cid)
        return (cid, "")
    return None


from scripts.chat_cmd_tvorba import _cmd_zrusmalbu   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_dum import _cmd_stop   # ROZDELENI_PRIKAZU_V1 — přesunuto


from scripts.chat_cmd_dum import _cmd_pauza   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "zrusmalbu",
    slash_aliases=["zrusmalbu", "zrušmalbu", "nemaluj"],
    nl_patterns=[
        r"\b(?:zru[šs]|zastav|stopni|ukon[čc]i|p[řr]eru[šs])\w*\s+(?:\w+\s+){0,2}?"
        r"(?:malov[áa]n[íi]|malbu|kresbu|kreslen[íi]|ten\s+obraz|obraz|obr[áa]zek)",
        r"\bnemaluj(?:te)?\b",
        r"\bp[řr]esta[ňn](?:te)?\s+malovat\b",
    ],
    handler=_cmd_zrusmalbu,
    help_text="Zruší moje čekající i právě běžící malování: /zrusmalbu (i „zruš malování“, „nemaluj to“)",
)


register(
    "stop",
    slash_aliases=["stop", "zastav", "vypni film"],
    nl_patterns=[
        r"\bzastav(\s+(to|film|ten\s+film|p[řr]ehr[áa]v[áa]n[íi]))?\b",
        r"\bvypni\s+(to|film|ten\s+film|televiz)",
        r"\bstopni\s+(to|film)",
        r"\bu[žz]\s+to\s+nechci\s+(koukat|sledovat)",
    ],
    handler=_cmd_stop,
    help_text="Zastaví přehrávání na TV: /stop (i „zastav film\")",
)

register(
    "pauza",
    slash_aliases=["pauza", "pause", "pauzni"],
    nl_patterns=[r"\bpauzn(i|out)\b", r"\b(dej|d[áa]|dejte)\s+pauzu\b",
                 r"\bzastav\s+na\s+chv[íi]li\b"],
    handler=_cmd_pauza,
    help_text="Pauza/pokračování přehrávání: /pauza",
)


# ─── /hledani — hledání na webshare.cz (HANS_WEBSHARE_CMD_V1) ───────────
# Otevřený bod z backlogu: film mimo Kodi knihovnu pro Hanse neexistoval.
# Datová cesta je `hans_webshare`, tohle je jen ústa k ní.
#
# ⚠️ VZORY MUSÍ BÝT UKOTVENÉ NA „WEBSHARE“, NE NA HOLÉM „NAJDI“ — to slovo
# už používají dvě jiné cesty (`/vzpominka` a `/nastroj`) a široký vzor by jim
# řeč sebral. Ověřeno grepem před stavbou.
# ⚠️ DOTAZ NA STAV („jak jde to stahování“) MUSÍ HLÁSIT, NE SPOUŠTĚT — ta past
# je doložená u `/preloz` a u `/hlidej`, proto má vlastní vzor i větev.
_WS_STAV: dict = {}          # jméno → {"nalezy": […], "dotaz": str, "ts": float}
_WS_TTL_S = 1800             # starší výběr než půl hodiny už není, co uživatel viděl

# Filtry, které smí stát na konci dotazu („duna 2 1080p“).
_WS_FILTRY = ("2160p", "1080p", "720p", "576p", "480p", "4k", "uhd",
              "cz", "cesky", "česky", "dabing", "sk", "slovensky",
              "tit", "titulky", "bluray", "web", "hdtv", "dvd", "cam")


def _ws_rozeber(args: str) -> tuple:
    """Z argumentů vytáhni (dotaz, filtr). U NL shody přijde CELÁ věta."""
    a = (args or "").strip()
    if _route_origin() == "nl":
        # usečni všechno po slovo „webshare“ včetně předložky za ním
        m = re.search(r"webshar\w*\s*", a, re.IGNORECASE)
        if m:
            pred, za = a[:m.start()], a[m.end():]
            # „najdi mi na websharu X“ → X ; „je X na websharu?“ → X
            a = za.strip() if za.strip() else pred
        a = re.sub(r"^(a\s+)?(pros[íi]m\s+)?(m[ůu][žz]e[šs]\s+)?"
                   r"(najdi|hledej|hledat|pod[íi]vej\s+se|zkus|kouk\w*|"
                   r"vyhledej|se[žz]eň|je)\s*(mi\s+)?(n[ěe]jak\w*\s+)?"
                   r"(film\w*\s+|seri[áa]l\w*\s+)?(na\s+|v\s+|o\s+)?", "",
                   a, flags=re.IGNORECASE).strip()
    # HANS_WEBSHARE_PREDLOZKA_V1 (4.9.) — po useknutí u slova „webshare“ zbyde
    # v „podívej se na webshare NA Dunu 2“ ještě předložka a hledalo by se
    # doslova „na Dunu 2“. Odhaleno vlastním testem extrakce, ne až provozem.
    if _route_origin() == "nl":
        a = re.sub(r"^(na|o|v|ve|k|ke|pro|po)\s+", "", a.strip(),
                   flags=re.IGNORECASE)
    a = a.strip(" ?!.,„“\"'")
    filtr = ""
    slova = a.split()
    if len(slova) > 1 and _fold_diacritics(slova[-1].lower()) in [
            _fold_diacritics(f) for f in _WS_FILTRY]:
        filtr = slova[-1].lower()
        a = " ".join(slova[:-1]).strip()
    return a, filtr


from scripts.chat_cmd_dum import _cmd_hledani   # ROZDELENI_PRIKAZU_V1 — přesunuto


def zapamatuj_nalezy(jmeno: str, dotaz: str, nalezy: list) -> None:
    """HANS_WEBSHARE_STAV_SDILENY_V1 — ulož výpis, ze kterého se pak vybírá číslem.

    ⚠️ Volá to i AGENTNÍ odmítací větev (`hans_agent._reject_kodi_play`), když
    film není v knihovně a Hans nabídne Webshare. Bez sdílení by po té nabídce
    „/hledani stahni 2“ nemělo z čeho vybírat — a dva samostatné stavy by se
    dřív nebo později rozešly. Jedna pravda, dva zapisovatelé.
    """
    _WS_STAV[jmeno or ""] = {"nalezy": nalezy, "dotaz": dotaz, "ts": time.time()}


def _ws_kolik(cfg) -> int:
    try:
        return int((cfg.get("webshare", {}) or {}).get("vypis_kolik", 8))
    except Exception:
        return 8


def _ws_zapis_denik(cfg, polozka, uloha) -> None:
    """Deníková událost `webshare_download` — ať se dá dohledat, co se stahovalo."""
    try:
        import json as _js
        import sqlite3 as _sq
        db = (cfg.get("diary_db")
              or (cfg.get("hans_idle", {}) or {}).get("diary_db")
              or "data/hans_diary.db")
        data = _js.dumps({"ident": polozka.get("ident"),
                          "nazev": polozka.get("nazev"),
                          "velikost": polozka.get("velikost"),
                          "kvalita": polozka.get("kvalita"),
                          "cesta": uloha.get("cesta")}, ensure_ascii=False)
        c = _sq.connect(db, timeout=5.0)
        c.execute("INSERT INTO diary (ts, event_type, title, data, note) "
                  "VALUES (?,?,?,?,?)",
                  (time.time(), "webshare_download", polozka.get("nazev", "")[:120],
                   data, "Stáhl jsem z Webshare „%s“ na počítač." % (
                       polozka.get("nazev", "")[:80])))
        c.commit()
        c.close()
    except Exception as e:
        _log.debug("webshare: deníkový zápis selhal: %s", e)


# HANS_CASES_CMD_V1 (26. 9.) — DOTAZ NA KOLÁČOVY PŘÍPADY NEMĚL CESTU.
# Změřeno 25. 9.: v reálných chatech 9 dotazů na Koláčovy případy („jaké
# případy jsi řešil?" 5× po sobě) a ani jednou skutečná odpověď — model si
# délku případů vymyslel, nebo je zapřel; „kolik máte otevřených případů?"
# poslal LLM router na NÁKUPNÍ seznam. Data přitom leží v `kolac_cases`.
# Odpověď je deterministická, bez LLM. Vzory chtějí 2. osobu (tykání
# i vykání) nebo jméno společníka, aby „co řešil Poirot za případy" ani
# „v tom případě…" nespadly sem; „případně" chrání koncovka.
_PRIPAD = r"případ(?:y|u|ů|ech|em|ům)?\b"
_PRIPAD_TY = (r"(řešíte|řešíš|řešite|vyšetřujete|vyšetřuješ|"
              r"(jsi|jste|ste|si)\s+(s\s+\w+\s+)?(řešil|vyšetřoval)\w*)")
_FAZE_CZ = {"opening": "zahájení", "gathering": "sbírání stop",
            "theory": "teorie", "resolution": "rozuzlení"}


def _dny_cz(d: float) -> str:
    d = round(d, 1)
    s = (f"{d:.1f}".rstrip("0").rstrip(".")).replace(".", ",")
    if d == 1:
        return "1 den"
    if d == int(d) and 2 <= d <= 4:
        return f"{s} dny"
    return f"{s} dne" if d != int(d) else f"{s} dní"


def _datum_cz(t: float) -> str:
    lt = time.localtime(t)
    return f"{lt.tm_mday}. {lt.tm_mon}."


from scripts.chat_cmd_zaklad import _cmd_pripady   # ROZDELENI_PRIKAZU_V1 — přesunuto


register(
    "pripady",
    slash_aliases=["pripady", "případy", "kauzy"],
    nl_patterns=[
        r"\b" + _PRIPAD + r".{0,40}\bkoláč\w*",
        r"\bkoláč\w*.{0,40}\b" + _PRIPAD,
        r"\b(jaké|jaký|jaká|jakej|kolik|které|který)\b.{0,30}\b" + _PRIPAD
        + r".{0,30}\b" + _PRIPAD_TY,
        r"\b" + _PRIPAD_TY + r"\s+(za\s+|teď\s+|zrovna\s+)?" + _PRIPAD,
        r"\b(otevřen|uzavřen|vyřešen|nevyřešen|rozdělan|běžící)\w*\s+" + _PRIPAD,
    ],
    handler=_cmd_pripady,
    help_text="Koláčovy případy: běžící, poslední uzavřené a jak dlouho typicky trvají",
)


register(
    "hledani",
    slash_aliases=["hledani", "hledání", "hledej", "webshare", "ws"],
    nl_patterns=[
        # Vždy ukotveno na „webshare“ — viz poznámka výše.
        r"\b(na|v|z|ze)\s+webshar\w*",
        r"\bwebshar\w*\s+(hledej|najdi|zkus|m[áa]|nem[áa])",
        r"\b(hledej|najdi|vyhledej|pod[íi]vej\s+se)\b.{0,40}\bwebshar",
        # dotaz na stav probíhajícího stažení
        r"\bjak\s+(to\s+)?(jde|pokra[čc]uje)\s+(to\s+)?stahov[áa]n[íi]",
        r"\bu[žz]\s+(je|to)\s+sta[žz]en\w*\b",
    ],
    handler=_cmd_hledani,
    help_text=("Hledání na Webshare: /hledani <název> [1080p|cz|bluray]; "
               "/hledani stahni N; /hledani stav"),
)
