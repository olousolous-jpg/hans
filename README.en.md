# Hans — a proactive AI majordomo

🇬🇧 English | [🇨🇿 Čeština](README.md)

Hans is not a chatbot. He is a **persistent character with an inner life**, running
locally on a Raspberry Pi: he perceives his surroundings, remembers experiences,
forms his own opinions, **evolves his identity over time**, acts on his own
initiative, and in idle moments creates (paints his dreams, writes reflections).

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

### Relationships
- **Relationship cards** per person (characterization, last seen), per-person
  **interests**, theory-of-mind threads.

### Mood
- A 6-state model (`content, curious, lonely, melancholic, engaged, worried`),
  shaped by events; reflected in tone and behavior.

### Reading program (the engine of personality growth)
In a bounded environment, **books are the main (and only suitable) channel for
character change**. Hans reads books chapter by chapter (Project Gutenberg +
your own uploaded ebooks); after finishing one he writes a reflection that is
allowed to **shape his stances**. Book selection is **semantic** (bge-m3
similarity between a book and Hans's interests), with ~25% exploration.
(`hans_library`, `ebook_import`)

### Self-directed creativity
Nothing commands the creation. It kicks in during idle moments (at night, when
it's quiet), but **what to create is Hans's own choice** — a weighted roulette
across forms, shaped by what's currently on his mind:
- **Paints his dreams** — a night dream → an SDXL image via ComfyUI, which he then
  critiques himself.
- **Paints his day / mood** — a symbolic scene capturing the day.
- **Paints finished books** — an image as a retrospective.
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
| Persona / chat | `hans-czech` (OpenEuroLLM finetune) | resident in VRAM |
| Analysis / prompts | `qwen2.5` (base) | cleaner than the finetune, on-demand |
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
