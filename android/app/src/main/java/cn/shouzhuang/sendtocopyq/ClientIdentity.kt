package cn.shouzhuang.sendtocopyq

import android.content.Context
import java.io.File
import java.nio.channels.FileChannel
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.nio.file.StandardOpenOption
import java.util.UUID

/** A random per-installation identity, kept outside Android backup and restore. */
internal object ClientIdentity {
    private const val ID_FILE = "client-instance-id"
    private const val LOCK_FILE = "client-instance-id.lock"
    private const val TEMP_FILE = "client-instance-id.tmp"
    private val processLock = Any()
    private val validId = Regex("[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    fun get(context: Context): String = getOrCreate(context.noBackupFilesDir)

    internal fun getOrCreate(directory: File): String = synchronized(processLock) {
        check(directory.isDirectory || directory.mkdirs()) { "无法创建手机身份存储目录" }
        val base = File(directory, ID_FILE)
        val temp = File(directory, TEMP_FILE)
        FileChannel.open(
            File(directory, LOCK_FILE).toPath(),
            StandardOpenOption.CREATE,
            StandardOpenOption.WRITE,
        ).use { channel ->
            channel.lock().use {
                readValid(base)?.let { return@synchronized it }
                // Recover a fully written temporary value if the process stopped before its atomic rename.
                val recovered = readValid(temp)
                val id = recovered ?: UUID.randomUUID().toString()
                if (recovered == null) {
                    FileChannel.open(
                        temp.toPath(),
                        StandardOpenOption.CREATE,
                        StandardOpenOption.TRUNCATE_EXISTING,
                        StandardOpenOption.WRITE,
                    ).use { output ->
                        output.write(java.nio.ByteBuffer.wrap(id.toByteArray(Charsets.US_ASCII)))
                        output.force(true)
                    }
                }
                try {
                    Files.move(temp.toPath(), base.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING)
                } catch (_: AtomicMoveNotSupportedException) {
                    Files.move(temp.toPath(), base.toPath(), StandardCopyOption.REPLACE_EXISTING)
                }
                check(readValid(base) == id) { "手机身份写入校验失败" }
                id
            }
        }
    }

    private fun readValid(file: File): String? = try {
        file.readText(Charsets.US_ASCII).trim().takeIf(validId::matches)
    } catch (_: Exception) {
        null
    }
}
