package com.example.kktv

import android.graphics.BitmapFactory
import android.os.Bundle
import android.util.Log
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.animation.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester

import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.input.key.*
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.kktv.api.KKTVApiClient
import com.example.kktv.model.*
import com.example.kktv.player.KaraokePlayer
import com.example.kktv.ui.components.LyricDisplay
import com.example.kktv.ui.theme.KKTVTheme
import kotlinx.coroutines.*


private const val TAG = "KKTV0.2"

class MainActivity : ComponentActivity() {
    private lateinit var api: KKTVApiClient
    private lateinit var player: KaraokePlayer

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        api = KKTVApiClient()
        player = KaraokePlayer(this)
        setContent {
            KKTVTheme { KKTVApp(api = api, player = player) }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        player.release()
    }
}

@Composable
fun KKTVApp(api: KKTVApiClient, player: KaraokePlayer) {

    // ★ FIX: 全局协程异常处理器
    val exceptionHandler = remember {
        CoroutineExceptionHandler { _, throwable ->
            Log.e(TAG, "★ 协程异常（已捕获，不崩溃）", throwable)
        }
    }

    // ★ FIX: 加载守卫，防止并发 loadAndPlay
    var isLoadingSong by remember { mutableStateOf(false) }

    // ★ 新增：设置对话框状态
    var showSettings by remember { mutableStateOf(false) }
    var settingsHost by remember { mutableStateOf(api.serverHost) }
    var settingsPort by remember { mutableStateOf(api.serverPort.toString()) }

    var isConnected by remember { mutableStateOf(false) }
    var currentSong by remember { mutableStateOf<SongInfo?>(null) }
    var songUrls by remember { mutableStateOf<SongUrls?>(null) }
    var lyrics by remember { mutableStateOf<List<LyricLine>>(emptyList()) }
    var tLyrics by remember { mutableStateOf<List<LyricLine>>(emptyList()) }
    var queueLength by remember { mutableIntStateOf(0) }
    var currentPositionMs by remember { mutableLongStateOf(0L) }
    var totalDurationMs by remember { mutableLongStateOf(0L) }
    var playMode by remember { mutableStateOf(PlayMode.ACCOMPANIMENT) }
    var musicVolume by remember { mutableIntStateOf(70) }
    var statusText by remember { mutableStateOf("启动中...") }
    var nextSongBrief by remember { mutableStateOf<NextSongBrief?>(null) }
    var preparingStatus by remember { mutableStateOf("") }
    var qrBitmap by remember { mutableStateOf<ImageBitmap?>(null) }
    var jkUrl by remember { mutableStateOf("") }

    var lastLocalModeChangeMs by remember { mutableLongStateOf(0L) }
    var lastServerModeChangedAt by remember { mutableStateOf(0.0) }
    var isSkipping by remember { mutableStateOf(false) }

    // ★ FIX: 用计数器判断是否需要切歌/重唱
    var lastSkipCounter by remember { mutableIntStateOf(0) }
    var lastReplayCounter by remember { mutableIntStateOf(0) }
    // ★ 长按计时状态
    var okPressStartMs by remember { mutableLongStateOf(0L) }


    val scope = rememberCoroutineScope()
    val rootFocus = remember { FocusRequester() }

    // ★★★ FIX: 提取QR码获取逻辑，供多处复用 ★★★
    val fetchQrCode: suspend () -> Unit = {
        for (attempt in 1..3) {
            try {
                Log.d(TAG, "QR获取 attempt $attempt")
                val stream = api.getQrCodeStream()
                if (stream != null) {
                    val bmp = BitmapFactory.decodeStream(stream)
                    if (bmp != null) {
                        qrBitmap = bmp.asImageBitmap()
                        Log.d(TAG, "✅ QR码获取成功 (attempt $attempt)")
                    }
                    stream.close()
                }
                val url = api.getQrCodeUrl()
                if (url != null) jkUrl = url
                if (qrBitmap != null) break
            } catch (e: Exception) {
                Log.e(TAG, "QR获取异常 attempt $attempt", e)
            }
            if (attempt < 3) delay(300)
        }
    }

// ======== Effect 1: 连接——自动发现 + QR码前置获取 ========
    LaunchedEffect(isConnected) {
        if (isConnected) return@LaunchedEffect

        statusText = "搜索KKTV服务器..."

        while (!isConnected) {
            // 通道A：主动发送discover请求
            val discovered = api.discoverServer(timeoutMs = 3000)
            if (discovered != null) {
                api.serverHost = discovered.first
                api.serverPort = discovered.second
                settingsHost = discovered.first
                settingsPort = discovered.second.toString()
                Log.d(TAG, "✅ 主动发现: ${discovered.first}:${discovered.second}")
            }

            // 验证连接
            val online = api.isServerOnline()
            if (online) {
                // ★★★ FIX: 连接确认后，先获取QR码，再设置isConnected ★★★
                statusText = "已连接，加载点歌二维码..."
                Log.d(TAG, "✅ 连接成功，开始获取QR码...")
                fetchQrCode()
                Log.d(TAG, "QR码获取完毕: bitmap=${qrBitmap != null}, url=$jkUrl")

                isConnected = true
                statusText = "已连接，等待点歌..."
                Log.d(TAG, "✅ 连接流程完成: ${api.serverHost}:${api.serverPort}")
                break
            }

            // 通道B：监听后端主动广播
            statusText = "等待KKTV服务器上线..."
            val announced = api.listenForAnnounce(timeoutMs = 5000)
            if (announced != null) {
                api.serverHost = announced.first
                api.serverPort = announced.second
                settingsHost = announced.first
                settingsPort = announced.second.toString()
                Log.d(TAG, "✅ 收到广播: ${announced.first}:${announced.second}")

                if (api.isServerOnline()) {
                    // ★★★ FIX: 同上——先QR码，后isConnected ★★★
                    statusText = "已连接，加载点歌二维码..."
                    Log.d(TAG, "✅ 广播连接成功，开始获取QR码...")
                    fetchQrCode()

                    isConnected = true
                    statusText = "已连接，等待点歌..."
                    break
                }
            }

            statusText = "未找到服务器，重试中..."
            delay(2000)
        }
    }

// ======== Effect 6: 断线重连监控 ========
    LaunchedEffect(isConnected) {
        if (!isConnected) return@LaunchedEffect
        // ★ 连接成功后，持续监控连接状态
        while (isConnected) {
            delay(10_000)  // 每10秒检查一次
            try {
                if (!api.isServerOnline()) {
                    Log.w(TAG, "⚠️ 连接丢失，触发重连")
                    isConnected = false  // 触发 Effect 1 重新发现
                }
            } catch (_: Exception) {
                isConnected = false
            }
        }
    }



    // ======== Effect 5: 二维码定期刷新（初始获取已移至Effect 1） ========
    LaunchedEffect(isConnected) {
        if (!isConnected) return@LaunchedEffect
        // ★ FIX: 初始QR码已在 Effect 1 连接成功时获取
        // 这里只负责每60秒定期刷新，防止URL过期
        while (true) {
            delay(60_000)
            try {
                val stream = api.getQrCodeStream()
                if (stream != null) {
                    val bmp = BitmapFactory.decodeStream(stream)
                    if (bmp != null) qrBitmap = bmp.asImageBitmap()
                    stream.close()
                }
                val url = api.getQrCodeUrl()
                if (url != null) jkUrl = url
            } catch (_: Exception) {}
        }
    }





    // ======== ★★★ Effect 2: 轮询歌曲 (FIX: 加载守卫) ★★★ ========
    LaunchedEffect(currentSong, isConnected) {
        if (!isConnected || currentSong != null) return@LaunchedEffect
        statusText = "已连接，等待点歌..."
        while (true) {
            delay(3000)
            // ★ FIX: 加载中跳过
            if (isLoadingSong) continue
            try {
                val resp = api.getNextSong()
                if (resp?.ok == true && resp.song != null) {
                    if (isLoadingSong) continue  // 双重检查
                    isLoadingSong = true
                    Log.d(TAG, "★ Effect2: 发现歌曲 ${resp.song.name}")

                    songUrls = resp.urls
                    statusText = "加载中..."

                    scope.launch(exceptionHandler) {
                        try {
                            loadAndPlay(player, api, resp, playMode,
                                onLyrics = { l, t ->
                                    lyrics = l; tLyrics = t
                                })
                            statusText = "播放中"
                        } catch (e: Exception) {
                            Log.e(TAG, "loadAndPlay 异常", e)
                            statusText = "加载失败..."
                        } finally {
                            isLoadingSong = false
                        }
                    }

                    currentSong = resp.song
                    break
                } else {
                    val qResp = api.getQueue()
                    if (qResp != null) {
                        queueLength = qResp.queue.size
                        val first = qResp.queue.firstOrNull()
                        if (first != null && first.state != "ready") {
                            preparingStatus = when (first.state) {
                                "queued" -> "「${first.name}」排队中..."
                                "waiting_download" -> "「${first.name}」等待准备..."
                                "downloading" -> "「${first.name}」正在触发音乐软件..."
                                "needs_download" -> "📥「${first.name}」请在电脑端音乐软件中手动下载"
                                "downloaded" -> "「${first.name}」下载完成，正在分离..."
                                "separating" -> "「${first.name}」正在分离伴奏..."
                                "error" -> "「${first.name}」出错了"
                                else -> ""
                            }
                        } else {
                            preparingStatus = ""
                        }
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "Effect2 异常", e)
            }
        }
    }

    // ======== Effect 3a: 播放进度 ========
    LaunchedEffect(currentSong) {
        while (true) {
            delay(30)
            if (player.isPlaying()) {
                currentPositionMs = player.getCurrentPosition()
                totalDurationMs = player.getDuration()
            }
        }
    }

    // ======== Effect 3b: 心跳上报（2s间隔）========
    LaunchedEffect(currentSong, isConnected) {
        while (true) {
            delay(2000)
            if (!isConnected) continue
            val song = currentSong
            try {
                val hb = api.sendHeartbeat(
                    playing = player.isPlaying(),
                    songUid = song?.uid ?: "",
                    songName = song?.name ?: "",
                    singer = song?.singer ?: "",
                    progress = currentPositionMs / 1000.0,
                    duration = totalDurationMs / 1000.0,
                    mode = playMode.name.lowercase(),
                    micVolume = 80,
                    musicVolume = musicVolume,
                )
                if (hb != null) {
                    queueLength = hb.queue_len
                    nextSongBrief = hb.next_song

                    // ★ FIX: 用计数器判断是否需要切歌
                    if (hb.skip_counter > lastSkipCounter && !isSkipping) {
                        lastSkipCounter = hb.skip_counter
                        isSkipping = true
                        scope.launch(exceptionHandler) {
                            try {
                                Log.d(TAG, "远程切歌信号 (counter=${hb.skip_counter})")
                                player.pause()
                                delay(300)
                                val resp = api.getNextSong()
                                if (resp?.ok == true && resp.song != null) {
                                    currentSong = resp.song
                                    songUrls = resp.urls
                                    loadAndPlay(player, api, resp, playMode,
                                        onLyrics = { l, t -> lyrics = l; tLyrics = t })
                                    statusText = "播放中"
                                } else {
                                    currentSong = null; songUrls = null
                                    lyrics = emptyList(); tLyrics = emptyList()
                                    statusText = "等待点歌..."
                                }
                            } finally {
                                isSkipping = false
                            }
                        }
                    }

                    // ★ FIX: 重唱也用计数器
                    if (hb.replay_counter > lastReplayCounter) {
                        lastReplayCounter = hb.replay_counter
                        Log.d(TAG, "远程重唱信号 (counter=${hb.replay_counter})")
                        player.replay()
                    }

                    // ★ mode 同步
                    val now = System.currentTimeMillis()
                    val localCooldown = now - lastLocalModeChangeMs > 8000
                    val serverChanged = hb.mode_changed_at > lastServerModeChangedAt

                    if (localCooldown && serverChanged) {
                        val serverMode = if (hb.mode == "original")
                            PlayMode.ORIGINAL else PlayMode.ACCOMPANIMENT
                        if (serverMode != playMode) {
                            Log.d(TAG, "mode同步: $playMode → $serverMode")
                            playMode = serverMode
                            player.switchMode(playMode)
                        }
                        lastServerModeChangedAt = hb.mode_changed_at
                    }
                }
            } catch (_: Exception) {}
        }
    }

    // ======== ★★★ Effect 4: 播放完毕 (FIX: 全面防崩) ★★★ ========
    LaunchedEffect(Unit) {
        player.onPlaybackFinished = {
            scope.launch(exceptionHandler) {
                // ★ FIX: 加载守卫
                if (isLoadingSong) {
                    Log.w(TAG, "onPlaybackFinished: 已有加载任务，跳过")
                    return@launch
                }
                isLoadingSong = true
                Log.d(TAG, "★ onPlaybackFinished 触发")
                statusText = "加载下一首..."
                try {
                    val resp = api.reportFinished()
                    Log.d(TAG, "reportFinished: ok=${resp?.ok}, song=${resp?.song?.name}")
                    if (resp?.ok == true && resp.song != null) {
                        songUrls = resp.urls
                        try {
                            loadAndPlay(player, api, resp, playMode,
                                onLyrics = { l, t ->
                                    Log.d(TAG, "★ 下一首歌词: ${l.size} 行")
                                    lyrics = l; tLyrics = t
                                })
                        } catch (e: Exception) {
                            Log.e(TAG, "loadAndPlay 异常", e)
                            // ★ FIX: 加载失败不崩溃，回到待机
                            try { player.pause() } catch (_: Exception) {}
                            currentSong = null; songUrls = null
                            lyrics = emptyList(); tLyrics = emptyList()
                            statusText = "加载失败，等待点歌..."
                            return@launch
                        }
                        currentSong = resp.song
                        statusText = "播放中"
                    } else {
                        Log.d(TAG, "没有下一首歌，回到待机")
                        try { player.pause() } catch (_: Exception) {}
                        currentSong = null; songUrls = null
                        lyrics = emptyList(); tLyrics = emptyList()
                        statusText = "等待点歌..."
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "★ onPlaybackFinished 处理异常", e)
                    try { player.pause() } catch (_: Exception) {}
                    currentSong = null; songUrls = null
                    lyrics = emptyList(); tLyrics = emptyList()
                    statusText = "出错了，等待点歌..."
                } finally {
                    isLoadingSong = false
                }
            }
        }
    }

    // ======== UI ========
    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(
                Brush.verticalGradient(
                    listOf(Color(0xFF0F0C29), Color(0xFF302B63), Color(0xFF24243E))
                )
            )
            .focusRequester(rootFocus)
            .focusable()
            .onKeyEvent { event ->
                when {
                    // ★ 长按OK键检测（700ms以上视为长按）
                    event.key == Key.Enter || event.key == Key.DirectionCenter -> {
                        when (event.type) {
                            KeyEventType.KeyDown -> {
                                if (okPressStartMs == 0L) {
                                    okPressStartMs = System.currentTimeMillis()
                                }
                                true
                            }
                            KeyEventType.KeyUp -> {
                                val pressDuration = System.currentTimeMillis() - okPressStartMs
                                okPressStartMs = 0L

                                if (pressDuration >= 700L) {
                                    // ★ 长按 → 打开设置
                                    showSettings = true
                                } else {
                                    // ★ 短按
                                    if (currentSong != null) {
                                        // 播放中：切换原唱/伴奏
                                        val newMode = if (playMode == PlayMode.ACCOMPANIMENT)
                                            PlayMode.ORIGINAL else PlayMode.ACCOMPANIMENT
                                        playMode = newMode
                                        lastLocalModeChangeMs = System.currentTimeMillis()
                                        player.switchMode(newMode)
                                        scope.launch { api.setMode(newMode.name.lowercase()) }
                                    }
                                    // 待机时短按OK：无操作
                                }
                                true
                            }
                            else -> false
                        }
                    }
                    event.type == KeyEventType.KeyDown -> {
                        when (event.key) {
                            Key.Back -> {
                                if (showSettings) {
                                    showSettings = false
                                    true
                                } else false
                            }
                            Key.DirectionUp -> {
                                musicVolume = (musicVolume + 5).coerceAtMost(100)
                                player.musicVolume = musicVolume / 100f
                                true
                            }
                            Key.DirectionDown -> {
                                musicVolume = (musicVolume - 5).coerceAtLeast(0)
                                player.musicVolume = musicVolume / 100f
                                true
                            }
                            Key.DirectionLeft -> {
                                player.replay()
                                scope.launch { api.replaySong() }
                                true
                            }
                            Key.DirectionRight -> {
                                if (!isSkipping) {
                                    isSkipping = true
                                    scope.launch(exceptionHandler) {
                                        try {
                                            doLocalSkip(api, player,
                                                setSong = { currentSong = it },
                                                setUrls = { songUrls = it },
                                                setLyrics = { l, t -> lyrics = l; tLyrics = t },
                                                setStatus = { statusText = it },
                                                playMode = playMode)
                                        } finally {
                                            isSkipping = false
                                        }
                                    }
                                }
                                true
                            }
                            else -> false
                        }
                    }
                    else -> false
                }
            },
    ) {
        val song = currentSong
        if (song == null) {
            IdleScreen(
                statusText, isConnected, queueLength,
                qrBitmap, jkUrl, preparingStatus,
                onOpenSettings = { showSettings = true },  // ★ 新增
            )
        } else {
            PlayingScreen(song, lyrics, tLyrics, currentPositionMs,
                totalDurationMs, playMode, musicVolume, queueLength,
                nextSongBrief, qrBitmap, jkUrl)
        }

        // ★ 设置弹窗
        if (showSettings) {
            SettingsDialog(
                host = settingsHost,
                port = settingsPort,
                onHostChange = { settingsHost = it },
                onPortChange = { settingsPort = it },
                onSave = {
                    api.serverHost = settingsHost
                    api.serverPort = settingsPort.toIntOrNull() ?: 8080
                    showSettings = false
                    // ★ 重新连接
                    isConnected = false
                },
                onDismiss = { showSettings = false },
            )
        }
    }

    LaunchedEffect(Unit) {
        delay(300)
        rootFocus.requestFocus()
    }
}

