package cz.hans.spotreba

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/** Nastavení (adresa Hanse, klíč, jméno telefonu) — výchozí hodnoty z sestavení. */
class Nastaveni(ctx: Context) {
    private val sp = ctx.getSharedPreferences("nastaveni", Context.MODE_PRIVATE)
    var url: String
        get() = sp.getString("url", BuildConfig.HANS_URL) ?: ""
        set(v) = sp.edit().putString("url", v.trim().trimEnd('/')).apply()
    var klic: String
        get() = sp.getString("klic", BuildConfig.HANS_KLIC) ?: ""
        set(v) = sp.edit().putString("klic", v.trim()).apply()
    var jmeno: String
        get() = sp.getString("jmeno", android.os.Build.MODEL) ?: ""
        set(v) = sp.edit().putString("jmeno", v.trim()).apply()
    val zarizeni: String
        get() = sp.getString("zarizeni", null) ?: java.util.UUID.randomUUID().toString().also {
            sp.edit().putString("zarizeni", it).apply()
        }
    var upozorneni: Boolean
        get() = sp.getBoolean("upozorneni", true)
        set(v) = sp.edit().putBoolean("upozorneni", v).apply()
}

object Sit {
    private fun post(url: String, klic: String, telo: JSONObject, timeoutMs: Int = 8000): JSONObject {
        val c = URL(url).openConnection() as HttpURLConnection
        try {
            c.requestMethod = "POST"
            c.connectTimeout = timeoutMs; c.readTimeout = 30000
            c.doOutput = true
            c.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            c.setRequestProperty("X-Hans-Klic", klic)
            c.outputStream.use { it.write(telo.toString().toByteArray(Charsets.UTF_8)) }
            val kod = c.responseCode
            val text = (if (kod in 200..299) c.inputStream else c.errorStream)?.bufferedReader()?.readText() ?: ""
            if (kod !in 200..299) throw RuntimeException("Hans odpověděl $kod: ${text.take(80)}")
            return JSONObject(text)
        } finally {
            c.disconnect()
        }
    }

    /** Synchronizace s Hansem. Vrací text pro uživatele. Bez sítě vyhodí výjimku. */
    fun sync(ctx: Context): String {
        val n = Nastaveni(ctx)
        if (n.url.isEmpty() || n.klic.isEmpty()) throw RuntimeException("V nastavení chybí adresa Hanse nebo klíč")
        val db = Db(ctx)
        val od = db.stav("sync_od")?.toLongOrNull() ?: 0L
        val ven = db.neodeslane()
        val pole = JSONArray()
        for (p in ven) pole.put(JSONObject().apply {
            put("id", p.id); put("kod", p.kod); put("nazev", p.nazev); put("nazev_rucne", p.nazevRucne)
            put("datum", p.datum); put("mnozstvi", p.mnozstvi); put("stav", p.stav)
            put("vytvoreno", p.vytvoreno); put("zmeneno", p.zmeneno)
        })
        val odp = post("${n.url}/api/spotreba/sync", n.klic, JSONObject().apply {
            put("zarizeni", n.zarizeni); put("jmeno", n.jmeno); put("od", od); put("polozky", pole)
            put("ladeni", org.json.JSONArray(db.stav("ocr") ?: "[]"))
        })
        db.nastav("ocr", "[]")
        for (p in ven) db.odeslano(p.id, p.zmeneno)
        val prisle = odp.optJSONArray("polozky") ?: JSONArray()
        var zmen = 0
        for (i in 0 until prisle.length()) {
            val o = prisle.getJSONObject(i)
            val id = o.getString("id")
            val mist = db.polozka(id)
            val zm = o.optLong("zmeneno")
            if (mist != null && mist.zmeneno >= zm) continue
            db.uloz(Polozka(id = id, kod = o.optString("kod"), nazev = o.optString("nazev"),
                datum = o.optString("datum"), mnozstvi = o.optString("mnozstvi"),
                stav = o.optString("stav", "zasoba"), zarizeni = o.optString("zarizeni"),
                vytvoreno = o.optLong("vytvoreno"), zmeneno = zm, neodeslano = false))
            zmen++
        }
        val prod = odp.optJSONObject("produkty")
        prod?.keys()?.forEach { k -> db.doplnNazev(k, prod.getString(k)) }
        db.nastav("sync_od", odp.optLong("cas").toString())
        db.nastav("sync_kdy", System.currentTimeMillis().toString())
        val off = runCatching { Off.stahni(ctx, n) }.getOrElse {
            "tabulku výrobků se nepodařilo stáhnout (${it.javaClass.simpleName}: ${it.message?.take(60)})"
        }
        val cas = java.time.LocalTime.now().format(java.time.format.DateTimeFormatter.ofPattern("H:mm"))
        return "Synchronizováno s Hansem v $cas: odesláno ${ven.size}, přijato $zmen; $off."
    }

