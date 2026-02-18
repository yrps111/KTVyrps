package com.example.kktv.model

data class SongInfo(
    val uid: String = "",
    val name: String = "",
    val singer: String = "",
    val album: String = "",
    val source: String = "",
    val state: String = "",
    val error_msg: String = "",
    val has_accompaniment: Boolean = false,
    val has_vocals: Boolean = false,
    val has_lyric: Boolean = false,
    val queued_at: Double = 0.0,
    val ready_at: Double = 0.0,
)

data class NextSongResponse(
    val ok: Boolean = false,
    val msg: String = "",
    val song: SongInfo? = null,
    val urls: SongUrls? = null,
)

data class SongUrls(
    val accompaniment: String = "",
    val vocals: String = "",
    val original: String = "",
    val lyric: String = "",
)

data class LyricLine(
    val time: Double = 0.0,
    val text: String = "",
    val duration: Double = 0.0,
    val words: List<LyricWord>? = null,
)

data class LyricWord(
    val text: String = "",
    val offset: Double = 0.0,
    val duration: Double = 0.0,
)

data class LyricResponse(
    val ok: Boolean = false,
    val lines: List<LyricLine> = emptyList(),
    val tlines: List<LyricLine> = emptyList(),
    val has_words: Boolean = false,
)

data class HealthResponse(
    val server: String = "",
    val lx_music: Boolean = false,
    val local_songs: Int = 0,
    val cached: Int = 0,
    val queue_len: Int = 0,
)

data class QueueResponse(
    val queue: List<QueueItem> = emptyList(),
)

data class QueueItem(
    val uid: String = "",
    val name: String = "",
    val singer: String = "",
    val state: String = "",
    val is_current: Boolean = false,
)

// ★ FIX: 增加 mode_changed_at 字段
data class HeartbeatResponse(
    val ok: Boolean = false,
    val queue_len: Int = 0,
    // ★ FIX: 布尔 → 计数器
    val skip_counter: Int = 0,
    val replay_counter: Int = 0,
    val mode: String = "accompaniment",
    val mode_changed_at: Double = 0.0,
    val mic_volume: Int = 80,
    val music_volume: Int = 70,
    val next_song: NextSongBrief? = null,
    val current_state: String? = null,
)


data class NextSongBrief(
    val name: String = "",
    val singer: String = "",
    val state: String = "",
)

enum class PlayMode {
    ACCOMPANIMENT,
    ORIGINAL
}

data class QrCodeUrlResponse(
    val url: String = "",
)
