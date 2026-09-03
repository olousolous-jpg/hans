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
                return (cmd_id, msg)
        for pat in spec.get("nl_fold", []):
            if pat.search(msg_fold):
                _set_route_origin("nl")
                return (cmd_id, msg)
    return None


# ── Dispatcher ─────────────────────────────────────────────────────────

def dispatch(command: tuple[str, str], handler, name: Optional[str]) -> str:
    """Spustí command. handler = openwebui_direct_handler instance.
    Vrátí text odpovědi pro chat."""
    cmd_id, args = command
    spec = _COMMANDS.get(cmd_id)
    if not spec:
        return f"⚠ Neznámý příkaz: {cmd_id}"
    try:
        return spec["handler"](handler, name, args)
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


def _cmd_verify(handler, name, args) -> str:
    """G5C: ověř faktická tvrzení proti Wikipedii. JEN diagnostika.
    /verify <text>  → ověří text;  /verify → poslední Hansovu odpověď.
    """
    # 1) Zdroj textu: arg má přednost, jinak poslední Hansova odpověď
    text = (args or "").strip()
    if not text:
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — poslední odpověď JEN v tomto kanálu
            _ch = _current_channel()
            hist = ((handler.conv_store.get_history_scoped(name, _ch)
                     if _ch else handler.conv_store.get_history(name)) or [])
            for msg in reversed(hist):
                if msg.get("role") == "assistant" and msg.get("content"):
                    text = msg["content"].strip()
                    break
        except Exception as e:
            _log.error("G5C: čtení historie selhalo: %s", e)
    if not text:
        return "Nemám co ověřovat, pane. Zadejte /verify <text> nebo se nejdřív na něco zeptejte."

    _log.info("G5C: verify začíná, text=%r", text[:120])

    # 2) Extrakce tvrzení (LLM)
    # G5J_VERIFY_UNIFY_V1 (a) — ENTITA = předmět tvrzení (dílo/událost/místo),
    # NE osoba, co o nich něco tvrdí. Sjednoceno s hans_routine extraktorem.
    extract_sys = (
        "Jsi extraktor faktických tvrzení. Z textu vypiš ověřitelná "
        "faktická tvrzení o světě (osoby, díla, události, místa, data). "
        "ENTITA je PŘEDMĚT tvrzení s vlastním heslem na Wikipedii — "
        "u díla samotné dílo, NE jeho autor. Tvrzení 'R.U.R. napsal "
        "Čapek' → ENTITA je 'R.U.R.' (dílo), NE 'Čapek'. Tvrzení "
        "'Wells napsal Válku světů' → ENTITA 'Válka světů'. "
        "Ignoruj dojmy, zdvořilosti, názory. Každé tvrzení na samostatný "
        "řádek ve tvaru 'ENTITA | tvrzení'. Max 3. Pokud žádné, napiš PRÁZDNÉ."
    )
    extract = _g5c_llm(handler, extract_sys, "Text:" + NL_RUNTIME + text, num_predict=200)
    if not extract or "PRÁZDNÉ" in extract.upper():
        return "Pane, v textu nenacházím konkrétní faktické tvrzení k ověření."

    # 3) Wikipedia raw_text pro každé tvrzení
    try:
        from scripts.web_reader import WebReader
        cfg = getattr(handler, "config", {}) or {}
        wr = WebReader(cfg)
    except Exception as e:
        _log.error("G5C: WebReader init selhal: %s", e)
        return "Pane, ověření selhalo (čtečka webu nedostupná): " + str(e)

    lines = [l.strip() for l in extract.splitlines() if l.strip()][:3]
    results = []
    for line in lines:
        entity = line.split("|", 1)[0].strip() if "|" in line else line
        claim = line.split("|", 1)[1].strip() if "|" in line else line
        if not entity:
            continue
        # G5J_VERIFY_UNIFY_V1 (b) — plný článek dle PŘEDMĚTU (kopie G5F z routine):
        # _wikipedia_search → fetch_url (2500 zn.), fallback REST summary.
        from urllib.parse import quote as _q
        wiki = ""
        try:
            _title = wr._wikipedia_search(entity)
            if _title:
                _url = 'https://cs.wikipedia.org/wiki/' + _q(_title.replace(' ', '_'))
                _full = wr.fetch_url(_url, topic='verify')
                if _full and getattr(_full, 'raw_text', ''):
                    wiki = _full.raw_text[:2500]
                    _log.info('G5C: [%s] plný článek %r (%d zn.)',
                              entity, _title, len(_full.raw_text))
            if not wiki:
                _rr = wr.wikipedia(entity)
                if _rr and getattr(_rr, 'raw_text', ''):
                    wiki = _rr.raw_text[:1200]
                    _log.info('G5C: [%s] fallback summary', entity)
        except Exception as e:
            _log.warning("G5C: zdroj pro %r selhal: %s", entity, e)
        if not wiki:
            results.append("• " + entity + ": Wikipedie nenašla (nelze ověřit)")
            continue
        cmp_sys = (
            "Jsi ověřovatel faktů. Porovnej TVRZENÍ s textem z Wikipedie. "
            "Odpověz stručně jedním z: SHODA / ROZPOR / NELZE OVĚŘIT, "
            "a krátce proč (max 1 věta). Buď přísný na fakta."
        )
        cmp_user = "TVRZENÍ: " + claim + NL_RUNTIME + NL_RUNTIME + "WIKIPEDIE:" + NL_RUNTIME + wiki
        verdict = _g5c_llm(handler, cmp_sys, cmp_user, num_predict=120)
        _log.info("G5C: [%s] verdikt=%s", entity, verdict[:120])
        results.append("• " + entity + ": " + verdict)

    if not results:
        return "Pane, nepodařilo se extrahovat ověřitelné entity."

    body = NL_RUNTIME.join(results)
    return ("Ověření proti Wikipedii, pane:" + NL_RUNTIME + NL_RUNTIME + body
            + NL_RUNTIME + NL_RUNTIME + "(Pozn.: jen diagnostika, nic se neukládá.)")



def _cmd_denik(handler, name, args) -> str:
    """Spustí evening reflection v jiném threadu."""
    _hi = getattr(handler, "_hans_idle", None)
    _routine = getattr(_hi, "_routine", None) if _hi else None
    if not _routine or not hasattr(_routine, "run_evening_reflection"):
        return "Omlouvám se, večerní reflexe není dostupná."

    def _run():
        try:
            _log.info("Chat command: spouštím evening reflection")
            result = _routine.run_evening_reflection()
            if result:
                _log.info("Evening reflection done: %s", result[:80])
            else:
                _log.warning("Evening reflection vrátil None")
        except Exception as e:
            _log.error("Evening reflection failed: %s", e)

    threading.Thread(target=_run, daemon=True).start()
    return "Připravuji dnešní deník, pane. Bude to chvíli trvat."


def _cmd_dialog(handler, name, args) -> str:
    """Spustí Hans-Koláč dialog flag souborem (totéž co web admin tlačítko)."""
    from pathlib import Path as _P
    try:
        flag = _P("data/.trigger_dialog")
        flag.parent.mkdir(exist_ok=True)
        flag.touch()
        from scripts.hans_kolac import kolac_name as _kn  # KOLAC_NAME_CONFIGURABLE_V1
        _k = _kn(getattr(handler, "config", {}) or {})
        return f"Zavolám {_k}. Dialog se spustí za chvíli."
    except Exception as e:
        return f"Nepodařilo se mi zavolat společníka: {e}"


def _cmd_zapomen(handler, name, args) -> str:
    """Smaže conversation history aktuální osoby."""
    if not name:
        return "Nevím, čí historii mám smazat."
    store = getattr(handler, "conv_store", None)
    if not store or not hasattr(store, "clear"):
        return "Conversation store není dostupný."
    try:
        store.clear(name)
        return f"Vymazal jsem naše předchozí hovory, {name}."
    except Exception as e:
        return f"Nepodařilo se smazat historii: {e}"


def _cmd_info(handler, name, args) -> str:
    """/stav — HANS_STATUS_UNIFIED_V1: tentýž text jako z mostu (Matrix).
    Dvě samostatné implementace se ukázaly jako matoucí (5.8.): most chytá
    „stav" dřív, takže z telefonu se tahle verze nikdy neukázala."""
    from scripts.hans_status import status_text
    return status_text(getattr(handler, "config", {}) or {}, handler, name or "")


def _cmd_help(handler, name, args) -> str:
    """Seznam commands."""
    lines = ["Dostupné příkazy:"]
    for c in list_commands():
        lines.append(f"  /{c['slash']} — {c['help']}")
    return "\n".join(lines)


def _cmd_zaptej(handler, name, args) -> str:
    """Vyvolá curiosity — Hans vygeneruje otázku a hledá odpověď.
    # CMD_ZAPTEJ_PATCH
    args = volitelný kontext (pokud prázdné, vezme z místnosti)."""
    _hi = getattr(handler, "_hans_idle", None)
    _cur = getattr(_hi, "_curiosity", None) if _hi else None
    if not _cur:
        return "Curiosity modul není dostupný."

    # Kontext: explicitně args nebo "rozhovor s {name}" jako placeholder
    context = args.strip() if args else f"rozhovor s osobou {name or 'host'}"

    import threading
    def _run():
        try:
            # source_type = jiný než observation/room → půjde do Wikipedie
            _cur.trigger_question(context, source_type="manual")
        except Exception as e:
            print(f"[Chat] zaptej failed: {e}")

    threading.Thread(target=_run, daemon=True).start()
    return "Položím si otázku a hledám odpověď, pane."


def _cmd_enroll(handler, name, args) -> str:
    """Spustí video enroll. # ENROLL_MULTI_DEFAULT
    Bez sekund (jen jméno) → multi-phase (3 vzdálenosti, ~2 min).
    Se sekundami → single phase (jen aktuální vzdálenost).
    """
    from pathlib import Path as _P
    parts = (args or "").strip().split()
    target_name = parts[0] if parts else (name or "")
    if not target_name:
        return "Použití: /enroll <jméno> [sekundy]   (bez sekund = multi-phase)"
    try:
        flag = _P("data/.video_enroll")
        flag.parent.mkdir(exist_ok=True)
        if len(parts) < 2:
            # Multi-phase mode (3 vzdálenosti, ~2 min)
            flag.write_text(f"multi:{target_name.lower()}|0")
            return (f"Spouštím multi-phase enrollment pro '{target_name}'. "
                    f"Budu vás vést — postavte se prosím asi metr od kamery.")
        try:
            secs = int(parts[1])
        except ValueError:
            secs = 30
        secs = max(5, min(secs, 120))
        flag.write_text(f"{target_name.lower()}|{secs}")
        return (f"Spouštím video enroll pro '{target_name}' na {secs}s. "
                f"Otáčejte hlavou pomalu.")
    except Exception as e:
        return f"Nepodařilo se spustit enroll: {e}"


# ── Registrace ─────────────────────────────────────────────────────────

def _cmd_ooda(handler, name, args) -> str:  # OODA_CMD_V1
    """/ooda — diagnostika OODA. Zavolá _decide_activity (zaloguje skóre),
    akci NEVYKONÁ, vrátí název vybrané aktivity. Skóre: grep z logu.
    Cesta k idle objektu kopíruje _cmd_denik (handler._hans_idle)."""
    _hi = getattr(handler, "_hans_idle", None)
    if _hi is None:
        return "OODA: idle objekt (handler._hans_idle) není dostupný."
    if not hasattr(_hi, "_decide_activity"):
        return "OODA: _decide_activity na idle objektu chybí (patch aplikován?)."
    try:  # OODA_CMD_SCORE_V1
        chosen_fn = _hi._decide_activity(dry_run=True)  # OODA_DRYRUN_V1
        fn_name = getattr(chosen_fn, "__name__", str(chosen_fn))
        label = fn_name.replace("_activity_", "")
        score = getattr(_hi, "_last_ooda_score", None)
        if score:
            return (
                "OODA skóre: %s\n"
                "(akce NEvykonána — jen diagnostika)"
            ) % score
        # Fallback: atribut chybí (starý běh / první patch nenasazen)
        return (
            "OODA by teď vybralo: %s\n"
            "(akce NEvykonána). Skóre v logu:\n"
            "  grep 'OODA skóre:' data/system.log | tail -1"
        ) % label
    except Exception as _e:
        return "OODA diagnostika selhala: %s" % _e


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

def _cmd_seznam(handler, name, args) -> str:  # HANS_AGENT_V1 — poznámky/seznam
    """/seznam — výpis poznámek; /seznam hotovo N; /seznam smaz N."""
    import sqlite3 as _sql
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
    a = (args or "").strip().lower()
    try:
        db = _sql.connect(dbp, timeout=5.0)
        db.execute("CREATE TABLE IF NOT EXISTS hans_notes ("
                   "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, "
                   "done INTEGER NOT NULL DEFAULT 0)")
        m = re.match(r"(hotovo|smaz|smaž)\s+(\d+)", a)
        if m:
            nid = int(m.group(2))
            if m.group(1).startswith("hotov"):
                db.execute("UPDATE hans_notes SET done=1 WHERE id=?", (nid,))
                db.commit(); db.close()
                return f"✓ Položka {nid} označena jako hotová."
            db.execute("DELETE FROM hans_notes WHERE id=?", (nid,))
            db.commit(); db.close()
            return f"✓ Položka {nid} smazána."
        rows = db.execute("SELECT id, text, done FROM hans_notes "
                          "ORDER BY done ASC, id ASC LIMIT 40").fetchall()
        db.close()
        if not rows:
            return "Seznam je prázdný, pane."
        lines = ["📝 Seznam:"]
        for i, t, d in rows:
            lines.append(f"  {'✓' if d else '•'} [{i}] {t}")
        return "\n".join(lines)
    except Exception as e:
        return f"/seznam: chyba ({e})"


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


def _cmd_zapis(handler, name, args) -> str:
    txt = _note_text(args or "")
    if not txt:
        return "Co si mám poznamenat, pane?"
    # týž kód jako agentní akce `add_note` (včetně větve „má to čas → je to
    # připomínka", HANS_REMINDER_ADD_V1) — jedna pravda o zápisu
    from scripts.hans_agent import _run_add_note
    return _run_add_note(handler, {"text": txt})


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

def _cmd_kalendar(handler, name, args) -> str:  # HANS_CALENDAR_V1
    """/kalendar — nadcházející události z Proton kalendáře; /kalendar sync."""
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.hans_calendar import CalendarStore, is_enabled, people_map
    except Exception:
        return "/kalendar: modul nedostupný."
    person = (name or "").lower()
    if not is_enabled(cfg) or person not in people_map(cfg):
        return ("Váš kalendář zatím nemám napojený, pane. Nasdílejte mi v Proton "
                "Calendar odkaz („pro kohokoli\") a přidejte ho do "
                "config.calendar.people.")
    try:
        dbp = (cfg.get("diary", {}) or {}).get("db_path", "data/hans_diary.db")
        st = CalendarStore(cfg, dbp)
        if (args or "").strip().lower().startswith("sync"):
            n = st.sync()
            return (f"✓ Kalendář synchronizován — {n} událostí." if n >= 0
                    else "⚠ Synchronizace se nezdařila (síť/odkaz).")
        evs = st.upcoming(person, hours=24 * 14, limit=15)
        if not evs:
            return "V nejbližších dvou týdnech nemám ve vašem kalendáři žádnou událost."
        lines = ["📅 Nadcházející:"]
        for e in evs:
            loc = f" ({e['location']})" if e.get("location") else ""
            lines.append(f"  • {st._fmt_when(e)} — {e['summary']}{loc}")
        return "\n".join(lines)
    except Exception as ex:
        return f"/kalendar: chyba ({ex})"


register(
    "kalendar",
    slash_aliases=["kalendar", "kalendář", "kalendar", "calendar"],
    nl_patterns=[
        r"kalend[áa]ř?",
        r"\bco.{0,8}m[áa]m.{0,12}(dnes|z[ií]tra|tento t[ýy]den|tenhle t[ýy]den)",
        r"\bmoje?\s+ud[áa]losti",
        r"napl[áa]novan",  # co mám naplánováno / nemám něco naplánovaného
        r"\bschůzk|\bschuzk",
    ],
    handler=_cmd_kalendar,
    help_text="Nadcházející události z Proton kalendáře (/kalendar, /kalendar sync)",
)


