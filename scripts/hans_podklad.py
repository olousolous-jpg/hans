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


_ZIVOTNE = {"ovi", "ove", "uv", "ova", "ovo", "ovy", "ovu"}


def je_tvar(slovo: str, lemma: str, zivotne: bool = True) -> bool:
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
        kon = slovo[len(s):]
        # latinské jméno na -us/-os/-es (Kolumbus → Kolumba, Kolumbovi):
        # „-o“/„-ou“ jeho tvar není („Kolumbo“ je sopka)
        if (kon in ("o", "ou") and lemma[-2:] in ("us", "es", "os")
                and s == lemma[:-2]):
            continue
        # „-ovi“, „-ův“ … mají jen osoby a postavy („Kolumbovi“ ≠ sopka Kolumbo)
        if not zivotne and kon in _ZIVOTNE:
            continue
        if kon in _KONC:
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


def nazev_ve_vete(nazev, veta, zivotne: bool = True) -> bool:
    """Každé slovo názvu stojí ve větě jako jeho pádový tvar."""
    nw, vw = nazev_slova(nazev), _slova(veta)
    return bool(nw) and all(any(je_tvar(v, n, zivotne) for v in vw) for n in nw)


def mluvi_o(text, nazev) -> bool:
    """Mluví kus podkladu o předmětu? Všechna slova názvu (tvarem) v textu."""
    if not text or not nazev:
        return False
    tw = set(_slova(str(text)[:4000]))
    nw = [n for n in nazev_slova(nazev)
          if not any(je_tvar(n, d) for d in _DRUHOVA)] or nazev_slova(nazev)
    # oběma směry: předmět může být kotva v pádě („Rudolfu Křesťanovi“) a text
    # nese jiný tvar („Rudolf Křesťan“) — /tazatel 15. 10. by vyřadil zprávy
    return all(any(je_tvar(t, n) or je_tvar(n, t) for t in tw) for n in nw)


# ── předmět ─────────────────────────────────────────────────────────────
def otazkova_cast(text) -> str:
    """Věty s otazníkem, jinak celý text. Rekapitulace před otázkou předmět
    nenese („…východně od Berouna. Znáte nějakou osobnost?“). Iniciála
    s tečkou („F. L. Věk“) větu nekončí."""
    t = str(text or "")
    # HANS_ANCHOR_RIMSKE_V1 — „Kde Karel IV. trávil dětství?“: tečka za řadovou
    # číslicí panovníka, po níž věta pokračuje malým písmenem, větu nekončí
    _t = re.sub(r"(?<=\s)([IVX]{1,5})\.(?=\s+[a-zá-ž])", "\\1\x00", t)
    # HANS_ANCHOR_SV_TITUL_V1 (17. 10.) — „katedrálu sv. Víta“: zkratka „sv.“
    # větu nekončí (jinak zbylo „Víta v Praze?“ a předmětem se stala „Praze“)
    _t = re.sub(r"(?<=\s)([Ss]v)\.(?=\s+[A-ZÁ-Ž])", "\\1\x00", _t)
    kusy = [k.strip().replace("\x00", ".")
            for k in re.split(r"(?<![ .][A-ZÁ-Ž]\.)(?<=[.!?])\s+", _t) if k.strip()]
    q = [k for k in kusy if "?" in k]
    return " ".join(q) if q and len(kusy) > 1 else t


_ZAPOR = re.compile(
    r"\b(?:nejde|nejedn\w+ se|nemysl[ií]m|nem[aá]m na mysli|nen[ií] to|ne o)\b"
    r"[^.;?!]*?,?\s*(?:ale|n[ýy]br[zž])\s+", re.IGNORECASE)


def bez_zaporu(text) -> str:
    """„Nejde o ponorné plavidlo Titan, ale o slavnou loď…“ → „o slavnou loď…“.
    Přepis F1 opravu vyjádří záporem a kotva by vzala právě odmítnutou věc
    (/tazatel 15. 10.: „Titan“ místo Titanicu)."""
    return _ZAPOR.sub("", str(text or ""))


