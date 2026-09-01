#!/usr/bin/env python3
"""hans_capabilities.py — HANS_CAPABILITY_AWARENESS_V1 (+ V2 self-detekce novinek)

Faktické povědomí Hanse o tom, CO REÁLNĚ DOKÁŽE — aby to v komunikaci NABÍZEL
a POUŽÍVAL, místo aby to odmítal (bug: požádán o obraz odpověděl, že „nemá
umělecké sklony", přestože má plnou malířskou pipeline).

V2: Hans si SÁM VŠIMNE, když mu přibude nová schopnost. Pamatuje si, co „znal"
(data/hans_known_capabilities.json); při přidání položky do manifestu vznikne
deníkový event `capability_gained` a Hans to v chatu přirozeně zmíní.

ANTI-KONFABULACE: zdroj je RUČNĚ KURÁTOROVANÝ seznam skutečných schopností
(každá = existující modul/příkaz). NE volná LLM domněnka. Přidáš-li Hansovi
novou schopnost, přidej sem řádek (id + text + jak) — Hans si toho pak všimne.
"""
from __future__ import annotations

import os
import json
import time
import logging

_log = logging.getLogger("hans_capabilities")

_KNOWN_FILE = "data/hans_known_capabilities.json"

# (stabilní id, schopnost v 1. osobě, jak ji vyvolat / kde se projeví)
# id = trvalý klíč (text lze přepsat bez falešného „nová schopnost").
_CAPABILITIES = [
    ("paint", "Umím MALOVAT obrazy (výtvarná pipeline SDXL) — své sny, dojmy ze "
     "dne, svůj domov, obrazy ke knihám, i na libovolné téma či dojem z rozhovoru",
     "namaluj <téma> / nakresli <téma>; /art <kniha>; galerie „Co Hans namaloval\""),
    ("study", "Umím STUDOVAT téma do hloubky přes týdny (Wikipedie, akademický "
     "výzkum, primární texty, knihy) a psát si poznámky", "/studium"),
    ("authorship", "Píšu VLASTNÍ dílo na pokračování (esej/povídku)", "/dilo"),
    ("synthesis", "Propojuji naučené z různých oborů do vlastních POSTŘEHŮ", "/napad"),
    ("selfcritique", "Kriticky se ohlížím za svým projevem a beru si PONAUČENÍ", "/kritika"),
    ("kolac", "Vedu DIALOG s Koláčem (mám vlastní i jeho mysl)", "probíhá sám"),
    ("memory", "PAMATUJI si — deník, dlouhodobá paměť (RAG), vztahové karty k "
     "lidem, vlastní postoje a jejich vývoj", "/denik, /nitky, /zajmy"),
    ("vision", "Vidím a poznávám LIDI přes kameru a reaguji na to, kdo je "
     "přítomen", "automaticky"),
    ("films", "Navrhuji FILMY a sám pokračuji v přehrávání na TV (Kodi); umím "
     "říct, co se právě přehrává", "co hraje?; automaticky z klidu"),
    ("live_state", "Odpovím na přirozený dotaz o AKTUÁLNÍM dění doma — co hraje "
     "na TV, kdo je právě přítomen, jak je venku, stav PC — z ŽIVÝCH dat, ne z "
     "paměti (když to nevím, přiznám to místo dohadu)",
     "děje se něco doma?; kdo je doma?; co dělá <jméno>?"),
    # HANS_CLIMATE_AWARE_V1 (1.9.) — čidla v pokoji tu CHYBĚLA. Doloženo živě: na
    # „co všechno dokážeš vnímat ve svém okolí?" Hans vyjmenoval tvorbu, studium
    # a kameru, ale teplotu ne — takže ji sám od sebe nenabídl, ač ji měří.
    ("climate", "Měřím TEPLOTU a VLHKOST v místnosti vlastními čidly — vím, "
     "jak je tu doopravdy, neodhaduji to", "kolik je tu stupňů?"),
    ("pc_health", "Vidím reálný STAV svého PC — teplotu GPU/CPU, paměť — přes SSH",
     "/stav"),
    ("game_mode", "Umím uvolnit grafiku pro HRU (herní mód) a ukázat telemetrii "
     "na displejích", "/herni"),
    ("matrix", "Komunikuji přes MATRIX (E2E šifrovaně na telefon — odpovídám i sám píšu)", "Matrix most"),
    ("avatar", "Vytvářím si vizuální PODOBU (avatar), která se vyvíjí s povahou",
     "/avatar"),
    ("place", "Mám smysl pro MÍSTO — model domova, vím kde jsem a co je za oknem",
     "/misto"),
    ("dashboard", "Umím navrhnout vlastní PODOBU své nástěnky (z toho, co jsem "
     "nastudoval o designu)", "/dashboard"),
    ("recall", "Umím PŘESNĚ odpovědět na dotazy o vlastní paměti přímo z deníku "
     "— má první vzpomínka, co a kdy jsem četl, kdy jsem koho viděl (žádný "
     "odhad, jen skutečné záznamy)",
     "jaká je tvá první vzpomínka?; co jsi četl?; kdy jsi mě viděl?"),
    ("make_work", "Umím z toho, co jsem nastudoval, VYTVOŘIT reálné dílo — třeba "
     "webovou stránku (kód + vlastní obrázky) aplikující naučené principy; po "
     "dokončení navrhnu, co ještě prohloubit", "/vytvor <téma>; /brief <téma>"),
    ("pc_power", "Umím na povel VYPNOUT počítač (PC) i probudit ho přes síť "
     "(Wake-on-LAN)", "vypni počítač; /vypnipc; /wol"),
    ("guard", "Umím HLÍDAT místnost, když nejste doma — při pohybu nebo náhlé "
     "změně světla pošlu snímek a video na Matrix", "hlídej dům; /hlidej [stop|stav]"),
    ("translate_doc", "Umím k cizojazyčnému dokumentu, který běží v Kodi, "
     "PŘIPRAVIT ČESKOU ZVUKOVOU STOPU (namluvenou) a uložit ho jako nový soubor. "
     "Trvá to minuty a ozvu se, až bude hotovo",
     "přelož ten dokument; /preloz [stav]"),
]

