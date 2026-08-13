"""
PC_SHUTDOWN_DEFER_V1 — „vypni PC, ale až dodělá, co má rozpracované."

DOLOŽENÝ PŘÍPAD (11.8. 23:06–23:16): uživatel napsal „vypni pc", Hans se zeptal,
protože něco počítal, a uživatel odpověděl **„ne, můžeš vypnout až doděláš co je
rozpracováno"**. Hans to nepochopil — věta začínala „ne" a měla 8 slov, takže
padla do pravidla `HANS_AGENT_CONFIRM_PAYLOAD_V1` („vypadá jako potvrzení, ale
nese vlastní obsah → nový požadavek") a propadla do chatu, kde na ni odpověděl
anti-konfabulační abstinencí. Uživatel pak musel po deseti minutách napsat
„vypni pc" znovu.

ŘEŠENÍ: na potvrzovací otázku smí přijít TŘETÍ odpověď — ODLOŽIT. Uloží se
záměr a Hans počítač vypne sám, jakmile přestane být zaneprázdněný.

ROZHODNUTÍ, KTERÁ STOJÍ ZA VYSVĚTLENÍ:
  • **Stav v SOUBORU, ne v paměti.** Restart Hanse (a ten se děje často) by
    záměr jinak zahodil a počítač by zůstal běžet celou noc. Týž důvod, proč je
    v souboru stav hlídacího režimu.
  • **Musí být klid OPAKOVANĚ**, ne jednou. Změřeno: uprostřed rozhovoru
    spadlo vytížení GPU na 0 % a příkon na 16 W — to je MEZERA MEZI ÚLOHAMI,
    ne konec práce. Jeden vzorek by stroj vypnul v půlce odpovědi.
  • **Strop čekání.** Když se klid nedostaví do `max_wait_h`, počítač se
    NEVYPNE a Hans to řekne. Vypnout stroj po čtyřech hodinách bez varování je
    horší než nevypnout ho vůbec — uživatel už u něj mezitím může sedět.
  • Zaneprázdněnost se pozná ZÁTĚŽÍ a herním módem — ne teplotou (zpožděná,
    závisí na okolí) a ne VRAM (trvale vysoká kvůli rezidentním modelům).
    Detaily a naměřená čísla u `pc_busy`. Předpoklad sdílí i potvrzovací
    hláška `_shutdown_confirm_text`, ať se ty dva pohledy nerozejdou.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("pc_defer")

STATE = "data/.pc_shutdown_pending"


# ── stav ─────────────────────────────────────────────────────────────────
def request(person: str = "", note: str = "") -> None:
    """Zapiš záměr vypnout PC, až dojede rozpracované."""
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "person": person or "",
                       "note": note or "", "idle_hits": 0}, f)
        log.info("odložené vypnutí PC uloženo (žádal %s)", person or "?")
    except Exception as e:
        log.warning("nelze uložit odložené vypnutí: %s", e)


def pending() -> dict | None:
    try:
        if not os.path.isfile(STATE):
            return None
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cancel() -> bool:
    try:
        if os.path.isfile(STATE):
            os.unlink(STATE)
            log.info("odložené vypnutí PC zrušeno")
            return True
    except Exception as e:
        log.warning("nelze zrušit odložené vypnutí: %s", e)
    return False


def _bump(state: dict, hits: int) -> None:
    try:
        state["idle_hits"] = hits
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception:
        pass


# ── je počítač zaneprázdněný? ────────────────────────────────────────────
def pc_busy(config: dict) -> tuple[bool, str]:
    """(zaneprázdněn?, čím). Jediná pravda o „PC pracuje" — sdílí ji i
    potvrzovací hláška před vypnutím.

    ⚠️ MĚŘÍ SE ZÁTĚŽ, NE TEPLOTA (změněno 12.8. na námitku uživatele:
    *„teplota není moc přesná, v zimě se nemusíme dostat přes práh vůbec"*).
    Měření při skutečné inferenci to potvrdilo tvrději, než čekal:
        klid       GPU   0 %   příkon   6 W   load/jádro 0.005   CPU 36 °C
        inference  GPU  96 %   příkon 219 W   load/jádro 0.018   CPU 52 °C
    Teplota by práci MINULA (52 < práh 68). A `loadavg` sám taky — při plné
    GPU práci byl 0.59 na 32 jádrech, tedy „skoro klid". Rozhoduje proto
    vytížení a příkon GPU, které skáčou 0→99 % a 6→219 W; zátěž CPU je
    doplňkem pro práci, která na GPU nejde (noční analytika s `num_gpu:0`).

    ⚠️ Nevím ≠ klid: když se stav nepodaří zjistit, hlásí se ZANEPRÁZDNĚNO.
    Vypnout stroj kvůli mlčícímu čidlu je horší chyba než počkat.
    """
    # PC_BUSY_SOURCE_AWARE_V1 — rozliš Hansovo MLUVENÍ od REÁLNÉ práce.
    c = (config.get("pc_defer", {}) or {})
    gpu_pct = float(c.get("busy_gpu_pct", 15.0))
    gpu_w = float(c.get("busy_gpu_watt", 40.0))
    cpu_load = float(c.get("busy_cpu_load", 0.25))
    verify_s = float(c.get("gpu_verify_s", 2.0))

    def _sample():
        """Jeden vzorek. (gpu_busy_pct, gpu_power_w, load_per_core) nebo
        None místo dvojice, když PC neodpovídá / nevidíme na vytížení."""
        from scripts import pc_remote
        t = pc_remote.telemetry(config) or {}
        if not t:
            return None, None, None, "počítač neodpovídá na dotaz po stavu"
        g, w, l = t.get("gpu_busy_pct"), t.get("gpu_power_w"), t.get("load_per_core")
        if g is None and w is None and l is None:
            return None, None, None, "nevidím na vytížení (raději počkám)"
        return g, w, l, None

    # 1) HRA
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return True, "běží hra (herní mód)"
    except Exception:
        pass

    # 2) REÁLNÁ Hansova práce (render/analytika) — ta si SAMA uspí warmup
    #    (avatar_render, evening_reflection…). Běžný chat to NEDĚLÁ → Hansovo
    #    mluvení se sem nechytí. Jádro rozlišení „Hans vs něco jiného".
    try:
        from scripts.ollama_client import warmup_paused
        if warmup_paused():
            return True, "ještě dokončuji rozpracované (kresba nebo analýza)"
    except Exception:
        pass

    # 3) TELEMETRIE PC (nevím ≠ klid → raději počkej)
    try:
        g, w, l, err = _sample()
    except Exception as e:
        return True, "stav počítače se nepodařilo zjistit (%s)" % str(e)[:40]
    if err:
        return True, err

    # 4) CPU ZÁTĚŽ = analytika (i lehká, num_gpu:0) = reálná práce. Mluvení
    #    procesor nezatěžuje → blokuj hned, bez ověřování.
    if l is not None and l >= cpu_load:
        return True, "běží analytika (procesor %.0f %%)" % (l * 100)

    # 5) GPU zátěž BEZ warmup_paused = Hansovo MLUVENÍ (krátká špička hans-czech)
    #    NEBO cizí GPU práce (trvá). Rozliš PERZISTENCÍ — krátká věta zmizí do ~2 s.
    gpu_hot = (g is not None and g >= gpu_pct) or (w is not None and w >= gpu_w)
    if gpu_hot:
        import time as _t
        _t.sleep(verify_s)
        try:
            g2, w2, _l2, _e2 = _sample()
        except Exception:
            g2, w2 = g, w
        still = ((g2 is not None and g2 >= gpu_pct)
                 or (w2 is not None and w2 >= gpu_w))
        if still:
            det = []
            if g2 is not None and g2 >= gpu_pct:
                det.append("GPU %.0f %%" % g2)
            if w2 is not None and w2 >= gpu_w:
                det.append("příkon %.0f W" % w2)
            return True, "něco zatěžuje grafiku (%s)" % ", ".join(det)
        # krátká špička → to jsem jen mluvil → neblokuje
    return False, "klid"


# ── tik ──────────────────────────────────────────────────────────────────
def tick(handler) -> str | None:
    """Zavolat z idle smyčky. Vrací text ke sdělení, nebo None.

    Vypne PC, jakmile je NÁSOBNĚ po sobě klid. Když se klid nedostaví do
    stropu, NEVYPÍNÁ a řekne to.
    """
    st = pending()
    if not st:
        return None
    cfg = getattr(handler, "config", {}) or {}
    c = (cfg.get("pc_defer", {}) or {})
    need = int(c.get("idle_checks", 3))
    max_wait_h = float(c.get("max_wait_h", 4.0))

    if time.time() - float(st.get("ts", 0)) > max_wait_h * 3600.0:
        cancel()
        return ("Počítač jsem nevypnul, pane — %.0f hodiny se nepřestal "
                "něčím zaměstnávat a nechtěl jsem ho vypnout jen tak. "
                "Řekněte, až mám znovu." % max_wait_h)

    busy, why = pc_busy(cfg)
    if busy:
        if int(st.get("idle_hits", 0)):
            _bump(st, 0)          # klid se přerušil → počítej znovu od nuly
        return None

    hits = int(st.get("idle_hits", 0)) + 1
    if hits < need:
        _bump(st, hits)
        return None

    cancel()
    try:
        from scripts.chat_commands import _cmd_vypnipc
        _cmd_vypnipc(handler, st.get("person") or None, "hned")
    except Exception as e:
        log.warning("odložené vypnutí selhalo: %s", e)
        return "Chtěl jsem počítač vypnout, ale nepovedlo se to, pane."
    log.info("odložené vypnutí PC provedeno")
    return "Počítač dokončil, co měl rozpracované — vypnul jsem ho, pane."