_DILO_A = re.compile(
    r"\b(?:hr[auy]|film\w*|oper\w*|balet\w*|knih\w*|knize|rom[aá]n\w*|p[ií]s[nň]\w*|d[ií]l[oau]|"
    r"seri[aá]l\w*|pohád\w*|pohad\w*)\s+([A-ZÁ-Ž]\w+(?:\s+[A-ZÁ-Ž]\w+)*\s+a\s+[A-ZÁ-Ž]\w+)")


def kotvy_vsechny(text, vynech=()) -> list:
    """Všechny skupiny kotev (`kotva_tematu` vrací jen první). Název díla
    „X a Y“ za druhovým slovem jde jako jedna kotva („hru Romeo a Julie“ —
    `kotva_tematu` ho na „a“ rozdělí; „Karlovi a Josefovi“ zůstávají dva).
    Druhové slovo i „dílo“ („známé dílo Romeo a Julie“, /tazatel 15. 10.)."""
    t = str(text or "")
    out = [m.group(1) for m in _DILO_A.finditer(t)]
    for _ in range(4):
        k = kotva_tematu(t, vynech=vynech)
        if not k:
            break
        if not any(k in x for x in out):
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


_DILO_ZAVORKA = re.compile(
    r"\((?:[^)]*\b)?(film|seri[aá]l|epizod|episode|p[ií]se[nň]|album|kniha|rom[aá]n|hra|"
    r"opera|muzik[aá]l|balet|komiks|videohr|hudba|skupina|Doctor Who|[ČC]erven[ýy] trpasl)",
    re.IGNORECASE)
_DILO_NAZNAK = re.compile(
    r"\b(film\w*|sn[ií]m\w*|seri[aá]l\w*|d[ií]l\w*|epizod\w*|p[ií]s[nň]\w*|alb(?:um|a|u|em|ech|ům)?|"
    r"knih\w*|knize|rom[aá]n\w*|hr[auy]|hr[aá]l\w*|hraj\w*|hrou|oper\w*|muzik[aá]l\w*|"
    r"balet\w*|komiks\w*|herc\w*|herec|re[zž]i\w*|nato[cč]\w*|vid[eě]l\w*|sledoval\w*|"
    r"pustil\w*|zahr\w*|skupin\w*|kapel\w*|zp[ií]v\w*)\b", re.IGNORECASE)

_SKUTECNA_OSOBA = re.compile(
    r"\b(skute[cč]n\w*|historick\w*|re[aá]ln\w*)\s+(osob\w*|postav\w*|"
    r"[cč]lov[eě]k\w*|kr[aá]lovn\w*|kr[aá]l\w*|panovn\w*)|"
    r"\b(skute[cč]n[eě]\s+(\w+\s+)?(existoval|[zž]il)\w*|vl[aá]dl\w*)\b",
    re.IGNORECASE)

_KDO_BYL = re.compile(r"\bkdo\s+(?:byl|je|byla|jsou|byli)\b", re.IGNORECASE)


def dilo_bez_naznaku(jmeno, *texty) -> bool:
    """Entita je DÍLO s rozlišovací závorkou („Kleopatra (film, 1963)“), ale věta
    o žádném díle nemluví („kdy žila Kleopatra?“) → předmětem je ta věc
    sama, ne film. /tazatel 15. 10.: královna dostala oporou popis filmu."""
    if not _DILO_ZAVORKA.search(str(jmeno or "")):
        return False
    # HANS_PODKLAD_DILO_OSOBA_V1 — ptá-li se věta na skutečnou osobu („vím, že
    # existuje film Kleopatra, ale byla skutečnou osobou?“, „existovala
    # historická osoba Drákula?“), zmínka o díle předmět nedělá. /tazatel
    # 15. 10.: Kleopatra 4×; 📏 korpus 16. 10.: 3 takové věty, všechny o osobě.
    if any(_SKUTECNA_OSOBA.search(str(t or "")) for t in texty):
        return True
    # HANS_PODKLAD_DILO_TITUL_V1 — víceslovný název díla stojí ve větě celý
    # („co víš o Vraždě v Orient expresu?“) = mluví se o díle, i bez slova
    # film/kniha; „kdo byl hrabě Monte Christo?“ ale míří na postavu.
    # 📏 sada variant 16. 10. + přehrání: 18 změn z 543, všechny Vražda v O. e.
    if len(nazev_slova(jmeno)) >= 2 and any(
            t and nazev_ve_vete(jmeno, t) and not _KDO_BYL.search(str(t))
            for t in texty):
        return False
    return not any(_DILO_NAZNAK.search(str(t or "")) for t in texty)