/**
 * TV本地切歌——使用 /api/tv/skip
 */
suspend fun doLocalSkip(
    api: KKTVApiClient, player: KaraokePlayer,
    setSong: (SongInfo?) -> Unit, setUrls: (SongUrls?) -> Unit,
    setLyrics: (List<LyricLine>, List<LyricLine>) -> Unit,
    setStatus: (String) -> Unit, playMode: PlayMode,
) {
    Log.d(TAG, "本地切歌")
    api.tvSkip()
    delay(500)
    val resp = api.getNextSong()
    if (resp?.ok == true && resp.song != null) {
        setUrls(resp.urls)
        loadAndPlay(player, api, resp, playMode,
            onLyrics = { l, t ->
                Log.d(TAG, "★ 切歌后歌词: ${l.size} 行")
                setLyrics(l, t)
            })
        setSong(resp.song)
        setStatus("播放中")
    } else {
        player.pause()
        setSong(null); setUrls(null)
        setLyrics(emptyList(), emptyList())
        setStatus("等待点歌...")
    }
}

suspend fun loadAndPlay(
    player: KaraokePlayer, api: KKTVApiClient,
    resp: NextSongResponse, currentMode: PlayMode,
    onLyrics: (List<LyricLine>, List<LyricLine>) -> Unit,
) {
    val urls = resp.urls ?: return
    val song = resp.song ?: return

    Log.d(TAG, "loadAndPlay: ${song.name} - ${song.singer}")

    player.loadSong(urls.accompaniment, urls.vocals, urls.original)
    player.switchMode(currentMode)
    player.play()

    // ★ 歌词获取——5次重试
    var lyricLoaded = false
    for (attempt in 1..5) {
        try {
            Log.d(TAG, "歌词请求 attempt $attempt, uid=${song.uid}")
            val lyricResp = api.getLyric(song.uid)
            Log.d(TAG, "歌词响应: ok=${lyricResp?.ok}, lines=${lyricResp?.lines?.size}, " +
                    "has_words=${lyricResp?.has_words}")
            if (lyricResp?.ok == true && lyricResp.lines.isNotEmpty()) {
                onLyrics(lyricResp.lines, lyricResp.tlines)
                lyricLoaded = true
                Log.d(TAG, "✅ 歌词加载成功: ${lyricResp.lines.size} 行, has_words=${lyricResp.has_words}")
                lyricResp.lines.take(5).forEachIndexed { i, line ->
                    val wordInfo = if (line.words != null && line.words.isNotEmpty()) {
                        "words=${line.words.size}, 首字='${line.words[0].text}'(${line.words[0].offset}+${line.words[0].duration})"
                    } else {
                        "无逐字数据"
                    }
                    Log.d(TAG, "  行$i: [${String.format("%.3f", line.time)}] ${line.text.take(20)}... ($wordInfo)")
                }
                break
            } else {
                Log.w(TAG, "歌词响应为空或无行")
            }
        } catch (e: Exception) {
            Log.e(TAG, "歌词请求异常 attempt $attempt", e)
        }
        if (attempt < 5) delay(2000L)
    }
    if (!lyricLoaded) {
        Log.w(TAG, "❌ 歌词加载失败")
        onLyrics(emptyList(), emptyList())
    }
}


