package cn.shouzhuang.sendtocopyq

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONArray
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class PairedComputer(
    val computerId: String,
    val name: String,
    val host: String,
    val port: Int,
    val fingerprint: String,
    val deviceId: String,
    val secret: ByteArray,
    val pairedAt: Long = System.currentTimeMillis(),
    val lastConnectedAt: Long = 0L,
)

object PeerStore {
    private const val PREFS = "paired_computer"
    private const val KEY_ALIAS = "send_to_copyq_secret_v1"
    private const val SECRET_TRANSFORMATION = "AES/GCM/NoPadding"
    private const val PEERS_KEY = "peers_v2"
    private const val LEGACY_KEY = "peer"

    @Synchronized
    fun list(context: Context): List<PairedComputer> {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val serialized = prefs.getString(PEERS_KEY, null)
        if (serialized != null) {
            return try {
                val array = JSONArray(serialized)
                buildList {
                    for (index in 0 until array.length()) {
                        decodePeer(array.getJSONObject(index))?.let(::add)
                    }
                }
            } catch (_: Exception) {
                emptyList()
            }
        }
        val legacy = prefs.getString(LEGACY_KEY, null) ?: return emptyList()
        return try {
            val peer = decodePeer(JSONObject(legacy), legacy = true) ?: return emptyList()
            saveList(context, listOf(peer))
            listOf(peer)
        } catch (_: Exception) {
            emptyList()
        }
    }

    @Synchronized
    fun deviceIdForComputer(context: Context, computerId: String): String? =
        list(context).firstOrNull { it.computerId == computerId }?.deviceId

    @Synchronized
    fun add(context: Context, peer: PairedComputer) {
        val peers = list(context).toMutableList()
        check(saveList(context, PeerCatalog.upsert(peers, peer)))
    }

    @Synchronized
    fun updateConnection(
        context: Context,
        computerId: String,
        host: String,
        port: Int,
        name: String? = null,
        connectedAt: Long = System.currentTimeMillis(),
    ) {
        val peers = list(context).map { peer ->
            if (peer.computerId == computerId) peer.copy(
                name = name?.take(64)?.ifBlank { peer.name } ?: peer.name,
                host = host,
                port = port,
                lastConnectedAt = connectedAt,
            ) else peer
        }
        check(saveList(context, peers))
    }

    @Synchronized
    fun remove(context: Context, computerId: String) {
        val peers = PeerCatalog.remove(list(context), computerId)
        check(saveList(context, peers))
    }

    private fun saveList(context: Context, peers: List<PairedComputer>): Boolean {
        val array = JSONArray()
        peers.forEach { peer -> array.put(encodePeer(peer)) }
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(PEERS_KEY, array.toString()).commit()
    }

    private fun encodePeer(peer: PairedComputer): JSONObject {
        val cipher = Cipher.getInstance(SECRET_TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        val packed = cipher.iv + cipher.doFinal(peer.secret)
        return JSONObject()
            .put("computerId", peer.computerId)
            .put("name", peer.name)
            .put("host", peer.host)
            .put("port", peer.port)
            .put("fingerprint", peer.fingerprint)
            .put("deviceId", peer.deviceId)
            .put("secret", Base64.encodeToString(packed, Base64.NO_WRAP))
            .put("pairedAt", peer.pairedAt)
            .put("lastConnectedAt", peer.lastConnectedAt)
    }

    private fun decodePeer(json: JSONObject, legacy: Boolean = false): PairedComputer? {
        val packed = Base64.decode(json.getString("secret"), Base64.NO_WRAP)
        require(packed.size > 12)
        val cipher = Cipher.getInstance(SECRET_TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(128, packed.copyOfRange(0, 12)))
        val fingerprint = json.getString("fingerprint").lowercase()
        return PairedComputer(
            computerId = json.optString("computerId").takeIf { it.isNotBlank() } ?: PeerCatalog.legacyComputerId(fingerprint),
            name = json.optString("name").takeIf { it.isNotBlank() } ?: "我的电脑",
            host = json.getString("host"),
            port = json.getInt("port"),
            fingerprint = fingerprint,
            deviceId = json.getString("deviceId"),
            secret = cipher.doFinal(packed.copyOfRange(12, packed.size)),
            pairedAt = json.optLong("pairedAt", System.currentTimeMillis()),
            lastConnectedAt = json.optLong("lastConnectedAt", 0L),
        )
    }

    private fun getOrCreateKey(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        val existing = store.getKey(KEY_ALIAS, null) as? SecretKey
        if (existing != null) return existing
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(KEY_ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .build(),
        )
        return generator.generateKey()
    }
}
