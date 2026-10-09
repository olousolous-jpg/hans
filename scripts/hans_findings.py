"""
HANS_INSTANT_LOOKUP_V1 (4.8.2026) — okamžité dohledání s UZAVŘENOU smyčkou.

Problém: na dotaz o tématu, které Hans nemá v paměti, dosud odpověděl poctivé
„nemám záznam, můžu si to nastudovat" (`hans_recall.knowledge_check_bypass`) a
uživatel čekal na noční studium. Instant read byl 19.7. ZAMÍTNUT, protože
Wikipedia resolution má false positives („Zootropium" → „Zootropic") a zápis
rovnou do paměti = tichá kontaminace.

Tenhle modul ten spor řeší tím, že ROZPOJÍ „odpovědět" a „zapamatovat si":

  1. HNED   — dohledá a odpoví PROVIZORNĚ („podle toho, co jsem právě našel").
              Do deníku/entit/RAG se NEZAPÍŠE NIC. Nález (dotaz + raw_text +
              provizorní odpověď) jde do čekárny `unverified_findings`.
  2. V NOCI — `verify_pending()` ověří, že článek reálně odpovídá dotazu
              (deterministický re-resolve + title-similarity gate + krátký
              EN úsudek). Teprve TEĎ se zapíše do paměti (přes `curiosity._store`
              = deník + entity + RAG). Když neprojde → NEZAPÍŠE se a označí se
              k ranní opravě.
  3. RÁNO   — `unannounced_corrections()` dá `hans_idle` seznam nálezů, které
              neprošly → proaktivní oprava uživateli.

Pravidlo, které tenhle modul drží: **do paměti se nezapisuje nic, co neprošlo
nočním ověřením.** Provizorní odpověď je označená jako provizorní i tónem.

⚠️ DĚLBA PRÁCE MEZI GATY (ověřeno testem, nepřehánět si deterministiku):
  • (a) re-resolve a (b) title-similarity chytí *nestabilní* resolution a
    *hrubý* nesoulad („Architektonické vlivy" → „Sursockovo muzeum").
  • **Near-miss pravopis NEROZLIŠÍ** — „Zootropium" vs „Zootropic" se liší jen
    koncovkou, tedy přesně tak, jak vypadá české skloňování; jakýkoli práh, co
    zamítne tohle, zamítne i „Dadština"/„dadštině". Proto o něm rozhoduje až
    (c) úsudek modelu.
  • Když model není k dispozici (mozek dole, nejednoznačná odpověď), nález
    zůstane *pending* / *rejected* — **nikdy se nezapíše naslepo**. Bezpečnost
    smyčky tedy nestojí na chytrosti gatů, ale na tom, že nerozhodnuto ≠ ano.

Vzor: [[anticonfabulation-guiding-principle]], [[ollama-deferred-processing]],
[[learning-must-close-the-loop]].
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Optional

_log = logging.getLogger(__name__)

# Provizorní odpověď = deterministický rámec (bypass mimo LLM persona vrstvu),
# uvnitř je jen shrnutí z reálného článku. Tón MUSÍ říct „teď jsem to našel",
# ne „vím" — jinak by se z provizorního nálezu stala zdánlivá vzpomínka.
_PROVISIONAL_TMPL = (
    "V paměti jsem o tom nic neměl, %(oslov)s, tak jsem se právě podíval. "
    "Podle toho, co jsem teď našel (%(source)s):\n\n%(summary)s\n\n"
    "Berte to zatím s rezervou — ještě jsem si to neověřil a nezapsal do paměti. "
    "Udělám to v noci a kdyby to nesedělo, ráno se ozvu."
)

# HANS_LOOKUP_HAD_NOTES_V1 (14. 9.) — dohledání spuštěné POJISTKOU (zápisky
# byly, ale neunesly odpověď). „V paměti jsem o tom nic neměl" tu byla NEPRAVDA:
# 14. 9. tak Hans odpověděl 3× na hrady Českého ráje, ačkoli měl dokončené
# studium „Český ráj a okolní hrady" a 11 studijních poznámek.
_PROVISIONAL_TMPL_ZAPISKY = (
    "Ve svých zápiscích jsem k tomu neměl dost, abych to řekl s jistotou, "
    "%(oslov)s, tak jsem se právě podíval. "
    "Podle toho, co jsem teď našel (%(source)s):\n\n%(summary)s\n\n"
    "Berte to zatím s rezervou — ještě jsem si to neověřil a nezapsal do paměti. "
    "Udělám to v noci a kdyby to nesedělo, ráno se ozvu."
)

_CORRECTION_TMPL = (
    "%(oslov)s, ještě k tomu, na co jste se ptal — „%(topic)s\". "
    "Odpověděl jsem tehdy z toho, co jsem narychlo našel, ale při nočním "
    "ověření to neobstálo (%(reason)s). Do paměti jsem si to proto NEZAPSAL "
    "a raději to neberte jako platné. Když budete chtít, můžu si téma zařadit "
    "do studia — stačí říct „nastuduj %(topic)s\"."
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS unverified_findings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL,
    asker         TEXT,
    query         TEXT,
    topic         TEXT,
    source        TEXT,
    resolved_title TEXT,
    url           TEXT,
    summary       TEXT,
    raw_text      TEXT,
    status        TEXT DEFAULT 'pending',
    verdict       TEXT,
    verified_ts   REAL,
    announced     INTEGER DEFAULT 0,
    announced_ts  REAL,
    created_ts    REAL
)
"""


def _cfg(config: dict) -> dict:
    return (config or {}).get("instant_lookup", {}) or {}


def ensure_schema(db_path: str) -> None:
    """Idempotentní vytvoření tabulky čekárny."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(_SCHEMA)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_uf_status "
                     "ON unverified_findings(status)")
        conn.commit()
    finally:
        conn.close()


# ── čekárna: zápis a čtení ───────────────────────────────────────────────────

def add_finding(db_path: str, *, asker: str, query: str, topic: str,
                source: str, resolved_title: str, url: str,
                summary: str, raw_text: str) -> Optional[int]:
    ensure_schema(db_path)
    now = time.time()
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cur = conn.execute(
            "INSERT INTO unverified_findings (ts, asker, query, topic, source,"
            " resolved_title, url, summary, raw_text, status, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (now, asker or "", query or "", topic or "", source or "",
             resolved_title or "", url or "", summary or "", raw_text or "",
             now))
        conn.commit()
        return int(cur.lastrowid)
    except Exception as e:
        _log.warning("instant_lookup: zápis nálezu selhal: %s", e)
        return None
    finally:
        conn.close()


def recent_pending_for_topic(db_path: str, topic: str,
                             max_age_s: float = 86400) -> Optional[dict]:
    """Už na tohle téma čeká nález? (aby opakovaný dotaz netahal článek znovu)"""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        r = conn.execute(
            "SELECT * FROM unverified_findings WHERE status='pending' "
            "AND LOWER(topic)=LOWER(?) AND ts > ? ORDER BY id DESC LIMIT 1",
            (topic, time.time() - max_age_s)).fetchone()
        return dict(r) if r else None
    except Exception:
        return None
    finally:
        conn.close()


def pending_findings(db_path: str, limit: int = 5) -> list:
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM unverified_findings WHERE status='pending' "
            "ORDER BY id ASC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.debug("pending_findings: %s", e)
        return []
    finally:
        conn.close()


def _set_status(db_path: str, fid: int, status: str, verdict: str) -> None:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute(
            "UPDATE unverified_findings SET status=?, verdict=?, verified_ts=? "
            "WHERE id=?", (status, verdict[:400], time.time(), int(fid)))
        conn.commit()
    except Exception as e:
        _log.warning("instant_lookup: status update selhal: %s", e)
    finally:
        conn.close()


def unannounced_corrections(db_path: str, limit: int = 10) -> list:
    """Zamítnuté nálezy, o kterých uživatel ještě neví (→ ranní oprava)."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM unverified_findings WHERE status='rejected' "
            "AND COALESCE(announced,0)=0 ORDER BY id ASC LIMIT ?",
            (int(limit),)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.debug("unannounced_corrections: %s", e)
        return []
    finally:
        conn.close()


