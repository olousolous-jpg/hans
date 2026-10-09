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

def paint_person_from_photo(config: dict, diary_db_path: str, subject: str,
                            ref_path: str, style: str = "",
                            person_name: str = "",
                            force_scene: bool = False) -> Optional[tuple]:
    """HANS_ART_PERSON_LIKENESS_V3 — přemaluj REÁLNÝ portrét osoby do Hansova
    stylu (img2img, denoise ~0.5 → drží podobu). Vrací (rel_path, caption) nebo
    None (→ volající spadne na text-grounded malbu). Nikdy nehází."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    ckpt = _ha._ckpt(config)
    if not ckpt or not ref_path or not os.path.exists(ref_path):
        return None
    base = _ha._comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception:
        return None
    img_name = _ha._comfy_upload_image(base, ref_path)
    try:
        os.remove(ref_path)
    except Exception:
        pass
    if not img_name:
        return None

    nm = person_name or subject
    acfg = _ha._acfg(config)
    pcfg = (acfg.get("person_likeness", {}) or {})
    seed = uuid.uuid4().int % (2**31)
    client_id = uuid.uuid4().hex
    # HANS_ART_PULID_V1 — když je FLUX+PuLID zapnutý, zachovej PODOBU z ref fota a
    # slož NOVOU scénu z celého námětu (osoba NA MOTORCE ap.). Jinak legacy
    # img2img (drží kompozici → jen portrét, akce/scéna se ztratí).
    #
    # HANS_ART_PORTRAIT_IMG2IMG_V1 (4.8.) — obě cesty mají OPAČNÝ kompromis a
    # dosud rozhodoval jen config, takže PuLID přebil i prosté portréty:
    #   • img2img  = překreslí SKUTEČNOU fotku → drží podobu, ale drží i
    #     kompozici, takže scéna/akce se ztratí,
    #   • PuLID    = složí novou scénu, podobu drží jen „na první pohled".
    # Rozhoduje tedy ZADÁNÍ: nese-li námět kromě jména i akci/místo („Radecký
    # na motorce"), je potřeba scéna → PuLID; holé jméno („Bud Spencer") =
    # portrét → img2img. Denoise 0.70 (laděno naživo 4.8. na Bud Spencerovi:
    # 0.50 = přemalovaná fotka, 0.75 už posouvá rysy, 0.70 drží podobu
    # a přitom je to malba). `denoise` čte JEN img2img větev — PuLID jede na
    # `pulid_weight`, tahle hodnota mu nesahá do scén.
    # HANS_ART_POSTAVA_PULID_V1 — u postavy vždy scéna (i holé jméno): img2img
    # by jen překreslil civilní fotku herce.
    _wants_scene = force_scene or _ha._subject_beyond_name(subject, nm)
    _use_pulid = (bool(pcfg.get("use_pulid", False))
                  and bool(acfg.get("use_flux", False))
                  and (_wants_scene or not pcfg.get("portrait_img2img", True)))
    _ha._log.info("art: podoba '%s' → %s (%s)", nm,
              "FLUX+PuLID (scéna)" if _use_pulid else "img2img překreslení fotky",
              "námět nese akci/místo" if _wants_scene else "holé jméno = portrét")
    if _use_pulid:
        _flux_ckpt = acfg.get("flux_ckpt", "flux1-dev-fp8.safetensors")
        _intro = ("Subject to depict (described in Czech): %s\n\n"
                  "Write ONE vivid English image prompt of THIS scene. Name the "
                  "person and describe era-appropriate clothing, the action and "
                  "the setting. The exact face is supplied separately, so focus on "
                  "scene, pose, attire and atmosphere.\n\n" % subject)
        prompt = _ha._scene_prompt(config, subject, "", diary_db_path,
                               system=_ha._SUBJECT_SCENE_SYSTEM, source_intro=_intro)
        w = int(acfg.get("width", 896)); h = int(acfg.get("height", 1152))
    else:
        style_kw = (", in the style of %s" % style) if style else ""
        prompt = ("expressive painterly portrait of %s%s, oil painting, artistic "
                  "brushwork, rich detail, atmospheric lighting, masterful" %
                  (nm, style_kw))
    dn = float(pcfg.get("denoise", 0.5))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    os.makedirs(_ha.ART_DIR, exist_ok=True)
    fname = "%d_%s_podoba.png" % (int(time.time()), _ha._slug(subject))
    dest = os.path.join(_ha.ART_DIR, fname)

    if not _ha._comfy_ready(config):
        return None
    loaded = _ha._ollama_loaded(config)
    _ha._ollama_unload(config, loaded)
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        if _use_pulid:
            wf = _ha._comfy_workflow_flux_pulid(
                _flux_ckpt, prompt, seed, w, h,
                int(acfg.get("flux_steps", 20)),
                float(acfg.get("flux_guidance", 3.5)), img_name,
                float(pcfg.get("pulid_weight", 0.9)))
            _ha._log.info("art: podoba osoby FLUX+PuLID start (%s) — %.90s", nm, prompt)
        else:
            _pneg = _ha._person_negative(diary_db_path)
            wf = _ha._comfy_workflow_img2img(ckpt, prompt, seed, img_name, dn, steps,
                                         cfg_s, negative=_pneg)
            _ha._log.info("art: podoba osoby img2img start (%s, denoise %.2f) — neg: %.90s",
                      nm, dn, _pneg)
        pid = _ha._comfy_submit(base, wf, client_id)
        if pid:
            hist = _ha._comfy_wait(base, pid, timeout=rtimeout)
            img = _ha._first_image(hist) if hist else None
            if img and _ha._comfy_fetch_image(base, img, dest):
                ok = True
    except Exception as e:
        _ha._log.warning("art: podoba img2img selhal: %s", e)
    finally:
        _ha._comfy_free(config)
        if ok:
            vision_desc = _ha._describe_render(config, dest)
        _ha._ollama_warm(config,
                     config.get("models", {}).get("dialog", "hans-czech:latest"))
    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    title = subject[:80] if not style else ("%s (styl: %s)" % (subject, style))[:80]
    caption = _ha._evaluate_artwork(config, diary_db_path, title, subject,
                                vision_desc,
                                source_label="podle skutečné podoby osoby")
    _ha._derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "person",
                         "vision": vision_desc,
                         "denoise": dn, "painted_ts": time.time()},
                        ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log person artwork failed: %s", e)
    _ha._log.info("art: Hans namaloval podobu osoby '%s' → %s", nm, rel_path)
    return rel_path, caption


def paint_place_from_photo(config: dict, diary_db_path: str, subject: str,
                           ref_path: str, style: str = "",
                           place_name: str = "",
                           grounded: str = "") -> Optional[tuple]:
    """HANS_ART_PLACE_LIKENESS_V1 (29.8.) — přemaluj REÁLNOU FOTKU místa do
    Hansova stylu (img2img). Vrací (rel_path, caption) nebo None (→ volající
    spadne na text-grounded malbu). Nikdy nehází.

    PROČ VŮBEC: text sám nestačil. I když `HANS_ART_PLACE_APPEARANCE_V1` dostal
    do promptu „the two spires and palaces", FLUX to přečetl jako ŠPIČATÉ
    STŘECHY a namaloval pohádkový zámek (29.8., 299 s). Fotka tutéž informaci
    nese jednoznačně.

    ⛔ IP-ADAPTER JE VYZKOUŠENÝ A ZAMÍTNUTÝ (29.8., měřeno na Troskách):
    weight 0.75 dal fotorealistický letecký pohled s JEDNOU věží — druhou
    ztratil a přiopsal z fotky i nesouvisející chalupu. Je to přesně týž
    kompromis, jaký má u osob PuLID (`HANS_ART_PORTRAIT_IMG2IMG_V1`): volná
    kompozice, podoba jen „na první pohled". U holého názvu místa chceme
    OPAK — věrnost. Proto img2img.

    ⚠️ Cena, kterou to má: img2img zdědí ZÁBĚR fotky. Wikipedia má u Trosek
    i u Kosti letecký snímek → Hans maluje z ptačí perspektivy. U osob je to
    táž vlastnost a je přijatá.
    """
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    ckpt = _ha._ckpt(config)
    if not ckpt or not ref_path or not os.path.exists(ref_path):
        return None
    base = _ha._comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception:
        return None
    img_name = _ha._comfy_upload_image(base, ref_path)
    try:
        os.remove(ref_path)
    except Exception:
        pass
    if not img_name:
        return None

    nm = place_name or subject
    acfg = _ha._acfg(config)
    lcfg = (acfg.get("place_likeness", {}) or {})
    # anglický název do promptu (HANS_ART_EN_TITLE_V1) — SDXL na český nereaguje
    _wl = (config.get("curiosity", {}) or {}).get("wiki_lang", "cs")
    nm_en = _ha._en_name(nm, lang=_wl) or nm
    style_kw = (", in the style of %s" % style) if style else ""
    # HANS_ART_PLACE_PROMPT_V1 (29.8.) — ⚠️ U MÍSTA NESTAČÍ HOLÁ ŠABLONA.
    # Doloženo živým během: prompt „expressive painterly view of Hrad Trosky"
    # + fotka Trosek při denoise 0.60 dal VESNICI S KOSTELEM. Fotka strukturu
    # NEUŘÍDÍ sama — prompt ji přebije. (U osob šablona stačí, protože portrét
    # žádný obsah nepotřebuje: obličej je obličej. Místo potřebuje vědět, ŽE
    # jsou to dvě věže na skalách — jinak si model dosadí, co je běžnější.)
    # Proto se prompt staví z groundingu (`HANS_ART_PLACE_APPEARANCE_V1`) touž
    # cestou jako u text-grounded malby.
    prompt = ""
    if grounded:
        _intro = ("Place to depict (described in Czech):\n%s\n\n"
                  "A real photograph of this place is supplied separately and "
                  "will be repainted. Write ONE English SDXL prompt that names "
                  "the place and describes its PHYSICAL FORM — rock, towers, "
                  "ruins, materials, surroundings. Do not invent buildings that "
                  "are not described.\n\n" % grounded)
        try:
            prompt = _ha._scene_prompt(config, subject, grounded, diary_db_path,
                                   system=_ha._SUBJECT_SCENE_SYSTEM,
                                   source_intro=_intro,
                                   cs_subject=subject) or ""
        except Exception as _spe:
            _ha._log.debug("art: scene prompt místa selhal: %s", _spe)
    if not prompt:
        prompt = ("expressive painterly view of %s%s, oil painting, artistic "
                  "brushwork, rich detail, atmospheric lighting, masterful"
                  % (nm_en, style_kw))
    elif style_kw:
        prompt += style_kw
    dn = float(lcfg.get("denoise", 0.60))
    steps = int(acfg.get("steps", 28)); cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2**31)
    client_id = uuid.uuid4().hex
    os.makedirs(_ha.ART_DIR, exist_ok=True)
    fname = "%d_%s_podoba.png" % (int(time.time()), _ha._slug(subject))
    dest = os.path.join(_ha.ART_DIR, fname)

    if not _ha._comfy_ready(config):
        return None
    _ha._ollama_unload(config, _ha._ollama_loaded(config))
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    vision_desc = ""
    try:
        wf = _ha._comfy_workflow_img2img(ckpt, prompt, seed, img_name, dn, steps,
                                     cfg_s, negative=_ha._NEG_BASE)
        _ha._log.info("art: podoba místa img2img start (%s, denoise %.2f) — %.90s",
                  nm_en, dn, prompt)
        pid = _ha._comfy_submit(base, wf, client_id)
        if pid:
            hist = _ha._comfy_wait(base, pid, timeout=rtimeout)
            img = _ha._first_image(hist) if hist else None
            if img and _ha._comfy_fetch_image(base, img, dest):
                ok = True
    except Exception as e:
        _ha._log.warning("art: podoba místa img2img selhala: %s", e)
    finally:
        _ha._comfy_free(config)
        if ok:
            vision_desc = _ha._describe_render(config, dest)
        _ha._ollama_warm(config,
                     config.get("models", {}).get("dialog", "hans-czech:latest"))
    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    title = subject[:80] if not style else ("%s (styl: %s)" % (subject, style))[:80]
    caption = _ha._evaluate_artwork(config, diary_db_path, title, subject, vision_desc,
                                source_label="podle skutečné podoby místa")
    _ha._derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "place",
                         "vision": vision_desc,
                         "denoise": dn, "painted_ts": time.time()},
                        ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log place artwork failed: %s", e)
    _ha._log.info("art: Hans namaloval podobu místa '%s' → %s", nm, rel_path)
    return rel_path, caption


def paint_self(config: dict, diary_db_path: str, full_figure: bool = True,
               style: str = ""):
    """HANS_ART_SELF_V1 — Hans namaluje SÁM SEBE z vlastního avatar descriptoru
    (jeho vzhled: butler, frak…), volitelně jako CELOU POSTAVU. Řeší, že „namaluj
    sebe" nemá znamenat namalovat uživatele. Vrací (rel_path, caption) nebo None.
    Nikdy nehází. VRAM: unload LLM → render → warm."""
    try:
        from scripts.ollama_client import game_mode_on
        if game_mode_on():
            return None
    except Exception:
        pass
    try:
        from scripts.avatar_descriptor import latest_descriptor
        from scripts.avatar_render import build_prompt
    except Exception as e:
        _ha._log.warning("art: paint_self import: %s", e)
        return None
    desc = latest_descriptor(diary_db_path) or {
        "role": "english butler", "attire": "black tailcoat, white gloves, "
        "high collar", "age_look": "late 50s", "build": "tall, slim",
        "demeanor": "formal and reserved", "setting": "wood-panelled study"}
    framing = ("full body shot, full-length portrait, standing upright, the "
               "complete figure from head to shoes, entire body and legs "
               "visible, shoes visible, wide framing, distant full-length view"
               if full_figure else "portrait, upper body")
    prompt = build_prompt(desc, "dignified, poised, looking at viewer", framing)
    if style:
        prompt = prompt + ", in the style of " + str(style)
    ckpt = _ha._ckpt(config)
    if not ckpt:
        _ha._log.warning("art: paint_self — image_model nenastaven")
        return None
    acfg = _ha._acfg(config)
    scfg = (acfg.get("self_portrait", {}) or {})
    base = _ha._comfy_url(config)
    try:
        urllib.request.urlopen(f"{base}/system_stats", timeout=10).read()
    except Exception:
        return None
    steps = int(acfg.get("steps", 28))
    cfg_s = float(acfg.get("cfg", 6.5))
    seed = uuid.uuid4().int % (2 ** 31)
    client_id = uuid.uuid4().hex
    os.makedirs(_ha.ART_DIR, exist_ok=True)
    fname = f"{int(time.time())}_hans_self.png"
    dest = os.path.join(_ha.ART_DIR, fname)

    # HANS_ART_SELF_V1 — TEMPLATE = Hansův vytvořený avatar (img2img) → DRŽÍ jeho
    # vzhled. Bez avatara fallback na txt2img z descriptoru.
    # HANS_ART_SELF_LATEST_FACE_V1 (5.9.) — dosud tu bylo natvrdo
    # „data/avatar/v1/idle.png". `hans_avatar.face_image` je v configu PRAZDNE,
    # takze autoportrety kreslily podobu z v1, zatimco aktualni tvar je v3 —
    # cela prace na vyvoji avatara se do nich nepromitala. Logika „vezmi
    # nejnovejsi vN" uz existuje (`avatar_render.identity_reference`), jen ji
    # `paint_self` nevolal.
    avatar_path = ((config.get("hans_avatar", {}) or {}).get("face_image")
                   or _ha._aktualni_tvar(config)
                   or "data/avatar/v1/idle.png")
    img_name = None
    if os.path.exists(avatar_path):
        tmp = _ha._resize_to_temp(avatar_path)
        if tmp:
            img_name = _ha._comfy_upload_image(base, tmp)
            try:
                os.remove(tmp)
            except Exception:
                pass
    # rozměry: celá postava = na výšku (IP-Adapter dá novou kompozici z portrétní
    # reference, na rozdíl od img2img, který drží kompozici vstupu = portrét)
    w, h = (832, 1216) if full_figure else (1024, 1024)
    ipa_model = scfg.get("ipadapter_file",
                         "ip-adapter-plus_sdxl_vit-h.safetensors")
    ipa_clip = scfg.get("clip_vision",
                        "CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors")
    ipa_weight = float(scfg.get("ipadapter_weight", 0.75))

    if not _ha._comfy_ready(config):
        return None
    _ha._ollama_unload(config, _ha._ollama_loaded(config))
    rtimeout = int(acfg.get("render_timeout", 600))
    ok = False
    try:
        if img_name:
            # IP-ADAPTER — NOVÁ kompozice (celá postava) + PODOBA avatara
            wf = _ha._comfy_workflow_ipadapter(ckpt, prompt, seed, w, h, steps,
                                           cfg_s, img_name, ipa_model, ipa_clip,
                                           ipa_weight)
            _ha._log.info("art: paint_self IP-ADAPTER z avatara (w=%.2f, %dx%d) — %.80s",
                      ipa_weight, w, h, prompt)
        else:
            wf = _ha._comfy_workflow(ckpt, prompt, seed, w, h, steps, cfg_s)
            _ha._log.info("art: paint_self txt2img (bez avatara) — %.80s", prompt)
        pid = _ha._comfy_submit(base, wf, client_id)
        hist = _ha._comfy_wait(base, pid, timeout=rtimeout) if pid else None
        img = _ha._first_image(hist) if hist else None
        if img and _ha._comfy_fetch_image(base, img, dest):
            ok = True
    except Exception as e:
        _ha._log.warning("art: paint_self render selhal: %s", e)
    finally:
        _ha._comfy_free(config)
        _ha._ollama_warm(config, config.get("models", {}).get("dialog",
                                                          "hans-czech:latest"))
    if not ok:
        return None
    rel_path = os.path.join("data", "hans_art", fname)
    caption = "Má vlastní podoba" + (" — celá postava" if full_figure else "")
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute("INSERT INTO diary (ts, event_type, title, note, data) "
                   "VALUES (?,?,?,?,?)",
                   (time.time(), "artwork", "Autoportrét", caption,
                    json.dumps({"path": rel_path, "prompt": prompt,
                                "source": "self"}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.debug("art: paint_self log: %s", e)
    return (rel_path, caption)


def paint_subject(config: dict, diary_db_path: str, subject: str,
                  style: str = "", zadal: str = "", _retry: bool = False):
    """Hans namaluje obraz na LIBOVOLNÉ téma / dojem (např. z rozhovoru).
    style: volitelný umělecký styl („Salvador Dalí", „Bauhaus", „gotika") —
    grounduje se a vloží do promptu místo pevného ocasu (HANS_ART_STYLE_V4).
    Loguje do galerie jako source='subject'. Vrací (rel_path, caption) nebo None.
    Nikdy nehází. VRAM orchestrace uvnitř _render_image."""
    subject = (subject or "").strip()
    if not subject:
        return None
    style = (style or "").strip()
    title = subject[:80]
    if style:
        title = ("%s (styl: %s)" % (subject, style))[:80]
    # HANS_ART_PERSON_LIKENESS_V3 — je-li námět OSOBA, nejdřív zkus img2img
    # z reálného portrétu (drží podobu). Miss / bez fotky → text-grounded malba.
    if (_ha._acfg(config).get("person_likeness", {}) or {}).get("enabled", True):
        try:
            # HANS_ART_PULID_V1 — nejdřív hledej ZNÁMOU OSOBU v námětu (i když jsou
            # tam další entity, např. 'Harley-Davidson', které by jinak přebily);
            # osoba má přednost → PuLID zachová podobu + složí scénu z celého námětu.
            _ent = None
            try:
                from scripts.hans_entities import EntityStore
                _ent = EntityStore(config, diary_db_path).resolve(
                    subject, loose=True, etype="osoba")
            except Exception:
                _ent = None
            # HANS_ART_COMMON_NOUN_V1 — `_resolve_entity` při chybějící entitě
            # SÁM dohledá na Wikipedii a ULOŽÍ ji. U obecného slova tím do
            # paměti propašuje nesmysl („kočku" → „Kockums", švédská loděnice),
            # i když `_ground_subject` níž grounding správně přeskočí. Cesta
            # k podobě OSOBY beztak dává smysl jen u jmen → stejná brána.
            if not _ent and _ha._name_shaped(subject) and _ha._is_proper_name(subject, config):
                _ent = _ha._resolve_entity(config, diary_db_path, subject)
            # HANS_ART_PERSON_WIKI_LOOKUP_V1 (4.8.) — osobu, kterou Hans JEŠTĚ
            # NEZNÁ, dohledej na Wikipedii a teprve pak rozhodni o podobě.
            # Bez tohohle měla cesta k podobě přísnější vstup než samotný
            # grounding (ten Wikipedii umí) → u neznámé/překlepnuté osoby se
            # PuLID vůbec nespustil a FLUX maloval jen podle jména.
            # Doloženo 4.8.: „Bud Spencer" (v paměti nebyl) i „TerRence Hill"
            # (překlep) skončily jako generický muž, ačkoli cs.wikipedia má
            # u obou článek s portrétem.
            if ((not _ent or _ent.get("etype") != "osoba")
                    and _ha._name_shaped(subject)
                    and _ha._is_proper_name(subject, config)):
                _wt = _ha._wiki_capture_person(config, diary_db_path, subject)
                if _wt:
                    try:
                        from scripts.hans_entities import EntityStore as _ES2
                        _es2 = _ES2(config, diary_db_path)
                        # resolvuj podle OPRAVENÉHO titulu (překlep by minul),
                        # fallback na původní námět
                        _ent2 = (_es2.resolve(_wt, loose=True, etype="osoba")
                                 or _es2.resolve(subject, loose=True, etype="osoba"))
                        if _ent2:
                            _ent = _ent2
                    except Exception:
                        pass
            # HANS_ENTITY_POSTAVA_V1 (20.7.) — ZKUŠENO+ZAMÍTNUTO: rozšíření gate
            # na etype=='postava' (fiktivní postavy → img2img z Wiki obrázku)
            # dopadlo špatně — Wiki obrázek postavy je FOTKA HERCE (Rimmer→Chris
            # Barrie), ne postavy → podoba nesedí; Kryten (těžká maska) render
            # nedoběhl. Text-grounded je pro fikci lepší (aspoň doběhne).
            # Ponecháno jen `etype=='osoba'` (reálné osoby: Matka Tereza, kde
            # Wiki obrázek = ta osoba). etype='postava' klasifikace zůstává
            # (neškodná metadata), jen NEROUTUJE na img2img.
            # HANS_ART_POSTAVA_PULID_V1 (6. 10.) — zamítnutí výš platí pro
            # IMG2IMG. Přes FLUX+PuLID (tvář z fotky, scéna a kostým z textu)
            # podoba sedí — ověřeno zkušebním renderem a posouzeno uživatelem.
            # Větev je níž; bez tváře na referenci a při neúspěchu → text.
            # Mez: postava s maskou vyjde jako herec bez masky.
            if _ent and _ent.get("etype") == "osoba":
                _ref = _ha._fetch_person_ref(config, _ent, diary_db_path)
                if _ref:
                    _r = _ha.paint_person_from_photo(
                        config, diary_db_path, subject, _ref, style,
                        person_name=_ent.get("name", subject))
                    if _r:
                        return _r
                    _ha._log.info("art: podoba osoby nevyšla → text-grounded malba")
            else:
                _plc = (_ha._acfg(config).get("person_likeness", {}) or {})
                if (_plc.get("postava_pulid", True) and _plc.get("use_pulid", False)
                        and _ha._acfg(config).get("use_flux", False)):
                    _pent = _ent if (_ent and _ent.get("etype") == "postava") else None
                    if not _pent:
                        from scripts.hans_entities import EntityStore as _ES3
                        _pent = _ES3(config, diary_db_path).resolve(
                            subject, loose=True, etype="postava")
                    _pref = _ha._fetch_person_ref(config, _pent, diary_db_path) if _pent else None
                    if _pref and _ha._ref_ma_tvar(_pref):
                        _ha._log.info("art: HANS_ART_POSTAVA_PULID_V1 postava '%s' — "
                                  "tvář z fotky na Wikipedii", _pent.get("name"))
                        _r = _ha.paint_person_from_photo(
                            config, diary_db_path, subject, _pref, style,
                            person_name=_pent.get("name", subject),
                            force_scene=True)
                        if _r:
                            return _r
                        _ha._log.info("art: HANS_ART_POSTAVA_PULID_V1 podoba postavy "
                                  "nevyšla → malba podle textu")
                    elif _pent:
                        _ha._log.info("art: HANS_ART_POSTAVA_PULID_V1 postava '%s' — %s "
                                  "→ malba podle textu", _pent.get("name"),
                                  "na obrázku není tvář" if _pref else "bez obrázku")
                        if _pref:
                            try:
                                os.remove(_pref)
                            except Exception:
                                pass
        except Exception as _pe:
            _ha._log.debug("art: person-likeness cesta selhala: %s", _pe)
    # HANS_ART_SUBJECT_GROUNDING_V1 — ukotvi námět (kdo/co to je) PŘED renderem
    grounded = _ha._ground_subject(config, diary_db_path, subject)
    # HANS_ART_PLACE_LIKENESS_V1 — je-li námět MÍSTO, přemaluj jeho skutečnou
    # fotku (analogie person_likeness). Běží AŽ ZA groundingem schválně: ten
    # entitu vyhledá a uloží, takže tady stačí LEVNÉ čtení ze storu bez sítě
    # — a u obecného slova („kočku") se grounding přeskočí, takže se sem
    # nedostane ani tahle cesta (HANS_ART_COMMON_NOUN_V1 platí i pro místa).
    if (_ha._acfg(config).get("place_likeness", {}) or {}).get("enabled", True):
        try:
            from scripts.hans_entities import EntityStore
            _pent = EntityStore(config, diary_db_path).resolve(
                subject, loose=True, etype="místo")
            if _pent:
                _pnm = _pent.get("name", subject)
                # Holý název = chceme VĚRNOST → fotka. Námět se scénou
                # („Trosky v bouři") by se překreslením fotky ztratil, proto
                # jde dál na text-grounded cestu. Táž úvaha jako
                # HANS_ART_PORTRAIT_IMG2IMG_V1 u osob, tatáž funkce.
                if _ha._subject_beyond_name(subject, _pnm):
                    _ha._log.info("art: místo '%s' nese scénu → text-grounded malba",
                              _pnm)
                else:
                    _pref = _ha._fetch_person_ref(config, _pent, diary_db_path)   # funkce je generická
                    if _pref:
                        _r = _ha.paint_place_from_photo(
                            config, diary_db_path, subject, _pref, style,
                            place_name=_pnm, grounded=grounded)
                        if _r:
                            return _r
                        _ha._log.info("art: podoba místa nevyšla → text-grounded malba")
                    else:
                        _ha._log.info("art: místo '%s' nemá na Wikipedii fotku "
                                  "→ text-grounded malba", _pnm)
        except Exception as _ple:
            _ha._log.debug("art: place-likeness cesta selhala: %s", _ple)
    # scene_intro musí NÉST námět — dřív bylo "" → LLM dostal prázdný prompt
    # a maloval naslepo (např. „pan Sorge" → generický stařec).
    if style:
        # HANS_ART_STYLE_V4 — ukotvi i STYL (stejná kaskáda: entity/RAG/Wiki)
        style_desc = _ha._ground_style(config, diary_db_path, style)
        scene_intro = (
            "Subject/theme to depict (described in Czech):\n%s\n\n"
            "Render it IN THIS ART STYLE (described in Czech):\n%s\n\n"
            "Create ONE English SDXL prompt of the subject rendered in that "
            "style; weave the style into composition, forms and palette, and "
            "end with strong English style keywords.\n\n" % (grounded, style_desc))
        _scene_sys = _ha._STYLE_SCENE_SYSTEM
    else:
        scene_intro = (
            "Subject/theme to depict (described in Czech):\n%s\n\n"
            "Create ONE evocative artistic English SDXL prompt of THIS subject "
            "(fitting the person/thing described).\n\n" % grounded)
        _scene_sys = _ha._SUBJECT_SCENE_SYSTEM
    res = _ha._render_image(config, title, grounded, diary_db_path,
                        scene_system=_scene_sys, scene_intro=scene_intro,
                        cs_subject=subject)
    if not res:
        _ha._log.warning('art: obraz na téma „%s" se nevyrenderoval', title)
        # HANS_ART_PROMISE_KEPT_V1 (6.8.) — uživateli už bylo řečeno „maluji"
        # (chat odpovídá HNED, render běží na pozadí). Když se render odloží,
        # nesmí to skončit jen řádkem v logu — jinak Hans slíbil obraz, který
        # nikdy nepřijde. Doloženo 15:06: Ollama timeout → render odložen,
        # uživatel čekal a `/stav` mezitím hlásil, že se nemaluje.
        # Fronta doručí zprávu Hansovým mostem (`HANS_NOTIFY_QUEUE_V1`).
        # HANS_ART_RETRY_V1 — omluva jen tomu, kdo o obraz žádal (autonomní
        # malování a opakovaný pokus nic neslibovaly), a slib se ULOŽÍ.
        if _retry or not zadal:
            return None
        try:
            from scripts.hans_commitments import add_paint_retry
            add_paint_retry(diary_db_path, zadal, subject, style)
        except Exception as _ce:
            _ha._log.warning("art: uložení dlužného obrazu selhalo: %s", _ce)
        try:
            import json as _js
            import time as _tm
            with open("data/notify_queue.jsonl", "a", encoding="utf-8") as _q:
                _q.write(_js.dumps({
                    "text": ("Omlouvám se, pane — obraz na téma „%s\" se mi "
                             "teď nepodařilo namalovat (nedostal jsem se ke "
                             "svému mozku). Poznamenal jsem si ho: namaluji ho, "
                             "jakmile to půjde, a hned vám ho pošlu."
                             % title),
                    # HANS_NOTIFY_DIRECT_V1 — uživatel na obraz ČEKÁ; tiché hodiny
                    # tuhle omluvu jednou odložily do 9:00 a slib zase visel.
                    "direct": True,
                    "ts": _tm.time()}, ensure_ascii=False) + "\n")
        except Exception as _ne:
            _ha._log.debug("art: notifikace o odloženém renderu: %s", _ne)
        return None
    rel_path, prompt, vision_desc = res
    _soud = _ha.soud_nametu(config, subject, grounded, vision_desc)   # HANS_ART_SUBJECT_CHECK_V1
    caption = _ha._evaluate_artwork(config, diary_db_path, title, subject, vision_desc,
                                source_label="tím, oč jsem byl požádán", soud=_soud)
    _ha._derive_art_lesson(config, diary_db_path, title, vision_desc, caption)
    try:
        db = sqlite3.connect(diary_db_path, timeout=5.0)
        db.execute(
            "INSERT INTO diary (ts, event_type, title, note, data) VALUES (?,?,?,?,?)",
            (time.time(), "artwork", title, caption,
             json.dumps({"path": rel_path, "prompt": prompt, "source": "subject",
                         "vision": vision_desc, "subject_check": _soud,
                         "painted_ts": time.time()}, ensure_ascii=False)))
        db.commit()
        db.close()
    except Exception as e:
        _ha._log.warning("art: log subject artwork failed: %s", e)
    _ha._log.info('art: Hans namaloval na téma „%s" → %s', title, rel_path)
    return rel_path, caption

# ROZDELENI_PRIKAZU_V1 — až na konci, viz hlavička
from scripts import hans_art as _ha  # noqa: E402
