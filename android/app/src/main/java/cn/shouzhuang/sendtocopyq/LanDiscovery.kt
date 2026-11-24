package cn.shouzhuang.sendtocopyq

import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Network
import android.net.nsd.DiscoveryRequest
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import java.net.Inet4Address
import java.util.UUID
import java.util.concurrent.Executor

data class DiscoveredComputer(val computerId: String, val name: String, val host: String, val port: Int, val protocolVersion: Int)

/** Foreground-only discovery; each information request has its own bounded lifetime. */
class LanDiscovery(context: Context) {
    interface Listener {
        fun onService(computer: DiscoveredComputer)
        fun onStage(computerId: String?, status: String) {}
        fun onFinished()
    }
    private val app = context.applicationContext
    private val nsd = app.getSystemService(NsdManager::class.java)
    private val connectivity = app.getSystemService(ConnectivityManager::class.java)
    private val handler = Handler(Looper.getMainLooper())
    private val executor = Executor { handler.post(it) }
    private var generation = 0
    private var scanOpen = false
    private var finished = false
    private var listener: Listener? = null
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var deadline: Runnable? = null
    private var taskId = ""
    private val requests = mutableMapOf<String, Request>()
    private val seen = mutableSetOf<String>()
    private class Request(val info: NsdServiceInfo, val id: String?) {
        var callback: NsdManager.ServiceInfoCallback? = null
        var legacy: NsdManager.ResolveListener? = null
        var timeout: Runnable? = null
    }
    private fun id(info: NsdServiceInfo): String? =
        info.attributes[KEY_COMPUTER_ID]?.toString(Charsets.UTF_8)
            ?: Regex("stpc-[0-9a-f]{32}").find(info.serviceName.orEmpty())?.value

    private fun log(event: String, info: NsdServiceInfo? = null, code: String? = null, addresses: List<String> = emptyList()) {
        DiagnosticLogger.event(app, taskId, event, fields = mapOf(
            "computer_id" to info?.let(::id), "service_name" to info?.serviceName,
            "network" to if (Build.VERSION.SDK_INT >= 33) info?.network?.toString() else null,
            "ip_addresses" to addresses.joinToString(","), "error_code" to code, "stage" to event,
        ))
    }

    fun activeWifiNetwork(): Network? = connectivity.activeNetwork?.takeIf {
        connectivity.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true
    }

