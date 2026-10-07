#!/usr/bin/env python3
"""
servo_stred — nastavení STŘEDU na víc servech po jednom (SunFounder robot hat).

Vybereš servo číslem, jemně s ním dojedeš tam, kde má být střed, a Enterem
polohu uložíš. Hodí se i při montáži: klávesa 0 pošle servo na 0° (neutrál
1500 µs), aby šla páka nasadit do středu zdvihu.

Použití:
  python3 tools/servo_stred.py --gui        # okno v prohlížeči: http://<Pi>:7873
  python3 tools/servo_stred.py --gui 0-6    # totéž, rovnou s vybranými kanály
  python3 tools/servo_stred.py              # terminál; zeptá se na kanály (výchozí 0-6)
  python3 tools/servo_stred.py 0-6          # P0 až P6
  python3 tools/servo_stred.py 2 3 4 5 6 7 9
  python3 tools/servo_stred.py 0-6 --limit 60   # větší povolený rozsah (výchozí ±40°)

GUI (--gui): nahoře zaškrtneš, která serva (P0–P11) chceš vidět; každé má
vlastní kartu s tlačítky. Platí stejná pravidla jako v terminálu — první pohyb
serva je až po potvrzení úhlu, meze MIN/MAX ukládáš jen ty. Port: --port N.

Ovládání v terminálu:
  1 … 9         -- vyber servo podle pořadí v seznamu (při výběru se NEHNE)
  ← / →         -- o krok méně / více stupňů   (také , a .)
  + / -         -- změna kroku (0.5 … 5°)
  0             -- jeď jemně na 0° (neutrál)
  c             -- jeď jemně na uložený střed
  Enter         -- ulož aktuální polohu jako STŘED vybraného serva
  [ / ]         -- ulož aktuální polohu jako MIN / MAX (meze určuješ jen ty)
  x             -- smaž uložené MIN a MAX vybraného serva
  r             -- uvolni vybrané servo (bez pulzu, ticho)
  p             -- vypiš tabulku
  q             -- konec (uloží, serva uvolní)

Bezpečnost:
  • skript NIKDY nejede na kraj rozsahu sám: žádné hledání dorazů, žádný test
    rozsahu, žádný pohyb při startu, při výběru serva ani při ukončení
  • meze MIN a MAX určíš jen ty klávesami [ a ]; jakmile jsou uložené, dál za
    ně skript servo nepustí. Do té doby platí pevný strop ±limit (výchozí 40°)
  • servo svou polohu nehlásí, takže PRVNÍ povel je skok z neznámé polohy.
    Skript se proto před prvním pohybem zeptá, na jaký úhel smí servo poslat
    (nabídne uložený střed, jinak 0°), a bez potvrzení se nehne
  • další pohyby jsou jen po krocích (ramp po 1°)
  • STOP při bzučení nebo odporu = doraz mechaniky → vrať se o krok a uvolni (r)
  • středy se ukládají do data/servo_stredy.json; kalibrace očí
    (eye_calibration.json) se NEMĚNÍ
  • běží-li Hans a hýbe očima, o tatáž serva se s ním budeš přetahovat —
    skript na to upozorní (zastavení: systemctl --user stop hans)
"""
import json
import subprocess
import sys
import termios
import threading
import time
import tty
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "servo_stredy.json"
EYE_CALIB = ROOT / "eye_calibration.json"

RAMP_STEP = 1.0      # stupeň na dílčí krok
RAMP_DELAY = 0.03    # s mezi dílčími kroky
DEFAULT_LIMIT = 40.0


def _kanaly(args):
    """„0-6“, „2 3 4“, „P2,P3“ → seřazený seznam čísel kanálů bez opakování."""
    out = []
    for a in args:
        for cast in a.replace(",", " ").split():
            cast = cast.upper().replace("P", "")
            if "-" in cast:
                od, do = cast.split("-", 1)
                out.extend(range(int(od), int(do) + 1))
            else:
                out.append(int(cast))
    seen, res = set(), []
    for k in out:
        if 0 <= k <= 11 and k not in seen:
            seen.add(k)
            res.append(k)
    return res


