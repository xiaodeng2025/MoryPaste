package cn.shouzhuang.sendtocopyq

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import android.util.Base64
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.io.File
import java.io.InputStream
import java.net.InetAddress
import java.net.URL
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.HostnameVerifier
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocketFactory
import javax.net.ssl.X509TrustManager
import java.security.cert.X509Certificate

object SecureTransport {
    private const val MAX_IMAGE_BYTES = 50 * 1024 * 1024
    private const val MAX_FILE_BYTES = 4L * 1024 * 1024 * 1024
    private val mimePattern = Regex("[a-z0-9!#\$&^_.+-]+/[a-z0-9!#\$&^_.+-]+")

    data class ReceiveResult(val savedCount: Int, val imageCount: Int, val clipboard: String)

    fun sendFile(context: Context, peer: PairedComputer, uri: Uri, taskId: String, index: Int, total: Int,
                 progress: (Long, Long) -> Unit): ReceiveResult {
        val resolver = context.contentResolver
        val displayName = if (uri.scheme == "content") try {
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use {
                val column = it.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                if (column >= 0 && it.moveToFirst()) it.getString(column) else null
            }
        } catch (_: UnsupportedOperationException) {
            null
        } else null
        val name = (displayName ?: uri.lastPathSegment ?: "接收文件").takeLast(170)
        val mimeHint = resolver.getType(uri)?.substringBefore(';')?.trim()?.lowercase()
            ?.takeIf(mimePattern::matches) ?: "application/octet-stream"
        val temporary = File.createTempFile("morypaste-send-", ".part", context.cacheDir)
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            val buffer = ByteArray(128 * 1024)
            var length = 0L
            val initial = ByteArray(8)
            val content = resolver.openInputStream(uri) ?: error("无法读取分享的文件")
            content.use { input ->
                temporary.outputStream().use { output ->
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        if (length < initial.size) {
                            val offset = length.toInt()
                            buffer.copyInto(initial, offset, 0, minOf(count, initial.size - offset))
                        }
                        length += count
                        require(length <= MAX_FILE_BYTES) { "单个文件最多支持 4 GiB。" }
                        digest.update(buffer, 0, count)
                        output.write(buffer, 0, count)
                    }
                }
            }
            val mime = if (mimeHint.startsWith("image/") || mimeHint == "application/octet-stream") {
                detectMime(initial) ?: mimeHint
            } else mimeHint
            if (mime == "image/png" || mime == "image/jpeg") {
                require(length in 1..MAX_IMAGE_BYTES.toLong()) { "PNG 或 JPEG 图片最多支持 50 MiB。" }
            }
            return uploadFile(peer, temporary, mime, name, length, digest.digest().toHex(), taskId, index, total, progress)
        } finally {
            temporary.delete()
        }
    }

    private fun uploadFile(peer: PairedComputer, file: File, mime: String, name: String, length: Long,
                           hash: String, task: String, index: Int, total: Int, progress: (Long, Long) -> Unit): ReceiveResult {
        val encodedName = Base64.encodeToString(name.toByteArray(Charsets.UTF_8),
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING)
        val time = (System.currentTimeMillis() / 1000L).toString()
        val nonce = ByteArray(18).also { SecureRandom().nextBytes(it) }.toHex()
        val canonical = listOf(time, nonce, mime, length.toString(), hash, "/receive", task,
            index.toString(), total.toString(), encodedName).joinToString("\n")
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(peer.secret, "HmacSHA256"))
        val connection = openPinned("https://${peer.host}:${peer.port}/receive", peer.fingerprint, readTimeout = 65000)
        try {
            connection.requestMethod = "POST"
            connection.doOutput = true
            connection.setFixedLengthStreamingMode(length)
            connection.setRequestProperty("Content-Type", mime)
            connection.setRequestProperty("X-Device-Id", peer.deviceId)
            connection.setRequestProperty("X-Timestamp", time)
            connection.setRequestProperty("X-Nonce", nonce)
            connection.setRequestProperty("X-Content-SHA256", hash)
            connection.setRequestProperty("X-Signature", mac.doFinal(canonical.toByteArray(Charsets.UTF_8)).toHex())
            connection.setRequestProperty("X-Task-Id", task)
            connection.setRequestProperty("X-Image-Index", index.toString())
            connection.setRequestProperty("X-Image-Count", total.toString())
            connection.setRequestProperty("X-File-Name", encodedName)
            val buffer = ByteArray(128 * 1024)
            var sent = 0L
            var lastUpdate = 0L
            file.inputStream().use { input ->
                connection.outputStream.use { output ->
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        output.write(buffer, 0, count)
                        sent += count
                        val now = android.os.SystemClock.elapsedRealtime()
                        if (sent == length || now - lastUpdate >= 250) {
                            lastUpdate = now
                            progress(sent, length)
                        }
                    }
                }
            }
            val status = connection.responseCode
            val response = readResponse(connection)
            check(status == 200 && response.optBoolean("ok") && response.optString("taskId") == task) {
                response.optString("error", "电脑未确认文件已保存")
            }
            return ReceiveResult(response.getInt("savedCount"), response.getInt("imageCount"), response.getString("clipboard"))
        } finally {
            connection.disconnect()
        }
    }

    private fun ByteArray.toHex() = joinToString("") { "%02x".format(it) }

    data class PairResult(val peer: PairedComputer, val pairingStatus: String)

    fun readSharedImage(open: () -> InputStream?, mimeHint: String?): Pair<ByteArray, String> {
        val stream = open() ?: throw IllegalArgumentException("无法读取分享的图片")
        val output = ByteArrayOutputStream()
        stream.use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                if (output.size() + count > MAX_IMAGE_BYTES) throw IllegalArgumentException("图片超过 50 MiB")
                output.write(buffer, 0, count)
            }
        }
        val bytes = output.toByteArray()
        val mime = detectMime(bytes)
            ?: throw IllegalArgumentException("只支持 PNG 或 JPEG 图片。此图片格式为 ${mimeHint ?: "未知格式"}。")
        return bytes to mime
    }

    private fun detectMime(bytes: ByteArray): String? {
        if (bytes.size >= 8 && bytes.copyOfRange(0, 8).contentEquals(byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a))) return "image/png"
        if (bytes.size >= 3 && bytes[0] == 0xff.toByte() && bytes[1] == 0xd8.toByte() && bytes[2] == 0xff.toByte()) return "image/jpeg"
        return null
    }

    fun computerIdForPayload(payload: JSONObject, pin: String = payload.getString("certificateSha256").lowercase()): String =
        payload.optString("computerId").takeIf { it.matches(Regex("stpc-[0-9a-f]{32}")) }
            ?: computerIdForFingerprint(pin)

    fun pair(
        payload: JSONObject,
        deviceName: String,
        clientInstanceId: String,
        previousDeviceId: String?,
    ): PairResult {
        val host = payload.getString("host")
        val port = payload.getInt("port")
        val pin = payload.getString("certificateSha256").lowercase()
        val pairingCode = payload.getString("pairingCode")
        require(port in 1..65535 && pin.matches(Regex("[0-9a-f]{64}"))) { "配对二维码参数无效" }
        val address = InetAddress.getByName(host)
        require(address is java.net.Inet4Address && address.isSiteLocalAddress) { "二维码不是局域网电脑地址" }
        val body = JSONObject()
            .put("pairingCode", pairingCode)
            .put("deviceName", deviceName)
            .put("clientInstanceId", clientInstanceId)
            .apply { if (!previousDeviceId.isNullOrBlank()) put("previousDeviceId", previousDeviceId) }
            .toString()
            .toByteArray(Charsets.UTF_8)
        val connection = openPinned("https://$host:$port/pair/claim", pin)
        try {
            connection.requestMethod = "POST"
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            connection.setRequestProperty("Content-Length", body.size.toString())
            connection.outputStream.use { it.write(body) }
            val response = readResponse(connection)
            if (connection.responseCode !in 200..299) throw IllegalStateException(response.optString("error", "配对失败"))
            val id = response.getString("deviceId")
            val secret = Base64.decode(response.getString("secret"), Base64.NO_WRAP)
            require(secret.size == 32) { "电脑返回的设备凭据无效" }
            val pairingStatus = response.optString("pairingStatus", "created")
            require(pairingStatus == "created" || pairingStatus == "updated") { "电脑返回的配对状态无效" }
            val computerId = computerIdForPayload(payload, pin)
            val computerName = payload.optString("computerName").trim().take(64).ifBlank { "我的电脑" }
            return PairResult(PairedComputer(computerId, computerName, host, port, pin, id, secret), pairingStatus)
        } finally {
            connection.disconnect()
        }
    }

    fun verifyComputer(peer: PairedComputer, host: String, port: Int): Boolean {
        verifiedHealth(peer, host, port)
        return true
    }

    fun verifyFileCapability(peer: PairedComputer): Boolean =
        verifiedHealth(peer, peer.host, peer.port).optBoolean("fileReceive")

    private fun verifiedHealth(peer: PairedComputer, host: String, port: Int, network: android.net.Network? = null): JSONObject {
        val active = openPinned("https://$host:$port/health", peer.fingerprint, 2000, 2000, network)
        try {
            active.requestMethod = "GET"
            active.connect()
            if (active.responseCode !in 200..299) {
                throw IllegalStateException("电脑身份验证失败（HTTP ${active.responseCode}）")
            }
            val response = readResponse(active)
            if (!response.optBoolean("ok", false) ||
                response.optString("computerId") != peer.computerId ||
                response.optInt("protocolVersion", -1) != 1
            ) {
                throw IllegalStateException("电脑身份与配对记录不符，已拒绝连接")
            }
            return response
        } finally {
            active.disconnect()
        }
    }

    fun sendImage(context: Context, peer: PairedComputer, bytes: ByteArray, mime: String, taskId: String, imageIndex: Int, imageCount: Int): String {
        require(bytes.size in 1..MAX_IMAGE_BYTES) { "图片大小无效" }
        require(mime == "image/png" || mime == "image/jpeg") { "不支持的图片类型" }
        val timestamp = (System.currentTimeMillis() / 1000L).toString()
        val nonceBytes = ByteArray(18).also { SecureRandom().nextBytes(it) }
        val nonce = nonceBytes.joinToString("") { "%02x".format(it) }
        val bodyHash = sha256(bytes)
        val canonical = "$timestamp\n$nonce\n$mime\n${bytes.size}\n$bodyHash"
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(peer.secret, "HmacSHA256"))
        val signature = mac.doFinal(canonical.toByteArray(Charsets.UTF_8)).joinToString("") { "%02x".format(it) }
        val transferStartedAt = System.nanoTime()
        var bytesSent = 0
        var responseReceived = false
        var transferCompleted = false
        var connection: HttpsURLConnection? = null
        DiagnosticLogger.event(context, taskId, "CONNECTION_STARTED", fields = mapOf(
            "image_index" to imageIndex, "image_count" to imageCount, "mime" to mime, "bytes" to bytes.size,
        ))
        try {
            val active = openPinned("https://${peer.host}:${peer.port}/upload", peer.fingerprint)
            connection = active
            active.requestMethod = "POST"
            active.doOutput = true
            active.setFixedLengthStreamingMode(bytes.size)
            active.setRequestProperty("Content-Type", mime)
            active.setRequestProperty("X-Device-Id", peer.deviceId)
            active.setRequestProperty("X-Timestamp", timestamp)
            active.setRequestProperty("X-Nonce", nonce)
            active.setRequestProperty("X-Content-SHA256", bodyHash)
            active.setRequestProperty("X-Signature", signature)
            active.setRequestProperty("X-Task-Id", taskId)
            active.setRequestProperty("X-Image-Index", imageIndex.toString())
            active.setRequestProperty("X-Image-Count", imageCount.toString())
            val output = active.outputStream
            DiagnosticLogger.event(context, taskId, "CONNECTION_ESTABLISHED", fields = mapOf(
                "image_index" to imageIndex, "image_count" to imageCount, "elapsed_ms" to ((System.nanoTime() - transferStartedAt) / 1_000_000L),
            ))
            DiagnosticLogger.event(context, taskId, "TRANSFER_STARTED", fields = mapOf(
                "image_index" to imageIndex, "image_count" to imageCount, "mime" to mime, "bytes" to bytes.size,
            ))
            val chunkSize = 64 * 1024
            output.use { stream ->
                while (bytesSent < bytes.size) {
                    val count = minOf(chunkSize, bytes.size - bytesSent)
                    stream.write(bytes, bytesSent, count)
                    bytesSent += count
                    if (bytesSent == bytes.size || bytesSent % (1024 * 1024) < chunkSize) {
                        DiagnosticLogger.event(context, taskId, "TRANSFER_PROGRESS", fields = mapOf(
                            "image_index" to imageIndex,
                            "image_count" to imageCount,
                            "bytes_sent" to bytesSent,
                            "bytes_total" to bytes.size,
                        ))
                    }
                }
                stream.flush()
            }
            transferCompleted = true
            DiagnosticLogger.event(context, taskId, "TRANSFER_COMPLETED", fields = mapOf(
                "image_index" to imageIndex,
                "image_count" to imageCount,
                "mime" to mime,
                "bytes_sent" to bytesSent,
                "elapsed_ms" to ((System.nanoTime() - transferStartedAt) / 1_000_000L),
            ))
            val response = readResponse(active)
            responseReceived = true
            if (active.responseCode !in 200..299) {
                val error = IllegalStateException(response.optString("error", "电脑拒绝了图片"))
                DiagnosticLogger.event(context, taskId, "ACK_FAILED", level = "ERROR", status = "failed", fields = mapOf(
                    "image_index" to imageIndex, "image_count" to imageCount, "http_status" to active.responseCode,
                ), error = error)
                throw error
            }
            val acknowledgedTaskId = response.optString("taskId")
            if (!response.optBoolean("ok", false) || (acknowledgedTaskId.isNotEmpty() && acknowledgedTaskId != taskId)) {
                val error = IllegalStateException("电脑返回的任务确认信息不匹配")
                DiagnosticLogger.event(context, taskId, "ACK_FAILED", level = "ERROR", status = "failed", fields = mapOf(
                    "image_index" to imageIndex, "image_count" to imageCount,
                    "ack_task_id_matches" to (acknowledgedTaskId == taskId),
                ), error = error)
                throw error
            }
            DiagnosticLogger.event(context, taskId, "ACK_RECEIVED", fields = mapOf(
                "image_index" to imageIndex,
                "image_count" to imageCount,
                "http_status" to active.responseCode,
                "ack_ok" to response.optBoolean("ok", false),
                "ack_task_id_matches" to (acknowledgedTaskId.isEmpty() || acknowledgedTaskId == taskId),
                "elapsed_ms" to ((System.nanoTime() - transferStartedAt) / 1_000_000L),
            ))
            return response.optString("message", "图片已发送到电脑")
        } catch (error: Exception) {
            if (transferCompleted && !responseReceived) DiagnosticLogger.event(context, taskId, "ACK_FAILED", level = "ERROR", status = "failed", fields = mapOf(
                "image_index" to imageIndex,
                "image_count" to imageCount,
                "bytes_sent" to bytesSent,
                "elapsed_ms" to ((System.nanoTime() - transferStartedAt) / 1_000_000L),
            ), error = error)
            DiagnosticLogger.event(context, taskId, "TRANSFER_FAILED", level = "ERROR", status = "failed", fields = mapOf(
                "image_index" to imageIndex, "image_count" to imageCount, "bytes_sent" to bytesSent,
            ), error = error)
            throw error
        } finally {
            connection?.disconnect()
        }
    }

    const val MAX_CLIPBOARD_TEXT_BYTES = 32 * 1024

    fun supportsClipboardText(peer: PairedComputer, network: android.net.Network): Boolean =
        verifiedHealth(peer, peer.host, peer.port, network).optBoolean("clipboardTextReceive")

    fun sendClipboardText(peer: PairedComputer, network: android.net.Network, bytes: ByteArray) {
        require(bytes.size in 1..MAX_CLIPBOARD_TEXT_BYTES)
        val connection = openClipboardRequest(peer, network, "/clipboard/text",
            "application/vnd.morypaste.clipboard-text", bytes)
        try {
            connection.outputStream.use { it.write(bytes) }
            val response = readResponse(connection)
            when (connection.responseCode) {
                200 -> check(response.optBoolean("ok")) { "电脑未确认写入剪贴板，请在电脑上检查。" }
                401 -> error("这台电脑的授权已失效，请重新配对。")
                404 -> error("这台电脑还不支持接收剪贴板文字。")
                503 -> error("电脑剪贴板暂时不可用，请再试一次。")
                else -> error("电脑未确认写入剪贴板，请在电脑上检查。")
            }
        } finally {
            connection.disconnect()
        }
    }

    fun openClipboardSubscription(peer: PairedComputer, network: android.net.Network): HttpsURLConnection =
        openClipboardRequest(peer, network, "/clipboard/subscribe", "application/vnd.morypaste.clipboard-subscription", ByteArray(0))

    private fun openClipboardRequest(peer: PairedComputer, network: android.net.Network, path: String,
                                     mime: String, body: ByteArray): HttpsURLConnection {
        val timestamp = (System.currentTimeMillis() / 1000L).toString()
        val nonce = ByteArray(18).also { SecureRandom().nextBytes(it) }.toHex()
        val hash = sha256(body)
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(peer.secret, "HmacSHA256"))
        val signature = mac.doFinal("$timestamp\n$nonce\n$mime\n${body.size}\n$hash".toByteArray(Charsets.UTF_8)).toHex()
        return openPinned("https://${peer.host}:${peer.port}$path", peer.fingerprint,
            connectTimeout = 2500, readTimeout = 7000, network = network).apply {
            requestMethod = "POST"
            doOutput = true
            setFixedLengthStreamingMode(body.size)
            setRequestProperty("Content-Type", mime)
            setRequestProperty("X-Device-Id", peer.deviceId)
            setRequestProperty("X-Timestamp", timestamp)
            setRequestProperty("X-Nonce", nonce)
            setRequestProperty("X-Content-SHA256", hash)
            setRequestProperty("X-Signature", signature)
        }
    }

    private fun openPinned(url: String, expectedFingerprint: String, connectTimeout: Int = 7000, readTimeout: Int = 20000, network: android.net.Network? = null): HttpsURLConnection {
        val manager = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) = Unit
            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
                require(!chain.isNullOrEmpty()) { "电脑没有提供 TLS 证书" }
                val actual = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded)
                    .joinToString("") { "%02x".format(it) }
                require(MessageDigest.isEqual(actual.toByteArray(), expectedFingerprint.toByteArray())) {
                    "电脑证书指纹不匹配，已拒绝连接"
                }
            }
        }
        val context = SSLContext.getInstance("TLS")
        context.init(null, arrayOf(manager), SecureRandom())
        val endpoint = URL(url)
        return ((network?.openConnection(endpoint) ?: endpoint.openConnection()) as HttpsURLConnection).apply {
            this.connectTimeout = connectTimeout
            this.readTimeout = readTimeout
            instanceFollowRedirects = false
            sslSocketFactory = context.socketFactory
            hostnameVerifier = HostnameVerifier { _, _ -> true }
        }
    }

    private fun readResponse(connection: HttpsURLConnection): JSONObject {
        val source = if (connection.responseCode in 200..299) connection.inputStream else connection.errorStream
        val bytes = source?.use { input -> readAtMost(input, 16 * 1024) } ?: ByteArray(0)
        return try { JSONObject(bytes.toString(Charsets.UTF_8)) } catch (_: Exception) { JSONObject() }
    }

    private fun readAtMost(input: InputStream, limit: Int): ByteArray {
        val output = ByteArrayOutputStream()
        val buffer = ByteArray(2048)
        while (true) {
            val count = input.read(buffer)
            if (count < 0) break
            if (output.size() + count > limit) throw IllegalStateException("电脑响应过大")
            output.write(buffer, 0, count)
        }
        return output.toByteArray()
    }

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256")
        .digest(bytes).joinToString("") { "%02x".format(it) }

    private fun computerIdForFingerprint(fingerprint: String): String {
        val material = "SendToCopyQ computer id v1\n${fingerprint.lowercase()}".toByteArray(Charsets.UTF_8)
        val suffix = MessageDigest.getInstance("SHA-256").digest(material).take(16)
            .joinToString("") { "%02x".format(it) }
        return "stpc-$suffix"
    }
}
