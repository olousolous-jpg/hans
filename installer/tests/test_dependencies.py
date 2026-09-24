"""Hlídač závislostí: každý balíček importovaný v kódu Hanse musí instalátor znát.

Když někdo do Hanse přidá nový `import`, tenhle test selže a řekne, který modul
chybí. Pak stačí:
  - přidat pip balíček do installer/requirements.txt (nebo -optional.txt), nebo
  - přidat apt balíček do installer/apt-packages.txt, a
  - doplnit mapování modul → balíček do KNOWN níže (nebo důvod do IGNORED).

Čte se jen zdrojový kód (ast), nic se nespouští ani nemění.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent.parent
ROOT = INSTALLER.parent

# Kód, který běží na Pi (deploy/pc/ běží na PC, tam se instaluje jinak).
SOURCES = [ROOT / "main.py", ROOT / "web_admin.py",
           *sorted((ROOT / "scripts").glob("*.py")), *sorted((ROOT / "tools").glob("*.py"))]

# importovaný modul → (zdroj, název balíčku)
KNOWN = {
    "numpy": ("apt", "python3-numpy"),
    "cv2": ("apt", "python3-opencv"),
    "PIL": ("apt", "python3-pil"),
    "scipy": ("apt", "python3-scipy"),
    "sklearn": ("apt", "python3-sklearn"),
    "psutil": ("apt", "python3-psutil"),
    "requests": ("apt", "python3-requests"),
    "picamera2": ("apt", "python3-picamera2"),
    "libcamera": ("apt", "python3-libcamera"),
    "lgpio": ("apt", "python3-lgpio"),
    "spidev": ("apt", "python3-spidev"),
    "hailo_platform": ("apt", "hailo-all"),
    "fastapi": ("pip", "fastapi"),
    "uvicorn": ("pip", "uvicorn"),
    "pydantic": ("pip", "pydantic"),
    "edge_tts": ("pip", "edge-tts"),
    "webrtcvad": ("pip", "webrtcvad"),
    "noisereduce": ("pip", "noisereduce"),
    "openwakeword": ("pip", "openwakeword"),
    "onnxruntime": ("pip", "onnxruntime"),
    "bs4": ("pip", "beautifulsoup4"),
    "lxml": ("pip", "lxml"),
    "icalendar": ("pip", "icalendar"),
    "recurring_ical_events": ("pip", "recurring-ical-events"),
    "mobi": ("pip", "mobi"),
    "passlib": ("pip", "passlib"),
    "nio": ("pip", "matrix-nio"),
}

# Moduly, které instalátor záměrně neinstaluje přes seznamy — s důvodem.
IGNORED = {
    "robot_hat": "instaluje se zvlášť z gitu SunFounder v kroku venv (volitelný HW)",
    "blazedetector": "starý externí detektor dlaní; gesta dnes jdou přes yolov8s_pose",
}


def _lines(path: Path) -> set[str]:
    out = set()
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#", 1)[0].strip().lstrip("?")
        for alt in ln.split("|"):
            name = re.split(r"[<>=!~\[ ;]", alt.strip(), maxsplit=1)[0].lower()
            if name:
                out.add(name)
    return out


def third_party_imports() -> dict[str, set[str]]:
    std = set(sys.stdlib_module_names)
    local = {p.stem for p in (ROOT / "scripts").glob("*.py")} | {
        p.stem for p in (ROOT / "tools").glob("*.py")} | {
        "scripts", "tools", "main", "web_admin", "settings"}
    found: dict[str, set[str]] = {}
    for f in SOURCES:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module]
            else:
                continue
            for m in mods:
                root = m.split(".")[0]
                if root not in std and root not in local:
                    found.setdefault(root, set()).add(str(f.relative_to(ROOT)))
    return found


class TestDependencies(unittest.TestCase):
    def test_every_import_is_installed(self):
        pip = _lines(INSTALLER / "requirements.txt") | _lines(INSTALLER / "requirements-optional.txt")
        apt = _lines(INSTALLER / "apt-packages.txt")
        problems = []
        for mod, files in sorted(third_party_imports().items()):
            if mod in IGNORED:
                continue
            where = ", ".join(sorted(files)[:3])
            if mod not in KNOWN:
                problems.append("neznámý modul %r (v %s) — doplň balíček do installeru "
                                "a mapování do KNOWN" % (mod, where))
                continue
            src, pkg = KNOWN[mod]
            if pkg.lower() not in (apt if src == "apt" else pip):
                problems.append("modul %r (v %s): balíček %s chybí v %s" % (
                    mod, where, pkg, "apt-packages.txt" if src == "apt" else "requirements*.txt"))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