def mark_announced(db_path: str, ids: list) -> None:
    if not ids:
        return
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.executemany(
            "UPDATE unverified_findings SET announced=1, announced_ts=? "
            "WHERE id=?", [(time.time(), int(i)) for i in ids])
        conn.commit()
    except Exception as e:
        _log.warning("instant_lookup: mark_announced: %s", e)
    finally:
        conn.close()


_PREDMET = re.compile(r"\bo\s+(.{2,80}?)\s*[?!.]*\s*$",
                      re.IGNORECASE | re.DOTALL)


def _delsi_nazev(topic, query):
    """HANS_ANCHOR_TRUNCATED_NAME_V1 — vrátí delší tvar tématu, když kotva
    usekla víceslovný NÁZEV na první slovo. Jinak None.

    `kotva_tematu` skládá téma z velkých písmen, jenže české názvy mají další
    slova malá. Změřeno 1. 9. na reálných zkouškách:
        „Nemocnice na kraji města" → „Nemocnice"  (dohledal definici špitálu)
        „Jeden svět nestačí"       → „Jeden"
        „Bolek a Lolek"            → „Bolek"
        „Heineken Open 2012"       → „Heineken Open"  (ztratil ročník)
    ⛔ Kotvu samotnou měnit NELZE — sdílí ji `relax_attempts` a umí věci, které
    tenhle predikát ne: nominativ („hradu Trosky" → „hrad Trosky"), závorky
    a věty bez předložky („kdy vzniklo Divadlo Járy Cimrmana?"), kde předmět
    vyjde prázdný. Proto se delší tvar jen ZKUSÍ a rozhodne o něm wiki gate.

    Sahá se sem jen při doloženém useknutí = kotva je PREFIX předmětu na
    hranici slova. „hrad Trosky" × „hradu Trosky" prefix není → beze změny.
    """
    if not topic or not query:
        return None
    m = _PREDMET.search((query or "").strip())
    if not m:
        return None
    pred = m.group(1).strip().rstrip("?!.,;:").strip()
    if not pred or len(pred) <= len(topic):
        return None
    a, b = pred.lower(), topic.strip().lower()
    if a.startswith(b) and a[len(b):len(b) + 1] in (" ", "-"):
        return pred
    return None


def _oslov(asker, config=None) -> str:
    """HANS_FINDINGS_TEST_PERSON_OSLOV_V1 — jak Hansa osloví protějšek v šablonách nálezů.

    ⚠️ Testovací identita NENÍ osoba. `kolac_exam.identita()` vrací technické
    jméno (dnes „zkouška"), které MUSÍ být v `config.test_persons`, aby se
    sebetest nezapsal do deníku ani RAG. Bez tohohle helperu z něj
    `cz_names.address` udělal vokativ a Hans Koláčovi odpovídal
    „…, Zkouško" (10 z 23 zkoušek, doloženo 1. 9.).

    Protahuje se predikát z `HANS_TEST_PERSON_V1`; sdílený helper proto, že
    šablony jsou DVĚ (provizorní odpověď + ranní oprava) a druhá by se na
    kopii kontroly dřív nebo později rozešla ([[test-the-fix-not-the-symptom]]).
    """
    try:
        _tp = [str(x).strip().lower()
               for x in ((config or {}).get("test_persons") or [])]
        if (asker or "").strip().lower() in _tp:
            return "pane"          # neutrální — zkouška má měřit odpověď, ne oslovení
    except Exception:
        pass
    try:
        from scripts.cz_names import address as _addr
        return _addr(asker) if asker else "pane"
    except Exception:
        return (asker or "pane")


def _je_test_osoba(asker, config=None) -> bool:
    """HANS_FINDINGS_TEST_NO_QUEUE_V1 (27. 9.) — testovací identita (`config.test_persons`)
    nesmí nic poslat do noční čekárny. Doloženo 27. 9.: „zkouška“ a „Marek“ tam měly
    26 nálezů, 22 z nich noc OVĚŘILA a zapsala do paměti jako Hansovu četbu —
    ačkoli testovací identita do deníku ani RAG nepíše (`HANS_TEST_PERSON_V1`).
    Stejný predikát jako `_oslov`."""
    try:
        return (asker or "").strip().lower() in [
            str(x).strip().lower() for x in ((config or {}).get("test_persons") or [])]
    except Exception:
        return False


def correction_text(row: dict, asker: Optional[str] = None,
                    config: Optional[dict] = None) -> str:
    if row.get("source") == CLAIM_SOURCE:              # HANS_CLAIM_CHECK_V1
        return claim_correction_text(row, asker, config)
    oslov = _oslov(asker, config)
    return _CORRECTION_TMPL % {
        "oslov": oslov,
        "topic": row.get("topic") or "to téma",
        "reason": row.get("verdict") or "nenašel jsem spolehlivý zdroj",
    }



# ── HANS_CLAIM_CHECK_V1 (28. 9., přání uživatele) — tvrzení z ÚVAHY ověřit v noci ──
# Úvahová otázka (HANS_REFLECTIVE_CHOICE_V1) jde k osobnosti BEZ opory, takže
# model smí fabulovat. Doloženo 28. 9.: „jakou skladbu byste dal k dílu
# o hradech?“ → Machaut, Hildegarda (správně) a „Ryba — Mistr Kryštof … té
# doby“ (vymyšlené). Pokyn uživatele: u takové odpovědi dodat, že to v noci
# ověří, a když to neobstojí, opravit to i v PAMĚTI (volný hovor do ní jde:
# deník, doslovný záznam v RAG, reflexe).
# Tok: HNED věta + řádek čekárny (source='uvaha') → V NOCI reasoning model
# vytáhne tvrzení, ke každému článek Wikipedie, verdikt → vyvrácené: ranní
# oprava + `lesson_learned` (korekční smyčka, neexpiruje) + oprava záznamu
# rozhovoru v RAG. Potvrzené se NIKAM nezapisují (nic nového se nedozvěděl).
CLAIM_SOURCE = "uvaha"
CLAIM_NOTE = ("Jména a díla, která jsem zmínil, si v noci ověřím — "
              "kdyby něco nesedělo, ráno se ozvu.")
