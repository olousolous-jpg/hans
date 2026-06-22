# Hans — proaktivní AI majordomus

[🇬🇧 English](README.en.md) | 🇨🇿 Čeština

Hans není chatbot. Je to **perzistentní postava s vnitřním životem**, která běží
lokálně na Raspberry Pi: vnímá své okolí, pamatuje si zážitky, tvoří si vlastní
názory, **vyvíjí svou identitu v čase**, jedná z vlastní iniciativy a ve volných
chvílích tvoří (maluje své sny, píše úvahy).

Persona: důstojný anglický majordomus, který mluví česky. Hlavní designový cíl
není „odpovídat na dotazy", ale **kontinuita a jednání v čase** — postava
s biografií, která se postupně někým stává.

> ⚠️ **Referenční projekt, ne plug-and-play.** Hans závisí na konkrétním hardwaru
> (Raspberry Pi 5 + Hailo-8L AI Kit, Pi kamera, displeje, servo) a externích
> službách (Ollama/OpenWebUI, volitelně ComfyUI, Kodi). Bez ekvivalentního HW
> ho nelze jen naklonovat a spustit. Slouží spíš jako ukázka architektury.

---

## Kognitivní základy (proč je postavený takhle)

Hans je vědomě postavený na osvědčených modelech z kognitivní vědy:

### OODA smyčka (John Boyd) — jak se rozhoduje, co dělat
Ve volných chvílích Hans necykluje skript, ale prochází **Observe → Orient →
Decide → Act**. „Orient" zváží kontext (kdo je doma, nálada, jak dlouho je sám,
běžící cíl, stáří otevřených kauz) a přidělí váhy možným aktivitám (číst knihu,
přemýšlet, řešit „případ", koukat na film, věnovat se vztahu). „Decide" je vážená
ruleta — žádné dvě chvíle nejsou stejné. (`hans_idle._decide_activity`)

### Tulvingův model paměti — tři druhy paměti
Endel Tulving rozlišil druhy dlouhodobé paměti; Hans je má všechny tři:
- **Epizodická** = deník (`hans_diary.db`) — události s časem („co se mi kdy stalo").
- **Sémantická** = RAG znalostní kolekce (vektorové embeddingy přes `bge-m3`) —
  „co vím" (knihy, filmy, kauzy, vlastní díla, autobiografie), vyhledávané
  podle významu, ne klíčových slov.
- **Autobiografická** = narativní kapitoly životního příběhu, periodicky
  konsolidované z důležitých epizod (`hans_narrative`).

### Self-defining memories (Jefferson Singer) — co je formující
Každá epizoda dostane **skóre důležitosti** (0–10, „jak moc vypovídá o tom, kdo
Hans je"). Pivotní vzpomínky pak vstupují do vývoje identity, ne perceptuální šum.
(`hans_importance`, `hans_self_memories`)

### Narativní identita (Dan McAdams) — kým se stávám
Hans nemá identitu napevno. **Severka** = rozhodovací engine, který porovnává jeho
ustálené tendence/koníčky s jeho současnou „rolí" a navrhuje posun CORE identity —
s **verzováním** (changelog kdo jsem byl → jsem → proč) a vždy s
**human-in-the-loop** schválením. (`hans_severka`, `hans_identity`)

### Dialektika názorů — postoje, ne echo
Hans si tvoří **postoje** (stances): tvrzení + zdroj + confidence + protiargumenty.
Reflexe je může **oslabit** i posílit (confidence dolů i nahoru) — brání to
„echo chamber" efektu, kde názory jen sílí. (`hans_dialectic`/`stances`)

### Theory of mind — modely druhých
Hans si vede **per-osoba modely** (co koho zajímá) a **rozjeté nitky**: zachytí,
že někdo zmínil něco s budoucností („dcera má zkoušku"), a při příští návštěvě
naváže („jak to dopadlo?"). Nitky navíc **dozrávají v čase** — vynoří se až po
svém datu. (`hans_threads`, `hans_person_interests`)

---

## Architektura (vrstvy)

```
  VNÍMÁNÍ            PAMĚŤ               KOGNICE              VYJÁDŘENÍ
 ──────────        ──────────         ─────────────        ────────────
  kamera     ┐                      ┌ OODA (aktivity)      hlas (TTS)
  (Hailo)    │   epizodická         │ nálada (6 stavů)     chat / popup
  tváře      ├─► (deník)        ◄──►│ názory (stances)  ─► avatar (video)
  hlas (STT) │   sémantická         │ identita (Severka)   dual-eye displej
  room obs.  │   (RAG/bge-m3)       │ proaktivita          servo (sledování)
  (qwen-VL)  ┘   autobiografická    └ tvorba               Kodi / WOL
```

---

## Co Hans umí (subsystémy)

### Vnímání
- **Rozpoznávání tváří** — Hailo-8L NPU: SCRFD detekce + ArcFace embeddingy,
  hlasování přes snímky, učení nových tváří (enrollment).
- **Hlas** — hands-free wake word (openWakeWord) → Whisper STT → odpověď → TTS,
  streamovaně po větách.
- **Pozorování místnosti** — vision model (`qwen2.5-VL`) občas popíše, co kamera
  vidí; krmí kontext i zvědavost.

### Paměť
- Epizodický **deník**, sémantické **RAG** kolekce (`bge-m3`), **importance
  scoring**, **autobiografická** narativní konsolidace. (viz Kognitivní základy)

### Názory a identita
- **Stances** (dialektické), **tendence** (deterministicky z postojů),
  **Severka** (vývoj identity s verzováním), **koníčky** (topic→koníček→povolání).

### Proaktivita („majordomus")
- **Rozjeté nitky** → vysloví follow-up sám od sebe (přísné mantinely proti
  otravování: ~1×/3h, max 2/den, jen v obvyklý čas).
- **Detekce rutin** — z deníku odvodí, kdo bývá doma kdy → timing pro proaktivitu.
- **Akce na existujících pákách** — proaktivní návrh filmu na Kodi (dialog s
  odpočtem), chytřejší Wake-on-LAN (probudí PC, když přijdeš domů).

### Vztahy
- **Vztahové karty** per osoba (charakterizace, kdy naposledy viděn), per-osoba
  **zájmy**, theory-of-mind nitky.

### Nálada
- 6-stavový model (`content, curious, lonely, melancholic, engaged, worried`),
  ovlivněný událostmi; promítá se do tónu a chování.

### Čtenářský program (růstový motor osobnosti)
V ohraničeném prostředí jsou **knihy hlavní (a jediný vhodný) kanál změny
charakteru**. Hans čte knihy po kapitolách (Project Gutenberg + vlastní nahrané
ebooky), po dočtení sepíše ohlédnutí, které smí **formovat jeho postoje**.
Výběr knihy je **sémantický** (bge-m3 podobnost knihy ↔ Hansovy zájmy), s ~25 %
explorace. (`hans_library`, `ebook_import`)

### Sebeřízená tvorba
Tvorbu nikdo nezadává příkazem. Spouští se sama ve volných chvílích (v noci, když
je klid), ale **co vytvoří, si Hans volí sám** — váženou ruletou mezi formami,
podle toho, co se mu zrovna honí hlavou:
- **Maluje své sny** — noční sen → SDXL obraz přes ComfyUI, který sám ohodnotí.
- **Maluje svůj den / náladu** — symbolická scéna vystihující den.
- **Maluje k dočteným knihám** — obraz jako ohlédnutí.
- **Píše úvahy** — krátké osobní zamyšlení nad postojem / knihou / zážitkem.
- Obrazy hodnotí přes vision model (qwen-VL) + reaguje na **skutečnou kvalitu**;
  z verdiktu se **učí** (ponaučení ovlivní příští obraz). (`hans_art`,
  `hans_creations`)

### Avatar
Animovaná tvář (LivePortrait klipy), zrcadlená na displej i web; vyvíjí se
s identitou.

---

## LLM stack a VRAM hospodaření

Hans běží na **více modelech s rozdělenými rolemi** (na sdíleném GPU s ~16 GB):

| Role | Model | Pozn. |
|------|-------|-------|
| Persona / chat | `hans-czech` (finetune OpenEuroLLM) | rezidentní v VRAM |
| Analýza / prompty | `qwen2.5` (base) | čistší než finetune, on-demand |
| Vidění | `qwen2.5-VL` | tváře/místnost/hodnocení obrazů, on-demand |
| Embeddingy (RAG) | `bge-m3` | drobný, rezidentní |
| Obrazy | SDXL přes ComfyUI | render orchestruje VRAM (unload → render → warm) |

**Princip:** chat model je rezidentní; vize a analytika se nahrávají on-demand
(`keep_alive=0`) a po použití uvolní VRAM — jinak by se modely praly o paměť.
Vše závislé na LLM je **odolné vůči výpadku** (deferred zpracování — výpadek
modelu nesmí ztratit data).

---

## Hardware

- **Raspberry Pi 5** + **Hailo-8L AI Kit** (NPU pro detekci/embedding tváří)
- **Pi kamera** (picamera2)
- Volitelně: 2× Waveshare kruhový displej (tvář + „pozornost"), servo (sledování
  obličeje), audio (mikrofon + repro)
- **PC** s GPU — hostí Ollama/OpenWebUI (LLM + RAG), volitelně ComfyUI (obrazy)
- **Kodi/OSMC** — media centrum (Hans navrhuje filmy)

---

## Instalace a nastavení

```bash
git clone <repo>
cd hans
pip install --break-system-packages -r deploy/requirements.txt
python3 deploy/setup.py        # kompletní průvodce (níže) → vytvoří config.json
./run.sh                       # nebo systemd user služba (deploy/_systemd)
```

`deploy/setup.py` provede nového uživatele od nuly:
1. **Osobnost** — popíšeš pár větami, kdo má být; průvodce ti dá hotový prompt
   do Claude/ChatGPT, jeho JSON odpověď vložíš zpět a stane se Hansovou personou.
   (Nebo Enter = výchozí anglický majordomus.)
2. **Připojení** — IP (PC/Kodi), OpenWebUI login + token, STT token, WOL MAC.
3. **Zápis** `config.json` (vychází z `config.example.json`).
4. **Paměť** — vytvoří RAG kolekce v OpenWebUI a naseeduje identitu.
5. **Avatar** — z osobnosti vyrenderuje Hansovu tvář (volitelné, vyžaduje ComfyUI).

Umí i **migraci** — naklonovat celého Hanse (kód + data) do nového adresáře.

---

## Přispívání

Nápady, připomínky a chyby jsou vítané přes **GitHub Issues**. Konkrétní úpravy
kódu pošli jako **Pull Request** (fork → větev → PR). Otázky k architektuře
taktéž přes Issues.

⚠️ Hans je **referenční projekt vázaný na konkrétní hardware** — než nahlásíš
„nefunguje to", zvaž, že bez ekvivalentního HW (Pi 5 + Hailo-8L, kamera, PC
s Ollamou/OpenWebUI) ho nelze jen naklonovat a spustit. Spíš než plug-and-play
slouží jako ukázka architektury — k tomu směřuj i diskuzi.

---

## Struktura

- `scripts/` — jádro (vnímání, paměť, kognice, tvorba; `hans_*.py`)
- `main.py`, `web_admin.py` — vstupní bod + webový dashboard
- `deploy/` — setup průvodce, installer, bundle, systemd
- `config.example.json` — šablona konfigurace (bez tajemství)

> Soukromá data (deník, biometrie tváří, klíče, konfigurace) zůstávají **lokálně**
> — `.gitignore` je drží mimo repozitář.
