#!/usr/bin/env python3
"""BODY_TRACK_ROZPOR_SNIMEK_V1 — ruční označení snímků rozporu stopy postavy a tváře.

Spuštění:  python3 tools/oznac_rozpory.py  → http://<Pi>:7872
Snímky:    data/mereni/postava_stin/snimky/*.jpg (zelený rámeček = postava + jméno
           stopy, červený = tvář + jméno, které Hans ukazuje)
Štítky:    data/mereni/postava_stin/snimky/stitky.json  {soubor: "stopa"|"tvar"|"obe"|"nevim"}
  stopa = pravdu má zelená (postava) · tvar = pravdu má červená (tvář)
  obe = obě jména špatně · parovani = obě jména sedí, ale tvář patří jiné osobě
  (např. tvář v pozadí uvnitř rámečku postavy vpředu) · nevim = nejde poznat
Klávesy: 1–5 štítek, ←/→ posun, Backspace smaže štítek.
Hanse nijak neovlivňuje — jen čte snímky a zapisuje štítky.
"""
import json, os, sys, threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, "data", "mereni", "postava_stin", "snimky")
LOGDIR = os.path.dirname(DIR)
STITKY = os.path.join(DIR, "stitky.json")
PORT = int(os.environ.get("PORT", "7872"))
DRUHY = ("stopa", "tvar", "obe", "parovani", "nevim")
_lock = threading.Lock()


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
    try:
        return sorted(f for f in os.listdir(DIR) if f.endswith(".jpg"))
    except Exception:
        return []


def _jmena():
    """soubor → {stopa, tvar} z jsonl záznamů rozporu (pro popisek)."""
    out = {}
    try:
        for fn in sorted(os.listdir(LOGDIR)):
            if not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(LOGDIR, fn), encoding="utf-8") as f:
                for l in f:
                    try:
                        d = json.loads(l)
                    except Exception:
                        continue
                    if d.get("snimek"):
                        out[d["snimek"]] = {"stopa": d.get("jmeno"), "tvar": d.get("tvar")}
    except Exception:
        pass
    return out


