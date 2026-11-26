package cn.shouzhuang.sendtocopyq

import android.app.Activity
import android.app.AlertDialog
import android.content.ClipboardManager
import android.content.Context
import android.net.Network
import android.os.Bundle
import android.view.View
import android.widget.Toast

class SendClipboardActivity : Activity() {
    companion object {
        const val TARGET_COMPUTER_ID = "clipboardSendTarget"
        private const val TARGET = "computer"
        private const val SENSITIVE = "android.content.extra.IS_SENSITIVE"

        private fun targetId(context: Context): String? =
            context.getSharedPreferences("clipboard_send", MODE_PRIVATE).getString(TARGET, null)
    }

    private val discovery by lazy { LanDiscovery(this) }
    private var started = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(View(this))
        window.decorView.postDelayed({
            if (!started && !isFinishing) finishWith("没能读取剪贴板，请解锁后重试。")
        }, 2500)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || started || isFinishing) return
        started = true
        val peers = PeerStore.list(this)
        if (peers.isEmpty()) {
            finishWith("请先在 MoryPaste 里配对一台电脑。")
            return
        }
        val clip = getSystemService(ClipboardManager::class.java).primaryClip
        if (clip?.description?.extras?.getBoolean(SENSITIVE, false) == true) {
            finishWith("这段剪贴板内容已标记为敏感，未发送。")
            return
        }
        val text = if (clip?.itemCount == 1) clip.getItemAt(0).text?.toString() else null
        if (text.isNullOrEmpty() || '\u0000' in text) {
            finishWith("请先在手机上复制一段可发送的文字。")
            return
        }
        val bytes = text.toByteArray(Charsets.UTF_8)
        if (bytes.size > SecureTransport.MAX_CLIPBOARD_TEXT_BYTES) {
            finishWith("文字超过 32 KB，请分段复制发送。")
            return
        }

        val requestedTarget = intent.getStringExtra(TARGET_COMPUTER_ID)
        val target = peers.firstOrNull { it.computerId == (requestedTarget ?: targetId(this)) }
        when {
            target != null -> send(target, bytes, rememberTarget = requestedTarget != null)
            requestedTarget != null -> finishWith("这台电脑已移除，请重新配对。")
            else -> chooseTarget(peers, bytes)
        }
    }

    private fun chooseTarget(peers: List<PairedComputer>, bytes: ByteArray) {
        var choice = -1
        val labels = peers.map { "${it.name} · ${it.host}" }.toTypedArray()
        val dialog = AlertDialog.Builder(this)
            .setTitle("发送剪贴板到")
            .setSingleChoiceItems(labels, choice) { dialog, position ->
                choice = position
                (dialog as AlertDialog).getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = true
            }
            .setNegativeButton("取消") { _, _ -> finish() }
            .setPositiveButton("发送") { _, _ ->
                val peer = peers[choice]
                send(peer, bytes, rememberTarget = true)
            }
            .setOnCancelListener { finish() }
            .create()
        dialog.show()
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).isEnabled = choice >= 0
    }

    private fun send(peer: PairedComputer, bytes: ByteArray, rememberTarget: Boolean) {
        if (rememberTarget) {
            getSharedPreferences("clipboard_send", MODE_PRIVATE).edit().putString(TARGET, peer.computerId).apply()
        }
        val network = discovery.activeWifiNetwork()
        if (network == null) {
            finishWith("请连接与电脑可互通的 Wi-Fi。")
            return
        }
        checkAndSend(peer, network, bytes, discoverOnFailure = true)
    }

    private fun checkAndSend(peer: PairedComputer, network: Network, bytes: ByteArray, discoverOnFailure: Boolean) {
        Thread({
            val supported = try {
                SecureTransport.supportsClipboardText(peer, network)
            } catch (_: Exception) {
                runOnUiThread {
                    if (!isFinishing && !isDestroyed) {
                        if (discoverOnFailure) discover(peer, network, bytes)
                        else finishWith("找不到「${peer.name}」，请检查电脑接收服务。")
                    }
                }
                return@Thread
            }
            val message = if (!supported) {
                "这台电脑还不支持接收剪贴板文字。"
            } else if (discovery.activeWifiNetwork() != network) {
                "Wi-Fi 已切换，请再点一次发送。"
            } else {
                try {
                    SecureTransport.sendClipboardText(peer, network, bytes)
                    "已发到「${peer.name}」，在电脑上按 Ctrl + V。"
                } catch (error: IllegalStateException) {
                    error.message ?: "电脑未确认接收，请在电脑上检查。"
                } catch (_: Exception) {
                    "电脑未确认接收，请在电脑上检查。"
                }
            }
            runOnUiThread { if (!isFinishing && !isDestroyed) finishWith(message) }
        }, "MoryPaste clipboard sender").start()
    }

    private fun discover(peer: PairedComputer, network: Network, bytes: ByteArray) {
        discovery.start(object : LanDiscovery.Listener {
            override fun onService(computer: DiscoveredComputer) {
                if (computer.computerId != peer.computerId) return
                discovery.stop()
                checkAndSend(peer.copy(host = computer.host, port = computer.port), network, bytes, discoverOnFailure = false)
            }

            override fun onFinished() {
                finishWith("找不到「${peer.name}」，请检查电脑接收服务。")
            }
        })
    }

    private fun finishWith(message: String) {
        Toast.makeText(applicationContext, message, Toast.LENGTH_SHORT).show()
        finish()
    }

    override fun onDestroy() {
        discovery.stop()
        super.onDestroy()
    }
}
