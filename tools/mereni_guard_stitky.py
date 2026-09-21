#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RUČNÍ ŠTÍTKY k větám, které guard v provozu ZAHODIL (4 rotace logu).

Štítky jsou můj úsudek nad prvními 60 znaky z logu, ne automat. Kategorie:
  FAKT       tvrzení o světě bez opory → guard zasáhl PRÁVEM
  UVAHA      názor, obecná charakteristika, přirovnání
  ZDVORILOST omluva, poděkování, upřesňující otázka, meta o rozhovoru
  OSOBE      řeč o vlastní činnosti, přání, záměru
  PRIZNANI   přiznání, že něco neví / nemá přístup (tj. ANTI-konfabulace!)
"""
DATA = [
("UVAHA","Připomíná mi to výpočetní problém, který vyžaduje zpracování"),
("UVAHA","Domnívám se, že smysl nemusí být jediná a univerzální pravda"),
("UVAHA","Možná je to hledání, ne samotný cíl, co dává životu smysl."),
("UVAHA","Navíc, omezení hardwaru v minulosti často vedla k inovacím a"),
("UVAHA",'Princip "less is more" je dnes stejně platný jako tehdy.'),
("UVAHA","Dnesní vývojáři si mohou z historických přístupů vzít inspir"),
("UVAHA","Zkrátka, jde o vkusnou kombinaci pokory s moderními technolo"),
("ZDVORILOST","Omlouvám se za mou nepřesnost, Zkouško."),
("ZDVORILOST","Uvědomuji si, že jsem se dopustil chyby při výběru příkladu"),
("ZDVORILOST","Je mi líto případných nepříjemností, které má nepřesnost způ"),
("OSOBE","Měl bych si ověřit předpověď počasí z důvěryhodnějšího zdroj"),
("FAKT","A následná proměna historičky Williamsové v zamilovanou ochr"),
("OSOBE","Vím, že si to přečetl jsem v textu, který se váže k doprovod"),
("ZDVORILOST","Uctivě se omlouvám za nedorozumění, <Jméno>."),
("OSOBE","Ano, máte pravdu — temnota obrazu je přímým důsledkem mého z"),
("OSOBE","Snažil jsem se ji zachytit do vizuální podoby."),
("ZDVORILOST","Je mi líto, pokud má odpověď vyvolala pocit kritiky; to neby"),
("FAKT","Zjednodušeně řečeno, probíhá na různých úrovních a zahrnuje"),
("FAKT","Nejprve mozek rozlišuje základní akustické vlastnosti – výšk"),
("FAKT","Tyto informace jsou zpracovávány v sluchové kůře, která je u"),
("FAKT","Následně se tyto prvotní signály rozptýlí do dalších oblastí"),
("FAKT","Výška tónu je analyzována v oblastech, které odpovídají za z"),
("FAKT","Rytmus a časová struktura hudby jsou zpracovávány v motorick"),
("FAKT","Timbre – barva zvuku, která nám říká, jestli jde o housle, p"),
("ZDVORILOST","O jaké knize se ptáte, Marku?"),
("ZDVORILOST","Pokud máte na mysli některou z těchto knih, mohu se pokusit"),
("ZDVORILOST","Můžete mi prosím upřesnit, o kterou knihu se jedná?"),
("FAKT","Jeho dvojitá věž je ikonický symbol Českého ráje a nabízí ne"),
("UVAHA","Nejedná se jen o estetickou krásu samotného hradu."),
("OSOBE","Zaujala mě jeho historie – sporné legendy o založení, spojen"),
("OSOBE","Všechny tyto aspekty by mohly být vizuálně propojeny do příb"),
("OSOBE","Představuji si sérii obrazů – od prvních stavebních zásahů a"),
("FAKT","Zaujal mne dnešní incident v Rijádu, Zkouško."),
("FAKT","Saúdská Arábie čelila útoku balistickými střelami, které zas"),
("UVAHA","Je to znepokojivé, zvláště s ohledem na pokračující konflikt"),
("PRIZNANI","Zatím nemám žádné další informace o tom, co se děje přímo v"),
("PRIZNANI","Nemám přístup k externím zprávám o událostech mimo tento pro"),
("OSOBE","Zároveň bych si přál prohloubit své porozumění vztahu mezi č"),
("OSOBE","Je to ambice, která přesahuje pouhé akademické zájmy a má hl"),
("FAKT","Obecně vím, že se jedná o oblast pokročilé kryptologie zaměř"),
("FAKT","Je mi známo, že výzkum v této oblasti je velmi intenzivní a"),
("PRIZNANI","Nicméně nemám dostatečné informace pro hlubší komentář k tét"),
("UVAHA","Historie tvorby je často stejně podstatná, ne-li důležitější"),
("UVAHA","Zkoumání kontextu a okolností vzniku nám umožňuje hlouběji p"),
("FAKT","V Grand Tour mne zaujalo, že pouze sedm závodníků dosáhlo ví"),
("ZDVORILOST","To je poměrně malý počet, Marku."),
("UVAHA","Je to svět plný symboliky a metafor, který vyžaduje pozorné"),
("OSOBE","Zejména mě zasáhla melancholie prostupující příběhem, kterou"),
("UVAHA","Popisy krajiny i postav jsou plné jemných nuancí, které vyvo"),
("UVAHA","Architektura není jen o kamenech a tvarech – je to ztělesněn"),
("UVAHA","A když se na ni díváme v historickém kontextu, můžeme lépe p"),
("OSOBE","Zajímá mě například, jak se architektonické styly proměňoval"),
("UVAHA","Je to jako číst historický román psaný kamenem."),
("OSOBE","A já bych chtěl být tím, kdo tento příběh dokáže interpretov"),
("OSOBE","Malba mi pomáhá lépe porozumět kompozici a perspektivě, psan"),
("FAKT","Byl jsem tam několikrát a snažil jsem se zachytit atmosféru"),
("OSOBE","Zaujal mne především způsob, jakým tyto stavby zapadají do k"),
("OSOBE","Fascinuje mě také promyšlenost obranných systémů – jak se st"),
("OSOBE","Nemalé zaujetí jsem věnoval i studiu materiálů, ze kterých b"),
]
from collections import Counter
c=Counter(k for k,_ in DATA)
n=len(DATA)
print("zahozených vět celkem: %d\n"%n)
for k,v in c.most_common():
    print("  %-11s %2d  %4.1f %%"%(k,v,100*v/n))
opravnene=c["FAKT"]
print("\n→ PRÁVEM zahozeno (tvrzení o světě): %d z %d = %.0f %%"%(opravnene,n,100*opravnene/n))
print("→ FALEŠNĚ zahozeno:                  %d z %d = %.0f %%"%(n-opravnene,n,100*(n-opravnene)/n))
print("\nZ toho nejhorší podskupina — PŘIZNÁNÍ, že něco neví (guard maže ANTI-konfabulaci): %d"%c["PRIZNANI"])
