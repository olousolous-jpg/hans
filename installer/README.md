# Instalace Hanse na nové zařízení

Samostatný instalátor v této složce. **Nemění žádný existující soubor Hanse** —
instaluje prostředí, zapisuje config (přes `scripts/config_io`, tedy rozděleně
na veřejnou a privátní část) a vytváří systemd službu.

Cíl: **Raspberry Pi 5 + AI Kit (Hailo-8L)**, Raspberry Pi OS 64 bit (Trixie,
Python 3.13) + **PC s GPU v téže síti** (Ollama, OpenWebUI).

## Postup

### 1. PC (Linux s GPU)
```bash
git clone <repo> hans && cd hans
bash installer/pc/install_pc.sh
```
Nainstaluje Ollamu (a pustí ji do sítě), stáhne modely, spustí OpenWebUI
v Dockeru, nastaví firewall, SSH a Wake-on-LAN. Na konci vypíše IP a MAC PC.
Pak ručně: otevři `http://<ip-pc>:8080`, vytvoř účet a API klíč (`sk-…`).

### 2. Raspberry Pi
```bash
git clone <repo> ~/hans && cd ~/hans
bash installer/install.sh
```

| Krok | Co dělá |
|---|---|
| `preflight` | kontrola Pi 5, systému, místa; **zastaví se, pokud tu Hans už běží** |
| `apt` | HW a vědecké balíčky z apt (`apt-packages.txt`) |
| `system` | skupiny uživatele, SPI/I2C, sudo bez hesla pro `systemctl stop hailortd` |
| `hailo` | kontrola `/dev/hailo0` a modelů; ArcFace HEF ze starého Pi nebo z Hailo Model Zoo |
| `venv` | `.venv` s `--system-site-packages` + `requirements.txt` (verze z apt zamčené) |
| `network` | IP PC → všechny adresy služeb, API klíč, Kodi, WOL, SSH klíč na PC |
| `llm` | model pro generování textů: Ollama na PC / lokálně na Pi / ručně přes Claude či ChatGPT |
| `persona` | domácnost (pády jmen → oslovení), jméno, rod, role a povaha persony, společník |
| `models` | náhrada `hans-czech` aliasem `<persona>-czech`, kontrola všech modelů z configu |
| `knowledge` | RAG kolekce v OpenWebUI + nahrání vygenerovaných dokumentů identity |
| `service` | `~/.config/systemd/user/hans.service` (PATH vede do `.venv`) |
| `finish` | shrnutí a ruční kroky |

Kroky jdou pouštět opakovaně i jednotlivě: `--only persona,models`, `--skip hailo`,
`--redo`. Stav je v `data/installer/state`, odpovědi průvodce v
`data/installer/answers.json`, zálohy configu v `data/installer/backup-*`
(vše v `data/`, tedy mimo git).

## Vyzkoušení bez instalace (i na Pi, kde Hans běží)
```bash
bash installer/install.sh --dry-run --fresh
```
Nic neinstaluje, nemění skutečný config ani službu — výsledný config zapíše do
`data/installer/dryrun/`. `--fresh` simuluje nové zařízení (ignoruje
`config.private.json`). **Pozor:** kroky `llm` a `persona` i v dry-run opravdu
volají Ollamu na PC (načtou model do VRAM). Bez zátěže PC: `--skip llm,persona,models`,
nebo v kroku `llm` zvol ruční režim.

Samotný průvodce: `python3 installer/wizard.py persona --dry-run --fresh`.

## Jak to funguje
- **Python ve venv, run.sh beze změny.** `run.sh` volá natvrdo `python3.13`; služba
  má v `PATH` jako první `.venv/bin`, kde `python3.13` existuje. Ručně:
  `PATH="$PWD/.venv/bin:$PATH" ./run.sh`.
- **Balíčky z apt vs pip.** HW (Hailo, picamera2, lgpio) a numpy/opencv/scipy jsou
  z apt; picamera2 je sestavená proti numpy z apt, proto se jejich verze pro pip
  zamykají (`data/installer/constraints.txt`).
- **Čistý privátní config.** `config.private.example.json` je anonymizovaný (porty
  `0000`, `uzivatel`, nulová UUID). Průvodce z něj bere jen strukturu, zástupné
  hodnoty zahodí, aby nepřebily výchozí hodnoty v kódu.
- **Persona.** Deterministicky (v kódu): jazyk, rod, emoji, vykání, pravidla oslovení
  z pádů jmen, hlas TTS (Antonín/Vlasta). LLM píše jen tvůrčí texty (`core`,
  zájmy, dokumenty identity, společník); výsledek se kontroluje (token `{name}`,
  role v první větě, emoji) a dá se vygenerovat znovu, s poznámkou nebo upravit.
- **`hans-czech`** se nedá stáhnout — vytvoří se alias `<persona>-czech` z veřejného
  modelu (bez SYSTEM promptu, identitu posílá Hans v každém dotazu).

## Známá omezení (v kódu Hanse, instalátor je neřeší)
- **Severka** (`scripts/hans_severka.py`) má v promptu napsáno, že persona „začínala
  jako MAJORDOMUS“. S jinou rolí bude z této věty vycházet.
- **Ženská persona:** kód má místy pevné mužské tvary (šablony, některé prompty).
- `kolac_exam.py` obsahuje oslovení „pane majordome“.
- `tools/knowledge_setup.py` a `tools/bootstrap_identity.py` čtou jen veřejný config,
  po rozdělení configu na čisté instalaci nefungují — instalátor má vlastní náhradu.
- Instalátor nebyl spuštěn na skutečném Pi; otestovaný je na x86 s falešnou
  Ollamou/OpenWebUI (`installer/tests/`).

## Testy
```bash
python3 -m unittest discover -s installer/tests -v
```
`test_dependencies.py` hlídá, že každý balíček importovaný v kódu Hanse instalátor
zná. Když do Hanse přibude nový `import`, test selže a řekne, co doplnit do
`requirements.txt` / `apt-packages.txt`.
