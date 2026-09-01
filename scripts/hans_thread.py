"""HANS_THREAD_V1 (6.8.) — vlákno rozhovoru pro ROZHODOVACÍ vrstvu.

PROČ: chatová cesta se rozhoduje BEZSTAVOVĚ. `parse_command` i detektory
v `_build_grounding` dostávají holou poslední větu (`str(_text)`), zatímco
generační vrstva (LLM) historii má. Následek doložen 5.8. 19:17–19:23:

    19:17  „jak slo malovani?“          → „Koláč a já jsme se bavili o Bratři“
    19:19  „v jakem kontextu jste se     → sumář rozhovoru s UŽIVATELEM
            bavili o Bratři“               (ne s Koláčem)
    19:23  „myslel jsem rozhovor         → TÝŽ sumář, jen o výměnu delší
            s Kolacem“                     (korekce neměla ŽÁDNÝ účinek)

Tenhle modul dodává rozhodovací vrstvě tři věci a NIC negeneruje:
  A1 `resolve_reference` — věta bez vlastního předmětu si ho doplní
     z poslední Hansovy repliky („myslel jsem rozhovor s Kolacem“ nese
     předmět „Bratři“ z repliky před ní).
  A3 `is_correction`     — uživatel opravuje, ne ptá se znovu.
  A2 `should_suppress`   — tatáž cesta podruhé PO korekci se nepustí;
     šablona ustoupí a odpovídá LLM, který kontext má.
  A4 `third_party_scope` — dotaz míří na rozhovor s TŘETÍ stranou (Koláč);
     dokud recall umí jen `human_chat`, je poctivější to přiznat než
     vrátit sumář jiného rozhovoru.

ZÁMĚRNĚ ŽÁDNÝ LLM a žádná věta do system promptu — tohle je rozhodovací
vrstva, ne prompt (past `[[prompt-debt-tool-calling]]`). Vše deterministické,
takže to funguje i s vypnutým PC.
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Optional

_log = logging.getLogger(__name__)

# Jak dlouho zpět se ještě považuje replika za „to, na co navazuje“.
_THREAD_TTL_S = 600.0
# Kolik posledních zpráv z historie bereme v úvahu.
_TURNS = 6


def _fold(s: str) -> str:
    """Bez diakritiky + malá písmena. Uživatel píše «malovani» i «malování»."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# ── funkční slova ────────────────────────────────────────────────────────
# Pozor: tohle NENÍ seznam témat, ale slov, která sama o sobě NEJSOU předmět.
# Když po jejich odstranění nezbude nic, věta se o předmět opírá jinde =
# navazuje na předchozí repliku.
_FUNCTION_WORDS = {
    # zájmena a ukazovací
    "ja", "ty", "on", "ona", "ono", "my", "vy", "oni", "me", "mne", "mi",
    "te", "tebe", "ti", "se", "si", "sebe", "ho", "jeho", "mu", "ji", "jeji",
    "je", "jim", "to", "ta", "ten", "tu", "toho", "tom", "tomu", "tim", "ta",
    "ty", "tyto", "tento", "tato", "toto", "tim", "tech", "temi", "onen",
    "sve", "svuj", "svoji", "muj", "moje", "tvuj", "tvoje", "nas", "vas",
    "nej", "nim", "nem", "ne", "nic", "neco", "vse", "vsechno",
    # tázací / navazovací
    "co", "kdo", "kde", "kdy", "jak", "jaky", "jaka", "jake", "jakem",
    "jakym", "ktery", "ktera", "ktere", "proc", "kam", "odkud", "cim",
    "kontextu", "kontext", "souvislosti", "smyslu",
    # slovesa běžná v navazování
    "je", "jsi", "jsem", "jsme", "jste", "jsou", "byl", "byla", "bylo",
    "byli", "byly", "bych", "bys", "by", "bude", "budes", "budu", "mel",
    "mela", "mas", "mam", "mate", "maji", "chci", "chtel", "chtela",
    "myslel", "myslela", "myslim", "rikal", "rikala", "ptal", "ptala",
    "bavili", "bavil", "bavila", "mluvili", "mluvil", "povidali", "resili",
    "delal", "delala", "slo", "sla", "sel", "jde", "dopadlo", "vyslo",
    "zkus", "zkusim", "znova", "znovu", "jeste", "opet", "prosim", "dik",
    "diky", "ok", "oki", "dobre", "jo", "ano", "no", "tak", "takze",
    # předložky a spojky
    "a", "i", "o", "u", "v", "ve", "s", "se", "z", "ze", "k", "ke", "na",
    "do", "od", "po", "pri", "pro", "za", "nad", "pod", "pred", "mezi",
    "ale", "nebo", "ci", "aby", "ze", "kdyz", "protoze", "jestli", "li",
    "uz", "jen", "jenom", "taky", "take", "asi", "spis", "vlastne", "tam",
    "tady", "ted", "dnes", "vcera", "potom", "pak", "hlavne",
    # oslovení
    "pane", "pani", "prosimte", "hansi", "hans",
}