@Composable
fun IdleScreen(
    statusText: String, isConnected: Boolean, queueLength: Int,
    qrBitmap: ImageBitmap?, jkUrl: String, preparingStatus: String,
    onOpenSettings: () -> Unit,    // ★ 新增回调
) {
    Box(modifier = Modifier.fillMaxSize()) {
        // 左上角二维码
        if (qrBitmap != null) {
            Column(
                modifier = Modifier.align(Alignment.TopStart).padding(24.dp),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Image(
                    bitmap = qrBitmap, contentDescription = "扫码点歌",
                    modifier = Modifier.size(120.dp).clip(RoundedCornerShape(8.dp))
                        .background(Color.White).padding(4.dp),
                )
                Spacer(modifier = Modifier.height(4.dp))
                Text("扫码点歌", style = TextStyle(fontSize = 12.sp,
                    color = Color.White.copy(alpha = 0.6f)))
                if (jkUrl.isNotEmpty()) {
                    Text(jkUrl, style = TextStyle(fontSize = 10.sp,
                        color = Color.White.copy(alpha = 0.3f)),
                        maxLines = 1, overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.widthIn(max = 150.dp))
                }
            }
        }

        // ★★★ 右上角设置按钮——可聚焦，D-pad可导航 ★★★
        val settingsFocus = remember { FocusRequester() }
        Box(
            modifier = Modifier
                .align(Alignment.TopEnd)
                .padding(24.dp)
                .focusRequester(settingsFocus)
                .focusable()
                .clickable { onOpenSettings() }
                .background(
                    Color.White.copy(alpha = 0.08f),
                    RoundedCornerShape(12.dp)
                )
                .border(
                    1.dp,
                    Color.White.copy(alpha = 0.15f),
                    RoundedCornerShape(12.dp)
                )
                .padding(horizontal = 16.dp, vertical = 10.dp),
        ) {
            Text("⚙ 设置", style = TextStyle(
                fontSize = 14.sp,
                color = Color.White.copy(alpha = 0.6f)))
        }

        // 中间主体
        Column(
            modifier = Modifier.fillMaxSize(),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text("🎤 KKTV", style = TextStyle(fontSize = 64.sp,
                fontWeight = FontWeight.Bold, color = Color(0xFFFF6EC4)))
            Spacer(modifier = Modifier.height(24.dp))
            Text(statusText, style = TextStyle(fontSize = 24.sp,
                color = Color.White.copy(alpha = 0.7f)))

            if (isConnected) {
                Spacer(modifier = Modifier.height(16.dp))
                Text("请在手机上扫码点歌", style = TextStyle(fontSize = 20.sp,
                    color = Color.White.copy(alpha = 0.4f)))
                if (queueLength > 0) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text("队列中有 $queueLength 首歌准备中...",
                        style = TextStyle(fontSize = 18.sp, color = Color(0xFF7873F5)))
                }
                if (preparingStatus.isNotEmpty()) {
                    Spacer(modifier = Modifier.height(8.dp))
                    val color = if (preparingStatus.contains("手动下载"))
                        Color(0xFFE91E63) else Color(0xFFFF9800)
                    Text(preparingStatus,
                        style = TextStyle(fontSize = 16.sp, color = color))
                }
            }

            Spacer(modifier = Modifier.height(40.dp))
            // ★ FIX: 提示文字更新
            Text("长按OK=设置  ↑↓=音量  ←=重唱  →=切歌",
                style = TextStyle(fontSize = 14.sp,
                    color = Color.White.copy(alpha = 0.3f)))
        }
    }
}

