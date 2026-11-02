"""scripts/hans_narrative.py

AUTOBIOGRAPHICAL_NARRATIVE_V1 — krok 3 autobiografické vrstvy: narativní konsolidace.

Periodicky (týdně) destiluje importance-vážený deník + postoje + koníčky + lidi
+ cíle + minulou kapitolu → KRÁTKOU reflektivní „kapitolu životního příběhu"
(1. osoba, Hans o sobě). Dává Hansovi narrative identity (McAdams) přes týdny —
ne jen sémantická fakta (stances), ale PŘÍBĚH: co ho formovalo, kým se stává.

Báze: analytický base model (anti-konfabulace, [[hans-czech-is-openeurollm-finetune]]),
striktně grounded v materiálu. Kapitola → diary event 'narrative_chapter'
(čte ji příští konsolidace pro kontinuitu; volitelně Severka/persona).
Viz [[autobiographical-layer-roadmap]].
"""
import sqlite3
import logging
from datetime import datetime, timedelta

_log = logging.getLogger("hans_narrative")

_EXCLUDE = ("distillation_finding", "tendency_snapshot", "kodi_playing",
            "person_seen", "teddy_arrived", "teddy_gone", "weather",
            "night_summary", "wol", "narrative_chapter",
            "severka_zamitnuto")   # SEVERKA_ZAMITNUTO_V1 — Severka čte kapitolu


def _ro(db_path):
    return sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=4.0)


def gather(db_path: str, period_days: int = 7, min_importance: int = 6) -> dict:
    """Posbírá materiál za období (read-only)."""
    out = {"episodes": [], "stances": [], "hobbies": [], "people": [],
           "goals": [], "prev": ""}
    since = (datetime.now() - timedelta(days=period_days)).timestamp()
    try:
        con = _ro(db_path)
    except Exception as e:
        _log.debug("narrative gather connect: %s", e)
        return out
    try:
        ph = ",".join("?" * len(_EXCLUDE))
        # pivotní epizody období (importance-vážené)
        for ts, imp, txt in con.execute(
                "SELECT ts, importance, COALESCE(data,note,title,'') FROM diary "
                "WHERE ts>=? AND importance>=? AND event_type NOT IN (%s) "
                "AND COALESCE(data,note,title,'')!='' "
                "ORDER BY importance DESC, ts DESC LIMIT 16" % ph,
                (since, int(min_importance), *_EXCLUDE)).fetchall():
            t = " ".join(str(txt).split())
            if not t.startswith("{"):
                out["episodes"].append((int(imp), t[:200]))
        # postoje (aktivní, nejsilnější)
        try:
            for claim, conf, ec in con.execute(
                    "SELECT claim, confidence, evidence_count FROM stances "
                    "WHERE status='active' ORDER BY confidence DESC LIMIT 8").fetchall():
                out["stances"].append((claim, conf, ec))
        except Exception:
            pass
        # koníčky
        try:
            for name, ec in con.execute(
                    "SELECT name, evidence_count FROM hobbies "
                    "ORDER BY sila DESC, evidence_count DESC LIMIT 8").fetchall():
                out["hobbies"].append((name, ec))
        except Exception:
            pass
        # lidé (charakterizace)
        try:
            for nm, role, ch in con.execute(
                    "SELECT display_name, role, characterization FROM relationships "
                    "WHERE deactivated_at IS NULL ORDER BY sightings_count DESC LIMIT 5").fetchall():
                if ch:
                    out["people"].append((nm, role or "", " ".join(str(ch).split())[:220]))
        except Exception:
            pass
        # cíle
        try:
            for topic, status in con.execute(
                    "SELECT topic, status FROM hans_goals ORDER BY opened_at DESC LIMIT 4").fetchall():
                out["goals"].append((topic, status))
        except Exception:
            pass
        # minulá kapitola (kontinuita)
        try:
            r = con.execute(
                "SELECT COALESCE(data,note,'') FROM diary WHERE event_type='narrative_chapter' "
                "ORDER BY ts DESC LIMIT 1").fetchone()
            if r and r[0]:
                out["prev"] = " ".join(str(r[0]).split())[:600]
        except Exception:
            pass
    finally:
        con.close()
    return out