def _hans_kanaly():
    """Kanály, kterými hýbe běžící Hans (oči a víčka). Prázdné = Hans neběží."""
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "hans"],
                           capture_output=True, text=True, timeout=5)
        if r.stdout.strip() != "active":
            return set()
        c = json.loads(EYE_CALIB.read_text(encoding="utf-8"))
        ch = [c.get("channels", {}).get("pan"), c.get("channels", {}).get("tilt")]
        ch += [v.get("channel") for v in (c.get("lids") or {}).values()
               if isinstance(v, dict)]
        return {int(str(x).upper().replace("P", "")) for x in ch if x}
    except Exception:
        return set()


def _klavesa():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            if sys.stdin.read(1) == "[":
                return {"C": "right", "D": "left", "A": "right", "B": "left"}.get(
                    sys.stdin.read(1), "")
            return ""
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            return "q"
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


class Serva:
    """Jádro společné pro terminál i GUI. Žádná metoda nehýbe servem sama od sebe:
    pohyb je jen `prvni_pohyb` (potvrzený skok) a `jed` (ramp po 1° uvnitř mezí)."""

    def __init__(self, limit=DEFAULT_LIMIT):
        self.limit = float(limit)
        self.zamek = threading.RLock()
        self.serva = {}      # kanál → Servo (vytvoří se až při prvním pohybu)
        self.poloha = {}     # kanál → úhel; chybí/None = od startu se nehnulo
        self.stredy, self.meze = {}, {}   # „P3“ → střed;  „P3“ → {"min": …, "max": …}
        self.vyber = []      # kanály zaškrtnuté v GUI
        try:
            for n, v in json.loads(OUT.read_text(encoding="utf-8")).items():
                if n == "_vyber":
                    self.vyber = [int(x) for x in v if 0 <= int(x) <= 11]
                elif isinstance(v, dict):
                    if v.get("stred") is not None:
                        self.stredy[n] = float(v["stred"])
                    self.meze[n] = {m: float(v[m]) for m in ("min", "max")
                                    if v.get(m) is not None}
                else:
                    self.stredy[n] = float(v)
        except Exception:
            pass

    def uloz(self):
        OUT.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for n in sorted(set(self.stredy) | set(self.meze), key=lambda x: int(x[1:])):
            m = self.meze.get(n) or {}
            data[n] = {"stred": self.stredy.get(n), "min": m.get("min"), "max": m.get("max")}
        if self.vyber:
            data["_vyber"] = self.vyber
        tmp = OUT.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(OUT)

    def rozsah(self, k):
        """Povolený rozsah: pevný strop, zúžený o meze, které uložil uživatel."""
        m = self.meze.get("P%d" % k) or {}
        return (max(-self.limit, m.get("min", -self.limit)),
                min(self.limit, m.get("max", self.limit)))

    def navrh(self, k):
        return self.stredy.get("P%d" % k, 0.0)

    def prvni_pohyb(self, k, cil):
        """Potvrzený SKOK z neznámé polohy. Vrací (ok, hláška)."""
        with self.zamek:
            if self.poloha.get(k) is not None:
                return False, "P%d už se hnulo — dál jen po krocích." % k
            lo, hi = self.rozsah(k)
            if not (lo <= cil <= hi):
                return False, ("%+.1f° je mimo povolený rozsah %+.1f° … %+.1f° — servo zůstává."
                               % (cil, lo, hi))
            if k not in self.serva:
                from robot_hat import Servo
                self.serva[k] = Servo("P%d" % k)
            self.serva[k].angle(cil)
            self.poloha[k] = cil
            return True, "P%d: výchozí poloha %+.1f°" % (k, cil)

    def jed(self, k, cil):
        """Ramp po 1° na cíl oříznutý do mezí. Bez prvního pohybu nedělá nic."""
        with self.zamek:
            a = self.poloha.get(k)
            if a is None:
                return False, "P%d se ještě nehnulo — nejdřív potvrď první pohyb." % k
            lo, hi = self.rozsah(k)
            chtel = cil
            cil = max(lo, min(hi, cil))
            d = RAMP_STEP if cil > a else -RAMP_STEP
            while a != cil:
                a += d
                if (d > 0 and a > cil) or (d < 0 and a < cil):
                    a = cil
                self.serva[k].angle(a)
                self.poloha[k] = a
                time.sleep(RAMP_DELAY)
            if chtel != cil:
                return True, "P%d: zastaveno na mezi %+.1f°" % (k, cil)
            return True, ""

    def krok(self, k, o):
        with self.zamek:
            if self.poloha.get(k) is None:
                return False, "P%d se ještě nehnulo — nejdřív potvrď první pohyb." % k
            return self.jed(k, self.poloha[k] + o)

    def uloz_stred(self, k):
        with self.zamek:
            if self.poloha.get(k) is None:
                return False, "P%d: nejdřív servem pohni." % k
            self.stredy["P%d" % k] = round(self.poloha[k], 1)
            self.uloz()
            return True, "P%d: střed %+.1f° uložen" % (k, self.poloha[k])

    def uloz_mez(self, k, co):
        with self.zamek:
            if self.poloha.get(k) is None:
                return False, "P%d: nejdřív servem pohni." % k
            m = dict(self.meze.get("P%d" % k) or {})
            m[co] = round(self.poloha[k], 1)
            if "min" in m and "max" in m and m["min"] > m["max"]:
                return False, "P%d: MIN by bylo nad MAX — neukládám." % k
            self.meze["P%d" % k] = m
            self.uloz()
            return True, "P%d: %s %+.1f° uloženo" % (k, co.upper(), self.poloha[k])

    def smaz_meze(self, k):
        with self.zamek:
            self.meze.pop("P%d" % k, None)
            self.uloz()
            return True, "P%d: MIN a MAX smazány (platí strop ±%.0f°)" % (k, self.limit)

    def uvolni(self, k):
        with self.zamek:
            if k in self.serva:
                try:
                    self.serva[k].pulse_width(0)
                except Exception as e:
                    return False, "P%d: uvolnění selhalo: %s" % (k, e)
            self.poloha[k] = None
            return True, "P%d: uvolněno" % k

    def uvolni_vse(self):
        for k in list(self.serva):
            self.uvolni(k)


