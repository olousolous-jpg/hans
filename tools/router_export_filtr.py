"""HANS_BACKUP_ROUTER_V1 (25. 9.) — filtr exportu RouterOS: pryč tajemství, zbytek beze změny.

Čte `/export` ze stdin (Hansův omezený účet, jen čtení), píše očištěný na stdout,
statistiku na stderr. Syrový export se NIKAM neukládá — filtruje se v rouře.
Odstraní: z poznámek peerů WireGuard část za „|“ (privátní klíč; název konfigurace
z Protonu zůstane → podle něj se klíč při obnově dohledá),
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
        # poznámka peeru = „wg-CZ-36.conf|<privátní klíč>“ → nech NÁZEV konfigurace
        # z Protonu (podle něj se klíč při obnově dohledá), klíč za „|“ pryč
        def _kom(m):
            v = m.group(1).strip('"')
            return ' comment="%s|<DOPLNIT_KLIC>"' % v.split("|", 1)[0] if "|" in v else ' comment="<ODSTRANENO>"'
        l, n = re.subn(r'\s*comment=("(?:[^"\\]|\\.)*"|\S+)', _kom, l); odstr_kom += n
    def _k(m):
        global odstr_klic
        pred = l[max(0, m.start() - 12):m.start()]
        if pred.endswith("public-key=") or pred.endswith('public-key="'): return m.group(1)
        odstr_klic += 1; return "<ODSTRANENO_KLIC>"
    l = KEY.sub(_k, l)
    # jen SKUTEČNÉ hodnoty (text v uvozovkách nebo holé slovo), NE výraz ve skriptu
    # (`private-key=[:pick $tc …]` v hans-vpn-dalsi — 1. verze ho rozbila)
    l = re.sub(r'((?<![\w-])(?:private-key|preshared-key|password|secret|passphrase)=)'
               r'("(?:[^"\\]|\\.)*"|(?![\[$\\])[^\s\]]+)', r'\1<ODSTRANENO>', l)
    out.append(l)
sys.stdout.write("# HANS_BACKUP_ROUTER_V1: tajemství odstraněna (poznámky WG peerů %d, klíče %d).\n" % (odstr_kom, odstr_klic)
+ """# OBNOVA VPN (privátní klíče v záloze NEJSOU):
#  1) Obnov konfiguraci: nahraj tenhle soubor do routeru (Winbox > Files) a spusť /import file=router.rsc
#  2) Na webu Proton VPN > Downloads > WireGuard vytvoř konfigurace pro TYTÉŽ servery, co jsou v poznámkách
#     peerů (wg-CZ-36.conf, …). ⚠️ Proton ukáže klíč jen při vytvoření — staré klíče znovu stáhnout nejdou,
#     vzniknou nové. Ověř, že Endpoint v .conf sedí s endpoint-address peeru.
#  3) Z každého .conf vezmi hodnotu `PrivateKey = …` a vlož ji do poznámky TOHO peeru za „|“:
#       /interface wireguard peers set [find comment~"^wg-CZ-36.conf"] comment="wg-CZ-36.conf|<PrivateKey>"
#     (Winbox: Interfaces > WireGuard > Peers > dvojklik > Comment)
#  4) Aktivnímu peeru (disabled=no) dej klíč i na rozhraní:
#       /interface wireguard set wireguard-inet private-key="<PrivateKey aktivního>"
#     Pak přepínání serverů (hans-vpn-dalsi, VPN-Interactive-Menu) bere klíče z poznámek samo.
""")
sys.stdout.write("\n".join(out) + "\n")
print("radku %d, poznamek WG %d, klicu %d" % (len(out), odstr_kom, odstr_klic), file=sys.stderr)