@Composable
fun PlayingScreen(
    song: SongInfo, lyrics: List<LyricLine>, tLyrics: List<LyricLine>,
    currentPositionMs: Long, totalDurationMs: Long,
    playMode: PlayMode, musicVolume: Int, queueLength: Int,
    nextSongBrief: NextSongBrief?, qrBitmap: ImageBitmap?, jkUrl: String,
) {
    Box(modifier = Modifier.fillMaxSize()) {
        LyricDisplay(
            lyrics = lyrics, currentTimeMs = currentPositionMs,
            modifier = Modifier.fillMaxSize().padding(top = 100.dp, bottom = 80.dp),
        )

        Box(
            modifier = Modifier.fillMaxWidth().align(Alignment.TopCenter)
                .background(
                    Brush.verticalGradient(listOf(
                        Color.Black.copy(alpha = 0.7f),
                        Color.Black.copy(alpha = 0.3f),
                        Color.Transparent,
                    ))
                ).padding(horizontal = 24.dp, vertical = 16.dp),
        ) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (qrBitmap != null) {
                        Image(bitmap = qrBitmap, contentDescription = "扫码",
                            modifier = Modifier.size(56.dp)
                                .clip(RoundedCornerShape(6.dp))
                                .background(Color.White).padding(2.dp))
                        Spacer(modifier = Modifier.width(12.dp))
                    }
                    Column {
                        Text("♪ ${song.name}",
                            style = TextStyle(fontSize = 22.sp,
                                fontWeight = FontWeight.Bold, color = Color.White),
                            maxLines = 1, overflow = TextOverflow.Ellipsis,
                            modifier = Modifier.widthIn(max = 400.dp))
                        Text(song.singer, style = TextStyle(fontSize = 14.sp,
                            color = Color.White.copy(alpha = 0.6f)))
                    }
                }

                if (nextSongBrief != null) {
                    Column(
                        horizontalAlignment = Alignment.CenterHorizontally,
                        modifier = Modifier.widthIn(max = 250.dp),
                    ) {
                        Text("下一首", style = TextStyle(fontSize = 12.sp,
                            color = Color(0xFF7873F5)))
                        Text("${nextSongBrief.name} - ${nextSongBrief.singer}",
                            style = TextStyle(fontSize = 14.sp,
                                color = Color.White.copy(alpha = 0.7f)),
                            maxLines = 1, overflow = TextOverflow.Ellipsis)
                        if (nextSongBrief.state != "ready") {
                            Text(
                                text = when (nextSongBrief.state) {
                                    "downloading" -> "⬇️ 准备中"
                                    "needs_download" -> "📥 请在电脑下载"
                                    "separating" -> "🔀 分离中"
                                    "queued", "waiting_download" -> "⏳ 等待中"
                                    else -> nextSongBrief.state
                                },
                                style = TextStyle(fontSize = 11.sp,
                                    color = if (nextSongBrief.state == "needs_download")
                                        Color(0xFFE91E63) else Color(0xFFFF9800)),
                            )
                        }
                    }
                }

                Column(horizontalAlignment = Alignment.End) {
                    val modeText = if (playMode == PlayMode.ACCOMPANIMENT)
                        "🎵 伴奏" else "🎤 原唱"
                    Text(modeText, style = TextStyle(fontSize = 18.sp,
                        color = Color(0xFFFF6EC4), fontWeight = FontWeight.Bold))
                    Text("🔊 $musicVolume%", style = TextStyle(fontSize = 14.sp,
                        color = Color.White.copy(alpha = 0.5f)))
                    if (queueLength > 0) {
                        Text("📋 还有${queueLength}首", style = TextStyle(
                            fontSize = 14.sp, color = Color(0xFF7873F5)))
                    }
                }
            }
        }

        Column(modifier = Modifier.fillMaxWidth().align(Alignment.BottomCenter)) {
            // ★ 提示文字更新
            Text("OK=切换原唱/伴奏  长按OK=设置  ↑↓=音量  ←=重唱  →=切歌",
                style = TextStyle(fontSize = 13.sp,
                    color = Color.White.copy(alpha = 0.3f),
                    textAlign = TextAlign.Center),
                modifier = Modifier.fillMaxWidth().padding(bottom = 8.dp))

            Row(
                modifier = Modifier.fillMaxWidth()
                    .padding(horizontal = 24.dp, vertical = 2.dp),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(formatTime(currentPositionMs), style = TextStyle(
                    fontSize = 13.sp, color = Color.White.copy(alpha = 0.4f)))
                Text(formatTime(totalDurationMs), style = TextStyle(
                    fontSize = 13.sp, color = Color.White.copy(alpha = 0.4f)))
            }

            Box(modifier = Modifier.fillMaxWidth().height(4.dp)) {
                Box(modifier = Modifier.fillMaxSize()
                    .background(Color.White.copy(alpha = 0.1f)))
                val progress = if (totalDurationMs > 0)
                    (currentPositionMs.toFloat() / totalDurationMs).coerceIn(0f, 1f)
                else 0f
                Box(modifier = Modifier.fillMaxHeight().fillMaxWidth(progress)
                    .background(Color(0xFFFF6EC4)))
            }
        }
    }
}

