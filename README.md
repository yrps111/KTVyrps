<div align="center">

# 🎤 KTVyrps

**局域网KTV系统 —— 手机点歌，电视唱歌**

用手机扫码点歌，电视大屏显示歌词，AI自动分离伴奏，在家就能开KTV！

[![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python)](https://python.org)
[![Android](https://img.shields.io/badge/Android_TV-Compose-green?logo=android)](https://developer.android.com)
[![License](https://img.shields.io/badge/License-MIT-yellow)](LICENSE)

**[中文](#-功能特性)** | **[English](#-english)**

---

## ✨ 功能特性

| 功能                | 说明                                                         |
| ------------------- | ------------------------------------------------------------ |
| 📱 **手机点歌**      | 扫码打开H5点歌台，搜索/推荐/排队一站式                       |
| 📺 **电视播放**      | Android TV端全屏歌词显示，遥控器操控                         |
| 🎵 **AI伴奏分离**    | 基于 [Demucs](https://github.com/facebookresearch/demucs) 自动分离人声与伴奏 |
| 🎤 **原唱/伴奏切换** | 一键切换，想唱就唱想听就听                                   |
| 📝 **逐字歌词**      | 支持 LRC / LXLRC逐字同步歌词，卡拉OK体验                     |
| 🔍 **在线搜索**      | 酷我/QQ音乐在线搜索，配合 [lx-music](https://github.com/lyswhut/lx-music-desktop) 获取音源 |
| 🔥 **热歌推荐**      | 多榜单推荐（热歌/新歌/飙升/流行等），分页浏览                |
| 🔁 **重唱/切歌**     | 手机端和TV端均可操控                                         |
| 📡 **自动发现**      | UDP广播自动发现，TV端零配置连接后端                          |
| 🖥️ **AMD显卡支持**   | 自动检测AMD显卡，通过 DirectML 实现GPU加速（v3.5新增）        |
| ⚡ **一键启动**       | 自动检测并安装所有依赖，自动推断路径，零配置启动（v3.5新增）  |

## 🏗️ 系统架构

![image](https://files.catbox.moe/k5mss1.png)

- **后端**（Python + Flask）：队列管理、歌曲搜索、伴奏分离、歌词解析、音频服务
- **TV端**（Kotlin + Jetpack Compose）：歌词显示、音频播放、遥控器交互
- **点歌台**（H5）：内嵌在后端，手机浏览器直接访问

## 📋 前置要求

### 必需

- **Python 3.8+**
- **[lx-music-desktop](https://github.com/lyswhut/lx-music-desktop)**（需开启「开放API服务」，默认端口 `23330`）
- **Android TV 设备**（或 Android TV 模拟器）
- 同一局域网环境

### Python 依赖

> 💡 **v3.5 起无需手动安装依赖！** 首次启动 `kktv_server.py` 会自动检测并安装所有缺失的包。

如需手动安装：
```bash
pip install flask flask-cors requests "numpy<2" qrcode pillow demucs torchaudio torchcodec
```

### GPU 加速

| 显卡         | 方案        | 说明                                              |
| ------------ | ----------- | ------------------------------------------------- |
| **NVIDIA**   | CUDA（自动）| 安装 CUDA 版 PyTorch 即可，Demucs 自动使用        |
| **AMD**      | DirectML（自动）| v3.5 启动时自动检测AMD显卡并安装 `torch-directml` |
| **无独显**   | CPU（自动） | 无GPU时自动使用CPU，速度较慢但功能完整            |

## 🚀 快速开始

### 1. 启动 lx-music

打开 lx-music-desktop，进入 **设置 → 开放API服务 → 启用**，确认端口为 `23330`。

### 2. 启动后端

```bash
cd Back
python kktv_server.py
```

首次启动会自动完成环境配置：
```
[Setup] 安装缺失依赖: demucs, torchaudio, torchcodec
[Setup] ✅ 依赖安装完成
[Setup] 检测到 AMD 显卡，正在安装 DirectML 加速支持...
[Setup] ✅ torch-directml 安装完成，音源分离将使用 AMD GPU 加速
============================================================
  🎤 KKTV Backend v3.5
  控制台:    http://localhost:8080
  点歌台:    http://192.168.x.x:8080/jukebox
============================================================
```

### 3. 安装 TV 端

将 `Andriod/` 目录用 Android Studio 打开，编译安装到 Android TV 设备。

TV端会通过 UDP 广播自动发现后端服务器，无需手动配置。

### 4. 手机点歌

用手机扫描TV 待机画面上的二维码，或直接访问 `http://<你的IP>:8080/jukebox`。

## 🎮 TV遥控器操作

| 按键           | 功能          |
| -------------- | ------------- |
| **OK（短按）** | 切换原唱/伴奏 |
| **OK（长按）** | 打开设置      |
| **↑ / ↓**      | 调节音量      |
| **←**          | 重唱          |
| **→**          | 切歌          |

## 📁 项目结构

```
KTVyrps/
├── Andriod/                  # Android TV 客户端
│   ├── app/src/main/java/com/example/kktv/
│   │   ├── MainActivity.kt          # 主界面
│   │   ├── api/
│   │   │   └── KKTVApiClient.kt     # API 客户端
│   │   ├── player/
│   │   │   └── KaraokePlayer.kt     # 音频播放器
│   │   ├── model/
│   │   │   └── Models.kt            # 数据模型
│   │   └── ui/
│   │       ├── components/
│   │       │   └── LyricDisplay.kt  # 歌词组件
│   │       └── theme/               # 主题
│   └── build.gradle.kts
├── Back/                     # Python 后端
│   └── kktv_server.py        # 后端服务（Flask）
├── .gitignore
├── LICENSE
└── README.md
```

## ⚙️ 配置说明

后端启动后，访问 `http://localhost:8080` 进入控制台，可修改：

| 配置项     | 默认值      | 说明                                       |
| ---------- | ----------- | ------------------------------------------ |
| 播放器IP   | `127.0.0.1` | lx-music 所在IP                            |
| 播放器端口 | `23330`     | lx-music API端口                           |
| 音乐目录   | 自动推断    | v3.5起自动定位到项目根目录下的 `vedio/`      |
| 分离缓存   | 自动推断    | v3.5起自动定位到项目根目录下的 `separated/`  |
| 服务端口   | `8080`      | 后端HTTP端口                               |
| 下载间隔   | `30s`       | 防止频繁触发下载                           |

## 🎵 工作流程

1. 用户在手机点歌台搜索并点歌
2. 后端通过 lx-music Scheme URL 触发搜索/播放
3. 用户在 lx-music 中手动下载歌曲到指定目录
4. 后端检测到文件后，调用 Demucs 分离伴奏
5. 分离完成后，TV端自动获取并播放

## 📸 截图

![image](https://files.catbox.moe/1krfmy.png)

![image](https://files.catbox.moe/9wt4xs.png)

![image](https://files.catbox.moe/g4g9a5.jpg)

![image](https://files.catbox.moe/6apei3.jpg)

## ⚠️ 免责声明

**本项目仅供个人学习和技术研究使用。**

- 本项目 **不提供** 任何音乐资源的下载、存储或分发功能
- 音乐文件的获取完全依赖第三方软件 [lx-music-desktop](https://github.com/lyswhut/lx-music-desktop)，两项目均不参与也不控制其音源获取过程，音源的获取请自行上网搜索
- 伴奏分离功能基于开源项目 [Demucs](https://github.com/facebookresearch/demucs)（Meta Research），仅对用户本地已有的音频文件进行处理
- 在线搜索功能仅获取歌曲元数据（歌名、歌手、专辑等），**不涉及音频流的获取或传输**
- 用户应确保其使用的音乐文件已获得合法授权，本项目开发者不对用户的使用行为承担任何法律责任
- **请尊重音乐创作者的版权**，支持正版音乐

**使用本项目即表示您已阅读并同意以上声明。如有侵权，请联系删除。**

## 💰 打赏支持

如果这个项目对你有帮助，欢迎请作者喝杯奶茶 ☕

[[未认证\]yrps正在创作一系列的好玩小应用 | 爱发电](https://afdian.com/a/yrpssss)

## 📄 License

[MIT License](LICENSE) — 随便用，开心就好。

## 🙏 致谢

- [lx-music-desktop](https://github.com/lyswhut/lx-music-desktop) — 优秀的音乐播放器
- [Demucs](https://github.com/facebookresearch/demucs) — Meta Research 的音源分离模型
- [Jetpack Compose](https://developer.android.com/jetpack/compose) — Android 现代UI框架
- [Flask](https://flask.palletsprojects.com/) — Python Web框架

---

<a id="-english"></a>

# 🎤 English

## KTVyrps — LAN Karaoke System

**Scan to pick songs on your phone, sing on your TV.**

A home karaoke system with AI-powered vocal separation. Pick songs from your phone, display lyrics on an Android TV, and let AI automatically split vocals from instrumentals.

### Features

| Feature               | Description                                                          |
| --------------------- | -------------------------------------------------------------------- |
| 📱 **Phone Song Picker** | Scan QR to open H5 jukebox — search, browse charts, queue songs    |
| 📺 **TV Playback**       | Full-screen lyrics on Android TV with remote control support        |
| 🎵 **AI Vocal Separation** | Powered by [Demucs](https://github.com/facebookresearch/demucs) — auto-split vocals & instrumentals |
| 🎤 **Vocal/Instrumental Toggle** | One-tap switch between original and karaoke track           |
| 📝 **Synced Lyrics**     | LRC / LXLRC word-by-word sync for a real karaoke experience        |
| 🔍 **Online Search**     | Search via Kuwo/QQ Music, powered by [lx-music](https://github.com/lyswhut/lx-music-desktop) |
| 🔥 **Chart Recommendations** | Hot / New / Trending / Pop and more, with pagination            |
| 🖥️ **AMD GPU Support**   | Auto-detects AMD GPUs, enables DirectML acceleration (v3.5)        |
| ⚡ **Zero-Config Setup**  | Auto-installs all dependencies and detects paths on first run (v3.5)|

### Architecture

- **Backend** (Python + Flask): Queue management, song search, vocal separation, lyrics, audio streaming
- **TV Client** (Kotlin + Jetpack Compose): Lyrics display, audio playback, remote control
- **Jukebox** (H5): Embedded web UI, accessed via phone browser

### Prerequisites

- **Python 3.8+**
- **[lx-music-desktop](https://github.com/lyswhut/lx-music-desktop)** (enable "Open API Service", default port `23330`)
- **Android TV device** (or emulator)
- All devices on the same LAN

### Quick Start

#### 1. Start lx-music

Open lx-music-desktop, go to **Settings → Open API Service → Enable**, confirm port `23330`.

#### 2. Start the backend

```bash
cd Back
python kktv_server.py
```

On first run, the server will automatically set up the environment:

```
[Setup] Installing missing dependencies: demucs, torchaudio, torchcodec
[Setup] ✅ Dependencies installed
[Setup] AMD GPU detected, installing DirectML acceleration...
[Setup] ✅ torch-directml installed, vocal separation will use AMD GPU
============================================================
  🎤 KKTV Backend v3.5
  Console:   http://localhost:8080
  Jukebox:   http://192.168.x.x:8080/jukebox
============================================================
```

#### 3. Install TV client

Open the `Andriod/` directory in Android Studio, build and install on your Android TV.

The TV client auto-discovers the backend via UDP broadcast — no manual configuration needed.

#### 4. Pick songs

Scan the QR code on the TV standby screen, or visit `http://<your-IP>:8080/jukebox` on your phone.

### GPU Acceleration

| GPU         | Method             | Notes                                               |
| ----------- | ------------------ | --------------------------------------------------- |
| **NVIDIA**  | CUDA (automatic)   | Install CUDA PyTorch; Demucs uses it automatically  |
| **AMD**     | DirectML (automatic) | v3.5 auto-detects AMD GPUs and installs `torch-directml` |
| **No GPU**  | CPU (fallback)     | Works automatically, just slower                    |

### TV Remote Controls

| Key            | Action               |
| -------------- | -------------------- |
| **OK (short)** | Toggle vocal/instrumental |
| **OK (long)**  | Open settings        |
| **↑ / ↓**      | Volume up/down       |
| **←**          | Replay current song  |
| **→**          | Skip to next song    |

### Disclaimer

**This project is for personal learning and technical research only.**

- This project does **not** provide any music download, storage, or distribution functionality
- Music files are obtained entirely through [lx-music-desktop](https://github.com/lyswhut/lx-music-desktop); this project does not participate in or control the source acquisition process
- Vocal separation is based on the open-source [Demucs](https://github.com/facebookresearch/demucs) (Meta Research), processing only local audio files
- Online search only retrieves song metadata (title, artist, album) — **no audio streams are fetched or transmitted**
- Users must ensure their music files are legally authorized; the developers assume no legal responsibility
- **Please respect music creators' copyrights** and support legitimate music

### License

[MIT License](LICENSE) — Use freely, be happy.

### Credits

- [lx-music-desktop](https://github.com/lyswhut/lx-music-desktop) — Excellent music player
- [Demucs](https://github.com/facebookresearch/demucs) — Meta Research vocal separation model
- [Jetpack Compose](https://developer.android.com/jetpack/compose) — Modern Android UI framework
- [Flask](https://flask.palletsprojects.com/) — Python web framework

---

<div align="center">
Made with ❤️ by Yrps
</div>
