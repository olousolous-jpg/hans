"""
FACE_HARVEST_V1 — průběžný sběr obličejů přes den pro pozdější označení.

Zadání (uživatel 8.8.): *„sbírat snímky všech lidí co se vyskytují před kamerou
v průběhu dne a já je pak označím, kdo je kdo"* — s tím, že označení přijde
**až s několikahodinovým až několikadenním zpožděním**, protože každý v
domácnosti má jiný denní rytmus.

Proč to je nejcennější zbývající krok (změřeno 8.8.):
    leave-one-out nad toutéž session   86 %
    živý provoz (galerie z jednoho rána) ~24 %
Ten propad dělá galerie z JEDNOHO sezení. Průběžný sběr přes den ho zaceluje —
a nikdo nemusí stát před kamerou, což je tvrdé omezení (enrollment je sociální
náklad, ne každý spolupracuje ochotně).

NÁVRHOVÁ ROZHODNUTÍ, která plynou ze zpožděného označování:
  • Neukládá se crop po cropu k označení, ale **SESSION** = souvislý výskyt
    jednoho tracku. Uživatel pak označuje skupiny, ne jednotlivé snímky
    (desítky rozhodnutí místo tisíců).
  • **Žádná agresivní rotace.** Data se nesmí ztratit dřív, než se k nim
    uživatel dostane. Při ~300 snímcích/den je to ~2.4 MB/den (~70 MB/měsíc),
    takže není důvod mazat brzy.
  • Ke každé session se ukládá **čas a Hansův tip** — podle denního rytmu
    (kdo chodí na směny, kdo je doma o prázdninách) jde osobu určit i po
    několika dnech.

ZÁPIS BĚŽÍ VE VLASTNÍM VLÁKNĚ z fronty — main loop na něm nesmí viset
(stejný vzor jako `_Recorder` v hans_guard). Plná fronta → vzorek se zahodí.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("face_harvest")

_STOP = object()


class FaceHarvester:
    """Sbírá zarovnané cropy + embeddingy pro pozdější dávkové označení."""

    def __init__(self, config: dict | None = None):
        cfg = (config or {}).get("face_harvest", {})
        self.enabled = bool(cfg.get("enabled", False))

        self._dir = Path(cfg.get("dir", "data/harvest"))
        self._interval_s = float(cfg.get("interval_s", 2.0))
        self._session_gap_s = float(cfg.get("session_gap_s", 60.0))
        self._min_area = float(cfg.get("min_face_area", 0.0015))
        self._min_sharp = float(cfg.get("min_sharpness", 15.0))
        self._min_bright = float(cfg.get("min_face_brightness", 35.0))
        self._diversity = float(cfg.get("diversity", 0.15))
        self._max_session = int(cfg.get("max_per_session", 40))
        # HODINOVÝ strop je hlavní regulátor. Denní strop má špatný tvar:
        # vyčerpá se dopoledne a o večerní data přijdeme — přitom pokrytí
        # RŮZNÝCH DENNÍCH DOB je celý smysl sběru (galerie z jednoho sezení
        # dává živě ~24 %, leave-one-out nad toutéž session 86 %).
        self._max_hour = int(cfg.get("max_per_hour", 300))
        # Globální paměť posledních přijatých vzorků. Filtr rozmanitosti se
        # držel JEN v rámci session (track_id), jenže track se tříští —
        # naměřeno 10.8.: 38 různých ID na 222 snímků, medián 2 snímky na
        # track, a 63 % snímků mělo jinde skoro dvojče. Per-track buffer se
        # tedy pořád resetoval a duplicity procházely. S více lidmi (míjení,
        # překryv) je přeskakování ID ještě častější.
        from collections import deque as _dq
        self._recent = _dq(maxlen=int(cfg.get("recent_memory", 300)))
        self._max_day = int(cfg.get("max_per_day", 3000))   # jen pojistka
        self._hour = None
        self._hour_count = 0

        # track_id → stav probíhající session
        self._tracks: dict = {}
        self._day = None
        self._day_count = 0

        # Diagnostika bran — bez ní se nepozná, PROČ se nic nesbírá
        # (a to je při ladění prahů ta jediná otázka, na které záleží).
        self._rej: dict = {}
        self._rej_at = 0.0

        # FACE_NEGATIVES_V1: nesbírat falešné detekce (ventilátor apod.) —
        # tvořily 53 % sběru a uživatel je musel odklikávat ručně.
        self._neg = None
        self._neg_thr = float(cfg.get("negative_thresh", 0.65))
        try:
            import pickle as _pk
            with open(cfg.get("negatives_path",
                              "data/known_faces_negative.pkl"), "rb") as _f:
                _n = np.array(_pk.load(_f), np.float32)
            self._neg = _n / (np.linalg.norm(_n, axis=1, keepdims=True) + 1e-9)
        except Exception:
            pass

        self._q: queue.Queue = queue.Queue(maxsize=64)
        self._thread = None
        if self.enabled:
            self._thread = threading.Thread(target=self._writer, daemon=True)
            self._thread.start()
            log.info("FaceHarvest zapnut → %s (interval %.1fs, strop %d/den)",
                     self._dir, self._interval_s, self._max_day)

    # ── veřejné API ──────────────────────────────────────────────────────

    def offer(self, track_id, crop, emb, box, guess=None, conf=0.0):
        """Nabídne jeden obličej ke sběru. Nikdy nevyhazuje a nikdy neblokuje.

        crop  — zarovnaný 112×112 RGB výřez (z `aligned_crop_lm`, NE syrový box)
        emb   — embedding téhož obličeje (512,)
        box   — normalizovaný [x1,y1,x2,y2] kvůli ploše
        guess — aktuální tip rozpoznávání (předvyplní se při označování)
        """
        if not self.enabled or crop is None or emb is None:
            return
        try:
            self._offer_inner(track_id, crop, emb, box, guess, conf)
        except Exception as e:          # sběr dat NIKDY nesmí shodit rozpoznávání
            log.debug("offer selhal: %s", e)

    # ── vnitřek ──────────────────────────────────────────────────────────

    def _roll_day(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        if today != self._day:
            self._day = today
            self._day_count = self._count_today()
        hour = now.strftime("%Y-%m-%d %H")
        if hour != self._hour:
            self._hour = hour
            self._hour_count = 0
        return self._day

    def _count_today(self) -> int:
        p = self._dir / self._day / "meta.jsonl"
        if not p.exists():
            return 0
        try:
            with open(p, "rb") as f:
                return sum(1 for _ in f)
        except Exception:
            return 0

    def _offer_inner(self, track_id, crop, emb, box, guess, conf):
        now = time.time()
        self._roll_day()
        if self._hour_count >= self._max_hour:
            return self._reject('strop_hodina')
        if self._day_count >= self._max_day:
            return self._reject('strop_den')

        bw = max(0.0, box[2] - box[0]); bh = max(0.0, box[3] - box[1])
        if bw * bh < self._min_area:
            return self._reject('plocha', bw*bh)

        st = self._tracks.get(track_id)
        if st is None or now - st["last"] > self._session_gap_s:
            # nová session — souvislý výskyt jednoho člověka
            st = {"id": f"{datetime.now().strftime('%H%M%S')}_t{track_id}",
                  "last": now, "saved": 0, "embs": [], "next_at": 0.0}
            self._tracks[track_id] = st
        st["last"] = now

        if st["saved"] >= self._max_session:
            return self._reject('strop_session')
        if now < st["next_at"]:
            return self._reject('interval')

        # ── brána kvality ────────────────────────────────────────────────
        # Jas se měří na STŘEDU cropu, ne na celém: vypálené okno v pozadí
        # vytáhne průměr nahoru, i když je tvář černá (doloženo 8.8. —
        # na tuhle chybu jsem během ladění dvakrát naletěl).
        g = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        face = g[28:96, 30:82]
        _b = float(face.mean())
        if _b < self._min_bright:
            return self._reject('jas', _b)
        sharp = float(cv2.Laplacian(face, cv2.CV_64F).var())
        if sharp < self._min_sharp:
            return self._reject('ostrost', sharp)

        # ── filtr rozmanitosti ───────────────────────────────────────────
        # Bez něj by z jednoho stání vzniklo 40 skoro shodných snímků, které
        # galerii nijak nerozšíří (jedna póza = jeden ostrůvek).
        e = np.asarray(emb, np.float32)
        n = float(np.linalg.norm(e))
        if n < 1e-6:
            return self._reject('nulovy_embedding')
        e = e / n
        if self._neg is not None and len(self._neg):
            sims = self._neg @ e
            k = min(3, len(sims))
            if float(np.mean(np.partition(sims, -k)[-k:])) >= self._neg_thr:
                return self._reject('falesna_detekce')
        if st["embs"]:
            if float(np.max(np.asarray(st["embs"]) @ e)) > 1.0 - self._diversity:
                return self._reject('rozmanitost')
        if self._recent:
            if float(np.max(np.asarray(self._recent) @ e)) > 1.0 - self._diversity:
                return self._reject('rozmanitost_globalni')
        st["embs"].append(e)
        self._recent.append(e)
        st["saved"] += 1
        st["next_at"] = now + self._interval_s
        self._day_count += 1
        self._hour_count += 1

        rec = {
            "session": st["id"],
            "n": st["saved"],
            "ts": round(now, 2),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "track": int(track_id),
            "guess": guess if guess and guess not in ("Unknown", "...", "?", "") else None,
            "conf": round(float(conf), 3),
            "area": round(bw * bh, 5),
            "jas": round(float(face.mean()), 1),
            "ostrost": round(sharp, 1),
            "emb": base64.b64encode(e.astype(np.float16).tobytes()).decode("ascii"),
            "label": None,          # doplní uživatel při označování
        }
        try:
            self._q.put_nowait((self._day, f"{st['id']}_{st['saved']:03d}.jpg",
                                crop.copy(), rec))
        except queue.Full:
            # radši vynechat vzorek než zdržet main loop (na kterém visí kamera)
            self._day_count -= 1
            self._hour_count -= 1
            st["saved"] -= 1


    def _reject(self, reason, val=None):
        """Zaznamená důvod odmítnutí. Souhrn 1× za 60 s do logu."""
        d = self._rej.setdefault(reason, [0, None])
        d[0] += 1
        if val is not None:
            d[1] = val if d[1] is None else max(d[1], val)
        now = time.time()
        if now - self._rej_at > 60.0:
            self._rej_at = now
            if self._rej:
                log.info("sběr — odmítnuto za minutu: %s (uloženo dnes %d)",
                         {k: (v[0] if v[1] is None else f"{v[0]}x max={v[1]:.3f}")
                          for k, v in self._rej.items()}, self._day_count)
            self._rej = {}
        return None

    def _writer(self):
        while True:
            item = self._q.get()
            if item is _STOP:
                return
            day, fname, crop, rec = item
            try:
                d = self._dir / day
                d.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(d / fname), cv2.cvtColor(crop, cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_JPEG_QUALITY, 92])
                rec["file"] = fname
                with open(d / "meta.jsonl", "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception as e:
                log.warning("zápis vzorku selhal: %s", e)


# ── čtení pro označovací UI ──────────────────────────────────────────────

def load_sessions(root="data/harvest", only_unlabeled=True,
                  gap_s=180.0, sim=0.45, days=14, max_block=1500) -> list:
    """Načte nasbírané vzorky SESKUPENÉ k označení.

    ⚠️ NESLUČUJE podle `session` (= track_id při sběru). Track ID se tříští —
    `_assign_track_ids` přiděluje nové ID při posunu boxu > 0.15 nebo při
    výpadku detekce, takže z jednoho průchodu pokojem vznikne i deset
    „session". Naměřeno 9.8.: 68 skupin na 124 snímků, medián 1 snímek na
    skupinu — což ruší celý smysl skupinového označování.

    Slučuje se proto AŽ PŘI ČTENÍ, podle dvou věcí zároveň:
      • blízkost v čase (`gap_s`)
      • podobnost embeddingu k libovolnému členu skupiny (`sim`, max-linkage;
        sousední framy téže osoby mají shodu 0.8+, takže 0.45 je bezpečné)
    Data na disku zůstávají syrová — sloučení je jen pohled, dá se přeladit
    bez ztráty čehokoli.
    """
    rows = []
    rootp = Path(root)
    if not rootp.exists():
        return []
    # ⚠️ Shlukování je O(n²) v paměti: 3 000 snímků v jednom bloku = 36 MB,
    # 12 000 = 576 MB a 8 s. Bez omezení by stránka po pár týdnech sběru
    # přestala jít otevřít. Proto: jen posledních `days` dní a bloky větší
    # než `max_block` se pro shlukování PROŘEDÍ (na disku zůstane vše).
    _dirs = sorted([d for d in rootp.iterdir() if d.is_dir()])
    if days:
        _dirs = _dirs[-int(days):]
    for day in _dirs:
        mp = day / "meta.jsonl"
        if not mp.is_file():
            continue
        for line in open(mp, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if only_unlabeled and r.get("label"):
                continue
            r["_day"] = day.name
            rows.append(r)
    if not rows:
        return []
    rows.sort(key=lambda r: r.get("ts", 0))

    for r in rows:
        e = np.frombuffer(base64.b64decode(r["emb"]), np.float16).astype(np.float32)
        n = float(np.linalg.norm(e))
        r["_e"] = e / n if n > 1e-6 else e

    # ⚠️ NEslučovat hladově „přes nejbližšího souseda" (max-linkage) — ŘETĚZÍ.
    # Doloženo 9.8.: A~B, B~C, ale A≠C → jedna skupina spolkla 111 snímků
    # DVOU lidí (rozpad 81+30, mezi shluky 0.297 vs uvnitř 0.55/0.60).
    # Kdyby to uživatel označil jedním jménem, otrávil by galerii — tedy přesně
    # to, čemu se celý den vyhýbáme. Proto: časové bloky + shlukování
    # PRŮMĚRNOU vazbou, která řetězení nepodléhá.
    blocks: list = []
    for r in rows:
        if blocks and r["_day"] == blocks[-1][-1]["_day"] \
                and r["ts"] - blocks[-1][-1]["ts"] <= gap_s:
            blocks[-1].append(r)
        else:
            blocks.append([r])

    groups: list = []
    for blk in blocks:
        if len(blk) == 1:
            parts = [blk]
        else:
            # ⚠️ Prořeďování se smí týkat jen SHLUKOVÁNÍ, ne výstupu: kdyby
            # vynechané snímky vypadly úplně, ve webu by se nikdy neobjevily
            # a zůstaly by navždy neoznačené (tichá ztráta dat).
            rest = []
            if len(blk) > max_block:
                step = len(blk) / float(max_block)
                keep = {int(i * step) for i in range(max_block)}
                rest = [r for i, r in enumerate(blk) if i not in keep]
                blk = [r for i, r in enumerate(blk) if i in keep]
            E = np.array([r["_e"] for r in blk])
            D = np.clip(1.0 - E @ E.T, 0.0, 2.0)
            np.fill_diagonal(D, 0.0)
            try:
                from scipy.cluster.hierarchy import fcluster, linkage
                from scipy.spatial.distance import squareform
                lab = fcluster(linkage(squareform(D, checks=False), "average"),
                               1.0 - sim, "distance")
            except Exception:
                lab = np.ones(len(blk), int)   # bez scipy radši nešlukovat
            parts = [[r for r, l in zip(blk, lab) if l == u] for u in sorted(set(lab))]
            if rest:
                # vynechané přiřadit k nejbližšímu shluku (podle centroidu)
                cents = []
                for prt in parts:
                    c = np.mean([r["_e"] for r in prt], axis=0)
                    cents.append(c / (np.linalg.norm(c) + 1e-9))
                C = np.array(cents)
                for r in rest:
                    parts[int(np.argmax(C @ r["_e"]))].append(r)
                for prt in parts:
                    prt.sort(key=lambda r: r.get("ts", 0))
        for part in parts:
            groups.append({
                "day": part[0]["_day"], "session": part[0]["session"],
                "files": [r.get("file") for r in part],
                "first": part[0]["time"], "last": part[-1]["time"],
                "label": part[0].get("label"),
                "_guesses": [r["guess"] for r in part if r.get("guess")],
                "_sessions": {r["session"] for r in part},
            })

    res = []
    for g in groups:
        gs = g.pop("_guesses")
        g["sessions"] = sorted(g.pop("_sessions"))   # kvůli set_label
        g["tip"] = max(set(gs), key=gs.count) if gs else None
        g["pocet"] = len(g["files"])
        res.append(g)
    res.sort(key=lambda x: (x["day"], x["first"]), reverse=True)
    return res


def set_label(day: str, session: str, label: str, root="data/harvest") -> int:
    """Označí celou session. Vrací počet dotčených vzorků.

    Přepisuje meta.jsonl na místě — snímky se NEMAŽOU ani nepřesouvají
    (jsou jediný auditní materiál; enrollment okno je mazalo a přišli jsme
    tím 8.8. o možnost zkontrolovat, co se do galerie dostalo).
    """
    p = Path(root) / day / "meta.jsonl"
    if not p.exists():
        return 0
    rows = []
    n = 0
    for line in open(p, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("session") == session:
            r["label"] = label
            n += 1
        rows.append(r)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return n


def set_label_files(day: str, files: list, label: str, root="data/harvest") -> int:
    """Označí konkrétní SOUBORY (ne celou `session`).

    Skupina po sloučení může přesahovat desítky původních track_id, takže
    klíčovat označení podle `session` by nestačilo. Snímky se nemažou ani
    nepřesouvají — přepíše se jen pole `label`.
    """
    p = Path(root) / day / "meta.jsonl"
    if not p.exists():
        return 0
    want = set(files)
    rows, n = [], 0
    for line in open(p, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("file") in want:
            r["label"] = label
            n += 1
        rows.append(r)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return n


def split_group(day: str, files: list, k: int = 2, root="data/harvest") -> list:
    """Rozdělí skupinu na k podskupin podle podoby.

    Seskupení je NÁVRH, ne pravda — embedding odliší dva lidi jen s malým
    odstupem (změřeno 8.8.), takže uživatel musí mít možnost skupinu rozpadnout,
    když v náhledech uvidí dva lidi. Vrací seznam seznamů souborů.
    """
    p = Path(root) / day / "meta.jsonl"
    if not p.exists():
        return [files]
    by = {}
    for line in open(p, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("file") in set(files):
            by[r["file"]] = r
    order = [f for f in files if f in by]
    if len(order) < k * 2:
        return [order]
    E = []
    for f in order:
        e = np.frombuffer(base64.b64decode(by[f]["emb"]), np.float16).astype(np.float32)
        E.append(e / (np.linalg.norm(e) + 1e-9))
    E = np.array(E)
    try:
        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import squareform
        D = np.clip(1.0 - E @ E.T, 0.0, 2.0)
        np.fill_diagonal(D, 0.0)
        lab = fcluster(linkage(squareform(D, checks=False), "average"), k, "maxclust")
    except Exception:
        return [order]
    return [[f for f, l in zip(order, lab) if l == u] for u in sorted(set(lab))]


def delete_samples(day: str, files: list, root="data/harvest") -> int:
    """Vyřadí vzorky (falešné detekce — ventilátor, zátylek, kus nábytku).

    NEMAŽE trvale: přesune je do `<den>/_kos/` a odstraní z `meta.jsonl`.
    Vzorek má ~6 kB, takže koš nic nestojí, a překlik by jinak nenávratně
    zahodil dobrý snímek. `_kos` je uvnitř denní složky, takže ho
    `load_sessions` (které iteruje jen denní adresáře) nikdy nenačte.
    """
    d = Path(root) / day
    p = d / "meta.jsonl"
    if not p.exists():
        return 0
    want = set(files)
    kos = d / "_kos"
    kos.mkdir(exist_ok=True)
    rows, n = [], 0
    for line in open(p, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("file") in want:
            n += 1
            src = d / r["file"]
            if src.is_file():
                try:
                    os.replace(src, kos / r["file"])
                except Exception:
                    pass
            continue                      # z meta.jsonl vypadne
        rows.append(r)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, p)
    return n


def build_negatives(root="data/harvest", out="data/known_faces_negative.pkl",
                    label="neni_tvar", keep=80) -> int:
    """Postaví NEGATIVNÍ galerii z označených falešných detekcí.

    Doloženo 10.8.: SCRFD detekuje kulatou mřížku ventilátoru jako obličej —
    445 falešných detekcí za dvě hodiny, tedy 53 % veškerého sběru. Ty pak
    (a) zaplaví označování, (b) generují `unknown_person` a náladu „worried".

    Falešné detekce téhož objektu tvoří jeden soudržný shluk (vnitřní
    podobnost 0.555), takže jdou odfiltrovat podobnostně: práh 0.65 zachytí
    98.9 % ventilátorů a falešně zahodí jen 2.3 % skutečných tváří.

    Vybírá `keep` nejrozmanitějších vzorků (farthest-point), ať je porovnání
    levné a shluk přitom pokrytý.
    """
    import pickle
    items = load_labeled_embeddings(root, with_meta=True).get(label)
    if not items:
        # load_labeled_embeddings vynechává 'neni_tvar' (není to osoba) —
        # projdeme meta.jsonl přímo
        items = []
        rootp = Path(root)
        for day in sorted(rootp.iterdir()):
            mp = day / "meta.jsonl"
            if not mp.is_file():
                continue
            for line in open(mp, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("label") == label:
                    e = np.frombuffer(base64.b64decode(r["emb"]),
                                      np.float16).astype(np.float32)
                    items.append({"emb": e})
    if not items:
        return 0
    X = np.array([it["emb"] for it in items], np.float32)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    if len(X) > keep:
        sel = [int(np.argmax((X @ X.T).sum(1)))]
        while len(sel) < keep:
            d = (X @ X[sel].T).max(1)
            d[sel] = 9
            sel.append(int(np.argmin(d)))
        X = X[sel]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        pickle.dump([x for x in X], f)
    return len(X)


def check_label(day: str, files: list, label: str, root="data/harvest") -> dict:
    """Ověří, jestli štítek sedí k tomu, co už je označené.

    Doloženo 10.8.: uživatel se ukliknul a označil 66 vlastních snímků jako
    cizí jméno. Měření to odhalilo (jeho vzorky skórovaly výš proti jeho galerii
    než proti její), ale až zpětně. Tahle kontrola to chytne rovnou při kliknutí.

    NEBLOKUJE — jen vrátí varování. Embedding dvě osoby odlišuje jen s malým
    odstupem, takže tvrdé odmítnutí by bránilo i správným štítkům.
    """
    out = {"ok": True, "warn": None}
    if label in ("neni_tvar", "_vyrazeno"):
        return out
    known = load_labeled_embeddings(root)
    known = {k: v for k, v in known.items() if k != label and len(v) >= 8}
    if not known:
        return out

    by = {}
    rootp = Path(root) / day
    mp = rootp / "meta.jsonl"
    if not mp.is_file():
        return out
    want = set(files)
    E = []
    for line in open(mp, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("file") in want:
            e = np.frombuffer(base64.b64decode(r["emb"]), np.float16).astype(np.float32)
            E.append(e / (np.linalg.norm(e) + 1e-9))
    if len(E) < 3:
        return out
    E = np.array(E)

    def score(X):
        X = np.asarray(X, np.float32)
        X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
        s = E @ X.T
        return float(np.median(np.sort(s, axis=1)[:, -3:].mean(1)))

    sc = {n: score(v) for n, v in known.items()}
    mine = load_labeled_embeddings(root).get(label)
    sc_mine = score(mine) if mine is not None and len(mine) >= 8 else None
    best, best_s = max(sc.items(), key=lambda x: x[1])
    out["skore"] = {**{k: round(v, 3) for k, v in sc.items()},
                    **({label: round(sc_mine, 3)} if sc_mine is not None else {})}
    if sc_mine is None or best_s > sc_mine + 0.02:
        out["ok"] = False
        out["warn"] = (f"Tahle skupina se víc podobá osobě „{best}“ "
                       f"({best_s:.2f})" +
                       (f" než „{label}“ ({sc_mine:.2f})." if sc_mine is not None
                        else f", a pro „{label}“ zatím nemáme s čím porovnat.") +
                       " Opravdu uložit?")
        out["navrh"] = best
    return out


def known_names(faces_path="data/known_faces.pkl") -> list:
    """Jména z galerie — nabídnou se jako tlačítka při označování."""
    try:
        import pickle
        with open(faces_path, "rb") as f:
            return sorted(pickle.load(f).keys())
    except Exception:
        return []


def load_labeled_embeddings(root="data/harvest", with_meta=False) -> dict:
    """Označené vzorky jako vstup do galerie.

    `with_meta=False` → {jméno: [embedding, ...]} (zpětně kompatibilní)
    `with_meta=True`  → {jméno: [{"emb", "ts", "time", "hodina", "day",
                                  "file", "jas"}, ...]}

    ⚠️ Meta se NESMÍ zahazovat: celý smysl průběžného sběru je pokrýt RŮZNÉ
    DENNÍ DOBY (galerie z jednoho sezení dává živě ~24 % proti 86 % v
    leave-one-out). Bez času u vzorku by při stavbě galerie nešlo vyvážit
    ráno/poledne/večer a skončili bychom zase u jednoho světla.
    """
    out: dict = {}
    rootp = Path(root)
    if not rootp.exists():
        return out
    for day in sorted(rootp.iterdir()):
        mp = day / "meta.jsonl"
        if not mp.is_file():
            continue
        for line in open(mp, encoding="utf-8"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            lab = r.get("label")
            if not lab or lab in ("nikdo", "neni_tvar"):
                continue
            e = np.frombuffer(base64.b64decode(r["emb"]), np.float16).astype(np.float32)
            if with_meta:
                out.setdefault(lab, []).append({
                    "emb": e, "ts": r.get("ts"), "time": r.get("time"),
                    "hodina": int(str(r.get("time", "  ")).split()[-1][:2] or 0)
                              if r.get("time") else None,
                    "day": day.name, "file": r.get("file"), "jas": r.get("jas"),
                })
            else:
                out.setdefault(lab, []).append(e)
    return out


def coverage(root="data/harvest", full=False) -> dict:
    """Kolik označených vzorků má každá osoba v které části dne.

    Slouží ke kontrole, že galerie nestojí zase na jednom světle — a k výběru,
    které vzorky do galerie vzít (vyvážit ráno/den/večer místo prostého
    „prvních 80").
    """
    def cast(h):
        if h is None: return "?"
        if h < 10: return "rano"
        if h < 16: return "den"
        if h < 21: return "vecer"
        return "noc"
    res: dict = {}
    for name, items in load_labeled_embeddings(root, with_meta=True).items():
        d = res.setdefault(name, {"rano": 0, "den": 0, "vecer": 0, "noc": 0, "?": 0})
        for it in items:
            d[cast(it["hodina"])] += 1
    if not full:
        return res

    # Denní doba nestačí. Uživatel (10.8.): jedna osoba zatím jen seděla před PC,
    # nemá žádné snímky před oknem a z větší vzdálenosti." Přesně tam přitom
    # rozpoznávání selhává — galerie musí pokrýt i ODSTUP a PROTISVĚTLO.
    idx = {}
    rootp = Path(root)
    if rootp.exists():
        for day in sorted(rootp.iterdir()):
            mp = day / "meta.jsonl"
            if not mp.is_file():
                continue
            for line in open(mp, encoding="utf-8"):
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("file"):
                    idx[r["file"]] = r
    for name, items in load_labeled_embeddings(root, with_meta=True).items():
        d = res[name]
        d.update({"blizko": 0, "stredne": 0, "daleko": 0,
                  "tmava_tvar": 0, "svetla_tvar": 0})
        for it in items:
            r = idx.get(it.get("file")) or {}
            a = r.get("area")
            if a is not None:
                d["blizko" if a >= 0.008 else
                  ("stredne" if a >= 0.003 else "daleko")] += 1
            j = it.get("jas")
            if j is not None:
                d["tmava_tvar" if j < 90 else "svetla_tvar"] += 1
    return res
