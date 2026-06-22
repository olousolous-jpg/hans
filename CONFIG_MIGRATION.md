# 🔧 Config migrace — sjednocení dvou UI na sdílené schéma

**Cíl:** dvě paralelní konfig UI (klávesa **S** ve streamu = Tkinter `config_gui*`, a **webadmin** `web_admin.py`+`templates/index.html`) generovat z JEDNOHO schématu `scripts/config_schema.py`. Přidání pole = jeden záznam → objeví se v obou UI. Konec driftu (dnes: 109 společných, 20 jen S, 28 jen web, 225 klíčů needituje nic).

**Marker:** `SCHEMA_DRIVEN_TABS_V1`.

**KONCOVÝ CÍL (rozhodnuto 12.6.):** klávesa-S Tkinter config se po dokončení migrace **CELÁ odstraní** (moc široká, nevejde se na obrazovku) → **webadmin = jediné konfig UI.** Tkinter generátor je interim parita; finální krok = smazat celou ConfigGUI + `ord('s')` handler (ne surgicky po sekci). Schéma dál slouží webadminu.

## Tier policy
- **basic** — co se reálně ladí (enabled, prahy, hosty/creds, modely, intervaly, persony/prompty). Vždy vidět.
- **expert** — detaily zřídka sahané (interní tuning, HEF cesty, kalibrace, weights). Pod rozbalovacím „Expert".
- **skrýt** (mimo schéma) — `_note`/`_comment`, cesty/db, auto-hodnoty (`calibrated_*`), RAG interní, `function_name`/`batch_size`. Edituje se v souboru.
- **custom editor** — dict-of-dict s vlastním UI (už ve webadminu): `known_persons`, `object_remapping`, `camera_presets`/`camera_model`, `autofocus_mode`/`hdr_mode`, `special_greetings`, `popup_chat`.

## Nutná rozšíření schématu (než půjdou některé vlny)
1. ~~**Top-level skalární klíče**~~ ✅ HOTOVO 12.6. (`TOP_LEVEL_SCALARS_V1`): `_normalize` path bez tečky → section=klíč, key=None. **Web bez změny** (getNestedVal/collectSection jsou dot-path, beztečka = 1-úroveň). Tkinter: `key=None` větve v `_num_entry`/`_entry`/`_toggle`(už byl)/save/reload. Round-trip top-level ověřen přes web.
2. ~~**Custom editor hook**~~ ✅ HOTOVO 12.6. (`CUSTOM_EDITOR_JSON_V1`): typ `"json"` — dict editovaný jako JSON text (robustní add/remove). Tkinter `_json_multiline` (kind `json`, save=`json.loads`), web textarea `data-json` + `fillSchemaJson()` (plní po loadu, populateAll skip) + saveSchema `JSON.parse` (přepíše string z collectSection, nevalidní JSON → abort). Round-trip ověřen (special_greetings zůstal dict). **Použitelné i pro `known_persons`/`object_remapping`/`camera_presets`** (zbývají).
3. ~~**Number-entry typ**~~ ✅ HOTOVO 12.6. (`NUMBER_ENTRY_V1`): schema typ `"number"` (+`"float":true`), Tkinter `_num_entry` (typovaný Entry, kindy `num_int`/`num_float`), web `<input type=number>`. Použito ve Vlně 8 (port, lat/lon). **TODO un-hide:** `num_ctx`, `chunk_size`, `recent_replies_max`, `voice.sample_rate`/`capture_rate` lze teď převést na `number` (zatím skryté).

## Pořadí prací (každá vlna = jeden patch, na konci smazat odpovídající natvrdo psaný tab/HTML = de-dup zisk)

