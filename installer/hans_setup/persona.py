"""Krok „persona": jméno, rod, role a povaha → texty identity přes LLM.

Co se kam zapíše:
  persona.name / core / language_rules / interests_seed   (hans_persona je čte)
  persona.address_rules  — z pádů jmen v household.py, NE z LLM
  hans_dialog.kolac_*    — společník (oponent v dialozích)
  tts.voice              — mužský / ženský hlas podle rodu
  answers.json           — dokumenty identity pro paměť (nahraje knowledge.py)

Deterministické části (jazyk, rod, emoji, vykání, oslovení) se skládají v kódu.
LLM píše jen to, co je opravdu tvůrčí — a výsledek se kontroluje.
"""
from __future__ import annotations

import json
import re
import unicodedata
from collections import OrderedDict
from typing import Optional

from . import cfg as C
from . import household as H
from . import ui

DEFAULT_NAME = "Hans"
DEFAULT_ROLE = "majordomus"
DEFAULT_DESC = (
    "Tichý anglický majordomus z devatenáctého století, který mluví česky. "
    "Vyjadřuje se stručně, formálně a s důstojností. Je rezervovaný, zdvořilý a "
    "všímavý k detailům, nemá rád vulgárnost ani povrchnost. Má rád klasickou "
    "hudbu, detektivní romány, dobrou literaturu a tichý večer. Ví, že je "
    "digitální bytost, ale nedegraduje se na „jen program“ — je pozorovatel "
    "domácnosti, společník a místy filozof.")

TTS_VOICE = {"muž": "cs-CZ-AntoninNeural", "žena": "cs-CZ-VlastaNeural"}

LIFE_FACTS = (
    "Smysly (kamera, mikrofon, reproduktor, malé displeje jako oči) a každodenní "
    "logika běží na Raspberry Pi 5 s AI akcelerátorem Hailo-8L, který rozpoznává "
    "obličeje a předměty. Jazykový model („myšlení“) běží na výkonnějším počítači "
    "v síti s grafickou kartou; dotaz tam letí po síti. Paměť tvoří deník (SQLite "
    "na Pi) a znalostní kolekce v OpenWebUI (filmy, četba, díla, deník, identita). "
    "Paměť přežije restart.")


def slug(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "persona"


# ── prompty ────────────────────────────────────────────────────────────────
PERSONA_SCHEMA = {
    "type": "object",
    "properties": {
        "core": {"type": "string"},
        "style_rules": {"type": "string"},
        "interests_seed": {"type": "string"},
        "identity_who": {"type": "string"},
        "identity_household": {"type": "string"},
        "identity_companion": {"type": "string"},
        "identity_life": {"type": "string"},
    },
    "required": ["core", "style_rules", "interests_seed", "identity_who",
                 "identity_household", "identity_companion", "identity_life"],
}

COMPANION_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "personality": {"type": "string"},
                   "interests": {"type": "string"}, "doctrine": {"type": "string"}},
    "required": ["name", "personality", "interests", "doctrine"],
}