// ★ FIX: 设置弹窗组件——增加焦点管理和 Back 键支持
@Composable
fun SettingsDialog(
    host: String,
    port: String,
    onHostChange: (String) -> Unit,
    onPortChange: (String) -> Unit,
    onSave: () -> Unit,
    onDismiss: () -> Unit,
) {
    val saveFocus = remember { FocusRequester() }

    Box(
        modifier = Modifier
            .fillMaxSize()
            .background(Color.Black.copy(alpha = 0.8f))
            // ★ FIX: Back 键关闭弹窗
            .onKeyEvent { event ->
                if (event.type == KeyEventType.KeyDown && event.key == Key.Back) {
                    onDismiss()
                    true
                } else false
            }
            .focusable(),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            modifier = Modifier
                .widthIn(max = 400.dp)
                .background(
                    Color(0xFF24243E),
                    RoundedCornerShape(16.dp)
                )
                .border(
                    1.dp,
                    Color(0xFFFF6EC4).copy(alpha = 0.3f),
                    RoundedCornerShape(16.dp)
                )
                .padding(24.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                "⚙️ 服务器设置",
                style = TextStyle(
                    fontSize = 22.sp,
                    fontWeight = FontWeight.Bold,
                    color = Color(0xFFFF6EC4),
                ),
            )
            Spacer(modifier = Modifier.height(12.dp))

            // ★ FIX: TV端用纯文本显示当前值，不用TextField
            Text("服务器 IP: $host",
                style = TextStyle(fontSize = 18.sp, color = Color.White))
            Spacer(modifier = Modifier.height(8.dp))
            Text("端口: $port",
                style = TextStyle(fontSize = 18.sp, color = Color.White))

            Spacer(modifier = Modifier.height(12.dp))
            Text("如需修改，请在电脑控制台操作",
                style = TextStyle(fontSize = 14.sp,
                    color = Color.White.copy(alpha = 0.5f)))
            Text("控制台地址: http://$host:$port",
                style = TextStyle(fontSize = 13.sp,
                    color = Color(0xFF7873F5)))

            Spacer(modifier = Modifier.height(20.dp))

            // ★ FIX: 只保留关闭按钮，给焦点
            androidx.compose.material3.Button(
                onClick = onDismiss,
                modifier = Modifier.focusRequester(saveFocus),
                colors = androidx.compose.material3.ButtonDefaults.buttonColors(
                    containerColor = Color(0xFFFF6EC4)
                ),
            ) {
                Text("关闭")
            }
        }
    }

    // ★ FIX: 弹窗打开时焦点给关闭按钮
    LaunchedEffect(Unit) {
        delay(100)
        try { saveFocus.requestFocus() } catch (_: Exception) {}
    }
}

private fun formatTime(ms: Long): String {
    val totalSec = ms / 1000
    return "%d:%02d".format(totalSec / 60, totalSec % 60)
}
