package cz.hans.spotreba

import android.Manifest
import android.app.DatePickerDialog
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.time.LocalDate
import java.time.format.DateTimeFormatter
import java.time.temporal.ChronoUnit

private val CZ = DateTimeFormatter.ofPattern("d. M. yyyy")

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Prace.naplanuj(this)
        setContent { MaterialTheme(colorScheme = lightColorScheme(primary = Color(0xFF2E7D5B))) { Aplikace() } }
    }
}

private enum class Obr { PREHLED, KOD, FORMULAR, FOTO_DATA, NASTAVENI }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun Aplikace() {
    val ctx = LocalContext.current
    val db = remember { Db(ctx) }
    val nast = remember { Nastaveni(ctx) }
    val scope = rememberCoroutineScope()
    var obr by remember { mutableStateOf(Obr.PREHLED) }
    var seznam by remember { mutableStateOf(db.zasoby()) }
    var zprava by remember { mutableStateOf("") }
    // rozpracovaná položka
    var upravovana by remember { mutableStateOf<Polozka?>(null) }
    var nazev by remember { mutableStateOf("") }
    var nazevRucne by remember { mutableStateOf(false) }
    var datum by remember { mutableStateOf<LocalDate?>(null) }
    var kandidati by remember { mutableStateOf(listOf<LocalDate>()) }
    var mnozstvi by remember { mutableStateOf("") }
    var vybrana by remember { mutableStateOf<Polozka?>(null) }
    val zachyt = remember { Zachyt() }

    fun obnov() { seznam = db.zasoby() }
    fun synchronizuj(tise: Boolean) {
        scope.launch {
            val r = withContext(Dispatchers.IO) { runCatching { Sit.sync(ctx) } }
            if (!tise || r.isSuccess) zprava = r.getOrElse { "Hans není dostupný (${it.message?.take(60)}) — data zůstávají v telefonu." }
            obnov()
        }
    }
    LaunchedEffect(Unit) { synchronizuj(true) }

