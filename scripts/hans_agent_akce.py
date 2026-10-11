"""Seznam agentních akcí: popis pro router, nápovědná slova, argumenty,
obsluha, potvrzení a odstup.

Odděleno z `scripts/hans_agent.py` (PREPIS_ROZHODOVANI, fáze 2). Tady je jen
POPIS; router, pravidla a potvrzování zůstaly tam.
⚠️ Popis akce jde doslova do zadání routeru — jeho změna je zásah do
rozhodování a chce přeměřit. Jména z původního modulu se píší `_ag.jmeno`;
soubor sám nic neimportuje.
"""


def postav_akce(_ag):
    return {        "kodi_play_film": _ag.Action(
            "kodi_play_film",
            "Pustit konkrétní film na TV. Argument 'titul' = název filmu, který "
            "uživatel chce vidět (musí být v knihovně).",
            hints=["film", "pust", "pusť", "koukni", "podivat", "podívat",
                   "smrtonosn", "bond", "sledovat", "na tv", "dej to", "spust"],
            args=["titul"], run=_ag._run_kodi_play, grounding=_ag._ground_kodi_play,
            needs_confirm=True, cooldown_s=300,
            reject_text=_ag._reject_kodi_play),
        "hans_sleep": _ag.Action(
            "hans_sleep",
            "Uspat sebe (Hanse) — ztišit se, přestat mluvit. Když uživatel řekne "
            "ať jde spát / ať je ticho / dobrou noc s přáním klidu.",
            hints=["spát", "spat", "spi", "ztich", "ticho", "klid", "unaven",
                   "dobrou noc", "jdi spat", "bež spát", "odpočin"],
            args=[], run=_ag._run_sleep, grounding=None,
            needs_confirm=True, cooldown_s=120),
        "add_book_wishlist": _ag.Action(
            "add_book_wishlist",
            "Přidat knihu na seznam ke čtení. Argument 'titul' = název knihy, "
            "kterou uživatel zmíní že by si chtěl přečíst / ať přečteš.",
            hints=["kniha", "knihu", "přečíst", "precist", "číst", "cist",
                   "na seznam ke", "wishlist", "knížk", "knizk"],
            args=["titul"], run=_ag._run_book_wishlist, grounding=_ag._ground_book,
            needs_confirm=True, cooldown_s=30),

        # ── Info dotazy (instant, bez potvrzení) ────────────────────────────────
        "report_weather": _ag.Action(
            "report_weather",
            # HANS_WEATHER_HINT_FIX_V1 (3.9.) — popis je ZÁMĚRNĚ zpět u „aktuálního
            # počasí“, ačkoli akce umí i zítřek. Rozšíření o slovo ZÍTŘEK bylo
            # REGRESE: router pak posílal na počasí i „máš něco v kalendáři na
            # zítra?" a „kdy se zitra hraje MS v hokeji?" (2. 9. obojí správně None).
            # ⛔ Negativní příklad („NE kalendář, NE sport") se sem NEPŘIDÁVÁ —
            # u `HANS_CMD_LLM_ROUTE_V3` je změřeno, že nepomohly, a byl by to
            # [[prompt-debt-tool-calling]]. Oprava patří do STRUKTURY: o zítřek se
            # stará `_ZITRA_PAT` uvnitř `_run_weather`, tedy až za rozhodnutím.
            # HANS_WEATHER_HINT_FIX_V2 (4.9.) — V1 NESTAČIL. Odebral hint „zitra"
            # i větu o ZÍTŘKU z popisu, ale slovo „zitra" nechal v řádku
            # o argumentu `kdy` — a popis je pro router JEDINÝ podklad, čím se
            # akce liší od sousedů. Přeměřeno 4.9. (3 běhy na větu):
            #     „kdy se zitra hraje MS v hokeji?"  → report_weather 3/3  ✗
            #     „zitra mam volno"                  → report_weather 3/3  ✗
            # Po odebrání slova i argumentu: obojí None 3/3, a všechny čtyři
            # legitimní dotazy („jaké bude zítra počasí?", „bude zítra pršet?",
            # „jaká je předpověď na zítra?", „jak je venku?") dál 3/3 správně.
            # ⚠️ Argument `kdy` je NADBYTEČNÝ: `_run_weather` si zítřek přečte
            # z věty přes `_ZITRA_PAT`, tedy AŽ ZA rozhodnutím routeru. Tím je
            # zítřejší předpověď zachovaná, aniž to slovo vidí router.
            # ⚠️ Ověření V1 bylo JEDNORÁZOVÉ a trefilo se do šťastného běhu —
            # proto se tohle měřilo 3× na větu. [[within-session-numbers-lie]]
            "Odpovědět na dotaz o AKTUÁLNÍM počasí / jak je venku.",
            # ⚠️ HANS_WEATHER_HINT_FIX_V1 (3.9.) — „zítra"/„zitra" tu bylo pár hodin
            # a bylo to REGRESE, kterou odhalilo až přeměření: samotné „zítra" nemá
            # s počasím nic společného, takže pre-gate pustil dál věty, které pak
            # router poslal na počasí.
            #     „kdy se zitra hraje MS v hokeji?"   None → report_weather  ✗
            #     „máš něco v kalendáři na zítra?"    None → report_weather  ✗
            # Dotaz na předpověď projde i bez toho: „jaké bude zítra počasí?" přes
            # „počasí", „jaká je předpověď na zítra?" přes „predpoved". Doplněn jen
            # chybějící tvar slovesa pršet.
            hints=["počasí", "pocasi", "venku", "prší", "prsi", "pršet", "prset",
                   "sněží", "snezi", "teplo venku", "zima venku", "za oknem",
                   "slunečno", "předpověď", "predpoved"],
            # args=[] — viz HANS_WEATHER_HINT_FIX_V2 výše: `kdy` router jen sváděl.
            args=[], run=_ag._run_weather, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_climate": _ag.Action(
            "report_climate",
            "Odpovědět na dotaz o teplotě nebo vlhkosti V MÍSTNOSTI / v pokoji / "
            "uvnitř (z vlastních čidel). "
            # HANS_CLIMATE_AWARE_V1 (1.9.) — patří sem i dotaz na POCIT tepla či chladu.
            # Změřeno na routeru: „je tu nejak dusno, ne?" a „kolik je tady stupnu?"
            # akci vybraly, ale „neni ti zima?" a „neni tu na tebe moc horko?"
            # vrátily None — LLM je četl jako konverzační zdvořilost, ne jako dotaz
            # na měřitelný údaj, a Hans si pak teplotu DOMYSLEL („teplota je vyšší
            # než obvykle") místo aby se zeptal čidla.
            "Patří sem i dotaz na POCIT tepla nebo chladu („není ti zima?“, „není "
            "tu na tebe moc horko?“, „nemrzneš?“) — na ten se odpovídá z ČIDEL, "
            "ne dojmem. "
            "⚠️ NE počasí venku — to je report_weather. "
            "⚠️ NE teplota počítače nebo procesoru — to je report_pc_health. "
            "⚠️ NE obecný dotaz na náladu nebo rozpoložení („je ti smutno?“, „jak "
            "se cítíš?“) — to není teplota.",
            hints=["kolik je v pokoji", "teplota v pokoji", "jak je tu teplo",
                   "je tu zima", "je tu horko", "vlhkost", "kolik je tu stupnu",
                   "kolik je tu stupňů", "teplota v mistnosti", "teplota v místnosti",
                   "jak je uvnitr", "jak je uvnitř", "dusno",
                   # HANS_CLIMATE_AWARE_V1 — bránu otevírá i otazník (`_looks_like_request`),
                   # tyhle jsou pro věty BEZ něj („neni ti zima.“ jako konstatování).
                   "neni ti zima", "není ti zima", "neni ti horko", "není ti horko",
                   "kolik je tady stupnu", "kolik je tady stupňů"],
            args=[], run=_ag._run_climate, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_pc_health": _ag.Action(
            "report_pc_health",
            "Odpovědět na dotaz o stavu POČÍTAČE (teploty, VRAM, RAM, zda běží).",
            hints=["počítač", "pocitac", "jak je na tom pc", "teplota pc",
                   "vram", "kolik ram", "gpu", "grafick", "jak se má počítač"],
            args=[], run=_ag._run_pc_health, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_household": _ag.Action(
            "report_household",
            "Odpovědět na dotaz, KDO V DOMĚ BYDLÍ / ŽIJE — složení domácnosti "
            "(kdo do ní patří a jakou má roli). ⚠️ NE kdo je právě doma nebo "
            "v místnosti TEĎ — to je report_who_is_home. Rozdíl: „kdo tu bydlí“ "
            "je trvalý stav, „kdo je doma“ je aktuální pozorování.",
            hints=["kdo tu bydli", "kdo tu žije", "kdo tu zije", "kdo v dome",
                   "kdo v domě", "domacnost", "domácnost", "kdo sem patri"],
            args=[], run=_ag._run_household, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_person": _ag.Action(
            "report_person",
            "Odpovědět na dotaz KDO JE konkrétní člověk / co o něm Hans ví / jaký "
            "je — identita a charakteristika osoby (člen domácnosti i osobnost "
            "z Hansova čtení). Patří sem i „co víš o <jméno>?“, „řekni mi o "
            "<jméno>“, „znáš <jméno>?“, „co je zač <jméno>?“ — tedy i tvary, kde "
            "jméno stojí v jiném pádě („o Janě“, „o Kláře“). "
            "⚠️ NE dotaz na PŘÍTOMNOST nebo polohu („je X doma?“, "
            "„kde je X?“, „kdo je doma?“) — ten patří report_who_is_home.",
            hints=["kdo je", "kdo to je", "co vis o", "co víš o", "co je zac",
                   "co je zač", "znas", "znáš", "kdo byl", "rekni mi o"],
            args=["jmeno"], run=_ag._run_person_info, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_who_is_home": _ag.Action(
            "report_who_is_home",
            "Odpovědět na dotaz o PŘÍTOMNOSTI osob TEĎ (koho Hans právě VIDÍ) — "
            "kdo je doma / v místnosti / "
            "kdo tu je, ale i „kde je <jméno>?“, „je <jméno> doma?“, „co dělá "
            "<jméno>?“ (odpověď = koho Hans právě VIDÍ; NEDOMÝŠLET činnost, jen "
            "přítomnost). Vyber TUTO akci u dotazů na aktuální polohu/přítomnost "
            "konkrétní osoby.",
            hints=["kdo je doma", "kdo je tu", "je někdo doma", "je nekdo doma",
                   "kdo tu je", "někdo tu", "nekdo tu", "jsem sám", "jsem sam",
                   "kdo tady", "kde je", "co dělá", "co dela"],
            args=[], run=_ag._run_who_home, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_now_playing": _ag.Action(
            "report_now_playing",
            "Odpovědět na dotaz, co PRÁVĚ hraje na TV.",
            hints=["co hraje", "co běží", "co bezi", "co dávají", "co davaji",
                   "co se přehrává", "co je na tv"],
            args=[], run=_ag._run_now_playing, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_home_status": _ag.Action(
            "report_home_status",
            "Odpovědět na ŠIROKÝ/VÁGNÍ dotaz o dění doma jako celku — „děje se "
            "něco doma?“, „co se doma děje?“, „co je doma nového?“, „je doma "
            "všechno v pořádku?“, „jak to vypadá doma?“. Shrne živý stav (co hraje "
            "na TV + kdo je doma). Vyber TUTO akci místo report_now_playing/"
            "report_who_is_home, když dotaz NENÍ konkrétně jen o TV ani jen o "
            "přítomnosti, ale o dění doma obecně.",
            hints=["děje se něco", "deje se neco", "co se doma", "co je doma",
                   "doma nového", "doma noveho", "vypadá to doma", "vypada to doma",
                   "všechno v pořádku doma", "vsechno v poradku doma",
                   "jak to doma", "co je doma nového", "něco nového doma"],
            args=[], run=_ag._run_home_status, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "report_kolac_status": _ag.Action(
            "report_kolac_status",
            "Odpovědět na dotaz o KOLÁČOVI (Hansův animatronický společník/"
            "topinkovač, se kterým Hans rozmlouvá) — „co dělá Koláč?“, „co Koláč?“, "
            "„jak se má Koláč?“, „co říká Koláč?“, „nezlobí Koláč?“. Odpověď = co "
            "Koláč teď dělá (z posledního dialogu). Koláč NENÍ osoba v domě ani "
            "„dění doma“ — u dotazu o Koláčovi NIKDY nevybírej report_who_is_home "
            "ani report_home_status.",
            hints=["kolac", "kolač", "koláč", "co dela kolac", "nezlobi kolac"],
            args=[], run=_ag._run_kolac_status, grounding=None,
            needs_confirm=False, cooldown_s=10),

        # ── Ovládání médií (confirm) ────────────────────────────────────────────
        "kodi_pause": _ag.Action(
            "kodi_pause",
            "Pozastavit (pauza) běžící film/přehrávání na TV.",
            hints=["pauza", "pauzni", "pozastav", "zastav to", "dej pauzu",
                   "stopni", "stop na chvíli"],
            args=[], run=_ag._run_kodi_pause, grounding=_ag._ground_playing,
            needs_confirm=True, cooldown_s=15),
        "kodi_stop": _ag.Action(
            "kodi_stop",
            "Úplně zastavit (ukončit) běžící film/přehrávání na TV.",
            hints=["vypni film", "vypni to", "ukonči film", "ukonci film",
                   "zastav film", "vypni tv", "konec filmu"],
            args=[], run=_ag._run_kodi_stop, grounding=_ag._ground_playing,
            needs_confirm=True, cooldown_s=15),

        # ── Studijní téma z chatu (confirm) ─────────────────────────────────────
        "add_study_topic": _ag.Action(
            "add_study_topic",
            "Zařadit téma ke studiu do hloubky. Argument 'tema' = co si uživatel "
            "přeje aby Hans nastudoval / prostudoval / naučil se.",
            # HANS_CHAT_STUDY_BRIDGE_V1 — „zjisti víc o" ZÁMĚRNĚ NENÍ hint
            # (patří k null/recall); studijní request = „zjisti si / podívej se".
            hints=["nastuduj", "prostuduj", "studuj", "nauč se", "nauc se",
                   "zaměř se na", "zamer se na", "prozkoumej", "zjisti si",
                   "zjistit si", "podívej se na", "podivej se na", "mrkni na"],
            args=["tema"], run=_ag._run_add_study, grounding=_ag._ground_study,
            needs_confirm=True, cooldown_s=30),

        # ── Poznámky / paměťové sliby (confirm, light) ──────────────────────────
        "add_note": _ag.Action(
            "add_note",
            "Přidat poznámku / úkol / položku na seznam. Argument 'text' = co si "
            "uživatel přeje poznamenat (nákup, připomínka, TODO).",
            hints=["poznamenej", "zapiš si", "zapis si", "na nákup", "na nakup",
                   "nezapomeň", "nezapomen", "připomeň", "pripomen", "na seznam",
                   "poznámka", "poznamka", "dej na seznam"],
            args=["text"], run=_ag._run_add_note, grounding=_ag._ground_note,
            needs_confirm=True, cooldown_s=10),

        # ── Ovládání PC + hlídání (mutace, confirm) ─────────────────────────────
        "pc_shutdown": _ag.Action(
            "pc_shutdown",
            "Vypnout stolní POČÍTAČ (PC) na povel — když uživatel jasně řekne ať "
            "vypneš/zhasneš počítač. NENÍ to uspání tebe (Hanse) — to je hans_sleep.",
            hints=["vypni pc", "vypni počítač", "vypni pocitac", "zhasni pc",
                   "vypnout počítač", "vypnout pocitac", "vypni ten pocitac"],
            args=[], run=_ag._run_pc_shutdown, grounding=None,
            needs_confirm=True, cooldown_s=60),
        "translate_doc": _ag.Action(
            "translate_doc",
            "Připravit ČESKOU ZVUKOVOU STOPU k cizojazyčnému dokumentu, který teď "
            "běží v Kodi. Volič u „přelož to“, „přelož ten dokument“, „udělej mi "
            "českou stopu“. Trvá to minuty, výsledek přijde zprávou. ⛔ Na DOTAZ tuto "
            "akci NEVOL — ani na průběh („jak jde ten překlad?“), ani na to, co už "
            "přeloženo BYLO („co jsi přeložil?“, „které dokumenty máš česky?“). "
            "Volič je jen ROZKAZ přelož.",
            hints=["přelož", "prelozit", "přeložit", "česká stopa", "ceska stopa",
                   "český dabing", "cesky dabing", "přelož dokument"],
            args=[], run=_ag._run_translate, grounding=None,
            needs_confirm=True, cooldown_s=30),
        "guard_toggle": _ag.Action(
            "guard_toggle",
            "Zapnout nebo VYPNOUT hlídací režim místnosti (při pohybu/změně světla "
            "pošle snímek na Matrix — pro hlídání prázdného domu). Argument "
            "'mode' = 'on' (zapni hlídání) nebo 'stop' (vypni hlídání). Volič u "
            "„hlídej dům/místnost“, „zapni hlídání“, „vypni hlídání“. Na pouhý "
            "DOTAZ na stav („hlídáš ještě?“) tuto akci NEvol.",
            hints=["hlídej", "hlidej", "hlídání", "hlidani", "stráž", "straz",
                   "hlídat dům", "hlidat dum", "hlídej místnost", "zapni hlidani"],
            args=["mode"], run=_ag._run_guard, grounding=None,
            needs_confirm=True, cooldown_s=15),

        # ── HANS_AGENT_ACTIONS_V3 (13.8.) — dosud chybějící protipóly ───────────
        # Brána rozkazy propouštěla (HANS_AGENT_NO_WORDGATE_V1), ale agent na ně
        # neměl akci → spadly do chatu a persona na ně jen odpověděla slovy.
        "pc_wake": _ag.Action(
            "pc_wake",
            "Probudit / zapnout stolní POČÍTAČ (PC) přes síť (Wake-on-LAN) — když "
            "uživatel řekne ať zapneš/probudíš/nahodíš počítač. OPAK akce "
            "pc_shutdown; nezaměňuj s ní („vypni pc\u201c = pc_shutdown).",
            hints=["zapni pc", "zapni počítač", "zapni pocitac", "probuď pc",
                   "probud pc", "nahoď pc", "nahod pc", "nastartuj pc",
                   "zapni ten pocitac", "probuď počítač", "wol"],
            args=[], run=_ag._run_pc_wake, grounding=None,
            needs_confirm=True, cooldown_s=60),
        "hans_wake": _ag.Action(
            "hans_wake",
            "Probudit SEBE (Hanse) ze spánku — když uživatel řekne ať se probudíš / "
            "vstáváš / už nespíš. OPAK akce hans_sleep. NENÍ to zapnutí počítače "
            "(to je pc_wake).",
            hints=["probuď se", "probud se", "vzbuď se", "vzbud se", "vstávej",
                   "vstavej", "už nespi", "uz nespi", "jsi vzhůru", "prober se"],
            args=[], run=_ag._run_hans_wake, grounding=None,
            needs_confirm=False, cooldown_s=30),
        "kodi_resume": _ag.Action(
            "kodi_resume",
            "Pokračovat v POZASTAVENÉM filmu/přehrávání na TV (odpauzovat). "
            "Bez argumentu — když uživatel chce pustit DÁL to, co běželo. "
            "Pro spuštění konkrétního filmu podle názvu je kodi_play_film.",
            hints=["pusť to dál", "pust to dal", "pokračuj", "pokracuj",
                   "odpauzuj", "zruš pauzu", "zrus pauzu", "hraj dál", "hraj dal",
                   "spusť to zas", "pusť to zpátky"],
            args=[], run=_ag._run_kodi_resume, grounding=None,
            needs_confirm=False, cooldown_s=10),
        "game_mode_toggle": _ag.Action(
            "game_mode_toggle",
            "Zapnout nebo vypnout HERNÍ MÓD (Hans uvolní grafiku pro hru a přestane "
            "používat svůj mozek; vypnutím si ho vezme zpět). Argument 'mode' = "
            "'on' (jdu hrát / uvolni grafiku) nebo 'off' (dohrál jsem / vrať mozek). "
            "Na pouhý DOTAZ na stav („máš herní mód?\u201c) tuto akci NEvol.",
            hints=["herní mód", "herni mod", "jdu hrát", "jdu hrat", "budu hrát",
                   "budu hrat", "uvolni grafiku", "dohrál jsem", "dohral jsem",
                   "dohrál", "vrať mozek", "vrat mozek"],
            args=["mode"], run=_ag._run_game_mode, grounding=None,
            needs_confirm=True, cooldown_s=15),
    }
