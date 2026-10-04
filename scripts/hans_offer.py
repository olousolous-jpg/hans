# -*- coding: utf-8 -*-
"""HANS_OFFER_TO_PENDING_V1 (2. 10.) — co Hans NABÍDNE, to po souhlasu SPLNÍ.

PROČ: potvrzování („ano“, „můžeš“) umí jen agent, a jen u SVÝCH návrhů.
Když nabídku vymyslí volný hovor (LLM) nebo ji vysloví pevná odpověď příkazu,
souhlas nemá na co navázat a nic se nestane — přitom Hans řekl „udělám“.
Doloženo 2. 10.: po hodnocení obrazu „sice to není letadlo“ Hans napsal
„Obraz smažu a namaluji ho znovu…“, na „můžeš“ odpověděl „Začínám kresbu…“
— a do fronty se nedostalo nic (ráno totéž: „Začínám malovat obraz…“).
Změřeno na celé historii (1 648 výměn): nabídka + souhlas 8×, sedm byly
návrhy agenta (všechny splněny), nesplněná jen ta z volného hovoru.
Pevná nabídka `/hraje` („Mohl bych navrhnout „X“ — stačí říct a pustím to.“)
čekající návrh neukládala vůbec.

CO SE DĚJE: po odpovědi se hledá nabídka/slib akce, kterou systém opravdu
umí (namalovat, nastudovat, pustit film). Najde-li se, uloží se jako ČEKAJÍCÍ
NÁVRH agenta a slib v odpovědi se nahradí otázkou, kterou skládá program
(`_default_text`, jako u ostatních návrhů) — „ano“ pak běží existující
cestou `check_confirmation`. Nejde-li nabídku spolehlivě převést, slib se
nahradí poctivou větou, jak akci zadat. Prázdný slib nezůstane.

⚠️ Nebezpečné akce (vypnutí PC, hlídání) se tudy NEpřevádějí — jen přímý povel.
⚠️ Přeskočí se, když v tomtéž tahu akce PROBĚHLA (úloha malby ve frontě od
začátku tahu) nebo už čeká návrh agenta.
"""
from __future__ import annotations

import json
import logging
import re
import time

_log = logging.getLogger(__name__)

# slib/nabídka MALOVÁNÍ (1. osoba, budoucí/probíhající; ne minulý čas)
_PAINT = re.compile(
    r"\b(?:namaluji|p[řr]emaluji|nakresl[íi]m)\b|"
    r"\bza[čc][íi]n[áa]m\s+(?:malovat|kresb\w*|kreslit|s\s+malov\w*)|"
    r"\bpust[íi]m\s+se\s+do\s+(?:malov\w*|kresb\w*)|"
    r"\bobraz\s+(?:sma[žz]u|p[řr]emaluji)|"
    r"\b(?:mohu|m[ůu][žz]u|m[áa]m)\s+(?:v[áa]m\s+|ti\s+)?(?:ho\s+|ji\s+|to\s+)?"
    r"(?:namalovat|p[řr]emalovat|nakreslit)\b", re.IGNORECASE)
_ZNOVU = re.compile(r"\b(?:znovu|znova|p[řr]emal\w*|sma[žz]u|je[šs]t[ěe]\s+jednou|"
                    r"oprav\w*|naprav\w*|kresb\w*|za[čc][íi]n[áa]m)\b", re.IGNORECASE)
# slib STUDIA s výslovným tématem („nastuduji si více o X“)
_STUDY = re.compile(r"\bnastuduji(?:\s+si)?\s+(?:(?:v[íi]ce|v[íi]c|informace|n[ěe]co)\s+)?"
                    r"o\s+([^.?!;\n]{3,60})", re.IGNORECASE)
# nabídka FILMU s titulem v uvozovkách („… „X“ — stačí říct a pustím to.“)
_FILM = re.compile(r"\bsta[čc][íi]\s+[řr][íi]ct\s+a\s+pust[íi]m\s+(?:to|ho|ji)\b|"
                   r"\b(?:mohu|m[ůu][žz]u)\s+(?:v[áa]m\s+|ti\s+)?(?:ho|ji|ten\s+film)\s+"
                   r"(?:pustit|spustit)", re.IGNORECASE)
# HANS_OFFER_PLAY_CLAIM_V1 (4. 10.) — TVRZENÍ, že film už běží („Přehrávám“,
# „Pouštím film X“, „Připravuji přehrání“), ačkoli agent v tomto tahu nic
# nespustil. Hlas 3. 10. 23:05: „Přehrej Hvězdné války 3.“ → volný hovor
# „Přehrávám, <Jméno>.“ a nespustilo se nic; 5. 8. totéž 3× („Připravuji přehrání
# filmu Kruh“). Změřeno na 1 679 rozhovorech deníku: 4 nepravdivá tvrzení,
# 1 skutečné spuštění (agent_action accepted — to se pozná), vyloučit
# „nechci předstírat, že ho pouštím“ (5×) a „pustil jsem se do studia“ (4×).
_PLAY_CLAIM = re.compile(
    r"\b(?:p[řr]ehr[áa]v[áa]m|pou[šs]t[íi]m(?!\s+se\b)|spou[šs]t[íi]m(?!\s+se\b)|"
    r"(?:pustil|spustil)\s+jsem(?!\s+se\b)|p[řr]ipravuj[iu]\s+(?:p[řr]ehr[áa]n|spu[šs]t)\w*|"
    r"zap[íi]n[áa]m\s+(?:film|p[řr]ehr\w*))\b", re.IGNORECASE)
