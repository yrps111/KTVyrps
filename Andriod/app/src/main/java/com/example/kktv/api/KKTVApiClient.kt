package com.example.kktv.api

import com.example.kktv.model.*
import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.TimeUnit

class KKTVApiClient {

    private val gson = Gson()
    private val client = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .writeTimeout(10, TimeUnit.SECONDS)
        .build()

    // ★ 新增：短超时client，专给QR码和健康检查用
    private val quickClient = OkHttpClient.Builder()
        .connectTimeout(3, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .writeTimeout(3, TimeUnit.SECONDS)
        .build()

    //"192.168.110.37"
    var serverHost: String = ""   // ★ 改：去掉硬编码IP
    var serverPort: Int = 8080

    private val baseUrl: String
        get() = "http://$serverHost:$serverPort"

    private suspend fun get(path: String): String? = withContext(Dispatchers.IO) {
        if (serverHost.isEmpty()) return@withContext null
        try {
            val request = Request.Builder().url("$baseUrl$path").get().build()
            val response = client.newCall(request).execute()
            if (response.isSuccessful) response.body?.string() else null
        } catch (e: IOException) { null }
    }

    private suspend fun post(path: String, jsonBody: String = "{}"): String? =
        withContext(Dispatchers.IO) {
            if (serverHost.isEmpty()) return@withContext null
            try {
                val body = jsonBody.toRequestBody("application/json".toMediaType())
                val request = Request.Builder().url("$baseUrl$path").post(body).build()
                val response = client.newCall(request).execute()
                if (response.isSuccessful) response.body?.string() else null
            } catch (e: IOException) { null }
        }

    // ★ 修改：健康检查用短超时
    suspend fun checkHealth(): HealthResponse? {
        val json = quickGet("/api/health") ?: return null
        return try { gson.fromJson(json, HealthResponse::class.java) } catch (_: Exception) { null }
    }

    // ★ 新增：短超时的GET
    private suspend fun quickGet(path: String): String? = withContext(Dispatchers.IO) {
        if (serverHost.isEmpty()) return@withContext null
        try {
            val request = Request.Builder().url("$baseUrl$path").get().build()
            val response = quickClient.newCall(request).execute()
            if (response.isSuccessful) response.body?.string() else null
        } catch (e: IOException) { null }
    }

    suspend fun isServerOnline(): Boolean {
        if (serverHost.isEmpty()) return false
        return checkHealth()?.server == "online"
    }

    suspend fun getNextSong(): NextSongResponse? {
        val json = post("/api/tv/next") ?: return null
        return try { gson.fromJson(json, NextSongResponse::class.java) } catch (_: Exception) { null }
    }

    suspend fun reportFinished(): NextSongResponse? {
        val json = post("/api/tv/finished") ?: return null
        return try { gson.fromJson(json, NextSongResponse::class.java) } catch (_: Exception) { null }
    }

    suspend fun getQueue(): QueueResponse? {
        val json = get("/api/queue") ?: return null
        return try { gson.fromJson(json, QueueResponse::class.java) } catch (_: Exception) { null }
    }

// KKTVApiClient.kt 中替换原有的 discoverServer

    /**
     * UDP广播发现KKTV后端服务器
     * 发送discover请求并等待回复，支持超时
     */
    suspend fun discoverServer(timeoutMs: Long = 3000): Pair<String, Int>? =
        withContext(Dispatchers.IO) {
            try {
                val socket = java.net.DatagramSocket()
                socket.broadcast = true
                socket.soTimeout = timeoutMs.toInt()

                val magic = "KKTV_DISCOVER".toByteArray()
                val broadcastAddr = java.net.InetAddress.getByName("255.255.255.255")
                val packet = java.net.DatagramPacket(
                    magic, magic.size, broadcastAddr, 8081
                )
                socket.send(packet)

                val buf = ByteArray(1024)
                val response = java.net.DatagramPacket(buf, buf.size)
                socket.receive(response)
                socket.close()

                val json = String(response.data, 0, response.length)
                parseDiscoveryResponse(json)
            } catch (e: Exception) {
                null
            }
        }

    /**
     * 监听后端主动广播的ANNOUNCE消息
     * 用于处理"TV先开、后端后启动"的场景
     */
    suspend fun listenForAnnounce(timeoutMs: Long = 10000): Pair<String, Int>? =
        withContext(Dispatchers.IO) {
            try {
                val socket = java.net.DatagramSocket(8082)
                socket.broadcast = true
                socket.soTimeout = timeoutMs.toInt()

                val buf = ByteArray(1024)
                val packet = java.net.DatagramPacket(buf, buf.size)
                socket.receive(packet)
                socket.close()

                val raw = String(packet.data, 0, packet.length)
                if (raw.startsWith("KKTV_ANNOUNCE|")) {
                    val json = raw.substringAfter("KKTV_ANNOUNCE|")
                    parseDiscoveryResponse(json)
                } else null
            } catch (e: Exception) {
                null
            }
        }

    private fun parseDiscoveryResponse(json: String): Pair<String, Int>? {
        return try {
            val data = gson.fromJson(json, Map::class.java)
            val host = data["host"] as? String ?: return null
            val port = (data["port"] as? Double)?.toInt() ?: 8080
            Pair(host, port)
        } catch (e: Exception) {
            null
        }
    }


    // ★ FIX: 点歌台用的切歌（设skip标记通知TV）
    suspend fun skipSong(): Boolean {
        val json = get("/api/queue/skip") ?: return false
        return try {
            val map = gson.fromJson(json, Map::class.java)
            map["ok"] == true
        } catch (_: Exception) { false }
    }

    // ★ FIX: TV本地切歌——不设skip标记，避免双跳
    suspend fun tvSkip(): Boolean {
        val json = get("/api/tv/skip") ?: return false
        return try {
            val map = gson.fromJson(json, Map::class.java)
            map["ok"] == true
        } catch (_: Exception) { false }
    }

    suspend fun replaySong(): Boolean {
        val json = get("/api/queue/replay") ?: return false
        return try {
            val map = gson.fromJson(json, Map::class.java)
            map["ok"] == true
        } catch (_: Exception) { false }
    }

    suspend fun sendHeartbeat(
        playing: Boolean, songUid: String, songName: String,
        singer: String, progress: Double, duration: Double,
        mode: String, micVolume: Int, musicVolume: Int
    ): HeartbeatResponse? {
        val data = mapOf(
            "playing" to playing, "song_uid" to songUid,
            "song_name" to songName, "singer" to singer,
            "progress" to progress, "duration" to duration,
            "mode" to mode, "mic_volume" to micVolume,
            "music_volume" to musicVolume,
        )
        val json = post("/api/tv/heartbeat", gson.toJson(data)) ?: return null
        return try { gson.fromJson(json, HeartbeatResponse::class.java) } catch (_: Exception) { null }
    }

    suspend fun getLyric(uid: String): LyricResponse? {
        val json = get("/api/lyric/$uid") ?: return null
        return try { gson.fromJson(json, LyricResponse::class.java) } catch (_: Exception) { null }
    }

    fun getAudioUrl(uid: String, type: String = "accompaniment"): String =
        "$baseUrl/api/audio/$type/$uid"

    // ★ 修改：QR码获取用短超时client
    suspend fun getQrCodeStream(): InputStream? = withContext(Dispatchers.IO) {
        try {
            val request = Request.Builder().url("$baseUrl/api/qrcode").get().build()
            val response = quickClient.newCall(request).execute()
            if (response.isSuccessful) response.body?.byteStream() else null
        } catch (_: IOException) { null }
    }

    suspend fun getQrCodeUrl(): String? {
        val json = get("/api/qrcode-url") ?: return null
        return try { gson.fromJson(json, QrCodeUrlResponse::class.java).url } catch (_: Exception) { null }
    }

    suspend fun setMode(mode: String): Boolean {
        val json = post("/api/tv/mode", """{"mode":"$mode"}""") ?: return false
        return try {
            val map = gson.fromJson(json, Map::class.java)
            map["ok"] == true
        } catch (_: Exception) { false }
    }

    suspend fun getConfig(): Map<String, Any>? {
        val json = get("/api/config") ?: return null
        return try {
            val type = object : TypeToken<Map<String, Any>>() {}.type
            gson.fromJson(json, type)
        } catch (_: Exception) { null }
    }
}