# NARRATIVE_KEY_SCENES_V1 (30. 10.) — klíčové scény (McAdams: zlomy, vrcholy,
# nezdary) z UDÁLOSTÍ, které se skutečně staly, ne z Hansovy prózy o nich
# (top-16 epizod bylo skoro jen „Velmi mě zasáhlo…“ k četbě a o lidech nic).
# Věty skládá kód; jen čte (read-only). Pořadí = přednost odezvy okolí.
def _jmeno(o: str, config) -> tuple:
    try:
        from scripts.cz_names import display_name, person_gender
        return display_name(o, config) or o, ("a" if person_gender(o, config) == "žena" else "")
    except Exception:
        return o, ""


def klicove_sceny(db_path: str, period_days: int = 7, config: dict = None,
                  max_scen: int = 8) -> list:
    """[věta] skutečných událostí období (nejvýš `max_scen`). Nikdy nehází."""
    import json as _j
    since = (datetime.now() - timedelta(days=period_days)).timestamp()
    cfg = config or {}
    test = {str(x).lower() for x in (cfg.get("test_persons") or [])}
    out = []
    try:
        con = _ro(db_path)
    except Exception:
        return []
    def rows(et, cols="title, coalesce(note,''), coalesce(data,'')"):
        try:
            return con.execute("SELECT %s FROM diary WHERE event_type=? AND ts>=? "
                               "ORDER BY ts" % cols, (et, since)).fetchall()
        except Exception:
            return []
    try:
        # 1) ohlas na obraz
        for ti, _n, d in rows("art_feedback"):
            try:
                dd = _j.loads(d or "{}")
            except Exception:
                dd = {}
            o = str(dd.get("person") or "")
            if not o or o.lower() in test:
                continue
            jm, a = _jmeno(o, cfg)
            r = int(dd.get("rating") or 0)
            kom = " a napsal%s: „%s“" % (a, dd["comment"][:80]) if dd.get("comment") else ""
            out.append("%s ohodnotil%s můj obraz „%s“ %s%s." % (
                jm, a, ti, "kladně" if r > 0 else "záporně" if r < 0 else "", kom))
        # 2) s kým jsem mluvil
        from collections import Counter
        c = Counter(str(t).lower() for t, _n, _d in rows("human_chat")
                    if t and str(t).lower() not in test)
        for o, n in c.most_common(3):
            jm, _a = _jmeno(o, cfg)
            out.append("Rozhovory se mnou: %s — %d %s za týden." % (
                jm, n, "zpráva" if n == 1 else "zprávy" if n < 5 else "zpráv"))
        # 3) filmy, které jsem navrhl, a jak je lidé přijali
        try:
            from scripts.hans_film_volba import historie
            _fl = []
            for o in sorted({str(t) for t, _n, _d in rows("film_suggestion") if t}):
                if o.lower() in test:
                    continue
                h = [x for x in historie(db_path, o, n=200, dni=period_days)]
                if h:
                    jm, _a = _jmeno(o, cfg)
                    pri = [x["titul"] for x in h if x["prijato"]]
                    _fl.append("%s %d× (přijato %s)" % (
                        jm, len(h), ("%d: %s" % (len(pri), ", ".join(pri[:3]))) if pri else "0"))
            if _fl:
                out.append("Navrhoval jsem filmy: %s." % "; ".join(_fl))
        except Exception as e:
            _log.debug("klicove_sceny filmy: %s", e)
        # 4) zastavený návrh Severky
        for ti, n, _d in rows("severka_zamitnuto"):
            role = ti.split(":", 1)[-1].strip()
            proc = ""
            if "Proč zastaveno:" in n:
                proc = n.split("Proč zastaveno:", 1)[1].split("Navržený", 1)[0].strip()
            # jednoznačně: měření 30. 10. — model četl „zastaven — jen jiné
            # pojmenování role X“ jako „role se změnila na X“
            out.append("Severka navrhla změnit mou roli na „%s“, ale návrh nebyl přijat%s; "
                       "moje role zůstává stejná." % (
                role, (" (" + proc[:140] + ")") if proc else ""))
        # 5) omyly zjištěné při noční kontrole + opravy
        ll = [n for _t, n, _d in rows("lesson_learned") if "nočním ověření" in n]
        if ll:
            import re as _re
            pr = [m.group(1) for m in (_re.search(r"hesl[ao] „([^“\"]+)", x) for x in ll) if m]
            out.append("Při nočních kontrolách jsem u %d svých odpovědí zjistil, že jsem se spletl%s." % (
                len(ll), (" (např. heslo „%s“ nesedělo na otázku)" % pr[0]) if pr else ""))
        kor = [t for t, _n, _d in rows("correction")]
        if kor:
            out.append("Opravil jsem své dřívější zápisy (%d): %s." % (
                len(kor), "; ".join(x.replace("Oprava ", "")[:60] for x in kor[:2])))
        # 6) Koláčovy zkoušky
        vv = Counter()
        for _t, _n, d in rows("kolac_exam"):
            try:
                vv[_j.loads(d or "{}").get("verdikt") or "?"] += 1
            except Exception:
                pass
        if sum(vv.values()) >= 3:
            ok = vv.get("ok", 0)
            spatne = sum(n for k, n in vv.items() if k not in ("ok", "neposouzeno", "?"))
            out.append("Koláč mě %d× zkoušel z toho, co jsem četl: obstál jsem %d×, neobstál %d×." % (
                sum(vv.values()), ok, spatne))
        # 7) dokončené studium a díla
        for ti, _n, _d in rows("study_mastery"):
            out.append("Dostudoval jsem téma „%s“." % ti.split(":", 1)[-1].strip())
        for ti, _n, _d in rows("writing_completed"):
            out.append("Dopsal jsem dílo „%s“." % ti)
        # 8) uzavřené případy (nové schopnosti ne: věty o sobě, useknuté)
        cc = {t for t, _n, _d in rows("case_closed") if t}
        if cc:
            out.append("S Koláčem jsme uzavřeli %d případ%s (%s)." % (
                len(cc), "" if len(cc) == 1 else "y" if len(cc) < 5 else "ů", ", ".join(sorted(cc)[:2])))
    except Exception as e:
        _log.debug("klicove_sceny: %s", e)
    finally:
        con.close()
    return out[:max_scen]