# Slova, která signalizují, že věta na něco NAVAZUJE, i když sama předmět má.
_ANAPHORA_HINTS = (
    "myslel jsem", "mysleu jsem", "myslela jsem", "ptal jsem se",
    "mel jsem na mysli", "narazel jsem", "chtel jsem", "chtela jsem",
    "to znova", "to znovu", "zkus to", "jeste jednou", "znovu to",
)

# HANS_THREAD_ANAPHORA_WORDBOUND_V1 (26.8.) — nápovědy se hledaly jako HOLÝ
# PODŘETĚZEC, takže „pře<myslel jsem>" spustilo rozřešení odkazu ve větě, která
# žádný odkaz nemá. Doloženo: „Poslyš, přemýšlel jsem, že bych ti dal na starost
# něco navíc… co všechno vlastně umíš?" → `odkaz rozřešen → Oldu` (Title-case
# záloha vytáhla jméno z předchozí Hansovy repliky). Věta má přitom vlastní
# předmět víc než dost. Hledá se proto na HRANICÍCH SLOV.
# ⚠️ Podmínku `not has_own_subject(...) OR nápověda` NELZE přehodit na AND —
# „namaluj ho" vlastní předmět MÁ („namaluj") a rozřešit se musí; právě kvůli
# tomu je tam `or`.
_ANAPHORA_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(h) for h in _ANAPHORA_HINTS))


def _tokens(text: str) -> list:
    return [t for t in re.split(r"[^a-z0-9]+", _fold(text)) if t]


def has_own_subject(text: str) -> bool:
    """Nese věta vlastní obsahový předmět, nebo se opírá o předchozí repliku?

    Vzor převzatý z `hans_art._is_instruction_only` (osvědčený 20.7.):
    odstraň funkční slova a když nic nezbude, věta sama o sobě nestačí.
    """
    return any(len(t) >= 3 and t not in _FUNCTION_WORDS for t in _tokens(text))


def is_correction(text: str) -> bool:
    """Uživatel OPRAVUJE předchozí odpověď (ne ptá se na nové téma).

    Doložený případ: „myslel jsem rozhovor s Kolacem“ po sumáři o jiném
    rozhovoru. Bez tohohle se cesta vybere znovu od nuly a vrátí totéž.
    """
    f = _fold(text).strip()
    if not f:
        return False
    if re.match(r"^(ne|nikoliv|nene)\b[\s,.]", f) or f in ("ne", "nikoliv"):
        return True
    pats = (
        r"\bmysl[ei]l\w*\s+jsem\b", r"\bmyslela\s+jsem\b",
        r"\bptal\s+jsem\s+se\s+na\b", r"\bneptal\s+jsem\s+se\b",
        # „měl jsem na mysli“ i lidovější „…jsem na mysli, že…“ (doloženo
        # 5.8. 10:53: „ze jsi v jednom kole jsem na mysli, ze mas praci“).
        r"\b(mel\s+)?jsem\s+na\s+mysli\b", r"\bna\s+mysli\s+jsem\b",
        # HANS_CORRECTION_NO_AUX_V1 (21.8.) — hovorová čeština pomocné sloveso
        # vypouští: „JÁ MĚL na mysli díl ze seriálu" (doloženo 20.8.). Vzor
        # výš čekal „jsem na mysli", takže korekce nebyla poznána a Hans
        # nabídl tentýž film podruhé. Změřeno na 1171 větách: přibude 1 —
        # právě ta doložená.
        r"\b(ja\s+)?m[ei]l\w*\s+na\s+mysli\b",
        r"\bnarazel\s+jsem\b",
        r"\bto\s+neby(l|la|lo)\b", r"\bto\s+neni\b", r"\bnemyslel\s+jsem\b",
        r"\bspatne\s+(jsi\s+)?(me\s+)?(pochopil|rozumel)\b",
        r"\bnerozumel\s+jsi\b", r"\bpta(m|l)\s+jsem\s+se\s+jinak\b",
        r"\bne,?\s+(myslel|chtel|ptal)\b",
    )
    return any(re.search(p, f) for p in pats)


