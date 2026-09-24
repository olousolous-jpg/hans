"""Krok „domácnost": kdo s personou bydlí a jak se jména skloňují.

Zapisuje do privátní části configu (config_io je tam pošle samo podle názvu
sekce):
  known_persons.<klíč>      — rod, poznámka a pády jména (vokativ pro oslovení)
  relationship_seed.<klíč>  — role a rodinné vazby (seed vztahových karet)
  person_name_forms.<klíč>  — tvary jména malými písmeny (hledání jména v textu)

Klíč osoby = křestní jméno malými písmeny bez diakritiky. MUSÍ se shodovat se
jménem, pod kterým se osoba později zapíše do rozpoznávání obličejů.
"""
from __future__ import annotations

import json
import unicodedata
from collections import OrderedDict
from typing import Optional

from . import cfg as C
from . import ui

CASES = (("gen", "2. pád (bez koho?)"), ("dat", "3. pád (komu?)"),
         ("acc", "4. pád (koho?)"), ("loc", "6. pád (o kom?)"),
         ("voc", "5. pád — OSLOVENÍ"))


def key_of(name: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", name.split()[0])
                if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def _fallback_forms(nom: str, gender: str) -> dict:
    """Bez LLM: vokativ a akuzativ podle pravidel z Hansova cz_names."""
    forms = {"gen": nom, "dat": nom, "acc": nom, "loc": nom, "voc": nom}
    try:
        C.config_io()  # přidá kořen do sys.path
        from scripts import cz_names
        forms["voc"] = cz_names.vocative(nom, gender, config={})
        forms["acc"] = cz_names.accusative(nom, gender, config={})
    except Exception:
        pass
    return forms


_SCHEMA = {"type": "object",
           "properties": {k: {"type": "string"} for k, _ in CASES},
           "required": [k for k, _ in CASES]}


def llm_forms(gen, nom: str, gender: str) -> Optional[dict]:
    if gen is None:
        return None
    prompt = (
        "Vyskloňuj české křestní jméno „%s“ (%s). Vrať JSON s klíči gen, dat, acc, "
        "loc, voc — tvary 2., 3., 4., 6. a 5. pádu jednotného čísla, BEZ předložek, "
        "s velkým počátečním písmenem. Příklad pro Karel (muž): "
        '{"gen":"Karla","dat":"Karlovi","acc":"Karla","loc":"Karlovi","voc":"Karle"}. '
        "Příklad pro Eva (žena): "
        '{"gen":"Evy","dat":"Evě","acc":"Evu","loc":"Evě","voc":"Evo"}.'
        % (nom, "mužské jméno" if gender == "muž" else "ženské jméno"))
    try:
        data = gen.json(prompt, _SCHEMA, temperature=0.0)
    except Exception as e:
        ui.warn("Skloňování přes LLM selhalo (%s) — použiju pravidla." % e)
        return None
    if not data or not all(isinstance(data.get(k), str) and data[k].strip() for k, _ in CASES):
        return None
    return {k: data[k].strip() for k, _ in CASES}


def _ask_person(gen, existing: Optional[dict] = None) -> Optional[dict]:
    nom = ui.ask("Jméno (1. pád, např. Karel) — Enter = konec", (existing or {}).get("nom", ""))
    if not nom:
        return None
    g_def = (existing or {}).get("gender", "žena" if nom.endswith("a") else "muž")
    gender = ui.choose("Rod:", [("m", "muž"), ("z", "žena")], "z" if g_def == "žena" else "m")
    gender = "žena" if gender == "z" else "muž"
    note = ui.ask("Kdo to je (např. pán domu, paní domu, syn, host)",
                  (existing or {}).get("notes", ""))
    forms = llm_forms(gen, nom, gender) or _fallback_forms(nom, gender)
    ui.info("Pády jména (Enter = souhlasí, jinak napiš správný tvar):")
    for k, label in CASES:
        forms[k] = ui.ask("  %-26s" % label, forms[k]) or forms[k]
    p = OrderedDict(gender=gender, notes=note, nom=nom)
    p.update((k, forms[k]) for k, _ in CASES)
    return p


def step(cfg: dict, gen, answers: dict) -> list[dict]:
    ui.header("Domácnost")
    ui.info("Koho bude persona znát a oslovovat jménem. Z pádů se sestaví pravidla")
    ui.info("oslovení (čeština chce vokativ: „Karle“, ne „Karel“).")
    ui.info("Klíč osoby (jméno bez diakritiky, malými) musí později sedět s jménem při")
    ui.info("zápisu obličeje do rozpoznávání.")
    people = list(answers.get("household") or [])
    if people:
        ui.info("Uloženo z minula: " + ", ".join(p["nom"] for p in people))
        if not ui.confirm("Ponechat?", True):
            people = []
    if not people:
        while True:
            print()
            p = _ask_person(gen)
            if not p:
                break
            people.append(p)
    if people and len(people) > 1 and ui.confirm("Zadat rodinné vazby (partner, děti)?", False):
        keys = {key_of(p["nom"]): p for p in people}
        for p in people:
            k = key_of(p["nom"])
            links = OrderedDict()
            partner = key_of(ui.ask("Partner osoby %s (jméno, Enter = nikdo)" % p["nom"], "") or "x")
            if partner in keys and partner != k:
                links["spouse"] = partner
            kids = [key_of(x) for x in ui.ask("Děti osoby %s (jména oddělená čárkou)" % p["nom"], "").split(",") if x.strip()]
            kids = [x for x in kids if x in keys and x != k]
            if kids:
                links["children"] = kids
            p["family_links"] = links
        for p in people:  # dopočítej rodiče z dětí
            for kid in p.get("family_links", {}).get("children", []):
                keys[kid].setdefault("family_links", OrderedDict()).setdefault("parents", [])
                if key_of(p["nom"]) not in keys[kid]["family_links"]["parents"]:
                    keys[kid]["family_links"]["parents"].append(key_of(p["nom"]))
    answers["household"] = people
    apply(cfg, people)
    return people


def apply(cfg: dict, people: list[dict]) -> None:
    kp, rs, nf = OrderedDict(), OrderedDict(), OrderedDict()
    for p in people:
        k = key_of(p["nom"])
        kp[k] = OrderedDict((f, p[f]) for f in ("gender", "notes", "nom", "gen", "dat", "acc", "loc", "voc") if p.get(f))
        rs[k] = OrderedDict(display_name=p["nom"], role=p.get("notes", ""),
                            family_links=p.get("family_links") or {})
        forms = []
        for f in ("nom", "gen", "dat", "acc", "loc", "voc"):
            v = (p.get(f) or "").lower()
            if v and v not in forms:
                forms.append(v)
        nf[k] = forms
    cfg["known_persons"] = kp
    cfg["relationship_seed"] = rs
    cfg["person_name_forms"] = nf
    C.set_(cfg, "persona.address_rules", address_rules(people))


def address_rules(people: list[dict]) -> str:
    """Deterministicky z pádů — tahle pravidla se LLM nesvěřují."""
    parts = []
    for p in people:
        if p.get("voc") and p["voc"] != p["nom"]:
            parts.append("Při oslovení osoby jménem %s používej vokativ %s (ne %s)."
                         % (p["nom"], p["voc"], p["nom"]))
    parts.append("Nikdy nepoužívej nominativ jako oslovení. Nikdy neopakuj jméno na konci věty.")
    return " ".join(parts)


def describe(people: list[dict]) -> str:
    """Krátký popis domácnosti pro prompt generátoru persony (jen jména a role)."""
    if not people:
        return "(domácnost zatím neuvedena)"
    return "; ".join("%s (%s%s)" % (p["nom"], p["gender"], ", " + p["notes"] if p.get("notes") else "")
                     for p in people)


def dump(people) -> str:
    return json.dumps(people, ensure_ascii=False, indent=2)
