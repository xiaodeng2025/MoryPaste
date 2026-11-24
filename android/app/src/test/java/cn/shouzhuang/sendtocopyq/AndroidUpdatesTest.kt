package cn.shouzhuang.sendtocopyq

import java.net.URI
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class AndroidUpdatesTest {
    @Test
    fun onlyAHigherVersionCodeOffersAnUpdate() {
        assertEquals(AndroidUpdates.Result.Current, AndroidUpdates.inspectManifest("16 0.5.5\n", 16))
        assertEquals(AndroidUpdates.Result.Current, AndroidUpdates.inspectManifest("15 0.5.4", 16))
        assertEquals(
            AndroidUpdates.Result.Available(17, "0.5.10"),
            AndroidUpdates.inspectManifest("17\t0.5.10", 16),
        )
    }

    @Test
    fun onlyUsesTheChecksumForTheExactApkName() {
        val zero = "00".repeat(32)
        val ff = "FF".repeat(32)
        val apk = "MoryPaste-Android-0.5.10.apk"
        assertArrayEquals(ByteArray(32) { -1 }, AndroidUpdates.inspectChecksum("$zero  other.apk\n$ff *$apk", apk))
        for (value in listOf("$zero  other.apk", "$zero  $apk\n$ff  $apk", "short  $apk")) {
            assertThrows(IllegalArgumentException::class.java) { AndroidUpdates.inspectChecksum(value, apk) }
        }
    }

    @Test
    fun streamsAndChecksAllDownloadedBytesWithOrWithoutContentLength() {
        val bytes = ByteArray(80_000) { it.toByte() }
        val hash = MessageDigest.getInstance("SHA-256").digest(bytes)
        for (length in listOf(bytes.size.toLong(), -1L)) {
            val output = ByteArrayOutputStream()
            val progress = mutableListOf<Int?>()
            AndroidUpdates.copyVerified(bytes.inputStream(), output, hash, length, progress::add)
            assertArrayEquals(bytes, output.toByteArray())
            assertEquals(if (length < 0) null else 100, progress.last())
        }
    }

    @Test
    fun rejectsTamperingTruncationAndInvalidSizes() {
        val bytes = "test".toByteArray()
        val hash = MessageDigest.getInstance("SHA-256").digest(bytes)
        for (length in listOf(0L, 1L, 8L, 65 * 1024 * 1024L)) {
            assertThrows(IllegalArgumentException::class.java) {
                AndroidUpdates.copyVerified(bytes.inputStream(), ByteArrayOutputStream(), hash, length) {}
            }
        }
        assertThrows(IllegalArgumentException::class.java) {
            AndroidUpdates.copyVerified("fake".byteInputStream(), ByteArrayOutputStream(), hash, 4) {}
        }
    }

    @Test
    fun rejectsMalformedManifestsAndEmbeddedDownloadUrls() {
        for (value in listOf("", "0 0.5.5", "2147483648 0.5.5", "17 0.5.6-beta", "17 0.5.6 https://example.com")) {
            assertThrows(IllegalArgumentException::class.java) { AndroidUpdates.inspectManifest(value, 16) }
        }
    }

    @Test
    fun redirectsStayOnHttpsGitHubDownloadHosts() {
        val current = URI("https://github.com/xiaodeng2025/MoryPaste/releases/download/android-stable/android-update.txt")
        val asset = "https://release-assets.githubusercontent.com/github-production-release-asset/example?test=1"
        assertEquals(URI(asset), AndroidUpdates.redirect(current, asset))
        assertEquals(URI("https://github.com/next"), AndroidUpdates.redirect(current, "/next"))
        for (location in listOf(null, "http://github.com/file", "https://github.com.example.com/file", "https://user@github.com/file", "https://github.com:444/file")) {
            assertThrows(IllegalArgumentException::class.java) { AndroidUpdates.redirect(current, location) }
        }
    }
}
