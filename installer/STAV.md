# Instalátor — stav, rozhodnutí a navazování

Přehled pro další práci na instalátoru (i pro jinou session). Jak instalátor
používat, popisuje [README.md](README.md). Tady je, proč vypadá tak, jak vypadá,
co je hotové, co ne a na co si dát pozor.

Stav k 24. 9. 2026 — sloučeno do `main` v PR #1, #2, #3.

## Pravidla (od uživatele)
1. **Neměnit kód Hanse.** O kód Hanse se stará jiná session. Instalátor žije jen
   v `installer/`. Z Hansova kódu jen čte (`scripts/config_io`, `cz_names`,
   `hans_knowledge`, `hans_persona`, seznam kolekcí z `tools/knowledge_setup.py`)
   a používá existující HTTP API webadminu. Když by byla potřeba změna v kódu
   Hanse, jen ji popsat (viz níže), ne ji dělat.
2. **Soukromí.** Nic, co uživatel zadá (jména domácnosti, API klíče, IP, MAC),
   se nesmí dostat do gitu. Všechno jde do `config.private.json` (přes
   `config_io`, který hlídá veřejnou část) a do `data/installer/` (v `.gitignore`).
   V kódu, testech a dokumentaci jsou jen smyšlená jména (Karel, Eva, Rozárka) a
   dokumentační adresy. Jména ze vzoru privátního configu (mohou být skutečná)
   se v instalátoru nepoužívají.
3. **Cíl je jen Raspberry Pi 5 + Hailo-8L**, persona vždy česky, Python v `.venv`.
4. **Uživatel nemůže instalaci naostro vyzkoušet** (má jedno Pi, na kterém Hans
   běží). Proto `--dry-run`, `test_install.sh` a ochrana v preflightu.

## Co je hotové
| Část | Soubor | Poznámka |
|---|---|---|
| Hlavní instalace | `install.sh` | 13 kroků, opakovatelné, `--dry-run/--only/--skip/--redo/--fresh` |
| Příprava PC | `pc/install_pc.sh` | Ollama v síti, modely, OpenWebUI v Dockeru, SSH, WOL |
| Průvodce | `wizard.py` + `hans_setup/` | network, llm, persona, household, models, knowledge, faces |
| Rozpoznávání osob | `hans_setup/faces.py` | přes `/api/enroll/start` a `quick_augment` webadminu |
| Zkouška | `test_install.sh` | dry-run celé instalace + ověření, že se nic nezměnilo |
| Testy | `tests/` | 18 testů; falešná Ollama/OpenWebUI/webadmin; hlídač závislostí |

## Klíčová rozhodnutí
- **venv s `--system-site-packages`:** Hailo, picamera2 a lgpio jdou jen z apt.
  numpy/opencv/scipy také z apt, protože picamera2 je sestavená proti numpy
  z apt. Jejich verze se pro pip zamykají (`data/installer/constraints.txt`).
- **`run.sh` se nemění:** volá natvrdo `python3.13`. Služba má v `PATH` jako
  první `.venv/bin`, kde `python3.13` existuje (případně odkaz).
- **`requirements.txt` z importů**, ne z `pip freeze` (`deploy/requirements.txt`
  obsahuje ~400 balíčků včetně CUDA). `setuptools<81` kvůli `webrtcvad`
  (potřebuje `pkg_resources`). `openwakeword==0.4.0` má modely přibalené.
- **Čistý privátní config:** vzor `config.private.example.json` je anonymizovaný
  (porty `0000`, `uzivatel`, nulová UUID, prázdné `hailo.recog_hef`). Bere se
  z něj jen struktura, zástupné hodnoty se zahodí (`cfg.fresh_private_from_example`).
- **Adresy PC** (`network.PC_URLS`): `openwebui_chat.base_url` je ve skutečnosti
  adresa **Ollamy** (:11434), `openwebui_direct.base_url` je OpenWebUI (:8080),
  STT jde přes OpenWebUI `/api/v1/audio/transcriptions`.
