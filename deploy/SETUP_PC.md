# 🖥️ Instalace PC služeb pro Hanse — krok za krokem (i pro netechnické)

Hans (na Raspberry Pi) potřebuje na **počítači v téže síti** tři služby:
| Služba | K čemu | Povinné? |
|---|---|---|
| **Ollama** | „mozek" — jazykové modely (chat, analýza) | ✅ ano |
| **OpenWebUI** | vrstva nad Ollamou (chat + dlouhodobá paměť/RAG) | ✅ ano |
| **ComfyUI** | generování/animace avatara (Hansova tvář) | ⬜ volitelné |

> **Předpoklady:** PC s **Linuxem** (Ubuntu/Debian apod.) a samostatnou grafickou
> kartou (GPU). Současný Hansův PC má **AMD Radeon (ROCm)**. NVIDIA je obecně
> jednodušší. Bez GPU poběží modely pomalu (na procesoru).
>
> **Jak otevřít terminál:** na PC stiskni `Ctrl+Alt+T`. Příkazy níže do něj vkládej
> (kopíruj) a potvrď `Enter`. Kde je `sudo`, vyžádá si tvé heslo (při psaní se
> nezobrazuje — to je normální).

---

## 1) Ollama — jazykové modely

### 1a. Instalace
```bash
curl -fsSL https://ollama.com/install.sh | sh
```
Tím se Ollama nainstaluje a spustí jako služba na pozadí (port `11434`).

Ověř, že běží:
```bash
ollama --version
curl http://localhost:11434/api/tags
```
Druhý příkaz vrátí `{"models":[...]}` (zatím prázdné) — to je v pořádku.

### 1b. Stažení modelů
Modely z veřejné knihovny stáhneš příkazem `ollama pull`:
```bash
ollama pull jobautomation/OpenEuroLLM-Czech:latest   # analytika (cca 5 GB)
ollama pull qwen2.5:14b      # záložní/silnější model (cca 9 GB)
ollama pull qwen2.5:7b       # menší pomocný model
ollama pull bge-m3           # „embeddings" pro paměť/RAG
ollama pull llava            # vidění (popis obrazu)
```
> Stahování chvíli trvá (gigabajty). Můžeš nechat běžet.

### 1c. ⚠️ Model `hans-czech` — VLASTNÍ, nejde stáhnout
`hans-czech` je **Hansova osobnost** (vlastní úprava modelu), NENÍ ve veřejné
knihovně. Musíš ho **přenést ze starého PC**:

**Na STARÉM PC** zjisti recept modelu a ulož ho:
```bash
ollama show --modelfile hans-czech > ~/hans-czech.Modelfile
```
Tento soubor (`hans-czech.Modelfile`) + případný základní model přenes na nový PC
(USB / síť). **Na NOVÉM PC** model znovu vytvoř:
```bash
ollama create hans-czech -f ~/hans-czech.Modelfile
```
> Pokud Modelfile odkazuje na základní model z knihovny, Ollama si ho stáhne sama.
> Pokud na lokální soubor (.gguf), musíš přenést i ten.

Ověř, že máš vše:
```bash
ollama list
```
Měl bys vidět `hans-czech`, `jobautomation/OpenEuroLLM-Czech`, `qwen2.5`, `bge-m3`, `llava`.

---

## 2) OpenWebUI — chat + paměť (přes Docker)

Nejsnazší cesta je **Docker** (zabalený program, který se spustí jedním příkazem).

### 2a. Nainstaluj Docker (pokud chybí)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
```
Po tomto příkazu se **odhlas a znovu přihlas** (nebo restartuj PC), aby se členství
ve skupině `docker` projevilo.

### 2b. Spusť OpenWebUI
```bash
docker run -d --network=host \
  -v open-webui:/app/backend/data \
  -e OLLAMA_BASE_URL=http://localhost:11434 \
  --name open-webui --restart always \
  ghcr.io/open-webui/open-webui:main
```
Po chvíli (první spuštění stahuje image) otevři v prohlížeči:
**`http://localhost:8080`**

### 2c. První přihlášení + API klíč
1. Při prvním otevření si vytvoř účet (jméno/heslo — lokální, jen pro tebe).
2. Vlevo dole **Nastavení → Účet → API klíče** → vytvoř klíč (`sk-…`).
3. Tento klíč zkopíruj do Hansova `config.json` na Raspberry Pi:
   pole `openwebui_direct.api_token` (a/nebo `voice.stt_token`).
4. V OpenWebUI ověř, že vidí Ollama modely (Nastavení → Modely) — měl by tam být
   `hans-czech` atd.