def bez_druhu_u_osoby(kotva, text):
    """HANS_PODKLAD_OSOBA_BEZ_DRUHU_V1 — ptá-li se věta na skutečnou osobu,
    druhové slovo kotvy („film Kleopatra“) do tématu nepatří: dohledání by
    zase našlo film. Jinak kotva beze změny („hrad Kost“, „film Kleopatra“
    u otázky na film)."""
    k = str(kotva or "")
    w = k.split()
    if (len(w) >= 2 and _SKUTECNA_OSOBA.search(str(text or ""))
            and any(je_tvar(_fold(w[0]), d) for d in _DRUHOVA)):
        return " ".join(w[1:])
    return kotva


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


def _na_zacatku(jmeno, text) -> bool:
    """HANS_PODKLAD_ENTITA_ZACATEK_V1 — začíná věta jménem entity (i skloněným)?"""
    ew = nazev_slova(jmeno)
    vw = _slova(text)
    return bool(ew and len(vw) >= len(ew)
                and all(je_tvar(v, e) for v, e in zip(vw[:len(ew)], ew)))


def predmet_z(h, text, vynech=()):
    """(druh, název, entita) z jedné věty; druh titul | entita | entita_slaba | kotva | None."""
    if not text:
        return None, None, None
    from scripts.hans_recall_znalost import fakta_tituly_ve_vete
    text = bez_zaporu(otazkova_cast(text))
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
    # „-ovi“ jen u osob; víceslovné jméno („F. L. Věk“ — v databázi dílo)
    # takhle náhodou nesedne, omezení jen u jednoslovného („Kolumbo“)
    _ziv = (str((ent or {}).get("etype") or "") in ("osoba", "postava")
            or len(str((ent or {}).get("name") or "").split()) >= 2)
    ent_ok = bool(ent and jm and nazev_ve_vete(jm, text, _ziv) and _entita_predmet(h, ent, text)
                  and not osloveni(jm, text) and not dilo_bez_naznaku(jm, text))
    kotva = kotvy[0] if kotvy else None
    if ent_ok and kotvy:
        ew = nazev_slova(jm)

        kryta = [k for k in kotvy if kryje(jm, k)]
        if kryta:
            kotva = kryta[0]
            if len(ew) == 1 and len(kotvy) > len(kryta):
                # jedna z víc kotev („Česko proti Slovensku“) → předmět nejistý
                return "entita_slaba", jm, ent
        elif _na_zacatku(jm, text):
            # HANS_PODKLAD_ENTITA_ZACATEK_V1 (20. 10.) — první slovo věty kotvou
            # není (velké písmeno je tam povinné), takže entita na začátku
            # („Korsika - je francouzský ostrov a kdy se stala součástí Francie?“)
            # prohrála s vedlejší kotvou „Francie“ → podklad bez entity, odpověď
            # z hlavy („Republika Ženeva“). /tazatel 20. 10.
            kotva = None
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
        return "kotva", bez_druhu_u_osoby(kotva, text), None
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
    # HANS_PODKLAD_ENTITA_WD_V1 (23. 10.) — údaje z Wikidat i k entitě, když je
    # nemá výpis faktů (HANS_FACTS_WIKIDATA_V1 je jen u něj): „prý Karlštejn
    # stavěl nějaký Francouz“ → podklad entita + článek bez stavitele → model
    # „Ulrico Aostalli z Itálie“ (16. stol.); Wikidata: architekt Matyáš z Arrasu.
    try:
        if (kusy["entita"] and druh in ("titul", "entita") and nazev
                and "Ověřené údaje" not in kusy["fakta"]
                and bool((h.config.get("grounding", {}) or {}).get("facts_wikidata", True))):
            from scripts.hans_findings import _wikidata_blok
            _t = re.sub(r"\s*\([^)]*\)$", "", nazev) if druh == "entita" else nazev
            _b = _wikidata_blok(_t)
            if _b:
                kusy["entita"] += ("\nOvěřené údaje k „%s“:\n" % _t
                                   + "\n".join("- " + r for r in _b.split("\n")[1:]))
    except Exception as _we:
        _log.debug("HANS_PODKLAD_ENTITA_WD_V1: %s", _we)
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
    # HANS_PODKLAD_CLANEK_V1 (20. 10.) — u konkrétní otázky na předmět výběr
    # z lokálního článku podle otázky (HANS_LOOKUP_VYBER_V1). Na podložené cestě
    # (entita + výpis faktů z noci) se nedohledává a model doplnil, co výpis
    # neměl: „na co zemřel Karel IV.?“ → „žlutá zimnice ve Vídni“ (zápal plic,
    # Praha — sekce „Poslední rok a smrt“). Vypínač `grounding.clanek_podle_otazky`.
    kusy["clanek"] = ""
    try:
        if (druh in ("titul", "entita") and nazev
                and bool((h.config.get("grounding", {}) or {}).get("clanek_podle_otazky", True))):
            from scripts.hans_findings import _konkretni_otazka, _podklad_podle_otazky
            if _konkretni_otazka(h.config, dotaz):
                _c = _podklad_podle_otazky(re.sub(r"\s*\([^)]*\)$", "", nazev)
                                           if druh == "entita" else nazev, dotaz, h.config)
                if _c:
                    kusy["clanek"] = ("Z encyklopedického článku o „%s“ (části k otázce):\n%s"
                                      % (nazev, _c))
    except Exception as _ce:
        _log.debug("HANS_PODKLAD_CLANEK_V1: %s", _ce)
    return kusy


