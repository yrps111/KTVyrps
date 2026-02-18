package com.example.kktv.ui.theme

import android.app.Activity
import android.os.Build
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.platform.LocalView
import androidx.core.view.WindowCompat

// ============================================================
// 颜色定义
// ============================================================
private val KKTVPink = Color(0xFFFF6EC4)
private val KKTVPurple = Color(0xFF7873F5)
private val KKTVDarkBg = Color(0xFF0F0C29)
private val KKTVDarkSurface = Color(0xFF302B63)

private val KKTVDarkColorScheme = darkColorScheme(
    primary = KKTVPink,
    secondary = KKTVPurple,
    tertiary = Color(0xFF4CAF50),
    background = KKTVDarkBg,
    surface = KKTVDarkSurface,
    onPrimary = Color.White,
    onSecondary = Color.White,
    onTertiary = Color.White,
    onBackground = Color.White,
    onSurface = Color.White,
    surfaceVariant = Color(0xFF24243E),
    onSurfaceVariant = Color.White.copy(alpha = 0.7f),
)

// ============================================================
// 主题
// ============================================================
@Composable
fun KKTVTheme(
    content: @Composable () -> Unit
) {
    val colorScheme = KKTVDarkColorScheme

    // 设置状态栏/导航栏颜色（TV上可能用不到，但不影响）
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            val window = (view.context as? Activity)?.window
            if (window != null) {
                window.statusBarColor = KKTVDarkBg.toArgb()
                window.navigationBarColor = KKTVDarkBg.toArgb()
                WindowCompat.getInsetsController(window, view)
                    .isAppearanceLightStatusBars = false
            }
        }
    }

    MaterialTheme(
        colorScheme = colorScheme,
        typography = Typography(),
        content = content,
    )
}
