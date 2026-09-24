"""Generátor textů: lokální LLM přes Ollamu, nebo ručně přes Claude/ChatGPT."""
from __future__ import annotations

import json
from typing import Optional

from . import ui
from .ollama import Ollama, OllamaError

SYSTEM = ("Jsi pečlivý pomocník, který píše konfiguraci pro českého domácího AI "
          "společníka. Píšeš spisovnou češtinou bez pravopisných chyb. Odpovídáš "
          "VÝHRADNĚ validním JSON objektem podle zadaného schématu, bez markdownu "
          "a bez komentářů.")


def extract_json(text: str) -> Optional[dict]:
    """Vytáhne první JSON objekt z textu (i obalený ```json … ```)."""
    if not text:
        return None
    i, j = text.find("{"), text.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        v = json.loads(text[i:j + 1])
        return v if isinstance(v, dict) else None
    except ValueError:
        return None


class OllamaGen:
    def __init__(self, url: str, model: str):
        self.client = Ollama(url)
        self.model = model
        self.label = "%s @ %s" % (model, url)

    def json(self, prompt: str, schema: dict, temperature: float = 0.7) -> Optional[dict]:
        msgs = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": prompt}]
        # Strukturovaný výstup (schema) umí Ollama ≥ 0.5; starší zná jen "json".
        for fmt in (schema, "json"):
            try:
                out = extract_json(self.client.chat(self.model, msgs, fmt=fmt,
                                                    temperature=temperature))
            except OllamaError:
                if fmt is schema:
                    continue
                raise
            if out:
                return out
        return None


class ManualGen:
    """Záloha bez lokálního LLM: prompt se zkopíruje do Claude/ChatGPT."""
    label = "ručně (Claude / ChatGPT)"

    def json(self, prompt: str, schema: dict, temperature: float = 0.7) -> Optional[dict]:
        keys = ", ".join(schema.get("properties", {}).keys())
        print("\n" + "=" * 72)
        print(">>> ZKOPÍRUJ text mezi čarami do Claude / ChatGPT <<<")
        print("=" * 72)
        print(SYSTEM + "\n\n" + prompt + "\n\nVrať jen JSON s klíči: " + keys)
        print("=" * 72)
        print("Vlož sem JSON odpověď a pak napiš samostatný řádek END:")
        return extract_json(ui.read_block())
