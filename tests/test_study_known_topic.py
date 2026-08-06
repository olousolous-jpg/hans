"""HANS_STUDY_KNOWN_TOPIC_V1 — nenabízej studium toho, co už umíš.

Doložený případ (6.8. 08:48–09:06): Hans 5× po sobě odpověděl „mám si to
nastudovat?" na témata, která má v kurikulu odškrtnutá jako hotová.
Běží proti ŽIVÉ DB — smyslem je ověřit skutečný stav studia.

Spuštění: python3 tests/test_study_known_topic.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hans_agent import _looks_like_recall  # noqa: E402
from scripts.hans_study import already_studied  # noqa: E402

OK = FAIL = 0


def t(label, got, exp):
    global OK, FAIL
    if got == exp:
        OK += 1
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      got={got!r}  exp={exp!r}")


# ── datová brzda: co je nastudované, se nenabízí ────────────────────────
# Pozn.: „Byzantská vojenská architektura" tu ZÁMĚRNĚ není — od 6.8. je
# označená jako PŘESKOČENÁ (`HANS_STUDY_SKIPPED_MARK_V1`), takže pokrytá
# být nesmí. Testuje to `test_study_skipped.py`.
for topic in ("hrady", "vývoj zbrojnic", "typologie hradů",
              "gotická architektura", "krizacke hrady v Levante"):
    t(f"pokryto: {topic!r}", bool(already_studied(topic)), True)

# Nenastudované se nabídnout SMÍ — jinak by Hans nešel nikdy dál.
for topic in ("Kunětická hora", "hrady ve východních Čechách",
              "Jára Cimrman", "hradní zahrady a symbolika",
              "kvantová fyzika", "vaření těstovin"):
    t(f"NEpokryto: {topic!r}", already_studied(topic), None)

# Prázdný / nesmyslný vstup nesmí spadnout
t("prázdný vstup", already_studied(""), None)
t("jen mezery", already_studied("   "), None)

# ── regexový guard: formulace, které dřív propadly ─────────────────────
# Doslovné věty uživatele z 6.8. — všechny znamenají „řekni mi TEĎ".
for q in ("co me muzes rict o krizackych stavebnich technikach?",
          "co muzes rici o byzantske vojenske technice?",
          "povez mi neco o hradech ?",
          "vice nezjistuj, rekni co o ni vis",
          "ne, jen rekni co uz vis",
          "co vis o dadstine?",
          "kdo je Bud Spencer?"):
    t(f"recall: {q[:42]!r}", _looks_like_recall(q), True)

# Skutečné pokyny ke studiu musí projít dál (jinak by Hans přestal studovat).
for q in ("nastuduj vice o Cimrmanovi", "zjisti si neco o hradech",
          "nauc se o byzantske architekture", "podivej se na Kuneticku horu"):
    t(f"pokyn ke studiu: {q[:42]!r}", _looks_like_recall(q), False)

print(f"\n{OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