_PLAY_NEG = re.compile(r"p[řr]edst[íi]rat|nemohu|nem[ůu][žz]u|neum[íi]m|nem[áa]m|kdyby|pokud|"
                       r"\bnepou|\bnep[řr]ehr|\bnespou", re.IGNORECASE)
_TITUL = re.compile(r"[„\"*]([^„\"“*\n]{2,70})[\"“*]")
_VETY = re.compile(r"(?<=[.!?])\s+")
_OPRAVA = re.compile(r"\b(?:nen[íi]|ne\b|nebyl\w*|nesed[íi]|jinak|m[íi]sto|chyb[íi]\w*|"
                     r"[šs]patn\w*|m[ěe]l\w*\s+(?:by|b[ýy]t)|sp[íi][šs]\b)", re.IGNORECASE)

_POCTIVE = {
    "paint": "Namalovat to mohu — napište mi prosím „namaluj …“ a co má na obraze být.",
    "study": "Zařadit to ke studiu mohu — napište mi prosím „nastuduj …“.",
    "film": "Pustit film mohu — napište mi prosím „pusť …“.",
    "film_tvrzeni": "Film se ale nespustil — napište mi prosím „pusť …“ s jeho názvem.",
}


def _je_znamy(config, name) -> bool:
    try:
        from scripts.cz_names import is_known_person
        return bool(name) and bool(is_known_person(name))
    except Exception:
        return False


def _smi_akce(config, name) -> bool:
    try:
        from scripts.hans_prava import muze
        return bool(muze(config, name, "akce"))
    except Exception:
        return True


def _db(config) -> str:
    return (config.get("diary_db") or (config.get("hans_idle", {}) or {}).get("diary_db")
            or "data/hans_diary.db")


def _malba_ve_fronte(config, name, od_ts) -> bool:
    """Zařadila se v tomto tahu malba? (pak slib PLATÍ, nic nepřevádět)"""
    try:
        import sqlite3
        c = sqlite3.connect("file:%s?mode=ro" % _db(config), uri=True, timeout=3)
        n = c.execute("SELECT count(*) FROM heavy_jobs WHERE kind='paint' AND created_ts>=?",
                      (od_ts - 2,)).fetchone()[0]
        c.close()
        return n > 0
    except Exception:
        return False


def _posledni_namet(config, name, max_h=6.0) -> str:
    try:
        import sqlite3
        c = sqlite3.connect("file:%s?mode=ro" % _db(config), uri=True, timeout=3)
        r = c.execute("SELECT payload FROM heavy_jobs WHERE kind='paint' AND lower(person)=lower(?) "
                      "AND created_ts>=? ORDER BY id DESC LIMIT 1",
                      (name or "", time.time() - max_h * 3600)).fetchone()
        c.close()
        return ((json.loads(r[0] or "{}") or {}).get("subject") or "").strip() if r else ""
    except Exception:
        return ""


def _run_paint(handler, args) -> str:
    from scripts.chat_commands import _cmd_namaluj
    return _cmd_namaluj(handler, args.get("_osoba") or getattr(handler, "_last_person", "") or "",
                        args.get("namet") or "")


def _paint_action():
    from scripts.hans_agent import Action
    # NENÍ v ACTIONS záměrně: router ji nevidí (popis akce = zásah do routeru),
    # vzniká JEN z Hansovy nabídky.
    return Action("paint_offer", "", [], ["namet"], _run_paint,
                  grounding=None, needs_confirm=True, cooldown_s=10)