def vyber(druh, kusy, nazev=None, texty=()) -> list:
    """Vybrané zdroje (pořadí = priorita), nebo ['kaskada'] = jako dnes;
    „-x“ = z kaskády vyřadit zdroj x."""
    if druh in ("titul", "entita"):
        v = [k for k in ("fakta", "entita") if kusy.get(k)]
        if kusy.get("zapisky") and kusy.get("zapisky_o_predmetu"):
            v.append("zapisky")
        if kusy.get("clanek") and v:   # HANS_PODKLAD_CLANEK_V1 — jen vedle vlastní opory
            v.append("clanek")
        if kusy.get("zpravy") and kusy.get("zpravy_o_predmetu"):
            v.append("zpravy")
        return v or ["bez_opory"]
    v = ["kaskada"]
    if (druh == "kotva" and nazev and kusy.get("entita_dnes")
            and (not kryje(kusy["entita_dnes"], nazev)
                 or dilo_bez_naznaku(kusy["entita_dnes"], *texty))):
        v.append("-entita")     # Bell → Alexandrie, Marťan; královna → film
    if druh in ("kotva", "entita_slaba") and kusy.get("zpravy") and not kusy.get("zpravy_o_predmetu"):
        v.append("-zpravy")
    return v


def _tituly_cteni(h, blok) -> list:
    """Názvy zápisků, které vybavila cesta četby (blok názvy nenese; dohledají
    se v deníku podle začátku textu — logika výběru se nekopíruje)."""
    import sqlite3
    out = []
    kusy = [k for k in re.split(r"\n\s*•\s*", str(blok or "")) if k.strip()][1:]
    try:
        c = sqlite3.connect("file:%s?mode=ro" % _db(h), uri=True, timeout=3)
        try:
            for k in kusy:
                zac = k.strip()[:60]
                r = c.execute(
                    "SELECT coalesce(title,'') FROM diary WHERE event_type IN "
                    "('web_read','reading_takeaway','study_note') AND "
                    "(instr(note, ?) > 0 OR instr(data, ?) > 0) ORDER BY ts DESC LIMIT 1",
                    (zac, zac)).fetchone()
                out.append(r[0] if r else "")
        finally:
            c.close()
    except Exception:
        return []
    return out