    val kameraOk = remember { mutableStateOf(ctx.checkSelfPermission(Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) }
    val pravo = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { kameraOk.value = it }
    val pravoNotif = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {}
    LaunchedEffect(Unit) {
        if (Build.VERSION.SDK_INT >= 33 && ctx.checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED)
            pravoNotif.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    fun novy(kod: String) {
        upravovana = Polozka(kod = kod, zarizeni = nast.zarizeni)
        nazev = db.produkt(kod) ?: ""; nazevRucne = false
        datum = null; kandidati = emptyList(); mnozstvi = ""
        obr = Obr.FORMULAR
        if (nazev.isEmpty() && kod.isNotEmpty()) scope.launch {
            val n = withContext(Dispatchers.IO) { Off.nazev(ctx, kod) ?: Sit.nazevOff(kod) }
            if (n != null && nazev.isEmpty()) { nazev = n; db.ulozProdukt(kod, n) }
        }
    }

    BackHandler(enabled = obr != Obr.PREHLED) {
        obr = if (obr == Obr.FOTO_DATA) Obr.FORMULAR else Obr.PREHLED
    }

    Scaffold(
        topBar = {
            TopAppBar(title = { Text(when (obr) {
                Obr.PREHLED -> "Zásoby"; Obr.KOD -> "Načti čárový kód"; Obr.FORMULAR -> "Nová položka"
                Obr.FOTO_DATA -> "Vyfoť datum spotřeby"; Obr.NASTAVENI -> "Nastavení" }) },
                actions = {
                    if (obr == Obr.PREHLED) {
                        TextButton(onClick = { synchronizuj(false) }) { Text("Hans") }
                        TextButton(onClick = { obr = Obr.NASTAVENI }) { Text("Nastavení") }
                    }
                })
        },
        floatingActionButton = {
            if (obr == Obr.PREHLED) ExtendedFloatingActionButton(onClick = {
                if (!kameraOk.value) pravo.launch(Manifest.permission.CAMERA)
                obr = Obr.KOD
            }) { Text("+ Přidat") }
        }
    ) { pad ->
        Box(Modifier.padding(pad).fillMaxSize()) {
            when (obr) {
                Obr.PREHLED -> Prehled(seznam, zprava) { vybrana = it }
                Obr.KOD -> Column(Modifier.fillMaxSize()) {
                    if (kameraOk.value) Kamera(Modifier.weight(1f).fillMaxWidth(), true, zachyt) { novy(it) }
                    else Text("Aplikace potřebuje přístup ke kameře.", Modifier.padding(16.dp).weight(1f))
                    Row(Modifier.padding(12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(onClick = { novy("") }) { Text("Bez čárového kódu") }
                        OutlinedButton(onClick = { obr = Obr.PREHLED }) { Text("Zpět") }
                    }
                }
                Obr.FORMULAR -> Column(Modifier.padding(16.dp).verticalScroll(rememberScrollState()),
                    verticalArrangement = Arrangement.spacedBy(10.dp)) {
                    val p = upravovana
                    if (p != null && p.kod.isNotEmpty()) Text("Kód ${p.kod}", color = Color.Gray, fontSize = 13.sp)
                    OutlinedTextField(nazev, { nazev = it; nazevRucne = true }, label = { Text("Název") },
                        modifier = Modifier.fillMaxWidth(), singleLine = true)
                    Text("Datum spotřeby: " + (datum?.format(CZ) ?: "—"), fontWeight = FontWeight.Bold)
                    if (kandidati.size > 1) Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        kandidati.take(3).forEach { d ->
                            FilterChip(selected = d == datum, onClick = { datum = d }, label = { Text(d.format(CZ)) })
                        }
                    }
                    // ruční zadání číslicemi: „271028“, „27.10.28“… — rychlejší než kalendář
                    var rucne by remember { mutableStateOf("") }
                    val zRuky = Datum.zRuky(rucne)
                    OutlinedTextField(rucne, { v ->
                        rucne = v
                        Datum.zRuky(v)?.let { datum = it; kandidati = emptyList() }
                    }, label = { Text("Datum ručně (např. 271028 nebo 27.10.28)") },
                        isError = rucne.length >= 4 && zRuky == null,
                        supportingText = { if (rucne.length >= 4 && zRuky == null) Text("Tohle jako datum nepřečtu") },
                        keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(
                            keyboardType = androidx.compose.ui.text.input.KeyboardType.Number),
                        modifier = Modifier.fillMaxWidth(), singleLine = true)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(onClick = {
                            if (!kameraOk.value) pravo.launch(Manifest.permission.CAMERA)
                            obr = Obr.FOTO_DATA
                        }) { Text("Vyfotit datum") }
                        OutlinedButton(onClick = {
                            val d = datum ?: LocalDate.now().plusDays(7)
                            DatePickerDialog(ctx, { _, r, m, den -> datum = LocalDate.of(r, m + 1, den) },
                                d.year, d.monthValue - 1, d.dayOfMonth).show()
                        }) { Text("Kalendář") }
                    }
                    OutlinedTextField(mnozstvi, { mnozstvi = it }, label = { Text("Množství (nepovinné)") },
                        modifier = Modifier.fillMaxWidth(), singleLine = true)
                    if (zprava.isNotEmpty() && obr == Obr.FORMULAR) Text(zprava, color = Color.Gray, fontSize = 13.sp)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(enabled = datum != null && (nazev.isNotBlank() || (p?.kod ?: "").isNotEmpty()), onClick = {
                            val ted = System.currentTimeMillis()
                            db.uloz((p ?: Polozka(zarizeni = nast.zarizeni)).copy(
                                nazev = nazev.trim(), nazevRucne = nazevRucne, datum = datum.toString(),
                                mnozstvi = mnozstvi.trim(), zmeneno = ted, neodeslano = true))
                            zprava = ""; obnov(); obr = Obr.PREHLED; synchronizuj(true)
                        }) { Text("Uložit") }
                        OutlinedButton(onClick = { zprava = ""; obr = Obr.PREHLED }) { Text("Zrušit") }
                    }
                }
                Obr.FOTO_DATA -> Column(Modifier.fillMaxSize()) {
                    var cte by remember { mutableStateOf(false) }
                    var precteno by remember { mutableStateOf("") }
                    // živě: datum se nabídne, až vyjde stejně aspoň ve dvou snímcích
                    val hlasy = remember { mutableMapOf<java.time.LocalDate, Int>() }
                    // živě: první snímek, ve kterém je datum, ho rovnou nabídne
                    if (kameraOk.value) Kamera(Modifier.weight(1f).fillMaxWidth(), false, zachyt, onText = { tx ->
                        if (tx.isNotBlank()) precteno = tx
                        val k = Datum.najdi(tx)
                        if (tx.any { it.isDigit() }) db.pridejOcr("živě: " + tx)
                        k.forEach { hlasy[it] = (hlasy[it] ?: 0) + 1 }
                        val jiste = hlasy.filter { it.value >= 2 }.toList()
                            .sortedByDescending { it.second }.map { it.first }
                        if (jiste.isNotEmpty() && obr == Obr.FOTO_DATA) {
                            kandidati = (jiste + k).distinct(); datum = jiste.first(); zprava = ""; obr = Obr.FORMULAR
                        }
                    }) {}
                    else Text("Aplikace potřebuje přístup ke kameře.", Modifier.padding(16.dp).weight(1f))
                    Text("Datum dej do bílého rámečku (čte se jen text v něm) — přečte se samo, nebo stiskni Vyfotit. Ťuknutím zaostříš.",
                        Modifier.padding(horizontal = 12.dp), fontSize = 13.sp)
                    if (precteno.isNotBlank()) Text("Vidím: " + precteno.replace('\n', ' ').take(80),
                        Modifier.padding(horizontal = 12.dp), fontSize = 12.sp, color = Color.Gray)
                    Row(Modifier.padding(12.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        Button(enabled = !cte, onClick = {
                            cte = true
                            precti(ctx, zachyt) { text, chyba ->
                                cte = false
                                val k = text?.let { Datum.najdi(it) } ?: emptyList()
                                if (!text.isNullOrBlank()) db.pridejOcr("foto: " + text)
                                if (k.isNotEmpty()) { kandidati = k; datum = k.first(); zprava = "" }
                                else zprava = chyba ?: ("Datum se z fotky přečíst nepodařilo — zkus znovu, nebo ho zadej ručně." +
                                    (if (!text.isNullOrBlank()) " Přečetl jsem: „" + text.replace('\n', ' ').take(100) + "“" else " Na fotce jsem nenašel žádný text."))
                                obr = Obr.FORMULAR
                            }
                        }) { Text(if (cte) "Čtu…" else "Vyfotit") }
                        OutlinedButton(onClick = { obr = Obr.FORMULAR }) { Text("Zpět") }
                    }
                }
                Obr.NASTAVENI -> NastaveniObr(nast) { synchronizuj(false); obr = Obr.PREHLED }
            }
        }
    }

    vybrana?.let { p ->
        AlertDialog(onDismissRequest = { vybrana = null },
            title = { Text(p.nazev.ifBlank { "Výrobek ${p.kod}" }) },
            text = { Text("Spotřebovat do " + (runCatching { LocalDate.parse(p.datum).format(CZ) }.getOrDefault("—"))) },
            confirmButton = {
                Column {
                    fun zmen(stav: String) {
                        db.uloz(p.copy(stav = stav, zmeneno = System.currentTimeMillis(), neodeslano = true))
                        vybrana = null; obnov(); synchronizuj(true)
                    }
                    TextButton(onClick = { zmen("snedeno") }) { Text("Snědeno") }
                    TextButton(onClick = { zmen("vyhozeno") }) { Text("Vyhozeno") }
                    TextButton(onClick = {
                        val d = runCatching { LocalDate.parse(p.datum) }.getOrDefault(LocalDate.now())
                        DatePickerDialog(ctx, { _, r, m, den ->
                            db.uloz(p.copy(datum = LocalDate.of(r, m + 1, den).toString(),
                                zmeneno = System.currentTimeMillis(), neodeslano = true))
                            vybrana = null; obnov(); synchronizuj(true)
                        }, d.year, d.monthValue - 1, d.dayOfMonth).show()
                    }) { Text("Změnit datum") }
                    TextButton(onClick = { zmen("smazano") }) { Text("Smazat (omyl)") }
                }
            },
            dismissButton = { TextButton(onClick = { vybrana = null }) { Text("Zavřít") } })
    }
}

@Composable
private fun Prehled(seznam: List<Polozka>, zprava: String, vyber: (Polozka) -> Unit) {
    val dnes = LocalDate.now()
    Column(Modifier.fillMaxSize()) {
        if (zprava.isNotEmpty()) Text(zprava, Modifier.padding(12.dp), fontSize = 13.sp, color = Color.Gray)
        if (seznam.isEmpty()) Text("Zatím žádné zásoby. Přidej první tlačítkem dole.", Modifier.padding(16.dp))
        LazyColumn(contentPadding = PaddingValues(12.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(seznam, key = { it.id }) { p ->
                val d = runCatching { LocalDate.parse(p.datum) }.getOrNull()
                val dni = d?.let { ChronoUnit.DAYS.between(dnes, it) }
                val barva = when {
                    dni == null -> Color(0xFFEEEEEE)
                    dni <= 0 -> Color(0xFFFFCDD2)
                    dni <= 2 -> Color(0xFFFFE0B2)
                    dni <= 5 -> Color(0xFFFFF9C4)
                    else -> Color(0xFFE8F5E9)
                }
                Row(Modifier.fillMaxWidth().background(barva, RoundedCornerShape(10.dp)).clickable { vyber(p) }
                    .padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text(p.nazev.ifBlank { "Výrobek ${p.kod}" }, fontWeight = FontWeight.SemiBold)
                        if (p.mnozstvi.isNotEmpty()) Text(p.mnozstvi, fontSize = 13.sp, color = Color.DarkGray)
                    }
                    Column(horizontalAlignment = Alignment.End) {
                        Text(dni?.let { Prace.kdy(it) } ?: "bez data", fontWeight = FontWeight.Bold)
                        Text(d?.format(CZ) ?: "", fontSize = 13.sp, color = Color.DarkGray)
                    }
                }
            }
        }
    }
}

@Composable
private fun NastaveniObr(n: Nastaveni, hotovo: () -> Unit) {
    var url by remember { mutableStateOf(n.url) }
    var klic by remember { mutableStateOf(n.klic) }
    var jmeno by remember { mutableStateOf(n.jmeno) }
    var upoz by remember { mutableStateOf(n.upozorneni) }
    Column(Modifier.padding(16.dp).verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        OutlinedTextField(url, { url = it }, label = { Text("Adresa Hanse (např. http://…:7860)") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(klic, { klic = it }, label = { Text("Klíč pro synchronizaci") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        OutlinedTextField(jmeno, { jmeno = it }, label = { Text("Jméno telefonu") }, modifier = Modifier.fillMaxWidth(), singleLine = true)
        Row(verticalAlignment = Alignment.CenterVertically) {
            Switch(upoz, { upoz = it }); Spacer(Modifier.width(8.dp)); Text("Upozornit v telefonu, co dnes a zítra končí")
        }
        Button(onClick = { n.url = url; n.klic = klic; n.jmeno = jmeno; n.upozorneni = upoz; hotovo() }) { Text("Uložit a synchronizovat") }
        Text("Názvy výrobků: Open Food Facts (openfoodfacts.org), licence ODbL.", fontSize = 12.sp, color = Color.Gray)
    }
}