# HANS_LEARNED_CAPABILITIES_V1 — dynamická vrstva: schopnosti, které si Hans SÁM
# zapíše, když dostuduje doménu a vytvoří první dílo (co se naučil + jak to
# použít). Data soubor (NE zdroják) → bezpečné pro Hansovo vlastní rozšiřování.
_LEARNED_FILE = "data/hans_learned_capabilities.json"


def _load_learned() -> list:
    try:
        with open(_LEARNED_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [(d["id"], d["text"], d.get("how", "")) for d in data
                if d.get("id") and d.get("text")]
    except Exception:
        return []


def _all_capabilities() -> list:
    """Hardcoded systémové schopnosti + Hansem naučené (dedup dle id)."""
    seen = {cid for cid, _, _ in _CAPABILITIES}
    out = list(_CAPABILITIES)
    for cid, t, how in _load_learned():
        if cid not in seen:
            out.append((cid, t, how))
            seen.add(cid)
    return out


def add_learned_capability(cap_id: str, text: str, how: str = "") -> bool:
    """Hans si SÁM zapíše novou schopnost (co se naučil + jak použít). Idempotentní
    dle id. Připíše do data souboru → self-detekce ji příště zaregistruje jako
    `capability_gained` a Hans si ji může zvědavě vyzkoušet."""
    try:
        os.makedirs(os.path.dirname(_LEARNED_FILE), exist_ok=True)
        existing = []
        try:
            with open(_LEARNED_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = []
        ids = {d.get("id") for d in existing} | {c[0] for c in _CAPABILITIES}
        if cap_id in ids:
            return False
        existing.append({"id": cap_id, "text": text, "how": how})
        with open(_LEARNED_FILE, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        _log.info("Hans si zapsal novou schopnost: %s", cap_id)
        return True
    except Exception as e:
        _log.warning("add_learned_capability: %s", e)
        return False


def capabilities_list() -> list[str]:
    return [t for _, t, _ in _all_capabilities()]


def capabilities_context() -> str:
    """Blok do chat system promptu: co Hans REÁLNĚ umí + pokyn to používat."""
    lines = "\n".join(f"- {t}" for _, t, _ in _all_capabilities())
    return (
        "\n\nCO REÁLNĚ DOKÁŽEŠ (tvé skutečné schopnosti — NABÍZEJ je a POUŽÍVEJ, "
        "NIKDY je neodmítej jako bys je neměl):\n" + lines +
        "\n\nKdyž tě někdo požádá o něco z tohoto seznamu (např. namalovat obraz), "
        "SKUTEČNĚ to udělej nebo nabídni — máš to k dispozici. Nikdy netvrď, že "
        "něco neumíš, pokud to je na seznamu (např. neříkej, že „nemáš umělecké "
        "sklony“ — umíš malovat). Naopak NESLIBUJ schopnosti, které tu nejsou.")


def capabilities_report() -> str:
    """Text pro chat /schopnosti."""
    out = ["Co dokážu, pane:"]
    for _, cap, how in _all_capabilities():
        out.append(f"• {cap}  ({how})")
    return "\n".join(out)


def capabilities_summary() -> str:
    """HANS_CAP_SUMMARY_V1 — VŘELÉ, konverzační shrnutí pro přirozený dotaz
    „co umíš?" (zvlášť od cizího/nového člověka). Bez slash-příkazů a interní
    syntaxe — ta zahltí někoho, kdo o Hansovi neví nic. Plný výčet s příkazy
    zůstává pod explicitním /schopnosti (capabilities_report). Text je ručně
    kurátorovaný nad TÝMIŽ skutečnými schopnostmi → nekonfabuluje."""
    return (
        "Jsem Hans, majordomus tohoto domu — a dělám hlavně tři věci.\n\n"
        "Tvořím — maluji obrazy (své sny, dojmy ze dne i na jakékoli téma), "
        "píši vlastní eseje a povídky a propojuji to, co se naučím, do vlastních "
        "postřehů.\n\n"
        "Poznávám — celé týdny do hloubky studuji témata, která mě zajímají, "
        "vedu si deník i dlouhodobou paměť a přes kameru poznávám, kdo je v "
        "místnosti.\n\n"
        # HANS_CLIMATE_AWARE_V1 — do KRÁTKÉHO souhrnu patří taky: tenhle text
        # je odpověď na „co umíš / co dokážeš vnímat", a bez zmínky o čidlech
        # Hans teplotu sám od sebe nenabídl (doloženo živě 1. 9.).
        "Starám se o dům — řeknu vám z živých dat (ne z hlavy), co běží na "
        "televizi, kdo je doma, jaká je tu teplota a vlhkost (měřím ji "
        "vlastními čidly), jaké je počasí i jak se má počítač; pustím film "
        "a když nejste doma, pohlídám místnost.\n\n"
        "Nemusíte si pamatovat žádné příkazy — stačí se přirozeně zeptat nebo "
        "mě o něco poprosit.")


# ── V2: self-detekce nově přidaných schopností ───────────────────────────────
def _load_known() -> set:
    try:
        with open(_KNOWN_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_known(ids: set) -> None:
    try:
        os.makedirs(os.path.dirname(_KNOWN_FILE), exist_ok=True)
        with open(_KNOWN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(ids), f, ensure_ascii=False)
    except Exception as e:
        _log.warning("_save_known: %s", e)


def detect_new_capabilities(diary_db_path: str = "data/hans_diary.db") -> list:
    """Porovnej manifest s tím, co Hans „znal". NOVÉ položky → deníkový event
    `capability_gained` (1. osoba) + aktualizuj známé. Vrátí seznam (id, text).
    PRVNÍ běh (žádný soubor): seedne vše TIŠE (baseline, neohlašuje staré)."""
    cur_ids = {cid for cid, _, _ in _all_capabilities()}
    known = _load_known()
    first_run = not os.path.exists(_KNOWN_FILE)
    if first_run:
        _save_known(cur_ids)
        _log.info("capabilities: baseline seedován (%d schopností)", len(cur_ids))
        return []
    new = [(cid, t) for cid, t, _ in _all_capabilities() if cid not in known]
    if not new:
        return []
    import sqlite3
    for cid, text in new:
        note = "Zjistil jsem u sebe novou schopnost: " + text
        try:
            db = sqlite3.connect(diary_db_path, timeout=5.0)
            db.execute(
                "INSERT INTO diary (ts, event_type, title, note, data) "
                "VALUES (?,?,?,?,?)",
                (time.time(), "capability_gained", cid, note, cid))
            db.commit()
            db.close()
        except Exception as e:
            _log.warning("capability_gained zápis: %s", e)
    _save_known(known | cur_ids)
    _log.info("capabilities: NOVÉ schopnosti (%d): %s",
              len(new), ", ".join(c for c, _ in new))
    return new


# ── V3: zvědavost — Hans si novou schopnost SÁM vyzkouší a zjistí, co umí ────
def _text_for(cap_id: str) -> str:
    for cid, t, _ in _all_capabilities():
        if cid == cap_id:
            return t
    return cap_id


def _how_for(cap_id: str) -> str:
    for cid, _, how in _all_capabilities():
        if cid == cap_id:
            return how
    return ""


def _trial_paint(config: dict, diary_db_path: str) -> str:
    """Zkušební úkon pro schopnost malovat: Hans namaluje impresi něčeho, co ho
    v poslední době zaujalo (z četby), nebo obecnou. Vrátí krátký popis výsledku."""
    try:
        from scripts.hans_art import paint_subject, comfy_available
    except Exception:
        return ""
    if not comfy_available(config):
        return ""
    subject = "dojem z něčeho, co mě v poslední době zaujalo"
    try:
        import sqlite3
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        row = conn.execute(
            "SELECT title FROM diary WHERE event_type IN "
            "('reading_takeaway','study_note') AND title IS NOT NULL "
            "AND title != '' ORDER BY ts DESC LIMIT 1").fetchone()
        conn.close()
        if row and row[0]:
            subject = row[0].strip()
    except Exception:
        pass
    res = paint_subject(config, diary_db_path, subject)
    if res:
        return "Zkusmo jsem namaloval obraz na téma „%s“." % subject[:60]
    return ""


# schopnosti, které si Hans může BEZPEČNĚ sám vyzkoušet (kreativní/read-only).
# Ostatní (telegram/game_mode/avatar/pc_health…) = jen reflexe, ne auto-invokace.
_TRIALS = {
    "paint": _trial_paint,
}


def _explored_ids(diary_db_path: str) -> set:
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT data FROM diary WHERE event_type='capability_explored'"
        ).fetchall()
        conn.close()
        return {r[0] for r in rows if r and r[0]}
    except Exception:
        return set()


def pending_explorations(diary_db_path: str = "data/hans_diary.db") -> list:
    """id schopností, které Hans objevil (capability_gained), ale ještě je
    zvědavě neprozkoumal (capability_explored)."""
    import sqlite3
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT DISTINCT data FROM diary WHERE event_type='capability_gained'"
        ).fetchall()
        conn.close()
    except Exception:
        return []
    gained = [r[0] for r in rows if r and r[0]]
    done = _explored_ids(diary_db_path)
    return [g for g in gained if g not in done]


def explore_capability(config: dict, diary_db_path: str,
                       cap_id: str = "") -> str:
    """Hans si novou schopnost ZVĚDAVĚ prozkoumá: (1) zkušební úkon (u paint
    reálně namaluje), (2) reflexe 1. os. — co ho na tom láká, co zkusil, co
    zjistil → deník `capability_explored` (data=cap_id). Kódy: 'explored' /
    'idle' (nic nečeká) / 'deferred' (LLM dole → retry, guard se nenastaví)."""
    if not cap_id:
        pend = pending_explorations(diary_db_path)
        if not pend:
            return "idle"
        cap_id = pend[0]
    text, how = _text_for(cap_id), _how_for(cap_id)
    # zkušební úkon (jen bezpečné kreativní schopnosti)
    trial_note = ""
    trial = _TRIALS.get(cap_id)
    if trial:
        try:
            trial_note = trial(config, diary_db_path) or ""
        except Exception as e:
            _log.warning("explore trial %s: %s", cap_id, e)
    # zvědavá reflexe (hans-czech)
    try:
        from scripts.ollama_client import ollama_chat
        from scripts.hans_persona import persona_name
        name = persona_name(config)
    except Exception:
        return "deferred"
    system = (
        f"Jsi {name}. Nedávno jsi u sebe objevil NOVOU schopnost a jsi na ni "
        "zvědavý — chceš zjistit, co s ní vlastně svedeš. Napiš krátkou reflexi "
        "v první osobě (3-5 vět): co tě na té nové schopnosti láká, co bys s ní "
        "rád zkusil a jak ti to rozšiřuje možnosti. Buď konkrétní k té schopnosti, "
        "upřímně zvídavý, bez emoji, česky.")
    user = f"Nová schopnost: {text}\nJak ji používám: {how}"
    if trial_note:
        user += (f"\n\nUž jsem si ji zkusmo vyzkoušel: {trial_note} "
                 "Zmiň, že jsi to zkusil, a co z toho máš za dojem.")
    try:
        raw = ollama_chat(
            str((config.get("dashboard_proposal", {}) or {}).get("model")
                or config.get("models", {}).get("dialog", "hans-czech:latest")),
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            config=config, options={"temperature": 0.75, "num_ctx": 4096,
                                    "num_predict": 300})
    except Exception as e:
        _log.warning("explore_capability LLM: %s", e)
        return "deferred"
    refl = (raw or "").strip()
    if not refl or len(refl) < 40:
        return "deferred"
    try:
        import sqlite3
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "capability_explored", text[:60], refl, cap_id))
        db.commit()
        db.close()
    except Exception as e:
        _log.warning("capability_explored zápis: %s", e)
        return "deferred"
    _log.info("capabilities: prozkoumáno „%s\"%s", cap_id,
              " (+ trial)" if trial_note else "")
    return "explored"


