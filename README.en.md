# Hans — a proactive AI majordomo

🇬🇧 English | [🇨🇿 Čeština](README.md)

Hans is not a chatbot. He is a **persistent character with an inner life**, running
locally on a Raspberry Pi: he perceives his surroundings, remembers experiences,
forms his own opinions, **evolves his identity over time**, acts on his own
initiative, and in idle moments creates and studies on his own (studies topics
in depth, writes his own serialized work, paints his dreams, spins his own
insights). He also **knows what he can do** — and when he gains a new ability he
notices it and is curious to try it out.

Persona: a dignified English butler who speaks Czech. The core design goal is not
"answer questions" but **continuity and agency over time** — a character with a
biography who is gradually *becoming someone*.

> ⚠️ **Reference project, not plug-and-play.** Hans depends on specific hardware
> (Raspberry Pi 5 + Hailo-8L AI Kit, Pi camera, displays, servo) and external
> services (Ollama/OpenWebUI, optionally ComfyUI, Kodi). Without equivalent
> hardware you can't just clone and run it. It serves more as an architecture
> showcase.

---

## Cognitive foundations (why it's built this way)

Hans is deliberately built on established models from cognitive science:

### OODA loop (John Boyd) — how he decides what to do
In idle moments Hans doesn't run a script; he goes through **Observe → Orient →
Decide → Act**. "Orient" weighs context (who's home, mood, how long he's been
alone, an active goal, the age of open "cases") and assigns weights to possible
activities (read a book, think, work a "case", watch a film, tend a relationship).
"Decide" is a weighted lottery — no two moments are the same.
(`hans_idle._decide_activity`)

### Tulving's memory model — three kinds of memory
Endel Tulving distinguished kinds of long-term memory; Hans has all three:
- **Episodic** = the diary (`hans_diary.db`) — time-stamped events ("what happened to me").
- **Semantic** = RAG knowledge collections (vector embeddings via `bge-m3`) —
  "what I know" (books, films, cases, my own works, autobiography), retrieved by
  meaning rather than keywords.
- **Autobiographical** = narrative life-story chapters, periodically consolidated
  from important episodes (`hans_narrative`).

