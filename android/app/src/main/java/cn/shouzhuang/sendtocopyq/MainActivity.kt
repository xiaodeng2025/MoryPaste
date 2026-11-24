package cn.shouzhuang.sendtocopyq

import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import android.widget.FrameLayout
import android.widget.ImageView
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import android.content.res.ColorStateList
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import androidx.core.content.pm.PackageInfoCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.doOnLayout
import com.google.android.material.button.MaterialButton
import com.google.android.material.card.MaterialCardView
import androidx.lifecycle.Lifecycle
import java.io.File
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

open class MainActivity : ComponentActivity() {
    protected open val shareWindow = false
    private val worker = Executors.newSingleThreadExecutor()
    private val routeProbes = Executors.newFixedThreadPool(4)
    private val mainHandler = Handler(Looper.getMainLooper())
    private data class VerifiedTarget(val peer: PairedComputer, val supportsFiles: Boolean)
    private class RouteSearch(val peers: List<PairedComputer>, val network: Long?) {
        val attempted = mutableSetOf<Triple<String, String, Int>>()
        val verified = mutableMapOf<String, VerifiedTarget>()
        val probes = mutableListOf<Future<*>>()
        var pending = 0
        var discoveryFinished = false
        var discoveryReliable = true
        var settled = false
        lateinit var deadline: Runnable
    }
    private var routeSearch: RouteSearch? = null
    private var resumeTargetSearch = false
    private var checkingAndroidUpdate = false
    private var updatePermissionResult: (() -> Unit)? = null
    private val updateSettingsLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        val callback = updatePermissionResult
        updatePermissionResult = null
        callback?.invoke()
    }
    private val diagnosticsExportLauncher = registerForActivityResult(ActivityResultContracts.CreateDocument("application/zip")) { uri ->
        if (uri != null) {
            try {
                contentResolver.openOutputStream(uri, "wt")?.use { it.write(DiagnosticLogger.exportZip(this)) }
                    ?: throw IllegalStateException("无法创建导出文件")
                Toast.makeText(this, "诊断日志已导出", Toast.LENGTH_SHORT).show()
            } catch (e: Exception) {
                Toast.makeText(this, "导出失败：${e.message ?: "请重试"}", Toast.LENGTH_LONG).show()
            }
        }
    }
    private val pairLauncher = registerForActivityResult(ActivityResultContracts.StartActivityForResult()) { result ->
        if (result.resultCode == RESULT_OK) {
            if (pendingUris.isNotEmpty()) sendNext()
            else {
                showHome()
                val computerName = result.data?.getStringExtra("computerName")?.takeIf { it.isNotBlank() }
                val status = result.data?.getStringExtra("pairingStatus")
                val message = when (status) {
                    "created" -> computerName?.let { "已添加电脑：$it" }
                    "updated" -> computerName?.let { "$it 的配对信息已更新" }
                    else -> "配对成功"
                }
                message?.let { Toast.makeText(this, it, Toast.LENGTH_SHORT).show() }
            }
        } else if (pendingUris.isEmpty()) {
            showHome()
        }
    }

    private lateinit var rootColumn: LinearLayout
    private lateinit var statusView: TextView
    private lateinit var pairButton: MaterialButton
    private lateinit var retryButton: MaterialButton
    private lateinit var statusIcon: TextView
    private lateinit var shareTarget: TextView
    private lateinit var shareStatusCard: LinearLayout
    private lateinit var targetButtons: LinearLayout
    private lateinit var targetSearchHint: TextView
    private var pendingUris: List<Uri> = emptyList()
    private var pendingBytes: ByteArray? = null
    private var batchPeer: PairedComputer? = null
    private var fileReceiveSupported: Boolean? = null
    private var receiveResult: SecureTransport.ReceiveResult? = null
    private var currentIndex = 0
    private var busy = false
    private var shareMode = false
    private var currentTaskId = ""
    private var taskStartedAt = 0L
    private var lastSentCount = 0
    private var activeTargetName = ""
    private var deviceList: LinearLayout? = null
    private var discoveryStatus: TextView? = null
    private val discovery by lazy { LanDiscovery(this) }
    private val computerStatus = ConcurrentHashMap<String, String>()
    private val probingComputers = ConcurrentHashMap.newKeySet<String>()
    private var scanGeneration = 0
    private var scanRunning = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        title = getString(R.string.app_name)
        shareMode = isFileShare(intent)
        if (shareWindow && !shareMode) {
            finish()
            return
        }
        if (shareWindow) buildShareShell() else buildShell()
        try {
            ClientIdentity.get(this)
        } catch (e: Exception) {
            Toast.makeText(this, "手机身份初始化失败：${e.message ?: "请重启应用后重试"}", Toast.LENGTH_LONG).show()
        }
        if (shareMode) handleIncomingIntent(intent) else showHome()
    }

    override fun onResume() {
        super.onResume()
        if (!shareMode) refreshDiscovery()
        else if (resumeTargetSearch) {
            resumeTargetSearch = false
            sendNext()
        }
    }

    override fun onPause() {
        resumeTargetSearch = routeSearch != null
        cancelRouteSearch()
        stopDiscovery()
        super.onPause()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (busy) {
            Toast.makeText(this, "当前发送尚未完成，请稍后再分享。", Toast.LENGTH_SHORT).show()
            return
        }
        if (shareWindow && !isFileShare(intent)) return
        setIntent(intent)
        shareMode = isFileShare(intent)
        if (shareMode) handleIncomingIntent(intent) else showHome()
    }

    private fun buildShell() {
        val scroll = ScrollView(this)
        scroll.isFillViewport = true
        rootColumn = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(22), dp(16), dp(22), dp(24))
        }
        ViewCompat.setOnApplyWindowInsetsListener(rootColumn) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(dp(24), bars.top + dp(20), dp(24), bars.bottom + dp(28))
            insets
        }
        scroll.addView(rootColumn)
        scroll.setBackgroundColor(color(R.color.page_background))
        setContentView(scroll)
    }

    private fun buildShareShell() {
        WindowCompat.setDecorFitsSystemWindows(window, false)
        val overlay = FrameLayout(this).apply {
            setOnClickListener { if (!busy || routeSearch != null) finish() }
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        val panel = ScrollView(this).apply {
            background = rounded(R.color.card_background, 24)
            clipToOutline = true
            isClickable = true
        }
        rootColumn = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(18), dp(20), dp(24))
        }
        panel.addView(rootColumn)
        overlay.addView(panel, FrameLayout.LayoutParams(-1, -2, Gravity.BOTTOM))
        ViewCompat.setOnApplyWindowInsetsListener(overlay) { view, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout())
            view.setPadding(bars.left + dp(10), bars.top + dp(12), bars.right + dp(10), bars.bottom + dp(8))
            insets
        }
        setContentView(overlay)
        panel.doOnLayout {
            panel.translationY = (overlay.height - panel.top).toFloat()
            panel.animate().translationY(0f).setDuration(240).start()
        }
    }

    private fun showHome() {
        rootColumn.removeAllViews()
        val peers = PeerStore.list(this)
        rootColumn.addView(brandHeader())
        TextView(this).apply {
            text = "从手机分享图片，直接发送到电脑。"
            textSize = 15f
            setTextColor(color(R.color.secondary_text))
            setPadding(dp(2), dp(14), 0, dp(22))
        }.also { rootColumn.addView(it) }
        rootColumn.addView(label(if (peers.isEmpty()) "添加一台电脑" else "已配对电脑", 17f, true).apply {
            setPadding(dp(2), dp(6), 0, dp(8))
        }, verticalParams(dp(10)))
        deviceList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        rootColumn.addView(deviceList, verticalParams(0))
        if (peers.isNotEmpty()) {
            val scanRow = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(2), dp(6), 0, dp(2))
            }
            discoveryStatus = label("正在查找局域网电脑…", 12f, false, R.color.secondary_text)
            scanRow.addView(discoveryStatus, LinearLayout.LayoutParams(0, -2, 1f))
            val refresh = secondaryButton("刷新") { refreshDiscovery() }
            scanRow.addView(refresh, LinearLayout.LayoutParams(dp(92), dp(40)))
            rootColumn.addView(scanRow, verticalParams(dp(4)))
        } else {
            discoveryStatus = null
            TextView(this).apply {
                text = "电脑接收端运行后，扫描一次性二维码即可配对。"
                textSize = 13f
                setTextColor(color(R.color.secondary_text))
                gravity = Gravity.CENTER
                setPadding(dp(8), dp(4), dp(8), dp(12))
            }.also { rootColumn.addView(it) }
        }
        renderComputerList(peers)

        pairButton = primaryButton(if (peers.isEmpty()) "添加电脑" else "＋   添加电脑") { openPairScanner() }
        rootColumn.addView(pairButton, verticalParams(dp(12)))

        addDivider(top = dp(8), bottom = dp(4))
        rootColumn.addView(settingsRow("关于与版本", "${BuildConfig.VERSION_NAME}  ›") {
            AlertDialog.Builder(this@MainActivity)
                .setTitle("发送到电脑")
                .setMessage("版本 ${BuildConfig.VERSION_NAME}\n图片只会在你主动分享时发送。配对凭据保存在此设备的安全存储中。\n\n最近发送：${DiagnosticLogger.latestSummary(this@MainActivity)}\n\n可导出近 7 天的本地诊断日志；不含图片内容、URI 或配对密钥。")
                .setNeutralButton("导出诊断日志") { _, _ ->
                    diagnosticsExportLauncher.launch("SendToCopyQ-Android-diagnostics.zip")
                }
                .setNegativeButton("检查更新") { _, _ -> checkAndroidUpdate() }
                .setPositiveButton("知道了", null)
                .show()
        })
        rootColumn.addView(View(this), LinearLayout.LayoutParams(-1, 0, 1f))
        rootColumn.addView(shareRouteHint(), LinearLayout.LayoutParams(-1, -2))
        rootColumn.addView(View(this), LinearLayout.LayoutParams(-1, 0, 1f))
        val footer = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            setPadding(0, dp(26), 0, dp(8))
            addView(TextView(this@MainActivity).apply { text = "▣"; textSize = 15f; setTextColor(color(R.color.brand_blue)) })
            addView(TextView(this@MainActivity).apply {
                text = "  局域网传输 · 不经过云端"
                textSize = 12f
                setTextColor(color(R.color.secondary_text))
            })
        }
        rootColumn.addView(footer)
    }

    private fun brandHeader(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        val mark = android.widget.ImageView(this@MainActivity).apply { setImageResource(R.drawable.ic_app_mark) }
        addView(mark, LinearLayout.LayoutParams(dp(48), dp(48)))
        addView(label(getString(R.string.app_name), 25f, true).apply { setPadding(dp(13), 0, 0, 0) })
    }

    private fun shareRouteHint(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.CENTER
        setPadding(0, dp(8), 0, dp(8))
        val route = LinearLayout(this@MainActivity).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val phone = android.widget.ImageView(this@MainActivity).apply {
            setImageResource(R.drawable.ic_phone_route)
            contentDescription = "手机图片"
            scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
        }
        route.addView(phone, LinearLayout.LayoutParams(dp(42), dp(54)))
        route.addView(label("→", 28f, false, R.color.brand_blue).apply {
            gravity = Gravity.CENTER
            setPadding(dp(20), 0, dp(20), 0)
        })
        val computer = android.widget.ImageView(this@MainActivity).apply {
            setImageResource(R.drawable.ic_computer_outline)
            contentDescription = "电脑"
            scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
        }
        route.addView(computer, LinearLayout.LayoutParams(dp(66), dp(54)))
        addView(route)
        addView(label("支持截图、相册及其他图片分享", 13f, false, R.color.secondary_text).apply {
            gravity = Gravity.CENTER
            setPadding(0, dp(8), 0, 0)
        })
    }

    private fun cleanComputerName(name: String): String = name.ifBlank { "我的电脑" }

    private fun checkAndroidUpdate() {
        if (checkingAndroidUpdate) {
            Toast.makeText(this, "正在检查更新，请稍候。", Toast.LENGTH_SHORT).show()
            return
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle("检查更新")
            .setMessage("正在检查 Android 更新…")
            .setNegativeButton("关闭", null)
            .setPositiveButton("下载并安装", null)
            .setNeutralButton("GitHub 下载页", null)
            .show()
        val install = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
        val github = dialog.getButton(AlertDialog.BUTTON_NEUTRAL)
        install.visibility = View.GONE
        github.visibility = View.GONE
        checkingAndroidUpdate = true
        routeProbes.execute {
            val result = try {
                AndroidUpdates.check(BuildConfig.VERSION_CODE)
            } catch (_: Exception) {
                null
            }
            runOnUiThread {
                checkingAndroidUpdate = false
                if (!dialog.isShowing || isFinishing || isDestroyed) return@runOnUiThread
                dialog.setMessage(when (result) {
                    AndroidUpdates.Result.Unpublished -> "Android 正式更新渠道尚未开放。"
                    AndroidUpdates.Result.Current -> if (BuildConfig.DEBUG) "当前为测试版，正式版使用独立签名，无法直接覆盖安装。"
                        else "当前已是最新版本。"
                    is AndroidUpdates.Result.Available -> if (BuildConfig.DEBUG)
                        "Android ${result.version} 正式版已发布。当前为测试版，无法直接覆盖安装。"
                        else "发现 Android ${result.version}。下载完成后，由 Android 确认安装。"
                    null -> "暂时无法检查更新，请稍后重试。"
                })
                if (result is AndroidUpdates.Result.Available) {
                    github.visibility = View.VISIBLE
                    github.setOnClickListener {
                        try {
                            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(result.releasePage)))
                            dialog.dismiss()
                        } catch (_: android.content.ActivityNotFoundException) {
                            Toast.makeText(this, "未找到可打开下载页的浏览器。", Toast.LENGTH_SHORT).show()
                        }
                    }
                    if (!BuildConfig.DEBUG) {
                        install.visibility = View.VISIBLE
                        install.setOnClickListener { requestUpdateInstall(dialog, result) }
                    }
                }
            }
        }
    }

    private fun requestUpdateInstall(dialog: AlertDialog, update: AndroidUpdates.Result.Available) {
        if (packageManager.canRequestPackageInstalls()) {
            downloadAndroidUpdate(dialog, update)
            return
        }
        AlertDialog.Builder(this)
            .setTitle("允许 MoryPaste 安装更新")
            .setMessage("首次安装更新需要在 Android 设置中允许 MoryPaste 安装应用。返回后，仍需确认安装。")
            .setNegativeButton("取消", null)
            .setPositiveButton("打开系统设置") { _, _ ->
                updatePermissionResult = {
                    if (dialog.isShowing && !isFinishing && !isDestroyed) {
                        if (packageManager.canRequestPackageInstalls()) downloadAndroidUpdate(dialog, update)
                        else dialog.setMessage("安装授权未开启。可以重试或使用 GitHub 下载页。")
                    }
                }
                try {
                    updateSettingsLauncher.launch(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:$packageName")))
                } catch (_: android.content.ActivityNotFoundException) {
                    updatePermissionResult = null
                    dialog.setMessage("无法打开安装授权设置。可以使用 GitHub 下载页。")
                }
            }.show()
    }

    private fun downloadAndroidUpdate(dialog: AlertDialog, update: AndroidUpdates.Result.Available) {
        val install = dialog.getButton(AlertDialog.BUTTON_POSITIVE)
        val close = dialog.getButton(AlertDialog.BUTTON_NEGATIVE)
        val github = dialog.getButton(AlertDialog.BUTTON_NEUTRAL)
        install.isEnabled = false
        close.isEnabled = false
        github.isEnabled = false
        dialog.setCancelable(false)
        dialog.setMessage("正在下载安装包…")
        routeProbes.execute {
            var candidate: File? = null
            val result = runCatching {
                val apk = AndroidUpdates.download(update, File(cacheDir, "android-updates")) { percent ->
                    if (percent != null) runOnUiThread {
                        if (dialog.isShowing) dialog.setMessage("正在下载安装包… $percent%")
                    }
                }
                candidate = apk
                verifyUpdatePackage(apk, update)
                apk
            }
            if (result.isFailure) candidate?.delete()
            runOnUiThread {
                val apk = result.getOrNull()
                if (!dialog.isShowing || isFinishing || isDestroyed) {
                    apk?.delete()
                    return@runOnUiThread
                }
                install.isEnabled = true
                close.isEnabled = true
                github.isEnabled = true
                dialog.setCancelable(true)
                if (apk == null) {
                    val cause = result.exceptionOrNull()
                    val reason = if (cause is IllegalArgumentException) cause.message else null
                    dialog.setMessage(reason ?: "下载安装包失败。可以重试或使用 GitHub 下载页。")
                    return@runOnUiThread
                }
                dialog.setMessage("安装包已校验，可以安装。")
                install.text = "安装"
                install.setOnClickListener { openUpdateInstaller(dialog, apk) }
                if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) openUpdateInstaller(dialog, apk)
            }
        }
    }

    @Suppress("DEPRECATION")
    private fun verifyUpdatePackage(apk: File, update: AndroidUpdates.Result.Available) {
        val archive = packageManager.getPackageArchiveInfo(apk.path, 0)
        require(archive != null && archive.packageName == packageName && archive.versionName == update.version &&
            PackageInfoCompat.getLongVersionCode(archive) == update.versionCode.toLong()) { "安装包与更新版本不匹配" }
    }

    private fun openUpdateInstaller(dialog: AlertDialog, apk: File) {
        if (!packageManager.canRequestPackageInstalls()) {
            dialog.setMessage("安装授权已关闭。请重新检查更新，或使用 GitHub 下载页。")
            return
        }
        val uri = FileProvider.getUriForFile(this, "$packageName.updates", apk)
        try {
            startActivity(Intent(Intent.ACTION_VIEW).setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION))
            dialog.dismiss()
        } catch (_: android.content.ActivityNotFoundException) {
            dialog.setMessage("没有找到 Android 安装器。可以使用 GitHub 下载页。")
        } catch (_: SecurityException) {
            dialog.setMessage("Android 拒绝启动安装器。请检查安装授权，或使用 GitHub 下载页。")
        }
    }

    private fun refreshDiscovery() {
        if (shareMode) return
        val peers = PeerStore.list(this)
        if (peers.isEmpty()) {
            discoveryStatus = null
            deviceList?.removeAllViews()
            return
        }
        scanGeneration++
        val generation = scanGeneration
        val network = discovery.activeWifiNetwork()?.networkHandle
        discovery.stop()
        recentShareTarget = null
        probingComputers.clear()
        scanRunning = true
        peers.forEach { computerStatus[it.computerId] = STATUS_SEARCHING }
        discoveryStatus?.text = "正在查找局域网电脑…"
        renderComputerList(peers)
        discovery.start(object : LanDiscovery.Listener {
            override fun onStage(computerId: String?, status: String) {
                if (generation != scanGeneration) return
                if (computerId != null) computerStatus[computerId] = status
                else if (status != "正在解析地址") peers.forEach { computerStatus[it.computerId] = status }
                renderComputerList()
            }
            override fun onService(computer: DiscoveredComputer) {
                if (generation != scanGeneration) return
                val peer = PeerStore.list(this@MainActivity).firstOrNull { it.computerId == computer.computerId } ?: return
                if (!probingComputers.add(peer.computerId)) return
                computerStatus[peer.computerId] = STATUS_VERIFYING
                renderComputerList()
                worker.execute {
                    var verified = false
                    var identityFailure = false
                    try {
                        verified = SecureTransport.verifyComputer(peer, computer.host, computer.port)
                        DiagnosticLogger.event(applicationContext, null, "TLS_PIN_VERIFIED", fields = mapOf(
                            "computer_id" to peer.computerId, "ip_addresses" to computer.host, "stage" to "identity_verified"))
                    } catch (error: Exception) {
                        val causes = generateSequence<Throwable>(error) { it.cause }.take(8).toList()
                        identityFailure = causes.any {
                            val message = it.message.orEmpty()
                            it is javax.net.ssl.SSLException || message.contains("指纹不匹配") || message.contains("身份与配对") || message.contains("身份验证失败")
                        }
                        DiagnosticLogger.event(applicationContext, null, if (identityFailure) "TLS_PIN_FAILED" else "TCP_CONNECT_FAILED",
                            level = "ERROR", status = "failed", fields = mapOf("computer_id" to peer.computerId,
                                "ip_addresses" to computer.host, "stage" to "identity_probe"), error = error)
                    }
                    runOnUiThread {
                        if (generation != scanGeneration) return@runOnUiThread
                        if (verified) {
                            PeerStore.updateConnection(this@MainActivity, peer.computerId, computer.host, computer.port, computer.name)
                            computerStatus[peer.computerId] = STATUS_REACHABLE
                        } else {
                            computerStatus[peer.computerId] = if (identityFailure) STATUS_IDENTITY_ERROR else "连接电脑失败"
                        }
                        rememberHomeTarget(peers, network)
                        renderComputerList()
                    }
                }
            }

            override fun onFinished() {
                if (generation != scanGeneration) return
                scanRunning = false
                peers.forEach { peer ->
                    if (computerStatus[peer.computerId] == STATUS_SEARCHING) computerStatus[peer.computerId] = "未发现电脑"
                }
                discoveryStatus?.text = "查找已完成 · 仅显示已配对电脑"
                rememberHomeTarget(peers, network)
                renderComputerList()
            }
        }, 2500L)
    }

    private fun stopDiscovery() {
        scanGeneration++
        scanRunning = false
        discovery.stop()
    }

    private fun rememberHomeTarget(peers: List<PairedComputer>, network: Long?) {
        if (scanRunning || network == null || network != discovery.activeWifiNetwork()?.networkHandle) return
        val reachable = peers.filter { computerStatus[it.computerId] == STATUS_REACHABLE }
        if (peers.any { computerStatus[it.computerId] !in setOf(STATUS_REACHABLE, "未发现电脑") }) return
        val candidate = reachable.singleOrNull()
        val current = PeerStore.list(this)
        recentShareTarget = current.firstOrNull { it.computerId == candidate?.computerId }
            ?.let { recentTarget(network, current, it) }
    }

    private fun recentTarget(network: Long, peers: List<PairedComputer>, peer: PairedComputer): RecentShareTarget =
        RecentShareTarget(network, peers.mapTo(mutableSetOf()) { it.computerId }, peer.computerId,
            peer.host, peer.port, SystemClock.elapsedRealtime())

    private fun renderComputerList(peers: List<PairedComputer> = PeerStore.list(this)) {
        val container = deviceList ?: return
        container.removeAllViews()
        val reachableIds = computerStatus.filterValues { it == STATUS_REACHABLE }.keys
        val ordered = PeerCatalog.ordered(peers, reachableIds)
        ordered.forEach { peer ->
            val reachable = computerStatus[peer.computerId] == STATUS_REACHABLE
            val card = surfaceCard().apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(dp(13), dp(13), dp(10), dp(13))
                background = rounded(R.color.card_background, 16)
            }
            val icon = android.widget.ImageView(this).apply {
                setImageResource(R.drawable.ic_computer_outline)
                scaleType = android.widget.ImageView.ScaleType.FIT_CENTER
                setPadding(dp(5), dp(7), dp(5), dp(7))
                background = rounded(R.color.brand_blue_soft, 14)
            }
            card.addView(icon, LinearLayout.LayoutParams(dp(44), dp(44)))
            val detail = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(12), 0, dp(4), 0) }
            detail.addView(label(cleanComputerName(peer.name), 16f, true))
            val status = computerStatus[peer.computerId] ?: "暂不可连接"
            val statusColor = when (status) {
                STATUS_REACHABLE -> R.color.success_green
                STATUS_IDENTITY_ERROR -> R.color.error_text
                STATUS_VERIFYING, STATUS_SEARCHING -> R.color.brand_blue
                else -> R.color.secondary_text
            }
            val info = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL; gravity = Gravity.CENTER_VERTICAL; setPadding(0, dp(3), 0, 0) }
            info.addView(TextView(this).apply {
                text = if (reachable) "●" else "○"
                textSize = 11f
                setTextColor(color(statusColor))
            })
            info.addView(label(status, 12f, false, statusColor).apply { setPadding(dp(5), 0, 0, 0) })
            detail.addView(info)
            card.addView(detail, LinearLayout.LayoutParams(0, -2, 1f))
            val remove = TextView(this).apply {
                text = "移除"
                textSize = 12f
                setTextColor(color(R.color.secondary_text))
                setPadding(dp(10), dp(12), dp(4), dp(12))
                setOnClickListener { confirmRemoveComputer(peer) }
            }
            card.addView(remove)
            container.addView(card, verticalParams(if (container.childCount == 0) 0 else dp(8)))
        }
    }

    private fun secondaryButton(title: String, action: () -> Unit) = MaterialButton(this).apply {
        text = title
        cornerRadius = dp(12)
        minimumHeight = dp(40)
        insetTop = 0
        insetBottom = 0
        backgroundTintList = ColorStateList.valueOf(color(R.color.brand_blue_soft))
        setTextColor(color(R.color.brand_blue))
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setOnClickListener { action() }
    }

    private fun label(textValue: String, size: Float, strong: Boolean, tint: Int = R.color.primary_text) = TextView(this).apply {
        text = textValue
        textSize = size
        setTextColor(color(tint))
        if (strong) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
    }

    private fun rounded(colorId: Int, radiusDp: Int) = GradientDrawable().apply {
        setColor(color(colorId))
        cornerRadius = dp(radiusDp).toFloat()
    }

    private fun surfaceCard(): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(dp(16), dp(16), dp(16), dp(16))
        background = rounded(R.color.card_background, 16)
        elevation = dp(1).toFloat()
    }

    private fun primaryButton(title: String, action: () -> Unit) = MaterialButton(this).apply {
        text = title
        cornerRadius = dp(14)
        minimumHeight = dp(52)
        backgroundTintList = ColorStateList.valueOf(color(R.color.brand_blue))
        setTextColor(color(R.color.on_brand))
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setOnClickListener { action() }
    }

    private fun settingsRow(label: String, trailing: String, onClick: () -> Unit) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        isClickable = true
        isFocusable = true
        setPadding(dp(4), dp(14), dp(4), dp(14))
        setOnClickListener { onClick() }
        addView(TextView(this@MainActivity).apply {
            text = label
            textSize = 15f
            setTextColor(color(R.color.secondary_text))
        }, LinearLayout.LayoutParams(0, -2, 1f))
        addView(TextView(this@MainActivity).apply {
            text = trailing
            textSize = 14f
            setTextColor(color(R.color.secondary_text))
        })
    }

    private fun addDivider(top: Int, bottom: Int) {
        rootColumn.addView(View(this).apply { setBackgroundColor(color(R.color.divider)) },
            LinearLayout.LayoutParams(-1, dp(1)).apply { topMargin = top; bottomMargin = bottom })
    }

    private fun instructionRow(number: String, message: String) = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        val badge = TextView(this@MainActivity).apply {
            text = number
            textSize = 13f
            typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
            gravity = Gravity.CENTER
            setTextColor(color(R.color.brand_blue))
            background = GradientDrawable().apply {
                setColor(color(R.color.brand_blue_soft))
                cornerRadius = dp(16).toFloat()
            }
        }
        addView(badge, LinearLayout.LayoutParams(dp(28), dp(28)))
        addView(TextView(this@MainActivity).apply {
            text = message
            textSize = 14f
            setTextColor(color(R.color.secondary_text))
            setPadding(dp(12), 0, 0, 0)
        }, LinearLayout.LayoutParams(0, -2, 1f))
    }

    private fun color(resource: Int): Int = ContextCompat.getColor(this, resource)

    private fun isFileShare(source: Intent?): Boolean =
        source?.action == Intent.ACTION_SEND || source?.action == Intent.ACTION_SEND_MULTIPLE

    private fun handleIncomingIntent(source: Intent) {
        stopDiscovery()
        resumeTargetSearch = false
        batchPeer = null
        fileReceiveSupported = null
        receiveResult = null
        currentTaskId = UUID.randomUUID().toString()
        taskStartedAt = SystemClock.elapsedRealtime()
        DiagnosticLogger.event(this, currentTaskId, "SHARE_RECEIVED", fields = mapOf(
            "action" to source.action,
            "intent_mime" to source.type,
            "read_grant_flag" to (source.flags and Intent.FLAG_GRANT_READ_URI_PERMISSION != 0),
            "clip_item_count" to (source.clipData?.itemCount ?: 0),
        ))
        if (source.action != Intent.ACTION_SEND && source.action != Intent.ACTION_SEND_MULTIPLE) {
            showShareError("请从文件的系统分享菜单选择“发送到电脑”。")
            logTaskFinished("failed", "UNSUPPORTED_ACTION")
            return
        }
        pendingUris = extractSharedUris(source)
        currentIndex = 0
        pendingBytes = null
        DiagnosticLogger.event(this, currentTaskId, "SHARE_INTENT_PARSED", status = if (pendingUris.isEmpty()) "failed" else "ok", fields = mapOf(
            "image_count" to pendingUris.size,
            "action" to source.action,
            "intent_mime" to source.type,
        ))
        pendingUris.forEachIndexed { index, uri ->
            val readGranted = if (uri.scheme == "content") {
                checkUriPermission(uri, Process.myPid(), Process.myUid(), Intent.FLAG_GRANT_READ_URI_PERMISSION) == android.content.pm.PackageManager.PERMISSION_GRANTED
            } else true
            DiagnosticLogger.event(this, currentTaskId, "URI_PERMISSION_CHECK", level = if (readGranted) "INFO" else "WARN", status = if (readGranted) "ok" else "denied", fields = mapOf(
                "image_index" to index + 1,
                "image_count" to pendingUris.size,
                "uri_scheme" to uri.scheme,
                "read_granted" to readGranted,
            ))
        }
        if (pendingUris.isEmpty()) {
            showShareError("没有收到可读取的文件。请分享图片、视频或文件本身。")
            logTaskFinished("failed", "NO_IMAGE_URI")
            return
        }
        if (pendingUris.size > MAX_SHARED_IMAGES) {
            pendingUris = emptyList()
            showShareError("一次最多发送 $MAX_SHARED_IMAGES 个文件，请分批分享。")
            logTaskFinished("failed", "TOO_MANY_IMAGES")
            return
        }
        buildShareUi()
        if (PeerStore.list(this).isEmpty()) {
            setShareStatus("先添加电脑。配对完成后继续发送这批文件。")
            pairButton.visibility = View.VISIBLE
            DiagnosticLogger.event(this, currentTaskId, "SEND_FINISHED", level = "WARN", status = "waiting_for_pairing",
                fields = mapOf("image_count" to pendingUris.size, "elapsed_ms" to (SystemClock.elapsedRealtime() - taskStartedAt)))
            DiagnosticLogger.rememberResult(this, currentTaskId, "failed", "请先添加电脑")
        } else {
            sendNext()
        }
    }

    private fun logTaskFinished(status: String, errorCode: String) {
        val taskId = currentTaskId
        DiagnosticLogger.event(this, taskId, "SEND_FINISHED", level = "ERROR", status = status, fields = mapOf(
            "image_count" to pendingUris.size,
            "elapsed_ms" to (SystemClock.elapsedRealtime() - taskStartedAt),
            "error_code" to errorCode,
        ))
        DiagnosticLogger.rememberResult(this, taskId, status, errorCode)
    }

    @Suppress("DEPRECATION")
    private fun extractSharedUris(source: Intent): List<Uri> {
        if (source.action == Intent.ACTION_SEND_MULTIPLE) {
            val many: ArrayList<Uri>? = if (Build.VERSION.SDK_INT >= 33) {
                source.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
            } else {
                source.getParcelableArrayListExtra(Intent.EXTRA_STREAM)
            }
            val streams = many.orEmpty().filter { it.scheme == "content" || it.scheme == "file" }.distinct()
            if (streams.isNotEmpty()) return streams
        } else {
            val one: Uri? = if (Build.VERSION.SDK_INT >= 33) {
                source.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
            } else {
                source.getParcelableExtra(Intent.EXTRA_STREAM) as? Uri
            }
            if (one != null && (one.scheme == "content" || one.scheme == "file")) return listOf(one)
        }
        val found = LinkedHashSet<Uri>()
        source.clipData?.let { clip ->
            for (index in 0 until clip.itemCount) {
                val uri = clip.getItemAt(index).uri
                if (uri != null && (uri.scheme == "content" || uri.scheme == "file")) found.add(uri)
            }
        }
        return found.toList()
    }

    private fun buildShareUi() {
        rootColumn.removeAllViews()
        activeTargetName = ""
        val header = FrameLayout(this)
        header.addView(label("发送到电脑", 21f, true).apply {
            gravity = Gravity.CENTER
            setPadding(dp(46), 0, dp(46), 0)
            minimumHeight = dp(44)
        }, FrameLayout.LayoutParams(-1, -2))
        header.addView(secondaryButton("×") { finish() }.apply {
            contentDescription = "关闭发送面板"
            setPadding(0, 0, 0, 0)
            minimumWidth = 0
            textSize = 21f
        }, FrameLayout.LayoutParams(dp(44), dp(44), Gravity.END or Gravity.TOP))
        rootColumn.addView(header)
        val countLabel = if (intent.type.orEmpty().startsWith("image/")) "张图片" else "个文件"
        if (pendingUris.isNotEmpty()) rootColumn.addView(label("将发送 ${pendingUris.size} $countLabel", 14f, false, R.color.secondary_text).apply {
            gravity = Gravity.CENTER
        })
        val transferArt = LinearLayout(this).apply {
            gravity = Gravity.CENTER
            background = rounded(R.color.brand_blue_soft, 16)
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO_HIDE_DESCENDANTS
            addView(ImageView(this@MainActivity).apply {
                setImageResource(R.drawable.ic_phone_route)
            }, LinearLayout.LayoutParams(dp(36), dp(45)))
            addView(label("→", 23f, true, R.color.brand_blue).apply {
                gravity = Gravity.CENTER
            }, LinearLayout.LayoutParams(dp(36), -2))
            addView(ImageView(this@MainActivity).apply {
                setImageResource(R.drawable.ic_computer_outline)
            }, LinearLayout.LayoutParams(dp(57), dp(45)))
        }
        rootColumn.addView(transferArt, LinearLayout.LayoutParams(dp(176), dp(68)).apply {
            gravity = Gravity.CENTER_HORIZONTAL
            topMargin = dp(14)
        })
        shareTarget = label("", 14f, true, R.color.brand_blue).apply {
            gravity = Gravity.CENTER
            visibility = View.GONE
        }
        rootColumn.addView(shareTarget, verticalParams(dp(12)))
        targetButtons = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            visibility = View.GONE
        }
        rootColumn.addView(targetButtons, verticalParams(dp(14)))
        targetSearchHint = label("正在查找其他电脑…", 13f, false, R.color.secondary_text).apply {
            gravity = Gravity.CENTER
            minHeight = dp(52)
            visibility = View.GONE
        }
        rootColumn.addView(targetSearchHint, verticalParams(dp(8)))
        shareStatusCard = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            minimumHeight = dp(52)
        }
        statusIcon = TextView(this).apply {
            text = "↗"
            textSize = 16f
            gravity = Gravity.CENTER
            setTextColor(color(R.color.brand_blue))
            background = rounded(R.color.brand_blue_soft, 14)
            importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO
        }
        shareStatusCard.addView(statusIcon, LinearLayout.LayoutParams(dp(28), dp(28)).apply {
            marginEnd = dp(8)
        })
        statusView = label("正在连接电脑…", 14f, false).apply {
            accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE
        }
        shareStatusCard.addView(statusView)
        rootColumn.addView(shareStatusCard, verticalParams(dp(14)))
        pairButton = primaryButton("添加电脑") { openPairScanner() }.apply { visibility = View.GONE }
        rootColumn.addView(pairButton, verticalParams(dp(18)))
        retryButton = primaryButton("重试发送") { sendNext() }.apply { visibility = View.GONE }
        rootColumn.addView(retryButton, verticalParams(dp(18)))
    }

    private fun topBar(title: String, close: () -> Unit): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(TextView(this@MainActivity).apply {
            text = "‹"
            textSize = 34f
            setTextColor(color(R.color.primary_text))
            setOnClickListener { close() }
        }, LinearLayout.LayoutParams(dp(48), dp(54)))
        addView(label(title, 19f, true))
    }

    private fun showSuccess(count: Int, result: SecureTransport.ReceiveResult?) {
        rootColumn.removeAllViews()
        rootColumn.addView(LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; gravity = Gravity.CENTER })
        val body = rootColumn.getChildAt(0) as LinearLayout
        val mark = TextView(this).apply {
            text = "✓"
            textSize = 34f
            gravity = Gravity.CENTER
            setTextColor(color(R.color.success_green))
            background = rounded(R.color.success_soft, 60)
        }
        body.addView(mark, LinearLayout.LayoutParams(dp(62), dp(62)))
        val (heading, proofText) = when (result?.clipboard) {
            "image" -> "发送成功" to "图片已复制，可直接 Ctrl+V"
            "files" -> "发送成功" to "${result.imageCount} 张图片已复制，可作为文件粘贴"
            "failed" -> "文件已保存" to "图片未能复制，请在电脑上重新复制"
            "unchanged" -> "发送成功" to "可在电脑接收目录中查看"
            else -> "发送成功" to "已写入系统剪贴板，可直接 Ctrl+V"
        }
        body.addView(label(heading, 22f, true).apply { gravity = Gravity.CENTER; setPadding(0, dp(12), 0, dp(6)) })
        val destination = activeTargetName.ifBlank { "电脑" }
        val detail = if (result == null) "${count} 张图片已发送到 $destination" else "已保存 $count 个文件到 $destination"
        body.addView(label(detail, 15f, false, R.color.secondary_text).apply {
            gravity = Gravity.CENTER
            setLineSpacing(dp(5).toFloat(), 1f)
        })
        val proof = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(16), dp(15), dp(16), dp(15))
            background = rounded(R.color.success_soft, 14)
        }
        proof.addView(label(proofText, 14f, true, R.color.primary_text), LinearLayout.LayoutParams(0, -2, 1f))
        body.addView(proof, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(16) })
        body.addView(primaryButton("完成") { finish() }, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(14) })
    }

    private fun showFailure(message: String) {
        setShareStatus(message, error = true)
        retryButton.visibility = View.VISIBLE
    }

    private fun findShareTarget() {
        val peers = PeerStore.list(this)
        if (peers.isEmpty()) {
            setShareStatus("尚未添加电脑。添加电脑后即可继续发送。", error = true)
            pairButton.visibility = View.VISIBLE
            retryButton.visibility = View.GONE
            return
        }
        val search = RouteSearch(peers, discovery.activeWifiNetwork()?.networkHandle)
        routeSearch = search
        busy = true
        pairButton.visibility = View.GONE
        retryButton.text = "重新查找"
        setShareStatus("正在查找可连接的电脑…")
        search.deadline = Runnable { finishTargetSearch(search, timedOut = true) }
        mainHandler.postDelayed(search.deadline, 8_500L)
        recentShareTarget?.peerFor(search.network, peers, SystemClock.elapsedRealtime())?.let { probeTarget(search, it) }
        peers.forEach { probeTarget(search, it) }
        discovery.start(object : LanDiscovery.Listener {
            override fun onStage(computerId: String?, status: String) {
                if (status != "正在解析地址") search.discoveryReliable = false
            }

            override fun onService(computer: DiscoveredComputer) {
                val peer = search.peers.firstOrNull { it.computerId == computer.computerId } ?: return
                probeTarget(search, peer.copy(host = computer.host, port = computer.port))
            }

            override fun onFinished() {
                search.discoveryFinished = true
                finishTargetSearch(search)
            }
        })
    }

    private fun probeTarget(search: RouteSearch, peer: PairedComputer) {
        if (routeSearch !== search || search.settled ||
            !search.attempted.add(Triple(peer.computerId, peer.host, peer.port))) return
        search.pending++
        search.probes += routeProbes.submit {
            val verified = try {
                VerifiedTarget(peer, SecureTransport.verifyFileCapability(peer))
            } catch (_: Exception) {
                null
            }
            runOnUiThread {
                if (routeSearch !== search || search.settled) return@runOnUiThread
                search.pending--
                if (verified != null && search.verified.putIfAbsent(peer.computerId, verified) == null &&
                    search.network == discovery.activeWifiNetwork()?.networkHandle) {
                    showVerifiedTarget(search, verified)
                }
                finishTargetSearch(search)
            }
        }
    }

    private fun finishTargetSearch(search: RouteSearch, timedOut: Boolean = false) {
        if (routeSearch !== search || search.settled) return
        if (search.network != discovery.activeWifiNetwork()?.networkHandle) {
            cancelRouteSearch()
            showFailure("网络已切换，请重试发送。")
            logTaskFinished("failed", "SHARE_NETWORK_CHANGED")
            return
        }
        val complete = search.discoveryFinished && search.pending == 0
        if (!timedOut && !complete && search.verified.size != search.peers.size) return
        recentShareTarget = if (complete && search.discoveryReliable && search.network != null)
            search.verified.values.singleOrNull()?.let { recentTarget(search.network, search.peers, it.peer) } else null
        stopTargetDiscovery(search)
        targetSearchHint.visibility = View.GONE
        if (search.verified.isEmpty()) {
            cancelRouteSearch()
            showFailure("没找到可连接的已配对电脑。请确认电脑正在接收，且与手机连接同一局域网。")
            logTaskFinished("failed", "NO_REACHABLE_PAIRED_COMPUTER")
        }
    }

    private fun showVerifiedTarget(search: RouteSearch, target: VerifiedTarget) {
        val button = primaryButton("发送到 ${target.peer.name}") {
            if (routeSearch === search) {
                cancelRouteSearch()
                if (search.network == discovery.activeWifiNetwork()?.networkHandle) sendToTarget(target)
                else {
                    showFailure("网络已切换，请重新查找。")
                    logTaskFinished("failed", "SHARE_NETWORK_CHANGED")
                }
            }
        }
        button.insetTop = 0
        button.insetBottom = 0
        targetButtons.addView(button, verticalParams(if (targetButtons.childCount == 0) 0 else dp(8)))
        targetButtons.visibility = View.VISIBLE
        shareStatusCard.visibility = View.GONE
        targetSearchHint.visibility = if (search.verified.size == 1 && search.peers.size > 1) View.VISIBLE else View.GONE
        DiagnosticLogger.event(this, currentTaskId, "SHARE_TARGET_AVAILABLE", fields = mapOf(
            "computer_id" to target.peer.computerId, "elapsed_ms" to (SystemClock.elapsedRealtime() - taskStartedAt)))
    }

    private fun stopTargetDiscovery(search: RouteSearch) {
        if (search.settled) return
        search.settled = true
        mainHandler.removeCallbacks(search.deadline)
        search.probes.forEach { it.cancel(true) }
        discovery.stop()
    }

    private fun cancelRouteSearch() {
        val search = routeSearch ?: return
        routeSearch = null
        stopTargetDiscovery(search)
        targetButtons.removeAllViews()
        targetButtons.visibility = View.GONE
        targetSearchHint.visibility = View.GONE
        shareStatusCard.visibility = View.VISIBLE
        busy = false
    }

    private fun sendToTarget(target: VerifiedTarget) {
        val peer = target.peer
        batchPeer = peer
        fileReceiveSupported = target.supportsFiles
        activeTargetName = peer.name
        shareTarget.text = "发送到 ${peer.name}"
        shareTarget.visibility = View.VISIBLE
        retryButton.text = "重试发送"
        DiagnosticLogger.event(this, currentTaskId, "SHARE_TARGET_VERIFIED", fields = mapOf(
            "computer_id" to peer.computerId, "stage" to "user_selected",
            "elapsed_ms" to (SystemClock.elapsedRealtime() - taskStartedAt)))
        sendNext()
    }

    private fun sendNext() {
        if (busy || pendingUris.isEmpty()) return
        val peer = batchPeer
        if (peer == null) {
            findShareTarget()
            return
        }
        if (currentIndex >= pendingUris.size) {
            finishSharing()
            return
        }
        busy = true
        retryButton.visibility = View.GONE
        pairButton.visibility = View.GONE
        val uri = pendingUris[currentIndex]
        val cached = pendingBytes
        var loadedForRetry: ByteArray? = cached
        val imageIndex = currentIndex + 1
        val imageCount = pendingUris.size
        val taskId = currentTaskId
        val readStartedAt = SystemClock.elapsedRealtime()
        setShareStatus("正在准备第 $imageIndex/$imageCount 个文件…")
        worker.execute {
            try {
                if (fileReceiveSupported == true) {
                    val result = SecureTransport.sendFile(applicationContext, peer, uri, taskId, imageIndex, imageCount) { sent, total ->
                        runOnUiThread {
                            if (busy && currentTaskId == taskId) {
                                val percent = if (total > 0) " · ${sent * 100 / total}%" else ""
                                setShareStatus("正在发送第 $imageIndex/$imageCount 个文件$percent")
                            }
                        }
                    }
                    check(result.savedCount == imageIndex && result.imageCount in 0..result.savedCount &&
                        (if (imageIndex == imageCount) result.clipboard in setOf("image", "files", "failed", "unchanged")
                        else result.clipboard == "pending")) { "电脑返回的文件接收确认不匹配" }
                    runOnUiThread {
                        busy = false
                        receiveResult = result
                        currentIndex++
                        if (currentIndex < pendingUris.size) sendNext() else finishSharing()
                    }
                    return@execute
                }
                check(intent.type.orEmpty().startsWith("image/", ignoreCase = true) ||
                    contentResolver.getType(uri).orEmpty().startsWith("image/", ignoreCase = true)) {
                    "当前电脑版本仅支持发送 PNG 或 JPEG 图片，请升级电脑客户端。"
                }
                if (cached == null) DiagnosticLogger.event(this, taskId, "IMAGE_READ_STARTED", fields = mapOf(
                    "image_index" to imageIndex,
                    "image_count" to imageCount,
                    "mime_hint" to contentResolver.getType(uri),
                    "elapsed_ms" to 0,
                ))
                val (bytes, mime) = if (cached != null) cached to null else {
                    val hint = contentResolver.getType(uri)
                    SecureTransport.readSharedImage({ contentResolver.openInputStream(uri) }, hint)
                }
                loadedForRetry = bytes
                val actualMime = mime ?: detectCachedMime(bytes)
                if (cached == null) DiagnosticLogger.event(this, taskId, "IMAGE_READ_COMPLETED", fields = mapOf(
                    "image_index" to imageIndex,
                    "image_count" to imageCount,
                    "mime" to actualMime,
                    "bytes" to bytes.size,
                    "elapsed_ms" to (SystemClock.elapsedRealtime() - readStartedAt),
                ))
                DiagnosticLogger.event(this, taskId, "IMAGE_FORMAT_DETECTED", fields = mapOf(
                    "image_index" to imageIndex,
                    "image_count" to imageCount,
                    "mime" to actualMime,
                    "bytes" to bytes.size,
                ))
                SecureTransport.sendImage(applicationContext, peer, bytes, actualMime, taskId, imageIndex, imageCount)
                runOnUiThread {
                    busy = false
                    pendingBytes = null
                    currentIndex++
                    if (currentIndex < pendingUris.size) sendNext() else finishSharing()
                }
            } catch (e: Exception) {
                if (loadedForRetry == null) DiagnosticLogger.event(this, taskId, "IMAGE_READ_FAILED", level = "ERROR", status = "failed", fields = mapOf(
                    "image_index" to imageIndex,
                    "image_count" to imageCount,
                    "elapsed_ms" to (SystemClock.elapsedRealtime() - readStartedAt),
                ), error = e)
                runOnUiThread {
                    busy = false
                    pendingBytes = loadedForRetry
                    DiagnosticLogger.event(this, taskId, "SEND_FINISHED", level = "ERROR", status = "failed", fields = mapOf(
                        "image_index" to imageIndex,
                        "image_count" to imageCount,
                        "elapsed_ms" to (SystemClock.elapsedRealtime() - readStartedAt),
                        "error_code" to e.javaClass.simpleName,
                    ), error = e)
                    DiagnosticLogger.rememberResult(this, taskId, "failed", friendlySendError(e))
                    val saved = receiveResult?.savedCount ?: 0
                    val prefix = if (saved > 0) "电脑已确认保存 $saved/$imageCount 个文件。\n" else ""
                    setShareStatus(prefix + friendlySendError(e), error = true)
                    retryButton.visibility = View.VISIBLE
                }
            }
        }
    }

    private fun detectCachedMime(bytes: ByteArray): String = when {
        bytes.size >= 8 && bytes.copyOfRange(0, 8).contentEquals(byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)) -> "image/png"
        bytes.size >= 3 && bytes[0] == 0xff.toByte() && bytes[1] == 0xd8.toByte() && bytes[2] == 0xff.toByte() -> "image/jpeg"
        else -> throw IllegalArgumentException("这张图片不是支持的 PNG 或 JPEG 格式。")
    }

    private fun finishSharing() {
        busy = false
        lastSentCount = pendingUris.size
        val taskId = currentTaskId
        DiagnosticLogger.event(this, taskId, "SEND_FINISHED", fields = mapOf(
            "image_count" to pendingUris.size,
            "status" to "success",
            "elapsed_ms" to (SystemClock.elapsedRealtime() - taskStartedAt),
        ))
        val result = receiveResult
        DiagnosticLogger.rememberResult(this, taskId, "success", when (result?.clipboard) {
            "failed" -> "已保存文件，图片未能复制"
            null -> "电脑已确认图片写入剪贴板"
            else -> "电脑已保存文件"
        })
        val count = lastSentCount
        pendingUris = emptyList()
        pendingBytes = null
        currentIndex = 0
        showSuccess(count, result)
    }

    private fun friendlySendError(error: Exception): String {
        val detail = error.message.orEmpty()
        return when {
            detail.contains("pairing code expired", true) -> "配对二维码已过期或用过。请在电脑端重新生成后再扫描。"
            detail.contains("certificate fingerprint", true) -> "电脑身份验证失败，已停止发送。请确认扫描的是可信电脑上的新二维码。"
            detail.contains("设备身份验证失败", true) || detail.contains("unauthorized", true) -> "这台手机的配对授权已失效。请在电脑端撤销旧授权后重新配对。"
            detail.contains("timed out", true) || detail.contains("failed to connect", true) || detail.contains("refused", true) || detail.contains("unreachable", true) -> "没有连上已配对电脑。请确认电脑接收端正在运行，且手机和电脑连在同一局域网，然后重试。"
            detail.contains("只接受 PNG 或 JPEG", true) || detail.contains("不支持的图片", true) || detail.contains("超过 50 MiB", true) -> detail
            detail.contains("clipboard", true) || detail.contains("CopyQ", true) -> "电脑收到了图片，但未能确认剪贴板写入成功：$detail"
            else -> "发送未完成：${detail.ifBlank { "请确认电脑接收端正在运行后重试。" }}"
        }
    }

    private fun showShareError(message: String) {
        buildShareUi()
        setShareStatus(message, error = true)
    }

    private fun setShareStatus(message: String, error: Boolean = false) {
        statusView.text = message
        statusView.setTextColor(color(if (error) R.color.error_text else R.color.primary_text))
        statusIcon.text = when {
            error -> "×"
            message.contains("连接", true) -> "◌"
            message.contains("发送", true) -> "➤"
            else -> "✓"
        }
        statusIcon.setTextColor(color(if (error) R.color.error_text else R.color.brand_blue))
        statusIcon.background = rounded(if (error) R.color.error_soft else R.color.brand_blue_soft, 60)
        if (error) retryButton.visibility = View.VISIBLE
        else if (busy) retryButton.visibility = View.GONE
    }

    private fun openPairScanner() {
        pairLauncher.launch(Intent(this, QrScanActivity::class.java))
    }

    private fun confirmRemoveComputer(peer: PairedComputer) {
        AlertDialog.Builder(this)
            .setTitle("移除 ${peer.name}？")
            .setMessage("这会删除手机保存的这台电脑的配对凭据，不影响其他电脑。若要同时撤销电脑上的授权，请在该电脑控制页撤销这台手机。")
            .setNegativeButton("取消", null)
            .setPositiveButton("移除") { _, _ ->
                PeerStore.remove(this, peer.computerId)
                computerStatus.remove(peer.computerId)
                showHome()
                Toast.makeText(this@MainActivity, "已从手机移除 ${peer.name}。", Toast.LENGTH_SHORT).show()
            }
            .show()
    }

    private fun verticalParams(top: Int = dp(10)) = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.MATCH_PARENT,
        LinearLayout.LayoutParams.WRAP_CONTENT,
    ).apply { topMargin = top }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    override fun onDestroy() {
        cancelRouteSearch()
        stopDiscovery()
        routeProbes.shutdownNow()
        worker.shutdownNow()
        super.onDestroy()
    }

    companion object {
        private var recentShareTarget: RecentShareTarget? = null
        private const val MAX_SHARED_IMAGES = 20
        private const val STATUS_REACHABLE = "可连接"
        private const val STATUS_VERIFYING = "正在验证身份"
        private const val STATUS_IDENTITY_ERROR = "身份验证失败"
        private const val STATUS_SEARCHING = "正在查找…"
    }
}