# ── GUI v prohlížeči ─────────────────────────────────────────────────────────
_HTML = r"""<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Středy serv</title><style>
:root{--bg:#f4f4f1;--card:#fff;--ink:#1c1c1a;--mut:#6b6b66;--line:#d9d9d3;--acc:#1f5f8b;--warn:#a23b2a;--ok:#2e6b3e}
@media (prefers-color-scheme:dark){:root{--bg:#161614;--card:#21211e;--ink:#ecece6;--mut:#a0a098;--line:#3a3a35;--acc:#6fb2e0;--warn:#e58a78;--ok:#84c796}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--ink);font:15px/1.45 system-ui,sans-serif}
h1{font-size:19px;margin:0 0 4px}p.s{margin:0 0 12px;color:var(--mut)}
.vyber,.karta,#msg{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;margin-bottom:12px}
.vyber label{display:inline-block;margin:2px 10px 2px 0;white-space:nowrap}.vyber .h{color:var(--warn)}
.radek{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
#karty{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}.karta{margin:0}
.karta h2{font-size:16px;margin:0;display:flex;justify-content:space-between;gap:8px}
.pol{font-size:26px;font-variant-numeric:tabular-nums;margin:6px 0 2px}.neh{color:var(--mut);font-size:17px}
.udaje{color:var(--mut);font-variant-numeric:tabular-nums}
button{font:inherit;padding:8px 12px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);cursor:pointer}
button:hover{border-color:var(--acc)}button.p{background:var(--acc);border-color:var(--acc);color:#fff}
button.k{min-width:64px;font-size:18px}button.w{color:var(--warn)}button:disabled{opacity:.4;cursor:default}
input[type=number]{font:inherit;width:84px;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
select{font:inherit;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink)}
#msg{min-height:44px}#msg.e{border-color:var(--warn);color:var(--warn)}.hans{color:var(--warn);font-size:13px}
</style></head><body>
<h1>Středy serv</h1>
<p class="s">Nic se nepohne samo. První pohyb serva je skok na úhel, který potvrdíš; dál jen po krocích. MIN a MAX ukládáš ty.</p>
<div class="vyber"><b>Která serva chceš nastavovat</b><div id="boxy"></div>
<div class="radek">Krok <select id="krok"><option>0.5</option><option selected>1</option><option>2</option><option>5</option></select>°
<button class="w" onclick="akce('uvolni_vse',-1)">Uvolnit všechna</button></div></div>
<div id="msg">Zaškrtni serva nahoře.</div><div id="karty"></div>
<script>
let S=null;const $=i=>document.getElementById(i),f=v=>v==null?'—':(v>0?'+':'')+v.toFixed(1)+'°';
function msg(t,e){const m=$('msg');m.textContent=t||'';m.className=e?'e':''}
async function nacti(){S=await (await fetch('/api/stav')).json();kresli();if(S.vyber.length)msg('')}
async function akce(a,k,extra){const r=await fetch('/api/akce',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify(Object.assign({akce:a,kanal:k,krok:parseFloat($('krok').value)},extra||{}))});
 const d=await r.json();S=d.stav;kresli();if(d.hlaska||!d.ok)msg(d.hlaska,!d.ok)}
function prvni(k){const v=parseFloat($('u'+k).value);if(isNaN(v)){msg('Zadej úhel.',1);return}
 if(confirm('P'+k+' skočí z neznámé polohy na '+v+'°. Má volnou dráhu?'))akce('prvni',k,{uhel:v})}
function kresli(){
 $('boxy').innerHTML=S.vsechny.map(k=>'<label'+(S.hans.includes(k)?' class="h" title="tímhle kanálem hýbe běžící Hans"':'')+
  '><input type="checkbox" '+(S.vyber.includes(k)?'checked':'')+' onchange="vyber('+k+',this.checked)"> P'+k+(S.hans.includes(k)?' ⚠':'')+'</label>').join('');
 $('karty').innerHTML=S.serva.map(s=>{const k=s.kanal,h=s.poloha!=null,d=h?'':'disabled';
  return '<div class="karta"><h2><span>P'+k+'</span><span class="udaje">smí '+f(s.lo)+' … '+f(s.hi)+'</span></h2>'+
  (s.hans?'<div class="hans">Hans běží a tímhle servem hýbe — zastav ho, jinak polohu přepíše.</div>':'')+
  '<div class="pol">'+(h?f(s.poloha):'<span class="neh">ještě se nehnulo</span>')+'</div>'+
  '<div class="udaje">střed '+f(s.stred)+' · min '+f(s.min)+' · max '+f(s.max)+'</div>'+
  (h?'<div class="radek"><button class="k" onclick="akce(\'krok\','+k+',{smer:-1})">−</button>'+
     '<button class="k" onclick="akce(\'krok\','+k+',{smer:1})">+</button>'+
     '<button onclick="akce(\'nula\','+k+')">na 0°</button>'+
     '<button '+(s.stred==null?'disabled':'')+' onclick="akce(\'na_stred\','+k+')">na střed</button></div>'
    :'<div class="radek">První pohyb na <input type="number" step="0.5" id="u'+k+'" value="'+s.navrh+'">° '+
     '<button class="p" onclick="prvni('+k+')">Pohnout</button></div>')+
  '<div class="radek"><button class="p" '+d+' onclick="akce(\'uloz_stred\','+k+')">Uložit střed</button>'+
  '<button '+d+' onclick="akce(\'min\','+k+')">Uložit MIN</button><button '+d+' onclick="akce(\'max\','+k+')">Uložit MAX</button></div>'+
  '<div class="radek"><button onclick="akce(\'smaz_meze\','+k+')">Smazat MIN/MAX</button>'+
  '<button class="w" '+d+' onclick="akce(\'uvolni\','+k+')">Uvolnit</button></div></div>'}).join('')}
function vyber(k,on){akce('vyber',k,{zap:on})}
nacti();
</script></body></html>"""