- [x] **Vlna 0 — pilot:** `persona` → 🎩 Hans / Identita. ✅ nasazeno, server-side ověřeno.
- [~] **Vlna 1a — Hansova duše (čisté `sekce.klíč`, 0 nové infra):** `hans_dialog`, `models`+`evening_reflection`, `hans_idle`+`hans_routine`, `room_observer`, `relationships`, `hans_library`, `hans_questions`, `kolac_cases`. **NASAZENO (aditivně), server-side ověřeno, ⏳ čeká live ověření + restart.** 9 schema skupin, ~24 basic + ~19 expert polí. Web nav „🎩 Hans" (vše pod jedním tabem jako karty), Tkinter = 9 záložek.
  - **Interim duplicita (záměr):** stará web záložka přejmenovaná na „Hans (legacy)" — DRŽÍ JEŠTĚ `openrouter` (Vlna 2) + `weather` (Vlna 8), proto NELZE smazat teď. `hans_dialog`/`hans_idle`/`room_observer` jsou teď v obou (legacy + schema) — de-dup až po Vlnách 2/8, kdy se legacy vyprázdní. Tkinter `_tab_hans`/`_tab_chat` zatím beze změny (stejný důvod).
- [~] **Vlna 1b:** 🌙 Hans/Denní režim (hour scalars + dreams/night_summary/night_reduce toggly). **NASAZENO, server-side ověřeno (round-trip top-level), ⏳ live.** Hodiny jako `number` (0-23).
- [~] **Vlna 1c:** `greeting` (system_prompt/user_prompt textarea + `special_greetings` JSON). **NASAZENO, server-side ověřeno vč. JSON round-trip, ⏳ live.** Skupina 👋 Hans/Pozdravy.
- [~] **Vlna custom:** `camera_model`/`autofocus_mode` (choice top-level), `hdr_mode` (number), `autofocus_lens_position`+`autofocus.*` (stragglery), `known_persons`/`object_remapping`/`camera_presets` (json). **NASAZENO, server-side ověřeno (json + top-level choice round-trip), ⏳ live.** 2 skupiny: 📷 Kamera-režim&ostření, 🧩 Pokročilé(JSON). Pozn.: camera_model/autofocus_mode/hdr_mode mají duplicitní ID s legacy cfg-camera tabem (sync přes querySelectorAll, de-dup při finále).
- [~] **Vlna 2 — Chat/LLM:** `openwebui_direct`, `openwebui_chat`, `openrouter`, `gemini`. **NASAZENO (aditivně), server-side ověřeno (vč. round-trip POST), ⏳ live ověření.** 3 skupiny: 💬 Chat/Připojení (direct), 💬 Chat/OpenWebUI (alt), ☁ Chat/Cloud fallback. `greeting_mode` → choice {once_per_session, once_per_day}.
  - **Odloženo:** `openwebui_chat.popup_chat.*` (3-úrovňový nested dict) — Tkinter helpery umí jen 2 úrovně (a chybí nested bool/str). Zůstává editovatelný přes legacy HTML do jeho odstranění; dořeším s rozšířením #2/nested support. `openrouter` tím migrován → legacy cfg-hans drží už JEN `weather` (Vlna 8), pak půjde celá legacy záložka pryč.
- [~] **Vlna 3 — Hlas:** `voice`, `tts`. **NASAZENO (aditivně), server-side ověřeno, ⏳ live ověření.** 2 skupiny: 🔊 TTS, 🎙 Voice. Skryto: `voice.sample_rate`/`capture_rate` (technické inty), `voice.wake_words` (list — čeká list-editor).
- [~] **Vlna 4 — Rozpoznávání:** `recognition_tuning`, `recognition`, `face_preprocess`, `face_processing`, `enrollment`, `unknown_enrollment`, `unknown_tracker`, `async_recognizer`. **NASAZENO (aditivně), server-side ověřeno, ⏳ live ověření.** 5 skupin: 🔍 Rozpoznávání-prahy (9b/12e), 🖼 Předzpracování (9b/2e), 🎚 Kvalita&zápis (3b/4e), ➕ Auto-zápis neznámých (4b/2e), 👤 Sledování neznámých (3b/9e). `recognition.recognition_threshold`+`auto_enrollment` ověřeno jako živé (CLI/cluster); `recognition.algorithm`/`feature_extraction` + `unknown_tracker.db_path` + `enrollment.poses`(list) skryto.
- [~] **Vlna 5 — Detekce/Hailo:** `hailo`, `hailo_server`, `object_detection`, `objects`, `gesture`, `scheduler`. **NASAZENO (aditivně), server-side ověřeno, ⏳ live ověření.** 4 skupiny: 🧠 Hailo (mode=choice+restart, HEF cesty restart), 📦 Objekty, ✋ Gesta, ⏱ Scheduler. Skryto: `hand_landmark.presence_threshold` (obskurní duplikát). `object_remapping`(custom dict) zatím přes legacy.
  - ⚠️ **POUČENÍ (12.6.):** při migraci NEPŘEJMENOVÁVAT hravé/easter-egg popisky na „odborné". `hans_dialog.toaster_mode` jsem v 1a přejmenoval „Svítorka mod → Toaster mód" + dal do Expert → uživatel ho nenašel. Vráceno: plný název „Svítorka mod — Koláč nabízí pečivo a nedá se odradit" + popis (Červený trpaslík) + basic. Migrace = zachovat původní UX, ne sterilizovat.