### 2d. Paměť (RAG kolekce)
Hansova dlouhodobá paměť (identita, deník, díla, četba, filmy, případy) žije
v „kolekcích" (knowledge bases) v OpenWebUI. **Musíš je jednou vytvořit —
NEvzniknou samy.** Dokud neexistují a jejich ID nejsou v `config.json`, Hans
do paměti nic neukládá (jen loguje „neznámá kolekce").

1. Měj v `config.json` vyplněné `openwebui_direct.base_url` + `api_token`
   (z kroku 2c — token z OpenWebUI: Nastavení → Účet → API klíče).
2. Z **kořene projektu** spusť:
   ```bash
   python3 tools/knowledge_setup.py
   ```
   → vytvoří 6 kolekcí v OpenWebUI a zapíše jejich ID do `config.json`
   (`knowledge.collections`) + nastaví `knowledge.enabled = true`.
   Idempotentní — když kolekce už existují, jen je použije.
3. (Volitelné, doporučeno) Naseeduj Hansovu výchozí identitu:
   ```bash
   python3 tools/bootstrap_identity.py
   ```
   → vloží úvodní dokumenty (kdo je Hans, kdo je Koláč, vztahy, hardware)
   do kolekce `hans_identita`. Obsah si uprav v `IDENTITY_DOCS` uvnitř skriptu.

Přenos staré paměti z jiného PC: exportuj/importuj data OpenWebUI
(Nastavení → Databáze); jinak Hans začne s prázdnou pamětí a naplní ji časem.

---

## 3) ComfyUI — avatar (volitelné, NÁROČNĚJŠÍ)

> ComfyUI generuje/animuje Hansovu tvář. **Je to nejtěžší část** — vyžaduje
> správně nastavené ovladače GPU. Pokud ti avatar nevadí, tuto sekci přeskoč;
> Hans funguje i bez něj (použije statický obrázek).

### 3a. Předpoklad: ovladače GPU
- **NVIDIA:** nainstaluj `nvidia-driver` + CUDA (dle návodu NVIDIA).
- **AMD (jako současný PC):** nainstaluj **ROCm** (dle oficiálního AMD ROCm návodu
  pro tvou distribuci) — to je nejnáročnější krok, postupuj přesně dle dokumentace.

### 3b. Instalace ComfyUI
```bash
cd ~
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python3 -m venv venv
source venv/bin/activate
# PyTorch: NVIDIA → z pytorch.org (CUDA); AMD → verze pro ROCm:
pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm6.2
pip install -r requirements.txt
```

### 3c. Spuštění (dostupné po síti pro Hanse)
```bash
cd ~/ComfyUI
source venv/bin/activate
python main.py --listen 0.0.0.0 --port 8188
```
Ověř v prohlížeči: **`http://localhost:8188`**

### 3d. Modely + rozšíření
- Stáhni **SDXL** checkpoint do `ComfyUI/models/checkpoints/`
  (např. `sd_xl_base_1.0.safetensors`).
- Pro animaci tváře nainstaluj rozšíření **ComfyUI-LivePortraitKJ**
  (přes „ComfyUI Manager" nebo `git clone` do `ComfyUI/custom_nodes/`).
> Tip: nainstaluj **ComfyUI-Manager** (custom node) — usnadní instalaci dalších
> rozšíření přes tlačítko v rozhraní.

---

## 4) Propojení s Hansem (Raspberry Pi)
V Hansově `config.json` na Pi zkontroluj/uprav **IP adresu PC** (zjistíš ji na PC
příkazem `hostname -I`):
- Ollama/OpenWebUI URL (kde je PC) a `wol_pc_ip` / `wol_pc_mac` (probuzení PC).
- Kodi (`kodi.host`) = jiná IP (OSMC přehrávač), ne PC.

**Firewall na PC** — povol porty, aby na ně Hans dosáhl:
```bash
sudo ufw allow 11434/tcp    # Ollama
sudo ufw allow 8080/tcp     # OpenWebUI
sudo ufw allow 8188/tcp     # ComfyUI (jen pokud používáš)
```

---

## 5) Když něco nefunguje
- **Hans říká „chat timeout / No route to host":** PC spí nebo služba neběží.
  Na PC: `curl http://localhost:11434/api/tags` (Ollama) a `http://localhost:8080`
  (OpenWebUI). Když neodpovídají, restartuj: `sudo systemctl restart ollama`,
  `docker restart open-webui`.
- **OpenWebUI po probuzení PC zamrzne:** `docker restart open-webui`.
- **Model se nenačítá / je pomalý:** GPU je přetížená (víc modelů naráz). Restart
  Ollamy: `sudo systemctl restart ollama`. Viz pravidlo VRAM tierů v hlavním handoffu.
- **PC se po síti nebudí (WOL):** povol Wake-on-LAN v BIOSu i v nastavení síťové
  karty; do `config.json` dej správnou MAC adresu PC (`ip link` ji ukáže).