_ZAJMENA_VYKANI = {"vy", "vás", "vám", "vámi", "váš", "vaše", "vaši", "vašeho",
                   "vašemu", "vašem", "vaším", "vašich", "vašim", "vašimi"}
_NAZEV_RE = re.compile(r"(?<![.!?:\n„\"])\s([A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ][a-záčďéěíňóřšťúůýž]{2,})")
_ROK_RE = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")
_TITUL_RE = re.compile(r"[„\"]([^“\"]{3,60})[“\"]")


def claim_names(text: str, vynech=()) -> list:
    """Vlastní jména uvnitř vět, letopočty a tituly v uvozovkách — signál, že
    odpověď tvrdí něco ověřitelného o světě. `vynech` = jména persony/tazatele."""
    t = str(text or "")
    ven = {str(v).lower() for v in vynech if v} | _ZAJMENA_VYKANI
    out = []
    for m in _NAZEV_RE.finditer(t):
        w = m.group(1)
        if w.lower() not in ven and not any(w.lower().startswith(v[:5]) for v in ven if len(v) >= 5):
            out.append(w)
    out += _ROK_RE.findall(t) + _TITUL_RE.findall(t)
    seen, res = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            res.append(x)
    return res


def add_claim_check(db_path: str, *, asker: str, query: str, answer: str,
                    names: list, chatlog_id: str = "") -> Optional[int]:
    import json as _json
    return add_finding(db_path, asker=asker, query=query,
                       topic=", ".join(names[:6])[:200], source=CLAIM_SOURCE,
                       resolved_title="", url="", summary=answer,
                       raw_text=_json.dumps({"chatlog": chatlog_id}, ensure_ascii=False))


_CLAIM_EXTRACT_SYS = (
    "Extract CHECKABLE FACTUAL claims about the real world from the assistant's answer "
    "(people, works, places, dates, who made what, when). Ignore opinions, feelings, "
    "hypotheticals and advice. Max 5. For each: entity = the name to look up in an "
    "encyclopedia (as written, nominative if possible); claim_en = the claim in English; "
    "claim_cz = the same claim as a short Czech sentence. JSON "
    "{\"claims\": [{\"entity\": …, \"claim_en\": …, \"claim_cz\": …}]}.")
# HANS_CLAIM_JUDGE_QUOTE_V1 (29. 9.) — soudce si dřív „pravdu“ domýšlel sám:
# u neexistujícího Dvořákova „Slavnostního pochodu z Nového světa“ napsal
# opravu „je součástí 9. symfonie“ (další smyšlenka) a ta šla do lekcí i RAG.
# Teď: vyvrácení jen s DOSLOVNOU citací z článku (kód ji ověří), a když dílo
# v článku prostě není → NOT_FOUND = „nepodařilo se doložit“, nic se nedomýšlí.
# Změřeno 29. 9. na 7 tvrzeních (4 vady, 3 pravdy): 0 vymyšlených oprav,
# 0 vyvrácených pravd; oba neexistující tituly → NOT_FOUND.
_CLAIM_JUDGE_SYS = (
    "You check ONE claim against an encyclopedia article. Verdicts: "
    "SUPPORTED = the article confirms the claim. "
    "CONTRADICTED = the article contains a sentence that directly shows the claim is false (e.g. a "
    "different author, a different date, the correct title of a misnamed work). Then `quote` = that "
    "sentence copied VERBATIM from the article, and pravda_cz = one short Czech sentence stating ONLY "
    "what the quote says - add nothing that is not in the quote. "
    "NOT_FOUND = the claim names a specific work, title or event and the article does not mention it "
    "anywhere. UNKNOWN = anything else. For verdicts other than CONTRADICTED leave quote and "
    "pravda_cz empty. JSON {\"verdict\": ..., \"quote\": ..., \"pravda_cz\": ...}.")
NEDOLOZENO = "to se mi nepodařilo doložit"


