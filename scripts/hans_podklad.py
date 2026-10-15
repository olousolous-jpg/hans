"""HANS_PODKLAD_V1 (15. 10.) — SPOLEČNÝ KROK PODKLADU: předmět → posbírat → vybrat.

Proč: konec faktické cesty (`handler_grounding._gr_rag` od přepisu F1 dál
a vkládání zpráv v `_sc_podklad`) je kaskáda, kde vyhraje první cesta, která
něco najde, a každá si sama určuje, podle čeho hledá. Téma k dohledání
(`_dohledej_kotvu`) se určuje zase jinde a jinak. Doloženo /tazatel 14. 10.:
kotva „Kolumbus“ → entita „Kolumbo“ (podmořská sopka), „William Shakespeare“
→ „K.Will“, „Alexander Graham Bell“ → „Alexandrie“, téma „Berouna“
z rekapitulace před otázkou, zprávy o seveřanech místo otázky na Kolumba.

Tady se předmět určí JEDNOU a k němu se posbírá vše:
- předmět z otázkové části (věty s „?“), holá věta před přepisem F1, když
  dává silný předmět; druhy titul s fakty · entita · slabá entita · kotva;
- entita je silná jen tehdy, když každé slovo jejího jména stojí ve větě jako
  PÁDOVÝ TVAR (ne jen stejný začátek), pokrývá některou kotvu, není oslovení
  a projde `_entita_je_predmet`;
- u silného předmětu: fakta podle titulu, entita, zápisky hledané podle
  předmětu, zprávy jen s předmětem v titulku; kotva → dnešní kaskáda, jen
  zprávy bez kotvy v titulku ven; bez předmětu → dnešní kaskáda.
📏 Prototyp `data/mereni/podklad_15_10/prototyp.py` na 432 větách (backlog
`PODKLAD_PROTOTYP_15_10`): ~360 beze změny, 18 nově s oporou, 30 s doplněnými
zápisky, 21 jiných témat dohledání (Kolumbo, K.Will, Alexandrie, Beroun opraveny).

⚠️ ZATÍM JEN STÍN (`PODKLAD_STIN_V1`): `stin()` zapíše do logu, co by krok
vybral, vedle toho, co se skutečně stalo. Odpověď se NEMĚNÍ. Vypínač
`grounding.podklad_stin` (výchozí zapnuto).
"""
import logging
import re
import threading

from scripts.hans_convindex import kotva_tematu, _fold, _WORD

_log = logging.getLogger(__name__)

# ── pádové tvary ────────────────────────────────────────────────────────
_KONC = ("", "a", "e", "u", "y", "i", "o", "ou", "em", "ovi", "ove", "ech", "ich",
         "ach", "am", "um", "ami", "emi", "ym", "im", "ymi", "imi", "eho", "emu",
         "iho", "imu", "ej", "uv", "ova", "ovo", "ovy", "es", "ovu")
_PAL = {"k": "c", "h": "z", "g": "z", "r": "r"}
_ADJ = re.compile(r"(?:ick|ck|sk|k|n|ov)(?:y|a|e|eho|emu|em|ych|ym|ou|i|iho|imu|im|ich|ymi|imi)$")
# druhové slovo před názvem („film Útěk…“, „hrad Kost“) se do kotvy nepočítá
_DRUHOVA = ("film", "kniha", "hrad", "serial", "pisen", "zamek", "opera", "obraz", "dil", "roman")


def _kmeny(lemma: str) -> set:
    """Možné kmeny slova v 1. pádě (bez diakritiky)."""
    k = {lemma}
    if lemma[-1:] in "aeiouy":
        k.add(lemma[:-1])
    if lemma[-2:] in ("us", "es", "os"):            # Kolumbus → Kolumba
        k.add(lemma[:-2])
    if re.search(r"[^aeiouy][eo][klcn]$", lemma):  # Karel → Karla
        k.add(lemma[:-2] + lemma[-1])
    for s in list(k):
        if s and s[-1] in _PAL:
            k.add(s[:-1] + _PAL[s[-1]])           # gotika → gotice
    return {s for s in k if len(s) >= 2}


