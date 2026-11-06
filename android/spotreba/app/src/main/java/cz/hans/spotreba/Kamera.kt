package cz.hans.spotreba

import android.annotation.SuppressLint
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.camera.core.Camera
import androidx.camera.core.FocusMeteringAction
import androidx.camera.core.resolutionselector.AspectRatioStrategy
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Text
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.unit.dp
import com.google.mlkit.vision.text.Text as MlText
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import com.google.mlkit.vision.barcode.BarcodeScanning
import com.google.mlkit.vision.barcode.common.Barcode
import com.google.mlkit.vision.common.InputImage
import com.google.mlkit.vision.text.TextRecognition
import com.google.mlkit.vision.text.latin.TextRecognizerOptions
import java.util.concurrent.Executors

/** Živý náhled; v režimu kódu hlásí první přečtený čárový kód (EAN/UPC), v režimu data
 *  drží `zachyt` pro fotku, kterou pak přečte rozpoznání textu (vše offline v telefonu). */
class Zachyt { var capture: ImageCapture? = null }

/** Rámeček pro datum: podíl šířky a výšky obrazu na výšku (náhled i analýza jsou 16 : 9). */
object Oblast {
    const val X0 = 0.08f; const val X1 = 0.92f
    const val Y0 = 0.38f; const val Y1 = 0.62f

    /** Jen řádky, jejichž střed leží v rámečku — okolní text obalu se ignoruje. */
    fun text(t: MlText, sirka: Int, vyska: Int, x0: Float = X0, x1: Float = X1): String =
        t.textBlocks.flatMap { it.lines }.filter { l ->
            val b = l.boundingBox ?: return@filter false
            val cx = b.exactCenterX() / sirka; val cy = b.exactCenterY() / vyska
            cx in x0..x1 && cy in Y0..Y1
        }.joinToString("\n") { it.text }

    /** Rozměry obrazu po otočení na výšku. */
    fun rozmery(w: Int, h: Int, rot: Int) = if (rot % 180 == 90) h to w else w to h
}

private val pomer169 = ResolutionSelector.Builder()
    .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY).build()

