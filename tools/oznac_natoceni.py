#!/usr/bin/env python3
"""FACE_POSE_LOG_V1 — ruční označení vzorků natočení hlavy (data/mereni/natoceni).

Spuštění:  python3 tools/oznac_natoceni.py  → http://<Pi>:7871
Štítky:    data/mereni/natoceni/stitky.json  {soubor: {"druh": ..., "kdo": ...}}
  druh: tvar (obličej vidět, i z profilu) · oci (jen oči / část obličeje)
        · ne (odvrácená hlava, zátylek, není tvář)
  kdo:  jméno z known_persons, „cizí“ nebo „?“ (jen u tvar/oci, nepovinné)
Klávesy: 1/2/3 druh, písmena podle tlačítek osob, ←/→ posun, Backspace smaže štítek.
Hanse nijak neovlivňuje — jen čte výřezy a zapisuje štítky.
"""
import json, os, sys, re, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
DIR = os.path.join(ROOT, "data", "mereni", "natoceni")
STITKY = os.path.join(DIR, "stitky.json")
PORT = int(os.environ.get("PORT", "7871"))
_lock = threading.Lock()


def _osoby():
    try:
        from scripts import config_io
        kp = config_io.load().get("known_persons") or {}
        return sorted(kp.keys())
    except Exception:
        return []


def _nacti():
    try:
        with open(STITKY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _uloz(d):
    tmp = STITKY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, STITKY)


def _soubory():
    return sorted(f for f in os.listdir(DIR) if f.endswith(".png"))


