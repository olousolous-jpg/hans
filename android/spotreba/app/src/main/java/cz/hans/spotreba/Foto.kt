package cz.hans.spotreba

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Matrix
import android.media.ExifInterface
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.core.content.ContextCompat
import java.io.ByteArrayOutputStream
import java.io.File

/** Fotky celých položek (s číslem napsaným fixem na obalu). V telefonu plná kvalita,
 *  Hansovi zmenšená (delší strana 1280 px). */
object Foto {
    fun soubor(ctx: Context, id: String): File =
        File(File(ctx.filesDir, "fotky").apply { mkdirs() }, "$id.jpg")

    /** Vyfotí do souboru položky; `hotovo(cesta, chyba)`. */
    fun vyfot(ctx: Context, zachyt: Zachyt, id: String, hotovo: (String?, String?) -> Unit) {
        val cap = zachyt.capture ?: return hotovo(null, "Kamera není připravená")
        val f = soubor(ctx, id)
        val tmp = File(f.path + ".tmp")
        cap.takePicture(ImageCapture.OutputFileOptions.Builder(tmp).build(), ContextCompat.getMainExecutor(ctx),
            object : ImageCapture.OnImageSavedCallback {
                override fun onImageSaved(r: ImageCapture.OutputFileResults) {
                    if (f.exists()) f.delete()
                    if (tmp.renameTo(f)) hotovo(f.path, null) else hotovo(null, "Fotku nejde uložit")
                }
                override fun onError(e: ImageCaptureException) { hotovo(null, e.message ?: "Fotka se nepovedla") }
            })
    }

    private fun otoceni(cesta: String): Int = runCatching {
        when (ExifInterface(cesta).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)) {
            ExifInterface.ORIENTATION_ROTATE_90 -> 90
            ExifInterface.ORIENTATION_ROTATE_180 -> 180
            ExifInterface.ORIENTATION_ROTATE_270 -> 270
            else -> 0
        }
    }.getOrDefault(0)

    /** Bitmapa s delší stranou nejvýš `max` px, otočená podle EXIF. */
    fun nacti(cesta: String, max: Int): Bitmap? = runCatching {
        if (cesta.isEmpty() || !File(cesta).exists()) return@runCatching null
        val o = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(cesta, o)
        var s = 1
        while (maxOf(o.outWidth, o.outHeight) / (s * 2) >= max) s *= 2
        val b = BitmapFactory.decodeFile(cesta, BitmapFactory.Options().apply { inSampleSize = s })
            ?: return@runCatching null
        val k = max.toFloat() / maxOf(b.width, b.height)
        val m = Matrix().apply {
            if (k < 1f) postScale(k, k)
            val r = otoceni(cesta)
            if (r != 0) postRotate(r.toFloat())
        }
        if (m.isIdentity) b else Bitmap.createBitmap(b, 0, 0, b.width, b.height, m, true)
    }.getOrNull()

    /** Zmenšený JPEG pro Hanse (~1280 px, kvalita 80). */
    fun proHanse(cesta: String): ByteArray? {
        val b = nacti(cesta, 1280) ?: return null
        return ByteArrayOutputStream().use { o -> b.compress(Bitmap.CompressFormat.JPEG, 80, o); o.toByteArray() }
    }
}