- [~] **Vlna 6 — Kamera/obraz:** `camera`, `fisheye`, `hq_zoom`, `display`, `display_controller`, `eyes`. **NASAZENO (aditivně), server-side ověřeno vč. round-trip camera number, ⏳ live ověření.** 5 skupin: 📷 Kamera (rozměry=number+restart), 🐟 Fisheye, 🔎 HQ Zoom (ms/px=number), 🖥 Displej, 👁 Oči. **Odloženo (custom widgety, rozšíření #2):** `camera_model`, `autofocus`/`autofocus_mode`, `hdr_mode`, `camera_presets`. Pozn.: lores<main clamp ve web save NENÍ (pre-existing gap, ne regrese).
- [~] **Vlna 7 — Servo:** `servo_tracking`, `features`. **NASAZENO (aditivně), server-side ověřeno, ⏳ live ověření.** 2 skupiny: 🎯 Servo-sledování (chování), 🔧 Servo-rozsahy (meze/kanály, offsety basic). Skryto: `calibration_completed` + `calibrated_*` (auto-zápis kalibrací).
- [~] **Vlna 8 — Kodi/počasí:** `kodi`, `weather`, `surroundings`. **NASAZENO (aditivně), server-side ověřeno vč. round-trip number port, ⏳ live ověření.** 2 skupiny: 🎬 Kodi (port jako `number`), ☀ Počasí (lat/lon jako `number float`). Skryto: `kodi.monitor_db`, `surroundings.db_path`.
  - **🔓 ODBLOKOVÁNO:** legacy „Hans (legacy)" záložka (hans_dialog/hans_idle/room_observer/openrouter/weather) je teď CELÁ pokrytá schématem → **připravená ke smazání** (nav button + `tab-cfg-hans` div + JS funkce saveGreeting apod. specifické pro ni). ⏳ smazat AŽ po live ověření že schema taby renderují (gate: verify-before-delete).
- [~] **Vlna 9 — Systém/infra:** WOL scalars, `debug`, `ui`, `performance`. **NASAZENO, server-side ověřeno, ⏳ live.** 2 skupiny: 🔌 Systém/WOL (top-level scalars), 🛠 Systém/Debug&UI (debug scalars + `ui` + `performance`). Skryto (mimo schéma, edit v souboru): `database`, `conversations`, `detection_log`, `knowledge`, `surroundings`, `models`, `known_persons`(custom).
- [x] **FINÁLE (a) — de-dup legacy web taby ✅ 12.6.:** smazáno 14 legacy `tab-cfg-*` divů z DOM (662 řádků, 2 souvislé bloky) po live ověření. Nav buttons už dřív pryč (APP_SHELL_V1). Zbylo 8 tabů: dashboard/diary/faces/objects/conversations/cfg-identity/log/questions. Duplicitní ID zmizely. Ověřeno: div balance 84/84, GET / 200, schema 38 skupin OK. `renderPersons` má guard → populateAll bezpečné.
- [x] **CLEANUP ✅ 12.6. (`CONFIG_CLEANUP_V1`):** smazán mrtvý JS (`saveSection`/`doSave`/`saveObjectConfig`/`saveGreeting`/`renderPersons`/`addPerson`/`saveDefault`/`loadDefault`/`sv`/`personsData` + redundantní special-cases v populateAll/collectSection). Smazány osiřelé soubory `scripts/config_gui.py`/`config_gui_base.py`/`config_gui_tabs.py` (snapshoty `*.orphan-deleted.bak`). Ověřeno: div 83/83, JS závorky 241/241 + 542/542, web_admin/display_controller/config_schema OK. Pozn.: backend `/api/config/save_default`+`load_default` zůstávají (feature lze vrátit tlačítkem).
- [x] **FINÁLE (b) — odstranit klávesu-S ✅ 12.6. (`CONFIG_GUI_REMOVED_V1`):** odstraněno drátování v `display_controller_picam.py` (import, instance, `pump()`, `_handle_keys` param, `ord('s')` handler). Klávesa S už nic nedělá. `_on_settings_save` zůstal (hot-reload z webadminu). py_compile OK. Soubory `config_gui.py`/`config_gui_base.py`/`config_gui_tabs.py` jsou teď OSIŘELÉ (žádný importér) — ponechány jako neaktivní (lze smazat, neurgentní). Legacy web taby ZACHOVÁNY jako fallback (uživatel zvolil bezpečný půlkrok — finále (a) odloženo na po live ověření).

## ✅ GLOBÁLNÍ UI REDESIGN — celý webadmin (12.6., `APP_SHELL_V1`)
Celý admin přestavěn z horní lišty tabů do **levého sidebaru** dle mockupu (tmavý Catppuccin zachován — volba uživatele „layout mockupu, tmavý motiv"):
- **Globální levý sidebar:** brand „🎩 Hans Admin" + nav (📊 Dashboard, 📔 Deník, 😀 Obličeje, 📦 Objekty, 💬 Konverzace, ❓ Otázky, ⚙ Nastavení, 📋 Log) + status dot dole.
- **Top bar:** titulek aktuální stránky (`#page-title`, mění se přes `PAGE_TITLES`).
- `showTab` přepsán: aktivace přes `data-tab` (ne `event.target`) + titulek.
- **Legacy `tab-cfg-*` taby vyřazeny z navigace** (NESMAZÁNY — zůstávají v DOM per přání „ještě nemaž"; jen nejsou v sidebaru). Konfig vede jen ⚙ Nastavení (schema).
- Na ⚙ Nastavení vzniká dvojitý sidebar (globální + kategorie konfigu) — master/detail pattern, OK.

## ✅ UI REDESIGN — sidebar konfig (12.6., `SCHEMA_CATEGORIES_V1`)
Web schema config přestavěn z jednoho nekonečného scrollu (38 karet) do **sidebar layoutu** dle design mockupu (ChatGPT Image 12.6.):
- **Levý sidebar:** 9 kategorií (Hans/Chování/Chat/Hlas/Rozpoznávání/Detekce/Kamera/Servo/Systém) + **search box** ("Hledat v nastavení").
- **Hlavní panel:** jen aktivní kategorie; sticky toolbar s **Uložit změny / Zrušit změny**.
- **Per skupina:** tlačítko „⚙ Pokročilé (N)" rozbalí expert pole.
- **Search:** filtruje pole napříč všemi kategoriemi (label+path), odhalí i expert.
- Mapování kategorií v `config_schema.CATEGORY_OF` (jedno místo); endpoint vrací i `categories`. **Render všech skupin** (skryté přes CSS) → přepínání kategorií NEZTRATÍ rozeditované hodnoty, save sebere vše. Nav button „🎩 Hans" → „⚙ Nastavení".

## ✅ MIGRACE OBSAHOVĚ KOMPLETNÍ (12.6.) — 38 schema skupin
Všech 80 sekcí buď migrováno (basic/expert), nebo vědomě skryto (infra/cesty/auto-hodnoty). Všechna 3 rozšíření schématu hotová. Zbývá jen FINÁLE (a)+(b) — gate: live ověření.

## Po každé vlně
1. `py_compile` + `/api/config/schema` smoke + TestClient `GET /`.
2. `rm` pyc, restart Hanse + web_admin, ověřit naživo (oba UI renderují, round-trip uložení).
3. **Smazat duplicitní natvrdo psaný tab** (Tkinter `_tab_*` + HTML `tab-cfg-*` + JS), pokud sekce migrovaná.
4. Odškrtnout vlnu zde.

> Pozn.: snapshoty originálů v `data/patch_snapshots/*.pilot.*`. Mechanika UI: web čte/ukládá přes `id="sekce.klíč"` (`populateAll`/`collectSection`); Tkinter přes registr `_widgets` + helpery (`_multiline`/`_slider`/…).
