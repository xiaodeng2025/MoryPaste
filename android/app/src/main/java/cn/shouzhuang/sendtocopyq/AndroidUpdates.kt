package cn.shouzhuang.sendtocopyq

import java.io.File
import java.io.InputStream
import java.io.OutputStream
import java.net.URI
import java.security.MessageDigest
import javax.net.ssl.HttpsURLConnection

internal object AndroidUpdates {
    private const val RELEASES = "https://github.com/xiaodeng2025/MoryPaste/releases"
    private const val MANIFEST = "$RELEASES/download/android-stable/android-update.txt"
    private const val APK_LIMIT = 64 * 1024 * 1024L
    private const val APK_CACHE_AGE_MS = 24 * 60 * 60 * 1_000L
    private val versionLine = Regex("([1-9][0-9]{0,9})[ \\t]+((?:0|[1-9][0-9]{0,5})(?:\\.(?:0|[1-9][0-9]{0,5})){2})")
    private val checksumLine = Regex("([0-9a-fA-F]{64})[ \\t]+\\*?([^ \\t]+)")
    private val downloadHosts = setOf("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com")

    sealed interface Result {
        data object Unpublished : Result
        data object Current : Result
        data class Available(val versionCode: Int, val version: String) : Result {
            val releasePage get() = "$RELEASES/tag/android-v$version"
            val apkName get() = "MoryPaste-Android-$version.apk"
            val assetBase get() = "$RELEASES/download/android-v$version"
        }
    }

    fun inspectManifest(manifest: String, installedCode: Int): Result {
        val match = requireNotNull(versionLine.matchEntire(manifest.trim())) { "Android 更新信息格式无效" }
        val (rawCode, version) = match.destructured
        val versionCode = requireNotNull(rawCode.toIntOrNull()) { "Android 更新版本号无效" }
        return if (versionCode <= installedCode) Result.Current
        else Result.Available(versionCode, version)
    }

    fun check(installedCode: Int): Result = readGithub(MANIFEST, Result.Unpublished) { connection ->
        inspectManifest(readText(connection, 256), installedCode)
    }

    fun download(update: Result.Available, directory: File, progress: (Int?) -> Unit): File {
        val expected = readGithub("${update.assetBase}/SHA256SUMS.txt") { connection ->
            inspectChecksum(readText(connection, 4_096), update.apkName)
        }
        check(directory.isDirectory || directory.mkdirs()) { "无法创建更新缓存目录" }
        val staleBefore = System.currentTimeMillis() - APK_CACHE_AGE_MS
        directory.listFiles { file -> file.name.startsWith("morypaste-") && file.lastModified() < staleBefore }
            ?.forEach { it.delete() }
        val apk = File.createTempFile("morypaste-", ".apk", directory)
        var verified = false
        try {
            readGithub("${update.assetBase}/${update.apkName}", readTimeout = 15_000) { connection ->
                connection.inputStream.use { input ->
                    apk.outputStream().use { output ->
                        copyVerified(input, output, expected, connection.contentLengthLong, progress)
                    }
                }
            }
            verified = true
            return apk
        } finally {
            if (!verified) apk.delete()
        }
    }

    fun inspectChecksum(manifest: String, apkName: String): ByteArray {
        val hashes = manifest.lineSequence().mapNotNull { line ->
            checksumLine.matchEntire(line.trim())?.takeIf { it.groupValues[2] == apkName }?.groupValues?.get(1)
        }.toList()
        require(hashes.size == 1) { "未找到唯一的 APK 校验值" }
        return hashes.single().chunked(2).map { it.toInt(16).toByte() }.toByteArray()
    }

    fun copyVerified(
        input: InputStream, output: OutputStream, expected: ByteArray, length: Long, progress: (Int?) -> Unit,
    ) {
        require(length in -1..APK_LIMIT && length != 0L) { "安装包大小无效" }
        val digest = MessageDigest.getInstance("SHA-256")
        val buffer = ByteArray(32 * 1024)
        var total = 0L
        var lastProgress = -1
        if (length < 0) progress(null)
        while (true) {
            if (Thread.currentThread().isInterrupted) throw InterruptedException()
            val count = input.read(buffer)
            if (count < 0) break
            total += count
            require(total <= APK_LIMIT && (length < 0 || total <= length)) { "安装包大小无效" }
            output.write(buffer, 0, count)
            digest.update(buffer, 0, count)
            if (length > 0) {
                val percent = (total * 100 / length).toInt()
                if (percent != lastProgress) {
                    progress(percent)
                    lastProgress = percent
                }
            }
        }
        require(total > 0 && (length < 0 || total == length)) { "安装包下载不完整" }
        require(MessageDigest.isEqual(digest.digest(), expected)) { "安装包校验失败" }
    }

    private fun readText(connection: HttpsURLConnection, limit: Int): String = connection.inputStream.use { input ->
        val bytes = ByteArray(limit)
        var size = 0
        while (size < bytes.size) {
            val count = input.read(bytes, size, bytes.size - size)
            if (count < 0) break
            size += count
        }
        require(size < bytes.size) { "Android 更新信息超过大小限制" }
        String(bytes, 0, size, Charsets.UTF_8)
    }

    private fun <T> readGithub(
        address: String, missing: T? = null, readTimeout: Int = 4_000, read: (HttpsURLConnection) -> T,
    ): T {
        var url = URI(address)
        repeat(4) {
            val connection = url.toURL().openConnection() as HttpsURLConnection
            connection.instanceFollowRedirects = false
            connection.connectTimeout = 4_000
            connection.readTimeout = readTimeout
            try {
                when (val status = connection.responseCode) {
                    200 -> return read(connection)
                    404 -> if (missing != null && url.toString() == address) return missing else error("更新文件不可用")
                    301, 302, 303, 307, 308 -> url = redirect(url, connection.getHeaderField("Location"))
                    else -> error("更新服务返回 HTTP $status")
                }
            } finally {
                connection.disconnect()
            }
        }
        error("更新地址重定向次数过多")
    }

    fun redirect(current: URI, location: String?): URI {
        val destination = current.resolve(requireNotNull(location) { "更新地址缺失" })
        require(destination.scheme == "https" && destination.host in downloadHosts &&
            destination.userInfo == null && destination.port in setOf(-1, 443)) { "更新地址无效" }
        return destination
    }
}