- **Persona:** deterministicky v kódu jsou jazyk, rod, emoji, vykání, pravidla
  oslovení (z pádů jmen) a hlas TTS (Antonín/Vlasta). LLM píše jen tvůrčí texty
  a výsledek se kontroluje (`persona.problems`: token `{name}`, role v první
  větě, emoji, délka).
- **`hans-czech`** (vlastní model původního PC) se nahrazuje aliasem
  `<persona>-czech` z veřejného modelu, bez SYSTEM promptu. Identitu posílá
  Hans v každém dotazu, takže ji Severka může měnit. Přepíší se všechny výskyty
  v configu (16 míst).
- **Paměť:** vlastní zakládání kolekcí přes `config_io`, protože
  `tools/knowledge_setup.py` a `bootstrap_identity.py` čtou jen veřejný config.
- **Obličeje:** zápis dělá běžící Hans. Na konci se na displeji Pi otevře okno,
  kam se ručně píše **klíč osoby** (jméno malými bez diakritiky) — musí sedět
  s `known_persons`. Průvodce klíč vypíše a čeká, až se osoba objeví v `/api/faces`.

## Neověřeno na skutečném HW
- Celá instalace naostro na Pi 5 (apt balíčky na Trixie, `hailo-all`, venv).
- Mapování verze HailoRT → Hailo Model Zoo pro ArcFace HEF (`mz_version_for`)
  je odhad. Stažený soubor se ověří `hailortcli parse-hef`; jistější je
  zkopírovat HEF ze starého Pi.
- `pc/install_pc.sh` na skutečném PC (zejména AMD/ROCm, zjištění VRAM).
- Vytvoření aliasu přes Ollama `/api/create` (nové API `from`, pád na `modelfile`).
- Že nový člověk z `wizard.py household` se po restartu Hanse správně načte.

## Zjištění v kódu Hanse (neopravováno — patří jiné session)
- `scripts/hans_severka.py`: prompt říká, že persona „začínal jako MAJORDOMUS“.
  S jinou rolí z toho Severka vychází. (Průvodce varuje.)
- `scripts/kolac_exam.py`: oslovení „pane majordome“.
- Ženská persona: v kódu jsou místy pevné mužské tvary. (Průvodce varuje.)
- `tools/knowledge_setup.py`, `tools/bootstrap_identity.py`: čtou jen veřejný
  `config.json` → po rozdělení configu na čisté instalaci nenajdou token;
  `knowledge_setup` navíc zapisuje do veřejného souboru.
- `deploy/setup.py`: ptá se na `greeting.user_prompt` s `{name}` = jméno persony,
  ale kód ho formátuje jménem HOSTA; a zapisuje jen část adres PC.
- `config.private.example.json`: zástupné hodnoty (`:0000`, `uzivatel`, prázdné
  `hailo.recog_hef`, `greeting.user_prompt`) by při prostém zkopírování přebily
  rozumné výchozí hodnoty v kódu.
- `scripts/cz_names.accusative`: pro „Karel“ dává „Karela“ (správně „Karla“).
  Instalátor to obchází (pády navrhne LLM, uživatel potvrdí).

## Když se Hans změní
- Nový `import` v kódu Hanse → selže `tests/test_dependencies.py` a řekne, co
  doplnit do `requirements*.txt` / `apt-packages.txt` (+ mapování `KNOWN`).
- Nový model v configu → instalátor ho najde sám (`models.model_paths`).
  Vlastní import mimo knihovnu Ollamy přidat do `models.CUSTOM_IMPORTS`.
- Nová adresa služby na PC v privátním configu → doplnit `network.PC_URLS`.
- Nová RAG kolekce → bere se sama z `tools/knowledge_setup.py`.
- Nový Hailo model v `/usr/share/hailo-models` nebo `resources/` → `step_hailo`.
- Změna API webadminu pro zápis tváří → `hans_setup/faces.py`.

## Jak ověřit změnu instalátoru
```bash
python3 -m unittest discover -s installer/tests -v
shellcheck -x installer/*.sh installer/pc/*.sh installer/lib/*.sh
bash installer/test_install.sh --yes
```
