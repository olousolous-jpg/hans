"""Funkce přesunuté z `scripts/hans_art.py` (ROZDELENI_PRIKAZU_V1).

Text funkcí je beze změny; jména původního modulu se čtou přes `_ha.` až při
volání. Původní modul si funkce bere zpět importem, takže dosavadní importy platí.
Import původního modulu je na KONCI souboru (kruhový import oběma směry).
"""
from __future__ import annotations

from typing import Optional
import json
import os
import sqlite3
import time
import urllib.request
import uuid

def _home_source(db_path: str) -> str:
    """Zdrojový text pro CHAT/prozaické použití: preferuj syntetizovaný 'home_model',
    fallback = spojené pohledy z fotek + fakta. '' když nic."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content FROM place_facts WHERE category='home_model' "
            "ORDER BY updated_ts DESC LIMIT 1").fetchone()
        if row and (row["content"] or "").strip():
            conn.close()
            return row["content"].strip()
        rows = conn.execute(
            "SELECT category, content FROM place_facts "
            "WHERE category != 'home_model' ORDER BY category, id").fetchall()
        conn.close()
        parts = [r["content"].strip() for r in rows if (r["content"] or "").strip()]
        return "\n".join(parts)
    except Exception as e:
        _ha._log.warning("art: _home_source selhal: %s", e)
        return ""


def _home_paint_source(db_path: str) -> str:
    """Zdroj pro VĚRNÝ render. PRIORITA = autoritativní fakta od uživatele
    (room/layout/window/door/neighbor/note — přesné rozložení a barvy); fotky
    (mental_map) doplní vizuální detail. Fallback home_model."""
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        urows = conn.execute(
            "SELECT content FROM place_facts WHERE category IN "
            "('room','layout','window','door','neighbor','note') ORDER BY category, id"
        ).fetchall()
        mrows = conn.execute(
            "SELECT content FROM place_facts WHERE category='mental_map' ORDER BY id"
        ).fetchall()
        conn.close()
        user = [(r["content"] or "").strip() for r in urows if (r["content"] or "").strip()]
        views = [(r["content"] or "").strip() for r in mrows if (r["content"] or "").strip()]
        parts = []
        if user:
            parts.append("AUTHORITATIVE layout of my living room, from my vantage "
                         "point — follow this precisely (Czech):\n- " + "\n- ".join(user))
        if views:
            parts.append("Extra visual detail from photos (colors, objects):\n- "
                         + "\n- ".join(views))
        if parts:
            return "\n\n".join(parts)
    except Exception as e:
        _ha._log.warning("art: _home_paint_source selhal: %s", e)
    return _ha._home_source(db_path)


def paint_home(config: dict, diary_db_path: str) -> Optional[tuple]:
    """Hans namaluje svůj obývák VĚRNĚ podle fotek (konkrétní popisy z mental_map,
    jedna realistická scéna). Loguje do galerie jako 'home'.
    Vrací (rel_path, caption) nebo None. Nikdy nehází."""
    text = _ha._home_paint_source(diary_db_path)
    if not text:
        _ha._log.warning("art: žádný model domova (place_facts prázdné) — skip")
        return None
    title = "Můj domov"
    scene_intro = "%s\n\n" % text
    res = _ha._render_image(config, title, text, diary_db_path,
                        en_fallback="a quiet home interior seen from within a "
                        "room, view from where one stands, soft natural light",
                        scene_system=_ha._HOME_SCENE_SYSTEM, scene_intro=scene_intro)
    if not res:
        _ha._log.warning("art: domov se nevyrenderoval — retry příště")
        return None
    rel_path, prompt, vision_desc = res
    caption = _ha._evaluate_artwork(config, diary_db_path, title, text, vision_desc,
                                source_label="svým domovem, jak si ho představuje")
    _ha._derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "home",
                         "vision": vision_desc,
                         "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log home artwork failed: %s", e)
    _ha._log.info("art: Hans namaloval svůj domov → %s", rel_path)
    return rel_path, caption


def render_home_now(config: dict, diary_db_path: str) -> Optional[tuple]:
    """Tenký veřejný wrapper pro chat (/misto obraz) — IMAGINOVANÝ z textu."""
    return _ha.paint_home(config, diary_db_path)


def _pick_home_photo(config: dict) -> str:
    """Vyber reprezentativní fotku obýváku z drop-folderu — preferuj 'hansuv pohled'
    (široký záběr z Hansova místa), jinak první."""
    pd = (config.get("place", {}) or {}).get("photo_dir") or os.path.join("data", "room_photos")
    if not os.path.isdir(pd):
        return ""
    files = [f for f in sorted(os.listdir(pd)) if f.lower().endswith(_ha._PHOTO_EXT)]
    for key in ("hansuv pohled", "pohled"):
        for f in files:
            if key in f.lower():
                return os.path.join(pd, f)
    return os.path.join(pd, files[0]) if files else ""


def paint_home_from_photo(config: dict, diary_db_path: str,
                          photo_path: str = "", denoise: float = None) -> Optional[tuple]:
    """Přemaluj REÁLNOU fotku Hansova pohledu na pokoj do uměleckého stylu (img2img,
    nízký denoise → kompozice zůstane z fotky). Nejvěrnější varianta. Loguje do
    galerie jako 'home_photo'. Vrací (rel_path, caption) nebo None. Nikdy nehází."""
    try:  # OLLAMA_GAME_MODE_V1 — nezabírat VRAM za hry
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            _ha._log.info("art: herní mód — img2img render odložen")
            return None
    except Exception:
        pass
    ckpt = _ha._ckpt(config)
    if not ckpt:
        _ha._log.warning("art: image_model nenastaven — skip")
        return None
    base = _ha._comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception as e:
        _ha._log.warning("art: ComfyUI nedostupný (%s) — odloženo", e)
        return None
    photo = photo_path or _ha._pick_home_photo(config)
    if not photo or not os.path.exists(photo):
        _ha._log.warning("art: žádná fotka pokoje v drop-folderu — skip")
        return None
    tmp = _ha._resize_to_temp(photo)
    if not tmp:
        return None
    img_name = _ha._comfy_upload_image(base, tmp)
    try:
        os.remove(tmp)
    except Exception:
        pass
    if not img_name:
        _ha._log.warning("art: upload fotky do ComfyUI selhal")
        return None

    acfg = _ha._acfg(config)
    pcfg = (config.get("place", {}) or {}).get("paint", {}) or {}
    dn = float(denoise if denoise is not None else pcfg.get("denoise", 0.5))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2**31)
    client_id = uuid.uuid4().hex
    os.makedirs(_ha.ART_DIR, exist_ok=True)
    fname = "%d_muj_domov_foto.png" % int(time.time())
    dest = os.path.join(_ha.ART_DIR, fname)

    if not _ha._comfy_ready(config):
        return None
    loaded = _ha._ollama_loaded(config)
    _ha._ollama_unload(config, loaded)
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        wf = _ha._comfy_workflow_img2img(ckpt, _ha._HOME_STYLE_PROMPT, seed, img_name,
                                     dn, steps, cfg_s)
        _ha._log.info("art: home img2img start (denoise %.2f, %d steps) z %s",
                  dn, steps, os.path.basename(photo))
        pid = _ha._comfy_submit(base, wf, client_id)
        if pid:
            hist = _ha._comfy_wait(base, pid, timeout=rtimeout)
            img = _ha._first_image(hist) if hist else None
            if img and _ha._comfy_fetch_image(base, img, dest):
                ok = True
            elif not hist:
                _ha._log.warning("art: home img2img vypršel (timeout %ds)", rtimeout)
            else:
                _ha._log.warning("art: home img2img — bez obrázku")
        else:
            _ha._log.warning("art: home img2img submit selhal")
    except Exception as e:
        _ha._log.warning("art: home img2img selhal: %s", e)
    finally:
        _ha._comfy_free(config)
        if ok:
            vision_desc = _ha._describe_render(config, dest)
        _ha._ollama_warm(config, config.get("models", {}).get("dialog", "hans-czech:latest"))

    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    caption = _ha._evaluate_artwork(config, diary_db_path, "Můj domov",
                                "Přemaloval jsem svůj pokoj z vlastního pohledu.",
                                vision_desc,
                                source_label="svým pokojem, jak ho vidí a přemaloval")
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", "Můj domov", caption,
             json.dumps({"path": rel_path, "prompt": _ha._HOME_STYLE_PROMPT,
                         "source": "home_photo", "denoise": dn,
                         "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log home_photo artwork failed: %s", e)
    _ha._log.info("art: Hans přemaloval svůj pokoj z fotky → %s", rel_path)
    return rel_path, caption


def render_home_photo_now(config: dict, diary_db_path: str) -> Optional[tuple]:
    """Veřejný wrapper pro chat — VĚRNÝ přemalovaný pokoj z reálné fotky (img2img)."""
    return _ha.paint_home_from_photo(config, diary_db_path)


# ── HANS_DREAMS_V1 — Hans z vlastního popudu namaluje svůj sen ───────────────
def _jmena_ve_snu(config: dict, text: str) -> list:
    """HANS_DREAM_NAME_GLOSS_V1 — vlastni jmena ve snu + jejich anglicka glosa.

    Mapa zije v configu (`hans_art.dreams.name_glosses`), aby slo jmeno pridat
    bez zasahu do kodu. Kmen se hleda BEZ DIAKRITIKY a zkraceny: Hans pise
    jmena ruzne (v dennicich je i tvar bez delky) a cestina je sklonuje.
    """
    try:
        from scripts.config_io import bez_diakritiky as _bd
    except Exception:
        return []
    mapa = ((_ha._acfg(config).get("dreams") or {}).get("name_glosses") or {})
    if not mapa or not text:
        return []
    low = _bd(str(text)).lower()
    ven = []
    for jmeno, glosa in mapa.items():
        # HANS_DREAM_NAME_GLOSS_V2 (18. 9.) — kmen NEZKRACOVAT natvrdo na 4
        # znaky. Zmereno na 325 snech: `kola` (z `Kolac`[:4]) chytlo i
        # `cokoladove` a `silnicniho kola`, tedy 2 falesne z 51. Cely zaklad
        # `kolac` dal 49/49 a 0 falesnych. Ustrihne se nejvys JEDNA koncova
        # samohlaska (na sklonovani staci, protoze hledame podretezec).
        kmen = _bd(str(jmeno)).lower()
        if len(kmen) > 4 and kmen[-1] in "aeiouy":
            kmen = kmen[:-1]
        if len(kmen) >= 4 and kmen in low:
            ven.append((str(jmeno), str(glosa)))
    return ven


def _last_dream_painting_ts(db_path: str) -> float:
    """Kdy Hans naposledy namaloval sen (throttle). 0.0 = nikdy."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        rows = con.execute(
            "SELECT data FROM diary WHERE event_type='artwork' "
            "AND data LIKE '%\"source\": \"dream\"%' ORDER BY ts DESC LIMIT 1").fetchall()
        con.close()
        for (d,) in rows:
            try:
                return float(json.loads(d).get("painted_ts", 0)) or 0.0
            except Exception:
                pass
    except Exception as e:
        _ha._log.debug("art: last_dream_painting_ts failed: %s", e)
    return 0.0


