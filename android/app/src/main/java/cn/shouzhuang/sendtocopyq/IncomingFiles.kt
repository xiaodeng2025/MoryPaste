package cn.shouzhuang.sendtocopyq

import android.content.ContentValues
import android.content.Context
import android.net.Network
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.webkit.MimeTypeMap
import androidx.core.content.FileProvider
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.util.Locale
import java.util.UUID

internal object IncomingFiles {
    data class Item(val name: String, val size: Long) {
        val mime: String
            get() = MimeTypeMap.getSingleton().getMimeTypeFromExtension(
                name.substringAfterLast('.', "").lowercase(Locale.ROOT)
            ) ?: "application/octet-stream"
    }

    data class Offer(val peer: PairedComputer, val network: Network, val id: String, val files: List<Item>)
    val DOWNLOAD_PATH = "${Environment.DIRECTORY_DOWNLOADS}/MoryPaste"

    suspend fun poll(peer: PairedComputer, network: Network): Offer? {
        val response = request(peer, network, JSONObject().put("action", "poll")) { connection ->
            connection.inputStream.bufferedReader(Charsets.UTF_8).use { JSONObject(it.readText()) }
        }
        val offer = response.optJSONObject("offer") ?: return null
        val files = offer.getJSONArray("files")
        require(files.length() in 1..20)
        val items = List(files.length()) { index ->
            val file = files.getJSONObject(index)
            Item(file.getString("name"), file.getLong("size")).also {
                require(it.name.isNotBlank() && it.size in 0..(4L * 1024 * 1024 * 1024))
            }
        }
        return Offer(peer, network, UUID.fromString(offer.getString("id")).toString(), items)
    }

    suspend fun respond(offer: Offer, action: String) {
        request(offer.peer, offer.network, payload(offer, action)) { connection ->
            connection.inputStream.use { check(JSONObject(it.bufferedReader().readText()).optBoolean("ok")) }
        }
    }

    suspend fun receive(context: Context, offer: Offer): List<Uri> {
        val resolver = context.contentResolver
        val directory = File(context.filesDir, "received-files/${offer.id}")
        val saved = mutableListOf<Uri>()
        val modernStorage = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
        respond(offer, "accept")
        try {
            if (!modernStorage) check(directory.mkdirs())
            for ((index, item) in offer.files.withIndex()) {
                val localFile = File(directory, index.toString())
                val uri = if (modernStorage) {
                    resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, ContentValues().apply {
                        put(MediaStore.Downloads.DISPLAY_NAME, item.name)
                        put(MediaStore.Downloads.MIME_TYPE, item.mime)
                        put(MediaStore.Downloads.RELATIVE_PATH, DOWNLOAD_PATH)
                        put(MediaStore.Downloads.IS_PENDING, 1)
                    }) ?: error("无法创建接收文件")
                } else {
                    FileProvider.getUriForFile(context, "${context.packageName}.updates", localFile, item.name)
                }
                saved += uri
                request(offer.peer, offer.network, payload(offer, "download").put("index", index)) { connection ->
                    val stream = if (modernStorage) resolver.openOutputStream(uri) else localFile.outputStream()
                    (stream ?: error("无法保存文件")).use { output ->
                        connection.inputStream.use { input ->
                            val buffer = ByteArray(128 * 1024)
                            var remaining = item.size
                            while (remaining > 0) {
                                currentCoroutineContext().ensureActive()
                                val count = input.read(buffer, 0, minOf(buffer.size.toLong(), remaining).toInt())
                                check(count > 0) { "文件未接收完整" }
                                output.write(buffer, 0, count)
                                remaining -= count
                            }
                            check(input.read() == -1) { "收到的文件大小不符" }
                        }
                    }
                }
            }
            if (modernStorage) for (uri in saved) {
                check(resolver.update(uri, ContentValues().apply {
                    put(MediaStore.Downloads.IS_PENDING, 0)
                }, null, null) == 1) { "无法保存文件" }
            }
        } catch (error: Exception) {
            if (modernStorage) saved.forEach { runCatching { resolver.delete(it, null, null) } }
            else directory.deleteRecursively()
            withContext(NonCancellable) { runCatching { respond(offer, "fail") } }
            throw error
        }
        if (runCatching { respond(offer, "complete") }.isFailure) {
            runCatching { respond(offer, "complete") }
        }
        return saved
    }

    private fun payload(offer: Offer, action: String) =
        JSONObject().put("action", action).put("id", offer.id)

    private suspend fun <T> request(
        peer: PairedComputer, network: Network, body: JSONObject,
        read: suspend (HttpURLConnection) -> T,
    ): T {
        repeat(5) {
            currentCoroutineContext().ensureActive()
            val connection = SecureTransport.outgoingRequest(peer, network, body)
            try {
                when (connection.responseCode) {
                    200 -> return read(connection)
                    425 -> Unit // The previous response is finishing on the computer.
                    409 -> error("传送已失效或电脑无法读取文件，请重新发送。")
                    else -> error("无法与电脑确认传送，请检查连接后重试。")
                }
            } finally {
                connection.disconnect()
            }
            delay(100)
        }
        error("电脑未确认传送，请重试。")
    }
}
