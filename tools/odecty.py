#!/usr/bin/env python3
"""Vypíše odečty z data/ODECTY.md, jejichž termín nastal (a nejbližší další).
Ranní kontrola, krok 1b. Hotový odečet označ v souboru `- [x]`."""
import datetime, re, sys
dnes = datetime.date.today()
due, dalsi = [], []
for ln in open("data/ODECTY.md", encoding="utf-8"):
    m = re.match(r"- \[( |x)\] (\d{4}-\d{2}-\d{2}) \| (.*)", ln.strip())
    if not m or m.group(1) == "x":
        continue
    d = datetime.date.fromisoformat(m.group(2))
    (due if d <= dnes else dalsi).append((d, m.group(3)))
print("ODEČTY NA ŘADĚ (%s):" % dnes.isoformat())
for d, t in sorted(due):
    print("  ⏰ %s%s | %s" % (d.strftime("%-d. %-m."), " (PROŠLÉ)" if d < dnes else "", t))
if not due:
    print("  nic")
for d, t in sorted(dalsi)[:3]:
    print("  · dál %s | %s" % (d.strftime("%-d. %-m."), t.split("|")[0].strip()))