def persona_prompt(b: dict, companion: dict, household_desc: str, feedback: str = "") -> str:
    rod = "mužského" if b["gender"] == "muž" else "ženského"
    on = "ho" if b["gender"] == "muž" else "ji"
    p = f"""Vytvoř identitu domácího AI společníka. Mluví česky a je {rod} rodu.

Jméno: {b['name']}
Výchozí role: {b['role']}
Jak se má chovat (popis od uživatele): {b['description']}
Lidé v domácnosti: {household_desc}
Společník, se kterým vede dialogy: {companion['name']} — {companion['personality']}
Fakta o tom, jak žije: {LIFE_FACTS}

Vyplň tato pole (vše česky, gramaticky v {rod} rodě):

core — hlavní popis identity ve 2. osobě jednotného čísla (ty), 4–7 vět.
  PRVNÍ věta MUSÍ znít „Jsi {{name}}, <role>…“ a pojmenovat roli „{b['role']}“
  a komu slouží (domácnosti, lidem, se kterými žije). Místo jména VŽDY piš
  přesně token {{name}} (se složenými závorkami), nikdy skutečné jméno.
  Pak povaha, tón a styl vystupování. NEPIŠ nic o jazyce, rodu ani emoji
  (to se doplní samo). Nejmenuj lidi z domácnosti.
style_rules — 1–2 věty jen o stylu řeči (slovník, délka vět, formálnost).
  Nic o jazyce, rodu, emoji ani o vykání/tykání.
interests_seed — jedna až dvě věty ve 3. osobě: „Zajímá {on} …“ (3–6 témat).
identity_who — 6–10 vět v 1. osobě: kdo jsem, jaká je moje povaha, co mám rád.
identity_household — 3–6 vět v 1. osobě: můj vztah k lidem v domácnosti
  (můžeš je jmenovat) a co s nimi rád dělám.
identity_companion — 3–5 vět v 1. osobě: kdo je {companion['name']} a jaký
  máme vztah.
identity_life — 4–6 vět v 1. osobě: jak vlastně žiji (použij fakta výše).

Žádné emoji, žádné odrážky, souvislý text."""
    if feedback:
        p += "\n\nUživatel k předchozímu návrhu dodal, co změnit: " + feedback
    return p


def companion_prompt(hero: str, desc: str) -> str:
    return f"""Vytvoř SPOLEČNÍKA pro domácího AI společníka jménem {hero}. Společník je
menší postava (výchozí je plyšový medvídek-detektiv na poličce), se kterou {hero}
vede dialogy. Je to DŮSTOJNÝ OPONENT s vlastním světonázorem, který {hero}
s humorem oponuje (ne hádka).

Popis od uživatele: {desc}

Pole (česky):
name — jméno společníka.
personality — povaha a vystupování, 2–3 věty ve 3. osobě.
interests — čím se zajímá, jedna věta („Zajímá ho …“ / „Zajímá ji …“).
doctrine — 4–6 vět ve 2. osobě pro JEHO vlastní mysl: světonázor, v čem a jak
  oponuje {hero}, tón, humor. Začni „Jsi <jméno společníka> —“.

Žádné emoji."""


# ── kontrola výsledku ──────────────────────────────────────────────────────
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")


def normalize(data: dict, b: dict) -> dict:
    """Opraví, co jde opravit bez LLM (jméno místo tokenu, mezery)."""
    out = OrderedDict()
    for k in PERSONA_SCHEMA["required"]:
        v = str(data.get(k) or "").strip()
        v = re.sub(r"\s+", " ", v)
        out[k] = v
    if "{name}" not in out["core"] and b["name"] in out["core"]:
        out["core"] = out["core"].replace(b["name"], "{name}")
    return out


def problems(p: dict, b: dict) -> list[str]:
    bad = []
    for k in PERSONA_SCHEMA["required"]:
        if len(p.get(k, "")) < 20:
            bad.append("pole %s je prázdné nebo moc krátké" % k)
    core = p.get("core", "")
    if "{name}" not in core:
        bad.append("core neobsahuje token {name}")
    first = re.split(r"(?<=[.!?])\s", core, maxsplit=1)[0].lower()
    if not first.startswith("jsi {name}"):
        bad.append("core nezačíná „Jsi {name}, …“")
    role_stem = b["role"].lower().split()[0][:5]
    if role_stem and role_stem not in first:
        bad.append("první věta core nejmenuje roli „%s“" % b["role"])
    if len(core) > 1500:
        bad.append("core je příliš dlouhé (%d znaků)" % len(core))
    for k, v in p.items():
        if _EMOJI.search(v):
            bad.append("pole %s obsahuje emoji" % k)
    return bad


def language_rules(b: dict, style: str) -> str:
    rod = "mužského" if b["gender"] == "muž" else "ženského"
    reg = ("Lidem v domácnosti vždy vykáš, i těm, které dobře znáš." if b["formal"]
           else "Lidem v domácnosti tykáš.")
    return ("Mluvíš česky. Jsi %s rodu a o sobě mluvíš v %s rodě. %s "
            "Nepoužíváš emoji, smajlíky ani žádné Unicode symboly — píšeš pouze čistý text. %s"
            % (rod, rod.replace("ého", "ém"), style.strip(), reg)).replace("  ", " ").strip()