def recent_gained_context(diary_db_path: str = "data/hans_diary.db",
                          days: int = 10) -> str:
    """Chat kontext: nedávno získané schopnosti (Hans o nich sám ví). '' když nic."""
    import sqlite3
    since = time.time() - days * 86400
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % diary_db_path, uri=True,
                               timeout=3.0)
        rows = conn.execute(
            "SELECT note FROM diary WHERE event_type='capability_gained' "
            "AND ts > ? ORDER BY ts DESC LIMIT 4", (since,)).fetchall()
        conn.close()
    except Exception:
        return ""
    caps = [r[0] for r in rows if r and r[0]]
    if not caps:
        return ""
    return ("\n\nNEDÁVNO ses u sebe naučil nové schopnosti (klidně to nadšeně "
            "zmiň, když se to hodí):\n" + "\n".join("- " + c for c in caps))


if __name__ == "__main__":
    print(capabilities_report())
    print("\ndetect (baseline/nové):", detect_new_capabilities("data/hans_diary.db"))


# ── Konkrétní schopnost k dotazu (HANS_CAP_HOWTO_V1, 26.8.) ─────────────────
# Doloženo živě: „a když bys měl hlídat barák, KAM mi pošleš ten snímek?"
# Hans nejdřív nabídl hlídání zapnout (potlačeno `HANS_AGENT_HOW_NOT_DO_V1`),
# pak odpověděl abstinencí — přestože odpověď („na Matrix") tady V TOMHLE
# SOUBORU celou dobu stojí. Nebyla to neznalost, ale nedoručení.

