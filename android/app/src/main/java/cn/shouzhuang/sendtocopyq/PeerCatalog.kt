package cn.shouzhuang.sendtocopyq

import java.security.MessageDigest

/** Pure list operations used by PeerStore and the home-screen ordering. */
internal object PeerCatalog {
    fun legacyComputerId(fingerprint: String): String {
        val material = "SendToCopyQ computer id v1\n${fingerprint.lowercase()}".toByteArray(Charsets.UTF_8)
        val suffix = MessageDigest.getInstance("SHA-256").digest(material).take(16)
            .joinToString("") { "%02x".format(it) }
        return "stpc-$suffix"
    }

    fun upsert(peers: List<PairedComputer>, incoming: PairedComputer): List<PairedComputer> {
        val index = peers.indexOfFirst { it.computerId == incoming.computerId }
        if (index < 0) return peers + incoming
        return peers.toMutableList().apply {
            val previous = this[index]
            this[index] = incoming.copy(
                pairedAt = previous.pairedAt,
                lastConnectedAt = previous.lastConnectedAt,
            )
        }
    }

    fun remove(peers: List<PairedComputer>, computerId: String): List<PairedComputer> =
        peers.filterNot { it.computerId == computerId }

    fun ordered(peers: List<PairedComputer>, reachableIds: Set<String>): List<PairedComputer> =
        peers.sortedWith(
            compareByDescending<PairedComputer> { it.computerId in reachableIds }
                .thenByDescending { it.lastConnectedAt },
        )

}

internal data class RecentShareTarget(
    val networkHandle: Long,
    val pairedIds: Set<String>,
    val peerId: String,
    val host: String,
    val port: Int,
    val capturedAt: Long,
) {
    fun peerFor(network: Long?, peers: List<PairedComputer>, now: Long): PairedComputer? {
        if (network != networkHandle || now - capturedAt !in 0..30_000L ||
            peers.mapTo(mutableSetOf()) { it.computerId } != pairedIds) return null
        return peers.firstOrNull { it.computerId == peerId }?.copy(host = host, port = port)
    }
}
