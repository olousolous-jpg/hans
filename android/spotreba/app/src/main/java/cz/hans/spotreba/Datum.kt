package cz.hans.spotreba

import java.time.LocalDate
import java.time.YearMonth

/**
 * Data z textu obalu (rozpoznání textu z fotky). Obal mívá „Spotřebujte do: 12.11.2026“,
 * „Minimální trvanlivost do: 03/2027“, „EXP 12 11 26“; vedle bývá i datum výroby.
 * Vrátí kandidáty od nejpravděpodobnějšího: blízko slov o spotřebě, rozumný rozsah
 * (nejvýš 30 dní zpět, nejvýš 5 let dopředu), datum výroby (vyr., výroba) dozadu.
 */
object Datum {
    private val plne = Regex("""(?<!\d)(\d{1,2})\s?[.\-/ ]\s?(\d{1,2})\s?[.\-/ ]\s?(\d{4}|\d{2})(?!\d)""")
    private val iso = Regex("""(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)""")
    private val slepene = Regex("""(?<!\d)(\d{2})(\d{2})\s?[./\-]\s?(20\d{2})(?!\d)""")   // „0911/2026“ — ztracené lomítko
    private val mesicRok = Regex("""(?<![\d.])(\d{1,2})\s?[./]\s?(20\d{2}|\d{2})(?!\d)(?!\.\d)""")
    // měsíc a rok mezerou: „12 2026“ (Hrošíci) — jen se čtyřmístným rokem
    private val mesicRokMezera = Regex("""(?<![\d.:])(\d{1,2}) (20\d{2})(?![\d.:])""")
    // tečka jen za dnem: „01.032027“ (Kinder Bueno) = 1. 3. 2027
    private val denMesicRok = Regex("""(?<![\d.])(\d{1,2})\s?[./\-]\s?(\d{2})(20\d{2})(?![\d.])""")
    // bodový tisk bez oddělovačů: „271028“ = 27. 10. 2028, „27102028“
    private val bezOddelovacu = Regex("""(?<![\d:])(\d{2})(\d{2})(20\d{2}|\d{2})(?![\d:])""")
    // šarže: „L 281025“, „LOT 12345“ — číslo za tím není datum spotřeby
    private val sarze = Regex("""(?:^|[^A-Za-z])(?:L|LOT|Lot)\s?$""")
    private val spotreba = Regex(
        """spot[řr]eb|trvanliv|použ|pouz|\bdo\b|exp|best|bbe|mhd|ddm|verbrauch|haltbar|consum|minim""",
        RegexOption.IGNORE_CASE)
    private val vyroba = Regex("""vyr|výr|prod|packed|bal\.|baleno|šarž|sarz|lot""", RegexOption.IGNORE_CASE)

    data class Kandidat(val datum: LocalDate, val skore: Int)

    /**
     * Bodový tisk (víčka lahví, kelímky) čte rozpoznání textu s chybami: 0→O, 1→I/l/|,
     * 5→S, 8→B, 2→Z, lomítko → \ ⁄ |. V úsecích, které vypadají jako číslo (aspoň dvě
     * číslice a jen „číselná“ písmena), se písmena převedou na číslice.
     */
    fun normalizuj(text: String): String {
        val mapa = mapOf('O' to '0', 'o' to '0', 'Q' to '0', 'D' to '0', 'I' to '1', 'l' to '1',
            'i' to '1', '|' to '1', 'S' to '5', 's' to '5', 'B' to '8', 'Z' to '2', 'z' to '2')
        // jakékoli lomítko, mezera nebo číslice Unicode → obyčejné (rozpoznání vrací i „⁄“,
        // nezlomitelné mezery a jiné podoby, které na displeji vypadají stejně)
        val sb = StringBuilder()
        for (ch in text) sb.append(when {
            ch == '\n' -> '\n'
            Character.isDigit(ch) && ch !in '0'..'9' -> ('0' + Character.getNumericValue(ch))
            Character.isSpaceChar(ch) || ch == '\t' -> ' '
            ch in "\\⁄∕／⧸╱⟋" -> '/'
            ch in "‐‑‒–—−" -> '-'
            ch in "·•∙‧" -> '.'
            else -> ch
        })
        val t = sb.toString().replace(',', '.')
        return Regex("""[0-9OoQDIli|SsBZz]{1,4}(?:\s?[./\-]\s?[0-9OoQDIli|SsBZz]{1,4}){1,2}""").replace(t) { m ->
            val s = m.value
            if (s.count { it.isDigit() } >= 2) s.map { mapa[it] ?: it }.joinToString("") else s
        }
    }

