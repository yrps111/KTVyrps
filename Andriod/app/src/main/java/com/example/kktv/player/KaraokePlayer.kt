package com.example.kktv.player

import android.content.Context
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.exoplayer.ExoPlayer
import com.example.kktv.model.PlayMode

class KaraokePlayer(context: Context) {

    companion object {
        private const val TAG = "KaraokePlayer"
    }

    private val exoPlayer: ExoPlayer = ExoPlayer.Builder(context).build()
    private val mainHandler = Handler(Looper.getMainLooper())

    var currentMode: PlayMode = PlayMode.ACCOMPANIMENT
        private set

    private var accompanimentUrl: String = ""
    private var vocalsUrl: String = ""
    private var originalUrl: String = ""

    private var isNewSong: Boolean = false
    private var finishHandled: Boolean = false

    // ★★★ 简化的时钟机制 ★★★
    private var clockAnchorSystemMs: Long = 0L
    private var clockAnchorPositionMs: Long = 0L
    private var isClockRunning: Boolean = false

    var musicVolume: Float = 0.7f
        set(value) {
            field = value.coerceIn(0f, 1f)
            exoPlayer.volume = field
        }

    var onPlaybackFinished: (() -> Unit)? = null

    init {
        exoPlayer.addListener(object : Player.Listener {

            override fun onIsPlayingChanged(isPlaying: Boolean) {
                Log.d(TAG, "onIsPlayingChanged: $isPlaying")
                if (isPlaying) {
                    setClockAnchor()
                } else {
                    if (isClockRunning) {
                        clockAnchorPositionMs = calculatePosition()
                        clockAnchorSystemMs = System.currentTimeMillis()
                        isClockRunning = false
                        Log.d(TAG, "时钟暂停于: ${clockAnchorPositionMs}ms")
                    }
                }
            }

            override fun onPlaybackStateChanged(playbackState: Int) {
                Log.d(TAG, "playbackState=$playbackState, finishHandled=$finishHandled")

                when (playbackState) {
                    Player.STATE_IDLE -> {
                        // 空闲状态，不做处理
                    }
                    Player.STATE_BUFFERING -> {
                        // 缓冲中，不做处理（时钟继续运行）
                    }
                    Player.STATE_READY -> {
                        if (exoPlayer.isPlaying) {
                            setClockAnchor()
                        }
                    }
                    Player.STATE_ENDED -> {
                        isClockRunning = false
                        if (!finishHandled) {
                            finishHandled = true
                            mainHandler.post {
                                try {
                                    onPlaybackFinished?.invoke()
                                } catch (e: Exception) {
                                    Log.e(TAG, "onPlaybackFinished 异常", e)
                                }
                            }
                        }
                    }
                }
            }

            override fun onPlayerError(error: PlaybackException) {
                Log.e(TAG, "播放器错误: ${error.message}", error)
                isClockRunning = false
                if (!finishHandled) {
                    finishHandled = true
                    mainHandler.post {
                        try {
                            onPlaybackFinished?.invoke()
                        } catch (e: Exception) {
                            Log.e(TAG, "onPlaybackFinished(error) 异常", e)
                        }
                    }
                }
            }
        })
    }

    private fun setClockAnchor() {
        clockAnchorSystemMs = System.currentTimeMillis()
        clockAnchorPositionMs = exoPlayer.currentPosition.coerceAtLeast(0L)
        isClockRunning = true
        Log.d(TAG, "setClockAnchor: pos=${clockAnchorPositionMs}ms")
    }

    private fun calculatePosition(): Long {
        if (!isClockRunning) return clockAnchorPositionMs
        val elapsed = System.currentTimeMillis() - clockAnchorSystemMs
        return clockAnchorPositionMs + elapsed
    }

    fun getAccuratePosition(): Long {
        val pos = calculatePosition()
        val duration = exoPlayer.duration
        return if (duration > 0) pos.coerceIn(0L, duration) else pos.coerceAtLeast(0L)
    }

    fun loadSong(
        accompanimentUrl: String,
        vocalsUrl: String,
        originalUrl: String,
    ) {
        Log.d(TAG, "loadSong: acc=$accompanimentUrl")
        this.accompanimentUrl = accompanimentUrl
        this.vocalsUrl = vocalsUrl
        this.originalUrl = originalUrl
        this.isNewSong = true
        this.finishHandled = false
        this.isClockRunning = false
        this.clockAnchorPositionMs = 0L
        this.clockAnchorSystemMs = 0L
        exoPlayer.stop()
        exoPlayer.clearMediaItems()
    }

    fun switchMode(mode: PlayMode) {
        val currentPos = if (isNewSong) 0L else getAccuratePosition()
        val wasPlaying = if (isNewSong) false else exoPlayer.isPlaying
        currentMode = mode
        isNewSong = false

        val url = when (mode) {
            PlayMode.ACCOMPANIMENT -> accompanimentUrl
            PlayMode.ORIGINAL -> originalUrl.ifEmpty { vocalsUrl }
        }
        if (url.isNotEmpty()) {
            finishHandled = false
            isClockRunning = false
            try {
                exoPlayer.setMediaItem(MediaItem.fromUri(url))
                exoPlayer.prepare()
                if (currentPos > 0) exoPlayer.seekTo(currentPos)
            } catch (e: Exception) {
                Log.e(TAG, "switchMode 异常", e)
            }
            if (wasPlaying) {
                mainHandler.postDelayed({
                    try {
                        exoPlayer.play()
                    } catch (e: Exception) {
                        Log.e(TAG, "delayed play 异常", e)
                    }
                }, 200)
            }
        }
    }

    fun play() {
        finishHandled = false
        try {
            exoPlayer.play()
        } catch (e: Exception) {
            Log.e(TAG, "play() 异常", e)
        }
    }

    fun pause() {
        exoPlayer.pause()
    }

    fun replay() {
        finishHandled = false
        isClockRunning = false
        clockAnchorPositionMs = 0L
        clockAnchorSystemMs = System.currentTimeMillis()
        try {
            exoPlayer.seekTo(0)
            if (exoPlayer.playbackState == Player.STATE_ENDED ||
                exoPlayer.playbackState == Player.STATE_IDLE
            ) {
                exoPlayer.prepare()
            }
            exoPlayer.play()
        } catch (e: Exception) {
            Log.e(TAG, "replay() 异常", e)
        }
    }

    fun seekTo(position: Long) {
        isClockRunning = false
        clockAnchorPositionMs = position
        clockAnchorSystemMs = System.currentTimeMillis()
        exoPlayer.seekTo(position)
        if (exoPlayer.playbackState == Player.STATE_ENDED ||
            exoPlayer.playbackState == Player.STATE_IDLE
        ) {
            exoPlayer.prepare()
        }
    }

    fun toggleMode() {
        val newMode = if (currentMode == PlayMode.ACCOMPANIMENT)
            PlayMode.ORIGINAL else PlayMode.ACCOMPANIMENT
        switchMode(newMode)
    }

    fun getCurrentPosition(): Long = getAccuratePosition()
    fun getDuration(): Long = exoPlayer.duration.coerceAtLeast(0)
    fun isPlaying(): Boolean = exoPlayer.isPlaying
    fun release() { exoPlayer.release() }
}