def _norm_ws(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


def _reason_json(config: dict, prompt: str, system: str, schema: dict,
                 num_predict: int = 700):
    import json as _json
    from scripts.ollama_client import ollama_generate
    c = _cfg(config)
    syn = (config.get("synthesis", {}) or {})
    model = str(c.get("claim_model") or syn.get("reasoning_model") or "qwen3:30b")
    opts = {"temperature": 0, "num_ctx": 8192, "num_predict": num_predict}
    think = None
    if model.startswith("qwen3"):
        opts["num_gpu"] = int(syn.get("reasoning_num_gpu", 0))
        prompt += " /no_think"
        # HANS_CLAIM_CHECK_NO_THINK_V1 (29. 9.) — samo „/no_think“ nestačí:
        # Ollama pak JSON vrátí v poli `thinking` a `response` je prázdné
        # (změřeno živě) → noc 28./29. 9. odložila ověření jako „mozek dole“.
        think = False
    raw = ollama_generate(model, prompt, system=system, config=config,
                          timeout=int(c.get("claim_timeout", 900)), keep_alive=0,
                          format=schema, options=opts, think=think)
    if not raw:
        # HANS_CLAIM_CHECK_HONEST_V1 (29. 9.) — dřív tiché None; noc 28./29. 9.
        # odložila ověření 3× a v logu nezůstala ani stopa proč.
        _log.warning("claim_check: %s nevrátil nic (mozek dole / timeout)", model)
        return None
    try:
        return _json.loads(re.sub(r"<think>.*?</think>", "", raw, flags=re.S))
    except Exception:
        _log.warning("claim_check: %s vrátil nečitelný JSON: %.200r", model, raw)
        return None


def _verify_claims(config: dict, row: dict):
    """(True|False|None, důvod, [vyvrácená]) — None = mozek dole."""
    ans = (row.get("summary") or "").strip()
    d = _reason_json(config, "Question: %s\n\nAssistant's answer:\n%s" % (
        row.get("query") or "", ans[:3000]), _CLAIM_EXTRACT_SYS,
        {"type": "object", "properties": {"claims": {"type": "array", "items": {
            "type": "object", "properties": {"entity": {"type": "string"},
                                            "claim_en": {"type": "string"},
                                            "claim_cz": {"type": "string"}},
            "required": ["entity", "claim_en", "claim_cz"]}}}, "required": ["claims"]})
    if d is None:
        return None, "mozek nedostupný", []
    claims = [c for c in (d.get("claims") or []) if c.get("entity") and c.get("claim_en")][:5]
    if not claims:
        return True, "odpověď neobsahovala ověřitelná tvrzení", []
    try:
        from scripts.web_reader import WebReader
        wr = WebReader(config)
    except Exception as e:
        _log.debug("claim_check: WebReader: %s", e)
        return None, "čtečka nedostupná", []
    vyvracena, podlozena, nevim = [], 0, 0
    for cl in claims:
        art = None
        for lang in ("cs", "en"):
            try:
                art = wr.wikipedia_article(cl["entity"], lang=lang, max_chars=4000)
            except Exception:
                art = None
            if art and (art.get("text") or "").strip():
                break
        if not art or not (art.get("text") or "").strip():
            nevim += 1
            continue
        v = _reason_json(config, "Claim: %s\n\nArticle „%s“:\n%s" % (
            cl["claim_en"], art.get("title") or cl["entity"], art["text"][:3500]),
            _CLAIM_JUDGE_SYS, {"type": "object", "properties": {
                "verdict": {"type": "string",
                            "enum": ["SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNKNOWN"]},
                "quote": {"type": "string"},
                "pravda_cz": {"type": "string"}}, "required": ["verdict", "quote", "pravda_cz"]},
            num_predict=400)
        if v is None:
            return None, "mozek nedostupný", []
        verd = str(v.get("verdict") or "UNKNOWN")
        _log.info("claim_check: %s → %s (%s)", cl["claim_en"][:80], verd,
                  art.get("title"))
        _q = _norm_ws(v.get("quote"))
        if verd == "CONTRADICTED" and not (len(_q) >= 15 and _q in _norm_ws(art["text"][:3500])
                                           and str(v.get("pravda_cz") or "").strip()):
            _log.info("claim_check: vyvrácení bez doslovné citace → UNKNOWN")
            verd = "UNKNOWN"                        # HANS_CLAIM_JUDGE_QUOTE_V1
        if verd == "CONTRADICTED":
            vyvracena.append({"entity": cl["entity"], "claim": cl.get("claim_cz") or cl["claim_en"],
                              "correction": str(v.get("pravda_cz") or "").strip(),
                              "quote": str(v.get("quote") or "").strip(),
                              "zdroj": art.get("title") or cl["entity"]})
        elif verd == "NOT_FOUND":
            _zdroj = art.get("title") or cl["entity"]
            vyvracena.append({"entity": cl["entity"], "claim": cl.get("claim_cz") or cl["claim_en"],
                              "correction": "%s (článek „%s“ o tom nic neuvádí)" % (NEDOLOZENO, _zdroj),
                              "zdroj": _zdroj, "nedolozeno": True})
        elif verd == "SUPPORTED":
            podlozena += 1
        else:
            nevim += 1
    if vyvracena:
        return False, "; ".join("%s → %s" % (x["claim"], x["correction"])
                                for x in vyvracena)[:400], vyvracena
    return True, "tvrzení: potvrzeno %d, neověřitelné %d" % (podlozena, nevim), []


def _promitni_opravu(config: dict, db_path: str, row: dict, vyvracena: list):
    """Vyvrácené tvrzení do PAMĚTI: lesson_learned (korekční smyčka) + oprava
    doslovného záznamu rozhovoru v RAG (hans_pripady)."""
    import json as _json
    asker = row.get("asker") or ""
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        for x in vyvracena:
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
                (time.time(), "lesson_learned", asker,
                 ("Při nočním ověření jsem nedoložil tvrzení „%s“ — článek „%s“ o tom nic neuvádí."
                  % (x["claim"].rstrip("."), x.get("zdroj") or x["entity"])
                  if x.get("nedolozeno") else
                  "Při nočním ověření jsem zjistil, že jsem se spletl: %s" % x["correction"]),
                 _json.dumps({"claim": x["claim"], "correction": x["correction"],
                              "entity": x["entity"], "zdroj": "noční ověření (%s)" % x["zdroj"]},
                             ensure_ascii=False)))
        conn.commit()
        conn.close()
    except Exception as e:
        _log.warning("claim_check: lesson zápis selhal: %s", e)
    try:
        chatlog = (_json.loads(row.get("raw_text") or "{}") or {}).get("chatlog") or ""
    except Exception:
        chatlog = ""
    if chatlog:
        try:
            from scripts.hans_knowledge import HansKnowledge
            oprava = "\n".join(
                ("NEDOLOŽENO po nočním ověření: „%s“ — %s" if x.get("nedolozeno") else
                 "OPRAVA po nočním ověření: „%s“ NEPLATÍ — %s") % (x["claim"], x["correction"])
                for x in vyvracena)
            # HANS_CLAIM_JUDGE_QUOTE_V1 — týž tvar jako `_upload_chat_memory`
            # (dřív se ztratil čas hovoru, štítek `kdy` a jméno persony).
            import datetime as _dt
            try:
                from scripts.hans_persona import persona_name
                _pn = persona_name(config)
            except Exception:
                _pn = "Hans"
            _kdy = _dt.datetime.fromtimestamp(float(row.get("ts") or time.time())
                                              ).strftime("%A %-d.%-m.%Y %H:%M")
            _q = (row.get("query") or "").strip()
            HansKnowledge(config).upload(
                collection_key="hans_pripady", doc_id=chatlog,
                title="Rozhovor s %s: %s" % (asker, _q[:60]),
                text="Rozhovor s %s (%s):\n[NEOVĚŘENO — vlastní výrok v hovoru, ne ověřený fakt]\n"
                     "%s: %s\n%s: %s\n\n%s" % (asker, _kdy, asker, _q, _pn,
                                                (row.get("summary") or "").strip(), oprava),
                metadata={"kdy": _kdy, "osoba": asker, "typ": "rozhovor", "overeno": False,
                          "puvod": "vlastni_vyrok", "opraveno": True})
        except Exception as e:
            _log.warning("claim_check: oprava RAG selhala: %s", e)


def claim_correction_text(row: dict, asker: Optional[str] = None,
                          config: Optional[dict] = None) -> str:
    oslov = _oslov(asker, config)
    casti = []
    for kus in str(row.get("verdict") or "").split("; "):
        tvrzeni, _, pravda = kus.partition(" → ")
        tvrzeni, pravda = tvrzeni.strip().rstrip("."), pravda.strip().rstrip(".")
        if tvrzeni and pravda and pravda.startswith(NEDOLOZENO):   # HANS_CLAIM_JUDGE_QUOTE_V1
            casti.append("Tvrdil jsem, že %s — ale %s, beru to zpět." % (tvrzeni, pravda))
        elif tvrzeni and pravda:
            casti.append("Tvrdil jsem, že %s — ale ve skutečnosti: %s." % (tvrzeni, pravda))
    if not casti:
        casti = [str(row.get("verdict") or "něco nesedělo")]
    return ("%s, ještě k našemu rozhovoru: slíbil jsem, že si v noci ověřím, co "
            "jsem zmínil. %s Zapsal jsem si to, ať to příště neopakuji."
            % (oslov, " ".join(casti)))

# ── 1) OKAMŽITÉ DOHLEDÁNÍ ────────────────────────────────────────────────────

