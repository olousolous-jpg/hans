package cz.hans.spotreba

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.util.UUID

/** Položka zásob. `datum` = "RRRR-MM-DD" nebo "", `stav` = zasoba / snedeno / vyhozeno / smazano. */
data class Polozka(
    val id: String = UUID.randomUUID().toString(),
    val kod: String = "",
    val nazev: String = "",
    val nazevRucne: Boolean = false,
    val datum: String = "",
    val mnozstvi: String = "",
    val stav: String = "zasoba",
    val zarizeni: String = "",
    val vytvoreno: Long = System.currentTimeMillis(),
    val zmeneno: Long = System.currentTimeMillis(),
    val neodeslano: Boolean = true,
    /** číslo kusu napsané fixem na obal (víc kusů téhož výrobku s jiným datem); 0 = žádné */
    val cislo: Int = 0,
    /** fotka celé položky v telefonu (cesta k JPEG), "" = žádná */
    val foto: String = "",
    /** fotka ještě nebyla odeslána Hansovi */
    val fotoNeodeslana: Boolean = false,
    /** Hans má fotku (poslal ji jiný telefon nebo tento) */
    val maFoto: Boolean = false,
)

/** Místní úložiště — aplikace funguje celá i bez Hanse. */
class Db(ctx: Context) : SQLiteOpenHelper(ctx, "zasoby.db", null, 2) {
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("""CREATE TABLE polozky (id TEXT PRIMARY KEY, kod TEXT, nazev TEXT,
            nazev_rucne INTEGER, datum TEXT, mnozstvi TEXT, stav TEXT, zarizeni TEXT,
            vytvoreno INTEGER, zmeneno INTEGER, neodeslano INTEGER, cislo INTEGER DEFAULT 0,
            foto TEXT DEFAULT '', foto_neodeslana INTEGER DEFAULT 0, ma_foto INTEGER DEFAULT 0)""")
        db.execSQL("CREATE TABLE produkty (kod TEXT PRIMARY KEY, nazev TEXT)")
        db.execSQL("CREATE TABLE stav (klic TEXT PRIMARY KEY, hodnota TEXT)")
    }

    override fun onUpgrade(db: SQLiteDatabase, a: Int, b: Int) {
        if (a < 2) for (s in listOf("cislo INTEGER DEFAULT 0", "foto TEXT DEFAULT ''",
                "foto_neodeslana INTEGER DEFAULT 0", "ma_foto INTEGER DEFAULT 0"))
            runCatching { db.execSQL("ALTER TABLE polozky ADD COLUMN $s") }
    }

    private fun zKurzoru(c: android.database.Cursor) = Polozka(
        id = c.getString(0), kod = c.getString(1) ?: "", nazev = c.getString(2) ?: "",
        nazevRucne = c.getInt(3) == 1, datum = c.getString(4) ?: "", mnozstvi = c.getString(5) ?: "",
        stav = c.getString(6) ?: "zasoba", zarizeni = c.getString(7) ?: "",
        vytvoreno = c.getLong(8), zmeneno = c.getLong(9), neodeslano = c.getInt(10) == 1,
        cislo = c.getInt(11), foto = c.getString(12) ?: "", fotoNeodeslana = c.getInt(13) == 1,
        maFoto = c.getInt(14) == 1,
    )

    private val sloupce = "id, kod, nazev, nazev_rucne, datum, mnozstvi, stav, zarizeni, vytvoreno, zmeneno, neodeslano, " +
        "cislo, foto, foto_neodeslana, ma_foto"

    fun zasoby(): List<Polozka> = readableDatabase.rawQuery(
        "SELECT $sloupce FROM polozky WHERE stav='zasoba' ORDER BY CASE WHEN datum='' THEN '9999' ELSE datum END",
        null).use { c -> buildList { while (c.moveToNext()) add(zKurzoru(c)) } }

    fun neodeslane(): List<Polozka> = readableDatabase.rawQuery(
        "SELECT $sloupce FROM polozky WHERE neodeslano=1", null)
        .use { c -> buildList { while (c.moveToNext()) add(zKurzoru(c)) } }

    fun polozka(id: String): Polozka? = readableDatabase.rawQuery(
        "SELECT $sloupce FROM polozky WHERE id=?", arrayOf(id)).use { c -> if (c.moveToNext()) zKurzoru(c) else null }

    fun uloz(p: Polozka) {
        val v = ContentValues().apply {
            put("id", p.id); put("kod", p.kod); put("nazev", p.nazev)
            put("nazev_rucne", if (p.nazevRucne) 1 else 0); put("datum", p.datum)
            put("mnozstvi", p.mnozstvi); put("stav", p.stav); put("zarizeni", p.zarizeni)
            put("vytvoreno", p.vytvoreno); put("zmeneno", p.zmeneno)
            put("neodeslano", if (p.neodeslano) 1 else 0)
            put("cislo", p.cislo); put("foto", p.foto)
            put("foto_neodeslana", if (p.fotoNeodeslana) 1 else 0); put("ma_foto", if (p.maFoto) 1 else 0)
        }
        writableDatabase.insertWithOnConflict("polozky", null, v, SQLiteDatabase.CONFLICT_REPLACE)
        if (p.nazevRucne && p.kod.isNotEmpty() && p.nazev.isNotEmpty()) ulozProdukt(p.kod, p.nazev)
    }

    /** Nejnižší volné číslo kusu pro výrobek (stejný kód, jinak stejný název) mezi tím, co je v zásobě. */
    fun dalsiCislo(kod: String, nazev: String): Int {
        val (sl, h) = if (kod.isNotEmpty()) "kod" to kod else "nazev" to nazev
        if (h.isEmpty()) return 1
        val obsazeno = readableDatabase.rawQuery(
            "SELECT cislo FROM polozky WHERE stav='zasoba' AND $sl=?", arrayOf(h))
            .use { c -> buildSet { while (c.moveToNext()) add(c.getInt(0)) } }
        var n = 1
        while (n in obsazeno) n++
        return n
    }

    fun fotkyKOdeslani(): List<Polozka> = readableDatabase.rawQuery(
        "SELECT $sloupce FROM polozky WHERE foto_neodeslana=1 AND foto!=''", null)
        .use { c -> buildList { while (c.moveToNext()) add(zKurzoru(c)) } }

    fun fotoOdeslana(id: String) {
        writableDatabase.execSQL("UPDATE polozky SET foto_neodeslana=0, ma_foto=1 WHERE id=?", arrayOf(id))
    }

    fun nastavFoto(id: String, cesta: String) {
        writableDatabase.execSQL("UPDATE polozky SET foto=? WHERE id=?", arrayOf(cesta, id))
    }

    /** Odeslaná položka — příznak smazat jen když se mezitím nezměnila. */
    fun odeslano(id: String, zmeneno: Long) {
        writableDatabase.execSQL("UPDATE polozky SET neodeslano=0 WHERE id=? AND zmeneno=?",
            arrayOf<Any>(id, zmeneno))
    }

    fun produkt(kod: String): String? = readableDatabase.rawQuery(
        "SELECT nazev FROM produkty WHERE kod=?", arrayOf(kod)).use { c -> if (c.moveToNext()) c.getString(0) else null }

    fun ulozProdukt(kod: String, nazev: String) {
        if (kod.isEmpty() || nazev.isEmpty()) return
        writableDatabase.execSQL("INSERT OR REPLACE INTO produkty VALUES (?,?)", arrayOf(kod, nazev))
    }

    /** Doplní název položkám s tímto kódem, které ho nemají (bez příznaku k odeslání). */
    fun doplnNazev(kod: String, nazev: String) {
        ulozProdukt(kod, nazev)
        writableDatabase.execSQL("UPDATE polozky SET nazev=? WHERE kod=? AND (nazev IS NULL OR nazev='')",
            arrayOf(nazev, kod))
    }

    /** Ladění čtení data: posledních 8 přečtených textů, ve kterých se datum nenašlo
     *  (při synchronizaci jdou Hansovi, pak se smažou). */
    fun pridejOcr(text: String) {
        val a = org.json.JSONArray(stav("ocr") ?: "[]")
        for (i in 0 until a.length()) if (a.getString(i) == text) return
        a.put(text)
        while (a.length() > 30) a.remove(0)
        nastav("ocr", a.toString())
    }

    fun stav(klic: String): String? = readableDatabase.rawQuery(
        "SELECT hodnota FROM stav WHERE klic=?", arrayOf(klic)).use { c -> if (c.moveToNext()) c.getString(0) else null }

    fun nastav(klic: String, hodnota: String) {
        writableDatabase.execSQL("INSERT OR REPLACE INTO stav VALUES (?,?)", arrayOf(klic, hodnota))
    }
}