PAGE = r"""<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Označení tváří</title><style>
:root{--bg:#111;--fg:#eee;--mut:#999;--acc:#3a7;--warn:#c93;--bad:#c44;--card:#1d1d1d}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:12px 16px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.prog{color:var(--mut)}
img{width:100%;max-width:448px;image-rendering:auto;display:block;margin:10px auto;border-radius:6px;background:#000}
.meta{text-align:center;color:var(--mut);font-size:13px;word-break:break-all}
.row{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:10px 0}
button{background:var(--card);color:var(--fg);border:1px solid #444;border-radius:6px;padding:10px 14px;font-size:15px;cursor:pointer}
button.on{border-color:var(--fg);background:#333}
.d-tvar.on{background:var(--acc)}.d-oci.on{background:var(--warn)}.d-ne.on{background:var(--bad)}
.hint{color:var(--mut);font-size:13px;text-align:center}
.strip{display:flex;flex-wrap:wrap;gap:3px;margin-top:14px}
.strip span{width:14px;height:14px;border-radius:3px;background:#333;cursor:pointer}
.strip span.tvar{background:var(--acc)}.strip span.oci{background:var(--warn)}.strip span.ne{background:var(--bad)}
.strip span.cur{outline:2px solid var(--fg)}
</style></head><body><div class="wrap">
<div class="top"><b>Natočení hlavy — označení</b><span class="prog" id="prog"></span></div>
<img id="img" alt="výřez">
<div class="meta" id="meta"></div>
<div class="row">
 <button class="d-tvar" data-d="tvar">1 · tvář (i profil)</button>
 <button class="d-oci" data-d="oci">2 · jen oči / část</button>
 <button class="d-ne" data-d="ne">3 · není tvář</button>
</div>
<div class="row" id="kdo"></div>
<div class="row"><button id="prev">← zpět</button><button id="del">smazat štítek</button><button id="next">dál →</button><button id="skip">další neoznačená</button></div>
<div class="hint">Osoba je nepovinná. Hans tipoval: jméno v názvu souboru (Unknown = nepoznal).</div>
<div class="strip" id="strip"></div>
</div><script>
let F=[],S={},O=[],i=0;
const $=id=>document.getElementById(id);
async function load(){const r=await (await fetch('api/stav')).json();F=r.soubory;S=r.stitky;O=r.osoby.concat(['cizí','?']);
 const k=$('kdo');k.innerHTML='';O.forEach(o=>{const b=document.createElement('button');b.textContent=o;b.dataset.k=o;b.onclick=()=>setK(o);k.appendChild(b)});
 i=Math.max(0,F.findIndex(f=>!S[f]));show()}
function show(){if(!F.length){$('meta').textContent='žádné vzorky';return}
 const f=F[i],s=S[f]||{};$('img').src='img/'+encodeURIComponent(f);
 $('meta').textContent=f;const n=Object.keys(S).filter(x=>F.includes(x)).length;
 $('prog').textContent=(i+1)+' / '+F.length+' · označeno '+n;
 document.querySelectorAll('[data-d]').forEach(b=>b.classList.toggle('on',b.dataset.d===s.druh));
 document.querySelectorAll('[data-k]').forEach(b=>b.classList.toggle('on',b.dataset.k===s.kdo));
 const st=$('strip');st.innerHTML='';F.forEach((x,j)=>{const e=document.createElement('span');e.className=(S[x]||{}).druh||'';if(j===i)e.classList.add('cur');e.title=x;e.onclick=()=>{i=j;show()};st.appendChild(e)})}
async function save(f,v){const r=await fetch('api/stitek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({soubor:f,stitek:v})});if(!r.ok){alert('uložení selhalo');return}
 if(v)S[f]=v;else delete S[f];show()}
function setD(d){const f=F[i],s=Object.assign({},S[f]||{});s.druh=d;if(d==='ne')delete s.kdo;save(f,s).then(()=>{if(d==='ne'&&i<F.length-1){i++;show()}})}
function setK(k){const f=F[i],s=Object.assign({},S[f]||{});if(!s.druh)s.druh='tvar';if(s.druh==='ne')return;s.kdo=k;save(f,s).then(()=>{if(i<F.length-1){i++;show()}})}
document.querySelectorAll('[data-d]').forEach(b=>b.onclick=()=>setD(b.dataset.d));
$('prev').onclick=()=>{if(i>0){i--;show()}};$('next').onclick=()=>{if(i<F.length-1){i++;show()}};
$('del').onclick=()=>save(F[i],null);
$('skip').onclick=()=>{const j=F.findIndex((f,j)=>j>i&&!S[f]);if(j>=0){i=j;show()}};
document.onkeydown=e=>{if(e.key==='1')setD('tvar');else if(e.key==='2')setD('oci');else if(e.key==='3')setD('ne');
 else if(e.key==='ArrowLeft')$('prev').click();else if(e.key==='ArrowRight')$('next').click();else if(e.key==='Backspace')$('del').click();
 else{const o=O.find(o=>o[0].toLowerCase()===e.key.toLowerCase());if(o)setK(o)}};
load();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urlparse(self.path).path
        if p in ("/", "/index.html"):
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/api/stav":
            return self._send(200, json.dumps({"soubory": _soubory(), "stitky": _nacti(),
                                               "osoby": _osoby()}, ensure_ascii=False))
        if p.startswith("/img/"):
            fn = os.path.basename(unquote(p[5:]))
            path = os.path.join(DIR, fn)
            if fn.endswith(".png") and os.path.isfile(path):
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/png")
        self._send(404, "{}")

    def do_POST(self):
        if urlparse(self.path).path != "/api/stitek":
            return self._send(404, "{}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}")
            fn = os.path.basename(d.get("soubor") or "")
            if not fn or not os.path.isfile(os.path.join(DIR, fn)):
                return self._send(400, '{"chyba":"soubor"}')
            st = d.get("stitek")
            if st is not None and st.get("druh") not in ("tvar", "oci", "ne"):
                return self._send(400, '{"chyba":"druh"}')
            with _lock:
                cur = _nacti()
                if st:
                    cur[fn] = {k: v for k, v in st.items() if k in ("druh", "kdo")}
                else:
                    cur.pop(fn, None)
                _uloz(cur)
            self._send(200, '{"ok":true}')
        except Exception as e:
            self._send(500, json.dumps({"chyba": str(e)}))


if __name__ == "__main__":
    print("Označení natočení: http://0.0.0.0:%d  (%d vzorků)" % (PORT, len(_soubory())))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