def lookup_now(config: dict, db_path: str, topic: str, query: str,
               asker: Optional[str] = None,
               mel_zapisky: bool = False) -> Optional[str]:   # HANS_LOOKUP_HAD_NOTES_V1
    """Dohledej téma HNED a vrať PROVIZORNÍ odpověď (nebo None → volající
    použije poctivé „nemám záznam").

    Do paměti (deník/entity/RAG) NEZAPISUJE NIC — jen do čekárny.
    Deferral-safe: mozek dole / herní mód / článek nenalezen → None.
    """
    c = _cfg(config)
    if not c.get("enabled", True):
        return None
    topic = (topic or "").strip()
    if not topic:
        return None

    # mozek dole → provizorní shrnutí by stejně nevzniklo; nech honest bypass
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return None
    except Exception:
        return None

    # už na to čeká nález z dneška → neopakuj stahování ani zápis
    try:
        prev = recent_pending_for_topic(db_path, topic)
    except Exception:
        prev = None
    if prev and (prev.get("summary") or "").strip():
        return _render_provisional(prev, asker, config, mel_zapisky)

    try:
        from scripts.web_reader import WebReader
        wr = WebReader(config)
        _lang = str(config.get("curiosity", {}).get("wiki_lang", "cs"))
        _max = int(c.get("max_chars", 3000))
        # HANS_ANCHOR_TRUNCATED_NAME_V1 — kotva mohla useknout víceslovný název.
        # O delším tvaru rozhoduje EXISTUJÍCÍ wiki gate, ne nová heuristika:
        # změřeno 1. 9., že název najde („Nemocnice na kraji města", „Bolek
        # a Lolek", „Heineken Open 2012") a popisnou frázi zamítne („Vývoj
        # zbrojnic a jejich dopad…" → None).
        _alt = _delsi_nazev(topic, query)
        if _alt:
            art = wr.wikipedia_article(_alt, lang=_lang, max_chars=_max)
            if not art:
                # Delší tvar článek nemá → dotaz je popisná fráze, ne název.
                # Dohledat podle USEKNUTÉ kotvy je horší než nedohledat nic:
                # doloženo „Vývoj zbrojnic…" → heslo „Vývoj" (definice slova).
                _log.info("HANS_ANCHOR_TRUNCATED_NAME_V1: %r neexistuje a %r je "
                          "useknuté → nedohledávám (raději nic než cizí heslo)",
                          _alt, topic)
                return None
            topic = _alt
        else:
            art = wr.wikipedia_article(topic, lang=_lang, max_chars=_max)
    except Exception as e:
        _log.debug("instant_lookup: fetch selhal: %s", e)
        return None
    if not art or not (art.get("text") or "").strip():
        return None

    # HANS_WIKI_NAMESAKE_V1 (5.9.) - nasel se clanek o VECI POJMENOVANE po tom,
    # nac se uzivatel ptal? Dolozeno: "neco od Karla Capka" -> "Karla Capka
    # (Pisek)", tedy ULICE. Sklonovany tvar dotazu se s nazvem ulice shoduje
    # doslova, takze wiki gate nema co zamitnout; rozhodne az P138 ve
    # Wikidatech. Sedi to sem, a ne do `_wikipedia_search`, protoze tady vzniká
    # skoda: odpoved jde cloveku a v noci by se zapsala do pameti.
    _namesake = None
    try:
        from scripts.hans_facts import pojmenovano_po_cloveku
        _osoba = pojmenovano_po_cloveku(art.get("title") or topic, topic,
                                        lang=_lang)
        if _osoba:
            _art2 = wr.wikipedia_article(_osoba, lang=_lang, max_chars=_max)
            # Prazdny druhy pokus nesmi shodit dohledani - puvodni clanek je
            # sice o ulici, ale porad je to lepsi nez tvrdy None; a
            # `_render_provisional` na neshodu nazvu upozorni sam.
            if _art2 and (_art2.get("text") or "").strip():
                art = _art2
                _namesake = _osoba
    except Exception as e:
        _log.debug("HANS_WIKI_NAMESAKE_V1 preskocen: %s", e)

    summary = _summarize_for_user(config, topic, art)
    if not summary:
        return None  # mozek mimo uprostřed → honest bypass

    row = {
        "topic": topic, "source": "wikipedia",
        "resolved_title": art.get("title") or topic,
        "url": art.get("url") or "", "summary": summary,
        # HANS_WIKI_NAMESAKE_NOTE_V1 - proc se heslo jmenuje jinak nez dotaz.
        # ⚠️ MEZ: nese se jen v pameti tohohle behu, ne v tabulce. Kdyz se
        # tentyz dotaz zopakuje TYZ DEN, vrati se ulozeny nalez pres
        # `recent_pending_for_topic` a poznamka spadne zpet na obecnou. Je to
        # podcenene, ne nepravdive, a novy sloupec kvuli tomu nestoji za
        # migraci; kdyby to nekdy vadilo, patri to do schematu.
        "namesake": _namesake,
    }
    if _je_test_osoba(asker, config):   # HANS_FINDINGS_TEST_NO_QUEUE_V1
        _log.info("instant_lookup: '%s' od testovací identity → čekárna ne", topic)
        return _render_provisional(row, asker, config, mel_zapisky)
    try:
        add_finding(db_path, asker=asker or "", query=query, topic=topic,
                    source="wikipedia", resolved_title=row["resolved_title"],
                    url=row["url"], summary=summary, raw_text=art.get("text") or "")
    except Exception as e:
        _log.warning("instant_lookup: čekárna nezapsala: %s", e)
        return None
    _log.info("instant_lookup: '%s' → '%s' (provizorně, čeká na ověření)",
              topic, row["resolved_title"])
    return _render_provisional(row, asker, config, mel_zapisky)


def _render_provisional(row: dict, asker: Optional[str],
                        config: Optional[dict] = None,
                        mel_zapisky: bool = False) -> str:
    oslov = _oslov(asker, config)
    src = row.get("resolved_title") or "Wikipedie"
    summary = (row.get("summary") or "").strip()
    # Heslo se nejmenuje jako dotaz → řekni to ROVNOU, ať to uživatel pozná sám
    # a nemusí čekat na noční ověření. Doloženo živě: „Zootropium" → heslo
    # „Zootropic" (přes redirect článek o heterotrofech) = úplně jiná věc.
    # HANS_WIKI_NAMESAKE_NOTE_V1 (5.9.) - kdyz se heslo zmenilo DOLOZENE
    # (P138 ve Wikidatech), neni to "nejblizsi nalez" a varovat pred nim by
    # bylo zavadejici: prave naopak, tenhle nazev je ten spravny a puvodni
    # shoda byla past. Doloženo pri zivem overeni opravy 5.9.: Hans vratil
    # spravneho Karla Capka a pod nim napsal, ze "muze jit o neco uplne
    # jineho". Duvod se proto rekne misto varovani.
    if row.get("namesake"):
        summary += ("\n\n(Poznámka: dotaz „%s\" byl ve skloňovaném tvaru a "
                    "Wikipedie pod ním vede něco pojmenovaného po té osobě; "
                    "vzal jsem proto heslo o ní samotné.)"
                    % (row.get("topic") or ""))
    elif not _titles_align(row.get("topic") or "", src):
        summary += ("\n\n(Poznámka: heslo přesně na „%s\" jsem nenašel, tohle je "
                    "nejbližší nález „%s\" — může jít o něco úplně jiného.)"
                    % (row.get("topic") or "", src))
    _tmpl = _PROVISIONAL_TMPL_ZAPISKY if mel_zapisky else _PROVISIONAL_TMPL
    if _je_test_osoba(asker, config):   # HANS_FINDINGS_TEST_NO_QUEUE_V1 — nic nesliby
        _tmpl = _tmpl.replace(
            "Berte to zatím s rezervou — ještě jsem si to neověřil a nezapsal do paměti. "
            "Udělám to v noci a kdyby to nesedělo, ráno se ozvu.",
            "Berte to s rezervou — je to jen rychlé dohledání, do paměti si ho neukládám "
            "a ověřovat ho nebudu.")
    return _tmpl % {   # HANS_LOOKUP_HAD_NOTES_V1
        "oslov": oslov,
        "source": "Wikipedie — heslo „%s\"" % src,
        "summary": summary,
    }