PAGE = r"""<!doctype html><html lang="cs"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rozpory postavy</title><style>
:root{--bg:#111;--fg:#eee;--mut:#999;--zel:#2a8a3a;--cer:#b33;--obe:#a7a;--par:#c90;--nev:#666;--card:#1d1d1d}
body{margin:0;background:var(--bg);color:var(--fg);font:15px system-ui,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:12px 16px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.mut{color:var(--mut)}
img{width:100%;display:block;margin:10px auto;border-radius:6px;background:#000}
.meta{text-align:center;font-size:15px}
.meta .z{color:#5c5}.meta .c{color:#e55}
.row{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin:10px 0}
button{background:var(--card);color:var(--fg);border:1px solid #444;border-radius:6px;padding:10px 14px;font-size:15px;cursor:pointer}
button.on{border-color:var(--fg)}
.b-stopa.on{background:var(--zel)}.b-tvar.on{background:var(--cer)}.b-obe.on{background:var(--obe)}.b-parovani.on{background:var(--par)}.b-nevim.on{background:var(--nev)}
.strip{display:flex;flex-wrap:wrap;gap:3px;margin-top:14px}
.strip span{width:14px;height:14px;border-radius:3px;background:#333;cursor:pointer}
.strip span.stopa{background:var(--zel)}.strip span.tvar{background:var(--cer)}.strip span.obe{background:var(--obe)}.strip span.parovani{background:var(--par)}.strip span.nevim{background:var(--nev)}
.strip span.cur{outline:2px solid var(--fg)}
</style></head><body><div class="wrap">
<div class="top"><b>Rozpor postavy a tváře — kdo je to doopravdy?</b><span class="mut" id="prog"></span></div>
<img id="img" alt="snímek">
<div class="meta" id="meta"></div>
<div class="row">
 <button class="b-stopa" data-d="stopa">1 · pravdu má ZELENÁ (postava)</button>
 <button class="b-tvar" data-d="tvar">2 · pravdu má ČERVENÁ (tvář)</button>
 <button class="b-obe" data-d="obe">3 · obě špatně</button>
 <button class="b-parovani" data-d="parovani">4 · obě sedí, tvář patří jiné osobě</button>
 <button class="b-nevim" data-d="nevim">5 · nejde poznat</button>
</div>
<div class="row"><button id="prev">← zpět</button><button id="del">smazat štítek</button><button id="next">dál →</button><button id="skip">další neoznačený</button></div>
<div class="mut" id="souhrn" style="text-align:center"></div>
<div class="strip" id="strip"></div>
</div><script>
let F=[],S={},J={},i=0;
const $=id=>document.getElementById(id);
async function load(){const r=await (await fetch('api/stav')).json();F=r.soubory;S=r.stitky;J=r.jmena;
 i=Math.max(0,F.findIndex(f=>!S[f]));show()}
function show(){if(!F.length){$('meta').textContent='zatím žádné snímky';return}
 const f=F[i],j=J[f]||{};$('img').src='img/'+encodeURIComponent(f);
 $('meta').innerHTML='<span class="z">zelená (postava): '+(j.stopa||'?')+'</span> &nbsp;·&nbsp; <span class="c">červená (tvář): '+(j.tvar||'?')+'</span> &nbsp;<span class="mut">'+f+'</span>';
 const n=Object.keys(S).filter(x=>F.includes(x)).length;$('prog').textContent=(i+1)+' / '+F.length+' · označeno '+n;
 const c={stopa:0,tvar:0,obe:0,parovani:0,nevim:0};Object.entries(S).forEach(([k,v])=>{if(F.includes(k)&&c[v]!==undefined)c[v]++});
 $('souhrn').textContent='zelená '+c.stopa+' · červená '+c.tvar+' · obě špatně '+c.obe+' · špatné spárování '+c.parovani+' · nejde poznat '+c.nevim;
 document.querySelectorAll('[data-d]').forEach(b=>b.classList.toggle('on',b.dataset.d===S[f]));
 const st=$('strip');st.innerHTML='';F.forEach((x,k)=>{const e=document.createElement('span');e.className=S[x]||'';if(k===i)e.classList.add('cur');e.title=x;e.onclick=()=>{i=k;show()};st.appendChild(e)})}
async function save(f,v){const r=await fetch('api/stitek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({soubor:f,stitek:v})});if(!r.ok){alert('uložení selhalo');return}
 if(v)S[f]=v;else delete S[f];show()}
function setD(d){const f=F[i];save(f,d).then(()=>{if(i<F.length-1){i++;show()}})}
document.querySelectorAll('[data-d]').forEach(b=>b.onclick=()=>setD(b.dataset.d));
$('prev').onclick=()=>{if(i>0){i--;show()}};$('next').onclick=()=>{if(i<F.length-1){i++;show()}};
$('del').onclick=()=>save(F[i],null);
$('skip').onclick=()=>{const k=F.findIndex((f,k)=>k>i&&!S[f]);if(k>=0){i=k;show()}};
document.onkeydown=e=>{const m={'1':'stopa','2':'tvar','3':'obe','4':'parovani','5':'nevim'};if(m[e.key])setD(m[e.key]);
 else if(e.key==='ArrowLeft')$('prev').click();else if(e.key==='ArrowRight')$('next').click();else if(e.key==='Backspace')$('del').click()};
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
                                               "jmena": _jmena()}, ensure_ascii=False))
        if p.startswith("/img/"):
            fn = os.path.basename(unquote(p[5:]))
            path = os.path.join(DIR, fn)
            if fn.endswith(".jpg") and os.path.isfile(path):
                with open(path, "rb") as f:
                    return self._send(200, f.read(), "image/jpeg")
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
            if st is not None and st not in DRUHY:
                return self._send(400, '{"chyba":"stitek"}')
            with _lock:
                cur = _nacti()
                if st:
                    cur[fn] = st
                else:
                    cur.pop(fn, None)
                _uloz(cur)
            self._send(200, '{"ok":true}')
        except Exception as e:
            self._send(500, json.dumps({"chyba": str(e)}))


if __name__ == "__main__":
    print("Rozpory postavy: http://0.0.0.0:%d  (%d snímků)" % (PORT, len(_soubory())))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
