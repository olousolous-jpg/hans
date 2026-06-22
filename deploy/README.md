# 📦 Přenos Hanse na jiné Raspberry Pi 5 + AI Kit

Hans NENÍ self-contained binárka — závisí na HW (Hailo-8L, Pi kamera, GPIO
displeje/servo, audio) a externích službách (Ollama+OpenWebUI na PC, Kodi/OSMC).
Tenhle balík proto cílí na **ekvivalentní HW** (jiné/nové Pi 5 + AI Kit + periferie),
ne na PC/laptop.

## Na ZDROJOVÉM Pi (kde Hans běží)
```bash
cd ~/Desktop/face-recognition
bash deploy/bundle.sh
# → ~/hans_bundle_<datum>.tar.gz  (kód + paměť/identita/faces + HEF + service)
```
**Co balík obsahuje:** kód (scripts/, main.py, web_admin.py, run.sh), config.json,
data (hans_diary.db = paměť/identita/postoje/nitky, *.pkl = faces galerie, avatar,
ostatní DB), resources/*.hef (Hailo modely), requirements.txt, hans.service.
**Co NE:** venv (relikt), logy, tts_cache, archive, patch_snapshots, dočasné DB.

## Na CÍLOVÉM Pi
```bash
mkdir hans && cd hans
tar -xzf ~/hans_bundle_<datum>.tar.gz
bash deploy/install.sh        # apt HW deps + pip + systemd služba
```

## ⚠️ Ruční kroky na cíli (HW-specifické, install.sh je jen připraví/zmíní)
1. **Hailo AI Kit** — `hailo-all` z apt + ověřit `hailortd` a `/dev/hailo0`
   (dle oficiálního Raspberry Pi AI Kit návodu). Bez Hailo → žádné rozpoznávání.
2. **Pi kamera** — `libcamera-hello` musí ukázat obraz.
3. **Dual-eye displeje + servo** — zapojit dle pinmapy (CLAUDE.md / `Eye_sphere.py`).
4. **Audio** (robot_hat / hifiberry-dac) — nakonfigurovat výstup.
5. **IP adresy v `config.json`** — Kodi (`kodi.host`), PC pro WOL/Ollama/OpenWebUI.
   Specifické pro síť → upravit ručně. (viz [[network-topology-pc-kodi]])
6. **passwordless sudo** pro `systemctl stop hailortd` (run.sh) — doporučeno.
7. **Skupiny uživatele:** `sudo usermod -aG video,audio,spi,i2c,gpio,render,input <user>`
8. **Gesta (detekce ruky)** — bez externí závislosti: palm anchory jsou nově
   v `scripts/blaze_palm_anchors.py` (dřív externí `~/blaze_app`, už NENÍ potřeba).

## Co je 100% přenositelné vs co potřebuje cíl
| Přenositelné (v balíku) | Potřebuje cíl |
|---|---|
| Kód, config, persona | Hailo HW + SDK |
| **Paměť/identita** (hans_diary.db) | Pi kamera |
| **Faces galerie** (*.pkl) | GPIO periferie (displeje/servo) |
| Avatar (idle.png, klipy) | Ollama+OpenWebUI (PC) + Kodi — síť |
| HEF modely | správné IP v configu |

## 🖥️ Setup PC (Ollama + OpenWebUI + ComfyUI) — RUČNĚ, jiný stroj
> 📖 **Detailní postup krok-za-krokem (i pro netechnické) je v [`SETUP_PC.md`](SETUP_PC.md).**
> Níže jen zkrácený přehled.

Hans potřebuje na PC (v síti) běžící služby. install.sh je NEinstaluje. Postup:
- **Ollama** (`:11434`) + modely: `ollama pull` pro `hans-czech` (persona finetune),
  `jobautomation/OpenEuroLLM-Czech` (analytika), `qwen2.5:14b`, `qwen2.5:7b`,
  `bge-m3` (embeddings), `llava` (vision). ⚠️ VRAM tiery — viz [[ollama-vram-tiers]].
- **OpenWebUI** (`:8080`) — chat běží přes ni (RAG vrstva nad Ollamou). Vytvořit
  API token → do `config.json` (`openwebui_*.api_token`). RAG kolekce (`hans_denik`,
  `hans_filmy`, `hans_cetba`, `hans_dila`, `hans_pripady`, `hans_identita`) se
  naplní časem (Hans uploaduje přes `hans_knowledge`), nebo přenést exportem OpenWebUI.
- **ComfyUI** (`:8188`, volitelné — avatar render) — SDXL + LivePortrait nodes.
- **WOL** — povolit Wake-on-LAN v BIOSu + NIC; MAC/IP do `config.json`.
- **Firewall** — otevřít porty 11434, 8080, (8188).
- Do `config.json` zapsat IP PC do `kodi.host`? ne — to je Kodi; PC IP je ve
  `wol_pc_ip` + OpenWebUI/Ollama URL.

## 📺 Setup Kodi (OSMC, jiný stroj `*.252`)
- **JSON-RPC** — Kodi → Nastavení → Služby → Ovládání: zapnout HTTP (port 80),
  uživatel/heslo (default `osmc`/`osmc`) → do `config.json` `kodi.{host,port,user,password}`.
- **Up Next** (`service.upnext`) — nainstalovat z repozitáře (binge-watch dialog).
- **Hansův addon `service.hans.suggest`** (návrh filmu — dialog ano/ne):
  zdroj je v balíku `data/kodi_addon/service.hans.suggest/`. Na Kodi:
  ```bash
  sshpass -p osmc scp -r data/kodi_addon/service.hans.suggest osmc@<KODI_IP>:/home/osmc/.kodi/addons/
  sshpass -p osmc ssh osmc@<KODI_IP> 'sudo systemctl restart mediacenter'
  # pak v Hansovi: Addons.SetAddonEnabled service.hans.suggest
  ```
  Detaily [[network-topology-pc-kodi]].

## Spuštění na cíli
- `systemctl --user start hans` (autostart po bootu už enabled installerem)
- logy: `journalctl --user -u hans -f`
- viz [[hans-service-control]]

## Poznámka k prostředí
Hans běží na **systémovém `python3.13`** (ne ve venv — ten je relikt; run.sh ho jen
`source`uje kvůli kompatibilitě). Proto install.sh dělá `pip install --break-system-packages`.
HW balíčky (hailort, picamera2, lgpio) dodává apt, ne pip.
