package cn.shouzhuang.sendtocopyq

import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/** Private, local-only JSONL diagnostics. Never accepts payload bytes, credentials, or full URIs. */
object DiagnosticLogger {
    private const val PREFS = "diagnostics"
    private const val MAX_FILE_BYTES = 1024L * 1024L
    private const val MAX_TOTAL_BYTES = 5L * 1024L * 1024L
    private const val RETENTION_DAYS = 7L
    private const val PROTOCOL_VERSION = "1"
    private val allowedFields = setOf(
        "action", "intent_mime", "read_grant_flag", "clip_item_count", "image_count", "image_index",
        "uri_scheme", "read_granted", "mime_hint", "mime", "bytes", "bytes_sent", "bytes_total",
        "elapsed_ms", "http_status", "ack_ok", "ack_task_id_matches", "error_code", "attempt",
        "cached_for_retry", "computer_id", "service_name", "network", "ip_addresses", "stage",
    )

    @Synchronized
    fun event(
        context: Context,
        taskId: String?,
        name: String,
        level: String = "INFO",
        status: String = "ok",
        fields: Map<String, Any?> = emptyMap(),
        error: Throwable? = null,
    ) {
        try {
            val now = Instant.now()
            val record = JSONObject()
                .put("timestamp", now.toString())
                .put("task_id", taskId ?: JSONObject.NULL)
                .put("event", name)
                .put("level", level)
                .put("status", status)
                .put("app_version", BuildConfig.VERSION_NAME)
                .put("protocol_version", PROTOCOL_VERSION)
                .put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
            fields.forEach { (key, value) ->
                if (key in allowedFields && value != null) record.put(key, sanitizeField(key, value))
            }
            if (error != null) {
                record.put("error_code", error.javaClass.simpleName)
                record.put("exception_message", sanitize(error.message.orEmpty()))
                val frames = JSONArray()
                error.stackTrace.take(10).forEach { frame ->
                    frames.put(JSONObject()
                        .put("class", frame.className.take(160))
                        .put("method", frame.methodName.take(100))
                        .put("file", frame.fileName?.substringAfterLast('/')?.substringAfterLast('\\')?.take(100) ?: "")
                        .put("line", frame.lineNumber))
                }
                record.put("stack", frames)
            }
            val dir = directory(context)
            val day = LocalDate.now(ZoneOffset.UTC).toString()
            var file = File(dir, "android-$day.jsonl")
            var suffix = 1
            while (file.exists() && file.length() + record.toString().toByteArray(Charsets.UTF_8).size + 1 > MAX_FILE_BYTES) {
                file = File(dir, "android-$day-$suffix.jsonl")
                suffix++
            }
            file.appendText(record.toString() + "\n", Charsets.UTF_8)
            prune(dir)
        } catch (_: Exception) {
            // Diagnostics must never interrupt or alter image delivery.
        }
    }

    fun rememberResult(context: Context, taskId: String, status: String, message: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("latest", JSONObject()
                .put("task_id", taskId)
                .put("status", status)
                .put("message", message.take(240))
                .put("timestamp", Instant.now().toString()).toString())
            .apply()
    }

    fun latestSummary(context: Context): String {
        val raw = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("latest", null)
            ?: return "尚无发送记录"
        return try {
            val item = JSONObject(raw)
            val result = if (item.optString("status") == "success") "成功" else "未完成"
            val time = item.optString("timestamp").replace('T', ' ').take(16)
            "$result · $time\n任务 ID：${item.optString("task_id")}"
        } catch (_: Exception) {
            "尚无发送记录"
        }
    }

    @Synchronized
    fun exportZip(context: Context): ByteArray {
        val dir = directory(context)
        prune(dir)
        val files = dir.listFiles()?.filter { it.isFile && it.name.endsWith(".jsonl") }.orEmpty()
        val latest = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("latest", null)
        val manifest = JSONObject()
            .put("application", "SendToCopyQ Android")
            .put("app_version", BuildConfig.VERSION_NAME)
            .put("protocol_version", PROTOCOL_VERSION)
            .put("device_model", "${Build.MANUFACTURER} ${Build.MODEL}".trim())
            .put("exported_at", Instant.now().toString())
            .put("latest_result", latest?.let { JSONObject(it) } ?: JSONObject.NULL)
        val output = java.io.ByteArrayOutputStream()
        ZipOutputStream(output).use { zip ->
            zip.putNextEntry(ZipEntry("manifest.json"))
            zip.write(manifest.toString(2).toByteArray(Charsets.UTF_8))
            zip.closeEntry()
            files.sortedBy { it.name }.forEach { file ->
                zip.putNextEntry(ZipEntry("android/${file.name}"))
                file.inputStream().use { it.copyTo(zip) }
                zip.closeEntry()
            }
        }
        return output.toByteArray()
    }

    private fun directory(context: Context): File = File(context.filesDir, "diagnostics").apply { mkdirs() }

    private fun prune(dir: File) {
        val files = dir.listFiles()?.filter { it.isFile && it.name.endsWith(".jsonl") }?.sortedBy { it.lastModified() }.orEmpty()
        val oldestAllowed = System.currentTimeMillis() - RETENTION_DAYS * 24L * 60L * 60L * 1000L
        files.filter { it.lastModified() < oldestAllowed }.forEach { it.delete() }
        var total = dir.listFiles()?.filter { it.isFile && it.name.endsWith(".jsonl") }?.sumOf { it.length() } ?: 0L
        (dir.listFiles()?.filter { it.isFile && it.name.endsWith(".jsonl") }?.sortedBy { it.lastModified() }.orEmpty())
            .forEach { file ->
                if (total > MAX_TOTAL_BYTES) {
                    val size = file.length()
                    if (file.delete()) total -= size
                }
            }
    }

    private fun sanitize(value: String): String = value
        .replace(Regex("(?i)(content|file)://\\S+"), "<uri>")
        .replace(Regex("(?i)(token|secret|signature|pairingCode)[=: ]+[^\\s,;]+"), "$1=<redacted>")
        .take(600)

    private fun sanitizeField(key: String, value: Any): Any {
        if (key !in setOf("action", "intent_mime", "mime_hint", "mime", "uri_scheme", "error_code")) return value
        return sanitize(value.toString()).take(120)
    }
}