    /**
     * Název výrobku z Open Food Facts (jen čtení, když je internet). Data © Open Food Facts
     * (ODbL), https://openfoodfacts.org — uvedeno v aplikaci.
     */
    fun nazevOff(kod: String): String? = runCatching {
        val c = URL("https://world.openfoodfacts.org/api/v2/product/$kod.json?fields=product_name_cs,product_name,brands")
            .openConnection() as HttpURLConnection
        c.connectTimeout = 6000; c.readTimeout = 8000
        c.setRequestProperty("User-Agent", "Hans-Spotreba-Android/1.0 (osobni aplikace)")
        try {
            if (c.responseCode != 200) return@runCatching null
            val p = JSONObject(c.inputStream.bufferedReader().readText()).optJSONObject("product") ?: return@runCatching null
            val n = p.optString("product_name_cs").ifBlank { p.optString("product_name") }.trim()
            val b = p.optString("brands").split(",").first().trim()
            when {
                n.isEmpty() -> null
                b.isEmpty() || n.contains(b, ignoreCase = true) -> n
                else -> "$n ($b)"
            }
        } finally {
            c.disconnect()
        }
    }.getOrNull()
}

/**
 * Offline tabulka výrobků z Česka a Slovenska (Open Food Facts, ODbL), kterou sestavuje
 * Hans; telefon si ji stáhne při synchronizaci a názvy hledá bez internetu.
 */
object Off {
    private fun soubor(ctx: Context) = ctx.getDatabasePath("off.db")

    /** Stáhne novou verzi, když ji Hans má. Vrátí větu o výsledku (chyba = výjimka). */
    fun stahni(ctx: Context, n: Nastaveni): String {
        val db = Db(ctx)
        val mam = if (soubor(ctx).exists()) db.stav("off_verze") ?: "" else ""
        val c = URL("${n.url}/api/spotreba/off?verze=" + java.net.URLEncoder.encode(mam, "UTF-8"))
            .openConnection() as HttpURLConnection
        c.connectTimeout = 8000; c.readTimeout = 60000
        c.setRequestProperty("X-Hans-Klic", n.klic)
        try {
            val kod = c.responseCode
            if (kod == 404) return "tabulka výrobků na Hansovi zatím není"
            if (kod != 200) throw RuntimeException("Hans odpověděl $kod")
            val verze = c.getHeaderField("X-Verze") ?: c.getHeaderField("x-verze")
            if (verze == null) {                                       // {"aktualni": true}
                c.inputStream.close()
                return "tabulka výrobků je aktuální (${db.stav("off_verze")}, ${pocet(ctx)} výrobků)"
            }
            val cil = soubor(ctx)
            cil.parentFile?.mkdirs()
            val nova = java.io.File(cil.path + ".nova")
            java.util.zip.GZIPInputStream(c.inputStream).use { vstup ->
                nova.outputStream().use { vstup.copyTo(it) }
            }
            if (cil.exists()) cil.delete()
            if (!nova.renameTo(cil)) { nova.delete(); throw RuntimeException("nejde uložit soubor") }
            db.nastav("off_verze", verze)
            return "tabulka výrobků stažena ($verze, ${pocet(ctx)} výrobků)"
        } finally {
            c.disconnect()
        }
    }

    fun pocet(ctx: Context): Int = runCatching {
        android.database.sqlite.SQLiteDatabase.openDatabase(soubor(ctx).path, null,
            android.database.sqlite.SQLiteDatabase.OPEN_READONLY).use { d ->
            d.rawQuery("SELECT count(*) FROM p", null).use { k -> if (k.moveToFirst()) k.getInt(0) else 0 }
        }
    }.getOrDefault(0)

    fun nazev(ctx: Context, kod: String): String? = runCatching {
        val f = soubor(ctx)
        if (!f.exists() || kod.isEmpty()) return@runCatching null
        android.database.sqlite.SQLiteDatabase.openDatabase(f.path, null,
            android.database.sqlite.SQLiteDatabase.OPEN_READONLY).use { d ->
            d.rawQuery("SELECT nazev FROM p WHERE kod=?", arrayOf(kod)).use { k ->
                if (k.moveToFirst()) k.getString(0)?.takeIf { it.isNotBlank() } else null
            }
        }
    }.getOrNull()
}
