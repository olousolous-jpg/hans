package cz.hans.spotreba

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.NetworkType
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.time.LocalDate
import java.time.temporal.ChronoUnit
import java.util.concurrent.TimeUnit

/** Na pozadí á hodinu: zkusí Hanse (doma se synchronizuje, venku tiše nic) a jednou denně
 *  připomene, co brzy končí — místní upozornění funguje i bez Hanse. */
class Prace(ctx: Context, p: WorkerParameters) : CoroutineWorker(ctx, p) {
    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        runCatching { Sit.sync(applicationContext) }
        runCatching { pripomen(applicationContext) }
        Result.success()
    }

    companion object {
        fun naplanuj(ctx: Context) {
            val r = PeriodicWorkRequestBuilder<Prace>(1, TimeUnit.HOURS)
                .setConstraints(Constraints.Builder().setRequiredNetworkType(NetworkType.NOT_REQUIRED).build())
                .build()
            WorkManager.getInstance(ctx).enqueueUniquePeriodicWork("zasoby", ExistingPeriodicWorkPolicy.KEEP, r)
        }

        fun pripomen(ctx: Context) {
            val n = Nastaveni(ctx)
            if (!n.upozorneni) return
            val db = Db(ctx)
            val dnes = LocalDate.now()
            if (java.time.LocalTime.now().hour < 9 || db.stav("pripomenuto") == dnes.toString()) return
            val kon = db.zasoby().mapNotNull { p ->
                runCatching { LocalDate.parse(p.datum) }.getOrNull()?.let { d ->
                    val dni = ChronoUnit.DAYS.between(dnes, d)
                    if (dni <= 1) p to dni else null
                }
            }
            db.nastav("pripomenuto", dnes.toString())
            if (kon.isEmpty()) return
            if (Build.VERSION.SDK_INT >= 33 &&
                ctx.checkSelfPermission(android.Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) return
            val nm = ctx.getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(NotificationChannel("konci", "Brzy končí", NotificationManager.IMPORTANCE_DEFAULT))
            val text = kon.joinToString(", ") { (p, d) ->
                (p.nazev.ifBlank { "výrobek ${p.kod}" }) + " (" + kdy(d) + ")"
            }
            val pi = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE)
            val nf = NotificationCompat.Builder(ctx, "konci")
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle("Brzy končí: ${kon.size}")
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setContentIntent(pi).setAutoCancel(true).build()
            NotificationManagerCompat.from(ctx).notify(1, nf)
        }

        fun kdy(dni: Long): String = when {
            dni < 0 -> "prošlo"
            dni == 0L -> "dnes"
            dni == 1L -> "zítra"
            dni < 5 -> "za $dni dny"
            else -> "za $dni dní"
        }
    }
}