    fun start(target: Listener, durationMs: Long = 2500L) {
        stop()
        listener = target
        taskId = UUID.randomUUID().toString()
        finished = false
        val run = generation
        val network = activeWifiNetwork()
        if (network == null) {
            log("ADDRESS_RESOLVE_FAILED", code = "NO_WIFI_NETWORK")
            target.onStage(null, "未连接 Wi-Fi")
            finishIfIdle()
            return
        }
        scanOpen = true
        val callback = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(type: String) {
                if (run != generation) return
                if (!scanOpen) stopScan() else log("DISCOVERY_STARTED", code = "network=$network")
            }
            override fun onDiscoveryStopped(type: String) { if (run == generation) { discoveryListener = null; log("DISCOVERY_STOPPED"); finishIfIdle() } }
            override fun onStartDiscoveryFailed(type: String, error: Int) {
                if (run != generation) return
                discoveryListener = null
                log("ADDRESS_RESOLVE_FAILED", code = "DISCOVERY_START_$error")
                listener?.onStage(null, "局域网发现失败（$error）")
                stopScan()
            }
            override fun onStopDiscoveryFailed(type: String, error: Int) { if (run == generation) log("DISCOVERY_STOPPED", code = "STOP_FAILED_$error") }
            override fun onServiceFound(info: NsdServiceInfo) {
                if (run != generation || !scanOpen) return
                log("SERVICE_FOUND", info)
                val candidate = id(info)
                // The name/TXT ID only filters candidates; TLS authenticates identity later.
                if (candidate != null && PeerStore.list(app).none { it.computerId == candidate }) return
                resolve(info, run)
            }
            override fun onServiceLost(info: NsdServiceInfo) {
                if (run != generation) return
                log("SERVICE_LOST", info)
                if (requests.containsKey(info.serviceName)) {
                    listener?.onStage(id(info), "服务已离开网络")
                    closeRequest(info.serviceName)
                }
            }
        }
        discoveryListener = callback
        deadline = Runnable { if (run == generation) stopScan() }.also { handler.postDelayed(it, durationMs) }
        try {
            if (Build.VERSION.SDK_INT >= 35) nsd.discoverServices(DiscoveryRequest.Builder(SERVICE_TYPE).setNetwork(network).build(), executor, callback)
            else { @Suppress("DEPRECATION") nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, callback) }
        } catch (e: Exception) {
            log("ADDRESS_RESOLVE_FAILED", code = e.javaClass.simpleName)
            listener?.onStage(null, "局域网发现启动失败")
            stopScan()
        }
    }

    private fun resolve(info: NsdServiceInfo, run: Int) {
        val key = info.serviceName
        if (!seen.add(key)) return
        val request = Request(info, id(info))
        requests[key] = request
        listener?.onStage(request.id, "正在解析地址")
        request.timeout = Runnable {
            if (run == generation && requests[key] === request) {
                log("ADDRESS_RESOLVE_TIMEOUT", info, "TIMEOUT_5000MS")
                listener?.onStage(request.id, "地址解析超时")
                closeRequest(key)
            }
        }.also { handler.postDelayed(it, 5000L) }
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                val callback = object : NsdManager.ServiceInfoCallback {
                    override fun onServiceInfoCallbackRegistrationFailed(error: Int) {
                        if (run != generation || requests[key] !== request) return
                        log("SERVICE_INFO_CALLBACK_FAILED", info, error.toString())
                        request.callback = null
                        listener?.onStage(request.id, "地址解析失败（$error）")
                        closeRequest(key)
                    }
                    override fun onServiceUpdated(updated: NsdServiceInfo) {
                        if (run == generation && requests[key] === request) accept(updated, key)
                    }
                    override fun onServiceLost() {
                        if (run != generation || requests[key] !== request) return
                        log("SERVICE_LOST", info)
                        listener?.onStage(request.id, "服务已离开网络")
                        closeRequest(key)
                    }
                    override fun onServiceInfoCallbackUnregistered() = Unit
                }
                request.callback = callback
                nsd.registerServiceInfoCallback(info, executor, callback)
                log("SERVICE_INFO_CALLBACK_REGISTERED", info)
            } else {
                val callback = object : NsdManager.ResolveListener {
                    override fun onResolveFailed(service: NsdServiceInfo, error: Int) {
                        if (run != generation || requests[key] !== request) return
                        log("ADDRESS_RESOLVE_FAILED", service, error.toString())
                        listener?.onStage(request.id, "地址解析失败（$error）")
                        closeRequest(key)
                    }
                    override fun onServiceResolved(service: NsdServiceInfo) {
                        if (run == generation && requests[key] === request) accept(service, key)
                    }
                }
                request.legacy = callback
                @Suppress("DEPRECATION")
                if (Build.VERSION.SDK_INT >= 33) nsd.resolveService(info, executor, callback) else nsd.resolveService(info, callback)
                log("SERVICE_INFO_CALLBACK_REGISTERED", info, "LEGACY_RESOLVE")
            }
        } catch (e: Exception) {
            log("ADDRESS_RESOLVE_FAILED", info, e.javaClass.simpleName)
            listener?.onStage(request.id, "地址解析启动失败")
            closeRequest(key)
        }
    }

    private fun accept(info: NsdServiceInfo, key: String) {
        val addresses = if (Build.VERSION.SDK_INT >= 34) info.hostAddresses else listOfNotNull(info.host)
        log("SERVICE_INFO_UPDATED", info, addresses = addresses.mapNotNull { it.hostAddress })
        val host = addresses.filterIsInstance<Inet4Address>().firstOrNull { it.isSiteLocalAddress }?.hostAddress ?: return
        val props = info.attributes.mapValues { it.value.toString(Charsets.UTF_8) }
        val computerId = props[KEY_COMPUTER_ID] ?: return
        val protocol = props[KEY_PROTOCOL]?.toIntOrNull() ?: return
        if (protocol != 1 || info.port !in 1..65535 || PeerStore.list(app).none { it.computerId == computerId }) {
            log("ADDRESS_RESOLVE_FAILED", info, "UNPAIRED_OR_UNSUPPORTED_SERVICE")
            closeRequest(key)
            return
        }
        log("ADDRESS_RESOLVED", info, addresses = listOf(host))
        listener?.onService(DiscoveredComputer(computerId, props[KEY_DISPLAY_NAME]?.take(64)?.ifBlank { "我的电脑" } ?: "我的电脑", host, info.port, protocol))
        closeRequest(key)
    }

    private fun stopScan() {
        deadline?.let(handler::removeCallbacks)
        deadline = null
        scanOpen = false
        val current = discoveryListener
        discoveryListener = null
        current?.let {
            try { nsd.stopServiceDiscovery(it) } catch (e: Exception) { log("DISCOVERY_STOPPED", code = e.javaClass.simpleName) }
        }
        // Pending address callbacks keep their separate deadlines.
        finishIfIdle()
    }
    private fun closeRequest(key: String) {
        val request = requests.remove(key) ?: return
        request.timeout?.let(handler::removeCallbacks)
        if (Build.VERSION.SDK_INT >= 34) request.callback?.let {
            try { nsd.unregisterServiceInfoCallback(it) } catch (e: Exception) { log("SERVICE_INFO_CALLBACK_FAILED", request.info, "UNREGISTER_${e.javaClass.simpleName}") }
        }
        finishIfIdle()
    }
    private fun finishIfIdle() {
        if (!scanOpen && requests.isEmpty() && !finished) { finished = true; listener?.onFinished() }
    }
    fun stop() {
        generation++
        listener = null
        stopScan()
        requests.keys.toList().forEach(::closeRequest)
        discoveryListener = null
        seen.clear()
    }
    companion object {
        const val SERVICE_TYPE = "_sendtocopyq._tcp."
        const val KEY_COMPUTER_ID = "computer_id"
        const val KEY_PROTOCOL = "protocol"
        const val KEY_DISPLAY_NAME = "display_name"
    }
}
