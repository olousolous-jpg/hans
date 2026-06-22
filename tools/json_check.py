#!/usr/bin/env python3
"""
JSON Validator - kontrola formátu JSON souboru.
Ověří, zda má soubor správnou syntaxi JSON (čárky, mezery, závorky, uvozovky).
"""

import sys
import json
from pathlib import Path


def validate_json_file(file_path: str) -> bool:
    """
    Zkontroluje, zda je soubor platný JSON.
    Vrací True, pokud je platný, jinak False.
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Pokus o parsování JSON
        json.loads(content)
        return True
    
    except FileNotFoundError:
        print(f"CHYBA: Soubor '{file_path}' nebyl nalezen.", file=sys.stderr)
        return False
    
    except json.JSONDecodeError as e:
        print(f"CHYBA: Neplatný JSON v souboru '{file_path}'", file=sys.stderr)
        print(f"  Pozice: řádek {e.lineno}, sloupec {e.colno}", file=sys.stderr)
        print(f"  Chyba: {e.msg}", file=sys.stderr)
        # Zobrazení problematického řádku
        if e.doc:
            lines = e.doc.splitlines()
            if e.lineno <= len(lines):
                print(f"  Kontext: {lines[e.lineno-1].strip()}", file=sys.stderr)
        return False


def main():
    if len(sys.argv) != 2:
        print("Použití: python json_validator.py <cesta_k_json_souboru>")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    if validate_json_file(file_path):
        print(f"✓ Soubor '{file_path}' je platný JSON.")
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()