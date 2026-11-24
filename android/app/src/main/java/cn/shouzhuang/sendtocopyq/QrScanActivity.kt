package cn.shouzhuang.sendtocopyq

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.util.Size
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.TextView
import android.widget.LinearLayout
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import androidx.activity.ComponentActivity
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import org.json.JSONObject
import java.util.EnumMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

class QrScanActivity : ComponentActivity() {
    private lateinit var previewView: PreviewView
    private lateinit var status: TextView
    private lateinit var cameraExecutor: ExecutorService
    private val handled = AtomicBoolean(false)
    private var provider: ProcessCameraProvider? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        cameraExecutor = Executors.newSingleThreadExecutor()
        buildUi()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED) {
            startCamera()
        } else {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA)
        }
    }

    private fun buildUi() {
        val root = FrameLayout(this)
        root.setBackgroundColor(ContextCompat.getColor(this, R.color.page_background))
        previewView = PreviewView(this).apply { scaleType = PreviewView.ScaleType.FILL_CENTER }
        root.addView(previewView, FrameLayout.LayoutParams(-1, -1))
        val shade = View(this).apply { setBackgroundColor(0x330B1220) }
        root.addView(shade, FrameLayout.LayoutParams(-1, -1))
        val top = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(10))
            setBackgroundColor(ContextCompat.getColor(this@QrScanActivity, R.color.card_background))
        }
        top.addView(TextView(this).apply {
            text = "‹"
            textSize = 34f
            setTextColor(ContextCompat.getColor(this@QrScanActivity, R.color.primary_text))
            setOnClickListener { finish() }
        }, LinearLayout.LayoutParams(dp(48), dp(52)))
        top.addView(TextView(this).apply {
            text = "添加电脑"
            textSize = 19f
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            setTextColor(ContextCompat.getColor(this@QrScanActivity, R.color.primary_text))
        })
        root.addView(top, FrameLayout.LayoutParams(-1, -2, Gravity.TOP))

        val scanFrame = View(this).apply {
            background = GradientDrawable().apply {
                setColor(0x12000000)
                setStroke(dp(3), ContextCompat.getColor(this@QrScanActivity, R.color.brand_blue))
                cornerRadius = dp(22).toFloat()
            }
        }
        root.addView(scanFrame, FrameLayout.LayoutParams(dp(276), dp(276), Gravity.CENTER))
        status = TextView(this).apply {
            text = "扫描电脑上的配对二维码\n二维码一次有效，且会自动过期"
            textSize = 15f
            setTextColor(ContextCompat.getColor(this@QrScanActivity, R.color.primary_text))
            setBackground(GradientDrawable().apply {
                setColor(ContextCompat.getColor(this@QrScanActivity, R.color.card_background))
                cornerRadius = dp(18).toFloat()
            })
            gravity = Gravity.CENTER
            setPadding(dp(22), dp(17), dp(22), dp(17))
        }
        ViewCompat.setOnApplyWindowInsetsListener(status) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(dp(18), dp(17), dp(18), bars.bottom + dp(17))
            insets
        }
        root.addView(status, FrameLayout.LayoutParams(-1, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM))
        setContentView(root)
    }

    private fun startCamera() {
        val providerFuture = ProcessCameraProvider.getInstance(this)
        providerFuture.addListener({
            try {
                provider = providerFuture.get()
                val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
                val analysis = ImageAnalysis.Builder()
                    .setTargetResolution(Size(640, 480))
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build()
                analysis.setAnalyzer(cameraExecutor) { image -> decodeFrame(image) }
                provider?.unbindAll()
                provider?.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
            } catch (e: Exception) {
                showStatus("无法启动相机：${e.message ?: "未知错误"}")
            }
        }, ContextCompat.getMainExecutor(this))
    }

    private fun decodeFrame(image: androidx.camera.core.ImageProxy) {
        try {
            if (handled.get()) return
            val plane = image.planes.firstOrNull() ?: return
            val width = image.width
            val height = image.height
            val rowStride = plane.rowStride
            val buffer = plane.buffer.duplicate()
            val luminance = ByteArray(width * height)
            for (row in 0 until height) {
                buffer.position(row * rowStride)
                buffer.get(luminance, row * width, width)
            }
            val source = PlanarYUVLuminanceSource(luminance, width, height, 0, 0, width, height, false)
            val bitmap = BinaryBitmap(HybridBinarizer(source))
            val reader = MultiFormatReader().apply {
                val hints = EnumMap<DecodeHintType, Any>(DecodeHintType::class.java)
                hints[DecodeHintType.POSSIBLE_FORMATS] = listOf(BarcodeFormat.QR_CODE)
                hints[DecodeHintType.TRY_HARDER] = true
                setHints(hints)
            }
            val decoded = try {
                reader.decodeWithState(bitmap)
            } catch (_: Exception) {
                reader.reset()
                if (bitmap.isRotateSupported) reader.decodeWithState(bitmap.rotateCounterClockwise()) else null
            }
            if (decoded != null && handled.compareAndSet(false, true)) {
                runOnUiThread { handlePayload(decoded.text) }
            }
        } catch (_: Exception) {
            // Frames without a QR code are expected; keep scanning without UI noise.
        } finally {
            image.close()
        }
    }

    private fun handlePayload(text: String) {
        val payload = try {
            JSONObject(text).also {
                require(it.optString("type") == "send-to-copyq-pair" && it.optInt("version") == 1) { "不是“发送到电脑”的配对二维码" }
            }
        } catch (e: Exception) {
            handled.set(false)
            showStatus(e.message ?: "配对二维码无效")
            return
        }
        provider?.unbindAll()
        showStatus("正在验证电脑证书并完成配对…")
        Executors.newSingleThreadExecutor().execute {
            try {
                val name = "${android.os.Build.MANUFACTURER} ${android.os.Build.MODEL}".trim()
                val pin = payload.getString("certificateSha256").lowercase()
                val computerId = SecureTransport.computerIdForPayload(payload, pin)
                val previousDeviceId = PeerStore.deviceIdForComputer(this, computerId)
                val paired = SecureTransport.pair(payload, name, ClientIdentity.get(this), previousDeviceId)
                PeerStore.add(this, paired.peer)
                runOnUiThread {
                    setResult(RESULT_OK, Intent()
                        .putExtra("pairingStatus", paired.pairingStatus)
                        .putExtra("computerName", paired.peer.name))
                    finish()
                }
            } catch (e: Exception) {
                runOnUiThread {
                    handled.set(false)
                    val detail = e.message.orEmpty()
                    val message = if (detail.contains("pairing code expired or already used", ignoreCase = true)) {
                        "这个二维码已使用或已过期。请在电脑端重新生成二维码，再扫描。"
                    } else {
                        "配对失败：${detail.ifBlank { "连接错误" }}。请确认电脑接收端已启动后重试。"
                    }
                    showStatus(message)
                    startCamera()
                }
            }
        }
    }

    private fun showStatus(text: String) {
        runOnUiThread { status.text = text }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_CAMERA && grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) startCamera()
        else {
            showStatus("首次配对需要相机权限来扫描电脑二维码。")
            finish()
        }
    }

    override fun onDestroy() {
        provider?.unbindAll()
        if (::cameraExecutor.isInitialized) cameraExecutor.shutdown()
        super.onDestroy()
    }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    companion object {
        private const val REQUEST_CAMERA = 301
    }
}