def _najdi(reply: str, user_message: str, config, name):
    """→ (druh, aid, args, věta) nebo None."""
    vety = _VETY.split(reply or "")
    for v in vety:
        if _PAINT.search(v):
            namet = ""
            if _ZNOVU.search(v) or re.search(r"\b(?:ho|ji|to)\b", v):
                namet = _posledni_namet(config, name)
                if namet:
                    um = (user_message or "").strip()
                    # jen skutečná OPRAVA („sice to není letadlo“), ne pochvala ani „můžeš“
                    if um and _OPRAVA.search(um):
                        namet = "%s (oprava: %s)" % (namet, um[:120])
            return ("paint", "paint_offer", {"namet": namet, "_osoba": name}, v)
        m = _STUDY.search(v)
        if m:
            tema = m.group(1).strip(" ,")
            if re.match(r"(?:tom|to|tomto|tomhle|n[ěe]m|n[íi])\b", tema, re.IGNORECASE):
                tema = ""
            # téma je v 6. pádě („o japonských zahradách“) a do studia patří 1. pád
            # (hledá se podle něj na Wikipedii) → nepřevádět, jen poctivá věta
            return ("study", "add_study_topic", {"tema": ""}, v)
        if _FILM.search(v):
            tit = _TITUL.findall(v) or _TITUL.findall(reply)
            return ("film", "kodi_play_film", {"titul": tit[-1].strip() if tit else ""}, v)
        if _PLAY_CLAIM.search(v) and not _PLAY_NEG.search(v):
            # název bývá ve vedlejší větě („Z knihovny je to *X*. Přehrávám.“),
            # jinak v dotazu uživatele (grounding ho najde v knihovně)
            tit = _TITUL.findall(v) or _TITUL.findall(reply)
            titul = tit[-1].strip() if tit else ""
            if not titul:
                m = re.search(r"\b(?:p[řr]ehr\w*|pus[tť]\w*|spus[tť]\w*|zahraj\w*)\s+"
                              r"(?:(?:mi|n[áa]m|film|seri[áa]l)\s+)*(.{2,60}?)[.?!]*$",
                              (user_message or "").strip(), re.IGNORECASE)
                titul = m.group(1).strip() if m else ""
            return ("film_tvrzeni", "kodi_play_film", {"titul": titul}, v)
    return None


def _film_spusten(config, od_ts) -> bool:
    """Spustil agent v tomto tahu film / přehrávání? (pak „Pouštím“ PLATÍ)"""
    try:
        import sqlite3
        c = sqlite3.connect("file:%s?mode=ro" % _db(config), uri=True, timeout=3)
        n = c.execute("SELECT count(*) FROM diary WHERE event_type='agent_action' AND ts>=? "
                      "AND data LIKE '%accepted%' AND (data LIKE '%kodi_play%' "
                      "OR data LIKE '%kodi_resume%')", (od_ts - 2,)).fetchone()[0]
        c.close()
        return n > 0
    except Exception:
        return True               # nevím → radši nechat, než vzít platné „Pouštím“


def zpracuj(handler, name: str, user_message: str, reply: str, t0: float):
    """→ (nová odpověď, dodatek). Dodatek = věta připojená/nahrazená navíc
    (hlas ji dořekne, když už zbytek odpověď vyslovil po větách)."""
    try:
        if not reply or not name:
            return reply, ""
        config = getattr(handler, "config", {}) or {}
        if not (config.get("agent", {}) or {}).get("offer_to_pending", True):
            return reply, ""
        nal = _najdi(reply, user_message, config, name)
        if not nal:
            return reply, ""
        druh, aid, args, veta = nal
        _ar = handler._agent_router() if hasattr(handler, "_agent_router") else None
        if _ar is None:
            return reply, ""
        _p = (getattr(_ar, "_pending", {}) or {}).get(name)
        if _p is not None and time.time() - _p.ts <= 180:
            return reply, ""                           # agent už se ptá
        if druh == "paint" and _malba_ve_fronte(config, name, t0):
            return reply, ""                           # malba se opravdu zařadila
        if druh == "film_tvrzeni":
            if _film_spusten(config, t0):
                return reply, ""                       # agent film opravdu pustil
            _log.info("HANS_OFFER_PLAY_CLAIM_V1: tvrzení o přehrávání bez akce: %.80s", veta)
        bez = re.sub(r"[ \t]{2,}", " ", reply.replace(veta, "")).strip()
        from scripts.hans_agent import ACTIONS, Proposal
        action = _paint_action() if aid == "paint_offer" else ACTIONS.get(aid)
        klic = list(args.values())[0] if args else ""
        if (action is None or not klic or not _je_znamy(config, name)
                or not _smi_akce(config, name)):
            dod = (_POCTIVE[druh] if _je_znamy(config, name)
                   else "Tohle mohu udělat jen pro svou domácnost.")
            _log.info("HANS_OFFER_TO_PENDING_V1: %s slib bez převodu (%s) → poctivá věta",
                      druh, "chybí předmět" if not klic else "osoba")
            return (bez + " " + dod).strip(), dod
        if action.grounding:
            ok, args, gmsg = action.grounding(handler, args)
            if not ok:
                dod = _POCTIVE[druh]
                _log.info("HANS_OFFER_TO_PENDING_V1: %s grounding zamítl (%s)", aid, gmsg)
                return (bez + " " + dod).strip(), dod
        prop = Proposal(action, args, "", 1.0, "nabídka Hanse", person=name)
        if aid == "paint_offer":
            otazka = "Mám namalovat obraz „%s“?" % args.get("namet", "")[:110]
        else:
            otazka = _ar._default_text(action, args).strip()
        prop.text = otazka
        _ar._pending[name] = prop
        try:
            _ar._log(handler, prop, "proposed")
        except Exception:
            pass
        _log.info("HANS_OFFER_TO_PENDING_V1: nabídka %s → čekající návrh %s pro %s",
                  druh, {k: v for k, v in args.items() if not k.startswith("_")}, name)
        return (bez + " " + otazka).strip(), otazka
    except Exception as e:
        _log.warning("HANS_OFFER_TO_PENDING_V1: %s", e)
        return reply, ""