# ── předmět z předchozí repliky ──────────────────────────────────────────
# Hans uvozuje názvy „takhle“ (deníkové i šablonové odpovědi), takže uvozovky
# jsou nejspolehlivější signál. Title-case je až záloha.
_QUOTED = re.compile(r"[„\"»']([^„\"»'\n]{2,60})[\"“«']")
_TITLED = re.compile(r"\b([A-ZÁ-Ž][a-zá-ž]{2,}(?:\s+[A-ZÁ-Ž][a-zá-ž]{2,})*)")


# Deterministický VÝPIS (/stav, /studium, /zdravi…) není konverzační replika
# a nesmí z něj plynout „téma“. Doloženo při testu 6.8.: z výpisu `/stav`
# vytáhl Title-case fallback „Teplota“ a nesl ji detektorům jako předmět.
_LIST_MARKS = re.compile(r"[✓✅⚠️❌•→│┃]|^\s*[-–—]\s|^\s*\d+[.)]\s", re.M)


# Uvozovky v NÁVODNÉ patičce nejsou téma. Doloženo živě 6.8.: šablona
# `/rozhovory` končí větou „řekněte třeba „připomeň rozhovor o …““ →
# `extract_subject` z ní vytáhl „připomeň rozhovor o …“ jako téma hovoru.
_INSTRUCTION_LEAD = re.compile(
    r"(t[řr]eba|nap[řr][íi]klad|sta[čc][íi]\s+[řr][íi]ct|[řr]ekn[ěe]te|"
    r"zkuste|nap[ií][šs]te|ru[čc]n[ěe])[\s:,]*$", re.I)
_INSTRUCTION_VERB = re.compile(
    r"^(p[řr]ipome[ňn]|[řr]ekni|uka[žz]|zkus|napi[šs]|pus[ťt]|nastuduj|"
    r"zjisti|namaluj|vypi[šs])", re.I)


def _is_instructional(cand: str, reply: str, pos: int) -> bool:
    """Je uvozený text NÁVOD („řekněte třeba „…““), ne téma?"""
    if "…" in cand or "..." in cand:
        return True
    if _INSTRUCTION_VERB.match(_fold(cand)):
        return True
    return bool(_INSTRUCTION_LEAD.search(reply[max(0, pos - 40):pos]))


def _looks_conversational(reply: str) -> bool:
    """Je to věta, nebo strukturovaný výpis?"""
    if not reply:
        return False
    if reply.count("\n") > 1:
        return False
    return not _LIST_MARKS.search(reply)


def extract_subject(reply: str) -> str:
    """Předmět, o kterém Hans naposledy mluvil (pro rozřešení odkazu).

    Uvozovky jsou spolehlivé (Hans názvy uvozuje) a berou se i z výpisů —
    „Studuji: „hrady a historická architektura““ JE legitimní téma.
    Title-case je jen záloha a POUZE u konverzačních replik: falešný předmět
    je horší než žádný, protože se nese do rozhodovací vrstvy.
    """
    if not reply:
        return ""
    for m in _QUOTED.finditer(reply):
        cand = m.group(1).strip()
        if _is_instructional(cand, reply, m.start()):
            continue
        return cand
    if not _looks_conversational(reply):
        return ""
    # Title-case, ale ne první slovo věty (to je velké z gramatiky).
    for m in _TITLED.finditer(reply):
        if m.start() == 0:
            continue
        cand = m.group(1).strip()
        if _fold(cand) not in _FUNCTION_WORDS and len(cand) >= 4:
            return cand
    return ""