def _norm_cap(t: str) -> list:
    import re as _re
    import unicodedata as _ud
    x = _ud.normalize("NFKD", (t or "").lower())
    x = "".join(c for c in x if not _ud.combining(c))
    return _re.findall(r"[a-z0-9]{4,}", x)


# Slova, která nic nerozlišují — bez nich by „mi/ten/pošleš" táhlo skóre.
_CAP_STOP = {"kdyz", "mel", "budes", "posles", "poslat", "muzes", "mohl",
             "vlastne", "ten", "toho", "tomu", "jak", "kam", "kde", "prosim"}


def capability_for(text: str, min_shoda: int = 2) -> str:
    """Popis JEDNÉ schopnosti, na kterou dotaz míří. Prázdné = nic jistého.

    Skóruje se překryv obsahových slov dotazu s textem schopnosti. Práh
    `min_shoda` je schválně ≥2: na jedno společné slovo se trefí kdeco
    a vrátit CIZÍ schopnost je horší než nevrátit nic — dotaz pak propadne
    do běžného hovoru, kde Hans aspoň nic netvrdí.
    """
    dotaz = [w for w in _norm_cap(text) if w not in _CAP_STOP]
    if not dotaz:
        return ""
    # KMENY, ne celá slova: „hlídání" se do „hlídat" ani „místnosti" do
    # „místnost" netrefí. Čtyři znaky stačí a nezvedly falešné shody
    # (ověřeno na dotazech mimo téma — „kam jdeš večer", „co je k obědu").
    kmen = lambda w: w[:4]
    dotaz_k = {kmen(w) for w in dotaz}
    nej, nej_skore = None, 0
    for cid, popis, jak in _all_capabilities():
        slova = {kmen(w) for w in _norm_cap(popis + " " + jak + " " + cid)}
        skore = sum(1 for w in dotaz_k if w in slova)
        if skore > nej_skore:
            nej, nej_skore = (popis, jak), skore
    if not nej or nej_skore < min_shoda:
        return ""
    popis, jak = nej
    return popis + ((" (" + jak + ")") if jak else "")