# ─── /rozvrh — Hansův behaviorální rozvrh (HANS_SCHEDULE_V1) ─────────────────
def _cmd_rozvrh(handler, name, args) -> str:  # HANS_SCHEDULE_V1
    """/rozvrh — kompletní Hansův rozvrh autonomních rutin (kdy naposledy tikly,
    zaostávají-li). Doplněk k /zdravi, který ukazuje jen zaostávající."""
    try:
        from scripts.hans_schedule import ScheduleStore
        import os, time
        db = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "hans_diary.db")
        st = ScheduleStore(db)
        rows = st.all()
        if not rows:
            return "Rozvrh je prázdný, pane."
    except Exception as e:
        return f"/rozvrh: chyba ({e})"

    labels = {
        "nightly_analytics":  "Noční analytika",
        "morning_reflection": "Ranní reflexe",
        "study_tick":         "Studijní tick",
        "curiosity_tick":     "Zvědavý tick",
        "calendar_sync":      "Sync Proton kalendáře",
        "catchup_drain":      "Dohnání odložených čtení",
    }
    now = time.time()
    lines = ["📋 Můj rozvrh (autonomní rutiny):"]
    stale_list = st.stale_list(now)
    stale_names = {s["name"] for s in stale_list}
    for r in rows:
        lbl = labels.get(r["name"], r["name"])
        last_ts = r["last_run_ts"]
        if not last_ts:
            when = "ještě neproběhla"
        else:
            age_h = (now - last_ts) / 3600
            if age_h < 1:
                when = f"před {age_h*60:.0f} min"
            elif age_h < 24:
                when = f"před {age_h:.1f}h"
            else:
                when = f"před {age_h/24:.1f} dny"
        gap_h = r["expected_gap_s"] / 3600
        marker = "⚠️ " if r["name"] in stale_names else "  "
        skip = ""
        if r["last_skip_reason"] and not r["last_run_ok"]:
            skip = f" [posl. skip: {r['last_skip_reason']}]"
        # HANS_SCHEDULE_LAST_OK_UI_V1 (18.8.) — „kdy naposledy BĚŽELA" a „kdy
        # naposledy USPĚLA" se u zaseklé rutiny rozcházejí: studium se 18.8.
        # hlásilo každou minutu, ale 5 h nic nenastudovalo. Bez tohohle by řádek
        # tvrdil „před 1 min" a vedle něj svítilo ⚠️, což mate.
        _ok_ts = r.get("last_ok_ts") or 0
        if _ok_ts and last_ts and (last_ts - _ok_ts) > 60:
            _oh = (now - _ok_ts) / 3600
            _ow = (f"před {_oh*60:.0f} min" if _oh < 1 else
                   (f"před {_oh:.1f}h" if _oh < 24 else f"před {_oh/24:.1f} dny"))
            skip += f" [posl. ÚSPĚCH: {_ow}]"
        elif not _ok_ts and last_ts:
            # Ne „nikdy neuspěla" — úspěchy se sledují teprve od migrace
            # (HANS_SCHEDULE_LAST_OK_V1), starší běhy o sobě data nemají.
            skip += " [bez zaznamenaného úspěchu]"
        enabled = "" if r["enabled"] else " (vypnuto)"
        lines.append(f"{marker}• {lbl} — {when} (max gap {gap_h:.0f}h){skip}{enabled}")
    if stale_list:
        lines.append("")
        lines.append(f"⚠️ Zaostává: {len(stale_list)} z {len(rows)} rutin.")
    else:
        lines.append("")
        lines.append("✅ Vše běží podle plánu.")
    return "\n".join(lines)


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

def _cmd_work(handler, name, args) -> str:  # WORK_CMD_V1 + WORK_REFACTOR_SHARED_V1
    """/work <téma> — tenký wrapper. Jádro je v hans_idle._create_work,
    aby ho mohla volat i automatika (idle smyčka) přes shim."""
    topic = (args or '').strip()
    if not topic:
        return 'Použití: /work <téma>  (např. /work sopky)'
    _hi = getattr(handler, '_hans_idle', None)
    if _hi is None:
        return 'WORK: idle objekt (handler._hans_idle) není dostupný.'
    if not hasattr(_hi, '_create_work'):
        return 'WORK: _create_work chybí na idle (refaktor patch nasazen?).'
    # llm_caller: zabal _g5c_llm s reálným handlerem (num_predict=900 jak dřív)
    _caller = lambda s, u: _g5c_llm(handler, s, u, num_predict=900)
    r = _hi._create_work(topic, _caller)
    if not r.get('ok'):
        return 'WORK: %s' % r.get('error', 'neznámá chyba')
    return (('Hotovo. Napsal jsem esej o \'%s\' (%d slov). '
             'Uloženo: %s · RAG: %s')
            % (topic, r.get('words', 0), r.get('path', '?'), r.get('rag', '?')))


def _cmd_interest(handler, name, args) -> str:  # INTEREST_CMD_C2 / INTEREST_DEL
    """/interest                -> výpis naučených zájmů
    /interest <téma>       -> zapíše nový zájem
    /interest del <téma>   -> smaže zájem(y) s daným textem
    /interest reset ano    -> smaže VŠECHNY naučené (Hans spadne na seed)
    Tytéž řádky (event_type=interest_update) řídí personu."""
    import sqlite3
    import time
    cfg = getattr(handler, "config", {}) or {}
    db_path = cfg.get("diary_db", "data/hans_diary.db")
    raw = (args or "").strip()
    parts = raw.split(None, 1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # ── /interest del <téma> ────────────────────────────────────────────
    if sub == "del":
        if not rest:
            return "Použití: /interest del <téma>  (smaže zájem s tímto textem)"
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                cur = conn.execute(
                    "SELECT id, note FROM diary WHERE event_type='interest_update' "
                    "AND lower(note)=lower(?)", (rest,)).fetchall()
                if not cur:
                    return "Žádný naučený zájem '%s' jsem nenašel." % rest
                conn.execute(
                    "DELETE FROM diary WHERE event_type='interest_update' "
                    "AND lower(note)=lower(?)", (rest,))
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            return "INTEREST: mazání selhalo: %s" % _e
        n = len(cur)
        return ("Smazal jsem zájem '%s'%s." %
                (rest, (" (%dx)" % n) if n > 1 else ""))

    # ── /interest reset [ano] ───────────────────────────────────────────
    if sub == "reset":
        if rest.lower() != "ano":
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            return (f"Reset smaže VŠECHNY naučené zájmy a {_pn(cfg)} spadne zpět "
                    "na základní. Pro potvrzení napiš: /interest reset ano")
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM diary "
                    "WHERE event_type='interest_update'").fetchone()
                n = cur[0] if cur else 0
                conn.execute(
                    "DELETE FROM diary WHERE event_type='interest_update'")
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            return "INTEREST: reset selhal: %s" % _e
        from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
        return ("Smazal jsem všechny naučené zájmy (%d). "
                "%s se vrací k základním." % (n, _pn(cfg)))

    # ── /interest (výpis) ───────────────────────────────────────────────
    if not raw:
        try:
            from scripts.hans_persona import recent_interests
            cur = recent_interests(db_path, limit=5)
        except Exception as _e:
            return "INTEREST: čtení selhalo: %s" % _e
        if not cur:
            seed = cfg.get("persona", {}).get("interests_seed", "")
            from scripts.hans_persona import persona_name as _pn  # PERSONA_NAME_CONFIGURABLE_V1
            return ("Žádné naučené zájmy zatím nejsou. "
                    "%s vychází ze základních: %s" % (_pn(cfg), seed or "(žádné)"))
        return ("Aktuální naučené zájmy (nejnovější první): %s\n"
                "Smazat: /interest del <téma>" % cur)

    # ── /interest <téma> (zápis) ────────────────────────────────────────
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note) VALUES (?,?,?,?)",
                (time.time(), "interest_update", "Zájem", raw))
            conn.commit()
        finally:
            conn.close()
    except Exception as _e:
        return "INTEREST: zápis selhal: %s" % _e
    return ("Zaznamenal jsem nový zájem: '%s'. "
            "Promítne se do Hansovy osobnosti." % raw)


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
        r"\bzapis.{0,20}den[íi]k",
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
        r"\bjak[ýé].{0,10}stav",
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


# ─── /sleep — manuální override spánku (SLEEP_TOGGLE_V1) ──────────────
def _cmd_sleep(handler, name, args) -> str:
    """/sleep — toggle Hansova spánku.
    Vzhůru → uspat (tichý). Spí → probudit (mluvící).
    Override drží přes okno, expiruje na přirozené opačné hraně."""
    _hi = getattr(handler, "_hans_idle", None)
    if _hi is None:
        return "/sleep: idle objekt není dostupný."
    _rt = getattr(_hi, "_routine", None)
    if _rt is None:
        return "/sleep: routine objekt není dostupný."
    if not hasattr(_rt, "set_manual_sleep"):
        return "/sleep: set_manual_sleep chybí (patch aplikován?)."
    try:
        currently_sleeping = bool(getattr(_rt, "_sleeping", False))
        new_state = not currently_sleeping
        _rt.set_manual_sleep(new_state)
        return "Usínám." if new_state else "Probudil jsem se."
    except Exception as _e:
        return "/sleep selhal: %s" % _e


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


def _cmd_herni(handler, name, args) -> str:
    """/herni [zap|vyp] — herní mód. ZAP: Hans uvolní modely z VRAM a přestane
    používat Ollamu (volná grafika pro hru). VYP: mozek zase k dispozici. Bez
    argumentu přepíná."""
    from scripts.ollama_client import set_game_mode, game_mode_on
    _hi = getattr(handler, "_hans_idle", None)
    cfg = getattr(_hi, "config", None) if _hi else None
    a = (args or "").strip().lower()
    if a in ("stav", "status"):
        return ("Herní mód je ZAPNUTÝ (grafika volná, mozek nepoužívám)."
                if game_mode_on() else "Herní mód je vypnutý.")
    if a in _HERNI_ON:
        target = True
    elif a in _HERNI_OFF:
        target = False
    else:
        target = not game_mode_on()   # toggle
    res = set_game_mode(target, config=cfg)
    if "error" in res:
        return "/herni selhal: %s" % res["error"]
    if target:
        return ("Herní mód ZAP — uvolnil jsem %d model(ů) z grafické paměti a "
                "mozek teď nepoužívám. Až dohraješ: /herni vyp."
                % res.get("unloaded", 0))
    return "Herní mód VYP — mozek je zase k dispozici."


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


