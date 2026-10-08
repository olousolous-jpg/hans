"""Obsluhy příkazů přesunuté z `scripts/chat_commands.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_cc.` až při
volání. Registrace příkazů (`register(...)`) zůstává v původním modulu.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations




def _cmd_zpravy(handler, name, args) -> str:
    """HANS_ZPRAVY_CHAT_V1 (3. 10.) — co je ve zprávách (sběr hans_zpravy).

    Hledá podle VÝZNAMU (bge-m3 na PC, napříč jazyky), při spícím PC podle
    slov v českých titulcích. Titulky cizích médií se nepřekládají (rozhodnutí
    uživatele). Bez tématu = největší události dne. Veřejné → smí i cizí.
    Dřív Hans ke zprávám přístup neměl a přehled si vymýšlel (/tazatel 3. 10.)."""
    from datetime import datetime as _dt
    from scripts.hans_zpravy import zpravy_hledej
    if _cc.dotaz_hlavni_zprava(args):       # HANS_HLAVNI_ZPRAVA_V1 — vybraná, je-li nějaká
        try:
            from scripts.hans_hlavni_zprava import posledni as _hz_posledni
            if _hz_posledni():
                return _cc._cmd_hlavnizprava(handler, name, args)
        except Exception:
            pass
    try:
        cfg = getattr(handler, "config", None)
        if cfg is None:
            from scripts.config_io import load as _cl
            cfg = _cl()
        r = zpravy_hledej(args or "", cfg, zaloha_prehled=True)   # HANS_ZPRAVY_TEMA_ZALOHA_V1
    except Exception:
        return "Do sebraných zpráv se mi teď nepodařilo nahlédnout."

    def _c(ts):
        try:
            d = _dt.fromtimestamp(ts)
            return ("dnes %s" % d.strftime("%H:%M") if d.date() == _dt.now().date()
                    else "%d. %d. %s" % (d.day, d.month, d.strftime("%H:%M")))
        except Exception:
            return "?"
    if not r["udalosti"]:
        if not r["tema"]:
            return "Za poslední den jsem ve zprávách nic nesebral."
        pozn = (" (počítač s jazykovým modelem teď neběží, hledal jsem jen v českých titulcích)"
                if r.get("hledano") == "slova" else "")
        return "O tom jsem ve zprávách za poslední tři dny nic nenašel%s." % pozn
    # HANS_ZPRAVY_PREKLAD_V1 — cizí titulky česky (překlad jen pro čtení)
    try:
        import sqlite3 as _sq
        from scripts.hans_zpravy import DB as _ZDB, preklad_mapa
        _pc = _sq.connect("file:%s?mode=ro" % _ZDB, uri=True, timeout=5)
        _pm = preklad_mapa(_pc, [u["titulek"] for u in r["udalosti"]] +
                           [u["trefa"]["titulek"] for u in r["udalosti"] if u.get("trefa")])
        _pc.close()
        for u in r["udalosti"]:
            u["titulek"] = _pm.get(u["titulek"], u["titulek"])
            if u.get("trefa"):
                u["trefa"]["titulek"] = _pm.get(u["trefa"]["titulek"], u["trefa"]["titulek"])
    except Exception:
        pass
    out = ["Největší zprávy posledních 24 hodin:" if not r["tema"]
           else "Ve zprávách k tomu mám:"]
    for u in r["udalosti"]:
        kolik = (" — píše o tom %d médií" % u["medii"]) if u["medii"] >= 5 else (
            " — píše o tom %d média" % u["medii"] if u["medii"] >= 2 else "")
        out.append("• %s, %s: %s%s" % (_c(u["cas"]), u["zdroj"], u["titulek"], kolik))
        if u.get("trefa"):
            out.append("   také %s, %s: %s" % (_c(u["trefa"]["cas"]), u["trefa"]["zdroj"],
                                            u["trefa"]["titulek"]))
        # HANS_ZPRAVY_ODKAZY_V1 (4. 10.) — odkaz na článek (jen RSS média; hlas
        # URL vynechá). Dřív Hans na „pošli odkaz“ tvrdil, že URL nemá.
        try:
            from scripts.hans_zpravy import zpravy_odkazy
            # článek téhož média jako řádek, když ho máme; jinak jiný (s názvem)
            for _o in (zpravy_odkazy(u["id"], medium=u["zdroj"]) or zpravy_odkazy(u["id"])):
                # holá adresa: médium je vidět v doméně a hlas (tts_speaker
                # maže http…) z řádku nic nepřečte
                out.append("   %s" % _o["url"])
        except Exception:
            pass
    if r["rezim"] == "slova":
        out.append("(Počítač s jazykovým modelem teď neběží, hledal jsem jen v českých titulcích.)")
    out.append("Sbírám je každou hodinu z českých i zahraničních médií; podrobnosti jsou "
               "v záložce Zprávy.")
    return "\n".join(out)


def _cmd_demagog(handler, name, args) -> str:
    """HANS_DEMAGOG_V1 (3. 10.) — ověřené výroky politiků z Demagog.cz.

    Hans jen PŘEDÁVÁ jejich verdikt s datem a odkazem, sám nic nehodnotí
    (pilot 3. 10.: vlastní ověření zvládne ~15 % toho, co ověřuje Demagog).
    Hledá se mluvčí (i skloňovaný: „o Babišovi“), jinak slova výroku.
    Veřejná data → smí i cizí."""
    from scripts.hans_zpravy import demagog_hledej
    import time as _t
    try:
        r = demagog_hledej(args or "", limit=4)
        # 3. 10. uživatel: „vypisuje stále stejné čtyři výroky“ → týž dotaz
        # téže osoby do 10 min ukáže DALŠÍ várku (na konci zase od začátku)
        klic = (name or "", r.get("klic") or (args or "").strip().lower())   # mluvčí + kmeny tématu
        pred = _cc._DEMAGOG_STRANKA.get(klic)
        if pred and _t.time() - pred[1] < 600 and r.get("celkem", 0) > 4:
            posun = pred[0] + 4 if pred[0] + 4 < r["celkem"] else 0
            if posun:
                r = demagog_hledej(args or "", limit=4, posun=posun)
        _cc._DEMAGOG_STRANKA[klic] = (r.get("posun", 0), _t.time())
    except Exception:
        return "Do záznamů z Demagogu se mi teď nepodařilo nahlédnout."
    od = r.get("od")
    if not od:
        return "Ověřené výroky z Demagogu zatím nemám stažené."
    def _d(s):
        try:
            y, m, d = (s or "")[:10].split("-")
            return "%d. %d. %s" % (int(d), int(m), y)
        except Exception:
            return s or "?"
    if not r["vyroky"]:
        if r.get("mluvci"):
            return ("U %s k tomuhle mezi ověřenými výroky z Demagog.cz nic nemám "
                    "(mám je od %s, a jen to, co ověřili oni)." % (r["mluvci"], _d(od)))
        return ("K tomu jsem mezi ověřenými výroky z Demagog.cz nic nenašel "
                "(mám je od %s, a jen to, co ověřili oni)." % _d(od))
    out = []
    sh = r.get("souhrn") or {}
    cel = r.get("celkem", 0)
    poradi = ("pravda", "nepravda", "zavádějící", "neověřitelné")
    sh_txt = ", ".join("%d %s" % (sh[k], k) for k in poradi if sh.get(k))
    if r.get("mluvci") and r.get("tema"):
        out.append("%s — k tomuhle tématu má Demagog.cz %d %s (%s):" % (
            r["mluvci"], cel, "výrok" if cel == 1 else ("výroky" if cel < 5 else "výroků"), sh_txt))
    elif r.get("mluvci"):
        out.append("%s — Demagog.cz od %s ověřil %d %s (%s):" % (
            r["mluvci"], _d(od), cel, "výrok" if cel == 1 else ("výroky" if cel < 5 else "výroků"),
            sh_txt) if cel else "Ověřené výroky — %s, podle Demagog.cz:" % r["mluvci"])
    elif cel:
        out.append("K tomu má Demagog.cz %d ověřených výroků (%s):" % (cel, sh_txt))
    else:
        out.append("Nejnovější ověřené výroky podle Demagog.cz:")
    for v in r["vyroky"]:
        kdo = "" if r.get("mluvci") else "%s%s: " % (
            v["mluvci"], " (%s)" % v["strana"] if v.get("strana") else "")
        vyr = v["vyrok"] if len(v["vyrok"]) <= 220 else v["vyrok"][:217] + "…"
        kr = v.get("kratce") or ""
        kr = kr if len(kr) <= 240 else kr[:237] + "…"
        out.append("• %s%s — %s„%s“ — %s. %s %s" % (
            _d(v["datum"]), (", " + v["porad"]) if v.get("porad") else "",
            kdo, vyr, v["verdikt"].upper(), kr, v["url"]))
    zbyva = cel - (r.get("posun", 0) + len(r["vyroky"]))
    if zbyva > 0:
        out.append("Verdikt je jejich, já ho jen předávám. Zeptáte-li se znovu, ukážu "
                   "dalších %d." % min(4, zbyva))
    else:
        out.append("Verdikt je jejich, já ho jen předávám.")
    return "\n".join(out)


def _cmd_hlavnizprava(handler, name, args) -> str:
    """HANS_HLAVNI_ZPRAVA_V1 (8. 10.) — naposledy vybraná hlavní zpráva dvou dnů
    (text, který šel na Matrix). Veřejná data → smí i cizí."""
    from scripts.hans_hlavni_zprava import posledni
    import time as _t
    try:
        p = posledni()
    except Exception:
        p = None
    if not p:
        return ("Hlavní zprávu jsem zatím žádnou nevybral — vybírám ji jednou za dva dny "
                "z toho, co se nejdéle drželo na špici ve více médiích.")
    return ("Naposledy, %s, jsem jako hlavní zprávu vybral: %s\n\n%s"
            % (_t.strftime("%d. %m.", _t.localtime(p["ts"])).replace(" 0", " ").lstrip("0"),
               (p["titulek"] or "").rstrip("."), p["text"]))


# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import chat_commands as _cc  # noqa: E402