def je_tvar(slovo: str, lemma: str) -> bool:
    """Je `slovo` (z věty) pádový tvar `lemma` (z názvu)? Obojí bez diakritiky.
    Nestačí stejný začátek: „kolumbus“ není tvar „kolumbo“, „william“ není
    „will“, „dal“ není „dalek“. Přídavné jméno od názvu ano („gotické“)."""
    if slovo == lemma:
        return True
    if len(lemma) < 3:
        return False
    for s in _kmeny(lemma):
        if len(s) < 3 or not slovo.startswith(s):
            continue
        if slovo[len(s):] in _KONC:
            return True
        if len(s) >= 4 and _ADJ.fullmatch(slovo[len(s):]):
            return True
    return False


def _slova(t) -> list:
    return [_fold(w) for w in _WORD.findall(str(t or ""))]


def nazev_slova(nazev) -> list:
    """Slova názvu bez rozlišovací závorky („(film, 2017)“)."""
    w = _slova(re.sub(r"\s*\([^)]*\)", " ", str(nazev or "")))
    return [x for x in w if len(x) >= 3] or w


def nazev_ve_vete(nazev, veta) -> bool:
    """Každé slovo názvu stojí ve větě jako jeho pádový tvar."""
    nw, vw = nazev_slova(nazev), _slova(veta)
    return bool(nw) and all(any(je_tvar(v, n) for v in vw) for n in nw)


def mluvi_o(text, nazev) -> bool:
    """Mluví kus podkladu o předmětu? Všechna slova názvu (tvarem) v textu."""
    if not text or not nazev:
        return False
    tw = set(_slova(str(text)[:4000]))
    return all(any(je_tvar(t, n) for t in tw) for n in nazev_slova(nazev))


# ── předmět ─────────────────────────────────────────────────────────────
def otazkova_cast(text) -> str:
    """Věty s otazníkem, jinak celý text. Rekapitulace před otázkou předmět
    nenese („…východně od Berouna. Znáte nějakou osobnost?“). Iniciála
    s tečkou („F. L. Věk“) větu nekončí."""
    t = str(text or "")
    kusy = [k.strip() for k in re.split(r"(?<![ .][A-ZÁ-Ž]\.)(?<=[.!?])\s+", t) if k.strip()]
    q = [k for k in kusy if "?" in k]
    return " ".join(q) if q and len(kusy) > 1 else t


def kotvy_vsechny(text, vynech=()) -> list:
    """Všechny skupiny kotev (`kotva_tematu` vrací jen první)."""
    out, t = [], str(text or "")
    for _ in range(4):
        k = kotva_tematu(t, vynech=vynech)
        if not k:
            break
        out.append(k)
        posl = k.split()[-1]
        i = t.find(posl)
        if i < 0:
            break
        t = "a " + t[i + len(posl):]
    return out


def osloveni(jm, text) -> bool:
    """Jméno stojí jako oslovení („…, Petře?“, „Petře, …“)."""
    text = str(text or "")
    for w in nazev_slova(jm):
        for m in _WORD.finditer(text):
            if je_tvar(_fold(m.group(0)), w):
                pred, za = text[:m.start()].rstrip(), text[m.end():].lstrip()
                if ((pred.endswith(",") and (not za or za[0] in "?!."))
                        or (not pred and za.startswith(","))):
                    return True
    return False


def kryje(jmeno, kotva) -> bool:
    """Pokrývá jméno entity kotvu? Druhová slova kotvy (film, hrad…) se
    nepočítají; víceslovná entita smí stát uvnitř delší kotvy
    („Sherlocka Holmesa Modrá“). „Alexandrie“ nepokrývá „Alexander Graham
    Bell“, „Marťan“ nepokrývá „Karel Martel“."""
    ew = nazev_slova(jmeno)
    kw = [x for x in nazev_slova(kotva)
          if not any(je_tvar(x, d) for d in _DRUHOVA)] or nazev_slova(kotva)
    if not ew or not kw:
        return False
    kryte = [any(je_tvar(x, e) for e in ew) for x in kw]
    if all(kryte):
        return True
    # tříslovné jméno s jedním zkomoleným slovem z přepisu („Erica Roberta
    # Sorgeho“): stačí, sedí-li příjmení a zbytek až na jedno slovo
    if len(kw) >= 3 and kryte[-1] and sum(kryte) >= len(kw) - 1:
        return True
    # víceslovné jméno: sedí příjmení a ostatní slova aspoň začátkem na
    # 5 písmen („Hermanu Göringovi“ × „Hermann Göring“)
    if (len(kw) >= 2 and kryte[-1]
            and all(k or any(len(x) >= 5 and len(e) >= 5 and x[:5] == e[:5] for e in ew)
                    for x, k in zip(kw, kryte))):
        return True
    return len(ew) >= 2 and all(any(je_tvar(x, e) for x in kw) for e in ew)