def _cmd_severka(handler, name, args) -> str:
    """/severka — stav / schválit / zamítnout / historie / rollback / teď."""
    _hi = getattr(handler, "_hans_idle", None)
    _rt = getattr(_hi, "_routine", None) if _hi else None
    if not _rt:
        return "Severka není dostupná (routine chybí)."
    ident = getattr(_rt, "_identity", None)
    sev = getattr(_rt, "_severka", None)
    if ident is None:
        return "Verzování identity není dostupné."
    parts = (args or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else "stav"
    rest = parts[1].strip() if len(parts) > 1 else ""
    by = name or "user"

    pend = ident.pending()

    if cmd in _SEVERKA_APPROVE:
        target = int(rest) if rest.isdigit() else (pend[0].id if pend else None)
        if target is None:
            return "Není co schvalovat — žádný čekající návrh, pane."
        if ident.approve(target, approved_by=by):
            cur = ident.current()
            core = cur.core if cur else ""
            return ("Děkuji za důvěru, pane. Přijal jsem novou podobu sebe sama. "
                    "Od této chvíle jsem:" + NL_RUNTIME + NL_RUNTIME + "„" + core + "\"")
        return "Schválení se nezdařilo (verze %s není čekající?)." % target

    if cmd in _SEVERKA_REJECT:
        target = int(rest) if rest.isdigit() else (pend[0].id if pend else None)
        if target is None:
            return "Není co zamítat, pane."
        if ident.reject(target, approved_by=by):
            return "Rozumím, pane. Zůstávám, kým jsem byl."
        return "Zamítnutí se nezdařilo."

    if cmd in _SEVERKA_ROLLBACK:
        if not rest.isdigit():
            return "Uveďte verzi: /severka rollback <id> (viz /severka historie)."
        if ident.rollback(int(rest), approved_by=by):
            cur = ident.current()
            return ("Vrátil jsem se k dřívější podobě:" + NL_RUNTIME + NL_RUNTIME
                    + "„" + (cur.core if cur else "") + "\"")
        return "Rollback se nezdařil."

    if cmd in _SEVERKA_HISTORY:
        hist = ident.history(limit=15)
        if not hist:
            return "Historie identity je prázdná."
        out = ["Historie mé identity, pane:"]
        for v in hist:
            out.append("  [%d] %s — %s: %.70s" % (v.id, v.status, v.source, v.core))
        return NL_RUNTIME.join(out)

    if cmd in _SEVERKA_RUN:
        if sev is None:
            return "Rozhodovací mechanismus není dostupný."
        def _run():
            try:
                sev.evaluate()
            except Exception as _e:
                _log.error("severka manual run: %s", _e)
        threading.Thread(target=_run, daemon=True).start()
        return ("Zamýšlím se nad tím, kým se stávám, pane. Chvíli to potrvá; "
                "výsledek pak najdete v /severka stav.")

    # default: stav
    cur = ident.current()
    out = []
    if cur:
        out.append("Současná identita (verze %d, zdroj %s):" % (cur.id, cur.source))
        out.append("„" + cur.core + "\"")
    if pend:
        out.append("")
        out.append("Čekající návrh změny:")
        for p in pend:
            out.append("  [verze %d] „%s\"" % (p.id, p.core))
            if p.rationale:
                out.append("    důvod: %s" % p.rationale)
        out.append("")
        out.append("Schválit: /severka schválit  |  zamítnout: /severka zamítnout")
    else:
        out.append("")
        out.append("Žádný čekající návrh změny, pane.")
    return NL_RUNTIME.join(out)


register(
    "severka",
    slash_aliases=["severka"],
    nl_patterns=[],
    handler=_cmd_severka,
    help_text="Sebereflexe identity: /severka [stav|schválit|zamítnout|historie|rollback <id>|teď]",
)


# ─── /art — Hans namaluje obraz k aktuální knize (HANS_ART_V1) ────────────
def _cmd_art(handler, name, args) -> str:
    """/art [název knihy] — Hans hned namaluje obraz k (zadané/aktuální) knize.
    Render běží na pozadí (chat odpoví hned), VRAM orchestrace uvnitř."""
    import threading as _t
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    title = (args or "").strip()
    try:
        from scripts import hans_art
    except Exception as e:
        return "Malování není dostupné: %s" % e
    # HANS_ART_UNREAD_WISHLIST_V1 — nečtenou knihu nemaluj naslepo; přiznej + přidej k přečtení
    if title and not hans_art.book_is_read(db, title):
        res = hans_art.add_to_wishlist(db, title)
        if res == "exists":
            return ("Knihu „%s\" jsem ještě nečetl, pane — a už ji mám na seznamu "
                    "k přečtení. Až ji poznám, rád k ní namaluji obraz." % title)
        return ("Přiznám se, pane — knihu „%s\" jsem ještě nečetl, takže bych jen "
                "hádal, oč v ní jde. Přidal jsem si ji na seznam k přečtení (nízká "
                "priorita); až ji přečtu, rád k ní namaluji obraz." % title)

    if not hans_art.comfy_available(cfg):
        return ("Bohužel, pane — výtvarná dílna (ComfyUI na PC) teď neběží, "
                "tak nemohu malovat. Zkuste to, až bude PC vzhůru.")
    book = title or hans_art._current_book_title(db)

    def _worker():
        try:
            hans_art.render_now(cfg, db, title)
        except Exception as _e:
            _log.warning("/art render selhal: %s", _e)
    _t.Thread(target=_worker, daemon=True).start()
    return ("Dám se do toho, pane — maluji obraz inspirovaný knihou „%s\". "
            "Za chvíli se objeví na nástěnce (Co Hans namaloval). "
            "Chat může být asi minutu zaneprázdněný." % book)


register(
    "art",
    slash_aliases=["art", "obraz"],
    nl_patterns=[],
    handler=_cmd_art,
    help_text="Hans namaluje obraz k aktuální knize: /art [název knihy]",
)


# ─── namaluj/nakresli <téma> — obraz na LIBOVOLNÉ téma (HANS_CAPABILITY_AWARENESS_V1) ──
def _cmd_namaluj(handler, name, args) -> str:
    """namaluj/nakresli <téma> — Hans namaluje obraz na libovolné téma, nebo
    dojem z nedávného rozhovoru (když téma neurčíš). Render na pozadí."""
    import threading as _t
    import re as _re
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db")
          or (cfg.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    try:
        from scripts import hans_art
    except Exception as e:
        return "Malování není dostupné: %s" % e

    # HANS_ART_SELF_V1 — „namaluj sebe / svůj avatar / jak vypadáš" = Hans maluje
    # SÁM SEBE (ne uživatele!). Musí PŘED strip sloves (ten „se" spolkne jako
    # spojku) i před distill (ten „mě=tazatel" by „sebe" zmapoval na uživatele).
    _raw = (args or "").lower()
    if _re.search(r"\bsebe\b|s[áa]m\s+sebe|\bsv[ůu][jě]?\s+avatar|sv[ée]ho\s+avatar"
                  r"|jak\s+(ty\s+)?vypad[áa]|autoportr[ée]t|namaluj\s+se\b|"
                  r"nakresli\s+se\b", _raw):
        _full = bool(_re.search(
            r"post(av|avu|avě|avou)|cel(ou|é|ého)\s*(t[ěe]lo|postav)?|"
            r"full\s*body|od\s+hlavy", _raw))
        _style_self = ""
        _ss = _re.search(
            r"(?i)(?:ve?\s+stylu|stylem|jako\s+od|po\s+vzoru)\s+(.+)$", _raw)
        if _ss:
            _style_self = _ss.group(1).strip(" ?.!,")

        def _self_render():
            try:
                r = hans_art.paint_self(cfg, db, full_figure=_full,
                                        style=_style_self)
                _log.info("namaluj SEBE (full=%s) → %s", _full,
                          "ok" if r else "nevyšlo")
            except Exception as _e:
                _log.warning("paint_self: %s", _e)
        _t.Thread(target=_self_render, daemon=True).start()
        return ("Namaluji sám sebe, pane — %s ze své avatarové podoby. Chvíli "
                "to potrvá, pak se podívej do galerie." %
                ("celou postavu" if _full else "podobiznu"))

    # HANS_ART_TV_V1 — „namaluj co dávají v TV / co běží / co hraje" → ŽIVÝ Kodi
    # stav (ne konverzace!). Namaluje aktuálně běžící pořad/film.
    if _re.search(r"co\s+(d[áa]v|b[ěe][žz]|hraj|je)\w*\s+(pr[áa]v[ěe]\s+)?"
                  r"(v\s+)?(tv|telev|kin[eě]|obrazovc)|"
                  r"co\s+(se\s+)?(pr[áa]v[ěe]\s+)?(hraje|d[áa]v[áa]|b[ěe][žz][íi]|"
                  r"koukám|d[íi]v[áa]m)|(film|po[řr]ad|seri[áa]l)\s+co\s+(hraje|"
                  r"b[ěe][žz]|d[áa]v)", _raw):
        try:
            from scripts.kodi_client import KodiClient
            _np = KodiClient(cfg).get_now_playing()
        except Exception as _ke:
            _log.debug("namaluj TV kodi: %s", _ke)
            _np = None
        if _np and (_np.get("title") or _np.get("label")):
            # HANS_ART_TV_GROUNDING_V1 — námět z POPISU děje (Kodi plot), při
            # chybějícím popisku dohledej na internetu; teprve pak jen název.
            _disp = (_np.get("title") or _np.get("label") or "").split(",")[0].strip()
            try:
                _subj, _src = hans_art.tv_paint_subject(cfg, db, _np)
            except Exception as _te:
                _log.debug("tv_paint_subject: %s", _te)
                _subj, _src = _disp, "jen podle názvu"
            _t.Thread(target=lambda: hans_art.paint_subject(cfg, db, _subj),
                      daemon=True).start()
            _log.info("namaluj CO V TV → '%s' (%s)", _subj[:60], _src)
            _note = {"z popisu pořadu": "podle jeho děje",
                     "z internetu (popisek u pořadu chyběl)":
                        "u pořadu chyběl popisek, tak jsem si děj dohledal na internetu",
                     "jen podle názvu":
                        "popisek chyběl a nedohledal jsem víc, takže jen podle názvu"
                     }.get(_src, "")
            return ("Namaluji, co právě běží na obrazovce, pane — „%s“%s. Chvíli "
                    "to potrvá, pak se podívej do galerie." %
                    (_disp, (" (%s)" % _note) if _note else ""))
        return ("V tuto chvíli na televizi nic nehraje, pane — nemám co "
                "namalovat z obrazovky.")

    # HANS_ART_HOME_ROUTE_V1 — „namaluj (svůj/můj/náš) domov / dům / byt / kde
    # bydlím" = Hans maluje SVŮJ obývák z modelu místa (place_facts, pohled z
    # jeho vantage pointu) přes paint_home, NE generický paint_subject — ten
    # „domov" mis-groundne na entitu „Kde domov můj?" (hymna) a maluje osobu
    # (smyšlenou osobu), navíc person-render timeoutuje (doloženo 30.7.).
    if _re.search(r"\b(sv[ůu]j|m[ůu]j|n[áa][šs])\s+(domov|d[ůu]m|byt)\b"
                  r"|\bdomov\b|\bkde\s+(bydl|[žz]ij)", _raw):
        def _home_render():
            try:
                r = hans_art.paint_home(cfg, db)
                _log.info("namaluj DOMOV → %s", "ok" if r else "nevyšlo/odloženo")
            except Exception as _e:
                _log.warning("paint_home: %s", _e)
        _t.Thread(target=_home_render, daemon=True).start()
        return ("Namaluji svůj domov, pane — obývák z místa, kde stojím. Chvíli "
                "to potrvá, pak se podívej do galerie.")

    # vytáhni téma z požadavku (odřízni sloveso a spojky)
    subj = (args or "").strip()
    # \w* za kmenem slovesa pokryje ČASOVANÉ tvary: „namaluješ/namaluje/namaloval
    # bys/nakreslíš" (dřív se ořízl jen „namaluj" → zbylo „eš o tom obraz").
    subj = _re.sub(r"(?i)^\s*(prosím\s+|můžeš\s+|mohl\s+bys\s+|nemohl\s+bys\s+)?"
                   r"(mi\s+)?(namaluj\w*|namalovat|namaloval\w*|nakresl\w*|"
                   r"vytvoř\w*|přemaluj\w*|překresl\w*)"
                   r"(\s+bys?|\s+byste)?"
                   r"\s*(mi\s+)?(prosím\s+)?(obraz|obrázek)?\s*"
                   r"(o\s+|s\s+|se\s+|na\s+t[eé]ma\s+|ohledně\s+|podle\s+|"
                   r"toho\s+jak\s+)?", "", subj).strip(" ?.!,")

    # HANS_ART_STYLE_V4 — odděl STYL od námětu („X ve stylu Y" / „stylem Y")
    style = ""
    _sm = _re.search(
        r"(?i)[\s,]+(?:ve?\s+stylu|stylem|jako\s+od|po\s+vzoru|[àa]\s+la)\s+(.+)$",
        subj)
    if _sm:
        style = _sm.group(1).strip(" ?.!,")
        subj = subj[:_sm.start()].strip(" ?.!,")

    # odkaz na rozhovor → sestav téma z posledních uživatelských zpráv
    if not subj or _re.search(r"(?i)bavili|mluvili|povídali|rozhovor|o\s+čem", subj):
        try:
            # HANS_CHAT_CHANNEL_AWARE_V1 — paint kontext JEN z tohoto kanálu.
            # Bug 3 (18.7.): „zkus to znova" ve web chatu vzalo Rimmera z
            # Telegram konverzace 7 min zpět. Cross-channel history je leak.
            _ch = _current_channel()
            hist = (handler.conv_store.get_history_scoped(name, _ch)
                    if _ch else handler.conv_store.get_history(name)) or []
            ux = [m["content"] for m in hist if m.get("role") == "user"][-4:]
            ux = [u for u in ux if not u.strip().lower().startswith(
                ("namaluj", "nakresli", "vytvoř"))]
            if ux:
                subj = "náš rozhovor: " + " • ".join(u[:80] for u in ux[-3:])
        except Exception:
            pass
    if not subj:
        subj = "dojem z našeho nedávného rozhovoru"

    # HANS_ART_SUBJECT_DISTILL_V1 — messy požadavek / odkaz na rozhovor
    # („tu kočku", „zkus znovu", „o čem jsme se bavili") → destiluj JEDEN
    # čistý námět přes LLM + kontext historie (řeší reference, ořeže instrukce).
    subj = _distill_paint_subject(cfg, name, handler, subj)

    # HANS_ART_DISTILL_REJECT_INSTRUCTION_V1 (20.7.) — destilace vrátila None
    # = ani po LLM není konkrétní námět (např. „to znova prosim" v prázdném
    # kanálu). Radši poprosit než malovat nesmysl.
    if not subj:
        return ("Nedokázal jsem z Vašeho požadavku určit, co mám namalovat, "
                "pane. Prosím upřesněte téma — třeba „namaluj kočku na zdi\" "
                "nebo „namaluj japonskou zahradu\".")

    if not hans_art.comfy_available(cfg):
        return ("Rád bych, pane — ale má výtvarná dílna (ComfyUI na PC) teď neběží. "
                "Až bude PC vzhůru, obraz namaluji.")

    def _worker():
        try:
            hans_art.paint_subject(cfg, db, subj, style=style)
        except Exception as _e:
            _log.warning("namaluj render selhal: %s", _e)
    _t.Thread(target=_worker, daemon=True).start()
    _st = (" ve stylu „%s\"" % style) if style else ""
    return ("S radostí, pane — maluji obraz na téma „%s\"%s. Za chvíli se objeví na "
            "nástěnce (Co Hans namaloval); chat může být asi minutu zaneprázdněný."
            % (subj[:70], _st))


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


register(
    "namaluj",
    slash_aliases=["namaluj", "nakresli"],
    nl_patterns=[r"\bnamaluj", r"\bnamalovat\b", r"\bnakresli", r"vytvoř\s+obr",
                 r"\bp[řr]ekresli", r"\bp[řr]emaluj",
                 r"\boprav\s+(ten\s+|ten[hz]le\s+)?(obraz|obr[áa]zek)"],
    handler=_cmd_namaluj,
    help_text="Hans namaluje/překreslí obraz: namaluj <téma> (i namaluj to jinak)",
)


def _cmd_obrazy(handler, name, args) -> str:  # HANS_ARTWORK_RECALL_V1
    from scripts.hans_recall import artwork_answer
    out = artwork_answer(_recall_db(handler), args or "")
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


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
        r"(posledn[íi]|nov[ýy])\s+obraz\b",
        r"jak[ýy]\s+obraz\s+jsi",
        r"kreslil\s+(jsi|si)\b",
        # HANS_ARTWORK_SHOW_V1 (30.8.) — „ukaž mi ten obraz" je dotaz, ne pokyn
        # malovat. Holé „ukaž mi to" tu ZÁMĚRNĚ není: bez předmětu může mířit
        # na cokoli (rozvrh, deník, nález) a únos by byl horší než dnešní stav.
        r"uka[žz]\w*\s+(mi\s+)?(ten\s+|ty\s+|sv[ůu]j\s+)?(obraz|obr[áa]zk|galerii)",
        r"m[ůu][žz]u\s+(to\s+)?vid[ěe]t\s+(ten\s+)?obraz",
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


def _cmd_jakto(handler, name, args) -> str:
    t = (args or "").strip()
    if not t or not _HOWTO_PAT.search(t):
        return ""
    try:
        from scripts.hans_capabilities import capability_for
        popis = capability_for(t)
    except Exception as e:
        _log.debug("HANS_CAP_HOWTO_V1: %s", e)
        return ""
    return ("%s, pane." % popis.rstrip(" .")) if popis else ""


# ⛔ NEREGISTROVAT jako příkaz s vzorem `\b(kam|kde|jak)\b…` — vyzkoušeno
# 26.8. a KRADE to routing: „jak jde studium?" šlo na `jakto` místo na
# `studium`, a protože handler vrátil prázdno, deterministická odpověď
# /studium se ztratila úplně. „jak" je moc běžné slovo.
# Doručuje se proto GROUNDINGEM (`openwebui_direct_handler`), který routing
# nesahá. `_cmd_jakto` zůstává jako pomocná funkce.


def _cmd_schopnosti(handler, name, args) -> str:
    # HANS_CAP_SUMMARY_V1 — plný výčet se slash-příkazy zahltí nováčka. Proto:
    # jen EXPLICITNÍ slash /schopnosti (origin "slash") → plný report; přirozený
    # dotaz „co umíš/dokážeš?" (origin "nl"/"llm"/neznámý) → vřelé shrnutí. Původ
    # rozhoduje, protože slash i LLM-route vracejí OBĚ prázdné args (nerozliší se).
    try:
        from scripts.hans_capabilities import (capabilities_report,
                                               capabilities_summary)
        if _route_origin() == "slash":
            return capabilities_report()
        return capabilities_summary()
    except Exception as e:
        return "Přehled schopností nedostupný: %s" % e


register(
    "schopnosti",
    slash_aliases=["schopnosti", "umis", "umíš", "capabilities"],
    nl_patterns=[r"co\s+(v[šs]echno\s+)?um[ií][šs]", r"co\s+dok[aá][žz]e[šs]"],
    handler=_cmd_schopnosti,
    help_text="Přehled toho, co Hans umí: /schopnosti",
)


# ─── /blink — mrknutí animatronickými víčky (HANS_EYE_BLINK_V1) ──────────────
def _cmd_blink(handler, name, args) -> str:
    fn = getattr(handler, "_blink_eyes", None)
    if not callable(fn):
        return "Oči teď nemám po ruce, pane."
    try:
        ok = fn()
    except Exception as e:
        return "Mrknutí se nepovedlo, pane: %s" % e
    return "*mrkl jsem*" if ok else "Víčka teď nejsou aktivní, pane."


register(
    "blink",
    slash_aliases=["blink", "mrkni"],
    # jen jednoznačné „mrkni okem/očima / zamrkej" — NE holé „mrkni" (kolize s
    # „mrkni na to" = podívej se)
    nl_patterns=[r"\bzamrkej\b", r"\bmrkni\s+(oč|ok|na\s+m[ěe])"],
    handler=_cmd_blink,
    help_text="Hans mrkne očima: /blink",
)


# ─── co hraje? — ŽIVÁ kontrola Kodi (HANS_LIVE_PLAYBACK_QUERY_V1) ────────────
def _cmd_hraje(handler, name, args) -> str:
    """co hraje / co se přehrává — Hans zkontroluje ŽIVÝ stav Kodi (ne deník);
    nic nehraje → navrhne film. Funguje i přes Telegram (jde přes send_chat_message).
    Pozn.: Kodi (252) je samostatné zařízení, funguje i když PC spí."""
    cfg = getattr(handler, "config", {}) or {}
    _hi = getattr(handler, "_hans_idle", None)
    kodi = getattr(_hi, "kodi", None) if _hi is not None else None
    if kodi is None:
        try:
            from scripts.kodi_client import KodiClient
            kodi = KodiClient(cfg)
        except Exception:
            return "Bohužel se teď nemohu spojit s přehrávačem, pane."
    # 1) živý stav
    try:
        now = kodi.get_now_playing()
    except Exception as e:
        _log.warning("hraje: get_now_playing selhal: %s", e)
        now = None
    if now:
        title = (now.get("title") or now.get("label") or "").strip()
        show = (now.get("showtitle") or "").strip()
        ep, se = now.get("episode"), now.get("season")
        if show and ep:
            base = f"seriál „{show}\""
            if se:
                base += f" (řada {se}, díl {ep})"
            elif ep:
                base += f" (díl {ep})"
            if title and title != show:
                base += f" – {title}"
            desc = base
        else:
            yr = now.get("year")
            desc = f"„{title}\"" + (f" ({yr})" if yr else "")
        return f"Právě se přehrává {desc}, pane."
    # 2) nic nehraje → návrh filmu
    names = list(getattr(_hi, "_present_names", []) or []) if _hi is not None else []
    if not names and name:
        names = [name]
    m = None
    if _hi is not None:
        try:
            m = _hi._pick_next_film(names, cfg.get("film_suggest", {}) or {})
        except Exception as e:
            _log.warning("hraje: _pick_next_film selhal: %s", e)
    if m is None:
        try:
            m = kodi.pick_suggestion(prefer_genres=kodi.favorite_genres())
        except Exception:
            m = None
    if m:
        mt = (m.get("title") or m.get("label") or "").strip()
        yr = m.get("year")
        return (f"Teď nic nehraje, pane. Mohl bych navrhnout „{mt}\""
                + (f" ({yr})" if yr else "")
                + " — stačí říct a pustím to.")
    return "Teď se nic nepřehrává, pane, a vhodný návrh se mi teď nepodařilo najít."


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


def _cmd_nitky(handler, name, args) -> str:
    """/nitky — výpis otevřených nitek; /nitky zavři <id>; /nitky vše."""
    import sqlite3 as _s
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    parts = (args or "").strip().split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    if cmd in _NITKY_CLOSE:
        if not rest.isdigit():
            return "Uveďte id: /nitky zavři <id> (viz /nitky)."
        try:
            from scripts.hans_threads import ThreadStore
            ok = ThreadStore(cfg, db).close(int(rest), resolution="ručně uzavřeno")
            return ("Nitku %s jsem uzavřel, pane." % rest if ok
                    else "Tu nitku se nepodařilo uzavřít (už uzavřená?).")
        except Exception as e:
            return "Chyba při uzavírání: %s" % e

    include_closed = cmd in _NITKY_ALL
    try:
        conn = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        conn.row_factory = _s.Row
        sql = ("SELECT id,person,topic,follow_up,status,times_surfaced "
               "FROM person_threads "
               + ("" if include_closed else "WHERE status='open' ")
               + "ORDER BY person, updated_ts DESC")
        rows = conn.execute(sql).fetchall()
        conn.close()
    except Exception as e:
        return "Nitky nedostupné: %s" % e
    if not rows:
        return "Zatím žádné rozjeté nitky, pane."
    out = ["Rozjeté nitky%s:" % (" (vč. uzavřených)" if include_closed else "")]
    cur_person = None
    for r in rows:
        if r["person"] != cur_person:
            cur_person = r["person"]
            out.append("")
            out.append("• %s:" % cur_person)
        mark = "" if r["status"] == "open" else " [%s]" % r["status"]
        out.append("   [%d] %s%s → „%s\" (×%d)" % (
            r["id"], r["topic"], mark, r["follow_up"] or "", r["times_surfaced"]))
    out.append("")
    out.append("Uzavřít: /nitky zavři <id>  |  vše vč. uzavřených: /nitky vše")
    return NL_RUNTIME.join(out)


register(
    "nitky",
    slash_aliases=["nitky", "threads"],
    nl_patterns=[],
    handler=_cmd_nitky,
    help_text="Rozjeté nitky per osoba: /nitky [vše|zavři <id>]",
)


# ─── /zajmy — per-osoba zájmy (HANS_PERSON_INTERESTS_V1, frontier #4) ─────
def _cmd_zajmy(handler, name, args) -> str:
    """/zajmy [jméno] — co kterou osobu zajímá."""
    import sqlite3 as _s
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db") or "data/hans_diary.db"
    who = (args or "").strip().lower()
    # HANS_LLM_ROUTE_ARGS_V2 — `zajmy` má nl_patterns=[] → chodí sem VÝHRADNĚ
    # přes LLM router, který dává args="" → „co zajímá Janu?" vypsalo VŠECHNY
    # (doloženo 13.8. voláním handleru). Příkaz je read-only (mode=ro), takže
    # vzít původní větu z vlákna je bezpečné. Jméno rozřeší `_resolve_person`
    # (sdílený helper z hans_recall, používá ho i `videl`) — volá se s asker=None,
    # aby se NEuplatnil jeho fallback na tazatele: „jaké zájmy mají lidi doma?"
    # musí dál vypsat všechny, ne jen tazatele. „mě/mne" se dořeší zvlášť.
    if not who:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            _q = str(_tc[0]) if (_tc and _tc[0]) else ""
        except Exception:
            _q = ""
        if _q:
            try:
                from scripts.hans_recall import _resolve_person
                _p = _resolve_person(_q, cfg, None)
                if not _p and re.search(r"\bm[ěe]\b|\bmne\b", _q.lower()):
                    _p = name
                if _p:
                    who = str(_p).strip().lower()
            except Exception:
                pass
    try:
        conn = _s.connect("file:%s?mode=ro" % db, uri=True, timeout=3.0)
        conn.row_factory = _s.Row
        if who:
            rows = conn.execute(
                "SELECT person,interest,evidence_count FROM person_interests "
                "WHERE status='active' AND person=? ORDER BY evidence_count DESC",
                (who,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT person,interest,evidence_count FROM person_interests "
                "WHERE status='active' ORDER BY person, evidence_count DESC").fetchall()
        conn.close()
    except Exception as e:
        return "Zájmy nedostupné: %s" % e
    if not rows:
        return (("O zájmech osoby %s zatím nic nevím, pane." % who) if who
                else "Zatím neznám zájmy žádné osoby, pane.")
    out = ["Zájmy%s:" % ((" — " + who) if who else "")]
    cur_p = None
    for r in rows:
        if r["person"] != cur_p:
            cur_p = r["person"]
            out.append("")
            out.append("• %s:" % cur_p)
        out.append("   %s (×%d)" % (r["interest"], r["evidence_count"]))
    return NL_RUNTIME.join(out)


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
    r"|\bod[kK]ud\s+(m[áa][sš]|se\s+vzalo)\b", re.IGNORECASE)


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
                if hledane in t or t in hledane or any(
                        w in t for w in hledane.split() if len(w) > 3):
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
    r"\b(jak[éeáa]|kter[éeáa])\s+([a-zá-žA-ZÁ-Ž]{4,})\w*\s+(?:\w+\s+){0,2}"
    r"(zn[áa][sš]|m[áa][sš]|v[íi][sš]|pamatuje[sš]|studoval)", re.IGNORECASE)


def _vycet_dotaz(text: str) -> str:
    """Vrátí hledané slovo („hrady"), nebo prázdno."""
    m = _VYCET_PAT.search(text or "")
    return m.group(2) if m else ""


def _cmd_vycet(handler, name, args) -> str:
    slovo = _vycet_dotaz(args or "") or (args or "").strip()
    # KMEN NA 4 ZNAKY, ne 5: české skloňování mění koncovku a „mesta" se do
    # „mesto" netrefí. „hrad", „film", „mest", „knih" projdou.
    kmen = _norm_veta(slovo)[:4]
    if len(kmen) < 4:
        return ""                       # příliš krátké → radši nic netvrdit
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        import sqlite3 as _sq
        con = _sq.connect(db, timeout=10)
        try:
            rows = con.execute(
                "SELECT e.name, f.hodnota FROM entity_facts f "
                "JOIN entities e ON e.id = f.entity_id "
                "WHERE f.klic='je to' ORDER BY e.name").fetchall()
        finally:
            con.close()
    except Exception as e:
        _log.debug("HANS_FACTS_ENUM_V1: %s", e)
        return ""
    nalez = [n for n, h in rows if kmen in _norm_veta(h)]
    if not nalez:
        return ""                       # nic → propadni do běžného hovoru
    if len(nalez) > 25:
        vypis = ", ".join(nalez[:25])
        return ("V ověřených faktech jich mám %d, pane. Prvních pětadvacet: %s."
                % (len(nalez), vypis))
    return ("V ověřených faktech mám tyto, pane: %s. Můžu o nich vědět i víc "
            "z četby, tohle je jen to, co mám doložené." % ", ".join(nalez))


register(
    "vycet",
    slash_aliases=["vycet", "výčet", "cojeto"],
    nl_patterns=[_VYCET_PAT.pattern],
    handler=_cmd_vycet,
    help_text="Co mám doloženo ve faktech: jaké hrady znáš? jaké filmy znáš?",
)


def _cmd_studium(handler, name, args) -> str:
    """/studium — stav studijního programu; /studium programy = všechny;
    /studium teď = spustí jednu studijní session na pozadí (noční práce ručně)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_study import StudyStore, run_study_session
    except Exception as e:
        return "Studijní modul nedostupný: %s" % e
    store = StudyStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in _STUDY_NOW:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        # HANS_STUDY_NUDGE_V1 — session běží na pozadí; když skončí JINAK než
        # úspěchem, uživatel se to dosud NEDOZVĚDĚL (odpověď zněla „výsledek
        # uvidíte v /studium" a pak ticho). Doloženo 4.8.: „Geologie Českého
        # ráje" → noread, program stál a nic to nehlásilo. Teď se výsledek
        # ohlásí zpět — u `noread` i s nabídkou ruční zkratky.
        _prev = store.get_active_program()
        _prev_sub = ""
        try:
            _prev_sub = str(_prev["curriculum"][_prev["current_index"]])
        except Exception:
            pass

        def _run():
            try:
                code = run_study_session(cfg, db, knowledge=kn)
                _log.info("/studium teď → %s", code)
                _msg = None
                if code == "noread":
                    _msg = ("K pod-tématu „%s\" jsem nenašel žádný použitelný "
                            "zdroj, pane — encyklopedie ho zřejmě nezná. "
                            "Program tím pádem stojí. Můžete mi říct "
                            "„/studium přeskoč\" a pustím se do dalšího."
                            % (_prev_sub or "aktuální"))
                elif code == "deferred":
                    _msg = ("Studium jsem musel odložit, pane — buď mi nebyl "
                            "dostupný mozek, nebo encyklopedie neodpovídala. "
                            "Zkusím to znovu sám.")
                elif code == "skipped":
                    # HANS_STUDY_UNIFY_V1 — `skipped` sem dosud nepropadl, takže
                    # uživatel po ručním „/studium teď" NEDOSTAL žádnou zprávu,
                    # ačkoli právě kvůli tomu HANS_STUDY_NUDGE_V1 vznikl.
                    _msg = ("Pod-téma „%s\" jsem po opakovaných pokusech "
                            "přeskočil, pane — encyklopedie k němu nic nemá. "
                            "Pokračuji dalším v pořadí."
                            % (_prev_sub or "aktuální"))
                elif code == "idle":
                    _msg = "Teď nemám co studovat, pane — vše z kurikula je hotové."
                if _msg:
                    _notify_user(handler, _msg)
            except Exception as _e:
                _log.warning("/studium teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="StudyNow").start()
        return ("Pustil jsem se do studia, pane — nastuduji další pod-téma. "
                "Chvíli to potrvá (čtení + zápis poznámky), výsledek pak "
                "uvidíte v /studium a v deníku. Kdyby se nedařilo, ozvu se.")

    if sub in _STUDY_SKIP:
        # HANS_STUDY_NUDGE_V1 — ruční přeskočení zaseklého pod-tématu. Automatika
        # ho přeskočí až po `max_subtopic_failures` nocích; tohle je zkratka,
        # když uživatel VIDÍ, že na tom program vázne.
        ap = store.get_active_program()
        if not ap:
            return "Teď nestuduji žádný program, pane — není co přeskočit."
        curriculum = ap["curriculum"]
        idx = int(ap["current_index"])
        if idx >= len(curriculum):
            return "Kurikulum už je u konce, pane — není co přeskočit."
        skipped = str(curriculum[idx])
        nxt = idx + 1
        store._update_fields(ap["id"], current_index=nxt, fail_count=0)
        # HANS_STUDY_SKIPPED_MARK_V1 — i RUČNÍ přeskočení se musí zapsat,
        # jinak by se ve /studium ukázalo jako nastudované (nález 6.8.).
        try:
            store.mark_skipped(ap["id"], idx)
        except Exception as _mse:
            _log.warning("mark_skipped (ruční): %s", _mse)
        _log.info("/studium přeskoč → '%s' (program [%d], %d→%d)",
                  skipped, ap["id"], idx, nxt)
        if nxt >= len(curriculum):
            return ("Přeskočil jsem „%s\", pane — a tím je kurikulum „%s\" "
                    "u konce. Mistrovskou reflexi sepíšu v noci."
                    % (skipped, ap["topic"]))
        return ("Přeskočil jsem „%s\", pane. Další na řadě: „%s\" "
                "(%d z %d). Nastuduji ho v noci — nebo hned, řeknete-li "
                "„/studium teď\"."
                % (skipped, curriculum[nxt], nxt + 1, len(curriculum)))

    if sub in _STUDY_ORIGIN or _je_dotaz_na_puvod(args):
        return _studium_puvod(store, db, args)

    if sub in {"programy", "programs", "vše", "vse", "all"}:
        progs = store.all_programs()
        if not progs:
            return "Zatím jsem nezačal žádný studijní program, pane."
        out = ["Studijní programy:"]
        for p in progs:
            out.append("  [%d] %s — %s (%d/%d, %d sessions)" % (
                p["id"], p["topic"], p["status"], p["current_index"],
                len(p["curriculum"]), p["sessions_done"]))
        return NL_RUNTIME.join(out)

    ap = store.get_active_program()
    if not ap:
        progs = store.all_programs()
        if progs:
            last = progs[0]
            return ("Právě nestuduji, pane. Naposledy: „%s\" (%s, %d/%d). "
                    "Další program si vyberu z trvalého koníčku. "
                    "(/studium programy, /studium teď)" % (
                        last["topic"], last["status"], last["current_index"],
                        len(last["curriculum"])))
        return ("Zatím jsem nezačal studijní program, pane — vyberu si trvalý "
                "koníček a sestavím kurikulum. (/studium teď to spustí ručně)")

    cur = ap["current_index"]
    total = len(ap["curriculum"])
    out = ["Studuji: „%s\" — pod-téma %d z %d:" % (ap["topic"], cur + 1 if cur < total else total, total)]
    # HANS_STUDY_SKIPPED_MARK_V1 — přeskočené se NESMÍ kreslit jako ✓
    # (nález uživatele 6.8.: „ukazuje jako nastudováno").
    _skipped = ap.get("skipped_idx") or set()
    _n_skip = 0
    for i, s in enumerate(ap["curriculum"]):
        if i in _skipped:
            mark = "⤼"
            _n_skip += 1
        elif i < cur:
            mark = "✓"
        elif i == cur:
            mark = "→"
        else:
            mark = " "
        out.append("   %s %s" % (mark, s))
    if _n_skip:
        out.append("   (⤼ = přeskočeno, nenašel jsem k tomu zdroj)")
    out.append("")
    # HANS_STUDY_NUDGE_V1 — bez tohohle nebylo z výpisu poznat, že program
    # VÁZNE (jen že stojí na pod-tématu). fail_count = kolik nocí po sobě se
    # k němu nenašel zdroj; po `max_subtopic_failures` ho automatika přeskočí.
    _fc = int(ap.get("fail_count", 0) or 0)
    if _fc:
        _maxf = int((cfg.get("study", {}) or {}).get("max_subtopic_failures", 3))
        out.append("⚠ K tomuhle pod-tématu se mi %d× nepodařilo najít zdroj "
                   "(z %d pokusů, pak ho přeskočím sám). "
                   "Chcete-li hned: /studium přeskoč" % (_fc, _maxf))
        out.append("")
    # HANS_STUDY_TODAY_LINE_V1 (18.8.) — DNEŠEK. Výpis dosud ukazoval jen stav
    # kurikula, takže na „jak ti dneska šlo studium?" i na přímé „povedlo se ti
    # dneska něco nastudovat?" chodila TÁŽ statická šablona (doloženo dialogem
    # 18.8.). Poctivá odpověď přitom v datech JE — `hans_schedule.study_tick`
    # od HANS_SCHEDULE_LAST_OK_V1 rozlišuje „kdy to naposledy zkusilo" od
    # „kdy naposledy USPĚLO". Bez tohohle řádku Hans o dnešku buď mlčel, nebo
    # si ho přisvojil („dnes jsem prohluboval znalosti…“, ač studium neproběhlo).
    # HANS_STUDY_TODAY_SHARED_V1 — věta o dnešku má JEDNU implementaci
    # (`hans_study.today_line`), ať se výpis a volný hovor nerozejdou.
    try:
        from scripts.hans_study import today_line as _today_line
        _tl = _today_line(_recall_db(handler))
        if _tl:
            out.append(_tl)
            out.append("")
    except Exception as _te:
        _log.debug("/studium: dnešní řádek nešel sestavit (%s)", _te)
    out.append("Sessions: %d  |  ručně: /studium teď, /studium přeskoč"
               % ap["sessions_done"])
    # HANS_STUDY_RECALL_V1 — fronta pending (aby bylo jasné, co JEŠTĚ NENÍ
    # nastudováno; jinak by se dalo splést zařazené s hotovým).
    try:
        pend = [p for p in store.all_programs() if p.get("status") == "pending"]
        if pend:
            out.append("Ve frontě ke studiu (zatím nenastudováno): %s"
                       % ", ".join(p["topic"] for p in pend))
    except Exception:
        pass
    return NL_RUNTIME.join(out)


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
        _ORIGIN_PAT.pattern,
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


def _cmd_smer(handler, name, args) -> str:
    """/smer — aktivní směr + čekající návrh; /smer schválit|ne; /smer teď =
    zvaž směr na pozadí; /smer <text> = zadej vlastní směr."""
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db") or (cfg.get("diary", {}) or {}).get("db_path")
          or "data/hans_diary.db")
    try:
        from scripts.hans_direction import HansDirection, DirectionStore
    except Exception as e:
        return "Modul směru nedostupný: %s" % e
    st = DirectionStore(cfg, db)
    sub = (args or "").strip()
    low = sub.lower()

    # schválit / zamítnout čekající návrh
    if low in {"schválit", "schvalit", "schval", "ano", "ok", "approve"}:
        a = st.approve()
        if not a:
            return "Žádný čekající návrh směru ke schválení."
        return "Přijato za svůj směr: „%s\"" % a["direction"]
    if low in {"ne", "zamítnout", "zamitnout", "zamítni", "zamitni", "reject"}:
        if st.pending():
            st.reject()
            return "Návrh směru zamítnut. Zůstávám u dosavadního (pokud nějaký byl)."
        return "Žádný čekající návrh směru."

    # spustit úvahu o směru na pozadí
    if low in {"teď", "ted", "now", "zvaž", "zvaz"}:
        import threading as _th

        def _run():
            try:
                r = HansDirection(cfg, db).evaluate()
                if r.get("decision") in ("propose", "evolve") and r.get("message"):
                    tg = getattr(handler, "telegram", None)
                    if tg is not None and hasattr(tg, "send_proactive"):
                        try:
                            tg.send_proactive(r["message"])
                        except Exception:
                            pass
            except Exception as _e:
                _log.warning("/smer teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True).start()
        return ("Zamýšlím se nad svým směrem — ohlédnu se za studiem a tvorbou. "
                "Když z toho vzejde záměr, dám vědět (chvíli to potrvá).")

    # zadat vlastní směr (uživatelem autorizovaný → rovnou aktivní).
    # HANS_DIRECTION_NL_ARG_GUARD_V1: jen oznamovací věta, ne otázka z NL shody.
    if sub and low not in {"stav", "status"} and _smer_is_custom(sub):
        pid = st.propose(sub, "zadáno uživatelem", "", "user")
        st.approve(pid)
        return "Nastaven tvůj směr: „%s\"" % sub

    # výpis (default / stav)
    cur = st.current_active()
    pend = st.pending()
    out = []
    if cur:
        out.append("🧭 Můj směr: „%s\"" % cur["direction"])
        if cur.get("rationale"):
            out.append("   %s" % cur["rationale"])
    else:
        out.append("Zatím nemám vědomě zvolený směr.")
    if pend:
        out.append("")
        out.append("⏳ Čeká na tvé rozhodnutí: „%s\"" % pend["direction"])
        out.append("   (/smer schválit — /smer ne — /smer <vlastní text>)")
    elif not cur:
        out.append("(/smer teď — zvážím ho ze studia a tvorby)")
    # HANS_ART_INTENT_V1 (5.8.) — trvalé tvůrčí záměry patří ke směru: destiluje
    # je reflexe tvorby z reálných děl, tak ať jsou vidět a dají se ověřit.
    try:
        from scripts.hans_art_intent import active_intentions as _ai
        _ints = _ai(db)
    except Exception:
        _ints = []
    if _ints:
        out.append("")
        out.append("🎨 V tvorbě sleduju:")
        for _t in _ints:
            out.append("   • %s" % _t)
    return NL_RUNTIME.join(out)


register(
    "smer",
    slash_aliases=["smer", "směr", "smetr", "direction", "aspirace"],
    nl_patterns=_SMER_NL,
    handler=_cmd_smer,
    help_text="Vlastní směr/aspirace: /smer [schválit|ne|teď|<vlastní text>]",
)


# ─── /dilo — autorský projekt (HANS_AUTHORSHIP_V1) ───────────────────────────
def _cmd_dilo(handler, name, args) -> str:
    """/dilo — stav autorského projektu; /dilo vše = všechny; /dilo teď = napiš
    další sekci na pozadí (jinak noční práce)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_authorship import AuthorshipStore, run_writing_session
    except Exception as e:
        return "Autorský modul nedostupný: %s" % e
    store = AuthorshipStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now", "piš", "pis", "session"}:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                _log.info("/dilo teď → %s", run_writing_session(cfg, db, knowledge=kn))
            except Exception as _e:
                _log.warning("/dilo teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="WriteNow").start()
        return ("Pustil jsem se do psaní, pane — napíšu další sekci svého díla. "
                "Chvíli to potrvá, výsledek pak uvidíte v /dilo a v deníku.")

    if sub in {"vše", "vse", "all", "projekty"}:
        projs = store.all_projects()
        if not projs:
            return "Zatím jsem nezačal žádné dílo, pane."
        out = ["Má díla:"]
        for p in projs:
            out.append("  [%d] „%s\" (%s) — %s (%d/%d sekcí)" % (
                p["id"], p["title"], p["kind"], p["status"],
                p["current_index"], len(p["outline"])))
        return NL_RUNTIME.join(out)

    ap = store.get_active()
    if not ap:
        projs = store.all_projects()
        if projs:
            last = projs[0]
            return ("Právě nepíšu, pane. Naposledy: „%s\" (%s). Najdete ho "
                    "v data/works/. Další dílo si vyberu z trvalého koníčku. "
                    "(/dilo vše, /dilo teď)" % (last["title"], last["status"]))
        return ("Zatím jsem nezačal psát, pane — vyberu si trvalý koníček a "
                "navrhnu dílo. (/dilo teď to spustí ručně)")

    cur, total = ap["current_index"], len(ap["outline"])
    out = ["Píšu: „%s\" (%s) — sekce %d z %d:" % (
        ap["title"], ap["kind"], cur + 1 if cur < total else total, total)]
    if ap.get("premise"):
        out.append("   námět: %s" % ap["premise"])
    for i, s in enumerate(ap["outline"]):
        mark = "✓" if i < cur else ("→" if i == cur else " ")
        out.append("   %s %s" % (mark, s))
    out.append("")
    out.append("Sessions: %d  |  ručně: /dilo teď" % ap["sessions_done"])
    return NL_RUNTIME.join(out)


register(
    "dilo",
    slash_aliases=["dilo", "dílo", "psani", "psaní", "kniha_moje"],
    nl_patterns=[],
    handler=_cmd_dilo,
    help_text="Autorský projekt: /dilo [vše|teď]",
)


# ─── /napad — vlastní nápady / synteze (HANS_SYNTHESIS_IDEAS_V1, #2) ──────────
def _cmd_napad(handler, name, args) -> str:
    """/napad — poslední Hansův postřeh; /napad vše = všechny; /napad teď =
    propoj věci z různých oblastí do nového postřehu (na pozadí, jinak v noci)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_ideas import IdeaStore, run_synthesis_session
    except Exception as e:
        return "Modul nápadů nedostupný: %s" % e
    store = IdeaStore(cfg, db)
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now", "synteze", "syntéza"}:
        import threading as _th
        kn = getattr(handler, "_knowledge", None) or getattr(handler, "knowledge", None)

        def _run():
            try:
                _log.info("/napad teď → %s",
                          run_synthesis_session(cfg, db, knowledge=kn))
            except Exception as _e:
                _log.warning("/napad teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="SynthNow").start()
        return ("Zkusím propojit pár věcí, co jsem se dozvěděl, pane — chvíli to "
                "potrvá. Postřeh pak najdete v /napad a v deníku.")

    if sub in {"vše", "vse", "all"}:
        ideas = store.all_ideas()
        if not ideas:
            return "Zatím mě nic nového nenapadlo, pane."
        import time as _t
        out = ["Mé postřehy:"]
        for it in ideas:
            day = _t.strftime("%-d.%-m.", _t.localtime(it["ts"]))
            out.append("  [%s] %s" % (day, (it["topics"] or "—")))
            out.append("     %s" % (it["insight"] or ""))
        return NL_RUNTIME.join(out)

    last = store.latest()
    if not last:
        return ("Zatím mě nic nového nenapadlo, pane — propojím věci z různých "
                "oblastí, které jsem si přečetl. (/napad teď to spustí ručně)")
    import time as _t
    day = _t.strftime("%-d.%-m.", _t.localtime(last["ts"]))
    return NL_RUNTIME.join([
        "Poslední postřeh (%s) — propojil jsem: %s" % (day, last["topics"] or "—"),
        "", last["insight"] or "",
        "", "ručně: /napad teď"])


register(
    "napad",
    slash_aliases=["napad", "nápad", "napady", "nápady", "synteze", "syntéza"],
    nl_patterns=[],
    handler=_cmd_napad,
    help_text="Vlastní nápady / synteze: /napad [vše|teď]",
)


# ─── /kritika — sebekritika z vlastního popudu (HANS_SELFCRITIQUE_V1, #6) ─────
def _cmd_kritika(handler, name, args) -> str:
    """/kritika — nedávná Hansova ponaučení o kvalitě vlastního projevu;
    /kritika teď = projdi své poslední repliky a vezmi si ponaučení (na pozadí)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_selfcritique import (
            recent_selfcritiques, run_self_critique)
    except Exception as e:
        return "Modul sebekritiky nedostupný: %s" % e
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now"}:
        import threading as _th

        def _run():
            try:
                _log.info("/kritika teď → %s", run_self_critique(cfg, db))
            except Exception as _e:
                _log.warning("/kritika teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="SelfCritique").start()
        return ("Projdu si své poslední odpovědi, pane, a vezmu si z nich "
                "ponaučení. Chvíli to potrvá — pak je uvidíte v /kritika.")

    crits = recent_selfcritiques(db, hours=24 * 30, limit=10)
    if not crits:
        return ("Zatím jsem si žádné ponaučení o vlastním projevu nevzal, pane. "
                "(/kritika teď to spustí ručně)")
    out = ["Co u sebe chci zlepšit:"]
    for c in crits:
        out.append("  • %s" % c)
    return NL_RUNTIME.join(out)


register(
    "kritika",
    slash_aliases=["kritika", "sebekritika", "sebereflexe"],
    nl_patterns=[],
    handler=_cmd_kritika,
    help_text="Sebekritika vlastního projevu: /kritika [teď]",
)


# ─── /dashboard — Hansův návrh vlastní nástěnky (HANS_DASHBOARD_PROPOSAL_V1) ──
def _cmd_dashboard(handler, name, args) -> str:
    """/dashboard — Hansova designová kritika + návrh vlastní nástěnky;
    /dashboard teď = vygeneruj hned (i bez dokončeného studia, na pozadí)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.hans_dashboard import latest_proposal, run_dashboard_proposal
    except Exception as e:
        return "Modul návrhu nástěnky nedostupný: %s" % e
    sub = (args or "").strip().lower()

    if sub in {"teď", "ted", "now"}:
        import threading as _th

        def _run():
            try:
                _log.info("/dashboard teď → %s",
                          run_dashboard_proposal(cfg, db, force=True))
            except Exception as _e:
                _log.warning("/dashboard teď selhalo: %s", _e)
        _th.Thread(target=_run, daemon=True, name="DashboardProposal").start()
        return ("Zamyslím se nad podobou své nástěnky, pane — kritika i návrh "
                "chvíli potrvají (a zkusím i obrazový mockup). Pak /dashboard.")

    p = latest_proposal(db)
    if not p:
        return ("Návrh své nástěnky jsem zatím nesepsal — přijde sám po "
                "dostudování designu, nebo ho vyžádejte přes /dashboard teď.")
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(p["ts"]).strftime("%d.%m. %H:%M")
    out = f"Můj návrh nástěnky ({when}):\n\n{p['text']}"
    if p.get("path"):
        out += f"\n\n(Mockup: {p['path']} — najdete v galerii.)"
    return out


register(
    "dashboard",
    slash_aliases=["dashboard", "nastenka", "nástěnka"],
    nl_patterns=[],
    handler=_cmd_dashboard,
    help_text="Hansův návrh vlastní nástěnky: /dashboard [teď]",
)


# AVATAR_CMD_V1 — ruční inspekce/refresh vizuálního descriptoru (fáze 2 avatara).
_AVATAR_GEN = {"gen", "generuj", "nový", "novy", "znovu", "teď", "ted", "refresh"}


def _cmd_avatar(handler, name, args) -> str:
    """/avatar — vizuální descriptor: bez argumentu ukáže aktuální, 'gen' přegeneruje
    z identity (CORE + tendence + koníčky). Render obrázku = fáze 3 (zatím TBD)."""
    cfg = getattr(handler, "config", {}) or {}
    db = cfg.get("diary_db", "data/hans_diary.db")
    try:
        from scripts.avatar_descriptor import (
            latest_descriptor, generate_descriptor, _save_descriptor,
            render_signature, needs_rerender, ALL_FIELDS)
    except Exception as e:
        return "Avatar modul nedostupný: %s" % e

    sub = (args or "").strip().lower()
    _RENDER_NOTE = ("⚠ Obrázek se zatím negeneruje — render (fáze 3, ComfyUI) "
                    "není postaven. Tohle je jen popis vzhledu.")

    def _fmt(d):
        lines = ["Podoba v%d:" % d.get("version", 0)]
        for f in ALL_FIELDS:
            lines.append("  %s: %s" % (f, d.get(f, "")))
        lines.append("  signature: %s" % render_signature(d))
        return NL_RUNTIME.join(lines)

    if sub in _AVATAR_GEN:
        prev = latest_descriptor(db)
        new = generate_descriptor(cfg, db, prev=prev)
        if not new:
            return "Nepodařilo se vygenerovat descriptor (qwen/Ollama nedostupná? viz log)."
        if needs_rerender(prev, new):
            _save_descriptor(db, new)
            tail = "Uloženo jako v%d (čeká na render)." % new["version"]
        else:
            tail = "Vzhled se neposunul (charakter stejný) — neukládám novou verzi."
        return NL_RUNTIME.join([_fmt(new), "", tail, _RENDER_NOTE])

    cur = latest_descriptor(db)
    if not cur:
        return ("Zatím žádná podoba. /avatar gen ji vygeneruje z aktuální identity "
                "(CORE + tendence + koníčky). " + _RENDER_NOTE)
    return NL_RUNTIME.join([_fmt(cur), "",
                            "/avatar gen = přegeneruj z aktuální identity.", _RENDER_NOTE])


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


def _cmd_misto(handler, name, args) -> str:
    """/misto — model domova (kde Hans je). Bez argumentu vypíše model.
    /misto okno <text> | dvere <text> | vedle <text> | mistnost <text> |
    rozlozeni <text> | pozn <text> = přidá fakt. /misto smaz <id> = odebere."""
    cfg = getattr(handler, "config", {}) or {}
    db = (cfg.get("diary_db")
          or (cfg.get("hans_idle", {}) or {}).get("diary_db")
          or "data/hans_diary.db")
    try:
        from scripts.hans_place import PlaceStore
        store = PlaceStore(cfg, db)
    except Exception as e:
        return "Modul místa nedostupný: %s" % e

    parts = (args or "").strip().split(maxsplit=1)
    sub = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""

    # namaluj domov (HANS_PLACE_PAINT_V1) — Hans vyrenderuje, jak si dům představuje
    if sub in {"obraz", "namaluj", "render", "nakresli"}:
        import threading as _t
        try:
            from scripts import hans_art
        except Exception as e:
            return "Malování není dostupné: %s" % e
        if not hans_art.comfy_available(cfg):
            return ("Bohužel, pane — výtvarná dílna (ComfyUI na PC) teď neběží, "
                    "tak domov namalovat nemohu. Zkuste to, až bude PC vzhůru.")
        if not store.get_facts():
            return ("Nemám zatím žádný model domova, pane — nejdřív mi ho popište "
                    "(/misto …) nebo nechte fotky v data/room_photos/.")

        def _worker():
            try:
                # Textový render (hezčí výsledek než img2img z reálné fotky).
                # paint_home_from_photo zůstává v hans_art pro budoucí použití.
                hans_art.render_home_now(cfg, db)
            except Exception as _e:
                _log.warning("/misto obraz render selhal: %s", _e)
        _t.Thread(target=_worker, daemon=True).start()
        return ("Dám se do toho, pane — maluji, jak si představuji svůj domov. "
                "Za chvíli se objeví na nástěnce (Co Hans namaloval). "
                "Chat může být asi minutu zaneprázdněný.")

    # smazání faktu
    if sub in _MISTO_DEL:
        if not rest.isdigit():
            return "Uveďte id: /misto smaz <id> (viz /misto)."
        ok = store.remove_fact(int(rest))
        return ("Fakt %s jsem odebral, pane." % rest if ok
                else "Ten fakt se nepodařilo odebrat (špatné id?).")

    # přidání faktu
    if sub in _MISTO_SUBS:
        if not rest:
            return "Doplňte text: /misto %s <popis>." % sub
        cat = _MISTO_SUBS[sub]
        fid = store.add_fact(cat, rest, source="user")
        if cat == "room":
            return "Zapsal jsem, že jsem v místnosti: %s." % rest
        return "Zapsal jsem fakt o místě [%s] (id %s)." % (
            _MISTO_CAT_LABEL.get(cat, cat), fid)

    if sub:
        return ("Neznámá část. Použij: /misto [mistnost|okno|dvere|vedle|"
                "rozlozeni|pozn] <text>, /misto smaz <id>, nebo /misto pro výpis.")

    # výpis modelu
    facts = store.get_facts()
    if not facts:
        return ("O svém místě zatím nic nevím, pane. Můžete mi to popsat: "
                "/misto mistnost <text>, /misto okno <text>, /misto vedle <text> … "
                "Nebo nechte širší fotku místnosti ve složce data/room_photos/ "
                "(udělám si z ní představu při startu).")
    by_cat: dict = {}
    for f in facts:
        by_cat.setdefault(f["category"], []).append(f)
    out = ["Můj model domova (kde jsem):"]
    order = ["room", "window", "door", "neighbor", "layout", "note", "mental_map"]
    for cat in order:
        for f in by_cat.get(cat, []):
            out.append("   [%d] %s: %s" % (
                f["id"], _MISTO_CAT_LABEL.get(cat, cat), f["content"]))
    out.append("")
    out.append("Přidat: /misto okno <text> (i dvere/vedle/mistnost/rozlozeni/pozn)  "
               "|  smazat: /misto smaz <id>  |  namalovat domov: /misto obraz")
    return NL_RUNTIME.join(out)


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


def _cmd_vzpominka(handler, name, args) -> str:
    from scripts.hans_recall import first_memory_answer
    out = first_memory_answer(_recall_db(handler))
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


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
        r"jak\s+dlouho\s+(u[žz]\s+)?(si\s+)?(vede[šs]|p[íi][šs]e[šs]|m[áa][šs])"
        r"\s+(ten\s+)?den[íi]k",
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


def _cmd_cetl(handler, name, args) -> str:
    from scripts.hans_recall import reading_answer
    q = (args or "").strip()
    # HANS_LLM_ROUTE_ARGS_V2 — LLM router předává args="" (záměrná pojistka proti
    # mutujícím podpříkazům), takže routovaný dotaz by ztratil TÉMA a spadl na
    # „poslední čtení". Příkaz je čistě ČTECÍ → původní věta se dá vzít z vlákna.
    # `reading_answer` si téma vytáhne samo (`_extract_topic`), proto celá věta.
    # Týž vzor jako `_cmd_rozhovory` (6.8.) a `_cmd_videl` (7.8.).
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    out = reading_answer(_recall_db(handler), q)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "cetl",
    slash_aliases=["cetl", "četl", "cteni", "čtení"],
    nl_patterns=[
        r"\bco\s+(jsi|sis)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
        r"(pře)?[čc]etl",
        r"\bcos?\s+(dnes\w*\s+|včera\s+|naposledy\s+)?[čc]etl",
        r"\bkdy\s+(jsi|sis)\s+[čc]etla?\b",
        r"\b(pře)?[čc]etla?\s+(jsi|sis)\s+(něco|neco|někdy|nekdy|už|uz)?\s*o?\b.{2,}\?",
        r"\bco\s+(pr[áa]vě\s+|te[ďd]\s+)?[čc]te[šs]\b",
    ],
    handler=_cmd_cetl,
    help_text="Co/kdy jsem četl (přímo z deníku): co jsi četl? četl jsi o X?",
)


# ─── /zdroje — odkud Hans čerpal (HANS_SOURCES_V1) ─────────────────────────
def _cmd_zdroje(handler, name, args) -> str:
    """Vypíše odkazy na to, co Hans četl. Deterministicky z deníku.

    Bez argumentu: posledních pár čtení. S argumentem: filtr na téma
    („zdroje vrak"). Co odkaz nemá, se přizná — nikdy se nedomýšlí.
    """
    import sqlite3 as _sq
    from datetime import datetime as _dt
    db = _recall_db(handler)
    # ⚠️ U NL vzorů přijde jako `args` CELÁ zpráva (parse_command vrací msg),
    # takže „odkud jsi vlastně čerpal?" by se hledalo jako téma a nic nenašlo.
    # Téma se proto tahá týmž extraktorem jako u /cetl; prázdné = vypiš poslední.
    raw = (args or "").strip()
    q = ""
    try:
        from scripts.hans_recall import _extract_topic
        q = (_extract_topic(raw) or "").strip().lower()
    except Exception:
        pass
    if not q and raw and len(raw.split()) <= 3 and "?" not in raw:
        q = raw.lower()          # slash forma: /zdroje vrak
    try:
        cx = _sq.connect("file:%s?mode=ro" % db, uri=True, timeout=5.0)
        if q:
            # HANS_SOURCES_TOPIC_V1 (21.8.) — hledat přes PAHÝLY, ne přes holé
            # téma. `_topic_stems` (české skloňování) tu existuje odjakživa,
            # jen je /zdroje jako jediné nepoužívalo → „normalizaci" by nikdy
            # nesedlo na zapsané „normalizace" a dotaz by spadl na obecný výpis.
            try:
                from scripts.hans_recall import _topic_stems
                _stems = _topic_stems(q) or [q]
            except Exception:
                _stems = [q]
            # ⚠️ Volné `LIKE %pahýl%` je pro češtinu PAST: „Říp" → pahýl
            # „říp" sedne doprostřed slova „případ" → na dotaz o Řípu vyšel
            # Retrográdní pohyb a Rozsudky soudce Ooky (změřeno při stavbě).
            # Skloňování mění KONEC slova, ne začátek → shoda musí začínat
            # na hranici slova (začátek textu nebo běžný oddělovač).
            _predpony = ("", " ", "„", "(", "\"", "\n")
            _casti, _args = [], []
            for _s in _stems:
                _sl = _s.lower()
                for _p in _predpony:
                    _vzor = ("%s%%" % _sl) if _p == "" else ("%%%s%s%%" % (_p, _sl))
                    _casti.append("lower(title) LIKE ?")
                    _args.append(_vzor)
                    _casti.append("lower(COALESCE(note,'')) LIKE ?")
                    _args.append(_vzor)
            _kde = " OR ".join(_casti)
            rows = cx.execute(
                "SELECT ts, title, source_url, COALESCE(note,'') FROM diary "
                "WHERE event_type IN ('web_read','reading_takeaway','study_note') "
                "AND (" + _kde + ") "
                "ORDER BY ts DESC LIMIT 12", _args).fetchall()
            # HANS_TOPIC_ENTITY_AWARE_V1 (21.8.) — je téma ZNÁMÁ OSOBA? Pak
            # nestačí pahýl jména: „svobod" sedne na „Svobodné zednářství"
            # i na „svobodou projevu". U osoby se žádá CELÉ jméno (změřeno:
            # dotaz na Václava Svobodu vracel i zednářství, hymnu a Kajínka).
            try:
                from scripts.hans_recall import (tema_entita, jmeno_entity,
                                                 osoba_sedi)
                _osoba = jmeno_entity(tema_entita(q))
                if _osoba:
                    rows = [r for r in rows
                            if osoba_sedi("%s %s" % (r[1] or "", r[3] or ""),
                                          _osoba)]
            except Exception:
                pass
            rows = [(r[0], r[1], r[2]) for r in rows][:8]
        else:
            rows = cx.execute(
                "SELECT ts, title, source_url FROM diary "
                "WHERE event_type IN ('web_read','reading_takeaway','study_note') "
                "ORDER BY ts DESC LIMIT 6").fetchall()
        cx.close()
    except Exception:
        return "Nepodařilo se mi teď nahlédnout do zápisků, pane."

    if not rows:
        # HANS_SOURCES_TOPIC_V1 — u pojmenovaného tématu přiznat i to, co
        # z prázdného výpisu plyne: řečené na žádném zapsaném zdroji nestojí.
        return ("K tomuhle nemám v zápiscích žádné čtení, pane — co jsem "
                "o tom říkal, tedy nestojí na žádném mém zdroji."
                if q else "Zatím jsem si nic nezapsal, pane.")

    # HANS_SOURCES_DEDUP_V1 (19.8.) — týž článek má v deníku `web_read`
    # I `reading_takeaway`, takže jedno čtení vyšlo dvakrát; a když se čtení
    # opakovalo (viz HANS_CURIOSITY_COOLDOWN_PERSIST_V1), vypsal se výpis
    # třikrát tentýž řádek. Doloženo 19.8.: 3× „Třetí skoba pro Kocoura"
    # v obou sekcích. Dedup na (titul, url), nejnovější výskyt vyhrává.
    # HANS_SOURCES_DEDUP_V2 — dedup na (titul, url) NESTAČIL: týž článek má
    # `web_read` S odkazem i `reading_takeaway` BEZ něj, takže vyšel v OBOU
    # sekcích naráz („mám odkaz" i „odkaz jsem si neuložil" o tomtéž).
    # Klíč je proto SAMOTNÝ TITUL a vyhrává výskyt S ODKAZEM.
    _best = {}
    for ts, title, url in rows:
        t = str(title or "")[:70]
        k = t.lower()
        prev = _best.get(k)
        if prev is None or (url and not prev[2]):
            _best[k] = (ts, t, url)
    s_url, s_bez = [], []
    for ts, t, url in sorted(_best.values(), key=lambda x: -x[0]):
        d = _dt.fromtimestamp(ts).strftime("%d.%m.")
        (s_url if url else s_bez).append((d, t, url))

    out = []
    if s_url:
        # HANS_SOURCES_TOPIC_V1 — „Četl jsem tohle" u tématického dotazu
        # tvrdí PŘÍČINU (odtud to mám), kterou Hans vědět nemůže: generace
        # si původ nenese. U tématu se proto tvrdí jen fakt — tohle čtení
        # k tématu mám zapsané. Bez tématu je původní znění v pořádku.
        out.append("K tomuhle mám v zápiscích tohle čtení, pane:" if q
                   else "Četl jsem tohle, pane:")
        for d, t, u in s_url:
            out.append("• %s %s — %s" % (d, t, u))
    if s_bez:
        if s_url:
            out.append("")
        out.append("U tohohle mám zápisek, ale odkaz jsem si tehdy neuložil "
                   "(ukládám ho až od 12. srpna) — nerad bych ho domýšlel:")
        for d, t, _ in s_bez:
            out.append("• %s %s" % (d, t))
    return "\n".join(out)


register(
    "zdroje",
    slash_aliases=["zdroje", "odkazy", "literatura", "zdroj", "odkaz"],
    nl_patterns=[
        r"\bodkud\s+(jsi|si|to)\s+.{0,12}(čerpal|cerpal|m[áa][šs]|vz[áa]l|v[íi][šs])",
        r"\b(z\s+)?[čc]eho\s+(jsi|si)\s+.{0,10}(čerpal|cerpal|vych[áa]zel)",
        r"\bd[áa][šs]\s+(mi\s+)?odkaz",
        r"\bkde\s+(jsi|si)\s+(to\s+)?(četl|cetl|na[šs]el|vzal)",
        r"\bjak[ýy]\s+(je\s+)?(ten\s+)?zdroj",
        r"\bposli\s+(mi\s+)?odkaz|\bpo[šs]li\s+(mi\s+)?odkaz",
    ],
    handler=_cmd_zdroje,
    help_text="Odkazy na to, co jsem četl (přímo z deníku): odkud jsi čerpal?",
)


def _cmd_videl(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    from scripts.hans_recall import last_seen_answer
    q = args or ""
    # HANS_LLM_ROUTE_SUBJECT_V1 (7.8.) — když příkaz vybral LLM router, args
    # jsou PRÁZDNÉ schválně (aby nemohl spustit mutující podpříkaz). Tady tím
    # ale zmizí OSOBA, na kterou se uživatel ptá, a `_resolve_person` spadne
    # na tazatele → Hans odpoví o někom jiném, a sebejistě.
    # Doloženo 7.8. 11:29: „<jméno> doma neni?" → router vybral `videl`
    # s args='' → „Naposledy jsem VÁS viděl ve čtvrtek…". Totéž u „kdy jsi
    # viděl <jméno>?" — dotaz na jinou osobu odpoví o tazateli.
    # Řešení je TÝŽ vzorec, jaký už 6.8. dostal `_cmd_rozhovory` (tehdy
    # „co delal Kolac?" → sumář rozhovoru s tazatelem) — jen sem nebyl
    # protažen. Příkaz je čistě ČTECÍ, takže vzít původní větu je bezpečné.
    # ⚠️ NEDĚLAT plošně: `smer`, `studium`, `seznam`, `zdravi`, `nitky`
    # a `kalendar` mají mutující podpříkazy a prázdné args je před nimi chrání.
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    out = last_seen_answer(_recall_db(handler), cfg, q, name)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


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


# ─── /dnes — co se dnes dělo v domě (HANS_DAY_AT_HOME_V1) ───────────────────
def _cmd_dnes(handler, name, args) -> str:
    """Shrnutí dneška z deníku Hansovým hlasem. Fakta deterministicky,
    hlas je jen formuluje; bez mozku se vypíšou fakta holá."""
    cfg = getattr(handler, "config", {}) or {}
    from scripts.hans_recall import day_facts, day_fact_lines
    f = day_facts(_recall_db(handler))
    lines = day_fact_lines(f, cfg)
    if not f.get("n_events"):
        return "K dnešku nemám v deníku zatím žádný záznam, pane."
    plain = NL_RUNTIME.join("• " + l for l in lines)

    # Bez mozku (herní mód / PC dole) NEČEKEJ a vrať fakta holá —
    # deferral-safe, vzor `_night_reflection` → statistika.
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(cfg):
            return "Dnešek podle mého deníku, pane:" + NL_RUNTIME + plain
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
        system = (core + "\n\n" if core else "") + (
            "Pán domu se ptá, co se dnes v domě dělo. Odpověz souvisle "
            "(3-5 vět, první osoba, tvým hlasem) — kdo tu byl a kdy, co "
            "běželo na televizi, co stálo za zmínku. Vyjdi POUZE z faktů "
            "níže; co v nich není, se nestalo — nic si nepřimýšlej a nic "
            "nedomýšlej o důvodech. Žádný nadpis, žádné uvozovky, žádný "
            "výčet s odrážkami.\n"
            # HANS_DAY_AT_HOME_EXACT_V1 (7.8.) — hlasový krok komolil PŘESNÁ
            # data: „10:03–10:15" přepsal na „mezi desátou minutou třetí
            # a čtvrtou minutou" a počet 23 na „dvacet čtyři krát". Persona
            # smí formulovat, ale ne přepočítávat.
            "ČASY A ČÍSLA opiš PŘESNĚ tak, jak jsou ve faktech (např. "
            "„10:03–10:15\", „23\") — nepřepisuj je slovy ani nezaokrouhluj. "
            "Oslovení „pan/paní\" u jmen zachovej, jak je uvedeno.")
        out = ollama_generate(
            model, "FAKTA DNEŠNÍHO DNE:\n" + NL_RUNTIME.join(lines)
            + "\n\nShrň to pánovi.",
            system=system, config=cfg, timeout=90)
        txt = (out or "").strip().strip('"')
        if len(txt) >= 60:
            return txt[:1200]
    except Exception as e:
        _log.warning("/dnes: hlasový krok selhal (%s) — vracím fakta", e)
    return "Dnešek podle mého deníku, pane:" + NL_RUNTIME + plain


register(
    "dnes",
    slash_aliases=["dnes", "dnesek", "den"],
    nl_patterns=[
        r"co\s+se\s+(dnes|dneska|d[ňn]es)\w*\s+(d[ěe]lo|stalo|ud[áa]lo)",
        r"co\s+se\s+(d[ěe]lo|stalo|ud[áa]lo)\s+(dnes|dneska)",
        r"co\s+(bylo|se\s+d[ěe]lo)\s+(dnes\s+)?(doma|v\s+dom[ěe])",
        r"jak[ýy]\s+byl\s+(dnes(n[íi])?)?\s*den",
        r"shr[nň]\s+(mi\s+)?(dnes(ek|n[íi]\s+den)?)",
    ],
    handler=_cmd_dnes,
    help_text="Co se dnes dělo v domě (z deníku): co se dnes dělo?",
)


# ─── /rezim — vlastní provozní stav (HANS_REZIM_SHORTCIRCUIT_V1) ────────────
def _cmd_rezim(handler, name, args) -> str:
    """Spím/bdím + hlídání — přímo ze stavu, ŽÁDNÝ LLM.

    Doloženo 7.8.: i když měl model fakt „teď: jsem vzhůru" v promptu (blok
    894 zn), odpověděl „Ano, jsem v režimu spánku." Fakta v promptu tenhle
    případ neuhlídají — proto deterministická odpověď, vzor `/vzpominka`.
    """
    _sleeping = None
    try:
        # ⚠️ `_routine` drží `hans_idle`, ne handler (vzorec TIME_AWARENESS_V1).
        _hi = getattr(handler, "_hans_idle", None)
        _rt = getattr(_hi, "_routine", None) if _hi else None
        if _rt is not None:
            _sleeping = bool(getattr(_rt, "_sleeping", False))
    except Exception:
        pass
    _guard = None
    try:
        import json as _js
        import os as _os
        _gp = "data/.hans_guard"
        if _os.path.exists(_gp):
            with open(_gp, encoding="utf-8") as _f:
                _guard = bool((_js.load(_f) or {}).get("armed"))
        else:
            _guard = False
    except Exception:
        pass
    if _sleeping is None and _guard is None:
        return "Svůj stav teď nedokážu spolehlivě zjistit, pane."
    out = []
    if _sleeping is not None:
        out.append("Ne, pane, nespím — jsem vzhůru a v běžném provozu."
                   if not _sleeping else
                   "Ano, pane, jsem v nočním režimu (spánek).")
    if _guard is not None:
        out.append("Hlídací režim mám %s." % ("zapnutý" if _guard else "vypnutý"))
    return " ".join(out)


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


def _cmd_film(handler, name, args) -> str:  # HANS_RECALL_FILM_V1
    from scripts.hans_recall import films_watched_answer
    out = films_watched_answer(_recall_db(handler), args or "")
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


register(
    "film",
    slash_aliases=["film", "filmy"],
    nl_patterns=[
        r"posledn[ií].{0,10}film",
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
        r"co\s+(jsi|sis)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
        r"(vid[ěe]l|koukal|sledoval|d[íi]val)",
        r"co\s+jsem?\s+(dnes\w*\s+)?(vid[ěe]l|koukal|sledoval)",
        r"\bfilm\w*\s+(jsi|sis)\s+(vid|koukal|sledoval)",
    ],
    handler=_cmd_film,
    help_text="Jaký film/pořad jsem viděl (přímo z deníku kodi_playing)",
)


def _cmd_rozhovory(handler, name, args) -> str:  # HANS_CHAT_SUMMARY_V1
    """Sumář toho, o čem se TAZATEL s Hansem bavil (deterministicky z deníku).
    Časová reference v dotazu („v pátek", „27. dubna 2026", „minulý týden")
    zúží období; bez ní = poslední den, kdy spolu mluvili. Delší období →
    témata; „připomeň rozhovor o X" → doslovné vybavení té výměny."""
    from scripts.hans_recall import (chat_summary, topic_conversation,
                                     _extract_conv_topic)
    cfg = getattr(handler, "config", {}) or {}
    q = args or ""
    # HANS_THREAD_V1 — když příkaz vybral LLM router, args jsou PRÁZDNÉ
    # (`resolve_command_llm` vrací `(cid, "")` schválně, aby se nespustil
    # mutující podpříkaz). Sumář rozhovorů tím ale ztratí celý dotaz a vždy
    # spadne na „poslední den" — doloženo živě 6.8.: „co delal Kolac?"
    # vrátilo sumář rozhovoru s TAZATELEM. Tenhle příkaz je čistě ČTECÍ,
    # takže původní věta se dá bezpečně vzít zpět z vlákna.
    if not q:
        try:
            _tc = getattr(handler, "_thread_ctx", None)
            if _tc and _tc[0]:
                q = str(_tc[0])
        except Exception:
            pass
    # HANS_THREAD_V1 — dotaz může mířit na rozhovor s TŘETÍ stranou (Koláč).
    # chat_summary umí jen `human_chat` (tazatel↔Hans) a `hans_recall`
    # dokonce vyřazuje `teddy_dialog` jako šum (_DIARY_NOISE) → vrátit místo
    # toho sumář JINÉHO rozhovoru je horší než přiznat, že to zatím neumím.
    # Doloženo 5.8. 19:23. Odpadne s vrstvou B (FTS nad všemi rozhovory).
    try:
        from scripts.hans_thread import third_party_scope, recent_turns
        # Vlákno je součást vstupu: „jste se o TOM bavili" neřekne, s KÝM —
        # to ví jen předchozí replika (doloženo živě 6.8.).
        _turns = recent_turns(handler, name)
        _tp = third_party_scope(q, cfg, turns=_turns)
        if _tp and _tp != "?":
            # HANS_CONVINDEX_V1 (6.8.) — hledej v ROZHOVORECH S KOLÁČEM
            # (`teddy_dialog`). Do 6.8. to nešlo vůbec: `hans_recall` ten
            # typ vyřazuje jako šum (_DIARY_NOISE) a `chat_summary` umí jen
            # `human_chat` → na „myslel jsem rozhovor s Kolacem" vracel
            # sumář rozhovoru s TAZATELEM (doloženo 5.8. 19:23).
            from scripts.hans_convindex import answer_about, topic_tokens
            # Hledej ROZŘEŠENOU větou — „v jakem kontextu jste se o tom
            # bavili" sama žádné téma nenese, to je v předchozí replice.
            _sq = q
            try:
                _tc = getattr(handler, "_thread_ctx", None)
                if _tc and _tc[2]:
                    _sq = "%s %s" % (q, _tc[2])
            except Exception:
                pass
            # Bez TÉMATU se nehledá: „co dělal Koláč?" je dotaz na STAV, ne
            # na rozhovor (jinak FTS vrátí náhodné staré dialogy — jméno je
            # v každém z nich, doloženo živě 6.8.). Odpoví na to ale TÁŽ
            # funkce jako agentní akce `kolac_status`, ne sumář rozhovoru
            # s tazatelem — jinak by dotaz na Koláče končil u výpisu „spolu
            # jsme vedli N výměn" (vzor HANS_UNIFY_ACTIONS_V1: jeden kód).
            if not topic_tokens(_sq, exclude=(_tp,)):
                from scripts.hans_agent import _run_kolac_status
                _st = _run_kolac_status(handler, {})
                if _st:
                    return _st
            if topic_tokens(_sq, exclude=(_tp,)):
                _hits = answer_about(_sq, source="teddy_dialog")
                if _hits:
                    # Jméno v 1. pádu — `cz_names` instrumentál neumí.
                    return ("%s a já jsme se o tom bavili, pane. Tady je, co "
                            "mám zapsáno:\n\n%s" % (_tp, _hits))
                return ("%s a já spolu rozprávíme, ale k tomuhle nemám "
                        "zapsaný žádný náš rozhovor, pane — a nebudu si ho "
                        "vymýšlet." % _tp)
    except Exception:
        pass
    topic = _extract_conv_topic(q)
    if topic:
        out = topic_conversation(_recall_db(handler), name, topic)
        if out:
            return out
    out = chat_summary(_recall_db(handler), name, q, config=cfg)
    return out or "Nepodařilo se mi teď nahlédnout do deníku, pane."


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
        r"(bavil|mluvil|pov[íi]dal)[iy]\s+jsme\s+(se\s+)?o\s+\w{3,}",
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


def _cmd_hlidej(handler, name, args) -> str:
    from scripts import hans_guard as g
    a = (args or "").strip().lower()
    # NL cesta posílá CELOU větu jako args → „vypni hlídání" nesmí režim
    # omylem ZAPNOUT. Rozhoduje záměr ve větě, ne přesná shoda.
    if re.search(r"\b(stop|vypni|vypnout|konec|off|zru[šs])\b", a):
        g.disarm()
        return "Hlídání jsem vypnul, pane. Snímky už posílat nebudu."
    if re.search(r"\b(stav|status)\b", a):
        return g.status_text()

    cfg = getattr(handler, "config", {}) or {}
    tg = getattr(handler, "telegram", None)
    if tg is None or not getattr(tg, "enabled", False):
        return ("Hlídat mohu, ale nemám kam poslat snímky — Matrix není "
                "zapojený, pane. Bez něj by poplach nikdo neviděl.")
    g.arm(by=name or "")
    _guard_camera_down(handler)
    c = (cfg.get("guard", {}) or {})
    return ("Hlídám, pane. Při pohybu nebo náhlé změně světla pošlu snímek "
            "na Matrix (nejvýš jednou za %d s, do %d snímků denně). "
            "Postupné rozednívání poplach nespustí. Kamera zůstane namířená "
            "do místnosti i v noci. Vypnout: /hlidej stop."
            % (int(c.get("cooldown_s", 60)), int(c.get("max_per_day", 60))))


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


def _cmd_preloz(handler, name, args) -> str:
    from scripts import hans_translate as ht
    a = (args or "").strip().lower()
    # NL cesta posílá CELOU větu → „jak jde ten překlad" se nesmí tvářit
    # jako povel spustit další (vzor _cmd_hlidej).
    if re.search(r"\b(seznam|v[ýy]pis|p[řr]elo[žz]en[éeý])\b"
                 r"|co\s+(jsi|u[žz]|v[šs]echno)\s+[\w\s]{0,25}?p[řr]elo[žz]"
                 r"|kter[ée]\s+[\w\s]{0,25}?p[řr]elo[žz]", a):
        return ht.seznam_text(cfg_of(handler))
    # ⚠️ Vzory MUSÍ počítat s psaním BEZ DIAKRITIKY — uživatel píše z mobilu.
    # Doloženo 28.8.: „uz mas hotovy preklad?" se k obsluze dostalo, ale
    # `hotov[oý]` neobsahovalo prosté „y", takže by to místo hlášení stavu
    # SPUSTILO DALŠÍ PŘEKLAD. nl_patterns se skládají i bez diakritiky
    # (nl_fold), tahle větev uvnitř obsluhy ne — proto tu jsou obě podoby.
    if re.search(r"\b(stav|status|hotovo|hotov[oýy])\b|jak\s+(to\s+)?(jde|pokra[čc]uje|dopadl)"
                 r"|u[žz]\s+(to\s+)?(je\s+)?(hotov|dod[ěe]l)"
                 r"|(m[áa][šs]|je)\s+[\w\s]{0,12}?hotov", a):
        return ht.stav_text()
    cfg = getattr(handler, "config", {}) or {}
    if not (cfg.get("translate", {}) or {}).get("enabled", True):
        return "Překládání dokumentů mám vypnuté, pane."
    return ht.spust_na_pozadi(cfg, handler)


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


def _cmd_vypnipc(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts import pc_remote
    except Exception:
        return "Na počítač teď nedosáhnu, pane."
    if not pc_remote.enabled(cfg):
        return ("Vzdálený přístup k počítači nemám povolený "
                "(pc_remote.enabled), pane — vypnout ho neumím.")
    if not _pc_ping(cfg):
        return "Počítač je už teď vypnutý (neodpovídá), pane. Nic nedělám."
    out = pc_remote.run(cfg, "sudo -n systemctl poweroff", timeout=10)
    if out is None:
        # poweroff často utne SSH spojení dřív, než stihne vrátit výstup —
        # proto None NEznamená selhání; rozhodne až ping.
        _log.info("/vypnipc: poweroff bez výstupu (SSH nejspíš utnuto)")
    import time as _t
    for _ in range(10):
        _t.sleep(3)
        if not _pc_ping(cfg):
            try:
                db = _recall_db(handler)
                import sqlite3 as _sql
                c = _sql.connect(db, timeout=5.0)
                c.execute("INSERT INTO diary (ts, event_type, title, note) "
                          "VALUES (?,?,?,?)",
                          (_t.time(), "pc_shutdown", "Vypnutí PC na povel",
                           "Na požádání jsem vypnul počítač."))
                c.commit()
                c.close()
            except Exception:
                pass
            return ("Počítač je vypnutý, pane. Můj mozek tím usnul — "
                    "ráno ho probudím, nebo si řekněte o /wol.")
    return ("Poslal jsem počítači povel k vypnutí, ale ještě odpovídá, pane. "
            "Možná se vypíná pomalu — nebo se něco vzpírá.")


register(
    "vypnipc",
    slash_aliases=["vypnipc", "vypnipc", "shutdown", "pcoff", "vypnout"],
    # HANS_UNIFY_ACTIONS_V1 — přirozená řeč („vypni pc") ZÁMĚRNĚ nemá regex:
    # vypnutí PC je destruktivní → padá k agentní akci pc_shutdown, která se
    # NAPŘED zeptá a vypíše, co na PC běží. Explicitní /vypnipc slash zůstává
    # okamžitý (výslovný povel = výslovný záměr). Ztráta při mozku dole = žádná
    # (PC dole ⇒ není co vypínat), slash funguje vždy.
    nl_patterns=[],
    handler=_cmd_vypnipc,
    help_text="Vypnu počítač (PC) — protějšek /wol",
)


# ─── /zdravi — zdraví závislostí (HANS_HEALTH_V1) ────────────────────────────
def _cmd_zdravi(handler, name, args) -> str:  # HANS_HEALTH_V1
    """Živá probe závislostí (Ollama/ComfyUI/Kodi/STT/PC/disk). /zdravi vylec =
    zkusí self-heal zaseklé Ollamy."""
    try:
        from scripts import hans_health
    except Exception as _e:
        return "Nemohu teď zkontrolovat své zdraví, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    do_heal = bool(args) and args.strip().lower() in (
        "vylec", "vyléč", "heal", "restart", "oprav")
    res = hans_health.run_health_check(cfg, heal=do_heal)
    health = res.get("health", {})
    if not health:
        return "Kontrola zdraví je vypnutá, pane."
    _lbl = {"ollama": "Mozek (Ollama)", "comfyui": "Malování (ComfyUI)",
            "kodi": "Televize (Kodi)", "stt": "Sluch (přepis)",
            "pc": "Počítač", "disk": "Disk", "camera": "Kamera",
            "schedule": "Rozvrh (autonomní rutiny)",
            # MATRIX_SYNC_HEARTBEAT_V1 — bez popisku by Hans hlásil holé
            # „matrix"; výpis jde přes health.items(), takže klíč bez labelu
            # projde, jen se nepřeloží.
            "matrix": "Most na telefon (Matrix)"}
    _ico = {"ok": "✅", "paused": "⏸️", "wedged": "⚠️", "down": "❌",
            "unknown": "❔", "warn": "⚠️"}
    lines = ["Stav mých systémů, pane:"]
    for k, s in health.items():
        st = s.get("status", "unknown")
        lines.append("%s %s — %s" % (_ico.get(st, "❔"), _lbl.get(k, k),
                                     s.get("detail", st)))
    # HANS_SCHEDULE_V1 — zvlášť vypsat KTERÉ rutiny zaostávají (detail)
    sched_stale = ((health.get("schedule") or {}).get("stale")) or []
    if sched_stale:
        lines.append("")
        lines.append("Zaostávající rutiny:")
        for x in sched_stale:
            reason = " [%s]" % x["last_skip_reason"] if x["last_skip_reason"] else ""
            lines.append("  • %s — %.1fh po termínu (max %.1fh)%s"
                         % (x["name"], x["late_s"] / 3600,
                            x["expected_gap_s"] / 3600, reason))
    if res.get("healed"):
        lines.append("(Zaseklý mozek jsem zkusil restartovat.)")
    # KOLAC_EXAM_V1 (22.8.) — jak jsem obstál ve zkoušení. Patří to sem, ne
    # do zvláštního příkazu: je to údaj o vlastním stavu, jako mozek či disk.
    try:
        from scripts import kolac_exam as _ke
        _souhrn = _ke.souhrn(_recall_db(handler) or "data/hans_diary.db")
        if _souhrn:
            lines.append("")
            lines.append(_souhrn)
    except Exception:
        pass
    return "\n".join(lines)


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


# ─── /nalez — co Koláč u Hanse našel (KOLAC_EXAM_CONFIRM_V1) ─────────────────
def _cmd_nalez(handler, name, args) -> str:
    """/nalez — neposouzené nálezy ze zkoušení; /nalez N = udělej z toho
    trvalou zkušební otázku; /nalez N ne = zamítni.

    Posuzuje ČLOVĚK. Stroj nález jen předloží a po potvrzení z něj udělá
    trvalý test — automatické učení z vlastní vymyšlené odpovědi by byla
    přesně ta otrava paměti, kvůli které zkoušení běží pod testovací identitou.
    """
    from scripts import kolac_exam as _ke
    db = _recall_db(handler) or "data/hans_diary.db"
    cfg = getattr(handler, "config", {}) or {}
    a = " ".join((args or "").split())
    _c = a.split()
    # Číslo nálezu bereme, JEN když je to číslo. Přirozený dotaz („co u tebe
    # našel Koláč?") dorazí sem s celou větou v argumentech — ta má vypsat
    # frontu, ne skončit na hlášce, že chybí číslo.
    if _c and _c[0].lstrip("#").isdigit():
        _id = _c[0].lstrip("#")
        _ne = len(_c) > 1 and _c[1].lower() in ("ne", "zamitnout", "zamítnout",
                                                "nic", "smaz", "smaž")
        return (_ke.zamitni(db, int(_id)) if _ne
                else _ke.potvrd(db, int(_id), cfg))
    polozky = _ke.nalezy(db)
    if not polozky:
        return ("Ze zkoušení nemám nic nevyřízeného, pane.")
    import time as _t
    radky = ["Co u mě Koláč našel a čeká na vaše posouzení:"]
    for p in polozky:
        radky.append("  #%d [%s] %s — %s (%s)" % (
            p["id"], _t.strftime("%-d.%-m. %H:%M", _t.localtime(p["ts"])),
            _ke._POPIS.get(p["verdikt"], p["verdikt"]),
            (p["tema"] or "")[:60], p["zdroj"]))
    radky.append("")
    radky.append("Potvrdit jako trvalou otázku: /nalez <číslo>. "
                 "Zamítnout: /nalez <číslo> ne.")
    return "\n".join(radky)


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


# ─── /nastroj — Hans si najde LLM nástroj pro dílo (HANS_TOOLSCOUT_V1) ────────
def _cmd_nastroj(handler, name, args) -> str:  # HANS_TOOLSCOUT_V1
    """/nastroj — stav návrhů; /nastroj <téma> = najdi nástroj pro doménu;
    /nastroj schválit N = schval + stáhni; /nastroj zamítnout N."""
    import threading as _th
    from scripts import hans_toolscout as ts
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    a = (args or "").strip()
    low = a.lower()

    def _fmt(props) -> str:
        if not props:
            return ""
        _F = {"coexist": "vejde se vedle chatu", "on_demand": "jen samostatně",
              "too_big": "nevejde se", "unknown": "?"}
        out = []
        for p in props:
            out.append("#%d [%s] %s %s (~%s GB, %s, %s stažení)\n   %s\n   %s" % (
                p["id"], p["status"], p["tool_name"], p["size_tag"], p["est_gb"],
                _F.get(p["fit"], p["fit"]), p["pulls"],
                (p.get("rationale") or "")[:180], p["url"]))
        return "\n".join(out)

    # schválit / zamítnout
    m = re.match(r"(schv[áa]l\w*|zam[íi]t\w*|odm[íi]t\w*)\s+(\d+)", low)
    if m:
        pid = int(m.group(2))
        store = ts.ToolStore(db)
        p = store.get(pid)
        if not p:
            return "Návrh č. %d neznám, pane." % pid
        if m.group(1).startswith(("zam", "odm")):
            store.set_status(pid, "rejected")
            return "Zamítnuto, pane. %s nebudu stahovat." % p["tool_name"]
        # schválit → pull na PC. HANS_TOOLSCOUT_PULL_TAG_V1: stáhni s KONKRÉTNÍ
        # velikostí (tool_name:size_tag), jinak `ollama pull qwen2.5-coder` vezme
        # default (7b) místo navržených 14b.
        store.set_status(pid, "approved")
        _pull = p["tool_name"] + (":" + p["size_tag"]
                                  if p.get("size_tag") else "")
        res = ts.pull_model(cfg, _pull)
        if res.get("ok"):
            # HANS_TOOLSCOUT_VERIFY_V1 — stav zůstává `approved`. `pull_model`
            # spouští stahování ODPOJENĚ a vrací ok už při „started", takže
            # `installed` by tu byla domněnka, ne fakt. Povýší ho až noční
            # `verify_approved` podle `ollama list`.
            return ("Schváleno, pane. Stahuji %s na počítač — %s. Až doběhne, "
                    "ověřím si, že skutečně dorazil, a pak ho použiji pro "
                    "dílo." % (_pull, res["detail"]))
        return ("Schválil jsem %s, ale stažení jsem nespustil: %s"
                % (_pull, res.get("detail", "")))

    # stav / výpis
    if not a or low in ("stav", "status", "seznam"):
        store = ts.ToolStore(db)
        pend = store.list("pending")
        if pend:
            return "Mé návrhy nástrojů, pane:\n" + _fmt(pend) + \
                "\n\n(/nastroj schválit N nebo zamítnout N)"
        allp = store.list()
        if allp:
            return "Aktuálně nemám čekající návrh. Poslední:\n" + _fmt(allp[:3])
        return ("Zatím jsem žádný nástroj nenavrhl, pane. Napiš /nastroj <téma> "
                "a prozkoumám vhodné modely (např. /nastroj Design).")

    # /nastroj <téma> → scout na pozadí (síť + LLM)
    topic = a

    def _scout():
        try:
            r = ts.propose_tool(cfg, db, topic)
            _log.info("/nastroj %s → %s", topic, r.get("status"))
        except Exception as _e:
            _log.warning("/nastroj scout selhal: %s", _e)
    _th.Thread(target=_scout, daemon=True).start()
    return ("Prozkoumám vhodné nástroje pro „%s“, pane, chvíli to potrvá. "
            "Pak zadej /nastroj a ukážu, co jsem našel." % topic)


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


# ─── /brief — destilát studia do prompt/briefu pro dílo (HANS_BRIEF_V1) ───────
def _cmd_brief(handler, name, args) -> str:  # HANS_BRIEF_V1
    """/brief — poslední brief; /brief <téma> [coder|esej|obraz] = destiluj
    studium do nejlepšího promptu pro tvorbu díla (bez persony, grounded)."""
    import threading as _th
    from scripts import hans_brief as hb
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    a = (args or "").strip()

    # bez argumentu → poslední brief
    if not a:
        last = hb.BriefStore(db).latest()
        if not last:
            done = hb.completed_study_topics(db)
            hint = (" Dostudoval jsem: %s." % ", ".join(done)) if done else ""
            return ("Zatím jsem žádný brief nedestiloval, pane. Napiš "
                    "/brief <téma> a připravím prompt z nastudovaného.%s" % hint)
        return ("Poslední brief — %s (%s), z poznámek: %s\n\n%s" % (
            last["topic"], last["target"],
            (last.get("source_notes") or "")[:120], last["brief"]))

    # /brief <téma> [cíl]
    parts = a.rsplit(None, 1)
    target = "coder"
    topic = a
    if len(parts) == 2 and parts[1].lower() in (
            "coder", "kod", "kód", "esej", "essay", "obraz", "image"):
        topic = parts[0]
        t = parts[1].lower()
        target = ("essay" if t in ("esej", "essay")
                  else "image" if t in ("obraz", "image") else "coder")

    def _build():
        try:
            r = hb.build_brief(cfg, db, topic, target)
            _log.info("/brief %s (%s) → %s", topic, target, r.get("status"))
        except Exception as _e:
            _log.warning("/brief selhal: %s", _e)
    _th.Thread(target=_build, daemon=True).start()
    return ("Destiluji, co jsem se naučil o „%s“, do promptu pro dílo (%s), "
            "pane — chvíli to potrvá. Pak zadej /brief a ukážu ho." % (
                topic, target))


register(
    "brief",
    slash_aliases=["brief", "zadani", "zadání"],
    nl_patterns=[
        r"(p[řr]iprav|udělej).{0,15}(brief|zad[áa]n[íi]|prompt)",
    ],
    handler=_cmd_brief,
    help_text="Destiluj studium do promptu pro dílo: /brief <téma> [coder|esej|obraz]",
)


# ─── /vytvor — celá smyčka studium → brief → nástroj → artefakt (HANS_MAKER_V1) ─
def _cmd_vytvor(handler, name, args) -> str:  # HANS_MAKER_V1
    """/vytvor <téma> [coder|obraz] — Hans z nastudovaného vyrobí reálný
    artefakt (coder→HTML/CSS, obraz→SDXL). Běží na pozadí (pomalé)."""
    import threading as _th
    from scripts import hans_maker as hm
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    a = (args or "").strip()
    if not a:
        arts = hm.latest_artifacts(db, 3)
        if arts:
            out = ["Má poslední díla, pane:"]
            for x in arts:
                out.append("• %s (%s) → %s" % (x.get("topic", x.get("title")),
                           x.get("target", "?"), x.get("path", "")))
            return "\n".join(out)
        return ("Zatím jsem žádné dílo nevytvořil, pane. Napiš /vytvor <téma> "
                "a z nastudovaného vyrobím artefakt (např. /vytvor Design).")
    parts = a.rsplit(None, 1)
    target, topic = "coder", a
    if len(parts) == 2 and parts[1].lower() in ("coder", "obraz", "image"):
        topic = parts[0]
        target = "image" if parts[1].lower() in ("obraz", "image") else "coder"

    def _make():
        try:
            r = hm.make_from_study(cfg, db, topic, target)
            _log.info("/vytvor %s (%s) → %s (%s)", topic, target,
                      r.get("status"), r.get("path", r.get("reason", "")))
        except Exception as _e:
            _log.warning("/vytvor selhal: %s", _e)
    _th.Thread(target=_make, daemon=True).start()
    return ("Pouštím se do díla k „%s“ (%s) z toho, co jsem nastudoval, pane. "
            "Vyrobí to nástroj podle mého briefu — chvíli to potrvá (i pár "
            "minut). Pak zadej /vytvor a ukážu, co vzniklo." % (topic, target))


register(
    "vytvor",
    slash_aliases=["vytvor", "vytvoř", "vyrob", "artefakt"],
    nl_patterns=[
        r"vytvo[řr].{0,20}(z\s+toho|co\s+ses|nastudova|dílo|artefakt)",
    ],
    handler=_cmd_vytvor,
    help_text="Vyrob artefakt z nastudovaného: /vytvor <téma> [coder|obraz]",
)


# ─── /prohloubit — schválení/kritika návrhu prohloubení studia (DEEPEN_V2) ────
def _cmd_prohloubit(handler, name, args) -> str:  # HANS_STUDY_DEEPEN_V2
    """/prohloubit — Hansovy návrhy prohloubení; /prohloubit schválit = přijmi;
    /prohloubit <vlastní kritika> = prohluť podle tebe; /prohloubit ne = zamítni."""
    import threading as _th
    from scripts.hans_study import StudyStore
    cfg = getattr(handler, "config", {}) or {}
    db = _recall_db(handler)
    st = StudyStore(cfg, db)
    a = (args or "").strip()
    low = a.lower()

    pend = st.get_pending_deepen()
    # bez argumentu → výpis návrhů
    if not a:
        if not pend:
            return ("Teď nemám žádný návrh na prohloubení, pane. Vytvořím ho, "
                    "až dokončím dílo z nastudovaného.")
        out = ["Mé návrhy na prohloubení studia, pane:"]
        for p in pend:
            out.append("• %s (kolo %d)\n  Kritika: %s\n  Doučil bych se: %s" % (
                p["topic"], p["round"], p["critique"] or "—",
                "; ".join(p["subtopics"])))
        out.append("\n/prohloubit schválit  ·  /prohloubit <vlastní kritika>  ·  "
                   "/prohloubit ne")
        return "\n".join(out)

    if not pend:
        return "Nemám čekající návrh, pane."

    # zamítnout
    if low in ("ne", "zamítnout", "zamitnout", "odmítnout", "odmitnout", "ignoruj"):
        st.reject_deepen_proposal(pend[0]["id"])
        return "Dobře, pane. Prohloubení „%s“ nechám být." % pend[0]["topic"]

    # schválit (dle Hansova návrhu) NEBO vlastní kritika
    is_approve = low in ("schválit", "schvalit", "ano", "ok", "jo", "souhlasím",
                         "souhlasim", "schvaluji")
    user_crit = "" if is_approve else a

    def _apply():
        try:
            r = st.apply_deepen_proposal(cfg, pend[0]["id"], user_critique=user_crit)
            _log.info("/prohloubit → %s (+%s)", r.get("status"),
                      len(r.get("added", [])))
        except Exception as _e:
            _log.warning("/prohloubit selhalo: %s", _e)
    _th.Thread(target=_apply, daemon=True).start()
    if user_crit:
        return ("Beru tvou kritiku, pane, a podle ní prohloubím studium „%s“. "
                "Chvíli to potrvá; pak uvidíš nová pod-témata v /studium." %
                pend[0]["topic"])
    return ("Schváleno, pane. Prohloubím studium „%s“ podle svého návrhu — "
            "nová pod-témata uvidíš v /studium a příště z nich vytvořím lepší "
            "dílo." % pend[0]["topic"])


register(
    "prohloubit",
    slash_aliases=["prohloubit", "prohloub", "deepen"],
    nl_patterns=[],
    handler=_cmd_prohloubit,
    help_text="Návrh prohloubení studia: /prohloubit [schválit|ne|<vlastní kritika>]",
)


# ─── /vhledy — Hansovy sebe-vhledy (HANS_SELF_INSIGHT_V1) ────────────────────
def _cmd_vhledy(handler, name, args) -> str:  # HANS_SELF_INSIGHT_V1
    """/vhledy — co si Hans všiml ve vlastních datech (offline_windows,
    game_mode). Podklady = nightly LLM analýza (deepseek-r1 → hans-czech).
    /vhledy teď = spusť run hned (bez ohledu na kadenci)."""
    try:
        from scripts.hans_self_insight import latest_insights, run_analysis
    except Exception as _e:
        return "Sebe-vhledy nejsou dostupné, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary_db")
           or (cfg.get("hans_idle", {}) or {}).get("diary_db")
           or "data/hans_diary.db")
    args_s = (args or "").strip().lower()

    if args_s in ("ted", "teď", "run", "nyní", "nyni"):
        # Vyžádaný okamžitý run (mimo weekly kadenci). Blokuje ~1-2 min.
        # HANS_SELF_INSIGHT_ROTATE_MANUAL_V1 (20.7.) — dřív běžel VŽDY
        # DEFAULT_LENS (offline_game) → nové lens (social/learning/creative/
        # physical) byly ručně nedosažitelné. Teď vybere DALŠÍ v rotaci
        # (nejdéle neběžel jde první) → opakované /vhledy teď procyklí všechny.
        import threading as _th
        try:
            from scripts.hans_self_insight import _next_lens
            _lens = _next_lens(dbp, cfg)
        except Exception:
            _lens = None
        def _bg(_l=_lens):
            try:
                if _l:
                    run_analysis(dbp, cfg, lens_id=_l, force=True)
                else:
                    run_analysis(dbp, cfg, force=True)
            except Exception:
                pass
        _th.Thread(target=_bg, daemon=True).start()
        _lbl = (" (perspektiva: %s)" % _lens) if _lens else ""
        return ("Spouštím rozbor svých vlastních dat na pozadí, pane%s. "
                "Za pár minut zkuste /vhledy znovu — objeví se v seznamu."
                % _lbl)

    ins = latest_insights(dbp, limit=int(args_s) if args_s.isdigit() else 3)
    if not ins:
        return ("Zatím jsem si o svých vlastních vzorcích nic nezapsal, pane. "
                "Zkuste /vhledy teď — spustím rozbor.")
    import datetime as _dt
    lines = ["Co jsem si v poslední době všiml ve vlastních datech:"]
    for i, r in enumerate(ins, 1):
        when = _dt.datetime.fromtimestamp(r["ts"]).strftime("%d.%m. %H:%M")
        lines.append("")
        lines.append("── %s (%dd okno) ──" % (when, r["window_days"]))
        lines.append(r["insight_cs"])
    return "\n".join(lines)


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


# ─── /experiment — footgun s auto-resume (HANS_FOOTGUN_V1) ───────────────────
def _cmd_experiment(handler, name, args) -> str:
    """/experiment [minut] — spusť experiment: Hans si zapne herní mód na
    N minut (default 5), pak auto-resume. Neutrální deník záznam. Config
    gate `hans_experiment.enabled`."""
    try:
        from scripts.hans_footgun import experiment_run, status, is_running
    except Exception as _e:
        return "Experiment modul nedostupný, pane. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    args_s = (args or "").strip().lower()
    if args_s in ("stav", "status"):
        s = status()
        if s.get("active"):
            return ("Experiment běží: %d minut, zbývá %d minut."
                    % (s["duration_s"] // 60, s.get("remaining_s", 0) // 60))
        return "Aktuálně žádný experiment neběží."
    if is_running():
        s = status()
        return ("Experiment už běží — zbývá %d minut. Počkej na auto-resume."
                % (s.get("remaining_s", 0) // 60))
    # default 5 min
    dur_min = 5
    try:
        if args_s and args_s.isdigit():
            dur_min = int(args_s)
    except Exception:
        pass
    r = experiment_run(cfg, duration_s=dur_min * 60)
    if not r.get("ok"):
        return "Experiment nespuštěn: %s" % r.get("message", "?")
    return ("Spouštím experiment: zapínám si herní mód na %d minut. "
            "Auto-resume je zajištěn — i kdybych se během toho nemohl "
            "vyjádřit, systém mě vrátí." % dur_min)


register(
    "experiment",
    slash_aliases=["experiment", "footgun"],
    nl_patterns=[],
    handler=_cmd_experiment,
    help_text="Experiment: zapnu si herní mód na N minut (default 5), pak auto-resume",
)


# ─── /anomalie — týdenní algoritmické odchylky (HANS_ANOMALY_V1) ─────────────
def _cmd_anomalie(handler, name, args) -> str:
    """/anomalie — poslední detekované odchylky ve tvém chování; /anomalie teď = spusť."""
    try:
        from scripts.hans_anomaly import latest_anomaly_note, run_once
    except Exception as _e:
        return "Anomaly modul nedostupný. (%s)" % _e
    cfg = getattr(handler, "config", {}) or {}
    dbp = (cfg.get("diary_db")
           or (cfg.get("hans_idle", {}) or {}).get("diary_db")
           or "data/hans_diary.db")
    args_s = (args or "").strip().lower()
    if args_s in ("ted", "teď", "run", "nyní", "nyni"):
        import threading as _th
        def _bg():
            try:
                run_once(dbp, cfg)
            except Exception:
                pass
        _th.Thread(target=_bg, daemon=True).start()
        return ("Spouštím detekci odchylek na pozadí, pane. Za pár desítek "
                "sekund zkuste /anomalie znovu.")
    row = latest_anomaly_note(dbp)
    if not row:
        return ("Zatím jsem si v posledních týdnech ničeho neobvyklého "
                "nevšiml, pane. /anomalie teď spustí kontrolu.")
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(row["ts"]).strftime("%d.%m. %H:%M")
    return "Poslední odchylky (%s):\n\n%s" % (when, row["note"])


register(
    "anomalie",
    slash_aliases=["anomalie", "anomaly", "odchylky"],
    nl_patterns=[r"\bco.{0,10}je\s+jinak", r"\bco.{0,10}se\s+zm[ěe]nilo"],
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
_LLM_ROUTE_RISKY = frozenset({"napad", "kritika", "vhledy", "smer", "cetl",
                              "rozhovory", "anomalie"})

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


def _je_navazujici_dotaz(msg: str) -> bool:
    """Krátká věta se zájmenem, které ukazuje na předchozí repliku."""
    try:
        from scripts.hans_thread import _fold
        f = _fold(msg or "")
    except Exception:
        f = (msg or "").lower()
    if _TAZACI_ZAJMENO.search(f) or _SCHOPNOST_DOTAZ.search(f):
        return True          # HANS_THREAD_NO_LIST_V3 — bez délkového limitu
    return len((msg or "").split()) <= 9 and bool(_ZPETNE_ZAJMENO.search(f))


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
    if cid:
        _set_route_origin("llm")  # HANS_CAP_SUMMARY_V1
        _log.info("HANS_CMD_LLM_ROUTE_V1: '%.40s' → /%s", msg, cid)
        return (cid, "")
    return None


# ── HANS_KODI_PLAYCTL_V1 (5.8.) — zastavení/pauza přehrávání ─────────────────
# Nález uživatele: „ještě film zastav" — a zjistilo se, že to NEJDE. Hans umí
# film PUSTIT (agentní akce kodi_play_film), ale zastavit ne; uživatel zkusil
# „/film stop" a dostal výpis, co Hans viděl (`film` je čtecí příkaz, `stop`
# jen ignorovaný argument). Kodi to přitom umí (`stop_playback`/`pause`).
def _cmd_stop(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.kodi_client import KodiClient
        k = KodiClient(cfg)
        if not k.is_playing():
            return "Teď nic neběží, pane."
        now = ""
        try:
            np = k.get_now_playing()
            now = (np or {}).get("title") or ""
        except Exception:
            pass
        ok = k.stop_playback()
        if ok:
            return ("Zastaveno, pane%s." % ((" — „%s\"" % now) if now else ""))
        return "Zastavit se to nepodařilo, pane."
    except Exception as e:
        _log.warning("/stop selhal: %s", e)
        return "K televizi se teď nedostanu, pane."


def _cmd_pauza(handler, name, args) -> str:
    cfg = getattr(handler, "config", {}) or {}
    try:
        from scripts.kodi_client import KodiClient
        k = KodiClient(cfg)
        if not k.is_playing():
            return "Teď nic neběží, pane."
        k.toggle_pause()
        return "Hotovo, pane."
    except Exception as e:
        _log.warning("/pauza selhal: %s", e)
        return "K televizi se teď nedostanu, pane."


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
