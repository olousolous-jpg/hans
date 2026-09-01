"""HANS_THREAD_V1 — regresní sada vlákna rozhovoru.

Případy jsou DOSLOVNÉ věty z reálných rozhovorů (deník 4.–6. 8. 2026),
ne vymyšlené. Spuštění: python3 tests/test_hans_thread.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hans_thread import (  # noqa: E402
    extract_subject, has_own_subject, is_correction, resolve_reference,
    third_party_scope, note_outcome, should_suppress, reset)

OK = FAIL = 0


def t(label, got, exp):
    global OK, FAIL
    if got == exp:
        OK += 1
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      got={got!r}\n      exp={exp!r}")


# ── A3: detekce korekce ─────────────────────────────────────────────────
for s in ("myslel jsem rozhovor s Kolacem",
          "to nebyla kritika, myslel jsem co jsis odnesl ze studia",
          "ze jsi v jednom kole jsem na mysli, ze mas hodne prace",
          "ne, ptal jsem se na neco jineho"):
    t(f"korekce: {s[:40]}", is_correction(s), True)

for s in ("jak slo malovani?", "namaluj kocku", "pust film kruh",
          "co se deje doma?", "jak se mas?", "nudis se?",
          "a neco z vychodnich cech jsis zapamatoval?",
          "co vis o historii vychodnich cech?"):
    t(f"NEkorekce: {s[:40]}", is_correction(s), False)

# ── předmět z předchozí repliky ─────────────────────────────────────────
t("uvozovky v konverzaci",
  extract_subject('Koláč a já jsme se před chvílí bavili o „Bratři“, pane.'),
  "Bratři")
t("uvozovky v malování",
  extract_subject('S radostí, pane — maluji obraz na téma „zensky kentaur".'),
  "zensky kentaur")
t("uvozovky ve výpisu jsou legitimní téma",
  extract_subject('Studuji: „hrady a historická architektura" — pod-téma 9 '
                  'z 12:\n   ✓ Románské prvky\n   → Byzantská architektura'),
  "hrady a historická architektura")
# Regrese 6.8.: Title-case fallback tahal „Teplota" z výpisu /stav.
t("výpis /stav nedá téma",
  extract_subject("Stav systému:\nTeplota CPU: 60 °C\nZátěž CPU: 12 %"), "")
t("výpis /zdravi nedá téma",
  extract_subject("Stav mých systémů, pane:\n✅ Mozek — ok\n✅ Kamera — vidí"),
  "")
t("konverzační Title-case projde",
  extract_subject("Naposledy jsem tu zahlédl Janu, teď tu nikoho nevidím."),
  "Janu")

# ── vlastní předmět ─────────────────────────────────────────────────────
t("má předmět: jak slo malovani", has_own_subject("jak slo malovani?"), True)
t("nemá předmět: to znova prosim", has_own_subject("to znova prosim"), False)
t("nemá předmět: a co ono?", has_own_subject("a co ono?"), False)

# ── A1: rozřešení odkazu ────────────────────────────────────────────────
TURNS = [("user", "jak slo malovani?"),
         ("assistant", 'Koláč a já jsme se před chvílí bavili o „Bratři“, pane.')]
res, subj = resolve_reference("myslel jsem rozhovor s Kolacem", TURNS)
t("doplní předmět z repliky", subj, "Bratři")
t("originál zůstane uvnitř", "myslel jsem rozhovor s Kolacem" in res, True)
t("samostatná věta se nemění",
  resolve_reference("namaluj psa", TURNS), ("namaluj psa", ""))
t("předmět už ve větě → nedoplňuj",
  resolve_reference("co je Bratři?", TURNS)[1], "")

# ── A4: rozhovor s třetí stranou ────────────────────────────────────────
t("s Kolacem", third_party_scope("myslel jsem rozhovor s Kolacem", {}), "Koláč")
t("s Koláčem (diakritika)",
  third_party_scope("o cem jste se bavili s Koláčem?", {}), "Koláč")
t("náš rozhovor → ne", third_party_scope("o cem jsme se bavili?", {}), "")

# HANS_THREAD_TP_FOLLOWUP_V1 — navazující věta zdědí třetí stranu z vlákna
_TP_KOLAC = [("assistant", "Koláč a já jsme se bavili o Norimberský proces.")]
_TP_JINY = [("assistant", "Dnes jsem maloval obraz Sen a četl o hradech.")]
t("navazující + Koláč ve vlákně",
  third_party_scope("o cem naposledy", {}, turns=_TP_KOLAC), "Koláč")
t("navazující bez předmětu + vlákno",
  third_party_scope("o cem to bylo?", {}, turns=_TP_KOLAC), "Koláč")
t("„jsme\" zůstává u tazatele i s vláknem",
  third_party_scope("o cem jsme se bavili?", {}, turns=_TP_KOLAC), "")
t("„jsme\" bez otazníku taky",
  third_party_scope("o cem jsme mluvili vcera", {}, turns=_TP_KOLAC), "")
t("vlákno bez Koláče → nedoplňuj",
  third_party_scope("o cem naposledy", {}, turns=_TP_JINY), "")
t("nenavazující věta → nedoplňuj",
  third_party_scope("co je noveho", {}, turns=_TP_KOLAC), "")

# ── A2: anti-repeat brzda ───────────────────────────────────────────────
reset()
note_outcome("uzivatel", "web", "rozhovory", "v jakem kontextu jste se bavili")
t("korekce → potlač",
  should_suppress("uzivatel", "web", "rozhovory", "myslel jsem rozhovor s Kolacem"),
  True)
reset()
note_outcome("uzivatel", "web", "namaluj", "namaluj kocku")
t("doslovné opakování NEpotlačovat (5.8. 08:34-08:50)",
  should_suppress("uzivatel", "web", "namaluj", "namaluj kocku"), False)
reset()
note_outcome("uzivatel", "web", "film", "pust film kruh")
t("jiná cesta → nepotlačovat",
  should_suppress("uzivatel", "web", "rozhovory", "myslel jsem X"), False)
reset()
t("bez předchozí cesty → nepotlačovat",
  should_suppress("uzivatel", "web", "rozhovory", "myslel jsem X"), False)

# -- A1: napoveda na HRANICI SLOV (HANS_THREAD_ANAPHORA_WORDBOUND_V1, 26.8.) --
# Doslovna veta z testovaciho rozhovoru 26.8.: 'pre<myslel jsem>' spoustelo
# rozreseni odkazu ve vete, ktera zadny odkaz nema -> 'odkaz rozresen -> Oldu'.
# Popisky schvalne BEZ ceskych uvozovek: ASCII " uvnitr nich ukonci retezec.
_TURNS_JMENO = [("assistant",
                "Naposledy jsem tu zahledl Standu, ted tu ale nikoho nevidim.")]
t("premyslel jsem NENI myslel jsem",
  resolve_reference(
      "Poslys, premyslel jsem, ze bych ti dal na starost neco navic, "
      "jenze si nejsem uplne jisty, co vsechno vlastne umis?", _TURNS_JMENO)[1],
  "")
t("prava napoveda myslel jsem dal rozresi",
  resolve_reference("myslel jsem to jinak", _TURNS_JMENO)[1], "Standu")
t("zkus to dal rozresi",
  resolve_reference("zkus to znovu", _TURNS_JMENO)[1], "Standu")
t("veta s vlastnim predmetem se nerozresuje",
  resolve_reference("co ted delas?", _TURNS_JMENO)[1], "")

print(f"\n{OK} OK, {FAIL} FAIL")
sys.exit(1 if FAIL else 0)
