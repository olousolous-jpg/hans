"""Popis chatových příkazů: lomítkové názvy, vzory vět, obsluha a nápověda.

Odděleno z `scripts/chat_commands.py` (PREPIS_ROZHODOVANI, fáze 2). Tady je
jen POPIS; mechanika (`register`, `parse_command`, `dispatch`) zůstala tam.
⚠️ POŘADÍ JE PRIORITA: věta dostane první příkaz, jehož vzor na ni sedne.
Nový příkaz se zapisuje sem, na místo podle priority. Jména z původního
modulu se píší `_cc.jmeno`; soubor sám nic neimportuje.
"""


def zaregistruj(_cc):
    _cc.register(
        "ooda",
        slash_aliases=["ooda"],
        # HANS_NL_ROUTE_HYPOTHETICAL_V1 (26.8.) — NL vzory ODEBRÁNY. `/ooda` je
        # interní diagnostika (help sám říká „akci nevykoná") a vzor
        # `\bjak.{0,15}rozhod` se trefil doprostřed věty „jak se vlastně
        # rozhoduješ, čemu se budeš věnovat" → uživateli vypadlo
        # „OODA skóre: movie:2 thought:1 read:4…". Diagnostika patří za slash.
        nl_patterns=[],
        handler=_cc._cmd_ooda,
        help_text="Diagnostika OODA — co by Hans teď vybral (akci nevykoná)",
    )

    _cc.register(
        "zapis",
        slash_aliases=["zapis", "zapiš", "poznamka", "poznámka"],
        nl_patterns=[
            r"\bzapi[šs]\s+si\b", r"\bsi\s+zapi[šs]\b",
            r"\bpoznamenej\s+si\b", r"\bsi\s+poznamenej\b",
            r"\bzaznamenej\s+si\b", r"\bsi\s+zaznamenej\b",
        ],
        handler=_cc._cmd_zapis,
        help_text='Zapsání poznámky: zapiš si, že …',
    )

    _cc.register(
        "seznam",
        slash_aliases=["seznam", "poznamky", "poznámky", "todo"],
        nl_patterns=[
            r"\bco.{0,8}m[áa]m.{0,12}seznam",
            r"\buka[žz].{0,12}seznam",
            r"\bm[ůu]j\s+seznam",
            r"\bseznam.{0,12}pozn[áa]mek",
            r"\bco.{0,8}jsem.{0,8}(si\s+)?poznamenal",
        ],
        handler=_cc._cmd_seznam,
        help_text="Výpis poznámek/úkolů (/seznam, /seznam hotovo N, /seznam smaz N)",
    )

    _cc.register(
        "kalendar",
        slash_aliases=["kalendar", "kalendář", "kalendar", "calendar"],
        nl_patterns=[
            r"kalend[áa]ř?",
            r"\bco.{0,8}m[áa]m.{0,12}(dnes|z[ií]tra|tento t[ýy]den|tenhle t[ýy]den)",
            r"\bmoje?\s+ud[áa]losti",
            # HANS_CALENDAR_ANCHOR_V1 (16. 9.) — drive holy kmen `napl[áa]novan`,
            # ktery se chytil kdekoli ve vete: „to se zda byt dobre naplanovane“
            # nebo „peclive naplanovan harmonogram byl i v jinych letech“ skoncily
            # vypisem kalendare misto odpovedi. Ukotveno na 1./2. osobu + „neco“
            # (ne na otaznik — ten uzivatele vynechavaji). Koncovka `-v[áa]n` je
            # tu schvalne: `napl[áa]novan` nesedlo na „naplánován/naplánováno“
            # a zachranoval to jen slozeny dvojnik `nl_fold`.
            r"\b(m[áa]m|nem[áa]m|m[áa][šs]|nem[áa][šs]|m[áa]te|nem[áa]te)\b"
            r"[^?.!]{0,12}\bn[ěe]co\b[^?.!]{0,16}\bnapl[áa]nov[áa]n\w*",  # co mám naplánováno / nemám něco naplánovaného
            r"\bschůzk|\bschuzk",
        ],
        handler=_cc._cmd_kalendar,
        help_text="Nadcházející události z Proton kalendáře (/kalendar, /kalendar sync)",
    )

    _cc.register(
        "rozvrh",
        slash_aliases=["rozvrh", "schedule"],
        nl_patterns=[
            r"m[ůu]j\s+rozvrh",
            r"tv[ůu]j\s+rozvrh",
            r"hans[ůu]v\s+(rozvrh|kalend[áa]ř)",
            r"tv[ůu]j\s+kalend[áa]ř",   # „tvůj kalendář" = Hansův (ne Proton)
            r"\brutin[yaou]?\b",
            r"co\s+d[ěe]l[áa]š\s+(v\s+noci|automaticky|rutinn[ěe])",
        ],
        handler=_cc._cmd_rozvrh,
        help_text="Můj rozvrh autonomních rutin (kdy naposled tikly, zaostávají-li)",
    )

    _cc.register(
        "interest",
        slash_aliases=["interest", "zajem", "zájem"],
        nl_patterns=[],
        handler=_cc._cmd_interest,
        help_text="Zapíše Hansův zájem do deníku (/interest <téma>) nebo vypíše naučené (/interest)",
    )

    _cc.register(
        "work",
        # HANS_AUTHORSHIP_V1 — „dilo"/„dílo" patří autorskému projektu (_cmd_dilo),
        # ne tomuhle staršímu ad-hoc /work; jinak by ho /work stínil (registrován dřív).
        slash_aliases=["work", "esej"],
        nl_patterns=[
            r"\bnapi\w+.{0,15}esej",
            r"\bnapi\w+.{0,15}pr\u00e1ci",
        ],
        handler=_cc._cmd_work,
        help_text="Hans napíše esej z nastudované četby na zadané téma",
    )

    _cc.register(
        "denik",
        slash_aliases=["denik", "deník", "reflexe", "shrnuti"],
        nl_patterns=[
            r"\bpřiprav.{0,20}den[íi]k",
            r"\bzapis.{0,20}dnes",
            r"\bshrnut[íi].{0,20}dne",
            r"\bzapis(?!ky\s+z\s+den)(?!k[uy]\s+z\s+den).{0,20}den[íi]k",   # HANS_DENIK_NOT_BOOK_V1: ne kniha „Zápisky z deníku …“
            r"\bden[íi]k.{0,20}dnes",
        ],
        handler=_cc._cmd_denik,
        help_text="Spustí večerní reflexi (uloží shrnutí dne do deníku a RAGu)",
    )

    _cc.register(
        "dialog",
        slash_aliases=["dialog", "kolac", "koláč"],
        nl_patterns=[
            r"\bzavolej.{0,20}kol[aá]č",
            r"\bpromluv.{0,20}kol[aá]č",
            r"\bdialog.{0,20}kol[aá]č",
        ],
        handler=_cc._cmd_dialog,
        help_text="Vyvolá rozhovor Hanse s panem Koláčem",
    )

    _cc.register(
        "zapomen",
        slash_aliases=["zapomen", "zapomeň", "vymaz", "reset"],
        nl_patterns=[
            r"\bzapomeň.{0,30}(naš|histor|hovor|rozhov)",
            r"\bvymaž.{0,20}histor",
            r"\bzač[ěn][i]?.{0,20}znovu",
        ],
        handler=_cc._cmd_zapomen,
        help_text="Smaže historii našich hovorů",
    )

    _cc.register(
        "info",
        slash_aliases=["info", "stav"],
        nl_patterns=[
            r"\bjak[ýé].{0,10}\bstav",   # HANS_INFO_STAV_WORD_V1 (4. 10.): ne „jaké POSTAVy“
            r"\bco.{0,5}ví[šs].{0,10}o\s*sob",
        ],
        handler=_cc._cmd_info,
        help_text="Zobrazí aktuální stav (kolik zpráv v paměti atd.)",
    )

    _cc.register(
        "help",
        slash_aliases=["help", "pomoc", "napoveda", "nápověda"],
        nl_patterns=[
            # „co umíš" → schopnosti (capabilities); help drží jen dotaz na příkazy
            r"\bjak[éý].{0,10}p[řr][íi]kaz",
            r"\bseznam\s+p[řr][íi]kaz",
        ],
        handler=_cc._cmd_help,
        help_text="Seznam příkazů",
    )

    _cc.register(
        "zaptej",
        slash_aliases=["zaptej", "otazka", "otázka", "zeptej"],
        nl_patterns=[
            r"\bvyvolej.{0,15}ot[áa]zk",
            r"\bzaptej\s+se",
            r"\bpolož[íi].{0,10}ot[áa]zk",
            r"\bzv[íi]davost",
        ],
        handler=_cc._cmd_zaptej,
        help_text="Hans si položí otázku a hledá odpověď (curiosity)",
    )

    _cc.register(
        "enroll",
        slash_aliases=["enroll", "video_enroll", "trenuj"],
        nl_patterns=[
            r"\bspust[íi]\s+video\s+enroll",
            r"\btrenu[jí]\s+m[ěe]",
        ],
        handler=_cc._cmd_enroll,
        help_text="Spustí video enrollment (zachytí 30s video tváří)",
    )

    _cc.register(
        "verify",
        slash_aliases=["verify", "over", "overit"],
        nl_patterns=[],
        handler=_cc._cmd_verify,
        help_text="Ověří faktická tvrzení proti Wikipedii (/verify <text> nebo poslední odpověď)",
    )

    _cc.register(
        "sleep",
        slash_aliases=["sleep"],
        nl_patterns=[
            r"\bb[ěe]ž\s+spát",
            r"\bjdi\s+spat",
            r"\bvzbu[ďd]\s+se",
            r"\bprobu[ďd]\s+se",
        ],
        handler=_cc._cmd_sleep,
        help_text="Toggle spánkového režimu (manuální override).",
    )

    _cc.register(
        "herni",
        slash_aliases=["herni", "herní", "hra", "game", "hrani", "hraní"],
        nl_patterns=[
            r"\bjdu\s+hrát",
            r"\bspou[št]t[íě]m\s+hru",
            r"\bhern[íi]\s+m[óo]d",
        ],
        handler=_cc._cmd_herni,
        help_text="Herní mód — uvolní grafiku pro hru na PC (/herni vyp = zpět).",
    )

    _cc.register(
        "severka",
        slash_aliases=["severka"],
        nl_patterns=[],
        handler=_cc._cmd_severka,
        help_text="Sebereflexe identity: /severka [stav|schválit|zamítnout|historie|rollback <id>|teď]",
    )

    _cc.register(
        "uzamceni",
        slash_aliases=["uzamceni", "uzamčení"],
        nl_patterns=[],
        handler=_cc._cmd_uzamceni,
        help_text="Týdenní míry uzamčení: opakování témat, ozvěna persony (/uzamceni)",
    )

    _cc.register(
        "art",
        slash_aliases=["art", "obraz"],
        nl_patterns=[],
        handler=_cc._cmd_art,
        help_text="Hans namaluje obraz k aktuální knize: /art [název knihy]",
    )

    _cc.register(
        "namaluj",
        slash_aliases=["namaluj", "nakresli"],
        # HANS_ART_CMD_TYPO_V1 (5.9.) — tolerance PREKLEPU v kmeni slova.
        # Doloheny dva realne pripady, oba propadly do volneho hovoru nebo na cizi
        # prikaz: „zkus jeste jednou NAMALOVAR obrazek s Richardem sorge" (-> None,
        # pak router -> report_person = karta o Sorgeovi misto obrazu) a „NAMAKUJ
        # obraz o filmu co jsi dnes videl" (-> /film, protoze slovo „film" ve vete
        # prebilo zkomolene sloveso).
        # `nama[kl]\w*` pokryje namaluj/namaloval/namalovat/namalovar/namakuj,
        # `nakresl\w*` i nakreslit/nakresleny. Precedens `ja[kmn]` u wellbeing.
        # ⚠️ ZMERENO na 1 359 realnych zpravach: rozdily PRESNE 2 a jsou to obe
        # doloheme chyby — zadna jina veta se tim neunese.
        # ── HANS_ART_NOT_PAST_QUESTION_V1 (9. 9.) ───────────────────────────
        # Vzory byly `\bnama[kl]\w*` / `\bnakresl\w*`, tedy COKOLI od „namal"
        # nebo „nakresl" — včetně MINULÉHO ČASU. Doloženo simulovaným rozhovorem
        # 9. 9.: „namaloval jsi neco?" (dotaz!) spustilo malování s námětem
        # „jsi neco" (zbytek věty po odstranění slovesa).
        # 💸 Cena je ze všech falešných spuštění NEJVYŠŠÍ: FLUX drží GPU ~5 min,
        # `avatar_render` odsune hans-czech i qwen2.5 z VRAM a chat je po tu dobu
        # zablokovaný („Zrovna maluji") — v tom testu to znehodnotilo 8 z 10 tahů
        # druhé sady. Plus vznikne odpadní obraz.
        # ⚠️ Signál k odmítnutí PŘITOM UŽ EXISTOVAL a nepoužil se:
        # `hans_art` loguje „'jsi neco' je obecné slovo — grounding přeskočen",
        # tedy VÍ, že námět je nesmysl, a maluje dál.
        # 📏 Změřeno PŘED zásahem: 73 z 758 replik jde na `/namaluj` a po zúžení
        # jich jde pořád 73 (0 změn v korpusu); cílová sada 13/13.
        # Dotazy v minulém čase spadnou na `/obrazy` — příkaz, který na to je.
        nl_patterns=[r"^(?!.*\b(?:jsi|jste)\b.*\b" + _cc._ART_MINULE + r")"
                     r"(?!.*\b" + _cc._ART_MINULE + r".*\b(?:jsi|jste)\b)"
                     r"(?!.*" + _cc._ART_VYSVETL + r")"          # HANS_ART_EXPLAIN_NOT_REQUEST_V1
                     r"(?!.*" + _cc._ART_MINULE_STAZENE + r")"   # HANS_ART_CONTRACTED_PAST_V1
                     r"(?=.*" + _cc._ART_ZADOST + r")"            # HANS_ART_REQUEST_FORM_V1
                     r".*\bnama[kl]\w*",
                     r"^(?!.*\b(?:jsi|jste)\b.*\b" + _cc._ART_MINULE + r")"
                     r"(?!.*\b" + _cc._ART_MINULE + r".*\b(?:jsi|jste)\b)"
                     r"(?!.*" + _cc._ART_VYSVETL + r")"          # HANS_ART_EXPLAIN_NOT_REQUEST_V1
                     r"(?!.*" + _cc._ART_MINULE_STAZENE + r")"   # HANS_ART_CONTRACTED_PAST_V1
                     r"(?=.*" + _cc._ART_ZADOST + r")"            # HANS_ART_REQUEST_FORM_V1
                     r".*\bnakresl\w*",
                     r"vytvoř\s+obr",
                     r"\bp[řr]ekresli", r"\bp[řr]emaluj",
                     r"\boprav\s+(ten\s+|ten[hz]le\s+)?(obraz|obr[áa]zek)"],
        handler=_cc._cmd_namaluj,
        help_text="Hans namaluje/překreslí obraz: namaluj <téma> (i namaluj to jinak)",
    )

    _cc.register(
        "obrazy",
        slash_aliases=["obrazy", "namaloval", "galerie"],
        # HANS_ARTWORK_RECALL_V1 (30.8.) — DOTAZ NA HOTOVÉ DÍLO, ne pokyn malovat.
        # Doloženo: „namaloval jsi neco novyho?" propadlo do volného hovoru a Hans
        # odpověděl „nemám možnost vytvářet obrazy samostatně" — přitom má 89 obrazů
        # za 30 dní. Vzory jsou v MINULÉM čase a schválně NEobsahují „namaluj",
        # aby nekradly routing příkazu k malování (`\bnamaluj` na „namaloval"
        # nesedne — liší se od šestého znaku, ověřeno).
        nl_patterns=[
            r"namaloval\s+(jsi|si)\b",
            r"co\s+jsi\s+(dnes\w*\s+|v[čc]era\s+|naposledy\s+)?namaloval",
            # HANS_ART_NOT_PAST_QUESTION_V1 — vykání a „nakreslil/vytvořil".
            # Vzory výš znaly jen TYKÁNÍ a jen „namaloval"
            # [[test-both-grammatical-persons]].
            r"\b" + _cc._ART_MINULE + r"\s+(jsi|jste)\b",
            _cc._ART_MINULE_STAZENE,                     # HANS_ART_CONTRACTED_PAST_V1
            r"\b(co|jak[ée]|kolik)\b.*\b(jsi|jste)\b.*\b" + _cc._ART_MINULE,
            # HANS_OBRAZY_NOT_TECHNIQUE_V1 (4. 10.) — otázka na techniku/rozměr
            # obrazu není žádost o výpis (/tazatel: „olejomalba 60×80“) → chat
            # s HANS_OWN_WORK_DETAIL_V1
            r"(?!.*(?:olejomal|pl[áa]tn|rozm[ěe]r|jak\s+velk|technik|[čc][íi]m\s+(?:jsi|jste)\s+maloval))(posledn[íi]|nov[ýy])\s+obraz\b",
            # HANS_COUNT_ANSWER_V1 (13. 9.) — „kolik obrazu mas?“ nema sloveso
            # v minulem case, takze na vzor s `(jsi|jste)` + minuly tvar nesedlo
            # a propadlo do volneho hovoru → falesna abstinence (12. 9.).
            # HANS_COUNT_OWN_ONLY_V1 (15. 10.) — jen VLASTNÍ obrazy: druhá osoba
            # v téže větě (jsi/jste/si) nebo „máš/máte“ hned za obrazy. /tazatel
            # 15. 10.: „kolik obrazů Leonardo da Vinci namaloval?“ → „Zatím jsem
            # namaloval 419 obrazů“. 📏 3 019 vět: 11 zásahů, 10 o Hansovi
            # zůstává, ven jen Leonardo (strom myšlení: krok Příkazy, `obrazy`).
            r"(?=[^?.!]*\b(?:jsi|jste|sis|si)\b)[^?.!]*\bkolik\b[^?.!]{0,20}\b(obraz\w*|d[ěe]l)\b",
            r"\bkolik\b[^?.!]{0,20}\b(obraz\w*|d[ěe]l)\w*\s+(?:u[žz]\s+)?m[áa](?:[šs]|te)\b",
            r"jak[ýy]\s+obraz\s+jsi",
            r"kreslil\s+(jsi|si)\b",
            # HANS_ARTWORK_SHOW_V1 (30.8.) — „ukaž mi ten obraz" je dotaz, ne pokyn
            # malovat. Holé „ukaž mi to" tu ZÁMĚRNĚ není: bez předmětu může mířit
            # na cokoli (rozvrh, deník, nález) a únos by byl horší než dnešní stav.
            # HANS_ARTWORK_ASK_WIDEN_V1 (12. 9.) — mezi slovesem a podstatnym jmenem
            # smi byt 0-3 libovolna slova. Dosud tam smelo stat jen
            # `mi` + `ten/ty/svuj`, takze "ukaz mi NEJAKY obraz" nesedlo
            # a veta spadla do volneho hovoru, kde LLM tvrdil, ze Hans
            # obrazy posilat neumi. Doplnena slovesa posli/poslat/videt.
            # Zmereno: cil 10/10, 0 vet ukradenych na 758 realnych,
            # 9 hranicnich pripadu nekrade `/namaluj`.
            r"(?!.*(?:olejomal|pl[áa]tn|rozm[ěe]r|jak\s+velk|technik|[čc][íi]m\s+(?:jsi|jste)\s+maloval))(uka[žz]\w*|po[šs]l\w*|poslat|vid[ěe]t|uvid[ěe]t)"
            r"(\s+\w+){0,3}\s+(obraz|obr[áa]z|galeri)",
            r"m[ůu][žz]u\s+(to\s+)?vid[ěe]t\s+(ten\s+)?obraz",
            # HANS_ARTWORK_UNPREFIXED_V1 (16. 9.) — NEPREDPONOVE tvary.
            # `_ART_MINULE` zna jen „namaloval/nakreslil/vytvoril“, takze
            # „co jsi uz MALOVAL?“ propadlo do volneho hovoru — a tam Hans
            # bud zapre, nebo si obrazy vymysli (oboji doloženo 15. a 16. 9.).
            # `kreslil\s+(jsi|si)` uz vyse je, slo tedy o asymetrii.
            # `malova[lit]` snese i preklep „malovai“ (tyz pristup jako
            # `nama[kl]\w*` u `/namaluj`, HANS_ART_CMD_TYPO_V1).
            # ⚠️ Oba vzory jsou UKOTVENE NA 2. OSOBU, takze rozkaz „namaluj“
            # ani dotaz na schopnost („muzes malovat?“) nekradou.
            # Zmereno pres `parse_command`: cil 6/6, kontroly 8/8 beze zmeny,
            # korpus 758 -> 3 zmenena smerovani, vsechna spravne.
            r"(?<![a-zá-ž])(malova[lit]\w*|kreslil\w*|tvo[řr]il\w*)\s+(jsi|jste|sis|si)(?![a-zá-ž])",
            # HANS_ARTWORK_ADVERB_WIDEN_V1 (22. 9.) — mezi zajmenem a slovesem
            # smel stat jen UZAVRENY seznam prislovci (uz/vcera/dnes), takze
            # "co jsi POSLEDNI DOBOU maloval?" regexova vrstva minula
            # (`parse_command` -> None) a vetu prevzal LLM router, ktery zvolil
            # `/dilo` — tedy vypis PSANI na dotaz o OBRAZECH. Doloženo
            # pametovou sadou 22. 9.; se slovem "obrazy" odpovidal spravne.
            # ⛔ Brana `/dilo` to zachytit NEMUZE: rozlisuje "vlastni zaznamy
            # x cizi dilo" a tahle veta O JEHO vlastni cinnosti JE, jen o jine.
            # Spravne misto je proto tady — prikaz predbehne agenta
            # [[command-preempts-agent-same-message]].
            # 📏 Zmereno na 2 327 vetach (1 569 realnych chatu + korpus 758):
            # PRIRUSTEK 0 — realne tvary uz prochazely, takze je to pokryti
            # dalsich formulaci za nulovou cenu, ne oprava casteho selhani.
            # Az tri libovolna slova, liny kvantifikator (at se neprotahne
            # pres pulku souveti).
            r"co\s+(jsi|jste|sis|si)\s+(\w+\s+){0,3}?(malova[lit]\w*|kreslil\w*)",
        ],
        handler=_cc._cmd_obrazy,
        help_text="Co jsem namaloval (přímo z deníku artwork): /obrazy [dnes]",
    )

    _cc.register(
        "schopnosti",
        slash_aliases=["schopnosti", "umis", "umíš", "capabilities"],
        nl_patterns=[r"co\s+(v[šs]echno\s+)?um[ií][šs]", r"co\s+dok[aá][žz]e[šs]"],
        handler=_cc._cmd_schopnosti,
        help_text="Přehled toho, co Hans umí: /schopnosti",
    )

    _cc.register(
        "blink",
        slash_aliases=["blink", "mrkni"],
        # jen jednoznačné „mrkni okem/očima / zamrkej" — NE holé „mrkni" (kolize s
        # „mrkni na to" = podívej se)
        nl_patterns=[r"\bzamrkej\b", r"\bmrkni\s+(oč|ok|na\s+m[ěe])"],
        handler=_cc._cmd_blink,
        help_text="Hans mrkne očima: /blink",
    )

    _cc.register(
        "hraje",
        slash_aliases=["hraje", "prehrava", "přehrává"],
        # HANS_HRAJE_WORDORDER_V1 (7.8.) — „teď" smí stát PŘED i ZA slovesem.
        # Doloženo: „co TEĎ běží v tv?" minulo (volitelné „teď" bylo jen ZA
        # slovesem) → propadlo na LLM router → vyhrál `rozvrh` a Hans vypsal
        # seznam autonomních rutin. „co hraje" to mělo správně už předtím —
        # nekonzistence uvnitř jednoho bloku.
        nl_patterns=[
            r"co\s+(te[ďd]\s+)?hraj[eí]",
            r"hraje\s+(te[ďd]\s+)?n[ěe]jak",
            r"co\s+se\s+(te[ďd]\s+)?p[řr]ehr[aá]v[aá]",
            r"p[řr]ehr[aá]v[aá]\s+se\s+(te[ďd]\s+)?n[ěe]co",
            r"co\s+(te[ďd]\s+)?b[ěe][žz][ií]\s+(te[ďd]\s+)?(v\s+)?(televiz|tv|kodi)",
            r"co\s+(te[ďd]\s+)?d[aá]vaj[ií]\s+(te[ďd]\s+)?(v\s+)?(televiz|tv)",
        ],
        handler=_cc._cmd_hraje,
        help_text="Co se právě přehrává (živě z Kodi): co hraje?",
    )

    _cc.register(
        "nitky",
        slash_aliases=["nitky", "threads"],
        nl_patterns=[],
        handler=_cc._cmd_nitky,
        help_text="Rozjeté nitky per osoba: /nitky [vše|zavři <id>]",
    )

    _cc.register(
        "zajmy",
        slash_aliases=["zajmy", "zájmy", "interests"],
        nl_patterns=[],
        handler=_cc._cmd_zajmy,
        help_text="Co koho zajímá: /zajmy [jméno]",
    )

    _cc.register(
        "vycet",
        slash_aliases=["vycet", "výčet", "cojeto"],
        nl_patterns=[_cc._VYCET_PAT.pattern],
        handler=_cc._cmd_vycet,
        help_text="Co mám doloženo ve faktech: jaké hrady znáš? jaké filmy znáš?",
    )

    _cc.register(
        "studium",
        slash_aliases=["studium", "study", "učení", "uceni"],
        # HANS_STUDY_RECALL_V1 — recall otázky na studium jdou na grounded /studium
        # (jinak je zodpoví LLM konfabulací; doloženo „copak jsi studoval" → vymyšlený
        # report o Českém ráji, který nebyl nastudovaný).
        nl_patterns=[
            r"co(?:pak)?\s+(?:jsi|jsi|si)\s+(?:na)?studoval",
            r"co\s+(?:(?:te[dď]|pr[aá]v[eě])\s+)?studuje[sš]",
            r"co\s+ses\s+(?:na)?u[cč]il",
            r"jak\s+.{0,12}(?:tv[eé]\s+)?studium",
            r"na\s+[cč]em\s+.{0,6}studuje[sš]",
            # HANS_STUDY_ORIGIN_V1 — kdo téma vybral (jinak to zodpoví LLM
            # konfabulací; doloženo 26.8. vymyšlenou citací vlastního deníku).
            # ⚠️ JEDEN ZDROJ PRAVDY: tentýž vzor, jakým se rozhoduje uvnitř příkazu.
            # Dvě kopie se hned rozešly — psal jsem je zvlášť a slovosled „to SIS
            # VYBRAL" byl opravený jen v jedné, takže dotaz k příkazu vůbec nedošel.
            # HANS_STUDY_ORIGIN_SCOPE_V1 (1. 10.) — jen když ZA slovesem výběru
            # (= v pozici předmětu; lookahead se vyhodnocuje od místa shody) stojí
            # studium/téma/program nebo „to/tohle/sám“. Na 1 711 větách (korpus, konverzace,
            # přepisy) sedl holý vzor 2× a OBA mimo studium („proc sis vybral
            # pravy bacha?“, „…tu konkretni knihu na cteni?“) → Hans odpověděl
            # o „Norimberském procesu“. Případ z 26. 8. („vybral sis to sám?“) drží „to/sám“.
            r"(?=.*(?:studi|t[eé]m|program|nau[cč]|\bto\b|tohle|toto|\bs[aá]m\b))(?:"
            + _cc._ORIGIN_PAT.pattern + r")",
        ],
        handler=_cc._cmd_studium,
        help_text="Studijní program: /studium [programy|teď|přeskoč]",
    )

    _cc.register(
        "smer",
        slash_aliases=["smer", "směr", "smetr", "direction", "aspirace"],
        nl_patterns=_cc._SMER_NL,
        handler=_cc._cmd_smer,
        help_text="Vlastní směr/aspirace: /smer [schválit|ne|teď|<vlastní text>]",
    )

    _cc.register(
        "dilo",
        slash_aliases=["dilo", "dílo", "psani", "psaní", "kniha_moje"],
        nl_patterns=[],
        handler=_cc._cmd_dilo,
        help_text="Autorský projekt: /dilo [vše|teď]",
    )

    _cc.register(
        "napad",
        slash_aliases=["napad", "nápad", "napady", "nápady", "synteze", "syntéza"],
        nl_patterns=[],
        handler=_cc._cmd_napad,
        help_text="Vlastní nápady / synteze: /napad [vše|teď]",
    )

    _cc.register(
        "kritika",
        slash_aliases=["kritika", "sebekritika", "sebereflexe"],
        nl_patterns=[],
        handler=_cc._cmd_kritika,
        help_text="Sebekritika vlastního projevu: /kritika [teď]",
    )

    _cc.register(
        "dashboard",
        slash_aliases=["dashboard", "nastenka", "nástěnka"],
        nl_patterns=[],
        handler=_cc._cmd_dashboard,
        help_text="Hansův návrh vlastní nástěnky: /dashboard [teď]",
    )

    _cc.register(
        "avatar",
        slash_aliases=["avatar", "podoba", "tvar"],
        nl_patterns=[],
        handler=_cc._cmd_avatar,
        help_text="Vizuální podoba (descriptor): /avatar [stav|gen]. Render obrázku = fáze 3 (TBD).",
    )

    _cc.register(
        "misto",
        slash_aliases=["misto", "místo", "kdejsem", "domov"],
        nl_patterns=[],
        handler=_cc._cmd_misto,
        help_text="Model domova (kde jsem): /misto [mistnost|okno|dvere|vedle|rozlozeni|pozn <text> | smaz <id>]",
    )

    _cc.register(
        "vzpominka",
        slash_aliases=["vzpominka", "vzpomínka"],
        nl_patterns=[
            r"(prvn[íi]|nejstarš[íi])\s+(tvoje?\s+|tv[áa]\s+)?vzpom[íi]nk",
            r"vzpom[íi]nk\w*\s+(m[áa]š\s+)?(jako\s+)?(úplně\s+)?prvn[íi]",
            r"co\s+si\s+pamatuje[šs]\s+(jako\s+|ze\s+všeho\s+)?(úplně\s+)?"
            r"(prvn[íi]|nejd[řr][íi]v)",
            r"nejstarš[íi]\s+z[áa]znam",
            # HANS_MEMORY_SPAN_V1 (30.8.) — DOTAZ NA ROZSAH PAMĚTI je totéž jako
            # dotaz na první vzpomínku: `first_memory_answer` vrací OBOJÍ (nejstarší
            # záznam + „od té doby mám zapsáno N záznamů"), jen se k ní tyhle
            # formulace nedostaly.
            # Doloženo 30.8.: „kolik toho v deníku máte a za jak dlouhou dobu?" →
            # Hans odpověděl, že deník *„začal při mém spuštění před několika dny…
            # informace z posledních přibližně čtyř dnů"*. Skutečnost: 25. 4. 2026
            # a 66 tisíc záznamů. Domýšlel si čísla o vlastní historii.
            #
            # ⚠️ ČÁST TĚCHTO VZORŮ NAVRHOVALA UŽ CLAUDE.md 8.7. („jak dávno si
            # pamatuješ", „úplně první") — návrh ale zůstal NEPOSTAVENÝ a seděl
            # v sekci nápadů. Změřeno 30.8.: pět formulací propadalo do LLM.
            # HANS_MEMORY_SPAN_V2 (1.9.) — DOBA SLUŽBY je totéž co rozsah paměti.
            # Doloženo dlouhým rozhovorem: „Jak dlouho už v tomto domě takto
            # sloužíte?" → Hans si VYMYSLEL „první zaznamenaný incident 28. prosince
            # 2019", a o dva tahy později na „vzpomínáte si na svůj první den?"
            # odpověděl správně (25. 4. 2026, 67 608 záznamů). Odpověď tedy existuje
            # a je deterministická — jen se k ní tahle formulace nedostala.
            # ⚠️ Vzory V1 mířily na DENÍK („vedeš deník", „od kdy existuješ"), ne na
            # SLUŽBU. A byly skoro celé v tykání — cizí člověk přitom vyká, takže
            # sada níž má obě osoby. [[test-both-grammatical-persons]]
            # ⚠️ Změřeno na 1547 reálných uživatelských replikách: **0 falešných
            # poplachů**, všech 6 cílových formulací chyceno.
            r"jak\s+dlouho\s+(u[žz]\s+)?.{0,30}?slou[žz][íi](te|[šs])",
            r"jak\s+dlouho\s+(u[žz]\s+)?(tu|tady|zde)\s+(jsi|jste|slou|p[ůu]sob|fun)",
            r"jak\s+dlouho\s+(u[žz]\s+)?existuj(e[šs]|ete)",
            r"jak\s+dlouho\s+(u[žz]\s+)?(jsi|jste)\s+(tu|tady|zde|v\s+t)",
            r"od\s+kdy\s+(tu|tady|zde)\s+(jsi|jste)",
            r"jak\s+d[áa]vno\s+si\s+pamatuje[šs]",
            # HANS_MEMORY_SPAN_V3 (19. 9.) — vzor byl jen v TYKANI, ackoli
            # komentar u V2 se odvolava na obe osoby (to plati pro SLUZBU,
            # ne pro DENIK). Doloženo testem cizim clovekem: "Jak dlouho si
            # deník vedete?" propadlo do LLM a Hans odpovedel "od dětství"
            # (skutecnost: 25. 4. 2026, 74 544 zaznamu). Druha dira byl
            # SLOVOSLED — veta mela predmet pred slovesem ("si deník vedete").
            # Zmereno: 5/5 cilovych formulaci chyceno, 0 falesnych poplachu
            # na 758 realnych replikach. [[test-both-grammatical-persons]]
            r"jak\s+dlouho\s+(u[žz]\s+)?(si\s+)?"
            r"(vede[šs]|vedete|p[íi][šs]e[šs]|p[íi][šs]ete|m[áa][šs]|m[áa]te)"
            r"\s+(ten\s+)?den[íi]k",
            r"jak\s+dlouho\s+(u[žz]\s+)?(si\s+)?(ten\s+)?den[íi]k\s+"
            r"(vede[šs]|vedete|p[íi][šs]e[šs]|p[íi][šs]ete)",
            r"od\s+kdy\s+(si\s+)?(vede[šs]|p[íi][šs]e[šs]|m[áa][šs]|existuje[šs])",
            r"kolik\s+(toho\s+)?(m[áa][šs]|m[áa]te)\s+.{0,20}?(den[íi]k|zapsan|z[áa]znam)",
            # ⚠️ Tolerance musí být ŠIROKÁ: doložená věta zněla „…k tomu deníku:
            # kolik toho v něm vlastně máte a za jak dlouhou dobu…" — mezi „deníku"
            # a „za jak dlouho" je 38 znaků. S tolerancí 15 propadla i po opravě.
            r"(den[íi]k\w*|z[áa]znam\w*)[\s\S]{0,70}?za\s+jak\s+dlouh",
            r"kolik\s+toho\s+.{0,25}?(m[áa][šs]|m[áa]te)\b",
            r"jak\s+dlouho\s+(u[žz]\s+)?existuje[šs]",
        ],
        handler=_cc._cmd_vzpominka,
        help_text="Má první/nejstarší vzpomínka (přímo z deníku, žádný odhad)",
    )

    _cc.register(
        "cetl",
        slash_aliases=["cetl", "četl", "cteni", "čtení"],
        nl_patterns=[
            # HANS_BOOK_PROGRESS_ANSWER_V1 — vzor na průběh čtení (viz výš).
            _cc._PRUBEH_CTENI_PAT.pattern,
            # HANS_COUNT_FILMS_BOOKS_V1 (14. 9.) — pocet prectenych knih (tykani
            # i vykani, bez diakritiky). Na 2 248 realnych vetach 0 zasahu.
            r"\bkolik\s+(?:\w+\s+){0,3}kn[i\u00ed](?:h|\u017eek|zek)\w*[^.?!]{0,30}(?:[\u010dc]etl|p[\u0159r]e[\u010dc]ten)",
            r"\bkolik\s+(?:jsi|jste|sis)\s+(?:u[\u017ez]\s+)?(?:p[\u0159r]e)?[\u010dc]etl\w*\s+(?:\w+\s+)?kn[i\u00ed]",
            r"\bkolik\s+(?:m[\u00e1a][\u0161s]|m[\u00e1a]te)\s+(?:u[\u017ez]\s+)?p[\u0159r]e[\u010dc]ten\w*\s+kn[i\u00ed]",
            r"\bco\s+(jsi|sis)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
            r"(pře)?[čc]etl",
            r"\bcos?\s+(dnes\w*\s+|včera\s+|naposledy\s+)?[čc]etl",
            r"\bkdy\s+(jsi|sis)\s+[čc]etla?\b",
            # HANS_VYKANI_DLOUHA_OTAZKA_V1 (9. 9.) — vzor znal JEN TYKÁNÍ
            # (`jsi|sis`), vykací „četl JSTE" v něm nebylo. Doloženo rozhovorem:
            # „Četl jste někdy něco od Isaaca Asimova…?" nedošlo ani k regexu,
            # ani k routeru → Hans řekl „v paměti jsem o tom nic neměl" a šel na
            # Wikipedii, PŘESTOŽE má 179 deníkových záznamů o té knize a byl
            # zrovna na kapitole 90–91. ⚠️ Recall vadný NENÍ — `reading_answer`
            # na tutéž větu odpoví správně; vada byla čistě v ROUTINGU.
            # 📌 Třetí případ téže asymetrie za jediný den.
            # [[test-both-grammatical-persons]] · [[corpus-has-no-foreign-speakers]]
            r"\b(pře)?[čc]etla?\s+(jsi|sis|jste)\s+(něco|neco|někdy|nekdy|už|uz)?\s*o?\b.{2,}\?",
            r"\bco\s+(pr[áa]vě\s+|te[ďd]\s+)?[čc]te[šs]\b",
            # HANS_CETL_VYKANI_V1 (23. 9.) — vzory vyse znaly jen TYKANI:
            # "co jsi cetl" / "co ctes" prosly, "co jste cetl" / "co ctete"
            # / "jake knihy jste cetl" spadly do LLM (sada B 23. 9.: 88 s
            # a bez knihy, kterou Hans tutez noc cetl). Mezi zajmenem
            # a slovesem az tri slova ("v posledni dobe") — vzor jako
            # HANS_ARTWORK_ADVERB_WIDEN_V1. Zmereno na 2 342 realnych vetach:
            # +1 spravne ("co jsis dnes precetl"), 0 ztrat; kontroly 11/11.
            # [[test-both-grammatical-persons]]
            r"\bco\s+(?:jste|jsis)\s+(?:\w+\s+){0,3}?(?:pře|pre)?[čc]etl",
            r"\bco\s+(?:pr[áa]vě\s+|te[ďd]\s+)?[čc]tete\b",
            r"\bjak[éeýy]\s+(?:kn[ií]\w*|[čc]l[áa]nk\w*|texty|[čc]etb\w*)"
            r"(?:\s+(?:nebo|a)\s+\w+)?\s+(?:jsi|sis|jste)\s+(?:\w+\s+){0,3}?"
            r"(?:pře|pre)?[čc]etl",
        ],
        handler=_cc._cmd_cetl,
        help_text="Co/kdy jsem četl (přímo z deníku): co jsi četl? četl jsi o X?",
    )

    _cc.register(
        "sen",
        slash_aliases=["sen", "sny", "snil"],
        # ⚠️ O tom, jestli se dotaz k obsluze vůbec dostane, rozhodují TYHLE vzory,
        # ne větvení uvnitř — proto jsou obě osoby (ti/vám) u KAŽDÉHO tvaru
        # [[test-both-grammatical-persons]].
        # ⛔ „zdálo se MI" schválně chybí: to o svém snu mluví uživatel, ne Hans.
        nl_patterns=[
            r"\bzd[áa]lo\s+se\s+(ti|v[áa]m)\b",
            r"\bnezd[áa]lo\s+se\s+(ti|v[áa]m)\b",
            r"\bsnilo\s+se\s+(ti|v[áa]m)\b",
            r"\bo\s+[čc]em\s+(jsi|jste)\s+snila?\b",
            r"\bm[ěe]l(a)?\s+(jsi|jste)\s+.{0,20}\bsen\b",
            # ── HANS_DREAM_NOT_ASPIRATION_V1 (21. 9.) — `sen` JE HOMONYMUM ──────
            # Nocni sen x TOUHA. Doloženo třikrát, a ve VŠECH TŘECH OSOBÁCH řeči:
            #   „váš sen je ukotvený v konkrétní realitě"        (cizi, 20. 9.)
            #   „co je ted tvuj sen, tvoje velka aspirace?"      (domaci, 21. 9.)
            #   „váš sen – myslím tím spíš vaši aspiraci – je…"  (cizi, 21. 9.)
            # Na vsechny tri Hans vypsal nocni sen o ledovych vlockach.
            # A neni to jen vec testu — v korpusu je SKUTECNA zprava
            # „popiš mi svůj sen do budoucna, o čem přemýšlíš".
            #
            # 🪤 PRVNI OPRAVA (tehoz dne rano) ZAKAZOVALA JEN SPONU za frazi
            # („sen JE…") a padla hned v prvnim rozhovoru: v A/13 stoji za „sen"
            # CARKA, v B/9 POMLCKA. Zakazovat dalsi a dalsi interpunkci je zavod,
            # ktery se nevyhraje — rozhoduje NAOPAK pritomnost dukazu.
            #
            # 🔑 Nocni vyznam nese skoro vzdy EPIZODICKOU STOPU (zdalo, snilo,
            # noc, spanek) nebo KVALIFIKATOR (posledni/dnesni sen). Holy tvar
            # „tvuj sen" bez obojiho je statisticky touha → radsi nic.
            # ⛔ Podminka „veta musi byt otazka" NESTACI — zabila by holy
            # kvalifikovany tvar z help_textu („tvůj poslední sen").
            # Zmereno na kontrolnim seznamu 8 vet: chybne verdikty 3 → 0;
            # na 1 566 skutecnych zpravach ma tenhle vzor 0 vyskytu, takze
            # zuzeni nestoji nic.
            r"\b(tv[ůu]j|v[áa][šs])\s+(posledn[íi]|dne[šs]n[íi])\s+sen\b",
            r"^(?=.*\b(zd[áa]l\w*|snilo|noc\w*|ve\s+sp[áa]nku|probudil))"
            r".*\b(tv[ůu]j|v[áa][šs])\s+sen\b",
            r"\bco\s+se\s+(ti|v[áa]m)\s+zd[áa]lo\b",
            r"\bjak[ýy]\s+(jsi|jste)\s+m[ěe]l(a)?\s+sen\b",
        ],
        handler=_cc._cmd_sen,
        help_text="Co se mi zdálo (přímo z deníku): zdálo se ti něco? tvůj poslední sen",
    )

    _cc.register(
        "zpravy",
        slash_aliases=["zpravy", "zprávy", "novinky"],
        # HANS_ZPRAVY_CHAT_V1 — ZÁMĚRNĚ úzké: holé „co je nového?“ je pozdrav
        # Hansovi, ne dotaz na zprávy. Musí zaznít zprávy/noviny/svět/píšou.
        nl_patterns=[
            r"\b(?:ve|v) zpr[áa]v[áa]ch\b",
            r"\b(?:ve|v) novin[áa]ch\b",
            r"\bco (?:se )?p[íi][šs]ou\b",
            r"\bp[íi][šs]ou\s+(?:o|v|ve|na)\b",
            r"\b(?:co|jak[ée]) (?:je |jsou )?(?:nov[ée]ho|novinky)\s+(?:ve|ze|v)\s+sv[ěe]t",
            r"\b(?:nejnov[ěe]j[šs][íi]|aktu[áa]ln[íi]|dne[šs]n[íi]|hlavn[íi]) zpr[áa]v",
            r"\b(?:n[ěe]jak[ée]|jak[ée]) zpr[áa]vy\b",
            r"\bzpr[áa]vy (?:o|ohledn[ěe]|ze sv[ěe]ta)\b",   # ne „zprávy z Matrixu“
            # HANS_ZPRAVY_CO_SE_DEJE_V1 (6. 10.) — /tazatel: „co se děje v poslední
            # době zajímavého ve světě?“ šlo do volného hovoru a model si zprávy
            # vymyslel (soud v Německu kvůli AI souhrnům — v titulcích není).
            r"\bco\s+se\s+(?:\w+\s+){0,5}?d[ěe]je\b[^.?!]{0,40}\bve?\s+sv[ěe]t[ěe]\b",
            # HANS_ZPRAVY_SLEDUJES_V1 (4. 10.) — „sledujete vůbec zprávy? co jste
            # v nich dnes zaznamenal?“ šlo do volného hovoru a model zprávy i odkaz
            # VYMYSLEL. 1 336 vět deníku: 0 shod (nic neukradne).
            r"\bsleduj\w*\s+(?:v[ůu]bec\s+|n[ěe]jak[ée]\s+)?zpr[áa]vy\b",
            r"\bzpr[áa]v\w*\b[^.?!]{0,40}\b(?:zaznamenal|zachytil|zaujal[oa]?|[čc]etl|vid[ěe]l|sly[šs]el)\w*",
            r"\b(?:zaznamenal|zachytil|[čc]etl|sly[šs]el)\w*\b[^.?!]{0,30}\bve?\s+zpr[áa]v",
            # HANS_ZPRAVY_DENI_MISTO_V1 (7. 10.) — „co se DNES děje v <zemi>?“ šlo do
            # volného hovoru (shoda se zprávami pod prahem podkladu) a model vypsal
            # VYMYŠLENÉ titulky ve tvaru přehledu zpráv. Příkaz na tutéž větu vrací
            # skutečné zprávy s odkazy, na neznámé místo „nic jsem nenašel“.
            # Úzké ZÁMĚRNĚ: musí zaznít časové slovo A místo za „v/ve/na“; věta
            # o tazateli (ti, vám) a místa domácnosti, dny a TV nesednou.
            # 📏 1 356 reálných vět + 1 262 vět tazatele: 0 shod (nic neukradne).
            (r"^(?![^?!]*\b(?:ti|tob[ěe]|tebe|v[áa]m|v[áa]s)\b)"
             r"(?=[^?!]*\b(?:dnes\w*|te[ďd]|aktu[áa]ln\w*|pr[áa]v[ěe]|v[čc]era|nyn[íi]|moment[áa]ln\w*)\b)"
             r"[^?!]*\bco\s+(?:se\s+)?(?:\w+\s+){0,3}?"
             r"(?:d[ěe]je|stalo|ud[áa]lo|d[ěe]lo|je\s+(?:\w+\s+)?nov[ée]ho)\s+(?:\w+\s+){0,2}?(?:v|ve|na)\s+"
             r"(?!dom|byt|pokoj|kuchyn|ob[ýy]v|lo[žz]n|zahrad|m[ée]\b|moj|na[šs]|va[šs]|tv[éeoůu]|posledn|noci|pond|"
             r"[úu]ter|st[řr]ed|[čc]tvrt|p[áa]t|sobot|ned[ěe]l|t[ýy]d|v[íi]kend|pr[áa]c|[šs]kol|televiz|tv\b|kodi|"
             r"film|tom|t[ée]\b|tomhle|hlav|studi|den[íi]k|sv[ěe]t|zpr[áa]v|novin|okol|m[íi]stnost|kamer|pam[ěe]t|"
             r"syst[ée]m|po[čc][íi]ta|pc\b)\w{3,}"),
            # HANS_ZPRAVY_SVET_NE_DENIK_V1 — „co se dneska událo ve světě“
            r"\bco\s+se\s+(?:\w+\s+){0,2}(?:stalo|d[ěe]je|d[ěe]lo|ud[áa]lo)\w*\s+(?:\w+\s+){0,2}ve?\s+sv[ěe]t",
        ],
        handler=_cc._cmd_zpravy,
        help_text="Co je ve zprávách — /zpravy [téma]",
    )

    _cc.register(
        "odkazy_zprav",
        slash_aliases=["odkazy_zprav"],
        nl_patterns=[],            # jen z vlákna (thread_zpravy), samo nesepne
        handler=_cc._cmd_odkazy_zprav,
        help_text="Odkazy na články k poslednímu výpisu zpráv",
    )

    _cc.register(
        "demagog",
        slash_aliases=["demagog", "overeno", "overene"],
        # HANS_DEMAGOG_V1 — ZÁMĚRNĚ úzké: slovo „demagog“, nebo dotaz na OVĚŘENÍ
        # výroku/politika. Obecné „je pravda, co řekl X?“ ne (X bývá člen
        # domácnosti). Tykání i vykání: ověřil/ověřoval/ověřujete…
        nl_patterns=[
            r"\bdemagog",
            r"\bov[eě][řr](il[ia]?|ovali?|uje|ujete|ujete|ili)\b.{0,40}\b(v[ýy]rok\w*|politik\w*)",
            # HANS_DEMAGOG_FOLLOWUP_V1 — „ověřené výroky“ (tazatel 3. 10., B8)
            r"\bov[eě][řr]en[éeý]\w*\s+v[ýy]rok",
            r"\bfact[- ]?check\w*",
        ],
        handler=_cc._cmd_demagog,
        help_text="Ověřené výroky politiků z Demagog.cz — /demagog <jméno nebo téma>",
    )

    _cc.register(
        "hlavnizprava",
        slash_aliases=["hlavnizprava", "hlavni"],
        # ZÁMĚRNĚ jen jednotné číslo: „jaké jsou hlavní zprávy“ je přehled dne (/zpravy).
        nl_patterns=[
            r"\bhlavn[ií]\s+zpr[aá]v(a|u|ou|[eě])\b",
        ],
        handler=_cc._cmd_hlavnizprava,
        help_text="Hlavní zpráva posledních dvou dnů, jak jsem ji vybral — /hlavnizprava",
    )

    _cc.register(
        "sleva",
        slash_aliases=["sleva", "slevy", "letaky"],
        nl_patterns=[
            r"\bco\b.{0,30}\bv\s+akci\b",
            r"\bjak[eé]\s+(?:jsou|m[aá]me)\b.{0,20}\bslevy\b",
            r"\bslevy\s+z\s+let[aá]k",
            r"\b(?:p[rř]idej|hl[ií]dej|sleduj|odeber|sma[zž])\b.{0,60}\bslev(?:u|y)?\b",
            r"\bseznam\s+(?:hl[ií]dan[yý]ch\s+)?slev\b",
        ],
        handler=_cc._cmd_sleva,
        help_text="Slevy z letáků podle hlídaného seznamu — /sleva (přidej …, odeber …, teď)",
    )

    _cc.register(
        "zdroje",
        slash_aliases=["zdroje", "odkazy", "literatura", "zdroj", "odkaz"],
        # HANS_SOURCES_VYKANI_V1 (6.9.) — vzory znaly JEN TYKANI a jen uzky
        # okruh formulaci. Zmereno: tykani 6/6, VYKANI 0/6 — a prave cizi clovek
        # Hansovi vyka. [[test-both-grammatical-persons]]
        # Dolozeno 6. 9.: "dokazal byste mi ukazat, odkud presne jste to cetl,
        # tedy nejaky odkaz nebo nazev?" -> prikaz se nespustil, odpovedel LLM
        # frazi "nemam pristup k externim zdrojum, moje znalosti pochazeji
        # z trenninku". To je NEPRAVDA: v deniku je 473 radku se `source_url`
        # (467 z nich web_read), takze Hans ty odkazy realne MA.
        #
        # ⚠️ Klauzule v promptu uz zuzena JE (HANS_SOURCE_QUERY_V1, 17. 7.) a
        # dokonce vyslovne rika "Neodbyvej frazi 'nemam pristup k externim
        # zdrojum'". Model ji presto pouzil — proto se to resi BRANOU, ne dalsi
        # vetou v promptu.
        #
        # 📏 ZMERENO na 1362 realnych replikach: +9 nove zachycenych,
        # 0 ukradenych jinemu prikazu, 0 ztracenych.
        # ⛔ Vzory na odkaz zamerne vyzaduji 2. osobu (das/date/mas/mate/posles),
        # aby neunesly opacny smer — kdyz uzivatel odkaz SAM POSILA
        # ("posilam ti odkaz", "precti si tenhle odkaz"), patri to do cteni webu,
        # ne do vypisu zdroju [[read-and-remember-links]].
        # Samostatny vzor na "odkaz(y) na clanek" tu BYL a je ZAMERNE PRYC:
        # chytal prave "posilam ti odkaz na clanek", a pritom je nadbytecny —
        # vsechny realne dotazy ("mas odkaz na clanek…", "muzes poslat odkazy
        # na clanky…") uz pokryvaji vzory s 2. osobou. Overeno na korpusu.
        nl_patterns=[
            # HANS_SOURCES_BODY_V1 (9. 9.) — „odkud máš/máte INFORMACE o X".
            # Tykací tvar kradlo `/studium` (`_ORIGIN_PAT`), vykací nechytal
            # NIKDO a padal na LLM. Obě osoby schválně v jednom vzoru
            # [[test-both-grammatical-persons]].
            r"\bod[kK]ud\s+(m[áa][sš]|m[áa]te)\s+(ty\s+|tyhle\s+)?"
            r"(informace|inform\w+|[úu]daje|poznatky)\b",
            r"\bodkud\s+(\w+\s+){0,2}(jsi|si|to|jste)\b.{0,18}"
            r"(čerpal|cerpal|m[áa][šs]|m[áa]te|vz[áa]l|v[íi][šs]|v[íi]te|[čc]etl|[čc]etla|dozv[ěe])",
            # HANS_VYKANI_DLOUHA_OTAZKA_V1 (9. 9.) — vzor chtel „cerpate" HNED
            # za „odkud". Doloženo rozhovorem: „…odkud PŘESNĚ TYTO INFORMACE
            # čerpáte?" propadlo, LLM router zvolil `/rozhovory` a druhá brána
            # to zamítla („ptá se na svět") — takže Hans řekl, že zdroj nemá,
            # ačkoli ho v deníku s odkazem MÁ. Povoleno až 4 slova mezi.
            r"\bodkud\s+(\w+\s+){0,4}[čc]erp[áa]([šs]|te)\b",
            # HANS_SOURCES_ODKUD_ZNAS_V1 (7. 10.) — „odkud znáš/víš X“ bez
            # „to/jsi“ propadlo: router zvolil /cetl, druhá brána ho zamítla
            # („ptá se na svět“) a Hans řekl, že odkaz nemá, ačkoli článek
            # s odkazem četl týž den. 📏 2 369 reálných vět: +3 („odkud víš
            # o <osobnosti>?“ 2×, dřív abstinence; „odkud víš, že tu někdo je?“).
            r"\bodkud\s+((to|ho|ji|je|jej|tohle|o\s+tom)\s+)?"
            r"(zn[áa][šs]|zn[áa]te|v[íi][šs]|v[íi]te)\b",
            r"\b(z\s+)?[čc]eho\s+(jsi|si|jste)\s+.{0,10}(čerpal|cerpal|vych[áa]zel)",
            r"\b(z\s+)?[čc]eho\s+(studuje[šs]|studujete)\b",
            r"\b(d[áa][šs]|d[áa]te|m[áa][šs]|m[áa]te|po[šs]le[šs]|po[šs]lete)"
            r"\s+(mi\s+)?(n[ěe]jak[ýéya]\s+)?odkaz",
            r"\bm[uů][žz]e([šs]|te)\s+(mi\s+)?(poslat|uk[áa]zat|d[áa]t)\s+.{0,14}odkaz",
            # HANS_SOURCES_PRONOUN_V1 (22. 9.) — zajmeno bylo jen "to", takze
            # "kde jste HO cetl?" propadlo az do volneho hovoru a Hans si
            # vymyslel cas zaznamu. Doloženo zivym testem 22. 9.
            # 📏 Zmereno na 1 569 realnych vetach: PRIRUSTEK 0 — nic to
            # nekrade a nic neztraci; je to pokryti tvaru, ktery se zatim
            # nevyskytl, za nulovou cenu.
            r"\bkde\s+(jsi|si|jste)\s+((to|ho|je|ji|jej|tohle|tenhle)\s+)?"
            r"(četl|cetl|na[šs]el|na[šs]la|vzal|vzala|dozv[ěe]d[ěe]l)",
            r"\bjak[ýy]\s+(je\s+)?(ten\s+)?zdroj",
            r"\b(uka[žz]|uka[žz]te)\s+(mi\s+)?(sv[ée]\s+)?zdroj",
            r"\bjak[ée]\s+(m[áa][šs]|m[áa]te)\s+.{0,12}zdroj",
            r"\bposli\s+(mi\s+)?odkaz|\bpo[šs]li\s+(mi\s+)?odkaz",
        ],
        handler=_cc._cmd_zdroje,
        help_text="Odkazy na to, co jsem četl (přímo z deníku): odkud jsi čerpal?",
    )

    _cc.register(
        "videl",
        slash_aliases=["videl", "viděl"],
        nl_patterns=[
            # jen mě/nás — obecné „kdy jsi X viděl" (film, věc) patří LLM,
            # špatný deterministický únos by byl horší než žádný. Jiné osoby
            # jdou přes /videl <jméno> (resolve person_name_forms v handleru).
            r"\bkdy\s+(jsi|si)\s+(m[ěe]|n[áa]s)\s+(naposledy\s+)?vid[ěe]l",
            r"\bvid[ěe]l\s+(jsi|si)\s+m[ěe]\s+(dnes|včera|naposledy)",
        ],
        handler=_cc._cmd_videl,
        help_text="Kdy jsem koho naposledy viděl (přímo z deníku person_seen)",
    )

    _cc.register(
        "dnes",
        slash_aliases=["dnes", "dnesek", "den"],
        nl_patterns=[
            # HANS_ZPRAVY_SVET_NE_DENIK_V1 (4. 10.) — „co se dneska událo ve světě“
            # (hlas 3. 10.) je dotaz na zprávy, ne na deník domu
            r"co\s+se\s+(dnes|dneska|d[ňn]es)\w*\s+(d[ěe]lo|stalo|ud[áa]lo)(?!.*\b(?:ve?\s+sv[ěe]t|v\s+[čc]esk|ve?\s+zpr[áa]v|v\s+republi))",
            r"co\s+se\s+(d[ěe]lo|stalo|ud[áa]lo)\s+(dnes|dneska)(?!.*\b(?:ve?\s+sv[ěe]t|v\s+[čc]esk|ve?\s+zpr[áa]v|v\s+republi))",
            r"co\s+(bylo|se\s+d[ěe]lo)\s+(dnes\s+)?(doma|v\s+dom[ěe])",
            r"jak[ýy]\s+byl\s+(dnes(n[íi])?)?\s*den",
            r"shr[nň]\s+(mi\s+)?(dnes(ek|n[íi]\s+den)?)",
        ],
        handler=_cc._cmd_dnes,
        help_text="Co se dnes dělo v domě (z deníku): co se dnes dělo?",
    )

    _cc.register(
        "rezim",
        slash_aliases=["rezim", "režim", "spis", "spíš"],
        nl_patterns=[
            r"\b(jsi|js|nejsi)\s+(te[ďd]\s+)?(v\s+)?(re[žz]imu\s+)?sp[áa]nk",
            r"\bsp[íi][sš]\s*\?",
            r"\bnesp[íi][sš]\b",
            r"\b(jsi|nejsi)\s+vzh[uů]ru",
            r"\bne?m[ěe]l\s+bys?\s+b[ýy]t\s+(v\s+)?(re[žz]imu\s+)?sp[áa]nk",
            r"\bv\s+jak[ée]m\s+jsi\s+re[žz]imu",
            r"\bhl[íi]d[áa][sš]\s+(te[ďd]\s+)?(d[uů]m|dom)",
        ],
        handler=_cc._cmd_rezim,
        help_text="V jakém jsem režimu (spánek, hlídání) — přímo ze stavu",
    )

    _cc.register(
        "film",
        slash_aliases=["film", "filmy"],
        nl_patterns=[
            # HANS_FILM_SIMILAR_V1 (25. 9.) — „něco podobného jako X“. SMĚROVÁNÍ chce
            # filmový kontext: holé „něco podobného“ poslalo na /film i „zažil jsi
            # někdy něco podobného?“. Uvnitř /film pak stačí široký predikát.
            r"\bfilm\w*\b[^?.!]{0,30}\bpodobn\w*|\bpodobn\w*\b[^?.!]{0,30}\bfilm",
            r"\b(?:pod[ií]vat|koukat|kouknout)\b[^?.!]{0,30}\bpodobn\w*|\bpodobn\w*\b[^?.!]{0,30}\b(?:pod[ií]vat|koukat|kouknout)\b",
            r"\bod\s+(?:stejn\w+|t\w+\s+sam\w+)\s+re[zž]is[eé]r",
            # HANS_FILM_RECOMMEND_V1 (24. 9.) — žádost o doporučení filmu
            r"\bdoporu[cč]\w*\b[^?.!]{0,40}\bfilm",
            r"\bfilm\w*\b[^?.!]{0,30}\bdoporu[cč]",
            r"\btip\w* na (?:n[eě]jak\w* )?film",
            r"\b(?:co|jak[yý] film)\b[^?.!]{0,20}\b(?:bych|bychom|bysme|m[aá]m|m[aá]me|m[eě]l|m[eě]la|m[eě]li)\b[^?.!]{0,15}\b(?:pustit|pod[ií]vat|koukat|vid[eě]t)\b[^?.!]{0,15}(?:film|ve[cč]er|dnes)",
            # HANS_COUNT_FILMS_BOOKS_V1 (14. 9.) — pocet videnych filmu. SLOVESO je
            # povinne: bez nej sedl vzor i na „kolik stoji ten film v kine?“.
            # Na 2 248 realnych vetach chyti jen doložený dotaz z 13. 8.
            r"\bkolik\s+(?:\w+\s+){0,3}film\w*[^.?!]{0,30}\b(?:vid[\u011be]l|koukal|sledoval|zhl[\u00e9e]dl)",
            r"\bkolik\s+(?:jsi|jste|sis)\s+(?:u[\u017ez]\s+)?(?:vid[\u011be]l|koukal|sledoval|zhl[\u00e9e]dl)\w*\s+(?:\w+\s+)?film",
            r"posledn[ií].{0,10}film",
            # HANS_FILM_OPINION_ANSWER_V1 (22. 9.) — "jaky je tvuj nejoblibenejsi
            # film?" nesedlo na ZADNY vzor a padalo do volneho hovoru, kde si Hans
            # oblibeny film VYMYSLEL (doloženo 22. 9.). Handler na to ma vlastni
            # vetev z `movie_opinion`, takze se to sem pusti zamerne.
            r"\bnejobl[ií]ben[eě]j[sš][ií]\b[^.?!]{0,12}\b(film|ser[ií][aá]l)",
            # HANS_FILM_QUERY_BOUNDARY_V1 (2.9.) — `\b` je tu NUTNA, ne kosmetika:
            # bez ni „jak[ýy]" matchne uvnitr slova „NEjaky", takze dotazovy vzor
            # spolknul ZADOST O SPUSTENI. Doloženo rozhovorem: „pust mi nejaky
            # film" → vypis, co se naposledy hralo. Tim se navic nikdy nedostane
            # ke slovu agentni `kodi_play_film` (a jeho HANS_KODI_NO_TITLE_V1).
            # Zmereno na 1028 realnych vetach: 7 zasahu → 1, a tou jedinou
            # zbylou je „jaky film jsi videl naposled?" (spravne). Sest, ktere
            # odpadly, jsou zadosti o spusteni („muzes pustit na kodi nejaky
            # film?"), dotazy na bezici prehravani („je pusteny nejaky film?")
            # a vypraveni — ani jedna neni dotaz na to, co Hans videl.
            # HANS_FILM_JAKY_O_HANSOVI_V1 (16. 10.) — jen když věta mluví NA Hanse
            # („jaký film jsi viděl / vás zaujal / sis dal“). Holé „jaký film je
            # Indiana Jones…?“ je otázka na svět; /film ji cizímu odmítl („O tom
            # mluvím jen se svou domácností“). 📏 3 484 vět: 5 shod, všech 5 drží.
            r"\bjak[ýy].{0,10}film(?=[^?.!]{0,40}\b(?:jsi|jste|sis|ses|vas|v[aá]s|v[aá]m|t[eě]|tebe|ti|naposled\w*|ned[aá]vno)\b)",
            # HANS_VIDEL_UPRESNI_V1 (23. 9.) — + vykani (`jste`); "videl"
            # s kamerovym dovetkem ("kamerou", "venku", "z okna") uz neni film
            # (driv i "co jsi videl kamerou?" vratilo vypis filmu).
            r"co\s+(jsi|sis|jste)\s+(dnes\w*\s+|včera\s+|naposledy\s+)?"
            r"(vid[ěe]l(?!\w*\s+(?:kamer|venku|z\s+okna|za\s+oknem|na\s+ulic))"
            r"|koukal|sledoval|d[íi]val)",
            r"co\s+jsem?\s+(dnes\w*\s+)?(vid[ěe]l|koukal|sledoval)",
            r"\bfilm\w*\s+(jsi|sis)\s+(vid|koukal|sledoval)",
        ],
        handler=_cc._cmd_film,
        help_text="Jaký film/pořad jsem viděl (přímo z deníku kodi_playing)",
    )

    _cc.register(
        "rozhovory",
        slash_aliases=["rozhovory", "rozhovor", "souhrn", "sumar", "sumář"],
        nl_patterns=[
            # Uživatel píše s překlepy a i/y („bavily", „pripomenou") → vzory musí
            # být tolerantní; jinak dotaz propadne do LLM a to si vymyslí rozhovor,
            # který se nikdy nestal (doložený případ 13.7. — smyšlený Vietnam).
            # „o čem jsme (se) bavili/mluvili/povídali (v pátek / 27. dubna)"
            r"o\s+[čc]em\s+jsme\b",
            r"co\s+jsme\s+(spolu\s+)?(prob[íi]r|[řr]e[šs]il|probir)",
            # „shrň náš rozhovor", „shrň o čem jsme mluvili"
            r"(shr[ňn]|shrnout|sum[áa]rizuj)\s+\w*\s*(n[áa][šs]\s+)?"
            r"(rozhovor|konverzac|chat)",
            # „vytáhni/vytáhneš vzpomínku z 27. dubna", „vzpomínky z minulého týdne"
            r"(vyt[áa]h\w*|uk[áa][žz]\w*|najdi)\s+\w*\s*vzpom[íi]nk",
            r"vzpom[íi]nk\w*\s+z\s+(\d|[a-zá-ž]{4,})",
            # „na co jsem se tě ptal (v pátek)"
            r"na\s+co\s+jsem\s+se\s+t[ěe]\s+ptal",
            # detail na vyžádání: „připomeň (mi) rozhovor o Maradonovi"
            r"(p[řr]ipome[ňnt]\w*|vzpome[ňn]\w*|zopakuj)\s+.{0,25}"
            r"(rozhovor|konverzac|bavil|mluvil|pov[íi]dal)",
            r"(rozhovor|konverzac\w*)\s+o\s+\w{3,}",
            r"(bavil|mluvil|pov[íi]dal)[iy]\s+jsme\s+(se\s+)?o\s+\w{3,}(?![^.?!]*\s[-–]\s)",  # HANS_ROZHOVORY_NOT_PREAMBLE_V1: „Mluvili jsme o X – <jiná otázka>“ je úvod, ne dotaz
            # „pošli detail o rychlém obědě…", „ukaž ten recept", „vypiš záznam o…"
            # Bez tohohle Hans odpověď VYGENERUJE ZNOVU (doložený případ 13.7. —
            # do receptu si přidal koriandr, který v původním zápisu nebyl).
            r"(po[šs]l\w*|uka[žz]\w*|zopakuj\w*|vypi[šs]\w*|dej\s+mi)\s+"
            r".{0,25}(detail|recept|postup|z[áa]znam|z[áa]pis)",
            r"co\s+jsi\s+(mi\s+)?(psal|napsal|poslal|[řr][íi]kal|navrhl|"
            r"doporu[čc]il)",
            r"\b(ten|tu|to)\s+(recept|postup|n[áa]vrh)\b",
        ],
        handler=_cc._cmd_rozhovory,
        help_text="O čem jsme se bavili (z deníku): /rozhovory [v pátek | "
                  "27. dubna 2026 | minulý týden]; detail: „připomeň rozhovor o X“",
    )

    _cc.register(
        "hlidej",
        slash_aliases=["hlidej", "hlídej", "guard", "hlidani", "hlídání"],
        nl_patterns=[
            r"(hl[íi]dej|str[ěe][žz]|dohl[íi]žej)\s+(dům|dum|byt|m[íi]stnost|to\b)",
            r"zapni\s+(hl[íi]d[áa]n[íi]|str[áa][žz])",
            r"vypni\s+(hl[íi]d[áa]n[íi]|str[áa][žz])",
        ],
        handler=_cc._cmd_hlidej,
        help_text="Hlídací režim: /hlidej [stop|stav] — při pohybu/změně světla "
                  "pošlu snímek na Matrix",
    )

    _cc.register(
        "preloz",
        slash_aliases=["preloz", "přelož", "preklad", "překlad", "translate"],
        nl_patterns=[
            # HANS_PRELOZ_HOLY_TVAR_V1 (28.8.) — nález uživatele: na holé „preloz"
            # se Hans pustil MALOVAT (vzory čekaly za slovem ještě „to"/„ten").
            # Holý tvar dnes řeší obecné pravidlo HANS_BARE_ALIAS_V1 v
            # `parse_command` — zvláštní vzor na něj tu ZÁMĚRNĚ NENÍ, ať totéž
            # nedělají dva mechanismy, které se můžou rozejít.
            # Zůstává jen infinitiv („zkus to přeložit", „můžeš to přeložit"). Vzor je široký
            # a chytí i větu, kde překlad Kodi nemyslíš — nevadí: obsluha se napřed
            # ptá Kodi, co běží, a bez přehrávání odpoví „nic neběží", nic nespustí.
            r"p[řr]elo[žz]it\b",
            r"p[řr]elo[žz]\s+(to|ten|tenhle|tenhleten|mi|nam|n[áa]m)\b",
            r"p[řr]elo[žz]\s+(ten\s+)?(dokument|film|po[řr]ad|dokument[áa]rn)",
            r"(ud[ěe]l[aáeě]\w*|p[řr]iprav\w*)\s+(mi\s+)?[čc]esk[ou]\w*\s+(stopu|dabing|verzi)",
            r"jak\s+(to\s+)?jde\s+(ten\s+)?p[řr]eklad",
            # 28.8.: „uz mas hotovy preklad?" LLM router poslal na /dilo, takže
            # uživatel dostal odpověď o něčem jiném, zatímco překlad běžel.
            r"hotov\w*\s+p[řr]eklad|p[řr]eklad\w*\s+(u[žz]\s+)?hotov",
            # DOTAZ na hotové překlady — musí být i TADY, ne jen v obsluze:
            # nl_patterns rozhodují, jestli se k obsluze vůbec dojde.
            r"co\s+(jsi|u[žz]|v[šs]echno)\s+[\w\s]{0,25}?p[řr]elo[žz]",
            r"kter[ée]\s+[\w\s]{0,25}?p[řr]elo[žz]",
            r"seznam\s+p[řr]eklad|p[řr]elo[žz]en[éy]\s+(dokument|po[řr]ad|film)",
        ],
        handler=_cc._cmd_preloz,
        help_text="Připrav českou stopu k tomu, co běží v Kodi: "
                  "/preloz [stav|seznam]",
    )

    _cc.register(
        "vypnipc",
        slash_aliases=["vypnipc", "vypnipc", "shutdown", "pcoff", "vypnout"],
        # HANS_UNIFY_ACTIONS_V1 — přirozená řeč („vypni pc") ZÁMĚRNĚ nemá regex:
        # vypnutí PC je destruktivní → padá k agentní akci pc_shutdown, která se
        # NAPŘED zeptá a vypíše, co na PC běží. Explicitní /vypnipc slash zůstává
        # okamžitý (výslovný povel = výslovný záměr). Ztráta při mozku dole = žádná
        # (PC dole ⇒ není co vypínat), slash funguje vždy.
        nl_patterns=[],
        handler=_cc._cmd_vypnipc_slash,
        help_text="Vypnu počítač, až dodělá práci (/vypnipc hned = okamžitě)",
    )

    _cc.register(
        "router",
        slash_aliases=["router", "sit", "síť", "vpn"],
        nl_patterns=[
            r"\b(?:stav|zkontroluj|zkontrolujte|jak\s+je\s+na\s+tom|jak\s+jde|"
            r"co\s+d[ěe]l[áa])\b[^?.!]{0,20}\b" + _cc._ROUTER_VEC + r"\b",
            r"\b" + _cc._ROUTER_ZLE + r"\b[^?.!]{0,15}\b" + _cc._ROUTER_VEC + r"\b",
            r"\b" + _cc._ROUTER_VEC + r"\b[^?.!]{0,15}\b" + _cc._ROUTER_ZLE + r"\b",
        ],
        handler=_cc._cmd_router,
        help_text="Stav routeru, internetu a VPN (jen pro domácnost)",
    )

    _cc.register(
        "vpnprepni",
        slash_aliases=["vpnprepni", "vpnpřepni", "prepnivpn", "přepnivpn"],
        nl_patterns=[
            r"\b(?:p[řr]epni|p[řr]epn[ěe]te|zm[ěe][nň]|zm[ěe][nň]te|vym[ěe][nň]|"
            r"vym[ěe][nň]te)\b[^?.!]{0,20}\b(?:vpn\w*|server\w*)\b",
        ],
        handler=_cc._cmd_vpnprepni,
        help_text="Přepnu VPN na další server (jen pro domácnost)",
    )

    _cc.register(
        "zdravi",
        slash_aliases=["zdravi", "zdraví", "health"],
        nl_patterns=[
            r"jak\s+(ti|se\s+ti|se\s+m[áa]š?).{0,15}(zdrav|syst[ée]m|slu[žz]b)",
            r"(zdrav|stav).{0,10}(syst[ée]m|slu[žz]eb|z[áa]vislost)",
            r"(funguje|jede|b[ěe][žz][íi]).{0,12}(ollama|comfyui|mozek|zrak)",
            r"jsi\s+v\s+po[řr][áa]dku",
        ],
        handler=_cc._cmd_zdravi,
        help_text="Zdraví závislostí (Ollama/ComfyUI/Kodi/STT/PC/disk); /zdravi vylec = self-heal",
    )

    _cc.register(
        "nalez",
        slash_aliases=["nalez", "nález", "nalezy", "nálezy", "zkousky", "zkoušky"],
        nl_patterns=[
            r"co\s+(u\s+tebe\s+)?na[šs]el\s+kol[áa][čc]",
            r"(nálezy|nalezy)\s+ze\s+zkou[šs]en[íi]",
            r"jak\s+jsi\s+(dopadl|obst[áa]l)\s+ve\s+zkou[šs]",
        ],
        handler=_cc._cmd_nalez,
        help_text="Nálezy ze zkoušení Koláčem; /nalez N = trvalá otázka, /nalez N ne = zamítnout",
    )

    _cc.register(
        "nastroj",
        slash_aliases=["nastroj", "nástroj", "tool"],
        nl_patterns=[
            r"najdi\s+(mi\s+)?(vhodn|n[ěe]jak).{0,15}(model|n[áa]stroj|llm)",
            r"jak[ýy]\s+(model|n[áa]stroj|llm).{0,20}(pro|na)\s+",
        ],
        handler=_cc._cmd_nastroj,
        help_text="Najdi LLM nástroj pro dílo: /nastroj <téma>; schválit N; zamítnout [N …] (bez čísla všechny čekající)",
    )

    _cc.register(
        "brief",
        slash_aliases=["brief", "zadani", "zadání"],
        nl_patterns=[
            r"(p[řr]iprav|udělej).{0,15}(brief|zad[áa]n[íi]|prompt)",
        ],
        handler=_cc._cmd_brief,
        help_text="Destiluj studium do promptu pro dílo: /brief <téma> [coder|esej|obraz]",
    )

    _cc.register(
        "vytvor",
        slash_aliases=["vytvor", "vytvoř", "vyrob", "artefakt"],
        nl_patterns=[
            r"vytvo[řr].{0,20}(z\s+toho|co\s+ses|nastudova|dílo|artefakt)",
        ],
        handler=_cc._cmd_vytvor,
        help_text="Vyrob artefakt z nastudovaného: /vytvor <téma> [coder|obraz]",
    )

    _cc.register(
        "prohloubit",
        slash_aliases=["prohloubit", "prohloub", "deepen"],
        nl_patterns=[],
        handler=_cc._cmd_prohloubit,
        help_text="Návrh prohloubení studia: /prohloubit [schválit|ne|<vlastní kritika>]",
    )

    _cc.register(
        "vhledy",
        slash_aliases=["vhledy", "insights"],
        nl_patterns=[
            r"\bco\s+sis?\s+vši?ml?\s+(u\s+sebe|na\s+sob[ěe])",
            r"\btv[ée]\s+vhledy?\b",
            r"\bm[áa]š?\s+n[ěe]jak[éy]\s+vhled",
        ],
        handler=_cc._cmd_vhledy,
        help_text="Co si Hans všiml ve vlastních datech (offline/herní mód); /vhledy teď = spusť rozbor hned",
    )

    _cc.register(
        "experiment",
        slash_aliases=["experiment", "footgun"],
        nl_patterns=[],
        handler=_cc._cmd_experiment,
        help_text="Experiment: zapnu si herní mód na N minut (default 5), pak auto-resume",
    )

    _cc.register(
        "anomalie",
        slash_aliases=["anomalie", "anomaly", "odchylky"],
        # HANS_LIST_NOT_CLAIM_V1 (21. 9.) — dve vady najednou, obe zmerene:
        #  (a) vzor sepnul na RELATIVNI VETE uprostred souveti: „všiml jste si
        #      něčeho v sobě samém, co se změnilo?" → vypis tydennich odchylek
        #      Hansova PROVOZU. Proto se vzor kotvi na zacatek vety/otazky.
        #  (b) ve vzorech CHYBELO SAMO SLOVO „anomálie"/„odchylka", takze
        #      „jsou nejake anomalie?" i „ukaz prosim anomalie a napady"
        #      propadly do volneho hovoru. Popisny vzor byl, pojmenovani ne.
        # Zmereno na 1 564 skutecnych zpravach: zuzeni nestoji ANI JEDNU
        # (oba vzory tam dnes maji 0 vyskytu), slovo nove chyta 1 realnou zpravu,
        # a na kontrolnim seznamu 11 vet klesnou chybne verdikty 7 → 0.
        nl_patterns=[r"(?:^|[?!.]\s*)co\b.{0,12}\bje\s+jinak",
                     r"(?:^|[?!.]\s*)co\b.{0,12}\bse\b.{0,12}\bzm[ěe]nilo",
                     # HANS_ANOMALIE_NOUN_ONLY_V1 (6. 10.) — hole „odchyl“
                     # chytalo i sloveso („to se trochu odchyluje od zamereni“
                     # → vypis tydennich odchylek). Jen podstatne jmeno.
                     r"\banom[áa]li", r"\bodchyl(?:k|ek)"],
        handler=_cc._cmd_anomalie,
        help_text="Týdenní odchylky ve tvém chování (algoritmicky) — /anomalie teď = spusť detekci",
    )

    _cc.register(
        "zrusmalbu",
        slash_aliases=["zrusmalbu", "zrušmalbu", "nemaluj"],
        nl_patterns=[
            r"\b(?:zru[šs]|zastav|stopni|ukon[čc]i|p[řr]eru[šs])\w*\s+(?:\w+\s+){0,2}?"
            r"(?:malov[áa]n[íi]|malbu|kresbu|kreslen[íi]|ten\s+obraz|obraz|obr[áa]zek)",
            r"\bnemaluj(?:te)?\b",
            r"\bp[řr]esta[ňn](?:te)?\s+malovat\b",
        ],
        handler=_cc._cmd_zrusmalbu,
        help_text="Zruší moje čekající i právě běžící malování: /zrusmalbu (i „zruš malování“, „nemaluj to“)",
    )

    _cc.register(
        "stop",
        slash_aliases=["stop", "zastav", "vypni film"],
        nl_patterns=[
            r"\bzastav(\s+(to|film|ten\s+film|p[řr]ehr[áa]v[áa]n[íi]))?\b",
            r"\bvypni\s+(to|film|ten\s+film|televiz)",
            r"\bstopni\s+(to|film)",
            r"\bu[žz]\s+to\s+nechci\s+(koukat|sledovat)",
        ],
        handler=_cc._cmd_stop,
        help_text="Zastaví přehrávání na TV: /stop (i „zastav film\")",
    )

    _cc.register(
        "pauza",
        slash_aliases=["pauza", "pause", "pauzni"],
        nl_patterns=[r"\bpauzn(i|out)\b", r"\b(dej|d[áa]|dejte)\s+pauzu\b",
                     r"\bzastav\s+na\s+chv[íi]li\b"],
        handler=_cc._cmd_pauza,
        help_text="Pauza/pokračování přehrávání: /pauza",
    )

    _cc.register(
        "pripady",
        slash_aliases=["pripady", "případy", "kauzy"],
        nl_patterns=[
            r"\b" + _cc._PRIPAD + r".{0,40}\bkoláč\w*",
            r"\bkoláč\w*.{0,40}\b" + _cc._PRIPAD,
            r"\b(jaké|jaký|jaká|jakej|kolik|které|který)\b.{0,30}\b" + _cc._PRIPAD
            + r".{0,30}\b" + _cc._PRIPAD_TY,
            r"\b" + _cc._PRIPAD_TY + r"\s+(za\s+|teď\s+|zrovna\s+)?" + _cc._PRIPAD,
            r"\b(otevřen|uzavřen|vyřešen|nevyřešen|rozdělan|běžící)\w*\s+" + _cc._PRIPAD,
        ],
        handler=_cc._cmd_pripady,
        help_text="Koláčovy případy: běžící, poslední uzavřené a jak dlouho typicky trvají",
    )

    _cc.register(
        "hledani",
        slash_aliases=["hledani", "hledání", "hledej", "webshare", "ws"],
        nl_patterns=[
            # Vždy ukotveno na „webshare“ — viz poznámka výše.
            r"\b(na|v|z|ze)\s+webshar\w*",
            r"\bwebshar\w*\s+(hledej|najdi|zkus|m[áa]|nem[áa])",
            r"\b(hledej|najdi|vyhledej|pod[íi]vej\s+se)\b.{0,40}\bwebshar",
            # dotaz na stav probíhajícího stažení
            r"\bjak\s+(to\s+)?(jde|pokra[čc]uje)\s+(to\s+)?stahov[áa]n[íi]",
            r"\bu[žz]\s+(je|to)\s+sta[žz]en\w*\b",
        ],
        handler=_cc._cmd_hledani,
        help_text=("Hledání na Webshare: /hledani <název> [1080p|cz|bluray]; "
                   "/hledani stahni N; /hledani stav"),
    )