def _entita_predmet(h, ent, text) -> bool:
    """Produkční HANS_ENTITY_SUBJECT_SUPPORT_V1; víceslovné jméno vždy projde
    (iniciály „F. L.“ produkční pravidlo nevidí, bere slova od 2 znaků)."""
    if len(str(ent.get("name") or "").split()) >= 2:
        return True
    try:
        return h._entita_je_predmet(ent, text)
    except Exception:
        return True


def _db(h) -> str:
    return ((h.config.get("hans_idle", {}) or {}).get("diary_db")
            or h.config.get("diary_db") or "data/hans_diary.db")


def predmet_z(h, text, vynech=()):
    """(druh, název, entita) z jedné věty; druh titul | entita | entita_slaba | kotva | None."""
    if not text:
        return None, None, None
    from scripts.hans_recall_znalost import fakta_tituly_ve_vete
    text = otazkova_cast(text)
    tit = fakta_tituly_ve_vete(_db(h), text)
    kotvy = [k for k in kotvy_vsechny(text, vynech) if not osloveni(k, text)]
    ent = None
    try:
        es = h._entity_store()
        if es is not None:
            ent = es.resolve(h._bez_tazatele(text))
            if ent and h._entita_je_tazatel(ent):
                ent = None
    except Exception:
        ent = None
    jm = str((ent or {}).get("name") or "")
    ent_ok = bool(ent and jm and nazev_ve_vete(jm, text) and _entita_predmet(h, ent, text)
                  and not osloveni(jm, text))
    kotva = kotvy[0] if kotvy else None
    if ent_ok and kotvy:
        ew = nazev_slova(jm)

        kryta = [k for k in kotvy if kryje(jm, k)]
        if kryta:
            kotva = kryta[0]
            if len(ew) == 1 and len(kotvy) > len(kryta):
                # jedna z víc kotev („Česko proti Slovensku“) → předmět nejistý
                return "entita_slaba", jm, ent
        else:
            ent_ok = False
    if tit:
        return "titul", tit[0], (ent if ent_ok else None)
    if ent_ok:
        # jednoslovné jméno bez kotvy (velkého písmene) je obecné slovo
        # („cesko“, „designu“) → téma ano, podklad kaskádou
        if not kotvy and len(nazev_slova(jm)) == 1:
            return "entita_slaba", jm, ent
        return "entita", jm, ent
    if kotva:
        return "kotva", kotva, None
    return None, None, None


def predmet(h, veta, prepis, vynech=()):
    """(druh, název, entita, odkud). Holá věta má přednost, když dává silný
    předmět; jinak přepis F1 (nese předmět z vlákna); kotva jako poslední."""
    hl = predmet_z(h, veta, vynech)
    if hl[0] in ("titul", "entita"):
        return hl + ("holá",)
    p = (predmet_z(h, prepis, vynech) if prepis and prepis != veta
         else (None, None, None))
    if p[0] in ("titul", "entita", "kotva", "entita_slaba"):
        return p + ("přepis",)
    if hl[0] in ("kotva", "entita_slaba"):
        return hl + ("holá",)
    return None, None, None, None


