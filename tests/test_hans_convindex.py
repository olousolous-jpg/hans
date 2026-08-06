"""HANS_CONVINDEX_V1 — regresní sada indexu rozhovorů.

Běží proti ŽIVÉMU indexu (`data/hans_conv_index.db`), protože jeho smysl je
najít reálné rozhovory. Dotazy jsou doslovné z deníku 4.–6. 8. 2026.
Spuštění: python3 tests/test_hans_convindex.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.hans_convindex import (  # noqa: E402
    _fts_query, _stem, format_hits, search, stats)

OK = FAIL = 0


def t(label, got, exp):
    global OK, FAIL
    if got == exp:
        OK += 1
    else:
        FAIL += 1
        print(f"  ✗ {label}\n      got={got!r}\n      exp={exp!r}")


# ── kmen ────────────────────────────────────────────────────────────────
# Prefix sám nestačí, když se mění kmen: „bratrich“ → „bratri“ (doloženo
# živým testem 6.8., kdy „/rozhovory s Kolacem o Bratrich“ nenašlo nic).
t("kmen bratrich", _stem("bratrich"), "bratri")
t("kmen kolacem", _stem("kolacem"), "kolac")
t("krátké slovo beze změny", _stem("rip"), "rip")

# ── stavba FTS dotazu ───────────────────────────────────────────────────
t("výplňková slova vypadnou", _fts_query("o cem jsme se bavili"), "")
t("prefix na obsahovém slovu", _fts_query("kentaur"), '"kentaur"*')
q = _fts_query("rozhovor s Kolacem o Bratrich")
t("stopwords pryč, obsah zůstane", q, '"kolacem"* "bratrich"*')
t("FTS operátory se nepropašují",
  "OR" in _fts_query("kocka OR pes") or "NOT" in _fts_query("a NOT b"), False)

# ── vyhledávání: musí NAJÍT ────────────────────────────────────────────
FOUND = [
    ("s Kolacem o Bratrich", {"source": "teddy_dialog"}),  # doložený 19:23
    ("Bratri", {}),
    ("kentaura", {}),          # skloňování (v datech „kentaura“)
    ("malovani", {}),          # bez diakritiky
    ("muzeum vychodnich cech", {}),
    ("Cimrman", {}),
    ("Hartenberg", {}),
]
for q_, kw in FOUND:
    t(f"najde: {q_!r}", len(search(q_, limit=3, **kw)) > 0, True)

# ── vyhledávání: NESMÍ najít ───────────────────────────────────────────
# Falešný nález je u recallu nejhorší chyba — Hans by tvrdil, že jsme
# o něčem mluvili, a doložil to nesouvisejícím záznamem. Proto AND-only
# (OR fallback byl 6.8. odstraněn, vracel náhodné rozhovory s Koláčem).
for q_ in ("uplne vymysleny nesmysl xyzzy",
           "kvantova teleportace mravencu", "zzz qqq www"):
    t(f"nenajde: {q_!r}", len(search(q_, limit=3)), 0)

# Pozn.: „Xqzybwrt Flurbex“ nález MÁ a je správný — na to se uživatel
# 4.8. 06:34 skutečně ptal. Nesmysl v dotazu ≠ nesmysl v datech.
t("existující rozhovor o nesmyslu se najde",
  len(search("Xqzybwrt Flurbex", limit=2)) > 0, True)

# ── filtr na zdroj ─────────────────────────────────────────────────────
rows = search("Bratri", limit=5, source="teddy_dialog")
t("filtr source drží", all(r[1] == "teddy_dialog" for r in rows), True)

# ── formátování ────────────────────────────────────────────────────────
t("prázdný vstup → prázdno", format_hits([]), "")
out = format_hits(search("Bratri", limit=1))
t("výpis nese datum i obsah", bool(out) and "2026" in out, True)

# ── index je naplněný ──────────────────────────────────────────────────
st = stats()
t("index má všechny tři zdroje",
  all(k in st for k in ("human_chat", "teddy_dialog", "chat_reflection")), True)
t("index není prázdný", st.get("total", 0) > 1000, True)

print(f"\n{OK} OK, {FAIL} FAIL   (index: {st.get('total')} rozhovorů)")
sys.exit(1 if FAIL else 0)