### Self-defining memories (Jefferson Singer) — what is formative
Every episode gets an **importance score** (0–10, "how much it says about who
Hans is"). Pivotal memories then feed identity development — not perceptual noise.
(`hans_importance`, `hans_self_memories`)

### Narrative identity (Dan McAdams) — who I'm becoming
Hans has no fixed identity. **Severka** is a decision engine that compares his
durable tendencies/hobbies against his current "role" and proposes a shift of the
CORE identity — with **versioning** (a changelog of who I was → am → why) and
always with **human-in-the-loop** approval. (`hans_severka`, `hans_identity`)

### Dialectical opinions — stances, not an echo chamber
Hans forms **stances**: claim + source + confidence + counterarguments. Reflection
can **weaken** them as well as reinforce them (confidence goes down too, not only
up) — preventing an "echo chamber" where opinions only ever strengthen.
(`hans_dialectic`/`stances`)

### Theory of mind — models of others
Hans keeps **per-person models** (what each person is interested in) and **open
threads**: he notices when someone mentions something with a future ("my daughter
has an exam") and follows up on their next visit ("how did it go?"). Threads also
**mature over time** — they surface only after their date.
(`hans_threads`, `hans_person_interests`)

---

## Architecture (layers)

```
  PERCEPTION         MEMORY              COGNITION            EXPRESSION
 ──────────        ──────────         ─────────────        ────────────
  camera     ┐                      ┌ OODA (activities)    voice (TTS)
  (Hailo)    │   episodic           │ mood (6 states)      chat / popup
  faces      ├─► (diary)        ◄──►│ opinions (stances)─► avatar (video)
  voice (STT)│   semantic           │ identity (Severka)   dual-eye display
  room obs.  │   (RAG/bge-m3)       │ proactivity          servo (tracking)
  (qwen-VL)  ┘   autobiographical   └ creativity           Kodi / WOL
```

---

## What Hans can do (subsystems)

### Perception
- **Face recognition** — Hailo-8L NPU: SCRFD detection + ArcFace embeddings,
  voting across frames, learning new faces (enrollment).
- **Voice** — hands-free wake word (openWakeWord) → Whisper STT → response → TTS,
  streamed sentence by sentence.
- **Room observation** — a vision model (`qwen2.5-VL`) periodically describes what
  the camera sees; feeds both context and curiosity.

### Memory
- Episodic **diary**, semantic **RAG** collections (`bge-m3`), **importance
  scoring**, **autobiographical** narrative consolidation. (see Cognitive foundations)

### Truthfulness — two registers of the mind (against confabulation)
A generative model likes to "fill in" facts. The fix isn't to patch the
generator, but to **split the mind into two registers**: the **factual** one (who
is who, what happened, what he read) is grounded and **abstains by default** — it
answers from data or admits "no record"; the **imaginative** one (dreams,
paintings, musings) is free to invent. Trouble only arises when imagination
_claims_ facts — and that's prevented by layers that work through **routing, not
prompts**:
- **Deterministic short-circuits** — lookup-able internal queries ("your earliest
  memory") never reach the model; answered straight from the database. (`hans_recall`)
- **RAG-first + abstinence** — facts go to retrieval first; weak/no match → "I have
  no reliable record" (the persona is never asked to invent a fact).
- **Semantic self-consistency** — a risky factual query is generated several times;
  if answers diverge → confabulation → abstain. (`hans_selfconsistency`)
- **Entity store** — typed entities from Hans's reading (verbatim definitions) →
  names resolve deterministically (handles namesakes and phantoms). (`hans_entities`)
- **Query rewriter** — the persona hears you raw, but retrieval reads a cleaned,
  explicit query (resolves even "who is _he_?"). (`hans_rewriter`)
- **Immune system + contradiction detection** — a nightly fact-check of his own
  claims against the entity store + a contradiction check at write time.
  (`hans_immune`, `hans_contradiction`)
- **Provenance** — every piece of knowledge carries its **source** (experienced /
  was told / read / inferred / imagined / created); Hans tells memory from
  imagination and speaks with calibration (source monitoring). (`hans_provenance`)
- **Opinion grounding** — for philosophy/opinions he should instead take his **own
  pointed stance** (the imaginative register), not generic both-sidesism. (`hans_opinion`)

### Opinions and identity
- **Stances** (dialectical), **tendencies** (derived deterministically from
  stances), **Severka** (identity evolution with versioning), **hobbies**
  (topic → hobby → vocation).

### Proactivity ("the majordomo")
- **Open threads** → he voices a follow-up on his own (strict guardrails against
  nagging: ~1×/3h, max 2/day, only at a typical time).
- **Routine detection** — from the diary he infers who is usually home when →
  timing for proactivity.
- **Action on existing levers** — proactively suggests a film on Kodi (dialog with
  a countdown), smarter Wake-on-LAN (wakes the PC when you come home).

### Agent layer (contextual actions from conversation)
Hans doesn't stop at text — from the conversation he **infers an action** and
**offers it with confirmation** ("Shall I play _Die Hard_? [yes/no]"), then carries
it out once approved. One unifying layer: an **action whitelist** (nothing outside
it), **always confirm** (human-in-the-loop), **argument grounding** (a film only
from the library), a confidence threshold, cooldown and anti-echo against nagging.
The router runs on the resident chat model (no added latency in normal chat thanks
to a pre-filter). V1 actions: play a film, put himself to sleep, add a book to the
reading list — extending it (e.g. smart lights) is one more adapter. (`hans_agent`)

### Relationships
- **Relationship cards** per person (characterization, last seen), per-person
  **interests**, theory-of-mind threads.

### Mood
- A 6-state model (`content, curious, lonely, melancholic, engaged, worried`),
  shaped by events; reflected in tone and behavior.

### Integration with PC, games and media
- **Live playback check** — asked "what's playing?" he checks the **current** Kodi
  state (not memory) and answers; if nothing is on, he suggests a film for the viewer.
- **Game mode** — on command (or automatically via the game launcher) he frees GPU
  memory for the game and stops using the GPU; afterwards his "brain" returns. The
  web button **verifies real free VRAM** (`rocm-smi`/ComfyUI) before you launch.
- **PC health** — over SSH he sees real GPU/CPU temperature, memory and status;
  during game mode the telemetry **cycles on the eye displays**. (`pc_remote`)
- **Self-maintenance (watchdog)** — Hans monitors the health of his own
  dependencies (Ollama, ComfyUI, Kodi, speech-to-text, PC, disk). He detects a
  wedged "brain" with a **real trial inference** (not just a ping, which won't
  catch a hang) and can **restart Ollama on the PC by himself**; status is
  surfaced on the dashboard and in chat. (`hans_health`, `/zdravi`)
- **Designs his own dashboard** — after studying design he writes a design critique
  and proposal for his web dashboard, and renders a mockup. (`hans_dashboard`)

### Reading program (the engine of personality growth)
In a bounded environment, **books are the main (and only suitable) channel for
character change**. Hans reads books chapter by chapter (Project Gutenberg +
your own uploaded ebooks); after finishing one he writes a reflection that is
allowed to **shape his stances**. Book selection is **semantic** (bge-m3
similarity between a book and Hans's interests), with ~25% exploration.
(`hans_library`, `ebook_import`)

### In-depth study (expertise from a hobby)
Beyond scattered reading, Hans runs **long-term study programs**: from a durable
hobby he builds a curriculum (6–10 sub-topics) and each night studies one in depth
→ notes into RAG → on completion a mastery reflection that grounds his "expertise".
For the strongest topics he escalates from Wikipedia to **real research**: OpenAlex
(academic abstracts), **Wikisource** (primary texts) and the **Internet Archive**
(full text of public-domain books) — deduplicated so he doesn't cite the same
source twice. (`hans_study`)

### From study to a real work (a closed creative arc)
Study doesn't end at text — Hans turns **what he learned into a real artifact**
and gradually improves it:
1. **Brief** — from *all* of his study notes on a topic (not just a few snippets)
   he distills the **best possible prompt** for a tool. Here the persona steps
   aside: Hans (from study) says **what** to apply, the tool knows **how**.
   Grounded — only principles he actually studied. (`hans_brief`, `/brief`)
2. **Tool selection** — after finishing a domain he **finds a suitable LLM in a
   grounded way** in the Ollama library (name/size/popularity/capabilities straight
   from ollama.com, no guessing), checks it fits in VRAM, and **proposes it for
   approval**. (`hans_toolscout`, `/nastroj`)
3. **The work** — the tool (a coder model) executes the brief → a **standalone web
   page** (code + its own images: image slots are rendered by SDXL based on what
   they should depict). Versions are saved and viewable in the web admin.
   (`hans_maker`, `/vytvor`)
4. **Critique and spiral** — after the work Hans **proposes what to deepen** (and
   why) and **asks you** (Telegram and chat). You can approve, **give your own
   critique** (he then studies deeper specifics — without repeating what he already
   knows), or decline. Deepening produces a **better version**. When he finishes a
   domain and makes his first work, he **records a new capability** himself ("I can
   make a work about X") and offers it next time. (`/prohloubit`)

### Self-knowledge — he knows what he can do
Hans has a **factual awareness of his own capabilities** (a curated manifest) — so
he offers and uses them in conversation instead of denying them. When a **new
capability** is added (the manifest grows), he **notices** it himself (logs it,
lifts his mood) and is **curious to try it** — for safe creative abilities he
actually tries it and writes down what he found. The source is factual, not a
guess. (`hans_capabilities`)

### Self-directed creativity
Nothing commands the creation. It kicks in during idle moments (at night, when
it's quiet), but **what to create is Hans's own choice** — a weighted roulette
across forms, shaped by what's currently on his mind:
- **Paints his dreams** — a night dream → an SDXL image via ComfyUI, which he then
  critiques himself.
- **Paints his day / mood** — a symbolic scene capturing the day.
- **Paints finished books** and **any subject on request** ("paint what we just
  talked about" → he actually paints it).
- **Writes his own serialized work** — an essay/story/guide, over nights for weeks
  into a finished artifact. (`hans_authorship`)
- **Spins his own insights (synthesis)** — connects things learned across domains
  into one unexpected thought. (`hans_ideas`)
- **Critiques himself** — reviews his own replies and takes a lesson on how to
  express himself better next time. (`hans_selfcritique`)
- **Writes reflections** — short personal musings on a stance / book / experience.
- He evaluates the images via a vision model (qwen-VL) and reacts to the **real
  quality**; from the verdict he **learns** (the lesson shapes the next image).
  (`hans_art`, `hans_creations`)

### Avatar
An animated face (LivePortrait clips), mirrored to the display and the web; it
evolves with his identity.

---

## LLM stack and VRAM management

Hans runs on **multiple models with split roles** (on a shared ~16 GB GPU):

| Role | Model | Note |
|------|-------|------|
| Persona / chat / voice | `hans-czech` (OpenEuroLLM finetune) | resident in VRAM |
| Analysis / extraction | `OpenEuroLLM` (base) | native Czech, anti-confabulation, on-demand |
| Judgment (synthesis, self-critique, stances) | `deepseek-r1:14b` (reasoning) | 2-call: reason in English → voice in Czech via hans-czech; runs in RAM/CPU (num_gpu:0) so it never touches VRAM |
| Vision | `qwen2.5-VL` | faces/room/image evaluation, on-demand |
| Embeddings (RAG) | `bge-m3` | tiny, resident |
| Images | SDXL via ComfyUI | render orchestrates VRAM (unload → render → warm) |

**Principle:** the chat model stays resident; vision and analytics load on demand
(`keep_alive=0`) and release VRAM after use — otherwise the models would fight
over memory. Everything depending on an LLM is **failure-resilient** (deferred
processing — an LLM outage must never lose data).

---

## Hardware

- **Raspberry Pi 5** + **Hailo-8L AI Kit** (NPU for face detection/embedding)
- **Pi camera** (picamera2)
- Optional: 2× Waveshare round display (face + "attention"), servo (face
  tracking), audio (mic + speaker)
- **A PC** with a GPU — hosts Ollama/OpenWebUI (LLM + RAG), optionally ComfyUI (images)
- **Kodi/OSMC** — media center (Hans suggests films)

---

## Install and setup

```bash
git clone <repo>
cd hans
pip install --break-system-packages -r deploy/requirements.txt
python3 deploy/setup.py        # full guided wizard (below) → creates config.json
./run.sh                       # or the systemd user service (deploy/_systemd)
```

`deploy/setup.py` walks a new user from scratch:
1. **Personality** — describe in a few sentences who he should be; the wizard hands
   you a ready prompt for Claude/ChatGPT, you paste its JSON answer back and it
   becomes Hans's persona. (Or Enter = default English butler.)
2. **Connectivity** — IPs (PC/Kodi), OpenWebUI login + token, STT token, WOL MAC.
3. **Write** `config.json` (based on `config.example.json`).
4. **Memory** — creates the RAG collections in OpenWebUI and seeds the identity.
5. **Avatar** — renders Hans's face from his personality (optional, needs ComfyUI).

It also supports **migration** — cloning the whole of Hans (code + data) into a
new directory.

---

## Contributing

Ideas, feedback and bug reports are welcome via **GitHub Issues**. For concrete
code changes open a **Pull Request** (fork → branch → PR). Architecture questions
also via Issues.

⚠️ Hans is a **reference project tied to specific hardware** — before reporting
"it doesn't work", note that without equivalent HW (Pi 5 + Hailo-8L, camera, a PC
running Ollama/OpenWebUI) it can't simply be cloned and run. It's a showcase of
the architecture rather than plug-and-play — please frame discussion accordingly.

---

## Layout

- `scripts/` — the core (perception, memory, cognition, creativity; `hans_*.py`)
- `main.py`, `web_admin.py` — entry point + web dashboard
- `deploy/` — setup wizard, installer, bundle, systemd
- `config.example.json` — config template (no secrets)

> Private data (diary, face biometrics, keys, configuration) stays **local** —
> `.gitignore` keeps it out of the repository.

## License

Hans is licensed under the **PolyForm Noncommercial License 1.0.0**. You may
freely use, modify, and share it for **any noncommercial purpose** — personal,
study, research, educational, or nonprofit (full text: [LICENSE](LICENSE)).

**Commercial use requires a separate license** from the authors — reach out via
a [GitHub issue](https://github.com/olousolous-jpg/hans/issues).

> Earlier versions were released under GPL-3.0 and remain available under GPL.
> The new license applies to versions from this change onward.