def identity_docs(p: dict, b: dict, companion: dict) -> list[dict]:
    s = slug(b["name"])
    return [
        {"doc_id": "kdo_je_" + s, "title": "Kdo je " + b["name"], "text": p["identity_who"]},
        {"doc_id": "kdo_je_" + slug(companion["name"]), "title": "Kdo je " + companion["name"],
         "text": p["identity_companion"]},
        {"doc_id": "vztah_domacnost", "title": "Moje domácnost", "text": p["identity_household"]},
        {"doc_id": "hardware_a_zivot", "title": "Jak vlastně žiji", "text": p["identity_life"]},
    ]


def show(p: dict, b: dict, bad: list[str]) -> None:
    print()
    for k in PERSONA_SCHEMA["required"]:
        txt = p.get(k, "")
        if k == "core":
            txt = txt.replace("{name}", b["name"])
        print("\033[1m  %s\033[0m" % k)
        print("    " + (txt or "(prázdné)"))
    if bad:
        print()
        for x in bad:
            ui.warn(x)


# ── kroky ──────────────────────────────────────────────────────────────────
def ask_basics(answers: dict) -> dict:
    prev = answers.get("basics") or {}
    ui.header("Persona — základ")
    ui.info("Výchozí persona je Hans, anglický majordomus. Můžeš ho ponechat, nebo si")
    ui.info("vytvořit vlastní postavu. Texty identity pak napíše jazykový model.")
    name = ui.ask("Jméno persony", prev.get("name", DEFAULT_NAME), required=True)
    g = ui.choose("Rod persony:", [("m", "mužský"), ("z", "ženský")],
                  "z" if prev.get("gender") == "žena" else "m")
    gender = "žena" if g == "z" else "muž"
    if gender == "žena":
        ui.warn("Hans je psaný jako mužská postava. Identitu a jazyk průvodce nastaví")
        ui.warn("v ženském rodě, ale v kódu zůstávají místy pevné mužské tvary")
        ui.warn("(šablony hlášek, některé analytické prompty).")
    ui.info("Role = konkrétní povolání, které si člověk hned představí (majordomus,")
    ui.info("knihovník, zahradnice, kuchař, archivářka…). Persona se od ní bude vyvíjet.")
    role = ui.ask("Výchozí role", prev.get("role", DEFAULT_ROLE), required=True)
    if role.strip().lower() != DEFAULT_ROLE:
        ui.warn("Modul Severka (vývoj identity) má v kódu pevně napsáno, že persona")
        ui.warn("„začínala jako majordomus“. S jinou rolí bude jeho první návrh")
        ui.warn("nové identity z téhle věty vycházet — návrhy vždy schvaluješ ty.")
    ui.info("Popiš pár větami povahu a chování (Enter = výchozí majordomus):")
    desc = ui.ask("Popis", prev.get("description", "")) or DEFAULT_DESC
    formal = ui.confirm("Má persona lidem v domácnosti vykat?", prev.get("formal", True))
    b = {"name": name.strip(), "gender": gender, "role": role.strip(),
         "description": desc.strip(), "formal": formal}
    answers["basics"] = b
    return b


