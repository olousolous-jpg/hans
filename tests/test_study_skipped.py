"""HANS_STUDY_SKIPPED_MARK_V1 — přeskočené pod-téma není nastudované.

Nález uživatele 6.8.: `/studium` kreslilo u přeskočeného pod-tématu ✓.
Stav se odvozoval jen z `current_index`, takže „prošel jsem kolem"
a „nastudoval jsem" vypadaly stejně.

Běží na KOPII databáze — živé DB se nedotýká.
Spuštění: python3 tests/test_study_skipped.py
"""
import json
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.chat_commands import _cmd_studium  # noqa: E402
from scripts.hans_study import (  # noqa: E402
    StudyStore, already_studied, _stem_tokens)

OK = FAIL = 0


def t(label, got, exp):
    global OK, FAIL
    if got == exp:
        OK += 1
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      got={got!r}  exp={exp!r}")


tmp = Path(tempfile.mkdtemp()) / "diary.db"
shutil.copy2(ROOT / "data/hans_diary.db", tmp)

store = StudyStore.__new__(StudyStore)
store._diary_path = str(tmp)
store._init_db()          # idempotentní ALTER přidá skipped_idx

db = sqlite3.connect(tmp)
row = db.execute("SELECT id, curriculum, current_index FROM study_program "
                 "WHERE status='active' AND current_index > 1 "
                 "ORDER BY id LIMIT 1").fetchone()
if not row:
    print("přeskakuji — v DB není rozstudovaný program")
    sys.exit(0)
pid, curr, idx = row
subs = json.loads(curr)

# Sloupec existuje a je čitelný. POZOR: od backfillu 6.8. už nese
# historická přeskočení, takže se NEsmí očekávat prázdno — test proto
# pracuje s PŘÍRŮSTKEM, ne s absolutní hodnotou.
ap = store.get_active_program()
t("skipped_idx je množina", isinstance(ap.get("skipped_idx"), set), True)
before = set(ap.get("skipped_idx") or set())

# Vyber dokončené pod-téma, které NEPOKRÝVÁ žádné jiné. Jinak by test
# selhal právem: `already_studied` hlásí pokrytí už při dvou shodných
# významových slovech, takže „Románské prvky v gotické architektuře"
# pokryje i „Architektonické vlivy: normanský versus románský styl".
_stem_ = _stem_tokens  # totéž kritérium, jaké používá already_studied
done = subs[:int(idx)]
target = sub_name = None
for i, s in enumerate(done):
    if i in before:                      # tenhle už přeskočený je
        continue
    if all(len(_stem_(s) & _stem_(x)) < 2
           for j, x in enumerate(done) if j != i):
        target, sub_name = i, s
        break
if target is None:
    print("přeskakuji — všechna pod-témata se navzájem překrývají")
    sys.exit(0)

# ── před označením se pod-téma tváří jako nastudované ───────────────────
t("před: téma je pokryté", bool(already_studied(sub_name, str(tmp))), True)

store.mark_skipped(pid, target)

# ── po označení už NE — jinak by Hans odmítl nabídnout studium ──────────
t("po: přeskočené NENÍ pokryté", already_studied(sub_name, str(tmp)), None)
t("zapsáno do programu",
  store.get_active_program().get("skipped_idx"), before | {target})

# ── ostatní pod-témata zůstávají nedotčená ─────────────────────────────
other = next((s for i, s in enumerate(subs[:int(idx)])
              if i != target and i not in before), None)
if other:
    t("jiné pod-téma zůstává pokryté",
      bool(already_studied(other, str(tmp))), True)

# ── idempotence: opakované označení nic nerozbije ──────────────────────
store.mark_skipped(pid, target)
t("idempotentní",
  store.get_active_program().get("skipped_idx"), before | {target})

# ── výpis /studium kreslí ⤼, ne ✓ ─────────────────────────────────────
class _H:
    config = {"diary_db": str(tmp)}


out = _cmd_studium(_H(), "uzivatel", "")
line = next((l for l in out.splitlines() if sub_name[:30] in l), "")
t("výpis kreslí ⤼", line.strip().startswith("⤼"), True)
t("výpis NEkreslí ✓", "✓" in line, False)
t("výpis má legendu", "přeskočeno" in out, True)

shutil.rmtree(tmp.parent, ignore_errors=True)
print(f"\n{OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