def _gui(jadro, port):
    from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

    def stav():
        hans = _hans_kanaly()
        out = []
        for k in jadro.vyber:
            m = jadro.meze.get("P%d" % k) or {}
            lo, hi = jadro.rozsah(k)
            out.append({"kanal": k, "poloha": jadro.poloha.get(k),
                        "stred": jadro.stredy.get("P%d" % k), "min": m.get("min"),
                        "max": m.get("max"), "lo": lo, "hi": hi,
                        "navrh": jadro.navrh(k), "hans": k in hans})
        return {"vsechny": list(range(12)), "vyber": jadro.vyber,
                "hans": sorted(hans), "serva": out}

    def proved(d):
        a, k = d.get("akce"), int(d.get("kanal", -1))
        if a == "uvolni_vse":
            jadro.uvolni_vse()
            return True, "Všechna serva uvolněna."
        if not 0 <= k <= 11:
            return False, "Neplatný kanál."
        if a == "vyber":
            with jadro.zamek:
                if d.get("zap") and k not in jadro.vyber:
                    jadro.vyber = sorted(jadro.vyber + [k])
                elif not d.get("zap") and k in jadro.vyber:
                    jadro.uvolni(k)              # odškrtnuté servo se jen uvolní
                    jadro.vyber = [x for x in jadro.vyber if x != k]
                jadro.uloz()
            return True, ""
        if k not in jadro.vyber:
            return False, "P%d není zaškrtnuté." % k
        if a == "prvni":
            return jadro.prvni_pohyb(k, float(d.get("uhel")))
        if a == "krok":
            o = max(0.5, min(5.0, float(d.get("krok", 1.0))))
            return jadro.krok(k, o if int(d.get("smer", 1)) > 0 else -o)
        if a == "nula":
            return jadro.jed(k, 0.0)
        if a == "na_stred":
            if "P%d" % k not in jadro.stredy:
                return False, "P%d nemá uložený střed." % k
            return jadro.jed(k, jadro.stredy["P%d" % k])
        if a == "uloz_stred":
            return jadro.uloz_stred(k)
        if a in ("min", "max"):
            return jadro.uloz_mez(k, a)
        if a == "smaz_meze":
            return jadro.smaz_meze(k)
        if a == "uvolni":
            return jadro.uvolni(k)
        return False, "Neznámá akce."

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _posli(self, kod, typ, telo):
            self.send_response(kod)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(telo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(telo)

        def do_GET(self):
            if self.path == "/api/stav":
                self._posli(200, "application/json", json.dumps(stav()).encode())
            elif self.path in ("/", "/index.html"):
                self._posli(200, "text/html; charset=utf-8", _HTML.encode("utf-8"))
            else:
                self._posli(404, "text/plain", b"nenalezeno")

        def do_POST(self):
            if self.path != "/api/akce":
                self._posli(404, "text/plain", b"nenalezeno")
                return
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                ok, hl = proved(d)
            except Exception as e:
                ok, hl = False, "Chyba: %s" % e
            self._posli(200, "application/json",
                        json.dumps({"ok": ok, "hlaska": hl, "stav": stav()}).encode())

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    ip = ""
    try:
        ip = subprocess.run(["hostname", "-I"], capture_output=True, text=True,
                            timeout=5).stdout.split()[0]
    except Exception:
        pass
    print("Středy serv: http://%s:%d   (Ctrl-C = konec, serva se uvolní)" % (ip or "<Pi>", port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        jadro.uvolni_vse()
        jadro.uloz()
        print("\nServa uvolněna.")


def main():
    args = [a for a in sys.argv[1:]]
    if "-h" in args or "--help" in args:
        print(__doc__)
        return
    limit, port = DEFAULT_LIMIT, 7873
    for jm in ("--limit", "--port"):
        if jm in args:
            i = args.index(jm)
            if jm == "--limit":
                limit = float(args[i + 1])
            else:
                port = int(args[i + 1])
            del args[i:i + 2]
    gui = "--gui" in args
    args = [a for a in args if a != "--gui"]

    if gui:
        jadro = Serva(limit)
        if args:
            jadro.vyber = sorted(_kanaly(args))
        _gui(jadro, port)
        return

    if not args:
        odp = input("Kanály serv (např. 0-6 nebo 2 3 4 5 6 7 9) [0-6]: ").strip()
        args = [odp or "0-6"]
    kan = _kanaly(args)
    if not kan:
        print("Žádný platný kanál (P0–P11).")
        return
    if len(kan) > 9:
        print("Nejvýš 9 serv najednou (v terminálu; GUI jich zvládne 12).")
        return

    kolize = sorted(_hans_kanaly() & set(kan))
    if kolize:
        print("⚠️  Hans běží a hýbe servy na kanálech %s — bude tvé polohy přepisovat."
              % ", ".join("P%d" % k for k in kolize))
        print("    Zastav ho (systemctl --user stop hans), nebo pokračuj na vlastní riziko.")
        if input("    Pokračovat? [a/N]: ").strip().lower() not in ("a", "ano", "y"):
            return

    j = Serva(limit)
    akt = 0
    krok = 1.0

    def prvni_pohyb(k):
        """Servo se od startu nehnulo → zeptej se, kam smí skočit."""
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        odp = input("\r\nP%d se ještě nehnulo; první povel je SKOK z neznámé polohy.\n"
                    "Na jaký úhel ho smím poslat? [Enter = %+.1f°, n = nehýbat]: "
                    % (k, j.navrh(k))).strip().lower().replace(",", ".")
        if odp in ("n", "ne", "q"):
            return
        try:
            cil = float(odp) if odp else float(j.navrh(k))
        except ValueError:
            print("Nerozumím — servo zůstává.")
            return
        ok, hl = j.prvni_pohyb(k, cil)
        if not ok:
            print(hl)

    def rekni(vysl):
        if vysl[1]:
            print("\r\n" + vysl[1])

    def tabulka():
        print("\r\n  #  kanál  poloha    střed     min      max")
        f = lambda v: "   —   " if v is None else "%+6.1f°" % v
        for i, k in enumerate(kan, 1):
            m = j.meze.get("P%d" % k) or {}
            print("\r %s%d  P%-4d %s  %s  %s  %s" % (
                ">" if i - 1 == akt else " ", i, k, f(j.poloha.get(k)),
                f(j.stredy.get("P%d" % k)), f(m.get("min")), f(m.get("max"))))
        print("\r")

    def stav():
        k = kan[akt]
        p = "neznámá" if j.poloha.get(k) is None else "%+.1f°" % j.poloha[k]
        s = j.stredy.get("P%d" % k)
        lo, hi = j.rozsah(k)
        sys.stdout.write("\r\x1b[K[%d] P%d  poloha %s  střed %s  krok %.1f°  (smí %+.0f° … %+.0f°) "
                         % (akt + 1, k, p, "—" if s is None else "%+.1f°" % s, krok, lo, hi))
        sys.stdout.flush()

    print(__doc__.split("Ovládání v terminálu:")[1].split("Bezpečnost:")[0].rstrip())
    tabulka()
    try:
        while True:
            stav()
            kl = _klavesa()
            k = kan[akt]
            nehnulo = j.poloha.get(k) is None
            if kl == "q":
                break
            elif kl.isdigit() and kl != "0":
                if int(kl) <= len(kan):
                    akt = int(kl) - 1
            elif kl in ("right", ".", "left", ","):
                if nehnulo:
                    prvni_pohyb(k)   # jen výchozí poloha; krok až další klávesou
                else:
                    j.krok(k, krok if kl in ("right", ".") else -krok)
            elif kl == "0":
                prvni_pohyb(k) if nehnulo else j.jed(k, 0.0)
            elif kl == "c":
                if nehnulo:
                    prvni_pohyb(k)
                elif "P%d" % k in j.stredy:
                    j.jed(k, j.stredy["P%d" % k])
            elif kl in ("[", "]"):
                rekni(j.uloz_mez(k, "min" if kl == "[" else "max"))
            elif kl == "x":
                rekni(j.smaz_meze(k))
            elif kl in ("+", "="):
                krok = min(5.0, krok + 0.5)
            elif kl in ("-", "_"):
                krok = max(0.5, krok - 0.5)
            elif kl == "enter":
                rekni(j.uloz_stred(k))
            elif kl == "r":
                j.uvolni(k)
            elif kl == "p":
                tabulka()
    finally:
        j.uvolni_vse()
        if j.stredy or j.meze:
            j.uloz()
        print("\r\nServa uvolněna. Středy: %s" % (
            ", ".join("%s %+.1f°" % (n, v) for n, v in sorted(
                j.stredy.items(), key=lambda x: int(x[0][1:]))) or "žádné"))


if __name__ == "__main__":
    main()