def vyber_cteni(blok, druh, nazev, h=None) -> list:
    """Cesta četby páruje téma začátkem slova („Titanic“ → Titan, Souboj
    Titánů; „Kolumbus“ → sopka Kolumbo). U silného předmětu nebo kotvy se
    počítá, kolik vybavených zápisků o předmětu skutečně mluví:
    ['cteni k/n'] nebo ['-cteni'] (žádný → četba ven, dál kaskáda)."""
    if druh not in ("titul", "entita", "kotva") or not nazev or not isinstance(blok, str):
        return ["cteni"]
    tit = _tituly_cteni(h, blok) if h is not None else []
    if not tit or not any(tit):
        return ["cteni"]
    # o předmětu je zápisek, jehož NÁZEV předmět nese („Titan (ponorka)“
    # ani „Souboj Titánů“ o Titanicu nejsou, i když ho text zmíní)
    o = sum(1 for t in tit if t and mluvi_o(t, nazev))
    return ["-cteni"] if o == 0 else ["cteni %d/%d" % (o, len(tit))]


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
_CTENI = {"reading_recall", "reading_recall_tema"}
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
    if not bool((h.config.get("grounding", {}) or {}).get("podklad_stin", True)):
        return
    _rozhodni(h, ctx)


def _rozhodni(h, ctx):
    """Rozhodnutí společného kroku + záznam stínu. Vrací dict, nebo None
    (věta nedošla ke konci faktické cesty / chyba)."""
    try:
        vysl = getattr(h, "_grounding_outcome", "") or ""
        cesta = getattr(h, "_grounding_cesta", "") or ""
        if not (vysl == "factual_nofacts"
                or (vysl == "grounded" and (cesta in _OCAS or cesta in _CTENI))):
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
            vb = vyber(druh, kusy, nazev, (veta, prepis))
            if cesta in _CTENI:
                vb = vyber_cteni(getattr(ctx, "_grounding", ""), druh, nazev, h) + vb[1:]
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
        return dict(druh=druh, nazev=nazev, vyber=vb, kusy=kusy, tema=tn,
                    vysl=vysl, cesta=cesta)
    except Exception as e:
        _TICHO.on = False
        _log.debug("PODKLAD_STIN_V1 selhal: %s", e)
        return None


# ── přepnutí ────────────────────────────────────────────────────────────
# HANS_PODKLAD_PREPNUTI_V1 (16. 10.) — společný krok podkladu NAOSTRO, za
# vypínačem `grounding.spolecny_krok` (výchozí vypnuto). 📏 před přepnutím:
# přehrání 882 vět (`STIN_ODECET_16_10`), sada 816 variant (`STIN_VARIANTY_16_10`).
_ENTITNI = {"entita_c1", "zapisky_pred_entitou"}


_BEZ_DEFINICE = re.compile(r"^Ověřený fakt o „[^“]*“ \([^)]*\): .{0,200}? — o tomto pojmu "
                           r"mám záznam, ale bez bližší definice\.\s*", re.M)


def _cista_cast(text, druh):
    """Kus podkladu bez prázdného řádku entity (strukturovaná fakta pod ním zůstanou)."""
    text = str(text or "")
    if druh == "entita":
        text = _BEZ_DEFINICE.sub("", text)
    return text.strip()


