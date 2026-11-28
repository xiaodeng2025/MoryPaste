package cn.shouzhuang.sendtocopyq

import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.res.ColorStateList
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.IBinder
import android.os.PowerManager
import android.provider.Settings
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.WindowManager
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import com.google.android.material.button.MaterialButton
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

class IncomingFilesService : Service() {
    companion object {
        // Activity and service update these on the main thread.
        internal val homeVisible = MutableStateFlow(false)
        internal var homeHandlingOffer = false
        internal var handlingOffer = false
        private const val CHANNEL = "backgroundFiles"
        private const val NOTIFICATION_ID = 630
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val discovery by lazy { LanDiscovery(this) }
    private val themed by lazy { ContextThemeWrapper(this, R.style.AppTheme) }
    private val windows by lazy { getSystemService(WindowManager::class.java) }
    private var started = false
    private var panel: LinearLayout? = null
    private var visibleOffer: String? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (!Settings.canDrawOverlays(this)) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (!started) {
            started = true
            val notifications = getSystemService(NotificationManager::class.java)
            notifications.createNotificationChannel(NotificationChannel(CHANNEL, "后台接收文件", NotificationManager.IMPORTANCE_LOW))
            val settings = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE)
            startForeground(NOTIFICATION_ID, Notification.Builder(this, CHANNEL)
                .setContentTitle("MoryPaste · 后台接收文件已开启")
                .setSmallIcon(R.drawable.ic_clipboard_notification)
                .setContentIntent(settings)
                .setOngoing(true)
                .build())
            scope.launch { watch() }
            scope.launch {
                homeVisible.collect { visible -> if (visible && visibleOffer != null) closePanel() }
            }
        }
        return START_NOT_STICKY
    }

    private fun canShowPanel() = Settings.canDrawOverlays(this) &&
        getSystemService(PowerManager::class.java).isInteractive &&
        !getSystemService(KeyguardManager::class.java).isKeyguardLocked

    private fun canOffer() = canShowPanel() && !homeVisible.value && !homeHandlingOffer && !handlingOffer

    private suspend fun watch() {
        while (scope.isActive) {
            if (!Settings.canDrawOverlays(this)) {
                stopSelf()
                return
            }
            if (!canShowPanel() || (visibleOffer != null && !canOffer())) closePanel()
            if (canOffer() && (panel == null || visibleOffer != null)) {
                val network = discovery.activeWifiNetwork()
                var offer: IncomingFiles.Offer? = null
                if (network != null) {
                    for (peer in PeerStore.list(this)) {
                        offer = try {
                            withContext(Dispatchers.IO) { IncomingFiles.poll(peer, network) }
                        } catch (canceled: CancellationException) {
                            throw canceled
                        } catch (_: Exception) {
                            null
                        }
                        if (offer != null || !canOffer()) break
                    }
                }
                if (canOffer() && (panel == null || visibleOffer != null) && offer?.id != visibleOffer) {
                    if (offer == null) closePanel() else showOffer(offer)
                }
            }
            delay(1800)
        }
    }

    private fun showOffer(offer: IncomingFiles.Offer) {
        val view = sheet("接收电脑文件")
        view.addView(receivedFileOffer(themed, offer) { accept -> respond(offer, accept) })
        if (showPanel(view)) visibleOffer = offer.id
    }

    private fun respond(offer: IncomingFiles.Offer, accept: Boolean) {
        if (visibleOffer != offer.id || !canOffer()) return
        if (PeerStore.list(this).none { it.computerId == offer.peer.computerId }) {
            closePanel()
            return
        }
        handlingOffer = true
        closePanel()
        if (accept && canShowPanel()) {
            showPanel(sheet("正在接收文件").apply { addView(receivingFilePlaceholder(themed)) })
        }
        scope.launch {
            try {
                val saved = withContext(Dispatchers.IO) {
                    if (accept) IncomingFiles.receive(applicationContext, offer)
                    else {
                        IncomingFiles.respond(offer, "reject")
                        emptyList()
                    }
                }
                closePanel()
                if (accept) {
                    val view = sheet("文件已接收")
                    view.addView(receivedFileResult(themed, offer.files.zip(saved), fromBackground = true) { closePanel() })
                    showPanel(view)
                }
            } catch (canceled: CancellationException) {
                throw canceled
            } catch (_: Exception) {
                val view = sheet(if (accept) "接收失败" else "无法拒绝")
                view.addView(label("请检查连接后从电脑重新发送。", 14f))
                view.addView(button("关闭", false) { closePanel() }, LinearLayout.LayoutParams(-1, dp(48)))
                showPanel(view)
            } finally {
                handlingOffer = false
            }
        }
    }

    private fun sheet(title: String) = incomingFileSheet(themed, title).apply {
        background = GradientDrawable().apply {
            cornerRadii = floatArrayOf(dp(24).toFloat(), dp(24).toFloat(), dp(24).toFloat(), dp(24).toFloat(), 0f, 0f, 0f, 0f)
            setColor(color(R.color.card_background))
        }
        elevation = dp(8).toFloat()
    }

    private fun label(value: String, size: Float) = TextView(themed).apply {
        text = value
        textSize = size
        setTextColor(color(R.color.primary_text))
        setPadding(0, dp(4), 0, dp(4))
    }

    private fun button(title: String, primary: Boolean, click: () -> Unit) = MaterialButton(themed).apply {
        text = title
        cornerRadius = dp(14)
        insetTop = 0
        insetBottom = 0
        backgroundTintList = ColorStateList.valueOf(color(if (primary) R.color.brand_blue else R.color.brand_blue_soft))
        setTextColor(color(if (primary) R.color.on_brand else R.color.brand_blue))
        setOnClickListener { click() }
    }

    private fun showPanel(view: LinearLayout): Boolean {
        closePanel()
        if (!canShowPanel()) return false
        val params = WindowManager.LayoutParams(-1, -2, WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE, PixelFormat.TRANSLUCENT).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            setTitle("MoryPaste")
        }
        return try {
            windows.addView(view, params)
            panel = view
            true
        } catch (_: SecurityException) {
            false
        } catch (_: WindowManager.BadTokenException) {
            false
        }
    }

    private fun closePanel() {
        panel?.let { windows.removeViewImmediate(it) }
        panel = null
        visibleOffer = null
    }

    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
    private fun color(id: Int) = ContextCompat.getColor(themed, id)

    override fun onDestroy() {
        scope.cancel()
        closePanel()
        handlingOffer = false
        super.onDestroy()
    }
}
