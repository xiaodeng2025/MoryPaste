package cn.shouzhuang.sendtocopyq

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors

class ClientIdentityTest {
    @Test fun identityIsStableAcrossRecreationAndUpgradeLikeReuse() {
        val dir = Files.createTempDirectory("stcq-identity").toFile()
        try {
            val first = ClientIdentity.getOrCreate(dir)
            assertEquals(first, ClientIdentity.getOrCreate(dir))
            assertTrue(first.matches(Regex("[0-9a-f-]{36}")))
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun clearingAppPrivateStorageCreatesANewIdentity() {
        val dir = Files.createTempDirectory("stcq-identity-clear").toFile()
        try {
            val first = ClientIdentity.getOrCreate(dir)
            dir.listFiles()?.forEach(File::delete)
            val second = ClientIdentity.getOrCreate(dir)
            assertNotEquals(first, second)
        } finally {
            dir.deleteRecursively()
        }
    }

    @Test fun concurrentCallersReceiveOneIdentity() {
        val dir = Files.createTempDirectory("stcq-identity-concurrent").toFile()
        val pool = Executors.newFixedThreadPool(8)
        try {
            val start = CountDownLatch(1)
            val futures = (1..16).map {
                pool.submit<String> {
                    start.await()
                    ClientIdentity.getOrCreate(dir)
                }
            }
            start.countDown()
            val ids = futures.map { it.get() }.toSet()
            assertEquals(1, ids.size)
        } finally {
            pool.shutdownNow()
            dir.deleteRecursively()
        }
    }

    @Test fun completeTemporaryIdentityIsRecoveredAfterInterruptedPromotion() {
        val dir = Files.createTempDirectory("stcq-identity-recovery").toFile()
        try {
            val interruptedId = "01234567-89ab-cdef-0123-456789abcdef"
            File(dir, "client-instance-id.tmp").writeText(interruptedId, Charsets.US_ASCII)
            assertEquals(interruptedId, ClientIdentity.getOrCreate(dir))
            assertEquals(interruptedId, File(dir, "client-instance-id").readText(Charsets.US_ASCII))
        } finally {
            dir.deleteRecursively()
        }
    }
}
