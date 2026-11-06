package cz.hans.spotreba

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.LocalDate

class DatumTest {
    private val dnes = LocalDate.of(2026, 11, 6)
    private fun prvni(t: String) = Datum.najdi(t, dnes).firstOrNull()

    @Test fun vicko() = assertEquals(LocalDate.of(2026, 11, 9), prvni("9/11/2026 L4613\n10 E 20:18 E8"))
    @Test fun vickoNula() = assertEquals(LocalDate.of(2026, 11, 9), prvni("09/11/2026 L4612\n10 E 20:18 E8"))
    @Test fun pismena() = assertEquals(LocalDate.of(2026, 11, 9), prvni("O9/II/2O26 L46I2"))
    @Test fun unicode() = assertEquals(LocalDate.of(2026, 11, 9), prvni("9\u2044 11\u2044 2026\u00a0L4613"))
    @Test fun mezery() = assertEquals(LocalDate.of(2026, 11, 9), prvni("9\u202f/\u202f11 / 2026"))
    @Test fun tecky() = assertEquals(LocalDate.of(2026, 11, 12), prvni("Spotřebujte do: 12.11.2026"))
    @Test fun mesic() = assertEquals(LocalDate.of(2027, 3, 31), prvni("Min. trvanlivost do 03/2027"))
    @Test fun vyroba() = assertEquals(LocalDate.of(2026, 12, 1), prvni("Vyrobeno 01.11.2026 Spotřebujte do 01.12.2026"))
    @Test fun bezOddelovacu() = assertEquals(LocalDate.of(2028, 10, 27), prvni("19:36\n271028\nL"))
    @Test fun sarzeNe() = assertEquals(LocalDate.of(2028, 10, 27), prvni("19:36\nL 281028\n271028"))
    @Test fun sarzeSama() = assertEquals(null, prvni("L 281025 19:3E"))
    @Test fun casNe() = assertEquals(null, prvni("92:6 19:36"))
    @Test fun mesicTecka() = assertEquals(LocalDate.of(2027, 3, 31), prvni("Min. trvanlivost do konce 03.2027."))
    @Test fun osmMist() = assertEquals(LocalDate.of(2028, 10, 27), prvni("27102028"))
    private fun r(t: String) = Datum.zRuky(t, dnes)
    @Test fun ruka6() = assertEquals(LocalDate.of(2028, 10, 27), r("271028"))
    @Test fun ruka8() = assertEquals(LocalDate.of(2028, 10, 27), r("27102028"))
    @Test fun rukaTecky() = assertEquals(LocalDate.of(2028, 10, 27), r("27.10.28"))
    @Test fun rukaMezery() = assertEquals(LocalDate.of(2028, 10, 27), r("27 10 2028"))
    @Test fun rukaDenMesic() = assertEquals(LocalDate.of(2027, 3, 5), r("5.3"))
    @Test fun ruka4() = assertEquals(LocalDate.of(2026, 12, 24), r("2412"))
    @Test fun rukaMesic() = assertEquals(LocalDate.of(2028, 10, 31), r("10/2028"))
    @Test fun rukaSpatne() = assertEquals(null, r("321028"))
    @Test fun rukaNedopsane() = assertEquals(null, r("271"))
    @Test fun kinder() = assertEquals(LocalDate.of(2027, 3, 1), prvni("2 darab / batoane\n22\n01.032027\nL200Z06---\n75308721"))
    @Test fun oblatky() = assertEquals(LocalDate.of(2027, 3, 31), prvni("3103.2027\nL245221---\ny-50 g O"))
    @Test fun hrosici() = assertEquals(LocalDate.of(2026, 12, 31), prvni("12 2026\n60760358 2\n14:10\n100127970\nL4462685"))
    @Test fun hmotnostNe() = assertEquals(null, prvni("4 kusy 50 g\n150 00 Praha 5"))
}