def _sklonovany_tvar(a: str, b: str, minlen: int = 4) -> bool:
    """Jsou to tytéž tokeny, jen jeden ve skloněném tvaru?

    Rozhoduje STRIKTNÍ prefix: „isaac" < „isaaca", „jiri" < „jirim".
    Naopak „babice" × „babicku" ani dvojice dvou různých obcí se shodným
    začátkem prefixem nejsou — jen sdílejí začátek, a to jsou doloženě
    RŮZNÉ věci.

    ⛔ ZÁMĚRNĚ NEPOUŽÍVÁ `web_reader._token_match`, ačkoli řeší totéž
    skloňování. Ten bere „prefix ≥4 znaky" oboustranně, což je správné
    pro HLEDÁNÍ článku (radši široká síť), ale tady by to VYPNULO
    varování i tam, kde patří. Změřeno 11. 9. na 30 dvojicích
    z `unverified_findings`: `_token_match` = 2 zisky a 2 ZTRÁTY
    („Babicku" → „Babice (okres Třebíč)" a jedna obdobná dvojice obcí),
    striktní prefix = 2 zisky a 0 ztrát. Sjednocovat je NENÍ zlepšení.
    """
    if a == b:
        return True
    kratsi, delsi = (a, b) if len(a) <= len(b) else (b, a)
    if len(kratsi) < minlen:
        return False
    return delsi.startswith(kratsi)


def _titles_align(topic: str, title: str) -> bool:
    """Sedí název hesla na dotaz aspoň hrubě? (jen pro varování v odpovědi —
    o platnosti nálezu rozhoduje až noční ověření, viz docstring modulu.)

    HANS_WIKI_ALIGN_DECLENSION_V1 (11. 9.) — porovnání podřetězců neuznalo
    skloněný dotaz: „Isaaca Asimova" není podřetězcem „Isaac Asimov" ani
    naopak, takže Hans ke správně nalezenému heslu připsal, že „může jít
    o něco úplně jiného". Proto se navíc zkouší shoda po TOKENECH.
    """
    def _fold(s: str) -> str:
        import unicodedata
        s = unicodedata.normalize("NFKD", (s or "").lower())
        return "".join(ch for ch in s if not unicodedata.combining(ch))
    a, b = _fold(topic), _fold(title)
    if not a or not b:
        return True
    if a in b or b in a:
        return True
    # Tokenově: každé obsahové slovo TITULU musí mít protějšek v dotazu.
    # Směr je podstatný — titul smí být kratší než dotaz („hrad Kost" →
    # „Kost"), ale nesmí přinést slovo, o které nikdo nežádal.
    try:
        from scripts.web_reader import _title_tokens, _PAREN
        tt = _title_tokens(_PAREN.sub("", title or ""))
        qt = _title_tokens(topic)
    except Exception:
        return False
    if not tt or not qt:
        return False
    return all(any(_sklonovany_tvar(q, x) for q in qt) for x in tt)


def _summarize_for_user(config: dict, topic: str, art: dict) -> Optional[str]:
    """Shrnutí článku HANS-CZECH modelem (rezidentní, žádný VRAM handoff).

    ⚠️ ZÁMĚRNĚ NEPOUŽÍVÁ `web_reader._summarize` — ten jede na BASE modelu
    (8 GB) a v interaktivním chatu by evictoval rezidentní hans-czech
    (8+8 > 16 GB VRAM) = thrashing uprostřed rozhovoru. Viz [[ollama-vram-tiers]],
    [[study-vram-handoff]].
    """
    c = _cfg(config)
    model = str((config.get("hans_dialog", {}) or {}).get(
        "ollama_model", "hans-czech:latest"))
    text = (art.get("text") or "")[:int(c.get("max_chars", 3000))]
    prompt = (
        "Níže je text encyklopedického článku „%s\". Shrň ve 2 až 4 větách, "
        "co je téma „%s\" — nejdřív jednou větou KDO/CO to je, pak konkrétní "
        "fakta z textu. Drž se VÝHRADNĚ textu, nic si nepřidávej. Bez uvozovek.\n\n"
        "Text:\n%s" % (art.get("title") or topic, topic, text))
    try:
        from scripts.ollama_client import ollama_chat
        out = ollama_chat(
            model,
            [{"role": "user", "content": prompt}],
            config=config,
            timeout=int(c.get("llm_timeout", 45)),
            options={"num_predict": int(c.get("num_predict", 220)),
                     "num_ctx": int(c.get("num_ctx", 4096)),
                     "temperature": 0.2},
        )
    except Exception as e:
        _log.debug("instant_lookup: summarize: %s", e)
        return None
    out = (out or "").strip()
    return out or None


# ── 2) NOČNÍ OVĚŘENÍ ─────────────────────────────────────────────────────────