    /**
     * Datum napsané rukou: „271028“, „27102028“, „27.10.28“, „27 10 2028“, „2710“
     * (letos, nebo příští rok, když už bylo), „10/2028“ (do konce měsíce).
     */
    fun zRuky(t0: String, dnes: LocalDate = LocalDate.now()): LocalDate? {
        val t = normalizuj(t0).trim().trimEnd('.')
        if (t.isEmpty()) return null
        fun rok(r: String) = if (r.length == 2) 2000 + r.toInt() else r.toInt()
        Regex("""^(\d{1,2})\s*[./\- ]\s*(\d{1,2})\s*[./\- ]\s*(\d{2}|\d{4})$""").find(t)?.let { m ->
            val (d, mm, r) = m.destructured
            return runCatching { LocalDate.of(rok(r), mm.toInt(), d.toInt()) }.getOrNull()
        }
        Regex("""^(\d{1,2})\s*[./\- ]\s*(\d{1,2})$""").find(t)?.let { m ->
            val (a, b) = m.destructured
            val d = runCatching { LocalDate.of(dnes.year, b.toInt(), a.toInt()) }.getOrNull() ?: return null
            return if (d.isBefore(dnes)) d.plusYears(1) else d
        }
        Regex("""^(\d{1,2})\s*[./\- ]\s*(20\d{2})$""").find(t)?.let { m ->
            val (mm, r) = m.destructured
            return runCatching { YearMonth.of(r.toInt(), mm.toInt()).atEndOfMonth() }.getOrNull()
        }
        if (!t.all { it.isDigit() }) return null
        return runCatching {
            when (t.length) {
                6 -> LocalDate.of(rok(t.substring(4)), t.substring(2, 4).toInt(), t.substring(0, 2).toInt())
                8 -> LocalDate.of(t.substring(4).toInt(), t.substring(2, 4).toInt(), t.substring(0, 2).toInt())
                4 -> LocalDate.of(dnes.year, t.substring(2).toInt(), t.substring(0, 2).toInt())
                    .let { if (it.isBefore(dnes)) it.plusYears(1) else it }
                else -> null
            }
        }.getOrNull()
    }

    fun najdi(text0: String, dnes: LocalDate = LocalDate.now()): List<LocalDate> {
        val text = normalizuj(text0)
        val kand = mutableListOf<Kandidat>()
        fun pridej(d: LocalDate?, start: Int, bonus: Int) {
            if (d == null || d.isBefore(dnes.minusDays(30)) || d.isAfter(dnes.plusYears(5))) return
            val okoli = text.substring(maxOf(0, start - 40), start).lowercase()
            var s = bonus
            if (spotreba.containsMatchIn(okoli)) s += 10
            if (vyroba.containsMatchIn(okoli)) s -= 15
            if (sarze.containsMatchIn(text.substring(maxOf(0, start - 5), start))) s -= 20
            if (!d.isBefore(dnes)) s += 3
            kand += Kandidat(d, s)
        }
        for (m in iso.findAll(text)) {
            val (r, mm, dd) = m.destructured
            pridej(runCatching { LocalDate.of(r.toInt(), mm.toInt(), dd.toInt()) }.getOrNull(), m.range.first, 2)
        }
        for (m in plne.findAll(text)) {
            val (dd, mm, rr) = m.destructured
            val rok = if (rr.length == 2) 2000 + rr.toInt() else rr.toInt()
            pridej(runCatching { LocalDate.of(rok, mm.toInt(), dd.toInt()) }.getOrNull(), m.range.first, 2)
        }
        for (m in slepene.findAll(text)) {
            val (dd, mm, rr) = m.destructured
            pridej(runCatching { LocalDate.of(rr.toInt(), mm.toInt(), dd.toInt()) }.getOrNull(), m.range.first, 0)
        }
        for (m in denMesicRok.findAll(text)) {
            val (dd, mm, rr) = m.destructured
            val d = runCatching { LocalDate.of(rr.toInt(), mm.toInt(), dd.toInt()) }.getOrNull() ?: continue
            if (kand.any { it.datum == d }) continue
            pridej(d, m.range.first, 0)
        }
        for (m in bezOddelovacu.findAll(text)) {
            val (dd, mm, rr) = m.destructured
            val rok = if (rr.length == 2) 2000 + rr.toInt() else rr.toInt()
            val d = runCatching { LocalDate.of(rok, mm.toInt(), dd.toInt()) }.getOrNull() ?: continue
            if (kand.any { it.datum == d }) continue
            pridej(d, m.range.first, -1)
        }
        for (m in mesicRok.findAll(text)) {
            // „03/2027“ = do konce měsíce (minimální trvanlivost bývá jen měsíc a rok)
            val (mm, rr) = m.destructured
            val rok = if (rr.length == 2) 2000 + rr.toInt() else rr.toInt()
            val ym = runCatching { YearMonth.of(rok, mm.toInt()) }.getOrNull() ?: continue
            if (kand.any { YearMonth.from(it.datum) == ym }) continue
            pridej(ym.atEndOfMonth(), m.range.first, 0)
        }
        // šarže a datum výroby se nenabízí vůbec (radši ruční zadání než špatné datum)
        for (m in mesicRokMezera.findAll(text)) {
            val (mm, rr) = m.destructured
            val ym = runCatching { YearMonth.of(rr.toInt(), mm.toInt()) }.getOrNull() ?: continue
            if (kand.any { YearMonth.from(it.datum) == ym }) continue
            pridej(ym.atEndOfMonth(), m.range.first, 0)
        }
        return kand.filter { it.skore > -10 }.sortedWith(compareByDescending<Kandidat> { it.skore }.thenBy { it.datum })
            .map { it.datum }.distinct()
    }
}
