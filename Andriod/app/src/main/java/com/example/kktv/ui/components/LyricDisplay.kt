package com.example.kktv.ui.components

import androidx.compose.animation.*
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.*
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.example.kktv.model.LyricLine

/**
 * ★ 修复：歌词不跟随播放进度滚动
 * 根因：derivedStateOf 无法追踪普通变量，改为直接计算
 */
@Composable
fun LyricDisplay(
    lyrics: List<LyricLine>,
    currentTimeMs: Long,
    modifier: Modifier = Modifier,
    highlightColor: Color = Color(0xFFFF6EC4),
    normalColor: Color = Color.White.copy(alpha = 0.5f),
    dimColor: Color = Color.White.copy(alpha = 0.2f),
) {
    val currentTimeSec = currentTimeMs / 1000.0

    // ★ FIX: 不用 derivedStateOf，直接在 Composable 作用域中计算
    // 每次 currentTimeMs 变化（50ms一次）都会触发 recomposition
    // 但只有 currentLineIndex 真正变化时 AnimatedContent 才会播动画
    val currentLineIndex = remember(lyrics, currentTimeMs) {
        var idx = -1
        for (i in lyrics.indices) {
            if (currentTimeSec >= lyrics[i].time) idx = i
            else break
        }
        idx
    }

    if (lyrics.isEmpty()) {
        Column(
            modifier = modifier.fillMaxWidth(),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            Text("♪ ～ ♪", style = TextStyle(fontSize = 36.sp, color = normalColor))
            Spacer(modifier = Modifier.height(8.dp))
            Text("暂无歌词", style = TextStyle(fontSize = 18.sp, color = dimColor))
        }
        return
    }

    AnimatedContent(
        targetState = currentLineIndex,
        transitionSpec = {
            if (targetState > initialState) {
                (slideInVertically(tween(380)) { it / 3 } + fadeIn(tween(380)))
                    .togetherWith(
                        slideOutVertically(tween(280)) { -it / 3 } + fadeOut(tween(280))
                    ).using(SizeTransform(clip = false))
            } else {
                (slideInVertically(tween(380)) { -it / 3 } + fadeIn(tween(380)))
                    .togetherWith(
                        slideOutVertically(tween(280)) { it / 3 } + fadeOut(tween(280))
                    ).using(SizeTransform(clip = false))
            }
        },
        label = "lyric-block",
        modifier = modifier,
    ) { animLineIdx ->
        Column(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 48.dp),
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
        ) {
            // ---- 上一行（暗） ----
            if (animLineIdx > 0) {
                Text(
                    text = lyrics[animLineIdx - 1].text,
                    style = TextStyle(
                        fontSize = 24.sp,
                        fontWeight = FontWeight.Normal,
                        color = dimColor,
                    ),
                    modifier = Modifier.padding(vertical = 6.dp),
                )
            } else {
                Spacer(modifier = Modifier.height(36.dp))
            }

            // ---- 当前行（高亮 / 逐字） ----
            if (animLineIdx >= 0 && animLineIdx < lyrics.size) {
                val line = lyrics[animLineIdx]
                if (line.words != null && line.words.isNotEmpty()) {
                    WordByWordLyric(
                        line = line,
                        currentTimeMs = currentTimeMs,
                        highlightColor = highlightColor,
                        normalColor = Color.White.copy(alpha = 0.7f),
                    )
                } else {
                    Text(
                        text = line.text,
                        style = TextStyle(
                            fontSize = 38.sp,
                            fontWeight = FontWeight.Bold,
                            color = highlightColor,
                        ),
                        modifier = Modifier.padding(vertical = 10.dp),
                    )
                }
            }

            // ---- 下一行 ----
            val nextIdx = animLineIdx + 1
            if (nextIdx in lyrics.indices) {
                Text(
                    text = lyrics[nextIdx].text,
                    style = TextStyle(
                        fontSize = 28.sp,
                        fontWeight = FontWeight.Normal,
                        color = normalColor,
                    ),
                    modifier = Modifier.padding(vertical = 6.dp),
                )
            } else {
                Spacer(modifier = Modifier.height(40.dp))
            }

            // ---- 预览行（更暗） ----
            val previewIdx = animLineIdx + 2
            if (previewIdx in lyrics.indices) {
                Text(
                    text = lyrics[previewIdx].text,
                    style = TextStyle(
                        fontSize = 22.sp,
                        fontWeight = FontWeight.Normal,
                        color = dimColor,
                    ),
                    modifier = Modifier.padding(vertical = 4.dp),
                )
            }
        }
    }
}

/**
 * ★ 逐字歌词组件
 * 接收 currentTimeMs（毫秒）直接计算每个字的高亮进度
 */
@Composable
fun WordByWordLyric(
    line: LyricLine,
    currentTimeMs: Long,
    highlightColor: Color,
    normalColor: Color,
) {
    val words = line.words ?: return
    val lineStartTime = line.time
    val currentTimeSec = currentTimeMs / 1000.0

    Row(
        modifier = Modifier.padding(vertical = 10.dp),
        horizontalArrangement = Arrangement.Center,
    ) {
        words.forEach { word ->
            val wordStartTime = lineStartTime + word.offset
            val wordEndTime = wordStartTime + word.duration

            val progress = when {
                currentTimeSec < wordStartTime -> 0f
                currentTimeSec >= wordEndTime -> 1f
                word.duration <= 0.001 -> 1f
                else -> ((currentTimeSec - wordStartTime) /
                        word.duration).toFloat().coerceIn(0f, 1f)
            }

            val color = lerp(normalColor, highlightColor, progress)

            Text(
                text = word.text,
                style = TextStyle(
                    fontSize = 38.sp,
                    fontWeight = FontWeight.Bold,
                    color = color,
                ),
            )
        }
    }
}

private fun lerp(start: Color, end: Color, fraction: Float): Color {
    val f = fraction.coerceIn(0f, 1f)
    return Color(
        red = start.red + (end.red - start.red) * f,
        green = start.green + (end.green - start.green) * f,
        blue = start.blue + (end.blue - start.blue) * f,
        alpha = start.alpha + (end.alpha - start.alpha) * f,
    )
}