# ── sběr a výběr ────────────────────────────────────────────────────────
def posbirej(h, druh, nazev, ent, dotaz, veta, zpravy=""):
    """Kusy podkladu k předmětu; `zpravy` = text, který už sebral produkční
    krok (stín nevolá vektory znovu)."""
    kusy = {}
    try:
        kusy["fakta"] = h._fakta_podle_titulu(
            (nazev + ": " + dotaz) if druh == "titul" else dotaz) or ""
    except Exception:
        kusy["fakta"] = ""
    try:
        kusy["entita"] = h._entity_store().fact_block(ent) if ent else ""
    except Exception:
        kusy["entita"] = ""
    z = ""
    # zápisky podle PŘEDMĚTU: hledání chce všechna slova, „Co vím o Českém
    # ráji?“ nenajde nic, „Český ráj“ ano
    hledej = ([re.sub(r"\s*\([^)]*\)", "", nazev)] if druh in ("titul", "entita") else [])
    for q in hledej + [dotaz, veta]:
        if not q:
            continue
        try:
            z = h._zapisky_k_veci(q) or ""
        except Exception:
            z = ""
        if z:
            break
    kusy["zapisky"] = z
    kusy["zapisky_o_predmetu"] = bool(z) and (not nazev or mluvi_o(z, nazev))
    # entita, kterou by vzala dnešní kaskáda (`_entity_fact` nad přepisem)
    kusy["entita_dnes"] = ""
    try:
        _ed = h._entity_store().resolve(h._bez_tazatele(dotaz))
        if _ed and not h._entita_je_tazatel(_ed):
            kusy["entita_dnes"] = str(_ed.get("name") or "")
    except Exception:
        pass
    kusy["zpravy"] = zpravy or ""
    kusy["zpravy_o_predmetu"] = bool(zpravy) and bool(nazev) and mluvi_o(zpravy, nazev)
    return kusy


def vyber(druh, kusy, nazev=None) -> list:
    """Vybrané zdroje (pořadí = priorita), nebo ['kaskada'] = jako dnes;
    „-x“ = z kaskády vyřadit zdroj x."""
    if druh in ("titul", "entita"):
        v = [k for k in ("fakta", "entita") if kusy.get(k)]
        if kusy.get("zapisky") and kusy.get("zapisky_o_predmetu"):
            v.append("zapisky")
        if kusy.get("zpravy") and kusy.get("zpravy_o_predmetu"):
            v.append("zpravy")
        return v or ["bez_opory"]
    v = ["kaskada"]
    if (druh == "kotva" and nazev and kusy.get("entita_dnes")
            and not kryje(kusy["entita_dnes"], nazev)):
        v.append("-entita")     # Bell → Alexandrie, Karel Martel → Marťan
    if druh in ("kotva", "entita_slaba") and kusy.get("zpravy") and not kusy.get("zpravy_o_predmetu"):
        v.append("-zpravy")
    return v


def tema_dnes(h, veta, prepis, vynech=()):
    """Rekonstrukce tématu, které by dnes vybralo `_dohledej_kotvu`."""
    tema = ((kotva_tematu(prepis, vynech=vynech) if prepis else None)
            or kotva_tematu(veta or "", vynech=vynech))
    try:
        e = h._entity_store().resolve(h._bez_tazatele(prepis or veta or ""))
        jm = str((e or {}).get("name") or "").strip()
        if e and jm and not h._entita_je_tazatel(e) and jm.lower() != str(tema or "").lower():
            tema = jm
    except Exception:
        pass
    return tema


# ── stín ────────────────────────────────────────────────────────────────
_OCAS = {"entita_c1", "zapisky_pred_entitou", "zapisky_fts", "zapisky_fallback",
         "fakta_titul", "rag", "zpravy", "karta_osoby"}
_TICHO = threading.local()
_filtr_hotov = False


class _BezPomocnych(logging.Filter):
    """Během stínu mlčí pomocné funkce handleru (`HANS_FACTS_BY_TITLE_V1`,
    `C1:` …) — jejich řádky by zkreslily odečty zásahů pravidel. Jen ve
    vlákně, které stín právě počítá; ostatní vlákna loguji dál."""
    def filter(self, record):
        return not (getattr(_TICHO, "on", False) and record.name != __name__)


def _umlc_pomocne():
    global _filtr_hotov
    if _filtr_hotov:
        return
    f = _BezPomocnych()
    for hd in logging.getLogger().handlers:
        hd.addFilter(f)
    _filtr_hotov = True


