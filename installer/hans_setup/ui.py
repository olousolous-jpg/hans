"""Interakce v terminálu."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

ASSUME_YES = False  # nastavuje wizard z --yes


def _tty() -> bool:
    return sys.stdin.isatty() and not ASSUME_YES


def header(title: str) -> None:
    print("\n\033[1m━━ %s %s\033[0m" % (title, "━" * max(3, 60 - len(title))))


def info(msg: str) -> None:
    print("  " + msg)


def ok(msg: str) -> None:
    print("\033[32m  ✓ %s\033[0m" % msg)


def warn(msg: str) -> None:
    print("\033[33m  ⚠ %s\033[0m" % msg)


def mask(v: str) -> str:
    return (v[:4] + "…" + v[-2:]) if len(v) > 8 else ("***" if v else "")


def ask(prompt: str, default: str = "", required: bool = False, secret: bool = False) -> str:
    if not _tty():
        return default
    while True:
        shown = mask(default) if secret else default
        hint = " [%s]" % shown if shown else ""
        try:
            ans = input("  %s%s: " % (prompt, hint)).strip()
        except EOFError:
            ans = ""
        ans = ans or default
        if ans or not required:
            return ans
        print("    (povinné)")


def confirm(prompt: str, default: bool = True) -> bool:
    if not _tty():
        return default
    hint = "[A/n]" if default else "[a/N]"
    try:
        ans = input("  %s %s: " % (prompt, hint)).strip().lower()
    except EOFError:
        ans = ""
    if not ans:
        return default
    return ans in ("a", "ano", "y", "yes")


def choose(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    """options = [(klíč, popis)], vrací klíč."""
    print("  " + prompt)
    for key, desc in options:
        print("    %s) %s%s" % (key, desc, "   ← výchozí" if key == default else ""))
    keys = {k for k, _ in options}
    while True:
        ans = ask("Volba", default)
        if ans in keys:
            return ans
        print("    Neplatná volba.")


def read_block(end: str = "END") -> str:
    """Víceřádkový vstup až po řádek obsahující jen `end` (nebo EOF)."""
    lines = []
    while True:
        try:
            ln = input()
        except EOFError:
            break
        if ln.strip() == end:
            break
        lines.append(ln)
    return "\n".join(lines)


def edit_json(data: dict) -> dict:
    """Otevře JSON v editoru ($EDITOR, jinak nano/vi) a vrátí upravený obsah.
    Při chybě parsování se nabídne oprava znovu."""
    editor = os.environ.get("EDITOR") or next(
        (e for e in ("nano", "vi") if shutil.which(e)), None)
    if not editor:
        warn("Nenašel jsem editor (nastav proměnnou EDITOR).")
        return data
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        path = fh.name
    try:
        while True:
            subprocess.call([editor, path])
            try:
                with open(path, encoding="utf-8") as fh:
                    return json.load(fh)
            except ValueError as e:
                warn("Neplatný JSON: %s" % e)
                if not confirm("Otevřít znovu a opravit?", True):
                    return data
    finally:
        os.unlink(path)