def prepni(h, ctx, pred_zpravami=None) -> None:
    """Rozhodne jako stín a podle výběru PŘESTAVÍ podklad:
    • silný předmět s kusy → podklad jen z kusů o předmětu (fakta, entita,
      zápisky; zprávy, když vyhrály a mluví o předmětu);
    • silný předmět bez kusů → bez opory (A1, dohledání předmětu);
    • `-zpravy` → podklad před vložením zpráv; `-entita` u entitní cesty
      a `-cteni` → bez opory;
    • téma dohledání = předmět (`h._podklad_tema`, čte `_dohledej_kotvu`)."""
    r = _rozhodni(h, ctx)
    if not r:
        return
    from scripts.openwebui_direct_handler import ANTIKONFAB, ANTIKONFAB_NOFACTS
    druh, vb, kusy = r["druh"], r["vyber"], r["kusy"]
    if r["tema"]:
        h._podklad_tema = r["tema"]
    zmena = None
    if druh in ("titul", "entita") and vb != ["bez_opory"] and vb[:1] != ["-cteni"] \
            and not str(vb[0]).startswith("cteni"):
        if "zpravy" in vb and r["cesta"] == "zpravy":
            zmena = None          # zprávy o předmětu vyhrály i dnes — beze změny
        else:
            # HANS_PODKLAD_SESTAVA_V1 — blok zápisků už instrukci podkladu nese
            # (dvakrát = model zmaten, párově 16. 10. „Dobrý den, ty.“ 2× ze 2)
            # a entita bez definice (145 ze 782) je jen řádek „bez bližší
            # definice“, který dnešní cesta zápisků do podkladu nedává
            _kc = [(k, _cista_cast(kusy[k], k).replace(ANTIKONFAB, "").strip())
                   for k in ("fakta", "entita", "zapisky", "clanek") if k in vb and kusy.get(k)]
            _kc = [(k, c) for k, c in _kc if c]
            casti = [c for _, c in _kc]
            if casti:
                ctx._grounding = "\n\n" + ANTIKONFAB + "\n\n" + "\n\n".join(casti)
                ctx._zpravy = None
                # HANS_PODKLAD_TENKY_V1 — podklad se zápisky je TENKÝ jako dnešní
                # `zapisky_pred_entitou` (název `zapisky…` čte GROUNDING_GUARD_ACTIVE_V2:
                # „zápisky nestačily → dohledej“). Párově 16. 10.: Český ráj bez
                # toho ztratil dohledání a odpověděl poeticky bez hradů.
                _jm = "+".join(k for k, _ in _kc)
                zmena = ("grounded", ("zapisky_podklad_" if any(k == "zapisky" for k, _ in _kc)
                                      else "podklad_") + _jm)
    elif druh in ("titul", "entita") and vb == ["bez_opory"]:
        ctx._grounding = "\n\n" + ANTIKONFAB_NOFACTS
        ctx._zpravy = None
        zmena = ("factual_nofacts", "podklad_bez_opory")
    else:
        if "-zpravy" in vb and r["cesta"] == "zpravy" and pred_zpravami:
            g, v, c = pred_zpravami
            ctx._grounding = g
            ctx._zpravy = None
            zmena = (v, c)
            r["cesta"] = c
        if ("-entita" in vb and r["cesta"] in _ENTITNI) or "-cteni" in vb:
            ctx._grounding = "\n\n" + ANTIKONFAB_NOFACTS
            ctx._zpravy = None
            zmena = ("factual_nofacts", "podklad_bez_" + ("entity" if "-entita" in vb else "cteni"))
    if zmena:
        h._vysledek_groundingu(*zmena)
        _log.info("HANS_PODKLAD_PREPNUTI_V1: %s ← %s (dnes %s ← %s) · téma %r",
                  zmena[0], zmena[1], r["vysl"], r["cesta"], r["tema"])