def recent_turns(handler, name: Optional[str], channel: Optional[str] = None,
                 limit: int = _TURNS) -> list:
    """Posledních N (role, text) z ConversationStore. Kanálově scoped —
    stejný princip jako `hans_agent._context` (proti cross-channel leaku,
    [[context-leak-shared-state-first]])."""
    try:
        store = getattr(handler, "conv_store", None)
        if store is None or not name:
            return []
        if channel is None:
            try:
                from scripts.openwebui_direct_handler import get_current_channel
                channel = get_current_channel()
            except Exception:
                channel = None
        hist = ((store.get_history_scoped(name, channel) if channel
                 else store.get_history(name)) or [])
        out = []
        for m in hist[-limit:]:
            c = (m.get("content") or "").strip()
            if c:
                out.append((m.get("role") or "", c))
        return out
    except Exception as e:
        _log.debug("recent_turns selhal: %s", e)
        return []


# HANS_THREAD_NO_ABSTAIN_SUBJ_V1 (21.8.) — Z „NEVÍM" SE NEDĚLÁ TÉMA HOVORU.
# Doloženo v simulovaném rozhovoru o Pride and Prejudice: po dvou abstinencích
# vzalo vlákno jako předmět slovo „Raději" (z věty „RADĚJI přiznám, že si tím
# nejsem jistý") a další dotaz šel do detektorů jako „a kdy to vyslo?
# (k tématu: Raději)". Jedno „nevím" tak otrávilo zbytek hovoru — abstinenční
# kaskáda. Předmět se proto bere z poslední VĚCNÉ repliky; odříkací hlášky se
# přeskakují, protože o tématu nenesou nic.
# HANS_ANCHOR_LOOKUP_ON_ADMIT_V2 (22.8.) — VZORY, ne podřetězce.
# První verze měla seznam celých obratů („nemam spolehlivy zaznam") a živý
# test ji vyvrátil hned první odpovědí: čeština mezi sloveso a předmět vloží
# cokoli („nemám K TOMU žádný záznam", „nemám V PAMĚTI spolehlivé informace")
# a doslovný podřetězec mine. Proto se povoluje pár slov mezi „nemám" a tím,
# co chybí. Ověřeno na 1 687 skutečných replikách a na obou doložených
# odpovědích o Scottu Eastwoodovi.
# ⚠️ Holé „nevím" je tu ZÚŽENÉ: „nevím, zda jste slyšel o filmu X" odříkání
# není (skutečná replika z hovoru s Koláčem) — proto negativní pohled dopředu.
_ABSTAIN_RE = re.compile(
    r"nem[aá]m(?:\s+\w+){0,4}\s+"
    r"(zaznam\w*|informac\w*|zapis\w*|znalost\w*|podklad\w*|doklad\w*"
    r"|udaj\w*|nic\b|pristup\b)"
    r"|nemohu(?:\s+\w+){0,3}\s*(poskytnout|poskytovat|potvrdit|rict|uvest)"
    r"|nenachazim|nenasel jsem|nedohledal jsem|netusim"
    r"|nedokazu(?:\s+\w+){0,3}\s*(rict|potvrdit|posoudit|urcit)"
    r"|neni mi (to )?znamo|nerad bych si domyslel|radeji priznam"
    r"|nejsem si (tim |timto )?jist\w*|zatim jsem si nic nezapsal"
    r"|bohuzel (nevim|nemam)|(?<!\w)nevim(?!\s*,?\s*(zda|jestli|co |jak ))")

# Slib dohledání („zkusím si to ověřit") — sám o sobě netvrdí nic o světě,
# a je to PRÁVĚ ten slib, který má `HANS_ANCHOR_LOOKUP_ON_ADMIT_V1` splnit.
_SLIB_RE = re.compile(
    r"overi\w*|overim|dohledam|zjistim|podivam se|pripomen\w*"
    r"|dam vedet|ozvu se|upresn\w*|sdilet zdroj")

# Zdvořilost a nabídka další služby — netvrdí fakt. JEDNO MÍSTO pro celý
# repozitář (`kolac_exam` si sem chodí pro totéž, ať nevzniknou dvě pravdy).
_ZDVORILOST = re.compile(
    r"pokud (byste|si p[řr]ejete|chcete)|p[řr]ejete[- ]li|r[áa]d (v[áa]m|to)|"
    r"k dispozici|m[ůu][žz]u (v[áa]m )?(nab[íi]dnout|dohledat|zjistit)|"
    r"budu[- ]li|dovol[íi]te[- ]li|s dovolen[íi]m", re.IGNORECASE)

