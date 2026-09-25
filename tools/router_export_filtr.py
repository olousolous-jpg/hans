"""HANS_BACKUP_ROUTER_V1 (25. 9.) — filtr exportu RouterOS: pryč tajemství, zbytek beze změny.

Čte `/export` ze stdin (Hansův omezený účet, jen čtení), píše očištěný na stdout,
statistiku na stderr. Syrový export se NIKAM neukládá — filtruje se v rouře.
Odstraní: poznámky (`comment=`) v sekcích WireGuard (nesou privátní klíče peerů),
hodnoty `private-key` / `preshared-key` / `password` / `secret` / `passphrase`
a každý řetězec, který vypadá jako WG klíč (44 zn. base64), KROMĚ `public-key`.
⚠️ Obnova z tohohle exportu VPN nerozjede — klíče se obnovují RUČNĚ z webu
Proton VPN (rozhodnutí uživatele 25. 9.).
"""
import re, sys
KEY = re.compile(r'(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{42,43}=)(?![A-Za-z0-9+/=])')
sekce = ""; odstr_kom = odstr_klic = 0; out = []
text = sys.stdin.read().replace("\\\r\n    ", "").replace("\\\n    ", "")   # slep pokračovací řádky
for l in text.splitlines():
    if l.startswith("/"):
        sekce = l.split()[0] + (" " + l.split()[1] if len(l.split()) > 1 else "")
    if "wireguard" in sekce and "comment=" in l:
        l, n = re.subn(r'\s*comment=("([^"\\]|\\.)*"|\S+)', "", l); odstr_kom += n
    def _k(m):
        global odstr_klic
        pred = l[max(0, m.start() - 12):m.start()]
        if pred.endswith("public-key=") or pred.endswith('public-key="'): return m.group(1)
        odstr_klic += 1; return "<ODSTRANENO_KLIC>"
    l = KEY.sub(_k, l)
    l = re.sub(r'((?:private-key|preshared-key|password|secret|passphrase)=)("([^"\\]|\\.)*"|\S+)', r'\1<ODSTRANENO>', l)
    out.append(l)
sys.stdout.write("# HANS_BACKUP_ROUTER_V1: tajemství odstraněna (poznámky WG peerů %d, klíče %d). "
                 "VPN klíče obnovit RUČNĚ z webu Proton VPN.\n" % (odstr_kom, odstr_klic))
sys.stdout.write("\n".join(out) + "\n")
print("radku %d, poznamek WG %d, klicu %d" % (len(out), odstr_kom, odstr_klic), file=sys.stderr)
