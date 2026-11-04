#!/usr/bin/env python3
"""HANS_OSM_POI_V1 (3. 11. 2026) — praktické údaje o místech z OpenStreetMap offline.

Z výřezu Česka (`data/osm/czech_republic-latest.osm.pbf`, OpenStreetMap France,
Geofabrik ze sítě Hanse neodpovídá) vytáhne do `data/osm/poi.db`:
  poi(lat, lon, druh, nazev, otevreno, web, wikidata, poplatek)  — památky, muzea,
      vyhlídky, jídlo (restaurace, kavárny, hospody, bistra), parkoviště
  trasa(id, nazev, barva, znacka, od, do_) + trasa_bod(trasa, lat, lon) — značené
      pěší trasy (route=hiking), body = vzorek uzlů jejich cest (každý ~10.)
Veřejné API Overpass na dotazy v chatu nestačí (3. 11.: 34 s / přetíženo).

    python3 tools/osm_poi.py              # stáhne nejnovější výřez a sestaví
    python3 tools/osm_poi.py --bez-stazeni
Data © přispěvatelé OpenStreetMap, ODbL.
"""
import os
import sqlite3
import subprocess
import sys
import time

import osmium

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://download.openstreetmap.fr/extracts/europe/czech_republic-latest.osm.pbf"
PBF = os.path.join(ROOT, "data", "osm", "czech_republic-latest.osm.pbf")
DB = os.path.join(ROOT, "data", "osm", "poi.db")

_HIST = {"castle", "ruins", "monastery", "fort", "city_gate", "archaeological_site", "memorial",
         "monument", "tower", "manor", "church"}
_TOUR = {"museum", "gallery", "viewpoint", "attraction", "zoo", "theme_park", "artwork"}
_JIDLO = {"restaurant", "cafe", "pub", "fast_food", "biergarten", "ice_cream"}
_MISTA_AM = {"place_of_worship", "theatre", "arts_centre", "cinema", "library"}


def _druh(t):
    """(druh, je_parkoviste) nebo (None, False)."""
    h, tu, am = t.get("historic"), t.get("tourism"), t.get("amenity")
    if am == "parking":
        return "parkoviště", True
    if am in _JIDLO:
        return am, False
    if h in _HIST:
        return ("castle:" + (t.get("castle_type") or "")) if h == "castle" else h, False
    if tu in _TOUR:
        return tu, False
    if t.get("man_made") == "tower" and t.get("tower:type") == "observation":
        return "rozhledna", False
    if am in _MISTA_AM:
        return am, False
    return None, False


class _Poi(osmium.SimpleHandler):
    def __init__(self, db):
        super().__init__()
        self.db, self.n, self.trasy_cesty = db, 0, {}

    def _uloz(self, o, lat, lon):
        t = {k: v for k, v in o.tags}
        d, park = _druh(t)
        if not d:
            return
        jm = t.get("name") or ""
        if not jm and not park:
            return
        if park and not (jm or t.get("fee")):
            return                      # bezejmenná parkoviště bez poplatku — tisíce, k ničemu
        self.db.execute("INSERT INTO poi VALUES (?,?,?,?,?,?,?,?)",
                        (lat, lon, d, jm, t.get("opening_hours") or "",
                         t.get("website") or t.get("contact:website") or "",
                         t.get("wikidata") or "", t.get("fee") or ""))
        self.n += 1

    def node(self, n):
        if n.tags and n.location.valid():
            self._uloz(n, n.location.lat, n.location.lon)

    def area(self, a):
        try:
            if not a.tags:
                return
            t = {k: v for k, v in a.tags}
            if not _druh(t)[0]:
                return
            ring = next(iter(a.outer_rings()))
            pts = [(p.lat, p.lon) for p in ring if p.location.valid()]
            if pts:
                self._uloz(a, sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        except Exception:
            pass

    def relation(self, r):
        t = {k: v for k, v in r.tags}
        if t.get("route") != "hiking":
            return
        rid = r.id
        self.db.execute("INSERT OR REPLACE INTO trasa VALUES (?,?,?,?,?,?)",
                        (rid, t.get("name") or "", t.get("colour") or t.get("kct_barva") or "",
                         t.get("osmc:symbol") or "", t.get("from") or "", t.get("to") or ""))
        for m in r.members:
            if m.type == "w":
                self.trasy_cesty.setdefault(m.ref, []).append(rid)


class _Body(osmium.SimpleHandler):
    def __init__(self, db, cesty):
        super().__init__()
        self.db, self.cesty, self.n = db, cesty, 0

    def way(self, w):
        tr = self.cesty.get(w.id)
        if not tr:
            return
        pts = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        for i, (a, b) in enumerate(pts):
            if i % 10 == 0 or i == len(pts) - 1:
                for t in tr:
                    self.db.execute("INSERT INTO trasa_bod VALUES (?,?,?)", (t, a, b))
                    self.n += 1


def sestav(pbf: str, db: str) -> None:
    tmp = db + ".nova"
    if os.path.exists(tmp):
        os.remove(tmp)
    c = sqlite3.connect(tmp)
    c.executescript("""
        CREATE TABLE poi(lat REAL, lon REAL, druh TEXT, nazev TEXT, otevreno TEXT, web TEXT,
                         wikidata TEXT, poplatek TEXT);
        CREATE TABLE trasa(id INTEGER PRIMARY KEY, nazev TEXT, barva TEXT, znacka TEXT,
                           od TEXT, do_ TEXT);
        CREATE TABLE trasa_bod(trasa INTEGER, lat REAL, lon REAL);""")
    t0 = time.time()
    h = _Poi(c)
    h.apply_file(pbf, locations=True, idx="flex_mem")
    print("místa: %d, tras: %d, cest tras: %d (%.0f s)" % (
        h.n, c.execute("SELECT count(*) FROM trasa").fetchone()[0], len(h.trasy_cesty),
        time.time() - t0), flush=True)
    b = _Body(c, h.trasy_cesty)
    b.apply_file(pbf, locations=True, idx="flex_mem")
    print("bodů tras: %d (%.0f s)" % (b.n, time.time() - t0), flush=True)
    c.executescript("CREATE INDEX ix_poi ON poi(lat); CREATE INDEX ix_tb ON trasa_bod(lat);")
    c.commit()
    c.close()
    os.replace(tmp, db)


if __name__ == "__main__":
    if "--bez-stazeni" not in sys.argv:
        os.makedirs(os.path.dirname(PBF), exist_ok=True)
        subprocess.run(["curl", "-sS", "-L", "-C", "-", "--retry", "5", "-A", "Hans/1.0 (osobni asistent)",
                        "-o", PBF + ".tmp", URL], check=True, timeout=7200)
        os.replace(PBF + ".tmp", PBF)
    sestav(PBF, DB)
    print("hotovo →", DB, "%.1f MB" % (os.path.getsize(DB) / 1e6))