def _sablony_snu() -> set:
    """HANS_DREAM_DEFER_V1 — doslovne sablony snu (historicke zaznamy je nesou)."""
    try:
        from scripts.hans_routine import _DREAM_SEEDS
        return {s.strip() for s in _DREAM_SEEDS}
    except Exception:
        return set()


def _recent_unpainted_dream(db_path: str, days: int = 4) -> Optional[dict]:
    """Nejnovější sen (deník event_type='dream') za posledních `days` dní, který
    Hans ještě nenamaloval (jeho ts není v žádném artwork.data.dream_ts)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        painted = set()
        for (d,) in con.execute(
                "SELECT data FROM diary WHERE event_type='artwork' "
                "AND data LIKE '%\"dream_ts\"%'").fetchall():
            try:
                dt = json.loads(d).get("dream_ts")
                if dt:
                    painted.add(int(dt))
            except Exception:
                pass
        cutoff = time.time() - days * 86400
        rows = con.execute(
            "SELECT ts, COALESCE(NULLIF(note,''), data) FROM diary "
            "WHERE event_type='dream' AND ts>=? "
            "AND COALESCE(NULLIF(note,''), data) IS NOT NULL "
            "ORDER BY ts DESC LIMIT 12", (cutoff,)).fetchall()
        con.close()
        for ts, text in rows:
            # HANS_DREAM_DEFER_V1 (14. 9.) — sablonu z `_DREAM_SEEDS` nemaluj:
            # neni to sen, ale nahradni veta z doby, kdy byl mozek dole
            # (12. 9. se tak namaloval „Sen" z doslovne sablony).
            if text and text.strip() in _ha._sablony_snu():
                continue
            if int(ts) not in painted and text and len(text.strip()) > 15:
                return {"ts": float(ts), "text": text.strip()}
    except Exception as e:
        _ha._log.debug("art: recent_unpainted_dream failed: %s", e)
    return None


def paint_dream(config: dict, diary_db_path: str) -> bool:
    """Sebeřízená tvorba: Hans namaluje obraz ke svému nedávnému snu (groundovaný
    v jeho dni). Throttle (min_interval_days) → zřídka. Vrací True při úspěchu.
    Deferral-safe — nikdy nehází, retry příště. Loguje do galerie jako 'dream'."""
    acfg = _ha._acfg(config)
    dcfg = acfg.get("dreams", {}) or {}
    if not dcfg.get("enabled", True):
        return False
    # HANS_DREAMS_PER_DREAM_V1 — maluj KAŽDÝ nový sen (idempotence dle dream_ts
    # zajistí 1×/sen); krátký odstup jen utlumí duplicitní sny z téže noci.
    interval_h = float(dcfg.get("min_interval_hours", 8))
    last = _ha._last_dream_painting_ts(diary_db_path)
    if last and (time.time() - last) < interval_h * 3600:
        return False  # krátký odstup proti duplicitám téže noci
    dream = _ha._recent_unpainted_dream(diary_db_path, int(dcfg.get("max_age_days", 4)))
    if not dream:
        return False  # nic čerstvého k namalování

    text = dream["text"]
    title = "Sen"
    scene_intro = "A dream (described in Czech):\n%s\n\n" % text
    # HANS_DREAM_NAME_GLOSS_V1 (18. 9.) — vlastni jmena se prekladala jako
    # obecna slova: jmeno medvida vyslo jako `pastries`/`cake` ve 4 ze 13 snu,
    # ktere ho zminuji (a `jezci` jako `thorny bushes`). Model dostane
    # anglickou glosu v zavorce — tentyz tvar, jaky uz pouziva cesta pri
    # ceskem uniku (`hint` v HANS_ART_CS_LEAK_V1).
    # ⛔ Samotny `_cs_leak` na tohle NESTACI a protahovat ho nelze: hleda
    #    ceske slovo, ktere v promptu ZUSTALO, kdezto tady se slovo prelozilo
    #    — jen spatne. Sedi jmeno, nesedi ucel.
    _glosy = _ha._jmena_ve_snu(config, text)
    if _glosy:
        scene_intro += (
            "PROPER NAMES in this dream — keep each name EXACTLY as written and\n"
            "never translate it; the gloss in brackets says what the thing is:\n"
            + "\n".join("- %s (%s)" % (j, g) for j, g in _glosy) + "\n\n")
        _ha._log.info("art: sen — glosa vlastnich jmen: %s",
                  ", ".join(j for j, _ in _glosy))
    # HANS_DREAM_SELF_FIGURE_V1 — vystupuje Hans v TOMHLE snu? Rozhoduje KOD
    # (klasifikator), ne instrukce v promptu — ta se zmerila jako nefunkcni.
    _sys = _ha._DREAM_SCENE_SYSTEM
    _ref, _wgt = "", 0.0
    _vaha = float(dcfg.get("pulid_weight", 0.45))
    if _vaha > 0 and _ha._snici_vystupuje(config, text):
        _tvar = _ha._aktualni_tvar(config)
        if _tvar:
            _sys, _ref, _wgt = _ha._DREAM_SCENE_SYSTEM_FIGURE, _tvar, _vaha
            _ha._log.info("art: sen — Hans v nem vystupuje, maluji ho podle %s", _tvar)
        else:
            _ha._log.info("art: sen — Hans v nem vystupuje, ale nemam jeho tvar "
                      "(data/avatar/vN/idle.png) → bez postavy")
    res = _ha._render_image(config, title, text, diary_db_path,
                        en_fallback="a surreal, dreamlike scene, soft and atmospheric",
                        scene_system=_sys, scene_intro=scene_intro,
                        # HANS_DREAM_NO_CONTINUITY_V1 (18. 9.) — SEN NENI SERIE.
                        # HANS_ART_CONTINUITY_V1 vklada do promptu CELY prompt
                        # predchoziho dila tez serie a zada nest jeden motiv dal.
                        # U serie zamernych obrazu je to smysl; u snu vada, protoze
                        # sen je zprava o JEDNE noci. Zmereno 18. 9.: 14 ze 77 snovych
                        # promptu preneslo >=2 vyrazna slova z predchoziho promptu,
                        # ktera ve vlastnim snu NEJSOU — sen ze 17. 9. o mecich
                        # a kompasu dostal knihovny a Fantomase ze snu 16. 9.
                        # `series` se v _render_image pouziva JEN k dohledani prev
                        # (`_last_in_series`), takze prazdna hodnota vypina jen tohle.
                        series="", ref_image=_ref, ref_weight=_wgt,
                        bez_zalohy=True)  # HANS_DREAM_NO_BARE_FALLBACK_V1
    if not res:
        _ha._log.warning("art: sen se nevyrenderoval — retry příště")
        return False
    rel_path, prompt, vision_desc = res
    caption = _ha._evaluate_artwork(config, diary_db_path, title, text, vision_desc,
                                source_label="svým snem z minulé noci")
    _ha._derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    # zápis do galerie s označením 'dream' + odkaz na zdrojový sen (idempotence)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "dream",
                         "vision": vision_desc,
                         "dream_ts": int(dream["ts"]), "painted_ts": time.time(),
                         "dream": text[:300]}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log dream artwork failed: %s", e)
    _ha._log.info('art: Hans namaloval svůj sen → %s', rel_path)
    return True


def _day_mood(config: dict) -> str:
    """Převažující nálada DNE (vážená dobou strávenou v každé náladě), ne okamžitá —
    nálada poskakuje, tak bereme, kde Hans strávil nejvíc času. Z system.logu.
    Vrací dominantní náladu (+ druhou, když je den proměnlivý), '' když nic."""
    import re
    from datetime import datetime
    path = (config.get("logging", {}) or {}).get("file", "data/system.log")
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400000))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    seq = []  # (epoch, new_mood) dnešní přechody
    for ln in tail.splitlines():
        if today not in ln or "hans_mood: Mood:" not in ln:
            continue
        m = re.search(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Mood: \S+ → (\S+)", ln)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
                seq.append((ts, m.group(2)))
            except Exception:
                pass
    if not seq:
        return ""
    seq.sort()
    now = time.time()
    dur = {}
    for i, (ts, mood) in enumerate(seq):
        end = seq[i + 1][0] if i + 1 < len(seq) else now
        dur[mood] = dur.get(mood, 0.0) + max(0.0, end - ts)
    ranked = sorted(dur.items(), key=lambda x: -x[1])
    dom = ranked[0][0]
    # proměnlivý den: druhá nálada má aspoň 60 % času té první → zmiň obě
    if len(ranked) > 1 and ranked[1][1] >= 0.6 * ranked[0][1]:
        return "%s (s přechody do %s)" % (dom, ranked[1][0])
    return dom


def _day_fragments(db_path: str) -> str:
    """Salientní dnešní zážitky z deníku (bez perceptuálního šumu) pro grounding."""
    bits = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        ph = ",".join("?" * len(_ha._DAY_EVENT_TYPES))
        rows = con.execute(
            "SELECT title, COALESCE(NULLIF(note,''), data) FROM diary "
            "WHERE date(ts,'unixepoch','localtime')=date('now','localtime') "
            "AND event_type IN (%s) "
            "AND COALESCE(NULLIF(note,''), data)<>'' "
            # HANS_SPONTANEOUS_TEMPLATE_MARK_V1/V2 (27.8.; V2 kotví na začátek
            # pole — `%"template"%` kdekoli by tiše zahodilo článek,
            # který o šablonách jen píše) — obraz dne se nesmí
            # opírat o šablonu. ⚠️ `%%` je nutné: celý řetězec jde přes `% ph`.
            "AND COALESCE(data,'') NOT LIKE '{\"template\":%%' "
            "ORDER BY COALESCE(importance,0) DESC, RANDOM() LIMIT 6" % ph,
            _ha._DAY_EVENT_TYPES).fetchall()
        con.close()
        for t, n in rows:
            frag = (t or "").strip()
            if n:
                frag = (frag + ": " + n.strip()) if frag else n.strip()
            if frag:
                bits.append("- " + frag[:120])
    except Exception as e:
        _ha._log.debug("art: day_fragments failed: %s", e)
    return "\n".join(bits[:6])


def _last_day_painting_ts(db_path: str) -> float:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT data FROM diary WHERE event_type='artwork' "
            "AND data LIKE '%\"source\": \"day\"%' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return float(json.loads(row[0]).get("painted_ts", 0)) or 0.0
    except Exception as e:
        _ha._log.debug("art: last_day_painting_ts failed: %s", e)
    return 0.0


def _last_home_painting_ts(db_path: str) -> float:
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=3.0)
        row = con.execute(
            "SELECT data FROM diary WHERE event_type='artwork' "
            "AND data LIKE '%\"source\": \"home%' ORDER BY ts DESC LIMIT 1").fetchone()
        con.close()
        if row:
            return float(json.loads(row[0]).get("painted_ts", 0)) or 0.0
    except Exception as e:
        _ha._log.debug("art: last_home_painting_ts failed: %s", e)
    return 0.0


def paint_day(config: dict, diary_db_path: str) -> bool:
    """Sebeřízená tvorba: Hans namaluje obraz vystihující svůj DEN a NÁLADU
    (symbolická atmosférická scéna). Deferral-safe. Galerie 'day'."""
    acfg = _ha._acfg(config)
    pcfg = acfg.get("day_painting", {}) or {}
    if not pcfg.get("enabled", True):
        return False
    frags = _ha._day_fragments(diary_db_path)
    if not frags or len(frags) < 30:
        return False  # málo materiálu na den
    mood = _ha._day_mood(config)
    title = "Můj den"
    # HANS_DAY_MOOD_VISUAL_V1 — náladu rozepiš na vizuální atmosféru a dej ji NAHORU,
    # ať dominuje (jinak ji den/SDXL přebijou do hezkého klidu).
    base = mood.split(" (")[0] if mood else ""
    vis = _ha._MOOD_VISUAL.get(base, "")
    mood_block = ("DOMINANT MOOD: %s — %s\n\n" % (mood, vis)) if vis else (
        ("Mood: %s\n\n" % mood) if mood else "")
    source = mood_block + "Today's moments (secondary motifs):\n" + frags
    scene_intro = "A person's day and mood:\n%s\n\n" % source
    res = _ha._render_image(config, title, source, diary_db_path,
                        en_fallback="a quiet everyday scene from domestic life",
                        scene_system=_ha._DAY_SCENE_SYSTEM, scene_intro=scene_intro,
                        series="day")
    if not res:
        _ha._log.warning("art: obraz dne se nevyrenderoval — retry příště")
        return False
    rel_path, prompt, vision_desc = res
    caption = _ha._evaluate_artwork(config, diary_db_path, title, source, vision_desc,
                                source_label="svým dnešním dnem")
    _ha._derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "day",
                         "vision": vision_desc,
                         "mood": mood, "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log day artwork failed: %s", e)
    _ha._log.info('art: Hans namaloval svůj den → %s', rel_path)
    return True

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_art as _ha  # noqa: E402