def verify_pending(config: dict, db_path: str, curiosity=None,
                   limit: Optional[int] = None) -> str:
    """Ověř čekající nálezy. Vrací kód: 'deferred' | 'idle' | 'done:N/M'.

    Ověřené → TEPRVE TEĎ se zapíšou do paměti. Neověřené → zůstanou mimo paměť
    a jdou do ranní opravy. Deferral-safe: mozek dole → 'deferred' (nic se
    nezmění, zkusí se příště).
    """
    c = _cfg(config)
    if not c.get("enabled", True):
        return "idle"
    rows = pending_findings(db_path, limit=int(limit or c.get("verify_batch", 5)))
    if not rows:
        return "idle"
    try:
        from scripts.ollama_client import brain_available
        if not brain_available(config):
            return "deferred"
    except Exception:
        return "deferred"

    ok = 0
    for row in rows:
        if row.get("source") == CLAIM_SOURCE:          # HANS_CLAIM_CHECK_V1
            try:
                v_ok, reason, vyvr = _verify_claims(config, row)
            except Exception as e:
                _log.warning("claim_check: ověření #%s selhalo: %s", row.get("id"), e)
                continue
            if v_ok is None:
                _log.info("claim_check: #%s odloženo (%s)", row.get("id"), reason)
                return "deferred"
            if v_ok:
                _set_status(db_path, row["id"], "verified", reason)
                ok += 1
            else:
                _promitni_opravu(config, db_path, row, vyvr)
                _set_status(db_path, row["id"], "rejected", reason)
                _log.info("claim_check: VYVRÁCENO #%s (%s) → paměť opravena, ranní oprava",
                          row.get("id"), reason[:120])
            continue
        try:
            verdict_ok, reason = _verify_one(config, row)
        except Exception as e:
            _log.warning("instant_lookup: ověření '%s' selhalo: %s",
                         row.get("topic"), e)
            continue  # nech pending → zkusí se příště
        if verdict_ok is None:
            return "deferred"  # mozek spadl uprostřed dávky
        if verdict_ok:
            if _commit_to_memory(config, row, curiosity):
                _set_status(db_path, row["id"], "verified", reason)
                ok += 1
                _log.info("instant_lookup: OVĚŘENO '%s' → zapsáno do paměti",
                          row.get("topic"))
            else:
                _log.warning("instant_lookup: '%s' ověřeno, ale zápis do paměti "
                             "selhal — zůstává pending", row.get("topic"))
        else:
            _set_status(db_path, row["id"], "rejected", reason)
            _log.info("instant_lookup: ZAMÍTNUTO '%s' (%s) → ranní oprava",
                      row.get("topic"), reason)
            # HANS_CLAIM_CHECK_V1 — provizorní odpověď UŽ je v paměti (deník
            # human_chat + reflexe rozhovoru). Samotné neuložení nálezu ji
            # nezruší → oprava do paměti stejnou cestou jako tvrzení z úvahy.
            try:
                _promitni_opravu(config, db_path, row, [{
                    "entity": row.get("topic") or "",
                    "claim": "o „%s“ jsem odpověděl podle narychlo nalezeného hesla "
                             "„%s“: %s" % (row.get("topic") or "",
                                          row.get("resolved_title") or "",
                                          (row.get("summary") or "")[:200]),
                    "correction": "tahle odpověď neobstála při nočním ověření (%s), "
                                  "neplatí" % reason,
                    "zdroj": row.get("resolved_title") or "Wikipedie"}])
            except Exception as _pe:
                _log.warning("instant_lookup: oprava paměti selhala: %s", _pe)
    return "done:%d/%d" % (ok, len(rows))


def _verify_one(config: dict, row: dict):
    """(True|False|None, důvod). None = mozek dole → neroz­hodnuto."""
    topic = (row.get("topic") or "").strip()
    title = (row.get("resolved_title") or "").strip()
    c = _cfg(config)

    # (a) deterministicky: opakuj resolution — musí vyjít TÝŽ článek
    try:
        from scripts.web_reader import WebReader, _title_similarity
        wr = WebReader(config)
        again = wr._wikipedia_search(
            topic, str(config.get("curiosity", {}).get("wiki_lang", "cs")))
    except Exception as e:
        _log.debug("instant_lookup verify search: %s", e)
        again, _title_similarity = None, None
    if again and title and again.strip().lower() != title.lower():
        return False, ("hledání téhož dotazu teď vede na jiné heslo („%s\" "
                       "místo „%s\")" % (again, title))

    # (b) přísnější title-similarity než při čtení (tam 0.6)
    try:
        sim = _title_similarity(topic, title) if _title_similarity else 1.0
    except Exception:
        sim = 1.0
    min_sim = float(c.get("verify_min_similarity", 0.75))
    substring = topic.lower() in title.lower() or title.lower() in topic.lower()
    if not substring and sim < min_sim:
        return False, ("název hesla „%s\" neodpovídá dost přesně dotazu „%s\""
                       % (title, topic))

    # (c) krátký úsudek modelu — ANGLICKY (vzor [[reasoning-tier-when-to-use]]),
    #     na BASE modelu uvnitř VRAM handoffu (v noci je hans-czech odpojený).
    return _llm_judge(config, topic, title, row.get("raw_text") or "")


def _llm_judge(config: dict, topic: str, title: str, raw_text: str):
    """YES/NO: popisuje článek reálně to, na co se uživatel ptal?"""
    c = _cfg(config)
    model = str(c.get("judge_model")
                or (config.get("hans_dialog", {}) or {}).get(
                    "ollama_model", "hans-czech:latest"))
    prompt = (
        "A user asked about the topic: \"%s\".\n"
        "An encyclopedia article titled \"%s\" was retrieved. Its beginning:\n"
        "---\n%s\n---\n"
        "Question: does this article actually describe the thing the user asked "
        "about? Answer with a single word: YES or NO.\n"
        "Answer NO if the article is about a different (merely similar-sounding "
        "or broader) subject." % (topic, title, raw_text[:900]))
    try:
        from scripts.ollama_client import ollama_chat
        out = ollama_chat(
            model, [{"role": "user", "content": prompt}], config=config,
            timeout=int(c.get("judge_timeout", 90)),
            options={"num_predict": 8, "temperature": 0.0})
    except Exception as e:
        _log.debug("instant_lookup judge: %s", e)
        return None, "úsudek se nepodařilo získat"
    if out is None:
        return None, "mozek nedostupný"
    ans = (out or "").strip().upper()
    if ans.startswith("YES") or ans.startswith("ANO"):
        return True, "ověřeno (heslo „%s\" odpovídá dotazu)" % title
    if ans.startswith("NO") or ans.startswith("NE"):
        return False, ("heslo „%s\" popisuje něco jiného, než na co jste se ptal"
                       % title)
    # nejednoznačná odpověď = NEověřeno (radši opatrně)
    return False, "ověření nebylo jednoznačné"


# ── HANS_LOOKUP_FULL_ARTICLE_V1 (9. 10.) — dočtení CELÉHO článku po ověření ──
# Do paměti šlo z ověřeného dohledání jen shrnutí (160–540 zn) z prvních
# 3 000 znaků článku → na jinou otázku k témuž heslu už Hans podklad neměl.
# Po ověření se proto článek stáhne celý, rozdělí stejně jako u studia
# (`hans_study._article_rest_parts`) a z každé části se uloží VÝPIS FAKTŮ jako
# běžné čtení (`web_read` se stejným titulem + RAG). Měření 9. 10. na třech
# heslech (`data/mereni/dohledani_docitani/`): čísel z článku ve shrnutí 0–2,
# ve studijní poznámce 0–3, ve výpisu faktů 16–18; přimyšlené číslo 0×.
# Běží JEN v nočním ověření: studijní model vedle rozhovoru zasekl Ollamu.
_FAKTA_SYS = (
    "Čteš část encyklopedického článku. Vypiš 10 až 15 KONKRÉTNÍCH faktů z textu "
    "(jména, letopočty, čísla, místa, kdo co udělal), každý jako jednu samostatnou "
    "českou větu na vlastním řádku. Drž se VÝHRADNĚ textu, nic si nepřidávej, "
    "nehodnoť, bez úvodu a bez číslování."
)
_FAKTA_CISLO = re.compile(r"(?<!\d)\d{3,4}(?!\d)")
_FAKTA_ODRAZKA = re.compile(r"^\s*(?:\d{1,2}[.)]\s+(?=[A-ZÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ\u201e])|[-*\u2022\u2013]\s+)")