def latest_chapter(db_path: str) -> str:
    """Nejnovější narativní kapitola (text) nebo '' — pro Severku/personu."""
    try:
        con = _ro(db_path)
        r = con.execute("SELECT COALESCE(data,note,'') FROM diary "
                        "WHERE event_type='narrative_chapter' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        return " ".join(str(r[0]).split()) if r and r[0] else ""
    except Exception as e:
        _log.debug("latest_chapter failed: %s", e)
        return ""


def _build_prompt(m: dict, name: str) -> str:
    def block(items, fmt):
        return "\n".join(fmt(x) for x in items) or "(žádné)"
    ep = block(m["episodes"], lambda x: f"- [{x[0]}/10] {x[1]}")
    st = block(m["stances"], lambda x: f"- {x[0]} (jistota {x[1]:.2f})")
    ho = block(m["hobbies"], lambda x: f"- {x[0]} (×{x[1]})")
    pe = block(m["people"], lambda x: f"- {x[0]} ({x[1]}): {x[2]}")
    go = block(m["goals"], lambda x: f"- {x[0]} [{x[1]}]")
    # NARRATIVE_NO_PREV_V1 (30. 10.) — minulá kapitola s „naváž na ni“ byla
    # rohatka: 12., 19. i 26. 10. začínalo „Kopeckého smrt…“ (zpráva z 10. 10.),
    # dřív 6.→13. 7., 21. 9.→5. 10. tytéž úvody. Měření na podkladu 30. 10.
    # (data/mereni/kapitola_30_10/, 2 série): A dnešní 2/2 Kopecký; B „neopakuj“
    # stále opisuje celé věty; C bez minulé kapitoly 0 převzatých, nejvíc z týdne.
    # Zvoleno C (uživatel 30. 10.). `m["prev"]` zůstává pro jiné čtenáře.
    # NARRATIVE_KEY_SCENES_V1 — skutečné události období jako první blok
    sc = "\n".join("- " + s for s in (m.get("scenes") or [])) or "(žádné)"
    return (f"KLÍČOVÉ UDÁLOSTI období (skutečně se staly):\n{sc}\n\n"
            f"PIVOTNÍ EPIZODY období:\n{ep}\n\n"
            f"POSTOJE:\n{st}\n\nKONÍČKY:\n{ho}\n\nLIDÉ:\n{pe}\n\nCÍLE:\n{go}")


# NARRATIVE_SHAPE_GUARD_V1 (17. 9.) — kapitola ma byt KRATKE vypraveni
# v 1. osobe. Kdyz se prompt utne, model misto toho vrati rozbor vstupu ve
# 3. osobe ("Tento text je fascinujici mix recenzi...", "Na zaklade
# poskytnutych dat jsem sestavil analyzu vasi osobnosti"). Dosud se takovy
# artefakt ulozil TISE jako uspech a cetla ho Severka i kazdy chatovy prompt.
_1OS_VZOR = (r"\b(jsem|jsem si|mne|mnou|m[ée]ho|m[ée]m|m[ýy]ch|m[ůu]j|sv[ůu]j"
             r"|sv[ée]|sv[ée]m|zji[šs]tuji|zji[šs][ťt]uji|zaznamen[áa]v[áa]m"
             r"|c[íi]t[íi]m|vn[íi]m[áa]m|uv[ěe]domuji|za[čc][íi]n[áa]m"
             r"|poz[oó]ruji|pozoruji)\b")
# Tvary, ktere se v Hansove kapitole objevit NEMAJI: rozbor, oslovovani
# ctenare (vasi/vasem) a odrazky (system prompt je vyslovne zakazuje).
_ROZBOR_VZOR = (r"(Tento text|Zde je (rozbor|analýza|shrnut)|Na z[áa]klad[ěe] "
                r"poskytnut|charakteristik|va[šs][íi] osobnost|va[šs]em|va[šs]e "
                r"osobnost|\*\*|^\s*\*\s|Analýza Textu|Interpretace a)")


def _proc_neni_kapitola(text: str, config: dict) -> str:
    """NARRATIVE_SHAPE_GUARD_V1 — vrati DUVOD zamitnuti, nebo '' kdyz je text OK.

    Zdrave kapitoly 15. 6.-24. 8. mely 700-999 znaku a zacinaly 1. osobou
    ("Zjistuji, ze..."). Poskozene od 31. 8. mely 2381-3267 znaku rozboru.
    Prah je proto s rezervou nad zdravym maximem.
    """
    import re
    cfg = (config.get("narrative", {}) or {})
    max_zn = int(cfg.get("max_chars", 1400))
    min_1os = int(cfg.get("min_first_person", 3))
    if len(text) > max_zn:
        return "kapitola ma %d znaku, strop je %d (rozbor misto vypraveni?)" % (
            len(text), max_zn)
    m = re.search(_ROZBOR_VZOR, text, re.I | re.M)
    if m:
        return "text ma tvar ROZBORU, ne vypraveni (nasel jsem %r)" % (
            m.group(0)[:40],)
    poc = len(re.findall(_1OS_VZOR, text, re.I))
    if poc < min_1os:
        return "jen %d znaku 1. osoby (min %d) — nevypada to, ze mluvi o sobe" % (
            poc, min_1os)
    return ""


def consolidate(config: dict, db_path: str, model: str = None,
                period_days: int = 7, timeout: int = 300) -> str:
    """Vytvoří a uloží narativní kapitolu. Vrací text (nebo '' při selhání).
    Deferral-safe — když LLM dole, nic nezapíše (zkusí se příště)."""
    from scripts.hans_persona import persona_name as _pn
    name = _pn(config)
    m = gather(db_path, period_days)
    m["scenes"] = klicove_sceny(db_path, period_days, config)   # NARRATIVE_KEY_SCENES_V1
    if not m["episodes"] and not m["stances"] and not m["scenes"]:
        _log.info("narrative: málo materiálu, skip")
        return ""
    if not model:
        er = config.get("evening_reflection", {}) or {}
        model = config.get("narrative", {}).get(
            "model", er.get("model", "jobautomation/OpenEuroLLM-Czech:latest"))
    system = (
        f"Jsi {name}ova autobiografická paměť. Z materiálu napiš KRÁTKOU reflektivní "
        f"kapitolu {name}ova životního příběhu v 1. OSOBĚ ({name} mluví o sobě). "
        "Shrň, co ho v tomto období formovalo, k čemu inklinuje, kým se POSTUPNĚ stává "
        "a jak se vyvíjejí jeho vztahy. "   # NARRATIVE_NO_PREV_V1 — bez „naváž na minulou kapitolu“
        # NARRATIVE_KEY_SCENES_V1 — měření 30. 10.: s událostmi 6–7 z 8 v kapitole, bez nich 0–1
        "Kapitolu postav hlavně na KLÍČOVÝCH UDÁLOSTECH — co se povedlo, co ne, jak "
        "reagovali lidé; úvahy z četby jen doplňují. "
        "STRIKTNĚ vycházej z materiálu — nic si nevymýšlej. Důstojný, střídmý, "
        "introspektivní tón. 4-7 vět, souvislý odstavec, žádné odrážky.")
    try:
        # HANS_BASE_MODEL_BATCH_V1 — consolidate běží na base OpenEuroLLM (8GB)
        # vedle rezidentního hans-czech (8GB > 16GB VRAM). Aktivní VRAM handoff,
        # jinak thrashing → prázdné/uříznuté odpovědi → „prázdná kapitola" loop
        # (doloženo 3.8.: 149× za noc + 300s timeouty).
        from scripts.ollama_client import ollama_generate, base_model_batch
        with base_model_batch(config, pause_s=max(timeout + 120, 600),
                              label="narrative"):  # HANS_BASE_SLOT_V1
            text = ollama_generate(
                model=model, prompt=_build_prompt(m, name), system=system,
                config=config, timeout=timeout, keep_alive=0,
                # NARRATIVE_NUM_CTX_V1 (17. 9.) — BEZ tohohle platilo
                # vychozich 2048 tokenu, zatimco prompt ma ~2143 (6431 zn / 3).
                # Utne se ZACATEK, tedy MINULA KAPITOLA a ramec zadani, a model
                # vstup komentuje misto vypraveni. Tataz vada a tataz oprava
                # jako IMPORTANCE_NUM_CTX_V1 / threads / book_mentions
                # (29. 8.) — narativ tehdejsi audit minul.
                # Spoustecem byl backfill duleitosti tehoz dne: gather() bere
                # epizody s importance >= 6, takze blok epizod se z temer
                # prazdneho naplnil na svuj strop 3385 zn.
                options={"temperature": 0.4,
                         "num_ctx": int((config.get("narrative", {}) or {})
                                        .get("num_ctx", 8192))})
    except Exception as e:
        _log.warning("narrative: LLM selhal (zkusím příště): %s", e)
        return ""
    text = (text or "").strip()
    if not text or len(text) < 40:
        _log.warning("narrative: prázdná kapitola, skip")
        return ""
    # NARRATIVE_SHAPE_GUARD_V1 — radsi zadna kapitola nez rozbor ulozeny jako
    # vzpominka. Prazdny navrat je deferral-safe: volajici nenastavi tydenni
    # guard (hans_routine) a zkusi to znovu, throttle drzi retry na 2x/hod.
    _spatne = _proc_neni_kapitola(text, config)
    if _spatne:
        _log.warning("narrative: NEZAPISUJI kapitolu — %s | zacatek: %.120s",
                     _spatne, " ".join(text.split()))
        return ""
    # ulož do deníku
    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        title = "Kapitola: %s" % datetime.now().strftime("%-d.%-m.%Y")
        con.execute("INSERT INTO diary (ts, event_type, title, data) VALUES (?,?,?,?)",
                    (datetime.now().timestamp(), "narrative_chapter", title, text))
        con.commit()
        con.close()
        _log.info("narrative: kapitola uložena (%d znaků)", len(text))
    except Exception as e:
        _log.warning("narrative: zápis selhal: %s", e)
    return text


if __name__ == "__main__":  # smoke: python3 scripts/hans_narrative.py [--dry]
    import sys, json
    try:                                   # HANS_MAIN_CONFIG_IO_V1
        from scripts.config_io import load as _cfg_load
    except ImportError:                    # spusteno jako skript, root chybi
        import sys as _s, os as _o
        _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
        from scripts.config_io import load as _cfg_load
    cfg = _cfg_load()
    if "--dry" in sys.argv:
        mm = gather("data/hans_diary.db")
        print(_build_prompt(mm, "Hans")[:1500])
    else:
        print(consolidate(cfg, "data/hans_diary.db"))