# Věta = konec interpunkce, nový řádek NEBO odrážka. Bez odrážek se dlouhý
# výpis schopností („Co dokážu, pane: • …") tváří jako JEDNA věta a projde
# jako čisté odříkání — změřeno.
_VETA_RE = re.compile(r"(?<=[.!?])\s+|\n+|\s+[•\-–]\s+")


def je_odrikaci(text: str) -> bool:
    """Obsahuje odpověď přiznání neznalosti? (Věcná odpověď s poctivým
    dovětkem sem taky spadá — na „je to CELÉ jen odříkání" je
    `je_ciste_odrikani`.)"""
    return bool(_ABSTAIN_RE.search(_fold(text or "")))


def je_ciste_odrikani(text: str) -> bool:
    """Je CELÁ odpověď jen přiznáním neznalosti (+ slib a zdvořilost)?

    Spouštěč dohledání (`HANS_ANCHOR_LOOKUP_ON_ADMIT_V1`) musí být úzký:
    kdyby stačilo „obsahuje odříkací obrat", přepsala by se věcná odpověď,
    která si jen poctivě ohraničí, kam sahá („o dalších povídkách nemám
    spolehlivé záznamy") — táž past, na kterou narazilo hodnocení zkoušek.
    Změřeno na 1 678 skutečných replikách: 60 zásahů, samé abstinence.
    """
    vety = [v.strip() for v in _VETA_RE.split((text or "").strip()) if v.strip()]
    if not vety or len(vety) > 8:
        return False       # dlouhý výklad není odříkání
    priznani = vecne = 0
    for v in vety:
        f = _fold(v)
        if _ABSTAIN_RE.search(f):
            priznani += 1
        elif _SLIB_RE.search(f) or _ZDVORILOST.search(v):
            continue
        else:
            vecne += 1
    return priznani > 0 and vecne == 0


# zpětná kompatibilita (volá se z regresní sady i zevnitř modulu)
_je_odrikaci = je_odrikaci


def last_assistant_text(turns: list) -> str:
    zaloha = ""
    for role, content in reversed(turns or []):
        if role != "assistant":
            continue
        if not _je_odrikaci(content):
            return content
        zaloha = zaloha or content
    # samé odříkání → radši nic (prázdný předmět = detektory dostanou
    # holou větu, což je pořád lepší než bogus téma)
    return ""


def resolve_reference(text: str, turns: list) -> tuple:
    """A1 — doplň předmět z předchozí Hansovy repliky, když ho věta nemá.

    Vrací `(text_pro_detektory, doplneny_predmet)`. Původní text se NEMĚNÍ
    pro generaci — persona dál slyší, co uživatel doopravdy napsal (stejný
    princip jako `HANS_QUERY_REWRITER_F1_V1`).
    """
    if not text:
        return text, ""
    f = _fold(text)
    needs = (not has_own_subject(text)
             or bool(_ANAPHORA_RE.search(f)))   # HANS_THREAD_ANAPHORA_WORDBOUND_V1
    if not needs:
        return text, ""
    subj = extract_subject(last_assistant_text(turns))
    if not subj:
        return text, ""
    if _fold(subj) in f:      # předmět už ve větě je → nic nedoplňuj
        return text, ""
    return ("%s (k tématu: %s)" % (text.strip(), subj)), subj


# ── A4: rozhovor s TŘETÍ stranou ─────────────────────────────────────────