@SuppressLint("UnsafeOptInUsageError")
@Composable
fun Kamera(modifier: Modifier, ctiKod: Boolean, zachyt: Zachyt, onText: ((String) -> Unit)? = null,
           onKod: (String) -> Unit) {
    val ctx = LocalContext.current
    val owner = LocalLifecycleOwner.current
    val vlakno = remember { Executors.newSingleThreadExecutor() }
    val skener = remember {
        BarcodeScanning.getClient(com.google.mlkit.vision.barcode.BarcodeScannerOptions.Builder()
            .setBarcodeFormats(Barcode.FORMAT_EAN_13, Barcode.FORMAT_EAN_8, Barcode.FORMAT_UPC_A,
                Barcode.FORMAT_UPC_E, Barcode.FORMAT_CODE_128).build())
    }
    var hotovo = false
    val nahled = remember { PreviewView(ctx).apply { scaleType = PreviewView.ScaleType.FILL_CENTER } }
    val datum = !ctiKod && onText != null
    var kamera by remember { mutableStateOf<Camera?>(null) }
    var zoom by remember { mutableStateOf(if (datum) 2f else 1f) }
    var svetlo by remember { mutableStateOf(false) }
    DisposableEffect(ctiKod) {
        val fut = ProcessCameraProvider.getInstance(ctx)
        fut.addListener({
            val prov = fut.get()
            val preview = Preview.Builder().apply { if (!ctiKod) setResolutionSelector(pomer169) }.build().also { it.setSurfaceProvider(nahled.surfaceProvider) }
            val cap = ImageCapture.Builder().setCaptureMode(ImageCapture.CAPTURE_MODE_MAXIMIZE_QUALITY)
                .setResolutionSelector(pomer169).build()
            zachyt.capture = cap
            val ana = ImageAnalysis.Builder().setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                .apply {
                    if (!ctiKod) setResolutionSelector(androidx.camera.core.resolutionselector.ResolutionSelector.Builder()
                        .setAspectRatioStrategy(AspectRatioStrategy.RATIO_16_9_FALLBACK_AUTO_STRATEGY)
                        .setResolutionStrategy(androidx.camera.core.resolutionselector.ResolutionStrategy(
                            android.util.Size(1920, 1080),
                            androidx.camera.core.resolutionselector.ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER))
                        .build())
                }.build()
            if (ctiKod) ana.setAnalyzer(vlakno) { img: ImageProxy ->
                val m = img.image
                if (m == null || hotovo) { img.close(); return@setAnalyzer }
                skener.process(InputImage.fromMediaImage(m, img.imageInfo.rotationDegrees))
                    .addOnSuccessListener { k ->
                        val v = k.firstOrNull { !it.rawValue.isNullOrBlank() }?.rawValue
                        if (v != null && !hotovo) { hotovo = true; ContextCompat.getMainExecutor(ctx).execute { onKod(v) } }
                    }
                    .addOnCompleteListener { img.close() }
            }
            // živé čtení textu z náhledu (bodový tisk se z jedné fotky čte špatně;
            // z mnoha snímků se datum trefí spíš) — nejvýš 2× za sekundu
            if (!ctiKod && onText != null) {
                val ocr = TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
                var posledni = 0L
                ana.setAnalyzer(vlakno) { img: ImageProxy ->
                    val m = img.image
                    val ted = System.currentTimeMillis()
                    if (m == null || hotovo || ted - posledni < 500) { img.close(); return@setAnalyzer }
                    posledni = ted
                    val rot = img.imageInfo.rotationDegrees
                    val (w, h) = Oblast.rozmery(m.width, m.height, rot)
                    ocr.process(InputImage.fromMediaImage(m, rot))
                        .addOnSuccessListener { tx ->
                            val v = Oblast.text(tx, w, h)
                            if (!hotovo) ContextCompat.getMainExecutor(ctx).execute { onText(v) }
                        }
                        .addOnCompleteListener { img.close() }
                }
            }
            prov.unbindAll()
            kamera = runCatching {
                if (ctiKod) prov.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, ana)
                else if (onText != null) prov.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, cap, ana)
                else prov.bindToLifecycle(owner, CameraSelector.DEFAULT_BACK_CAMERA, preview, cap)
            }.getOrNull()
            kamera?.cameraControl?.setZoomRatio(zoom)
            if (svetlo) kamera?.cameraControl?.enableTorch(true)
        }, ContextCompat.getMainExecutor(ctx))
        onDispose { runCatching { fut.get().unbindAll() } }
    }
    // ťuknutí do náhledu zaostří na to místo
    @Suppress("ClickableViewAccessibility")
    nahled.setOnTouchListener { v, e ->
        if (e.action == android.view.MotionEvent.ACTION_UP) {
            val bod = nahled.meteringPointFactory.createPoint(e.x, e.y)
            kamera?.cameraControl?.startFocusAndMetering(FocusMeteringAction.Builder(bod).build())
            v.performClick()
        }
        true
    }
    Box(modifier) {
        AndroidView(factory = { nahled }, modifier = Modifier.fillMaxSize())
        if (datum) {
            Canvas(Modifier.fillMaxSize()) {
                // obraz 9 : 16 vyplní náhled (FILL_CENTER) → přepočet rámečku do náhledu
                val s = maxOf(size.width / 9f, size.height / 16f)
                val iw = 9f * s; val ih = 16f * s
                val ox = (size.width - iw) / 2f; val oy = (size.height - ih) / 2f
                drawRect(Color.White, Offset(ox + Oblast.X0 * iw, oy + Oblast.Y0 * ih),
                    Size((Oblast.X1 - Oblast.X0) * iw, (Oblast.Y1 - Oblast.Y0) * ih), style = Stroke(3.dp.toPx()))
            }
        }
        Row(Modifier.align(Alignment.BottomCenter).padding(8.dp), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            // přisvětlovací dioda (při kódu i datu)
            if (kamera?.cameraInfo?.hasFlashUnit() != false) FilterChip(selected = svetlo, onClick = {
                svetlo = !svetlo; kamera?.cameraControl?.enableTorch(svetlo)
            }, label = { Text(if (svetlo) "Světlo zap." else "Světlo") })
            if (datum) {
                val max = kamera?.cameraInfo?.zoomState?.value?.maxZoomRatio ?: 4f
                listOf(1f, 2f, 3f).filter { it <= max }.forEach { z ->
                    FilterChip(selected = zoom == z, onClick = {
                        zoom = z; kamera?.cameraControl?.setZoomRatio(z)
                    }, label = { Text("${z.toInt()}×") })
                }
            }
        }
    }
}

/** Vyfotí a přečte text; výsledek (celý text, nebo chyba) jde do `hotovo`. */
@SuppressLint("UnsafeOptInUsageError")
fun precti(ctx: android.content.Context, zachyt: Zachyt, hotovo: (String?, String?) -> Unit) {
    val cap = zachyt.capture ?: return hotovo(null, "Kamera není připravená")
    cap.takePicture(ContextCompat.getMainExecutor(ctx), object : ImageCapture.OnImageCapturedCallback() {
        override fun onCaptureSuccess(img: ImageProxy) {
            val m = img.image
            if (m == null) { img.close(); hotovo(null, "Fotka se nepovedla"); return }
            val rot = img.imageInfo.rotationDegrees
            val (w, h) = Oblast.rozmery(m.width, m.height, rot)
            TextRecognition.getClient(TextRecognizerOptions.DEFAULT_OPTIONS)
                .process(InputImage.fromMediaImage(m, rot))
                .addOnSuccessListener { t -> hotovo(Oblast.text(t, w, h), null) }
                .addOnFailureListener { e -> hotovo(null, e.message ?: "Text se nepřečetl") }
                .addOnCompleteListener { img.close() }
        }
        override fun onError(e: ImageCaptureException) { hotovo(null, e.message ?: "Fotka se nepovedla") }
    })
}