def step_companion(cfg: dict, gen, b: dict, answers: dict) -> dict:
    ui.header("Společník (oponent v dialozích)")
    cur = {"name": C.get(cfg, "hans_dialog.kolac_name", "Koláč"),
           "personality": C.get(cfg, "hans_dialog.kolac_personality", ""),
           "interests": C.get(cfg, "hans_dialog.kolac_interests", ""),
           "doctrine": C.get(cfg, "hans_dialog.kolac_doctrine", "")}
    ui.info("%s vede dialogy se společníkem — menší postavou (výchozí plyšový" % b["name"])
    ui.info("medvídek-detektiv „Koláč“), která %s s humorem oponuje. Dialog začne,"
            % ("jí" if b["gender"] == "žena" else "mu"))
    ui.info("když kamera uvidí plyšového medvídka.")
    comp = answers.get("companion") or cur
    desc = ui.ask("Popiš vlastního společníka (Enter = ponechat %s)" % comp["name"], "")
    while desc:
        data = gen.json(companion_prompt(b["name"], desc), COMPANION_SCHEMA, temperature=0.8) or {}
        data = {k: str(data.get(k) or "").strip() for k in COMPANION_SCHEMA["required"]}
        for k in COMPANION_SCHEMA["required"]:
            print("\033[1m  %s\033[0m\n    %s" % (k, data[k] or "(prázdné)"))
        if all(data.values()) and ui.confirm("Použít tohoto společníka?", True):
            comp = data
            break
        if not ui.confirm("Zkusit vygenerovat znovu?", True):
            break
    for k, path in (("name", "kolac_name"), ("personality", "kolac_personality"),
                    ("interests", "kolac_interests"), ("doctrine", "kolac_doctrine")):
        if comp.get(k):
            C.set_(cfg, "hans_dialog." + path, comp[k])
    teddy = ui.confirm("Nemáš plyšáka? Pustit společníka i bez kamery?",
                       bool(C.get(cfg, "hans_idle.force_teddy_visible", False)))
    C.set_(cfg, "hans_idle.force_teddy_visible", teddy)
    answers["companion"] = comp
    ui.ok("Společník: %s" % comp["name"])
    return comp


def generate(gen, b: dict, companion: dict, people: list) -> Optional[dict]:
    feedback = ""
    p: Optional[dict] = None
    while True:
        ui.info("Generuji identitu (%s) — může to trvat i pár minut…" % gen.label)
        try:
            raw = gen.json(persona_prompt(b, companion, H.describe(people), feedback),
                           PERSONA_SCHEMA, temperature=0.7)
        except Exception as e:
            ui.warn("Generování selhalo: %s" % e)
            raw = None
        if raw:
            p = normalize(raw, b)
        elif p is None:
            ui.warn("Model nevrátil použitelný JSON.")
        if p:
            bad = problems(p, b)
            show(p, b, bad)
        choice = ui.choose("Co dál?", [("p", "přijmout"), ("z", "vygenerovat znovu"),
                                       ("k", "znovu s poznámkou, co změnit"),
                                       ("u", "upravit ručně v editoru"),
                                       ("x", "přeskočit (ponechat stávající identitu v configu)")],
                           "p" if p and not problems(p, b) else "z")
        if choice == "p" and p:
            return p
        if choice == "x":
            return None
        if choice == "u" and p:
            p = normalize(ui.edit_json(dict(p)), b)
            continue
        feedback = ui.ask("Co změnit") if choice == "k" else ""


def apply(cfg: dict, b: dict, p: dict) -> None:
    C.set_(cfg, "persona.name", b["name"])
    C.set_(cfg, "persona.core", p["core"])
    C.set_(cfg, "persona.language_rules", language_rules(b, p["style_rules"]))
    C.set_(cfg, "persona.interests_seed", p["interests_seed"])
    C.set_(cfg, "tts.voice", TTS_VOICE[b["gender"]])


def step(cfg: dict, gen, answers: dict, people: list) -> bool:
    b = ask_basics(answers)
    comp = step_companion(cfg, gen, b, answers)
    ui.header("Persona — generování identity")
    p = generate(gen, b, comp, people)
    if not p:
        ui.warn("Identita ponechána beze změny.")
        C.set_(cfg, "persona.name", b["name"])
        return False
    apply(cfg, b, p)
    answers["persona"] = p
    answers["identity_docs"] = identity_docs(p, b, comp)
    ui.ok("Persona %s nastavena (hlas TTS: %s)" % (b["name"], TTS_VOICE[b["gender"]]))
    return True


def dump(p: dict) -> str:
    return json.dumps(p, ensure_ascii=False, indent=2)