def third_party_scope(text: str, config: Optional[dict] = None,
                      turns: Optional[list] = None) -> str:
    """Míří dotaz na rozhovor s NĚKÝM JINÝM než s tazatelem?

    Recall dnes umí jen `human_chat` (rozhovory uživatel↔Hans); Koláčovy
    dialogy jsou v `teddy_dialog` a `hans_recall._DIARY_NOISE` je dokonce
    vyřazuje jako šum. Vracet místo nich sumář jiného rozhovoru je horší
    než přiznat, že to zatím neumím. Odpadne s vrstvou B (FTS nad všemi
    rozhovory) — pak se tenhle scope použije jako FILTR, ne jako brzda.
    """
    try:
        from scripts.hans_kolac import kolac_name
        kn = kolac_name(config or {})
    except Exception:
        kn = "Koláč"
    f = _fold(text)
    kf = _fold(kn)
    # „s Koláčem“, „s Kolacem“, „rozhovor s ...“ — stačí kmen (skloňování)
    stem = kf[:max(4, len(kf) - 1)]
    if stem and stem in f:
        return kn
    # „jste se bavili“ = Hans + někdo další, ne tazatel. Mezi „se“ a slovesem
    # bývá vsuvka („jste se O TOM bavili“) — doloženo živě 6.8., kdy původní
    # těsný vzor minul a dotaz spadl na sumář rozhovoru s tazatelem.
    if re.search(r"\bjste\s+se\s+(?:\w+\s+){0,3}"
                 r"(bavili|bavil|mluvili|povidali|meli|domlouvali|resili)\b", f):
        # Kdo je ta třetí strana, řekne VLÁKNO: když o ní Hans mluvil
        # v předchozí replice, je to ona. Bez toho zbývalo jen „?“, které
        # volající stejně zahodil → chyba se projevila jako by scope nebyl.
        if turns:
            prev = _fold(last_assistant_text(turns))
            if stem and stem in prev:
                return kn
        return "?"
    # HANS_THREAD_TP_FOLLOWUP_V1 — NAVAZUJÍCÍ věta zdědí třetí stranu z vlákna.
    # Doloženo živě 1. 9.: tah „bavis se s kolacem?" → Hans odpověděl o Koláčovi,
    # a hned další „o cem naposledy" spadlo na rozhovory s TAZATELEM. Vzor
    # „jste se bavili" nesepnul (věta ho nemá) a jméno v ní taky není, takže
    # scope vyšel prázdný, ač kontext byl jednoznačný.
    # Protahuje se pohled do vlákna, který tahle funkce UŽ POUŽÍVÁ o pár řádků
    # výš — jen byl dostupný jedině uvnitř větve „jste se bavili".
    # ⚠️ „jsme/náš/my" MUSÍ zůstat u tazatele: „o cem jsme se bavili?" je taky
    # navazující věta a bez tohohle vyloučení by ji vlákno přepsalo na Koláče
    # (změřeno — je to jediná regrese, kterou zásah hrozil; kryje ji i test
    # „náš rozhovor → ne" v tests/test_hans_thread.py).
    if turns and stem and not re.search(r"\b(jsme|nas|nase|nasi|nasem|my)\b", f):
        try:
            from scripts.chat_commands import _je_navazujici_dotaz as _nav
        except Exception:
            _nav = None
        if _nav is not None and _nav(text or ""):
            if stem in _fold(last_assistant_text(turns)):
                return kn
    return ""


# ── A2: anti-repeat brzda ────────────────────────────────────────────────
# Stav je krátkodobý a procesní (restart Hanse ho vyresetuje — to je správně,
# vlákno rozhovoru restart nepřežívá). Klíč (osoba, kanál).
_LAST: dict = {}


def note_outcome(name: Optional[str], channel: Optional[str], kind: str,
                 text: str = "") -> None:
    """Zapiš, KTERÁ cesta právě odpověděla (aby šla příště potlačit)."""
    if not kind:
        return
    _LAST[(name or "", channel or "")] = {
        "kind": kind, "text": text or "", "ts": time.time()}


def should_suppress(name: Optional[str], channel: Optional[str], kind: str,
                    text: str) -> bool:
    """A2 — potlač TUTÉŽ cestu, když ji uživatel právě OPRAVIL.

    ÚZKÉ SCHVÁLNĚ. Brzda sepne jen při korekci, NE při doslovném opakování:
    „namaluj kočku“ 5× za sebou je legitimní záměr (doloženo 5.8. 08:34–08:50)
    a zablokovat ho by byl horší bug než ten, co opravujeme.
    """
    if not kind:
        return False
    prev = _LAST.get((name or "", channel or ""))
    if not prev or prev.get("kind") != kind:
        return False
    if (time.time() - float(prev.get("ts") or 0)) > _THREAD_TTL_S:
        return False
    if not is_correction(text):
        return False
    _log.info("HANS_THREAD_V1: korekce po '%s' → cesta potlačena, "
              "odpoví model s kontextem", kind)
    return True


def reset(name: Optional[str] = None, channel: Optional[str] = None) -> None:
    """Zapomeň vlákno (test / `zapomeň historii`)."""
    if name is None:
        _LAST.clear()
    else:
        _LAST.pop((name or "", channel or ""), None)