def _fakta_z_vystupu(raw: str, part: str) -> str:
    """Očistí výpis faktů z modelu: bez číslování a odrážek, bez krátkých
    zbytků; věta s letopočtem nebo číslem, které v části článku NENÍ, se zahodí."""
    v_clanku = set(_FAKTA_CISLO.findall(part or ""))
    out = []
    for ln in (raw or "").splitlines():
        ln = _FAKTA_ODRAZKA.sub("", ln).strip()
        if len(ln) < 15 or ln.endswith(":"):   # i úvodní věta modelu („Zde je 15 faktů…:“)
            continue
        if any(c not in v_clanku for c in _FAKTA_CISLO.findall(ln)):
            continue
        out.append(ln)
    return "\n".join(out)


def _casti_textu(text: str, part_max: int, max_parts: int) -> list:
    """Rozdělí text na části do `part_max` znaků na hranicích řádků."""
    parts, cur = [], ""
    for ln in (text or "").split("\n"):
        while len(ln) > part_max:
            if cur.strip():
                parts.append(cur)
                cur = ""
            parts.append(ln[:part_max])
            ln = ln[part_max:]
        if cur and len(cur) + len(ln) + 1 > part_max:
            parts.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        parts.append(cur)
    return [p.strip() for p in parts if len(p.strip()) >= 300][:max_parts]


def zapis_fakta(config: dict, diary_path: str, knowledge, title: str, topic: str,
                url: str, parts: list, doc_prefix: str, zdroj: str = "wikipedia",
                popis: str = "z článku") -> tuple:
    """Z každé části textu nechá model vypsat fakta a uloží je jako běžné čtení
    (deník `web_read` + RAG). Vrací (zapsaných částí, vět). Sdílí ho dohledání
    (HANS_LOOKUP_FULL_ARTICLE_V1) i studium (HANS_STUDY_FACTS_V1).
    Zapisuje přímo do deníku (ne přes `diary_writer`), ať z každé části
    nevzniká další reflexe čtení."""
    c = _cfg(config)
    from scripts import hans_study as _hs
    from scripts.ollama_client import ollama_generate
    hotovo = vet = 0
    for i, part in enumerate(parts, 1):
        try:
            raw = ollama_generate(
                model=_hs._model(config), system=_FAKTA_SYS, config=config,
                prompt="Článek: %s — část %d z %d:\n%s\n\nVypiš fakta z této části."
                       % (title, i, len(parts), part),
                timeout=int(c.get("full_article_timeout", 300)), keep_alive=0,
                options={"temperature": 0.2, "num_ctx": 8192, "num_predict": 900})
        except Exception as e:
            _log.warning("zapis_fakta: model selhal u '%s' (%d/%d): %s",
                         title, i, len(parts), e)
            break
        fakta = _fakta_z_vystupu(raw or "", part)
        if len(fakta) < 200:
            break                          # model dole nebo prázdný výpis
        note = "[%s] Fakta %s (část %d/%d):\n%s" % (topic, popis, i, len(parts), fakta)
        conn = sqlite3.connect(diary_path, timeout=10.0)
        try:
            conn.execute(
                "INSERT INTO diary (ts, event_type, title, note, source_url) "
                "VALUES (?,?,?,?,?)",
                (time.time(), "web_read", title[:120], note, url))
            conn.commit()
        finally:
            conn.close()
        if knowledge is not None and getattr(knowledge, "enabled", False):
            try:
                knowledge.upload(
                    collection_key=str((config.get("curiosity", {}) or {}).get(
                        "rag_collection", "hans_cetba")),
                    doc_id="%s_p%d" % (doc_prefix, i),
                    title="%s — fakta %s (%d/%d)" % (title[:100], popis, i, len(parts)),
                    text=fakta,
                    metadata={"téma": topic, "zdroj": zdroj, "url": url,
                              "typ": "fakta_z_clanku"})
            except Exception as e:
                _log.debug("zapis_fakta RAG upload: %s", e)
        hotovo += 1
        vet += fakta.count("\n") + 1
    return hotovo, vet


def _read_full_article(config: dict, row: dict, curiosity) -> int:
    """Z celého článku ověřeného nálezu zapíše výpisy faktů. Vrací počet
    zapsaných částí; `full_article_parts` 0 = vypnuto."""
    c = _cfg(config)
    max_parts = int(c.get("full_article_parts", 4))
    title = (row.get("resolved_title") or "").strip()
    url = row.get("url") or ""
    m = re.match(r"https://(\w+)\.wikipedia\.org/wiki/", url)
    if max_parts <= 0 or not m or not title or curiosity is None:
        return 0
    from scripts.web_reader import WebReader
    from scripts import hans_study as _hs
    full = WebReader(config)._wiki_extract(title, m.group(1), intro_only=False)
    parts, zbyva = _hs._article_rest_parts(
        full, 0, int(c.get("full_article_part_chars", 12000)), max_parts)
    if not parts:
        return 0
    hotovo, vet = zapis_fakta(
        config, curiosity._diary_path, getattr(curiosity, "_knowledge", None),
        title, row.get("topic") or "dotaz", url, parts,
        "read_fakta_%s" % (row.get("id") or int(time.time())))
    _log.info("HANS_LOOKUP_FULL_ARTICLE_V1 '%s' — fakta z %d/%d částí, %d vět "
              "(článek %d zn, nepřečteno %d zn)", title, hotovo, len(parts), vet,
              len(full or ""), zbyva)
    return hotovo


def _commit_to_memory(config: dict, row: dict, curiosity) -> bool:
    """Zápis do paměti AŽ PO ověření — přes `curiosity._store` (deník + entity
    + RAG + synthesis hook), ať je nález nerozeznatelný od běžného čtení."""
    if curiosity is None:
        _log.debug("instant_lookup: curiosity není napojená → nelze zapsat")
        return False
    try:
        from scripts.web_reader import ReadResult
        res = ReadResult(
            source="wikipedia",
            title=row.get("resolved_title") or row.get("topic") or "",
            url=row.get("url") or "",
            raw_text=row.get("raw_text") or "",
            summary=row.get("summary") or "",
            topic=row.get("topic") or "dotaz",
            pending=False,
        )
        # HANS_LOOKUP_FULL_ARTICLE_V1 — fakta z celého článku PŘED shrnutím,
        # ať je shrnutí nejnovější záznam pod tím titulem. Selhání nevadí.
        try:
            _read_full_article(config, row, curiosity)
        except Exception as _fe:
            _log.warning("HANS_LOOKUP_FULL_ARTICLE_V1 selhalo: %s", _fe)
        curiosity._store(res)
        return True
    except Exception as e:
        _log.warning("instant_lookup: commit do paměti selhal: %s", e)
        return False


# ── údržba ───────────────────────────────────────────────────────────────────

def purge_old(db_path: str, keep_days: int = 30) -> int:
    """Uklidí staré vyřízené nálezy (pending se NEMAŽE — čeká na mozek)."""
    ensure_schema(db_path)
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        cur = conn.execute(
            "DELETE FROM unverified_findings WHERE status IN "
            "('verified','rejected') AND COALESCE(verified_ts,0) < ? "
            "AND COALESCE(announced,0)=1",
            (time.time() - keep_days * 86400,))
        conn.commit()
        return cur.rowcount or 0
    except Exception:
        return 0
    finally:
        conn.close()