def _vynech(h, name) -> tuple:
    kp = h.config.get("known_persons", {}) or {}
    out = tuple(kp.keys()) + tuple(str(v.get("nom", "")) for v in kp.values()
                                   if isinstance(v, dict))
    try:
        from scripts.hans_persona import persona_name
        out += (str(persona_name(h.config) or ""),)
    except Exception:
        pass
    if name:
        out += (str(name),)
    return tuple(x for x in out if x)


STIN_SOUBOR = "data/mereni/podklad_stin.jsonl"


def _zapis_jsonl(h, ctx, veta, prepis, druh, nazev, odkud, vb, vysl, cesta,
                 kusy, td, tn) -> None:
    """Celý záznam stínu pro odečet (řádek v logu má větu zkrácenou a nemá
    přepis F1). Soubor je v `data/` → mimo git (věty domácnosti)."""
    try:
        import json
        import time
        kdo = str(getattr(ctx, "name", "") or "")
        testy = {str(x).lower() for x in (h.config.get("test_persons") or [])}
        kp = {str(k).lower() for k in (h.config.get("known_persons") or {})}
        trida = ("test" if kdo.lower() in testy
                 else "domacnost" if kdo.lower() in kp else "jiny")
        z = dict(t=time.strftime("%Y-%m-%d %H:%M:%S"), trida=trida, veta=veta,
                 prepis=prepis, druh=druh, nazev=nazev, odkud=odkud, vyber=vb,
                 dnes="%s<%s" % (vysl, cesta),
                 entita_dnes=kusy.get("entita_dnes") or None,
                 kusy={k: bool(v) for k, v in kusy.items() if k != "entita_dnes"},
                 tema_dnes=td, tema_nove=tn)
        with open(STIN_SOUBOR, "a", encoding="utf-8") as f:
            f.write(json.dumps(z, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.debug("PODKLAD_STIN_V1 jsonl: %s", e)


def stin(h, ctx) -> None:
    """PODKLAD_STIN_V1 — zapiš, co by společný krok vybral; NIC NEMĚNÍ."""
    try:
        if not bool((h.config.get("grounding", {}) or {}).get("podklad_stin", True)):
            return
        vysl = getattr(h, "_grounding_outcome", "") or ""
        cesta = getattr(h, "_grounding_cesta", "") or ""
        if not (vysl == "factual_nofacts" or (vysl == "grounded" and cesta in _OCAS)):
            return
        veta = str(getattr(ctx, "_raw_message", "") or ctx.user_message or "")
        prepis = getattr(h, "_f1_query", None) or ""
        zpravy = ""
        if cesta == "zpravy":
            zp = getattr(ctx, "_zpravy", None) or {}
            zpravy = "\n".join(zp.get("radky") or [])
        _umlc_pomocne()
        _kft = getattr(h, "_kfts_tituly", None)
        _TICHO.on = True
        try:
            vyn = _vynech(h, getattr(ctx, "name", None))
            druh, nazev, ent, odkud = predmet(h, veta, prepis, vyn)
            kusy = posbirej(h, druh, nazev, ent, prepis or veta, veta, zpravy)
            vb = vyber(druh, kusy, nazev)
            td = tema_dnes(h, veta, prepis, vyn)
        finally:
            _TICHO.on = False
            h._kfts_tituly = _kft
        tn = nazev if druh else None
        _zapis_jsonl(h, ctx, veta, prepis, druh, nazev, odkud, vb, vysl, cesta,
                     kusy, td, tn)
        _log.info(
            "PODKLAD_STIN_V1: předmět %s %r (%s) · výběr %s · dnes %s ← %s%s · "
            "téma dohledání %r → %r%s · věta %r",
            druh, nazev, odkud, "+".join(vb), vysl, cesta,
            (" (entita %r)" % kusy["entita_dnes"]) if kusy.get("entita_dnes") else "",
            td, tn,
            "" if (td or None) == (tn or None) else " (JINÉ)", veta[:70])
    except Exception as e:
        _TICHO.on = False
        _log.debug("PODKLAD_STIN_V1 selhal: %s", e)
