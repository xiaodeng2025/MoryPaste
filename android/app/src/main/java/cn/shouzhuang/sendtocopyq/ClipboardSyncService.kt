package cn.shouzhuang.sendtocopyq

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipData
import android.content.ClipDescription
import android.content.ClipboardManager
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PersistableBundle
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONObject
import java.io.BufferedReader
import java.io.IOException
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import javax.net.ssl.HttpsURLConnection

class ClipboardSyncService : Service() {
    data class ReceptionUi(val computerId: String, val status: String)
    companion object {
        const val COMPUTER_ID = "clipboardComputerId"
        private const val STOP = "stopClipboardSync"
        private const val CHANNEL = "clipboardSync"
        private const val NOTIFICATION_ID = 629
        private val receptionUiState = MutableStateFlow<ReceptionUi?>(null)
        val receptionUi = receptionUiState.asStateFlow()

        @Volatile var selectedComputer: String? = null
            private set
    }

    private val main = Handler(Looper.getMainLooper())
    private val discovery by lazy { LanDiscovery(this) }
    private val notifications by lazy { getSystemService(NotificationManager::class.java) }
    @Volatile private var worker: Thread? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val computerId = intent?.getStringExtra(COMPUTER_ID)
        val peer = computerId?.let { id -> PeerStore.list(this).firstOrNull { it.computerId == id } }
        if (intent?.action == STOP || peer == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (selectedComputer == computerId && worker?.isAlive == true) return START_NOT_STICKY

        worker?.interrupt()
        discovery.stop()
        selectedComputer = computerId
        notifications.createNotificationChannel(NotificationChannel(CHANNEL, "剪贴板同步", NotificationManager.IMPORTANCE_LOW))
        startForeground(NOTIFICATION_ID, notification(peer.name, "正在连接"))
        receptionUiState.value = ReceptionUi(peer.computerId, "正在连接")
        Thread({ receive(peer) }, "MoryPaste clipboard sync").also {
            worker = it
            it.start()
        }
        return START_NOT_STICKY
    }

    private fun notification(computer: String, status: String): Notification {
        val stop = PendingIntent.getService(this, 0, Intent(this, ClipboardSyncService::class.java).setAction(STOP),
            PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("剪贴板同步 · $computer")
            .setContentText(status)
            .setSmallIcon(R.drawable.ic_clipboard_notification)
            .setOnlyAlertOnce(true)
            .setVisibility(Notification.VISIBILITY_PRIVATE)
            .setOngoing(true)
            .addAction(Notification.Action.Builder(null, "停止", stop).build())
            .build()
    }

    private fun receive(original: PairedComputer) {
        val thread = Thread.currentThread()
        var peer = original
        var retrySeconds = 1L
        var status = "正在连接"
        fun update(next: String) {
            if (next != status && worker === thread) {
                status = next
                val state = ReceptionUi(original.computerId, next)
                main.post {
                    if (worker === thread) receptionUiState.value = state
                }
                notifications.notify(NOTIFICATION_ID, notification(peer.name, next))
            }
        }
        try {
            while (worker === thread) {
                val saved = PeerStore.list(this).firstOrNull { it.computerId == peer.computerId }
                if (saved == null) {
                    main.post { if (worker === thread) stopSelf() }
                    return
                }
                val network = discovery.activeWifiNetwork()
                var connection: HttpsURLConnection? = null
                try {
                    if (network == null) throw IOException("Wi-Fi unavailable")
                    connection = SecureTransport.openClipboardSubscription(peer, network)
                    connection.outputStream.close()
                    when (connection.responseCode) {
                        200 -> Unit
                        403 -> throw IOException("需要在电脑上授权这台手机")
                        404 -> throw IOException("这台电脑尚未支持剪贴板同步")
                        else -> throw IOException("无法连接")
                    }
                    connection.inputStream.bufferedReader(Charsets.UTF_8).use { input ->
                        while (worker === thread && discovery.activeWifiNetwork() == network) {
                            val packet = JSONObject(readPacket(input) ?: break)
                            update("同步已连接")
                            retrySeconds = 1
                            if (!packet.isNull("text") && worker === thread && discovery.activeWifiNetwork() == network) {
                                val text = packet.getString("text")
                                val clip = ClipData.newPlainText("MoryPaste", text)
                                if (android.os.Build.VERSION.SDK_INT >= 33) {
                                    clip.description.extras = PersistableBundle().apply {
                                        putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true)
                                    }
                                }
                                getSystemService(ClipboardManager::class.java).setPrimaryClip(clip)
                            }
                        }
                    }
                    update("正在重连")
                } catch (error: Exception) {
                    update(when (error.message) {
                        "需要在电脑上授权这台手机", "这台电脑尚未支持剪贴板同步" -> error.message.orEmpty()
                        else -> "正在重连"
                    })
                } finally {
                    connection?.disconnect()
                }
                if (worker !== thread) return
                if (network != null && status == "正在重连") {
                    peer = discover(peer, thread) ?: saved
                }
                TimeUnit.SECONDS.sleep(retrySeconds)
                retrySeconds = (retrySeconds * 2).coerceAtMost(15)
            }
        } catch (_: InterruptedException) {
            // The service stopped or the user picked another computer.
        }
    }

    private fun discover(peer: PairedComputer, thread: Thread): PairedComputer? {
        val finished = CountDownLatch(1)
        var result: PairedComputer? = null
        main.post {
            if (worker !== thread) {
                finished.countDown()
                return@post
            }
            discovery.start(object : LanDiscovery.Listener {
                override fun onService(computer: DiscoveredComputer) {
                    if (computer.computerId == peer.computerId) {
                        result = peer.copy(host = computer.host, port = computer.port)
                        discovery.stop()
                        finished.countDown()
                    }
                }

                override fun onFinished() { finished.countDown() }
            })
        }
        return if (finished.await(4, TimeUnit.SECONDS)) result else null
    }

    private fun readPacket(input: BufferedReader): String? {
        val packet = StringBuilder()
        while (packet.length < 200_000) {
            val character = input.read()
            if (character == -1) return null
            if (character == 10) return packet.toString()
            packet.append(character.toChar())
        }
        throw IOException("Clipboard packet too large")
    }

    override fun onDestroy() {
        selectedComputer = null
        val oldWorker = worker
        worker = null
        receptionUiState.value = null
        oldWorker?.interrupt()
        discovery.stop()
        super.onDestroy()
    }
}
