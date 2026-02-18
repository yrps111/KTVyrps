# -*- coding: utf-8 -*-
"""
KKTV 后端服务 v3.4 - 修复切歌/mode同步/歌词健壮性
Author: Yrps

★ v3.4 变更:
  - 新增 /api/tv/skip TV专用切歌端点（不触发心跳skip标记，避免双跳）
  - TVState 新增 set_mode + mode_changed_at 时间戳
  - 心跳响应增加 mode_changed_at
  - _grab_lyric 增强：多编码尝试 + 更宽松匹配 + 增强日志
  - _prepare 缓存命中时从分离目录推断 file_path
  - awlrc 解析增强：re.DOTALL + 清理空白
  - 歌词端点增强日志
  - 点歌台 mode 同步改进：本地10秒保护期 + 从queue响应同步
  - 新增 /api/recommend 热歌推荐（QQ音乐/网易云多榜单+缓存）

"""

import os
import sys
import json
import time
import hashlib
import threading
import subprocess
import traceback
import io
import re
import base64
import socket
import urllib.parse
from pathlib import Path
from typing import Optional, List, Tuple, Dict
from enum import Enum

import socket
import threading
import numpy as np
import qrcode
import requests as req_lib
from flask import (
    Flask, Response, send_file,
    render_template_string, request as flask_request
)
from flask_cors import CORS


# ============================================================
# 全局配置
# ============================================================
class Config:
    PLAYER_HOST = "127.0.0.1"
    PLAYER_PORT = 23330
    MUSIC_DIR = r"F:\KKTV\vedio"
    SEPARATED_DIR = r"F:\KKTV\separated"
    LX_DOWNLOAD_DIR = ""
    SERVE_HOST = "0.0.0.0"
    SERVE_PORT = 8080
    DOWNLOAD_MIN_INTERVAL = 30
    DOWNLOAD_COOLDOWN_AFTER_FAIL = 60
    MAX_QUEUE_SIZE = 50
    LAN_IP = "192.168.110.37"
    SCHEME_PREFIX = "lxmusic://"


def _detect_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return Config.LAN_IP


Config.LAN_IP = _detect_lan_ip()

for _d in [Config.MUSIC_DIR, Config.SEPARATED_DIR]:
    Path(_d).mkdir(parents=True, exist_ok=True)


def start_discovery_service():
    """UDP发现服务——被动监听 + 启动时主动广播"""
    DISCOVERY_PORT = 8081
    MAGIC = b"KKTV_DISCOVER"
    ANNOUNCE = b"KKTV_ANNOUNCE"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))
    print("[Discovery] 监听 UDP:{} 等待TV发现...".format(DISCOVERY_PORT))

    response_data = json.dumps({
        "service": "KKTV",
        "host": Config.LAN_IP,
        "port": Config.SERVE_PORT,
    }).encode("utf-8")

    # ★ 启动时主动广播3次，通知已在线的TV
    for i in range(3):
        try:
            broadcast_addr = ("255.255.255.255", DISCOVERY_PORT + 1)
            sock.sendto(ANNOUNCE + b"|" + response_data, broadcast_addr)
            print("[Discovery] 主动广播 #{} -> port {}".format(
                i + 1, DISCOVERY_PORT + 1))
            time.sleep(1)
        except Exception as e:
            print("[Discovery] 广播异常: {}".format(e))

    # ★ 之后持续被动监听
    while True:
        try:
            data, addr = sock.recvfrom(1024)
            if data.startswith(MAGIC):
                sock.sendto(response_data, addr)
                print("[Discovery] 回复 {} -> {}:{}".format(
                    addr, Config.LAN_IP, Config.SERVE_PORT))
        except Exception as e:
            print("[Discovery] 监听异常: {}".format(e))


# ============================================================
# 工具
# ============================================================
def json_resp(data, status=200):
    return Response(
        json.dumps(data, ensure_ascii=False),
        status=status,
        mimetype="application/json; charset=utf-8"
    )


def audio_mime(path):
    m = {
        '.mp3': 'audio/mpeg', '.flac': 'audio/flac',
        '.wav': 'audio/wav', '.ogg': 'audio/ogg',
        '.m4a': 'audio/mp4', '.aac': 'audio/aac',
    }
    return m.get(Path(path).suffix.lower(), 'application/octet-stream')


AUDIO_EXTS = {'.mp3', '.flac', '.wav', '.ogg', '.m4a', '.aac', '.wma'}


# ============================================================
# lx-music API 客户端
# ============================================================
class LxMusicClient:

    def __init__(self):
        self._base = "http://{}:{}".format(Config.PLAYER_HOST, Config.PLAYER_PORT)

    def update_endpoint(self, host, port):
        self._base = "http://{}:{}".format(host, port)
        Config.PLAYER_HOST = host
        Config.PLAYER_PORT = port

    def _get(self, path, params=None, timeout=5):
        url = "{}{}".format(self._base, path)
        try:
            r = req_lib.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r
        except Exception:
            return None

    def is_connected(self):
        try:
            r = req_lib.get("{}/status".format(self._base),
                            params={"filter": "status"}, timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def get_status(self, fields=None):
        p = {"filter": fields} if fields else None
        r = self._get("/status", p)
        return r.json() if r else None

    def get_lyric(self):
        r = self._get("/lyric")
        return r.text if r else None

    def get_lyric_all(self):
        r = self._get("/lyric-all")
        return r.json() if r else None

    def control(self, action, params=None):
        r = self._get("/{}".format(action), params)
        return r is not None

    def play(self):
        return self.control("play")

    def pause(self):
        return self.control("pause")

    def skip_next(self):
        return self.control("skip-next")

    def skip_prev(self):
        return self.control("skip-prev")

    def seek(self, offset):
        return self.control("seek", {"offset": offset})

    def set_volume(self, vol):
        return self.control("volume", {"volume": vol})

    def set_mute(self, mute):
        return self.control("mute", {"mute": str(mute).lower()})

    def collect(self):
        return self.control("collect")

    def uncollect(self):
        return self.control("uncollect")

    # ★★★ FIX: _call_scheme —— Windows的return True修正 + 去重★★★
    def _call_scheme(self, path):
        url = "{}{}".format(Config.SCHEME_PREFIX, path)
        print("[Scheme] {}".format(url[:120]))
        try:
            if sys.platform == "win32":
                os.startfile(url)
            else:
                subprocess.Popen(["xdg-open", url],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)# ★ FIX: return True 放在 if/else 外面，两个平台都能走到
            return True
        except Exception as e:
            print("[Scheme] 调用失败: {}".format(e))
            return False

    # ★★★ FIX: scheme_search_play —— 只调一次scheme，不盲目重试 ★★★
    def scheme_search_play(self, name, singer="", album_name="",interval="", play_later=False):
        data = {"name": name}
        if singer:
            data["singer"] = singer
        if album_name:
            data["albumName"] = album_name
        if interval:
            data["interval"] = interval
        data["playLater"] = play_later
        json_str = json.dumps(data, ensure_ascii=False)
        encoded = urllib.parse.quote(json_str)
        path = "music/searchPlay?data={}".format(encoded)

        # ★ FIX: 只调一次scheme，不重试（重试会导致lx反复打开搜索）
        ok = self._call_scheme(path)
        if ok:
            print("[Scheme] 调用成功")
        else:
            print("[Scheme] 调用失败")
        return ok

    def scheme_play_song(self, name, singer, source, songmid,
                         types=None, album_name="", interval="",
                         img="", album_id="",
                         str_media_mid="", album_mid=""):
        """★ 精确播放：使用 music/play 接口，传入平台歌曲ID。根据 source 自动附加平台特定参数。
        """
        if not types:
            types = [{"type": "128k"}, {"type": "320k"}]

        data = {
            "name": name,
            "singer": singer,
            "source": source,
            "songmid": str(songmid),
            "types": types,
        }
        if album_name:
            data["albumName"] = album_name
        if interval:
            data["interval"] = interval
        if img:
            data["img"] = img
        if album_id:
            data["albumId"] = str(album_id)

        # ★★★ 平台特定参数 ★★★
        if source == "tx":
            # tx源必传 strMediaMid
            data["strMediaMid"] = str(str_media_mid or songmid)
            if album_mid:
                data["albumMid"] = str(album_mid)

        json_str = json.dumps(data, ensure_ascii=False)
        encoded = urllib.parse.quote(json_str)
        path = "music/play?data={}".format(encoded)

        ok = self._call_scheme(path)
        if ok:
            print("[Scheme] 精确播放: {} - {} (source={}, mid={})".format(
                name, singer, source, songmid))
        else:
            print("[Scheme] 精确播放调用失败")
    
        return ok



    def scheme_search_play_simple(self, name_singer):
        encoded = urllib.parse.quote(name_singer)
        path = "music/searchPlay/{}".format(encoded)
        return self._call_scheme(path)

    # ★★★ FIX: pause_with_retry —— 用API确认暂停 ★★★
    def pause_with_retry(self, max_retries=5, interval=0.5):
        """暂停播放，用API轮询确认真的暂停了"""
        for i in range(max_retries):
            self.pause()
            time.sleep(interval)
            status = self.get_status("status")
            if status and status.get("status") != "playing":
                print("[LX] 暂停确认成功 (attempt {})".format(i + 1))
                return True
            print("[LX] 暂停未生效，重试 {}...".format(i + 1))
        print("[LX] ⚠️ 暂停重试全部失败")
        return False

    # ★★★ FIX: wait_until_playing —— 用API轮询等待播放开始 ★★★
    def wait_until_playing(self, song_name="", max_wait=15):
        """
        用API轮询等待lx-music开始播放。
        不检查歌名匹配——只要lx在播放就返回True。
        歌名匹配太严格会导致误判超时。
        """
        name_l = song_name.lower().strip()
        waited = 0.0
        while waited < max_wait:
            time.sleep(0.5)
            waited += 0.5
            try:
                status = self.get_status("status,name,singer")
                if not status:
                    continue
                lx_status = status.get("status", "")
                if lx_status == "playing":
                    lx_name = (status.get("name") or "").lower()
                    lx_singer = (status.get("singer") or "").lower()
                    print("[LX] 检测到播放: '{}' by '{}' (等待{:.1f}s)".format(
                        status.get("name"), status.get("singer"), waited))
                    #宽松匹配：歌名互相包含 或 不检查歌名（scheme调用后lx播的就是目标歌）
                    if not name_l or name_l in lx_name or lx_name in name_l:
                        return True
                    # 即使歌名不完全匹配，scheme调用后lx播放的大概率就是目标歌
                    # 等3秒后如果lx确实在播放，也认为成功
                    if waited >= 3.0:
                        print("[LX] 歌名不完全匹配但lx在播放，视为成功: lx='{}' vs target='{}'".format(
                            lx_name, name_l))
                        return True
            except Exception:
                pass
        print("[LX] 等待播放超时 ({:.1f}s)".format(waited))
        return False

    def verify_playing_song(self, song_name, song_singer=""):
        """
        验证lx-music当前播放的歌曲是否是目标歌曲。
        返回 (is_correct, lx_status_dict)
        """
        status = self.get_status("name,singer,albumName")
        if not status or status.get("status") not in ("playing", "paused"):
            return False, status

        lx_name = (status.get("name") or "").lower().strip()
        lx_singer = (status.get("singer") or "").lower().strip()
        target_name = song_name.lower().strip()
        target_singer = song_singer.lower().strip() if song_singer else ""

        # ★ 检查1:伴奏/纯音乐 版本误匹配
        instrumental_tags = ["伴奏", "inst", "instrumental", "off vocal", "karaoke"]
        lx_is_instrumental = any(tag in lx_name for tag in instrumental_tags)
        target_is_instrumental = any(tag in target_name for tag in instrumental_tags)

        if lx_is_instrumental and not target_is_instrumental:
            print("[Verify] ❌ 伴奏版误匹配: lx='{}' vs target='{}'".format(
                status.get("name"), song_name))
            return False, status

        if not lx_is_instrumental and target_is_instrumental:
            print("[Verify] ❌ 需要伴奏但播放的是人声版: lx='{}' vs target='{}'".format(
                status.get("name"), song_name))
            return False, status

        # ★ 检查2: 歌手匹配（至少有一个歌手重叠）
        if target_singer:
            #拆分多歌手: "张杰, HOYO-MiX" → ["张杰", "hoyo-mix"]
            import re as _re
            target_parts = [s.strip() for s in _re.split(r'[,，、/]', target_singer) if s.strip()]
            lx_parts = [s.strip() for s in _re.split(r'[,，、/]', lx_singer) if s.strip()]

            has_overlap = False
            for tp in target_parts:
                for lp in lx_parts:
                    if tp in lp or lp in tp:
                        has_overlap = True
                        break
                if has_overlap:
                    break

            #★ 歌手完全不重叠时，如果歌名也不是精确匹配，判定为错误
            if not has_overlap:
                name_exact = (target_name == lx_name or
                              target_name in lx_name or
                              lx_name in target_name)
                if not name_exact:
                    print("[Verify] ❌ 歌手+歌名均不匹配: lx='{}'-'{}' vs target='{}'-'{}'".format(
                        status.get("name"), status.get("singer"), song_name, song_singer))
                    return False, status# 歌名匹配但歌手不匹配——可能是同名歌，记录警告但不阻断
                print("[Verify] ⚠️ 歌名匹配但歌手不同: lx_singer='{}' vs target_singer='{}'".format(
                    lx_singer, target_singer))

        print("[Verify] ✅ 验证通过: lx='{}' by '{}'".format(
            status.get("name"), status.get("singer")))
        return True, status





lx = LxMusicClient()


#============================================================
# ★酷我音乐 Token 管理器
# ============================================================
class KuwoTokenManager:
    """酷我API需要csrf token，从首页cookie获取并缓存"""
    _token = ""
    _token_time = 0.0
    _lock = threading.Lock()
    TOKEN_TTL = 600  # 10分钟刷新

    @classmethod
    def get_token(cls):
        # type: () -> str
        with cls._lock:
            if cls._token and time.time() - cls._token_time < cls.TOKEN_TTL:
                return cls._token
        try:
            resp = req_lib.get("http://www.kuwo.cn/", headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) ""AppleWebKit/537.36 Chrome/120.0.0.0"),}, timeout=8, allow_redirects=True)
            token = resp.cookies.get("kw_token", "")
            if token:
                with cls._lock:
                    cls._token = token
                    cls._token_time = time.time()
                print("[Kuwo] token获取成功: {}...".format(token[:16]))
                return token
        except Exception as e:
            print("[Kuwo] token获取失败: {}".format(e))
        return cls._token  # 返回旧token兜底



# ============================================================
# ★ 搜索模块 v3.3 — 恢复 QQ/网易在线搜索 + 本地搜索
# ============================================================
class MusicSearcher:
    """
    v3.3: 恢复在线元数据搜索 + 本地文件搜索。
    在线搜索只获取歌曲列表信息（名字/歌手/专辑/时长），
    实际音频下载仍然全部通过 lx-music Scheme URL 完成。
    用户可以在搜索结果列表中自由选择想唱的版本。
    """

    @staticmethod
    def search(keyword, limit=30):
        local = MusicSearcher._search_local(keyword)
        online = []
        try:
            online = MusicSearcher._search_online(keyword, limit)
        except Exception as e:
            print("[搜索] 在线搜索异常: {}".format(e))
            traceback.print_exc()

        # ★★★ FIX: 构建本地 (name, singer) 集合 ★★★
        local_keys = set()
        for r in local:
            if r.get("singer"):
                local_keys.add((r["name"].lower(), r["singer"].lower()))

        #★★★ FIX: 统计在线结果中每个 (name, singer) 出现次数 ★★★
        from collections import Counter
        online_key_count = Counter()
        for r in online:
            k = (r["name"].lower(), r["singer"].lower())
            online_key_count[k] += 1

        for r in online:
            online_key = (r["name"].lower(), r["singer"].lower())
            #★ 只有当在线结果中该 (name, singer) 唯一时才标已下载
            #多版本（不同专辑/时长）时不标，避免用户误以为都已下载
            if online_key in local_keys and online_key_count[online_key] == 1:
                r["is_local"] = True
            else:
                r["is_local"] = False

        return {
            "local": local,
            "online": online,
            "local_count": len(local),
            "online_count": len(online),
            "keyword": keyword,
        }



    @staticmethod
    def _search_local(keyword):
        # type: (str) -> list
        music_dir = Path(Config.MUSIC_DIR)
        if not music_dir.exists():
            return []
        keywords = keyword.lower().split()
        results = []
        sep_dir = Path(Config.SEPARATED_DIR)
        for f in music_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
                continue
            stem_lower = f.stem.lower()
            if not all(kw in stem_lower for kw in keywords):
                continue
            # 文件名解析: "歌曲名 - 艺术家 (备注).mp3"
            parts = f.stem.split(" - ", 1)
            if len(parts) == 2:
                song_name = parts[0].strip()
                singer = parts[1].strip()
                singer_clean = re.sub(r'\s*[（(][^）)]*[）)]$', '', singer)
            else:
                song_name = f.stem.strip()
                singer = ""
                singer_clean = ""
            has_cache = (sep_dir / f.stem / "no_vocals.wav").exists()
            results.append({
                "name": song_name,
                "singer": singer_clean or singer,
                "album": "",
                "duration": 0,
                "interval": "",
                "source": "local",
                "file_path": str(f),
                "has_cache": has_cache,
                "size_mb": round(f.stat().st_size / (1024 * 1024), 2),
            })
        results.sort(key=lambda x: (not x["has_cache"], x["name"]))
        return results

    @staticmethod
    def _search_online(keyword, limit=30):
        # type: (str, int) -> List[dict]
        """★ v3.5:酷我音乐优先→ QQ音乐备选
        """
        results = []  # type: List[dict]
        # 优先酷我
        try:
            results = MusicSearcher._api_kuwo(keyword, limit)
            if results:
                return results
        except Exception as e:
            print("[搜索] 酷我: {}".format(e))
        # 备用：QQ音乐
        try:
            results = MusicSearcher._api_qq(keyword, limit)
            if results:
                return results
        except Exception as e:
            print("[搜索] QQ音乐: {}".format(e))
        return results

    @staticmethod
    def _api_kuwo(keyword, limit):
        # type: (str, int) -> List[dict]
        token = KuwoTokenManager.get_token()
        url = "http://www.kuwo.cn/api/www/search/searchMusicBykeyWord"
        params = {
            "key": keyword,
            "pn": 1,
            "rn": limit,
            "httpsStatus": 1,
        }
        headers = {
            "Referer": "http://www.kuwo.cn/search/list?key={}".format(
                urllib.parse.quote(keyword)),
            "Cookie": "kw_token={}".format(token),
            "csrf": token,
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) ""AppleWebKit/537.36 Chrome/120.0.0.0"),
        }
        resp = req_lib.get(url, params=params, headers=headers, timeout=8)
        resp.encoding = "utf-8"
        data = resp.json()

        if data.get("code") != 200 or "data" not in data:
            return []

        results = []
        song_list = data["data"].get("list", [])
        for s in song_list:
            dur_sec = s.get("duration", 0)
            if isinstance(dur_sec, str):
                try:
                    dur_sec = int(dur_sec)
                except ValueError:
                    dur_sec = 0
            interval = ""
            if dur_sec > 0:
                interval = "{:02d}:{:02d}".format(dur_sec // 60, dur_sec % 60)

            #★★★ FIX: 提取 rid 作为 songmid ★★★
            rid = s.get("rid", s.get("musicrid", ""))
            #酷我有时返回 "MUSIC_12345" 格式，取纯数字
            if isinstance(rid, str) and rid.startswith("MUSIC_"):
                rid = rid[6:]
            rid = str(rid) if rid else ""

            # ★ 提取可用音质
            has_lossless = s.get("hasLossless", False)
            types_list = [{"type": "128k"}, {"type": "320k"}]
            if has_lossless:
                types_list.append({"type": "flac"})

            results.append({
                "name": s.get("name", ""),
                "singer": s.get("artist", ""),
                "album": s.get("album", ""),
                "duration": dur_sec,
                "interval": interval,
                "source": "online",
                "songmid": rid,           # ★ 新增
                "search_source": "kw",    # ★ 新增：标记来自酷我
                "types": types_list,# ★ 新增
            })
        return results



    @staticmethod
    def _api_netease(keyword, limit):
        # type: (str, int) -> List[dict]
        url = "https://music.163.com/api/search/get/web"
        headers = {
            "Referer": "https://music.163.com/",
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0"),
            "Content-Type": "application/x-www-form-urlencoded"
        }
        resp = req_lib.post(url, data={"s": keyword, "type": 1,
                                       "limit": limit, "offset": 0},
                            headers=headers, timeout=8)
        resp.encoding = "utf-8"
        data = resp.json()
        if data.get("code") != 200 or "result" not in data:
            return []
        results = []
        for s in data["result"].get("songs", []):
            artists = ", ".join(
                a.get("name", "") for a in s.get("artists", [])
            )
            dur_ms = s.get("duration", 0)
            dur_sec = round(dur_ms / 1000.0) if dur_ms else 0
            interval = ""
            if dur_sec > 0:
                interval = "{:02d}:{:02d}".format(dur_sec // 60, dur_sec % 60)
            results.append({
                "name": s.get("name", ""),
                "singer": artists,
                "album": s.get("album", {}).get("name", ""),
                "duration": dur_sec,
                "interval": interval,
                "source": "online",
            })
        return results

    @staticmethod
    def _api_qq(keyword, limit):
        # type: (str, int) -> List[dict]
        url = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"
        params = {
            "w": keyword, "format": "json",
            "p": 1, "n": limit, "cr": 1, "new_json": 1
        }
        headers = {
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 AppleWebKit/537.36"}
        resp = req_lib.get(url, params=params, headers=headers, timeout=8)
        resp.encoding = "utf-8"
        data = resp.json()
        songs = (data.get("data", {}).get("song", {}).get("list", []))
        results = []
        for s in songs:
            singers = ", ".join(
                x.get("name", "") for x in s.get("singer", [])
            )
            dur_sec = s.get("interval", 0)
            interval = ""
            if dur_sec > 0:
                interval = "{:02d}:{:02d}".format(dur_sec // 60, dur_sec % 60)

            # ★★★ FIX: 提取 tx 源字段 ★★★
            song_mid = s.get("mid", s.get("songmid", ""))
            str_media_mid = s.get("strMediaMid", song_mid)
            album_mid = s.get("album", {}).get("mid", "")

            results.append({
                "name": s.get("name", s.get("title", "")),
                "singer": singers,
                "album": s.get("album", {}).get("name", ""),
                "duration": dur_sec,
                "interval": interval,
                "source": "online",
                "songmid": str(song_mid),
                "str_media_mid": str(str_media_mid),
                "album_mid": str(album_mid),
                "search_source": "tx",
            })
        return results



# ============================================================
# ★ 热歌推荐模块 v2.0 — 分页缓存 + 多用户安全
# ============================================================
class MusicRecommender:
    """
    v3.6: 推荐榜单全部来自QQ音乐（tx源），删除酷我榜单。
    """

    _cache = {}            # type: Dict[str, list]
    _cache_ts = {}         # type: Dict[str, float]
    _locks = {}            # type: Dict[str, threading.Lock]
    _global_lock = threading.Lock()
    CACHE_TTL = 1800
    PAGE_SIZE = 15
    MAX_SONGS = 300
    CHARTS = {
        "hot":{"name": "热歌榜","source": "qq", "topId": 26},
        "new":      {"name": "新歌榜",     "source": "qq", "topId": 27},
        "surge":    {"name": "飙升榜",     "source": "qq", "topId": 62},
        "pop":      {"name": "流行指数榜", "source": "qq", "topId": 4},
        "variety":  {"name": "综艺新歌榜", "source": "qq", "topId": 67},
        "movie":    {"name": "影视金曲榜", "source": "qq", "topId": 59},
        "network":  {"name": "网络歌曲榜", "source": "qq", "topId": 28},
        "kpop":     {"name": "韩国榜",     "source": "qq", "topId": 16},
        "jpop":     {"name": "日本榜",     "source": "qq", "topId": 17},
        "western":  {"name": "欧美榜",     "source": "qq", "topId": 3},
    }



    @classmethod
    def _get_lock(cls, chart_key):
        # type: (str) -> threading.Lock
        """为每个榜单创建独立锁，避免全局锁瓶颈"""
        with cls._global_lock:
            if chart_key not in cls._locks:
                cls._locks[chart_key] = threading.Lock()
            return cls._locks[chart_key]

    @classmethod
    def get_page(cls, chart_key, page=1):
        # type: (str, int) -> dict
        """
        获取指定榜单的指定页。
        page 从 1 开始。
        返回该页的歌曲 + 分页元信息。
        """
        if chart_key not in cls.CHARTS:
            return {"ok": False, "msg": "未知榜单: {}".format(chart_key),
                    "songs": [], "page": page, "total_pages": 0, "total_songs": 0}

        page = max(1, page)
        lock = cls._get_lock(chart_key)

        # ★ 检查缓存是否存在且未过期
        now = time.time()
        need_fetch = False
        with lock:
            if chart_key not in cls._cache:
                need_fetch = True
            else:
                age = now - cls._cache_ts.get(chart_key, 0)
                if age >= cls.CACHE_TTL:
                    need_fetch = True

        # ★ 需要拉取（拉取过程中不持有锁太久，只在写入时加锁）
        if need_fetch:
            songs = cls._fetch_all(chart_key)
            if songs is not None:
                with lock:
                    cls._cache[chart_key] = songs
                    cls._cache_ts[chart_key] = time.time()
                    print("[Recommend] 缓存写入: {} -> {} 首".format(
                        chart_key, len(songs)))
            else:
                # 拉取失败，尝试用过期缓存
                with lock:
                    if chart_key in cls._cache:
                        songs = cls._cache[chart_key]
                        print("[Recommend] 拉取失败，降级用过期缓存: {} ({} 首)".format(
                            chart_key, len(songs)))
                    else:
                        return {"ok": False, "msg": "获取榜单失败",
                                "songs": [], "page": page,
                                "total_pages": 0, "total_songs": 0}

        # ★ 从缓存中读取（只读，不需要长时间持锁）
        with lock:
            all_songs = cls._cache.get(chart_key, [])

        total_songs = len(all_songs)
        total_pages = max(1, (total_songs + cls.PAGE_SIZE - 1) // cls.PAGE_SIZE)

        # ★ 页码越界保护
        if page > total_pages:
            page = total_pages

        start = (page - 1) * cls.PAGE_SIZE
        end = min(start + cls.PAGE_SIZE, total_songs)
        page_songs = all_songs[start:end]

        return {
            "ok": True,
            "chart_key": chart_key,
            "chart_name": cls.CHARTS[chart_key]["name"],
            "songs": page_songs,
            "page": page,
            "page_size": cls.PAGE_SIZE,
            "total_pages": total_pages,
            "total_songs": total_songs,
            "has_next": page < total_pages,
            "has_prev": page > 1,
            "cached_at": cls._cache_ts.get(chart_key, 0),
        }

    @classmethod
    def _fetch_all(cls, chart_key):
        # type: (str) -> Optional[list]
        chart_info = cls.CHARTS[chart_key]
        try:
            songs = cls._fetch_qq_chart(chart_info["topId"], cls.MAX_SONGS)
            if songs:
                print("[Recommend] 拉取成功: {} -> {} 首".format(chart_key, len(songs)))
            return songs if songs else None
        except Exception as e:
            print("[Recommend] 拉取失败 {}: {}".format(chart_key, e))
            traceback.print_exc()
            return None



    @classmethod
    def get_all_charts_info(cls):
        # type: () -> list
        """返回所有可用榜单的元信息"""
        result = []
        now = time.time()
        for key, info in cls.CHARTS.items():
            lock = cls._get_lock(key)
            with lock:
                cached = key in cls._cache
                age = now - cls._cache_ts.get(key, 0) if cached else -1
                total = len(cls._cache[key]) if cached else 0
            result.append({
                "key": key,
                "name": info["name"],
                "source": info["source"],
                "cached": cached,
                "cache_age": round(age, 0) if cached else -1,
                "total_songs": total,
            })
        return result

    @classmethod
    def _fetch_qq_chart(cls, top_id, limit):
        # type: (int, int) -> list
        url = "https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg"
        params = {
            "topid": top_id,
            "needNewCode": 0,
            "uin": 0,
            "tpl": 3,
            "page": "detail",
            "type": "top",
            "platform": "h5",
            "format": "json",
            "song_begin": 0,
            "song_num": limit,
        }
        headers = {
            "Referer": "https://y.qq.com/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0",
        }

        resp = req_lib.get(url, params=params, headers=headers, timeout=15)
        resp.encoding = "utf-8"
        data = resp.json()

        songs = []
        song_list = data.get("songlist", [])
        for item in song_list[:limit]:
            d = item.get("data", item)
            singers = ", ".join(
                s.get("name", "") for s in d.get("singer", [])
            )
            album_name = d.get("albumname", "")
            dur_sec = d.get("interval", 0)
            interval = ""
            if dur_sec > 0:
                interval = "{:02d}:{:02d}".format(dur_sec // 60, dur_sec % 60)

            # ★★★ FIX: 提取 tx源必需的字段 ★★★
            song_mid = d.get("songmid", "")
            str_media_mid = d.get("strMediaMid", song_mid)  # 兜底用songmid
            album_mid = d.get("albummid", "")

            songs.append({
                "name": d.get("songname", d.get("name", "")),
                "singer": singers,
                "album": album_name,
                "duration": dur_sec,
                "interval": interval,
                "source": "online",
                "songmid": song_mid,            # ★ QQ音乐歌曲ID
                "str_media_mid": str_media_mid,  # ★ tx源必传
                "album_mid": album_mid,          # ★ tx源选传
                "search_source": "tx",           # ★ 标记来自QQ音乐
            })
        return songs



    @classmethod
    def clear_cache(cls, chart_key=None):
        # type: (Optional[str]) -> None
        if chart_key:
            lock = cls._get_lock(chart_key)
            with lock:
                cls._cache.pop(chart_key, None)
                cls._cache_ts.pop(chart_key, None)
        else:
            with cls._global_lock:
                cls._cache.clear()
                cls._cache_ts.clear()







# ============================================================
# 歌曲状态
# ============================================================
class SongState(Enum):
    QUEUED = "queued"
    WAITING_DOWNLOAD = "waiting_download"
    DOWNLOADING = "downloading"
    NEEDS_DOWNLOAD = "needs_download"
    DOWNLOADED = "downloaded"
    SEPARATING = "separating"
    READY = "ready"
    PLAYING = "playing"
    PLAYED = "played"
    ERROR = "error"


class SongInfo:
    def __init__(self, name, singer, source="online", album="",
                 interval="", file_path="", songmid="", search_source="",
                 str_media_mid="", album_mid=""):
        self.name = name
        self.singer = singer
        self.source = source
        self.album = album
        self.interval = interval
        self.file_path = file_path
        self.songmid = songmid
        self.search_source = search_source
        self.str_media_mid = str_media_mid    # ★ tx源必传
        self.album_mid = album_mid            # ★ tx源选传
        self.state = SongState.QUEUED
        self.error_msg = ""
        self.vocals_path = ""
        self.accompaniment_path = ""
        self.lrc = ""
        self.tlyric = ""
        self.rlyric = ""
        self.lxlyric = ""
        self.queued_at = time.time()
        self.ready_at = 0.0
        self._collected = False
        self._download_triggered_at = 0.0
        self.uid = hashlib.md5(
            "{}-{}-{:.4f}".format(name, singer, time.time()).encode()
        ).hexdigest()[:12]


    def to_dict(self):
        return {
            "uid": self.uid,
            "name": self.name,
            "singer": self.singer,
            "album": self.album,
            "source": self.source,
            "state": self.state.value,
            "error_msg": self.error_msg,
            "has_accompaniment": bool(self.accompaniment_path),
            "has_vocals": bool(self.vocals_path),
            "has_lyric": bool(self.lrc or self.lxlyric),
            "queued_at": self.queued_at,
            "ready_at": self.ready_at,}



# ============================================================
# 下载保护器
# ============================================================
class DownloadGuard:
    def __init__(self):
        self._lock = threading.Lock()
        self.is_downloading = False
        self.last_time = 0.0
        self.count = 0
        self.fails = 0

    def can_download(self):
        with self._lock:
            if self.is_downloading:
                return False, "有下载任务进行中"
            elapsed = time.time() - self.last_time
            if elapsed < Config.DOWNLOAD_MIN_INTERVAL:
                return False, "冷却中({:.0f}s)".format(
                    Config.DOWNLOAD_MIN_INTERVAL - elapsed)
            return True, "ok"

    def start(self):
        with self._lock:
            self.is_downloading = True

    def finish(self, success=True):
        with self._lock:
            self.is_downloading = False
            self.last_time = time.time()
            self.count += 1
            if not success:
                self.fails += 1
                self.last_time += Config.DOWNLOAD_COOLDOWN_AFTER_FAIL

    def status(self):
        with self._lock:
            cd = max(0, Config.DOWNLOAD_MIN_INTERVAL -
                     (time.time() - self.last_time))
            return {
                "is_downloading": self.is_downloading,
                "cooldown": round(cd, 1),
                "total": self.count,
                "fails": self.fails,
            }


dl_guard = DownloadGuard()


# ============================================================
# TV端播放状态
# ============================================================
class TVState:
    def __init__(self):
        self._lock = threading.Lock()
        self.connected = False
        self.playing = False
        self.song_uid = ""
        self.song_name = ""
        self.singer = ""
        self.progress = 0.0
        self.duration = 0.0
        self.mode = "accompaniment"
        self.mode_changed_at = 0.0
        self.mic_volume = 80
        self.music_volume = 70
        self.last_beat = 0.0
        # ★ FIX: 布尔标记 → 递增计数器
        self._skip_counter = 0
        self._replay_counter = 0

    def set_mode(self, mode):
        with self._lock:
            if mode in ("accompaniment", "original") and mode != self.mode:
                self.mode = mode
                self.mode_changed_at = time.time()

    def update(self, data):
        with self._lock:
            for k in ("playing", "song_uid", "song_name", "singer",
                       "progress", "duration",
                       "mic_volume", "music_volume"):
                if k in data:
                    setattr(self, k, data[k])
            self.last_beat = time.time()
            self.connected = True

    def to_dict(self):
        with self._lock:
            age = time.time() - self.last_beat if self.last_beat else -1
            if age > 15 and self.last_beat > 0:
                self.connected = False
            return {
                "connected": self.connected,
                "playing": self.playing,
                "song_uid": self.song_uid,
                "song_name": self.song_name,
                "singer": self.singer,
                "progress": round(self.progress, 2),
                "duration": round(self.duration, 2),
                "mode": self.mode,
                "mode_changed_at": round(self.mode_changed_at, 3),
                "mic_volume": self.mic_volume,
                "music_volume": self.music_volume,
                "heartbeat_age": round(age, 1) if age >= 0 else -1,
                # ★ FIX
                "skip_counter": self._skip_counter,
                "replay_counter": self._replay_counter,
            }

    def is_finished(self):
        with self._lock:
            if (not self.playing and self.progress > 0
                    and self.duration > 0
                    and self.progress >= self.duration - 1.5):
                return True
            return False



tv = TVState()


# ============================================================
# ★ LRC 歌词解析（修复 awlrc 标签支持）
# ============================================================
class LrcParser:

    @staticmethod
    def parse(text):
        """解析普通 LRC 歌词（行级）"""
        if not text:
            return []
        tp = re.compile(r'\[(\d{1,3}):(\d{1,2})\.(\d{1,3})\]')
        res = []
        for line in text.strip().split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            # 跳过 metadata 行
            if re.match(r'^\[(ti|ar|al|by|offset|kuwo|ver|awlrc):', stripped):
                continue
            ms = list(tp.finditer(line))
            if not ms:
                continue
            txt = tp.sub('', line).strip()
            if not txt:
                continue
            for m in ms:
                mn, sc = int(m.group(1)), int(m.group(2))
                mss = m.group(3)
                if len(mss) == 1:
                    msi = int(mss) * 100
                elif len(mss) == 2:
                    msi = int(mss) * 10
                else:
                    msi = int(mss)
                t = mn * 60 + sc + msi / 1000.0
                res.append({"time": round(t, 3), "text": txt})
        res.sort(key=lambda x: x["time"])
        for i in range(len(res)):
            if i < len(res) - 1:
                res[i]["duration"] = round(
                    res[i + 1]["time"] - res[i]["time"], 3)
            else:
                res[i]["duration"] = 5.0
        return res

    @staticmethod
    def parse_enhanced(text):
        """自动判断是否含逐字标记，选择对应解析器"""
        if not text:
            return []
        if '<' in text and '>' in text:
            return LrcParser._parse_words(text)
        return LrcParser.parse(text)

    @staticmethod
    def _parse_words(text):
        """解析带逐字标记的 LRC（lxlrc 格式）
        格式: [MM:SS.mmm]<offset_ms,duration_ms>字<offset_ms,duration_ms>字...
        """
        tp = re.compile(r'\[(\d{1,3}):(\d{1,2})\.(\d{1,3})\]')
        wp = re.compile(r'<(\d+),(\d+)>([^<]*)')
        res = []
        for line in text.strip().split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            # ★ 跳过 metadata 行（包括 awlrc, ti, ar, al, by, offset, ver, kuwo 等）
            if re.match(r'^\[(ti|ar|al|by|offset|kuwo|ver|awlrc):', stripped):
                continue

            tm = tp.search(line)
            if not tm:
                continue
            mn, sc = int(tm.group(1)), int(tm.group(2))
            mss = tm.group(3)
            if len(mss) == 1:
                msi = int(mss) * 100
            elif len(mss) == 2:
                msi = int(mss) * 10
            else:
                msi = int(mss)
            lt = mn * 60 + sc + msi / 1000.0
            body = tp.sub('', line).strip()
            words = []
            full = ""
            for wm in wp.finditer(body):
                wo = int(wm.group(1)) / 1000.0
                wd = int(wm.group(2)) / 1000.0
                wt = wm.group(3)
                full += wt
                words.append({"text": wt,
                               "offset": round(wo, 3),
                               "duration": round(wd, 3)})
            if not full:
                full = body
            if not full.strip():
                continue
            entry = {"time": round(lt, 3), "text": full.strip()}
            if words:
                entry["words"] = words
                lw = words[-1]
                entry["duration"] = round(
                    lw["offset"] + lw["duration"], 3)
            res.append(entry)
        res.sort(key=lambda x: x["time"])
        for i in range(len(res)):
            if "duration" not in res[i]:
                if i < len(res) - 1:
                    res[i]["duration"] = round(
                        res[i + 1]["time"] - res[i]["time"], 3)
                else:
                    res[i]["duration"] = 5.0
        return res

    # ★★★ 重写: 解析 [awlrc:] base64 嵌入标签 ★★★
    @staticmethod
    def parse_awlrc_tag(lrc_text):
        """
        解析 [awlrc:lrc:<b64>,tlrc:<b64>,rlrc:<b64>,awlrc:<b64>] 标签。

        关键修复:
        1. 正则用 [^\\]]+ 匹配到标签结束的 ]（base64不含]字符）
        2. 按逗号分割键值对时考虑base64内无逗号
        3. 清理空白后再解码
        """
        # ★ FIX: 用 [^\]]+ 替代 .*? —— base64 字符集不含 ]，所以安全
        m = re.search(r'\[awlrc:([^\]]+)\]', lrc_text)
        if not m:
            print("[LrcParser] 未找到 [awlrc:] 标签")
            return None

        content = m.group(1)
        print("[LrcParser] awlrc 标签内容长度: {} chars".format(len(content)))

        # ★ FIX: 用正则提取每个 key:value 对
        # key 是 lrc/tlrc/rlrc/awlrc，value 是连续的 base64 字符
        # base64 字符集: A-Z a-z 0-9 + / = 以及可能的空白
        kv_pattern = re.compile(
            r'(?:^|,)\s*(lrc|tlrc|rlrc|awlrc)\s*:\s*'
            r'([A-Za-z0-9+/=\s]+)'
        )

        decoded = {}
        for km in kv_pattern.finditer(content):
            key = km.group(1)
            b64data = km.group(2).strip()
            if not b64data:
                continue
            try:
                # ★ FIX: 清理所有空白字符后再解码
                b64clean = re.sub(r'\s+', '', b64data)
                # ★ FIX: 补齐 padding
                padding = 4 - (len(b64clean) % 4)
                if padding != 4:
                    b64clean += '=' * padding
                text = base64.b64decode(b64clean).decode('utf-8')
                decoded[key] = text
                # 打印前100字符用于调试
                preview = text[:100].replace('\n', '\\n')
                print("[LrcParser] awlrc key={}: {} chars, preview: {}".format(
                    key, len(text), preview))
            except Exception as e:
                print("[LrcParser] awlrc decode err {}: {}".format(key, e))
                # ★ 尝试去掉末尾可能多余的字符再试
                try:
                    for trim in range(1, 4):
                        try:
                            trimmed = b64clean[:-trim]
                            pad2 = 4 - (len(trimmed) % 4)
                            if pad2 != 4:
                                trimmed += '=' * pad2
                            text = base64.b64decode(trimmed).decode('utf-8')
                            decoded[key] = text
                            print("[LrcParser] awlrc key={}: 修剪{}字符后解码成功, {} chars".format(
                                key, trim, len(text)))
                            break
                        except Exception:
                            continue
                except Exception:
                    pass

        if decoded:
            print("[LrcParser] awlrc 解析成功, keys: {}".format(list(decoded.keys())))
        else:
            print("[LrcParser] awlrc 解析失败，未解码出任何内容")

        return decoded if decoded else None


# ============================================================
# 队列管理器
# ============================================================
class QueueManager:

    def __init__(self):
        self._version = 0             # type: int  # 队列版本号，每次变更递增
        self._queue = []               # type: List[SongInfo]
        self._lock = threading.RLock()
        self._current = None           # type: Optional[SongInfo]
        self._history = []             # type: List[SongInfo]
        self._stop = threading.Event()
        self._thread = None

    def _bump_version(self):
        """队列变更时递增版本号（调用时已在锁内）"""
        self._version += 1

    def _set_song_state(self, song, new_state, error_msg=""):
        """统一状态变更入口——自动bump版本号通知前端"""
        with self._lock:
            old = song.state
            song.state = new_state
            if error_msg:
                song.error_msg = error_msg
            if old != new_state:
                self._bump_version()


    def get_version(self):
        """获取当前版本号（线程安全）"""
        with self._lock:
            return self._version


    def add(self, song):
        with self._lock:
            if len(self._queue) >= Config.MAX_QUEUE_SIZE:
                return False, "队列已满"
            for s in self._queue:
                if (s.name == song.name
                        and s.singer == song.singer
                        and s.album == song.album
                        and s.interval == song.interval):
                    return False, "「{}」已在队列中".format(song.name)
            if (self._current
                    and self._current.name == song.name
                    and self._current.singer == song.singer
                    and self._current.album == song.album
                    and self._current.interval == song.interval):
                return False, "「{}」正在播放".format(song.name)
            self._queue.append(song)
            self._bump_version()  # ★★★ 插在这里 ★★★
            pos = len(self._queue)
            print("[+Queue] #{} {} - {} [{}] ({})".format(
                pos, song.name, song.singer, song.album, song.source))
            return True, "已加入第{}位".format(pos)



    def move_top(self, uid):
        with self._lock:
            for i, s in enumerate(self._queue):
                if s.uid == uid:
                    if i == 0:
                        return True, "已在最前"
                    if s.state in (SongState.DOWNLOADING,
                                   SongState.SEPARATING):
                        return False, "处理中，无法移动"
                    self._queue.insert(0, self._queue.pop(i))
                    self._bump_version()  # ★★★ 插在这里 ★★★
                    return True, "已置顶「{}」".format(s.name)
            return False, "未找到"


    def skip(self):
        with self._lock:
            if self._current:
                name = self._current.name
                self._current.state = SongState.PLAYED
                self._history.append(self._current)
                self._current = None
                self._bump_version()
                result = (True, "已切歌「{}」".format(name))
            else:
                result = (False, "没有正在播放的歌曲")

        # ★ FIX: 切歌后立即检查待下载歌曲并尝试推进
        if result[0]:
            self._kick_worker()
        return result


    def finish_current(self):
        with self._lock:
            if self._current:
                self._current.state = SongState.PLAYED
                self._history.append(self._current)
                self._current = None
                self._bump_version()

        # ★ FIX: 播完后也立即检查
        self._kick_worker()

    def _kick_worker(self):
        """切歌/播完后立即检查队列中待下载的歌曲，不等worker循环"""
        try:
            self._check_pending_downloads()
            target = self._next_to_prepare()
            if target and target.state in (SongState.QUEUED,SongState.WAITING_DOWNLOAD):
                # 本地/缓存命中可以同步处理（很快）
                local = self._find_local(target)
                if local:
                    target.file_path = str(local)
                    self._set_song_state(target, SongState.DOWNLOADED)
                    self._grab_lyric(target)
                else:
                    cached = self._find_cache(target)
                    if cached:
                        target.vocals_path = cached[0]
                        target.accompaniment_path = cached[1]
                        target.ready_at = time.time()
                        self._set_song_state(target, SongState.READY)
                        self._grab_lyric(target)
        except Exception:
            traceback.print_exc()


    def pop_next_ready(self):
        with self._lock:
            for i, s in enumerate(self._queue):
                if s.state == SongState.READY:
                    self._current = self._queue.pop(i)
                    self._current.state = SongState.PLAYING
                    self._bump_version()  # ★★★ 插在这里 ★★★
                    print("[Play] {} - {}".format(
                        self._current.name, self._current.singer))
                    return self._current
            return None


    def get_current(self):
        with self._lock:
            return self._current

    def get_list(self):
        with self._lock:
            out = []
            if self._current:
                d = self._current.to_dict()
                d["is_current"] = True
                out.append(d)
            for s in self._queue:
                d = s.to_dict()
                d["is_current"] = False
                out.append(d)
            return out

    def find(self, uid):
        with self._lock:
            if self._current and self._current.uid == uid:
                return self._current
            for s in self._queue:
                if s.uid == uid:
                    return s
            for s in self._history:
                if s.uid == uid:
                    return s
            return None

    # ---- 后台工作线程 ----
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="QueueWorker")
        self._thread.start()
        print("[Worker] 启动")

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                if tv.is_finished():
                    self.finish_current()
                self._check_pending_downloads()
                target = self._next_to_prepare()
                if target:
                    self._prepare(target)
                time.sleep(2)
            except Exception:
                traceback.print_exc()
                time.sleep(3)


    def _next_to_prepare(self):
        with self._lock:
            for s in self._queue[:3]:
                if s.state == SongState.DOWNLOADED:
                    return s
            for s in self._queue[:2]:
                if s.state in (SongState.QUEUED,
                               SongState.WAITING_DOWNLOAD):
                    return s
            return None

    # ★ FIX: _prepare 缓存命中时从分离目录推断 file_path
    def _prepare(self, song):
        if song.state in (SongState.QUEUED, SongState.WAITING_DOWNLOAD):
            local = self._find_local(song)
            if local:
                song.file_path = str(local)
                self._set_song_state(song, SongState.DOWNLOADED)  # ★
                print("[Prep] 本地命中: {}".format(local.name))
                self._grab_lyric(song)
                return
            cached = self._find_cache(song)
            if cached:
                song.vocals_path = cached[0]
                song.accompaniment_path = cached[1]
                sep_stem = Path(cached[0]).parent.name
                for ext in AUDIO_EXTS:
                    candidate = Path(Config.MUSIC_DIR) / (sep_stem + ext)
                    if candidate.exists():
                        song.file_path = str(candidate)
                        print("[Prep] 缓存→file_path: {}".format(candidate.name))
                        break
                song.ready_at = time.time()
                self._set_song_state(song, SongState.READY)  # ★
                print("[Prep] 缓存命中: {} (file_path={})".format(
                    song.name, song.file_path or "未找到"))
                self._grab_lyric(song)
                return

            if song.source == "local" and not song.file_path:
                self._set_song_state(song, SongState.ERROR, "本地文件未找到")  # ★
                return
            ok, msg = dl_guard.can_download()
            if not ok:
                self._set_song_state(song, SongState.WAITING_DOWNLOAD)  # ★
                return
            self._download_semi_auto(song)
        elif song.state == SongState.DOWNLOADED:
            self._separate(song)



    def _find_local(self, song):
        # type: (SongInfo) -> Optional[Path]
        d = Path(Config.MUSIC_DIR)
        if not d.exists():
            return None

        # ★ 如果 file_path 已指定且文件存在，直接返回
        if song.file_path:
            fp = Path(song.file_path)
            if fp.exists():
                return fp

        name_l = song.name.lower().strip()
        singer_l = song.singer.lower().strip() if song.singer else ""
        album_l = song.album.lower().strip() if song.album else ""

        # Phase 1: 精确匹配（歌名 - 歌手 或 歌手 - 歌名）
        exact_matches = []
        for f in d.iterdir():
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
                continue
            stem = f.stem.lower().strip()
            stem_clean = re.sub(r'\s*[（(][^）)]*[）)]\s*$', '', stem).strip()
            for fmt in ["{} - {}", "{}-{}"]:
                for a, b in [(name_l, singer_l), (singer_l, name_l)]:
                    if not b:
                        continue
                    target = fmt.format(a, b).strip()
                    if stem_clean == target or stem == target:
                        exact_matches.append(f)

        #★★★ 核心变化：精确匹配到多个文件 = 歧义，不自动选★★★
        if len(exact_matches) == 1:
            return exact_matches[0]
        elif len(exact_matches) > 1:
            # 多个精确匹配（同名同歌手不同版本）
            # 如果有专辑信息，尝试用专辑名筛选
            if album_l:
                album_filtered = [f for f in exact_matches if album_l in f.stem.lower()]
                if len(album_filtered) == 1:
                    return album_filtered[0]
            # ★ 在线点歌指定了专辑/时长，但本地有多个版本 → 不匹配，走下载
            if song.source == "online" and (album_l or song.interval):
                print("[FindLocal] 多版本歧义({}个), 在线点歌需精确版本, 跳过本地".format(
                    len(exact_matches)))
                return None#本地点歌无专辑信息，返回第一个
            return exact_matches[0]

        # Phase 2: 模糊匹配 + singer强制校验 + 歧义检测
        candidates = []
        for f in d.iterdir():
            if not f.is_file() or f.suffix.lower() not in AUDIO_EXTS:
                continue
            stem = f.stem.lower().strip()
            score = 0
            if name_l and name_l in stem:
                score += 10
            if singer_l and singer_l in stem:
                score += 5
            if album_l and album_l in stem:
                score += 3
            if score >= 10:
                candidates.append((score, f))

        if not candidates:
            return None

        candidates.sort(key=lambda x: -x[0])

        # ★ singer不为空时，候选文件必须包含singer
        if singer_l:
            singer_matched = [(sc, f) for sc, f in candidates if singer_l in f.stem.lower()]
            if singer_matched:
                candidates = singer_matched
            else:
                print("[FindLocal] 歌名命中但singer不匹配: 搜索singer='{}', 候选={}".format(
                    song.singer, [f.name for _, f in candidates[:3]]))
                return None

        #★★★ 核心变化：在线点歌有专辑信息时，模糊匹配也要检查专辑 ★★★
        if album_l and song.source == "online":
            album_matched = [(sc, f) for sc, f in candidates if album_l in f.stem.lower()]
            if album_matched:
                candidates = album_matched
            else:
                # 在线点歌指定了专辑，但本地文件名不含该专辑 → 跳过
                print("[FindLocal] 在线点歌专辑'{}' 本地无匹配, 走下载流程".format(song.album))
                return None

        if len(candidates) == 1:
            return candidates[0][1]

        if candidates[0][0] > candidates[1][0]:
            return candidates[0][1]

        #★ 多个同分候选 = 歧义，在线点歌不猜
        if song.source == "online":
            print("[FindLocal] 歧义且为在线点歌, 走下载: {}个候选".format(
                len([c for c in candidates if c[0] == candidates[0][0]])))
            return None

        return candidates[0][1]




    def _find_cache(self, song):
        sep = Path(Config.SEPARATED_DIR)
        if not sep.exists():
            return None

        # ★ 优先使用 file_path 推断
        if song.file_path:
            stem = Path(song.file_path).stem
            v = sep / stem / "vocals.wav"
            a = sep / stem / "no_vocals.wav"
            if v.exists() and a.exists():
                return (str(v), str(a))

        # ★ 精确匹配，必须同时含name 和 singer
        if song.singer:
            candidates = [
                "{} - {}".format(song.singer, song.name),
                "{} - {}".format(song.name, song.singer),
            ]
            matched = []
            for stem in candidates:
                v = sep / stem / "vocals.wav"
                a = sep / stem / "no_vocals.wav"
                if v.exists() and a.exists():
                    matched.append((stem, str(v), str(a)))

            if len(matched) == 1:
                return (matched[0][1], matched[0][2])
            elif len(matched) > 1 and song.source == "online":
                #★ 多个缓存版本 + 在线点歌 → 不猜，走重新分离
                print("[FindCache] 多版本缓存歧义, 在线点歌跳过缓存")
                return None
            elif matched:
                return (matched[0][1], matched[0][2])

        # ★ 无singer时才允许纯歌名匹配
        if not song.singer:
            v = sep / song.name / "vocals.wav"
            a = sep / song.name / "no_vocals.wav"
            if v.exists() and a.exists():
                return (str(v), str(a))

        return None





    def _download_semi_auto(self, song):
        self._set_song_state(song, SongState.DOWNLOADING)
        dl_guard.start()
        print("[DL] 半自动触发: {} - {} (songmid={}, source={})".format(
            song.name, song.singer, song.songmid, song.search_source))

        # ★ 有songmid时用精确播放，否则回退模糊搜索
        if song.songmid and song.search_source:
            ok = lx.scheme_play_song(
                name=song.name,
                singer=song.singer,
                source=song.search_source,
                songmid=song.songmid,
                album_name=song.album,
                interval=song.interval,
                str_media_mid=song.str_media_mid,
                album_mid=song.album_mid,
            )
        else:
            ok = lx.scheme_search_play(
                name=song.name,
                singer=song.singer,
                album_name=song.album,
                interval=song.interval,
                play_later=False,
            )

        if not ok:
            print("[DL] scheme调用失败")
            self._set_song_state(song, SongState.ERROR, "无法调用音乐软件（请确认已打开）")
            dl_guard.finish(False)
            return

        played = lx.wait_until_playing(song_name=song.name, max_wait=15)
        if not played:
            print("[DL] ⚠️ 未检测到播放，仍尝试暂停+收藏")

        # ★ 精确播放模式下跳过验证（ID已精确）
        verified = True
        if not song.songmid and played:
            is_correct, _ = lx.verify_playing_song(song.name, song.singer)
            if is_correct:
                verified = True
            else:
                verified = False
                for retry in range(3):
                    print("[DL] 版本不对，skip (retry {}/3)".format(retry + 1))
                    lx.skip_next()
                    time.sleep(2.5)
                    lx.wait_until_playing(song_name=song.name, max_wait=8)
                    is_correct, _ = lx.verify_playing_song(song.name, song.singer)
                    if is_correct:
                        verified = True
                        break
                if not verified:
                    print("[DL] ⚠️ 3次skip后仍未找到正确版本")

        paused = lx.pause_with_retry(max_retries=5, interval=0.5)
        if not paused:
            print("[DL] ⚠️ 暂停确认失败，强制继续")

        time.sleep(0.3)
        if lx.collect():
            song._collected = True
            print("[DL] ✅ 已收藏: {}{}".format(
                song.name, " (⚠️版本可能不对)" if not verified else ""))
        else:
            print("[DL] ⚠️ 收藏失败: {}".format(song.name))

        self._grab_lyric(song)

        dl_guard.finish(True)
        song._download_triggered_at = time.time()
        self._set_song_state(song, SongState.NEEDS_DOWNLOAD)
        print("[DL] 等待手动下载: {} - {}".format(song.name, song.singer))






    # ★★★ FIX 1: 放宽歌名匹配——只要lx在播放就算命中 ★★★
    def _wait_lx_playing(self, song, max_wait=8):
        """等待lx-music开始播放，放宽匹配条件"""
        waited = 0.0
        name_l = song.name.lower()
        singer_l = (song.singer or "").lower()

        while waited < max_wait and not self._stop.is_set():
            time.sleep(0.4)
            waited += 0.4
            try:
                status = lx.get_status("status,name,singer")
                if not status:
                    continue
                lx_status = status.get("status", "")
                lx_name = (status.get("name") or "").lower()
                lx_singer = (status.get("singer") or "").lower()

                if lx_status != "playing":
                    continue

                # ★ FIX: 放宽匹配——满足以下任一条件即可：
                # 1. 歌名互相包含
                # 2. 歌手匹配 + 歌名部分匹配（至少2个字）
                # 3. 歌名完全一致
                name_match = (name_l in lx_name or lx_name in name_l)
                singer_match = singer_l and (singer_l in lx_singer or lx_singer in singer_l)

                #部分歌名匹配（取歌名前N个字符）
                partial_name = name_l[:max(2, len(name_l) // 2)]
                partial_match = partial_name in lx_name

                if name_match or (singer_match and partial_match):
                    print("[DL] lx-music确认播放: '{}' by '{}' (等待{:.1f}s)".format(
                        status.get("name"), status.get("singer"), waited))
                    return True# ★ FIX: 如果lx在播放但歌名完全不匹配，也记录日志
                if lx_status == "playing" and waited > 3:
                    print("[DL] lx在播放但歌名不匹配: lx='{}' vs target='{}'".format(
                        lx_name, name_l))

            except Exception:
                pass

        print("[DL] 等待播放超时 ({:.1f}s)".format(waited))
        return False

    # ★★★ FIX: remove时检查收藏并取消 ★★★
    def remove(self, uid):
        with self._lock:
            for i, s in enumerate(self._queue):
                if s.uid == uid:
                    if s.state in (SongState.DOWNLOADING, SongState.SEPARATING):
                        return False, "处理中，无法移除"
                    name = s.name
                    was_collected = s._collected
                    self._queue.pop(i)
                    self._bump_version()

                    #★ FIX: 已收藏的歌被删除时，异步取消收藏
                    if was_collected:
                        print("[Queue] 移除已收藏歌曲「{}」，触发取消收藏".format(name))
                        threading.Thread(
                            target=self._try_uncollect, args=(s,),
                            daemon=True).start()

                    return True, "已移除「{}」".format(name)
            return False, "未找到"

    # ★★★ FIX: 增强取消收藏——先API检查，再scheme导航 ★★★
    def _try_uncollect(self, song):
        """取消收藏——先检查当前播放是否匹配，不匹配则scheme导航"""
        try:
            time.sleep(1)

            # 方案A：当前lx播放/暂停的就是这首歌，直接uncollect
            status = lx.get_status("name,singer,status")
            if status:
                lx_name = (status.get("name") or "").lower()
                song_name = song.name.lower()
                if song_name in lx_name or lx_name in song_name:
                    lx.uncollect()
                    song._collected = False
                    print("[DL] 直接取消收藏成功: {}".format(song.name))
                    return

            # 方案B：用scheme导航到这首歌
            print("[DL] 当前播放不匹配，用scheme导航取消收藏: {}".format(song.name))
            lx.scheme_search_play(
                name=song.name, singer=song.singer, play_later=False)

            # 用API等待播放开始
            lx.wait_until_playing(song_name=song.name, max_wait=10)

            # 暂停
            lx.pause_with_retry(max_retries=3, interval=0.3)
            time.sleep(0.3)

            # 取消收藏
            lx.uncollect()
            song._collected = False
            print("[DL] scheme导航+取消收藏成功: {}".format(song.name))

        except Exception as e:
            print("[DL] 取消收藏失败(非致命): {}".format(e))

    # ★★★ FIX: _check_pending_downloads 也用增强版uncollect ★★★
    def _check_pending_downloads(self):
        with self._lock:
            for s in list(self._queue):
                if s.state != SongState.NEEDS_DOWNLOAD:
                    continue
                found = self._find_local(s)
                if found:
                    s.file_path = str(found)
                    self._set_song_state(s, SongState.DOWNLOADED)  # ★
                    print("[DL] 检测到已下载: {}".format(found.name))
                    if s._collected:
                        threading.Thread(
                            target=self._try_uncollect, args=(s,),
                            daemon=True).start()
                    self._grab_lyric(s)
                else:
                    triggered_at = s._download_triggered_at
                    if triggered_at > 0 and time.time() - triggered_at > 1800:
                        self._set_song_state(s, SongState.ERROR, "等待下载超时(30分钟)")  # ★




    # ★★★ FIX: 增强歌词获取——日志 + 多编码 + 更宽松的匹配 ★★★
    def _grab_lyric(self, song):
        """健壮歌词获取：本地.lrc文件优先 → lx-music API兜底"""
        print("[Lyric] === 获取歌词: {} - {} ===".format(song.name, song.singer))
        print("[Lyric]   file_path = {}".format(song.file_path))

        # ===== 步骤1: 本地 .lrc 文件 =====
        lrc_content = None
        lrc_source = None

        # 1a. file_path 同名 .lrc
        if song.file_path:
            lrc_path = Path(song.file_path).with_suffix(".lrc")
            print("[Lyric]   1a: checking {}".format(lrc_path))
            if lrc_path.exists():
                lrc_content = self._read_lrc_file(lrc_path)
                if lrc_content:
                    lrc_source = str(lrc_path)
                    print("[Lyric]   1a: OK {} chars".format(len(lrc_content)))

        # 1b. 模糊搜索音乐目录中的 .lrc —— ★ FIX: 要求歌手也匹配
        if not lrc_content and song.name:
            music_dir = Path(Config.MUSIC_DIR)
            if music_dir.exists():
                name_l = song.name.lower().strip()
                singer_l = (song.singer or "").lower().strip()
                # ★ 拆分多歌手用于匹配
                singer_parts = []
                if singer_l:
                    singer_parts = [s.strip() for s in re.split(r'[,，、/]', singer_l) if s.strip()]

                best_lrc = None
                best_score = 0
                for f in music_dir.iterdir():
                    if f.suffix.lower() != '.lrc':
                        continue
                    stem_l = f.stem.lower().strip()
                    score = 0
                    if name_l in stem_l:
                        score += 10
                    if singer_parts:
                        # ★ 任一歌手名出现在文件名中 +5
                        for sp in singer_parts:
                            if sp in stem_l:
                                score += 5
                                break

                    # ★★★ FIX: 排除明显的版本误匹配 ★★★
                    instrumental_tags = ["伴奏", "inst", "instrumental", "off vocal"]
                    stem_is_inst = any(tag in stem_l for tag in instrumental_tags)
                    name_is_inst = any(tag in name_l for tag in instrumental_tags)
                    if stem_is_inst and not name_is_inst:
                        score -= 8
                    if not stem_is_inst and name_is_inst:
                        score -= 8

                    if score > best_score:
                        best_score = score
                        best_lrc = f

                # ★★★ FIX: 有歌手信息时要求 score >= 10 ★★★
                min_score = 10 if singer_parts else 5

                if best_lrc and best_score >= min_score:
                    lrc_content = self._read_lrc_file(best_lrc)
                    if lrc_content:
                        lrc_source = str(best_lrc)
                        print("[Lyric]   1b: OK {} (score={}, {} chars)".format(
                            best_lrc.name, best_score, len(lrc_content)))
                else:
                    print("[Lyric]   1b: 未找到匹配的lrc (best_score={}, min={}, best={})".format(
                        best_score, min_score,
                        best_lrc.name if best_lrc else "None"))




        # 1c. 处理本地歌词
        if lrc_content and lrc_content.strip():
            self._process_lrc_content(song, lrc_content, lrc_source)
            return

        # ===== 步骤2: lx-music API =====
        print("[Lyric]   本地未找到，尝试 lx-music API...")
        try:
            data = lx.get_lyric_all()
            if data and data.get("lyric"):
                song.lrc = data["lyric"]
                song.tlyric = data.get("tlyric", "")
                song.rlyric = data.get("rlyric", "")
                song.lxlyric = data.get("lxlyric", "")
                print("[Lyric]   API获取成功: lrc={}, lxlyric={}".format(
                    len(song.lrc), len(song.lxlyric or "")))
                return
        except Exception as e:
            print("[Lyric]   API异常: {}".format(e))

        print("[Lyric]   ❌ 最终未获取到歌词: {} - {}".format(
            song.name, song.singer))

    @staticmethod
    def _read_lrc_file(path):
        """尝试多种编码读取 lrc 文件"""
        for enc in ["utf-8", "utf-8-sig", "gbk", "gb2312", "big5", "latin-1"]:
            try:
                return path.read_text(encoding=enc)
            except (UnicodeDecodeError, UnicodeError):
                continue
        return None

    def _process_lrc_content(self, song, lrc_content, lrc_source):
        """处理读取到的 LRC 内容：检查 awlrc 标签 → 普通 LRC"""
        # ★ 优先检查 [awlrc:] 标签
        awlrc_data = LrcParser.parse_awlrc_tag(lrc_content)
        if awlrc_data:
            # awlrc 标签中的 lrc 是纯行级歌词
            song.lrc = awlrc_data.get('lrc', '')
            # awlrc 标签中的 awlrc 是逐字歌词（带 <offset,dur> 标记）
            song.lxlyric = awlrc_data.get('awlrc', '')
            song.tlyric = awlrc_data.get('tlrc', '')
            song.rlyric = awlrc_data.get('rlrc', '')

            print("[Lyric]   awlrc标签解析: lrc={}, awlrc={}, tlrc={}, rlrc={}".format(
                len(song.lrc or ""), len(song.lxlyric or ""),
                len(song.tlyric or ""), len(song.rlyric or "")))

            # ★ 验证解码后的逐字歌词确实包含 <> 标记
            if song.lxlyric and '<' in song.lxlyric and '>' in song.lxlyric:
                print("[Lyric]   ✅ 逐字歌词已就绪 (awlrc)")
            elif song.lxlyric:
                print("[Lyric]   ⚠️ awlrc 解码成功但不含逐字标记，当作普通 lrc")
                if not song.lrc:
                    song.lrc = song.lxlyric
                song.lxlyric = ""

            if song.lrc or song.lxlyric:
                print("[Lyric]   ✅ 来源: {}".format(lrc_source))
                return

        # ★ 无 awlrc 标签，看正文本身是否含逐字
        if '<' in lrc_content and '>' in lrc_content:
            # 检查是否真的是逐字标记（不是 HTML 之类的）
            if re.search(r'<\d+,\d+>', lrc_content):
                song.lxlyric = lrc_content
                song.lrc = lrc_content  # 备用
                print("[Lyric]   ✅ 本地LRC(含逐字): {}".format(lrc_source))
                return

        # ★ 纯行级 LRC
        song.lrc = lrc_content
        print("[Lyric]   ✅ 本地LRC(纯行): {}".format(lrc_source))


    def _separate(self, song):
        if not song.file_path or not Path(song.file_path).exists():
            self._set_song_state(song, SongState.ERROR, "音频文件不存在")  # ★
            return
        audio = Path(song.file_path)
        out_dir = Path(Config.SEPARATED_DIR)
        stem = audio.stem
        v = out_dir / stem / "vocals.wav"
        a = out_dir / stem / "no_vocals.wav"
        if v.exists() and a.exists():
            song.vocals_path = str(v)
            song.accompaniment_path = str(a)
            song.ready_at = time.time()
            self._set_song_state(song, SongState.READY)  # ★
            print("[Sep] 缓存: {}".format(stem))
            return
        self._set_song_state(song, SongState.SEPARATING)  # ★
        print("[Sep] 开始: {}".format(audio.name))
        result = run_demucs(audio, out_dir)
        if result["success"]:
            song.vocals_path = result["vocals_path"]
            song.accompaniment_path = result["accompaniment_path"]
            song.ready_at = time.time()
            self._set_song_state(song, SongState.READY)  # ★
            print("[Sep] 完成: {}".format(stem))
        else:
            self._set_song_state(song, SongState.ERROR, result["message"][:200])  # ★
            print("[Sep] 失败: {}".format(song.error_msg[:80]))



# ============================================================
# Demucs 分离
# ============================================================
DEMUCS_SCRIPT = '''
import sys, os, torch, torchaudio
try:
    from demucs.pretrained import get_model
    from demucs.apply import apply_model
except ImportError:
    print("ERROR: demucs unavailable"); sys.exit(1)

model = get_model("htdemucs")
model.eval()
dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("Device:", dev, flush=True)
model.to(dev)

wav, sr = torchaudio.load(sys.argv[1])
msr = model.samplerate
if sr != msr:
    wav = torchaudio.functional.resample(wav, sr, msr)
if wav.shape[0] == 1: wav = wav.repeat(2, 1)
elif wav.shape[0] > 2: wav = wav[:2]

ref = wav.mean(0)
wm, ws = ref.mean(), ref.std()
wn = (wav - wm) / (ws + 1e-8)
wi = wn.unsqueeze(0).to(dev)
print("Separating...", flush=True)
with torch.no_grad():
    src = apply_model(model, wi, device=dev)
src = src * (ws + 1e-8) + wm
src = src.cpu()

stem = os.path.splitext(os.path.basename(sys.argv[1]))[0]
out = os.path.join(sys.argv[2], stem)
os.makedirs(out, exist_ok=True)
nv = []
for i, nm in enumerate(model.sources):
    t = src[0, i].clamp(-1, 1)
    torchaudio.save(os.path.join(out, nm + ".wav"), t, msr)
    if nm != "vocals": nv.append(t)
if nv:
    torchaudio.save(os.path.join(out, "no_vocals.wav"),
                    sum(nv).clamp(-1, 1), msr)
print("DONE", flush=True)
'''


def run_demucs(audio_path, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    script = output_dir / "_demucs_run.py"
    script.write_text(DEMUCS_SCRIPT, encoding="utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, str(script),
             str(audio_path), str(output_dir)],
            capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            return {"success": False,
                    "message": (proc.stderr or proc.stdout)[:500]}
        stem = audio_path.stem
        return {
            "success": True,
            "vocals_path": str(output_dir / stem / "vocals.wav"),
            "accompaniment_path": str(output_dir / stem / "no_vocals.wav"),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "message": "超时(>10min)"}
    except Exception as e:
        return {"success": False, "message": str(e)}
    finally:
        try:
            script.unlink()
        except Exception:
            pass


queue = QueueManager()


# ============================================================
# Flask
# ============================================================
app = Flask(__name__)
CORS(app)


@app.route("/api/health")
def api_health():
    lx_ok = lx.is_connected()
    d = Path(Config.MUSIC_DIR)
    lc = 0
    if d.exists():
        lc = len([f for f in d.iterdir()
                  if f.is_file() and f.suffix.lower() in AUDIO_EXTS])
    sd = Path(Config.SEPARATED_DIR)
    sc = len([x for x in sd.iterdir() if x.is_dir()]) if sd.exists() else 0
    return json_resp({
        "server": "online", "lx_music": lx_ok,
        "local_songs": lc, "cached": sc,
        "tv": tv.to_dict(),
        "queue_len": len(queue.get_list()),
        "guard": dl_guard.status(),
    })


@app.route("/api/config", methods=["GET"])
def api_config_get():
    return json_resp({
        "player_host": Config.PLAYER_HOST,
        "player_port": Config.PLAYER_PORT,
        "music_dir": Config.MUSIC_DIR,
        "serve_port": Config.SERVE_PORT,
        "lan_ip": Config.LAN_IP,
        "dl_interval": Config.DOWNLOAD_MIN_INTERVAL,
        "scheme_prefix": Config.SCHEME_PREFIX,
        "lx_download_dir": Config.LX_DOWNLOAD_DIR,
    })


@app.route("/api/config", methods=["POST"])
def api_config_set():
    d = flask_request.get_json(force=True)
    changed = []
    if "player_host" in d:
        Config.PLAYER_HOST = d["player_host"]; changed.append("player_host")
    if "player_port" in d:
        Config.PLAYER_PORT = int(d["player_port"]); changed.append("player_port")
    if "music_dir" in d:
        Config.MUSIC_DIR = d["music_dir"]
        Path(Config.MUSIC_DIR).mkdir(parents=True, exist_ok=True)
        changed.append("music_dir")
    if "lan_ip" in d:
        Config.LAN_IP = d["lan_ip"]; changed.append("lan_ip")
    if "dl_interval" in d:
        Config.DOWNLOAD_MIN_INTERVAL = int(d["dl_interval"]); changed.append("dl_interval")
    if "scheme_prefix" in d:
        Config.SCHEME_PREFIX = d["scheme_prefix"]; changed.append("scheme_prefix")
    if "lx_download_dir" in d:
        Config.LX_DOWNLOAD_DIR = d["lx_download_dir"]; changed.append("lx_download_dir")
    lx.update_endpoint(Config.PLAYER_HOST, Config.PLAYER_PORT)
    return json_resp({"ok": True, "changed": changed})


# ★ 搜索恢复：本地+在线
@app.route("/api/search")
def api_search():
    kw = flask_request.args.get("q", "").strip()
    if not kw:
        return json_resp({"local": [], "online": [], "keyword": ""})
    print("[Search] '{}'".format(kw))
    result = MusicSearcher.search(kw)
    return json_resp(result)

@app.route("/api/queue/remove/<uid>")
def api_queue_remove(uid):
    ok, msg = queue.remove(uid)
    return json_resp({"ok": ok, "msg": msg})


@app.route("/api/queue/top/<uid>")
def api_queue_top(uid):
    ok, msg = queue.move_top(uid)
    return json_resp({"ok": ok, "msg": msg})


@app.route("/api/queue/skip")
def api_queue_skip():
    ok, msg = queue.skip()
    if ok:
        tv._skip_counter += 1   # ★ FIX: 递增
    return json_resp({"ok": ok, "msg": msg})

#重唱api
@app.route("/api/queue/replay")
def api_queue_replay():
    current = queue.get_current()
    if current:
        tv._replay_counter += 1  # ★ FIX: 递增
        return json_resp({"ok": True,
                          "msg": "重唱「{}」".format(current.name)})
    return json_resp({"ok": False, "msg": "没有正在播放的歌曲"})



@app.route("/api/queue/status")
def api_queue_status():
    return json_resp({
        "queue": queue.get_list(),
        "guard": dl_guard.status(),
        "current_mode": tv.mode,
    })


# ========== TV端接口 ==========

@app.route("/api/tv/next", methods=["POST", "GET"])
def api_tv_next():
    song = queue.pop_next_ready()
    if song:
        base = "http://{}:{}".format(Config.LAN_IP, Config.SERVE_PORT)
        return json_resp({
            "ok": True, "song": song.to_dict(),
            "urls": {
                "accompaniment": "{}/api/audio/accompaniment/{}".format(base, song.uid),
                "vocals": "{}/api/audio/vocals/{}".format(base, song.uid),
                "original": "{}/api/audio/original/{}".format(base, song.uid),
                "lyric": "{}/api/lyric/{}".format(base, song.uid),
            },
        })
    current = queue.get_current()
    if current:
        base = "http://{}:{}".format(Config.LAN_IP, Config.SERVE_PORT)
        return json_resp({
            "ok": True, "song": current.to_dict(),
            "urls": {
                "accompaniment": "{}/api/audio/accompaniment/{}".format(base, current.uid),
                "vocals": "{}/api/audio/vocals/{}".format(base, current.uid),
                "original": "{}/api/audio/original/{}".format(base, current.uid),
                "lyric": "{}/api/lyric/{}".format(base, current.uid),
            },
        })
    return json_resp({"ok": False, "msg": "没有就绪的歌曲"})


@app.route("/api/tv/finished", methods=["POST"])
def api_tv_finished():
    queue.finish_current()
    return api_tv_next()


# ★ FIX: 心跳响应加 mode_changed_at
@app.route("/api/tv/heartbeat", methods=["POST"])
def api_tv_heartbeat():
    d = flask_request.get_json(force=True)
    tv.update(d)

    next_song_info = None
    with queue._lock:
        for s in queue._queue:
            next_song_info = {
                "name": s.name, "singer": s.singer,
                "state": s.state.value,
            }
            break

    current = queue.get_current()
    current_state = current.state.value if current else None

    return json_resp({
        "ok": True,
        "queue_len": len(queue.get_list()),
        # ★ FIX: 返回计数器，不再返回布尔
        "skip_counter": tv._skip_counter,
        "replay_counter": tv._replay_counter,
        "mode": tv.mode,
        "mode_changed_at": tv.mode_changed_at,
        "mic_volume": tv.mic_volume,
        "music_volume": tv.music_volume,
        "next_song": next_song_info,
        "current_state": current_state,
    })


# ★ FIX: TV专用切歌端点——不设skip标记，避免双跳
@app.route("/api/tv/skip")
def api_tv_skip():
    """TV端直接切歌，不设tv._skip_requested（避免心跳双跳）"""
    ok, msg = queue.skip()
    return json_resp({"ok": ok, "msg": msg})


@app.route("/api/tv/state")
def api_tv_state():
    return json_resp(tv.to_dict())


@app.route("/api/tv/volume", methods=["POST"])
def api_tv_volume():
    d = flask_request.get_json(force=True)
    if "mic_volume" in d:
        tv.mic_volume = int(d["mic_volume"])
    if "music_volume" in d:
        tv.music_volume = int(d["music_volume"])
    return json_resp({"ok": True})


# ★ FIX: 使用统一的 set_mode
@app.route("/api/tv/mode", methods=["POST"])
def api_tv_mode():
    d = flask_request.get_json(force=True)
    mode = d.get("mode", "accompaniment")
    tv.set_mode(mode)
    return json_resp({"ok": True, "mode": tv.mode,
                      "mode_changed_at": tv.mode_changed_at})


@app.route("/api/tv/mode", methods=["GET"])
def api_tv_mode_get():
    return json_resp({"mode": tv.mode,
                      "mode_changed_at": tv.mode_changed_at})

# ========== 推荐歌曲 v2.0 ==========

@app.route("/api/recommend/charts")
def api_recommend_charts():
    """获取所有可用榜单列表"""
    charts = MusicRecommender.get_all_charts_info()
    return json_resp({"charts": charts})


@app.route("/api/recommend")
def api_recommend():
    """获取指定榜单的指定页歌曲"""
    chart_key = flask_request.args.get("chart", "qq_hot")
    page = flask_request.args.get("page", 1, type=int)
    result = MusicRecommender.get_page(chart_key, page)
    return json_resp(result)


@app.route("/api/recommend/refresh")
def api_recommend_refresh():
    """强制刷新指定榜单缓存"""
    chart_key = flask_request.args.get("chart", "")
    if chart_key:
        MusicRecommender.clear_cache(chart_key)
        result = MusicRecommender.get_page(chart_key, 1)
        return json_resp(result)
    else:
        MusicRecommender.clear_cache()
        return json_resp({"ok": True, "msg": "已清空全部缓存"})


# ========== 队列——支持版本号增量同步 ==========

@app.route("/api/queue")
def api_queue():
    """
    支持 ?v=<version> 参数。
    如果客户端传的版本号等于当前版本号，返回 changed=false 减少传输。
    """
    client_ver = flask_request.args.get("v", -1, type=int)
    current_ver = queue.get_version()

    if client_ver >= 0 and client_ver == current_ver:
        # ★ 版本未变，返回精简响应
        return json_resp({
            "changed": False,
            "version": current_ver,
            "queue_len": len(queue.get_list()),
            "tv": tv.to_dict(),
        })

    return json_resp({
        "changed": True,
        "version": current_ver,
        "queue": queue.get_list(),
        "guard": dl_guard.status(),
        "tv": tv.to_dict(),
    })


@app.route("/api/queue/add", methods=["POST"])
def api_queue_add():
    d = flask_request.get_json(force=True)
    name = d.get("name", "").strip()
    singer = d.get("singer", "").strip()
    if not name:
        return json_resp({"ok": False, "msg": "歌名为空"})
    song = SongInfo(
        name=name, singer=singer,
        source=d.get("source", "online"),
        album=d.get("album", ""),
        interval=d.get("interval", ""),
        file_path=d.get("file_path", ""),
        songmid=d.get("songmid", ""),
        search_source=d.get("search_source", ""),str_media_mid=d.get("str_media_mid", ""),
        album_mid=d.get("album_mid", ""),
    )
    ok, msg = queue.add(song)
    return json_resp({
        "ok": ok, "msg": msg, "uid": song.uid,
        "version": queue.get_version(),
    })



# ========== 音频服务 ==========

@app.route("/api/audio/original/<uid>")
def api_audio_original(uid):
    s = queue.find(uid)
    if not s or not s.file_path:
        return "not found", 404
    return send_file(s.file_path, mimetype=audio_mime(s.file_path))


@app.route("/api/audio/vocals/<uid>")
def api_audio_vocals(uid):
    s = queue.find(uid)
    if not s or not s.vocals_path:
        return "not found", 404
    return send_file(s.vocals_path, mimetype="audio/wav")


@app.route("/api/audio/accompaniment/<uid>")
def api_audio_accompaniment(uid):
    s = queue.find(uid)
    if not s or not s.accompaniment_path:
        return "not found", 404
    return send_file(s.accompaniment_path, mimetype="audio/wav")


# ★★★ FIX: 歌词端点增强日志 + retry ★★★
@app.route("/api/lyric/<uid>")
def api_lyric(uid):
    s = queue.find(uid)
    if not s:
        print("[LyricAPI] uid={} 未找到".format(uid))
        return json_resp({"ok": False, "msg": "未找到"})

    print("[LyricAPI] uid={}, name={}, file_path={}".format(
        uid, s.name, s.file_path))
    print("[LyricAPI]   lrc={} chars, lxlyric={} chars, tlyric={} chars".format(
        len(s.lrc or ""), len(s.lxlyric or ""), len(s.tlyric or "")))

    # ★ FIX: 如果歌词为空，再次尝试获取
    if not s.lrc and not s.lxlyric:
        print("[LyricAPI] 歌词为空，重新获取...")
        queue._grab_lyric(s)
        print("[LyricAPI] 重新获取后: lrc={} chars, lxlyric={} chars".format(
            len(s.lrc or ""), len(s.lxlyric or "")))

    # ★ 优先使用逐字歌词（lxlyric），其次普通歌词（lrc）
    lyric_text = s.lxlyric if s.lxlyric else s.lrc
    if lyric_text:
        lines = LrcParser.parse_enhanced(lyric_text)
        tlines = LrcParser.parse(s.tlyric) if s.tlyric else []
        has_words = any(l.get("words") for l in lines)

        print("[LyricAPI] 解析结果: {} 行, has_words={}, tlines={}".format(
            len(lines), has_words, len(tlines)))

        # ★ 打印前3行用于调试
        for i, line in enumerate(lines[:3]):
            word_count = len(line.get("words", []))
            print("[LyricAPI]   行{}: [{:.3f}] {} (words={})".format(
                i, line["time"], line["text"][:30], word_count))

        return json_resp({
            "ok": True, "lines": lines, "tlines": tlines,
            "has_words": has_words,
            "raw_lrc": s.lrc, "raw_tlyric": s.tlyric,
        })

    # ★ 兜底——lx-music API
    print("[LyricAPI] 兜底: 尝试 lx-music API...")
    la = lx.get_lyric_all()
    if la and la.get("lyric"):
        s.lrc = la["lyric"]
        s.tlyric = la.get("tlyric", "")
        s.lxlyric = la.get("lxlyric", "")
        lyric_text = s.lxlyric or s.lrc
        lines = LrcParser.parse_enhanced(lyric_text)
        tlines = LrcParser.parse(s.tlyric) if s.tlyric else []
        print("[LyricAPI] 兜底成功: {} 行".format(len(lines)))
        return json_resp({
            "ok": True, "lines": lines, "tlines": tlines,
            "has_words": any(l.get("words") for l in lines),
            "raw_lrc": s.lrc, "raw_tlyric": s.tlyric,
        })

    print("[LyricAPI] ❌ 无歌词")
    return json_resp({"ok": False, "msg": "无歌词"})


# ========== lx-music代理 ==========

@app.route("/api/lx/status")
def api_lx_status():
    s = lx.get_status()
    return json_resp(s) if s else json_resp({"error": "未连接"}, 502)


@app.route("/api/lx/subscribe")
def api_lx_subscribe():
    def gen():
        try:
            r = req_lib.get(
                "http://{}:{}/subscribe-player-status".format(
                    Config.PLAYER_HOST, Config.PLAYER_PORT),
                stream=True, timeout=None)
            for chunk in r.iter_content(chunk_size=None,
                                        decode_unicode=False):
                if chunk:
                    yield chunk.decode("utf-8", errors="replace")
        except Exception as e:
            yield 'event: error\ndata: "{}"\n\n'.format(e)
    return Response(gen(),
                    mimetype="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/api/lx/control/<action>")
def api_lx_control(action):
    valid = {"play", "pause", "skip-next", "skip-prev"}
    if action == "seek":
        lx.seek(flask_request.args.get("offset", 0, type=float))
    elif action == "volume":
        lx.set_volume(flask_request.args.get("volume", 50, type=int))
    elif action in valid:
        lx.control(action)
    else:
        return json_resp({"error": "unknown"}), 400
    return json_resp({"ok": True, "action": action})


@app.route("/api/qrcode")
def api_qrcode():
    url = "http://{}:{}/jukebox".format(Config.LAN_IP, Config.SERVE_PORT)
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/api/qrcode-url")
def api_qrcode_url():
    url = "http://{}:{}/jukebox".format(Config.LAN_IP, Config.SERVE_PORT)
    return json_resp({"url": url})


@app.route("/api")
def api_index():
    return json_resp({
        "name": "KKTV Backend API v3.4",
        "changelog": [
            "新增 /api/tv/skip TV专用切歌（避免双跳）",
            "TVState 新增 set_mode + mode_changed_at",
            "心跳响应增加 mode_changed_at",
            "_grab_lyric 增强：多编码 + 宽松匹配 + 增强日志",
            "_prepare 缓存命中时推断 file_path",
            "awlrc 解析增强：re.DOTALL + 清理空白",
            "歌词端点增强日志",
            "点歌台 mode 同步改进",
        ],
    })


# ========== 伴奏分离废料清理 ==========
@app.route("/api/cleanup")
def api_cleanup():
    """清理分离目录中的无用文件（bass, drums, other）"""
    sep_dir = Path(Config.SEPARATED_DIR)
    if not sep_dir.exists():
        return json_resp({"ok": False, "msg": "分离目录不存在",
                          "deleted_count": 0, "freed_mb": 0})

    deleted_count = 0
    freed_bytes = 0
    useless_files = ["bass.wav", "drums.wav", "other.wav"]

    for song_dir in sep_dir.iterdir():
        if not song_dir.is_dir():
            continue
        for filename in useless_files:
            file_path = song_dir / filename
            if file_path.exists():
                try:
                    size = file_path.stat().st_size
                    file_path.unlink()
                    deleted_count += 1
                    freed_bytes += size
                except Exception as e:
                    print("[Cleanup] 删除失败: {} - {}".format(file_path, e))

    freed_mb = round(freed_bytes / (1024 * 1024), 2)
    print("[Cleanup] 完成: 删除{}个文件, 释放{}MB".format(deleted_count, freed_mb))
    return json_resp({
        "ok": True,
        "deleted_count": deleted_count,
        "freed_mb": freed_mb,
        "msg": "已删除 {} 个文件，释放 {} MB".format(deleted_count, freed_mb)
    })


@app.route("/api/cleanup/preview")
def api_cleanup_preview():
    """预览可清理的文件数量和大小"""
    sep_dir = Path(Config.SEPARATED_DIR)
    if not sep_dir.exists():
        return json_resp({"count": 0, "size_mb": 0})

    count = 0
    total_bytes = 0
    useless_files = ["bass.wav", "drums.wav", "other.wav"]

    for song_dir in sep_dir.iterdir():
        if not song_dir.is_dir():
            continue
        for filename in useless_files:
            file_path = song_dir / filename
            if file_path.exists():
                count += 1
                total_bytes += file_path.stat().st_size

    return json_resp({
        "count": count,
        "size_mb": round(total_bytes / (1024 * 1024), 2)
    })



# ============================================================
# ★ 点歌台 H5 v3.4 — mode同步改进 + 重唱反馈
# ============================================================
JUKEBOX_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>KKTV 点歌台</title>
<style>
:root{--p:#ff6ec4;--s:#7873f5;--bg:#0f0c29;--card:rgba(255,255,255,.08);--dim:#999}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Microsoft YaHei',sans-serif;
  background:linear-gradient(135deg,var(--bg),#302b63,#24243e);
  color:#fff;min-height:100vh;padding:12px;padding-bottom:80px;
  -webkit-tap-highlight-color:transparent}
.hdr{text-align:center;padding:14px 0}
.hdr h1{font-size:1.5em;background:linear-gradient(90deg,var(--p),var(--s));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sub{color:var(--dim);font-size:.78em;margin-top:3px}
.sbox{position:sticky;top:0;z-index:100;
  background:rgba(15,12,41,.95);padding:8px 0;backdrop-filter:blur(10px)}
.srow{display:flex;gap:6px}
.srow input{flex:1;background:var(--card);border:1px solid rgba(255,255,255,.15);
  color:#fff;padding:10px 12px;border-radius:10px;font-size:1em;outline:none}
.srow input:focus{border-color:var(--p)}
.sbtn{background:linear-gradient(135deg,var(--s),var(--p));border:none;color:#fff;
  padding:10px 16px;border-radius:10px;font-size:1em;cursor:pointer;white-space:nowrap}
.tabs{display:flex;margin:10px 0;background:var(--card);border-radius:10px;overflow:hidden}
.tab{flex:1;padding:9px;text-align:center;cursor:pointer;font-size:.88em;transition:.3s}
.tab.on{background:linear-gradient(135deg,var(--s),var(--p));font-weight:bold}
.card{background:var(--card);border-radius:10px;padding:11px 12px;margin-bottom:7px;
  display:flex;align-items:center;justify-content:space-between;
  border:1px solid rgba(255,255,255,.04)}
.info{flex:1;min-width:0}
.nm{font-size:.92em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ar{font-size:.78em;color:var(--dim);margin-top:1px}
.bg{font-size:.62em;padding:1px 4px;border-radius:3px;margin-left:3px;vertical-align:middle}
.bg-l{background:#4caf50}.bg-c{background:var(--s)}.bg-o{background:#2196f3}
.bg-q{background:#ff9800}.bg-r{background:#4caf50}
.bg-p{background:var(--p);animation:pls 1.5s infinite}
.bg-d{background:#2196f3}.bg-s{background:#9c27b0}.bg-e{background:#f44336}
.bg-nd{background:#e91e63}
@keyframes pls{50%{opacity:.5}}
.acts{display:flex;gap:4px;margin-left:6px;flex-shrink:0}
.ab{background:linear-gradient(135deg,var(--s),var(--p));border:none;color:#fff;
  width:32px;height:32px;border-radius:50%;font-size:.9em;cursor:pointer;
  display:flex;align-items:center;justify-content:center}
.ab.red{background:linear-gradient(135deg,#e91e63,#f44336)}
.empty{text-align:center;padding:28px 14px;color:var(--dim)}
.empty .ic{font-size:2.2em;margin-bottom:6px}
.bbar{position:fixed;bottom:0;left:0;right:0;background:rgba(15,12,41,.98);
  backdrop-filter:blur(10px);padding:8px 10px;display:flex;gap:5px;
  border-top:1px solid rgba(255,255,255,.08);z-index:200}
.bb{flex:1;background:var(--card);border:1px solid rgba(255,255,255,.06);
  color:#fff;padding:8px;border-radius:7px;font-size:.82em;cursor:pointer;text-align:center}
.bb.pri{background:linear-gradient(135deg,var(--s),var(--p));border:none;font-weight:bold}
.toast{position:fixed;top:14px;left:50%;transform:translateX(-50%) translateY(-70px);
  background:rgba(0,0,0,.9);color:#fff;padding:8px 18px;border-radius:7px;
  font-size:.82em;z-index:1000;transition:.3s;pointer-events:none}
.toast.show{transform:translateX(-50%) translateY(0)}
.tip{font-size:.78em;color:var(--dim);margin:6px 0;padding:6px 8px;
  background:rgba(255,255,255,.03);border-radius:5px}
.stitle{font-size:.95em;color:var(--p);margin:10px 0 6px;display:flex;align-items:center;gap:5px}
.cnt{background:var(--p);color:#fff;min-width:18px;height:18px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;font-size:.7em;font-weight:bold}
.cur{border-left:3px solid var(--p);background:rgba(255,110,196,.08)}
.gi{font-size:.72em;color:var(--dim);text-align:center;margin:6px 0}
.sep{border-top:1px solid rgba(255,255,255,.06);margin:8px 0;font-size:.75em;
  color:var(--dim);padding-top:6px}
.dur{color:var(--dim);font-size:.72em}
.ld{display:inline-block;width:12px;height:12px;border:2px solid rgba(255,255,255,.3);
  border-top-color:#fff;border-radius:50%;animation:sp .7s linear infinite;vertical-align:middle}
@keyframes sp{to{transform:rotate(360deg)}}

.chart-tabs{display:flex;gap:4px;overflow-x:auto;padding:6px 0;-webkit-overflow-scrolling:touch}
.chart-tabs::-webkit-scrollbar{display:none}
.ct{background:var(--card);border:1px solid rgba(255,255,255,.1);color:var(--dim);
  padding:6px 12px;border-radius:16px;font-size:.78em;cursor:pointer;
  white-space:nowrap;transition:.3s;flex-shrink:0}
.ct.on{background:linear-gradient(135deg,var(--s),var(--p));color:#fff;border-color:transparent}
.rank{display:inline-flex;align-items:center;justify-content:center;
  width:22px;height:22px;border-radius:50%;font-size:.72em;font-weight:bold;
  margin-right:6px;flex-shrink:0}
.rank.t1{background:linear-gradient(135deg,#ff6b35,#ff2d00);color:#fff}
.rank.t2{background:linear-gradient(135deg,#ff9800,#ff6d00);color:#fff}
.rank.t3{background:linear-gradient(135deg,#ffc107,#ff9800);color:#fff}
.rank.tn{background:rgba(255,255,255,.08);color:var(--dim)}
.chart-src{font-size:.62em;padding:1px 4px;border-radius:3px;margin-left:3px;
  vertical-align:middle}
.chart-src.kw{background:#ff6600;color:#fff}
.chart-src.qq{background:#18b636;color:#fff}
.rec-hdr{display:flex;justify-content:space-between;align-items:center;margin:8px 0 4px}
.rec-hdr h3{font-size:.95em;color:var(--p)}
.rec-hdr .rfbtn{font-size:.72em;color:var(--s);cursor:pointer;padding:3px 8px;
  border:1px solid rgba(120,115,245,.3);border-radius:12px;background:transparent}

/* ★ 分页控件 */
.pager{display:flex;justify-content:center;align-items:center;gap:12px;
  padding:12px 0;margin:4px 0}
.pager .pbtn{background:linear-gradient(135deg,var(--s),var(--p));border:none;color:#fff;
  padding:8px 20px;border-radius:8px;font-size:.88em;cursor:pointer;
  min-width:70px;text-align:center;transition:.2s}
.pager .pbtn:active{transform:scale(.95)}
.pager .pbtn.dis{opacity:.3;pointer-events:none}
.pager .pinfo{color:var(--dim);font-size:.82em;min-width:80px;text-align:center}
</style>
</head>
<body>
<div class="hdr">
  <h1>🎤 KKTV 点歌台</h1>
  <div class="sub" id="conn">连接中...</div>
</div>

<div class="sbox">
  <div class="srow">
    <input id="kw" placeholder="搜索歌曲名、歌手..." onkeypress="if(event.key==='Enter')search()">
    <button class="sbtn" onclick="search()">搜索</button>
  </div>
</div>

<div class="tabs">
  <div class="tab on" id="t0" onclick="tab('s')">🔍 搜索</div>
  <div class="tab" id="t1" onclick="tab('q')">📋 已点 <span id="qb"></span></div>
  <div class="tab" id="t2" onclick="tab('r')">🔥 推荐</div>
</div>

<div id="ps">
  <div id="sr">
    <div class="empty"><div class="ic">🎵</div>
    <p>搜索你想唱的歌！</p>
    <p class="tip">支持在线搜索全网歌曲，可选择不同版本/翻唱</p></div>
  </div>
</div>

<div id="pq" style="display:none">
  <div id="cur"></div>
  <div class="stitle"><span>等待播放</span><span class="cnt" id="qc">0</span></div>
  <div id="ql"></div>
  <div class="gi" id="gi"></div>
</div>

<div id="pr" style="display:none">
  <div class="rec-hdr">
    <h3 id="chart-title">🔥 热歌推荐</h3>
    <span class="rfbtn" onclick="refreshChart()">🔄 刷新</span>
  </div>
  <div class="chart-tabs" id="chart-tabs"></div>
  <div id="chart-list">
    <div class="empty"><span class="ld"></span> 加载榜单中...</div>
  </div>
</div>

<div class="bbar">
  <button class="bb" onclick="replay()">🔁 重唱</button>
  <button class="bb" onclick="skip()">⏭ 切歌</button>
  <button class="bb" id="modeBtn" onclick="toggleMode()">🎵 伴奏</button>
  <button class="bb pri" onclick="rq()">🔄 刷新</button>
</div>

<div class="toast" id="tt"></div>

<script>
var ct='s';
var curMode='accompaniment';
var modeChangeTime=0;
var localModeOverride=false;

// ★ 队列版本号（增量同步）
var queueVer=-1;     // 仅在 rq() 成功渲染后更新
var badgeVer=-1;     // ubadge() 独立版本号，不影响渲染判断


// ★ 推荐相关状态
var curChart='hot';
var chartPageState={};  // {chart_key: {page:1, totalPages:0, totalSongs:0, loading:false}}

function tab(t){
  ct=t;
  document.getElementById('t0').className=t==='s'?'tab on':'tab';
  document.getElementById('t1').className=t==='q'?'tab on':'tab';
  document.getElementById('t2').className=t==='r'?'tab on':'tab';
  document.getElementById('ps').style.display=t==='s'?'block':'none';
  document.getElementById('pq').style.display=t==='q'?'block':'none';
  document.getElementById('pr').style.display=t==='r'?'block':'none';
  if(t==='q'){queueVer=-1;rq();}
  if(t==='r')loadChartTabs();
}

function search(){
  var k=document.getElementById('kw').value.trim();
  if(!k){toast('请输入关键词');return}
  tab('s');
  var d=document.getElementById('sr');
  d.innerHTML='<div class="empty"><span class="ld"></span> 搜索中...</div>';
  fetch('/api/search?q='+encodeURIComponent(k))
  .then(function(r){return r.json()})
  .then(function(data){
    var local=data.local||[];
    var online=data.online||[];
    if(!local.length && !online.length){
      d.innerHTML='<div class="empty"><div class="ic">😅</div><p>没找到「'+he(k)+'」</p></div>';
      return;
    }
    var h='';
    if(local.length){
      h+='<div class="tip">🏠 本地已有 ('+local.length+'首，点歌秒唱)</div>';
      local.forEach(function(s){
        var bg='<span class="bg bg-l">本地</span>';
        if(s.has_cache)bg+='<span class="bg bg-c">秒唱</span>';
        h+=mkCard(s, bg, true, -1);
      });
    }
    if(online.length){
      h+='<div class="sep">🌐 在线搜索 ('+online.length+'首，可选版本)</div>';
      online.forEach(function(s){
        var bg='<span class="bg bg-o">在线</span>';
        if(s.is_local)bg+='<span class="bg bg-l">已下载</span>';
        h+=mkCard(s, bg, false, -1);
      });
    }
    d.innerHTML=h;
  })
  .catch(function(e){
    d.innerHTML='<div class="empty"><div class="ic">❌</div><p>搜索失败</p><p class="tip">'+e+'</p></div>';
  });
}

function mkCard(s, badges, isLocal, rank){
  var dur=s.duration>0?fmt(s.duration):'';
  var src=isLocal?'local':'online';
  var fp=s.file_path||'';
  var iv=s.interval||'';
  var alb=s.album||'';
  var mid=s.songmid||'';
  var ssrc=s.search_source||'';
  var smm=s.str_media_mid||'';
  var amm=s.album_mid||'';
  var rankHtml='';
  if(rank>=0){
    var rc=rank<1?'t1':rank<2?'t2':rank<3?'t3':'tn';
    rankHtml='<span class="rank '+rc+'">'+(rank+1)+'</span>';
  }
  return '<div class="card"><div class="info" style="display:flex;align-items:center">'
    +rankHtml
    +'<div style="min-width:0">'
    +'<div class="nm">'+he(s.name)+badges+'</div>'
    +'<div class="ar">'+he(s.singer||'未知')+(alb?' · '+he(alb):'')
      +(dur?' · <span class="dur">'+dur+'</span>':'')
    +'</div></div></div><div class="acts">'
    +'<button class="ab" onclick="add(this,'+js(s.name)+','+js(s.singer)+','+js(src)+','+js(fp)+','+js(alb)+','+js(iv)
      +','+js(mid)+','+js(ssrc)+','+js(smm)+','+js(amm)
      +')" title="点歌">+</button>'
    +'</div></div>';
}



function add(btn,nm,sg,src,fp,alb,iv,mid,ssrc,smm,amm){
  btn.disabled=true; btn.textContent='…';
  fetch('/api/queue/add',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:nm,singer:sg,source:src,
      file_path:fp,album:alb,interval:iv,
      songmid:mid||'',search_source:ssrc||'',
      str_media_mid:smm||'',album_mid:amm||''})}).then(function(r){return r.json()})
  .then(function(d){
    toast(d.msg);
    if(d.ok){
      btn.textContent='✓';btn.style.background='#4caf50';
      if(d.version!==undefined) badgeVer=d.version;
      queueVer=-1;
    }
    else{btn.disabled=false;btn.textContent='+'}ubadge();
  }).catch(function(){btn.disabled=false;btn.textContent='+'});
}



// ★★★ 队列请求——带版本号增量同步 ★★★
function rq(){
  var url='/api/queue';
  if(queueVer>=0) url+='?v='+queueVer;
  fetch(url).then(function(r){return r.json()})
  .then(function(d){
    if(d.version!==undefined) queueVer=d.version;

    // ★ 如果版本未变且不是强制刷新，跳过DOM更新
    if(d.changed===false){
      // 仍然更新TV状态和badge
      var tvd=d.tv||{};
      if(tvd.connected && tvd.playing){
        document.getElementById('conn').textContent='📺 '+tvd.song_name+' 播放中';
        document.getElementById('conn').style.color='#4caf50';
      }
      if(!localModeOverride && tvd.mode){
        curMode=tvd.mode;
        updateModeBtn();
      }
      var len=d.queue_len||0;
      document.getElementById('qb').textContent=len>0?'('+len+')':'';
      return;
    }

    // ★ 版本变了，完整渲染
    renderQ(d.queue);
    ubadge(d.queue?d.queue.length:0);
    var g=d.guard||{};
    var gi=document.getElementById('gi');
    if(g.is_downloading)gi.textContent='⬇️ 正在触发音乐软件...';
    else if(g.cooldown>0)gi.textContent='🛡️ 冷却 '+g.cooldown+'s';
    else gi.textContent='';
    var tvd=d.tv||{};
    if(tvd.connected && tvd.playing){
      document.getElementById('conn').textContent='📺 '+tvd.song_name+' 播放中';
      document.getElementById('conn').style.color='#4caf50';
    }
    if(!localModeOverride && tvd.mode){
      curMode=tvd.mode;
      updateModeBtn();
    }
  });
}

function renderQ(q){
  var cd=document.getElementById('cur');
  var ld=document.getElementById('ql');
  var cn=document.getElementById('qc');
  var c=null,w=[];
  (q||[]).forEach(function(s){if(s.is_current)c=s;else w.push(s)});
  if(c){
    cd.innerHTML='<div class="stitle">🎤 正在播放</div>'
      +'<div class="card cur"><div class="info">'
      +'<div class="nm">'+he(c.name)+'<span class="bg bg-p">♪</span></div>'
      +'<div class="ar">'+he(c.singer)+'</div></div></div>';
  }else cd.innerHTML='';
  cn.textContent=w.length;
  if(!w.length){
    ld.innerHTML='<div class="empty"><div class="ic">📭</div><p>暂无等待</p></div>';
    return;
  }
  var h='';
  w.forEach(function(s,i){
    var sl=stl(s.state),bc=stb(s.state);
    var cr=s.state!=='downloading'&&s.state!=='separating';
    h+='<div class="card"><div class="info">'
      +'<div class="nm"><span style="color:var(--dim);margin-right:3px">'+(i+1)+'.</span>'
      +he(s.name)+'<span class="bg '+bc+'">'+sl+'</span></div>'
      +'<div class="ar">'+he(s.singer)
      +(s.error_msg?' · <span style="color:#f44336;font-size:.72em">'+he(s.error_msg)+'</span>':'')
      +'</div></div><div class="acts">'
      +'<button class="ab" onclick="top1(\''+s.uid+'\')">⬆</button>'
      +(cr?'<button class="ab red" onclick="rm(\''+s.uid+'\')">✕</button>':'')
      +'</div></div>';
  });
  ld.innerHTML=h;
}

function stl(s){return{queued:'排队',waiting_download:'等下载',downloading:'准备中',
  needs_download:'待下载',downloaded:'处理中',
  separating:'分离中',ready:'就绪',playing:'播放中',error:'错误'}[s]||s}
function stb(s){return{queued:'bg-q',waiting_download:'bg-q',downloading:'bg-d',
  needs_download:'bg-nd',downloaded:'bg-d',
  separating:'bg-s',ready:'bg-r',playing:'bg-p',error:'bg-e'}[s]||''}

function rm(u){fetch('/api/queue/remove/'+u).then(function(r){return r.json()})
  .then(function(d){toast(d.msg);queueVer=-1;rq()})}
function top1(u){fetch('/api/queue/top/'+u).then(function(r){return r.json()})
  .then(function(d){toast(d.msg);queueVer=-1;rq()})}
function skip(){fetch('/api/queue/skip').then(function(r){return r.json()})
  .then(function(d){toast(d.msg);queueVer=-1;rq()})}

function replay(){
  fetch('/api/queue/replay').then(function(r){return r.json()})
  .then(function(d){
    toast(d.msg);
    if(d.ok) toast('🔁 已发送重唱指令');
  })
  .catch(function(){toast('❌ 重唱失败')});
}

function ubadge(n){
  var b=document.getElementById('qb');
  if(n!==undefined){b.textContent=n>0?'('+n+')':'';return}
  fetch('/api/queue?v='+badgeVer).then(function(r){return r.json()})
  .then(function(d){
    if(d.version!==undefined) badgeVer=d.version;
    var l=d.queue?d.queue.length:(d.queue_len||0);
    b.textContent=l>0?'('+l+')':''
  })
  .catch(function(){});
}


function toggleMode(){
  var newMode=curMode==='accompaniment'?'original':'accompaniment';
  modeChangeTime=Date.now();
  localModeOverride=true;
  curMode=newMode;
  updateModeBtn();
  fetch('/api/tv/mode',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:newMode})})
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.ok){
      toast(curMode==='accompaniment'?'🎵 已切换为伴奏':'🎤 已切换为原唱');
    } else { toast('❌ 切换失败'); }
  })
  .catch(function(){toast('❌ 网络错误')});
  setTimeout(function(){localModeOverride=false;},10000);
}

function updateModeBtn(){
  var btn=document.getElementById('modeBtn');
  if(curMode==='accompaniment'){
    btn.textContent='🎵 伴奏';btn.style.background='var(--card)';
  }else{
    btn.textContent='🎤 原唱';
    btn.style.background='linear-gradient(135deg, #e91e63, #f44336)';
  }
}

// ============================================================
// ★★★ 推荐功能 v2.0 — 分页 ★★★
// ============================================================

function loadChartTabs(){
  fetch('/api/recommend/charts')
  .then(function(r){return r.json()})
  .then(function(d){
    var tabs=document.getElementById('chart-tabs');
    var charts=d.charts||[];
    if(!charts.length){
      tabs.innerHTML='<span style="color:var(--dim)">暂无可用榜单</span>';
      return;
    }
    var h='';
    charts.forEach(function(c){
      var on=c.key===curChart?' on':'';
      var extra=c.total_songs>0?' ('+c.total_songs+')':'';
      h+='<span class="ct'+on+'" data-key="'+c.key+'" onclick="switchChart(\''+c.key+'\')">'
        +c.name+extra+'</span>';
    });
    tabs.innerHTML=h;
    // ★ 初始化页面状态（如果还没有的话）
    if(!chartPageState[curChart]){
      chartPageState[curChart]={page:1,totalPages:0,totalSongs:0,loading:false};
    }
    loadChartPage(curChart, chartPageState[curChart].page);
  })
  .catch(function(e){
    document.getElementById('chart-tabs').innerHTML=
      '<span style="color:#f44336">加载榜单分类失败</span>';
  });
}

function switchChart(key){
  curChart=key;
  // 更新tab样式
  var allCt=document.querySelectorAll('.ct');
  allCt.forEach(function(el){
    el.className=el.getAttribute('data-key')===key?'ct on':'ct';
  });
  // ★ 初始化该榜单的分页状态
  if(!chartPageState[key]){
    chartPageState[key]={page:1,totalPages:0,totalSongs:0,loading:false};
  }
  loadChartPage(key, chartPageState[key].page);
}

function loadChartPage(key, page){
  var st=chartPageState[key];
  if(!st) st=chartPageState[key]={page:1,totalPages:0,totalSongs:0,loading:false};
  if(st.loading) return;  // ★ 防止重复请求
  st.loading=true;
  st.page=page;

  var list=document.getElementById('chart-list');
  list.innerHTML='<div class="empty"><span class="ld"></span> 加载第'+page+'页...</div>';

  fetch('/api/recommend?chart='+encodeURIComponent(key)+'&page='+page)
  .then(function(r){return r.json()})
  .then(function(d){
    st.loading=false;
    if(!d.ok){
      list.innerHTML='<div class="empty"><div class="ic">😅</div>'
        +'<p>'+(d.msg||'加载失败')+'</p></div>';
      return;
    }
    st.page=d.page;
    st.totalPages=d.total_pages;
    st.totalSongs=d.total_songs;
    renderChartPage(d, key);
  })
  .catch(function(e){
    st.loading=false;
    list.innerHTML='<div class="empty"><div class="ic">❌</div>'
      +'<p>加载失败</p><p class="tip">'+e+'</p></div>';
  });
}

function renderChartPage(d, key){
  var list=document.getElementById('chart-list');
  var title=document.getElementById('chart-title');

  title.textContent='🔥 '+d.chart_name+' ('+d.total_songs+'首)';
  var songs=d.songs||[];

  if(!songs.length){
    list.innerHTML='<div class="empty"><div class="ic">📭</div><p>该榜单暂无数据</p></div>';
    return;
  }

  var h='<div class="tip">第 '+d.page+' / '+d.total_pages+' 页 · 共 '+d.total_songs+' 首· 每页 '+d.page_size+' 首</div>';

  var rankOffset=(d.page-1)*d.page_size;

  songs.forEach(function(s,i){
    h+=mkCard(s, '', false, rankOffset+i);
  });

  h+='<div class="pager">';
  h+='<span class="pbtn'+(d.has_prev?'':' dis')+'" onclick="chartPrev(\''+key+'\')">← 上一页</span>';
  h+='<span class="pinfo">'+d.page+' / '+d.total_pages+'</span>';
  h+='<span class="pbtn'+(d.has_next?'':' dis')+'" onclick="chartNext(\''+key+'\')">下一页 →</span>';
  h+='</div>';

  list.innerHTML=h;
}


function chartPrev(key){
  var st=chartPageState[key];
  if(!st||st.page<=1||st.loading) return;
  loadChartPage(key, st.page-1);
}

function chartNext(key){
  var st=chartPageState[key];
  if(!st||!st.totalPages||st.page>=st.totalPages||st.loading) return;
  loadChartPage(key, st.page+1);
}

function refreshChart(){
  // ★ 清除该榜单的分页状态
  delete chartPageState[curChart];
  chartPageState[curChart]={page:1,totalPages:0,totalSongs:0,loading:false};
  var list=document.getElementById('chart-list');
  list.innerHTML='<div class="empty"><span class="ld"></span> 强制刷新中...</div>';
  fetch('/api/recommend/refresh?chart='+encodeURIComponent(curChart))
  .then(function(r){return r.json()})
  .then(function(d){
    if(d.ok){
      var st=chartPageState[curChart];
      st.page=d.page||1;
      st.totalPages=d.total_pages||0;
      st.totalSongs=d.total_songs||0;
      renderChartPage(d, curChart);
    }else{
      list.innerHTML='<div class="empty"><div class="ic">❌</div><p>'+(d.msg||'刷新失败')+'</p></div>';
    }
    toast('已刷新');
  })
  .catch(function(e){
    list.innerHTML='<div class="empty"><div class="ic">❌</div><p>刷新失败</p></div>';
  });
}

// ============================================================
// 通用工具
// ============================================================
function toast(m){var t=document.getElementById('tt');t.textContent=m;
  t.className='toast show';setTimeout(function(){t.className='toast'},2500)}
function he(s){if(!s)return'';var d=document.createElement('div');d.textContent=s;return d.innerHTML}
function js(s){return"'"+((s||'')+'').replace(/\\/g,'\\\\').replace(/'/g,"\\'")+"'"}
function fmt(s){if(!s)return'';return Math.floor(s/60)+':'+(('0'+Math.floor(s%60)).slice(-2))}

window.onload=function(){
  fetch('/api/health').then(function(r){return r.json()})
  .then(function(d){
    var el=document.getElementById('conn');
    var p=[];
    p.push(d.lx_music?'✅ 音源连接':'⚠️ 音源未连接');
    if(d.local_songs>0)p.push('本地'+d.local_songs+'首');
    if(d.cached>0)p.push('缓存'+d.cached+'首');
    if(d.tv&&d.tv.connected)p.push('📺 TV在线');
    el.textContent=p.join(' | ');
    el.style.color=d.lx_music?'#4caf50':'#ff9800';
  }).catch(function(){
    document.getElementById('conn').textContent='❌ 服务器离线';
    document.getElementById('conn').style.color='#f44336';
  });
  ubadge();

  // ★★★ 优化轮询策略 ★★★
  // 队列tab激活时3秒轮询（带版本号增量），非激活时8秒轮询只更badge
  setInterval(function(){
    if(ct==='q'){
      rq();  // 带版本号，未变化时不更新DOM
    }else{
      ubadge();  // 非队列tab只更新badge
    }
  },3000);
};
</script>
</body>
</html>
"""

# ============================================================
# 控制台
# ============================================================
CONSOLE_HTML = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><title>KKTV 控制台</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Microsoft YaHei',sans-serif;background:linear-gradient(135deg,#0f0c29,#302b63,#24243e);
  color:#fff;min-height:100vh;padding:20px}
h1{text-align:center;font-size:1.8em;margin-bottom:18px;
  background:linear-gradient(90deg,#ff6ec4,#7873f5);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent}
.g{display:grid;grid-template-columns:1fr 1fr;gap:14px;max-width:1000px;margin:0 auto}
.p{background:rgba(255,255,255,.08);border-radius:12px;padding:18px;
  backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,.08)}
.p.f{grid-column:span 2}
.p h2{color:#ff6ec4;margin-bottom:10px;font-size:1.1em}
.qrc{text-align:center;padding:14px}
.qrc img{border-radius:8px;background:#fff;padding:5px}
.qru{margin-top:5px;color:#aaa;font-family:monospace;font-size:.82em;word-break:break-all}
.sg{display:grid;grid-template-columns:auto 1fr;gap:5px 10px}
.sg .l{color:#aaa}
.qi{background:rgba(255,255,255,.04);border-radius:5px;padding:7px 10px;margin-bottom:3px;
  display:flex;justify-content:space-between;font-size:.9em}
.qi.c{border-left:3px solid #ff6ec4;background:rgba(255,110,196,.06)}
.btn{background:linear-gradient(90deg,#7873f5,#ff6ec4);border:none;color:#fff;
  padding:7px 14px;border-radius:5px;cursor:pointer;margin:2px;font-size:.85em}
.btn.red{background:linear-gradient(90deg,#e91e63,#f44336)}
.btn.green{background:linear-gradient(90deg,#4caf50,#8bc34a)}
.cf{display:grid;grid-template-columns:90px 1fr;gap:5px;align-items:center;font-size:.9em}
.cf input{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.12);
  color:#fff;padding:5px;border-radius:4px;font-size:.85em}
.cleanup-info{font-size:.82em;color:#aaa;margin:8px 0}
.cleanup-info .num{color:#ff6ec4;font-weight:bold}
</style></head><body>
<h1>🎤 KKTV 控制台 v3.5</h1>
<div class="g">
<div class="p"><h2>📱扫码点歌</h2><div class="qrc"><img id="qr" src="/api/qrcode" width="160" height="160">
  <div class="qru" id="qu">...</div></div></div>

<div class="p"><h2>📊 状态</h2>
  <div class="sg">
    <span class="l">TV</span><span id="ztv">--</span>
    <span class="l">lx-music</span><span id="zlx">--</span>
    <span class="l">本地</span><span id="zl">--</span>
    <span class="l">缓存</span><span id="zc">--</span>
    <span class="l">下载</span><span id="zg">--</span>
  </div>
  <div style="margin-top:8px">
    <button class="btn" onclick="doReplay()">🔁 重唱</button>
    <button class="btn" onclick="doSkip()">⏭ 切歌</button>
  </div>
</div>

<div class="p f"><h2>📋 队列 <button class="btn" onclick="rq()" style="float:right">🔄</button></h2>
  <div id="qd"><p style="color:#aaa">加载中</p></div></div>

<div class="p"><h2>⚙️ 配置</h2>
  <div class="cf">
    <label>播放器IP</label><input id="ch">
    <label>端口</label><input id="cp">
    <label>局域网IP</label><input id="ci">
    <label>下载间隔</label><input id="cd">
    <label>LX下载目录</label><input id="cl" placeholder="lx-music下载路径">
  </div>
  <button class="btn" onclick="sc()" style="margin-top:6px">💾 保存</button></div>

<div class="p"><h2>🧹缓存清理</h2>
  <p class="cleanup-info">分离缓存中的无用文件（bass/drums/other）：<br>
    <span class="num" id="cleanup-count">--</span> 个文件，约
    <span class="num" id="cleanup-size">--</span> MB</p>
  <button class="btn red" onclick="doCleanup()">🗑️ 一键清理</button>
  <button class="btn" onclick="previewCleanup()">🔍 刷新统计</button>
</div>

<div class="p"><h2>🔗 API</h2>
  <p style="font-size:.82em;color:#aaa"><a href="/api" style="color:#7873f5">/api</a> - API文档索引<br>
    <a href="/jukebox" style="color:#ff6ec4">/jukebox</a> - 手机点歌台
  </p></div>
</div>
<script>
function rq(){
  fetch('/api/queue').then(function(r){return r.json()}).then(function(d){
    var div=document.getElementById('qd');
    var q=d.queue||[];
    if(!q.length){div.innerHTML='<p style="color:#aaa">空</p>';return}
    var h='';q.forEach(function(s){
      var stateLabel={queued:'排队',waiting_download:'等下载',downloading:'准备中',needs_download:'⚠待下载',downloaded:'处理中',separating:'分离中',
        ready:'就绪',playing:'播放中',error:'错误'}[s.state]||s.state;
      h+='<div class="qi'+(s.is_current?' c':'')+'"><span>'+(s.is_current?'🎤':'')+s.name+' - '+s.singer+'</span>'+'<span style="color:#aaa">'+stateLabel+'</span></div>';
    });div.innerHTML=h;
    var g=d.guard||{};
    document.getElementById('zg').textContent=
      (g.is_downloading?'触发中':'空闲')+' | CD:'+g.cooldown+'s | #'+g.total;var tv=d.tv||{};
    document.getElementById('ztv').textContent=
      tv.connected?(tv.playing?'▶ '+tv.song_name:'⏸ 已连接'):'❌ 未连接';});
}
function lxs(){
  fetch('/api/lx/status').then(function(r){return r.json()}).then(function(d){
    document.getElementById('zlx').textContent=(d.status||d.error||'--')+' '+(d.name||'');
  }).catch(function(){document.getElementById('zlx').textContent='离线'});
}
function hi(){
  fetch('/api/health').then(function(r){return r.json()}).then(function(d){
    document.getElementById('zl').textContent=d.local_songs+'首';
    document.getElementById('zc').textContent=d.cached+'首';
  });
}
function lc(){
  fetch('/api/config').then(function(r){return r.json()}).then(function(d){
    document.getElementById('ch').value=d.player_host;
    document.getElementById('cp').value=d.player_port;
    document.getElementById('ci').value=d.lan_ip;
    document.getElementById('cd').value=d.dl_interval;
    document.getElementById('qu').textContent='http://'+d.lan_ip+':'+d.serve_port+'/jukebox';
    document.getElementById('cl').value=d.lx_download_dir||'';
  });
}
function sc(){
  fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({
      player_host:document.getElementById('ch').value,
      player_port:document.getElementById('cp').value,
      lan_ip:document.getElementById('ci').value,
      dl_interval:document.getElementById('cd').value,
      lx_download_dir:document.getElementById('cl').value})}).then(function(r){return r.json()})
  .then(function(d){alert('已保存');location.reload()});
}
function doReplay(){fetch('/api/queue/replay').then(function(r){return r.json()}).then(function(d){alert(d.msg)})}
function doSkip(){fetch('/api/queue/skip').then(function(r){return r.json()}).then(function(d){alert(d.msg);rq()})}

//★ 清理功能
function previewCleanup(){
  fetch('/api/cleanup/preview').then(function(r){return r.json()}).then(function(d){
    document.getElementById('cleanup-count').textContent=d.count;
    document.getElementById('cleanup-size').textContent=d.size_mb;
  }).catch(function(){
    document.getElementById('cleanup-count').textContent='?';
    document.getElementById('cleanup-size').textContent='?';
  });
}
function doCleanup(){
  if(!confirm('确定要删除所有 bass/drums/other 文件吗？\\n（vocals和 no_vocals 会保留）')) return;
  fetch('/api/cleanup').then(function(r){return r.json()}).then(function(d){
    alert(d.msg);
    previewCleanup();
  }).catch(function(e){alert('清理失败: '+e)});
}

window.onload=function(){rq();lxs();hi();lc();previewCleanup();setInterval(rq,5000);setInterval(lxs,5000)};
</script></body></html>
"""



@app.route("/")
def page_console():
    return render_template_string(CONSOLE_HTML)


@app.route("/jukebox")
def page_jukebox():
    return render_template_string(JUKEBOX_HTML)




# ============================================================
# 启动
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("  🎤 KKTV Backend v3.4")
    print("=" * 60)
    print("  lx-music:  http://{}:{}".format(
        Config.PLAYER_HOST, Config.PLAYER_PORT))
    print("  Scheme:    {}".format(Config.SCHEME_PREFIX))
    print("  音乐目录:  {}".format(Config.MUSIC_DIR))
    print("  分离缓存:  {}".format(Config.SEPARATED_DIR))
    print("  控制台:    http://localhost:{}".format(Config.SERVE_PORT))
    print("  点歌台:    http://{}:{}/jukebox".format(
        Config.LAN_IP, Config.SERVE_PORT))
    print("  Python:    {}".format(sys.version))
    print("=" * 60)

    print("\n[Check] lx-music...", end=" ")
    if lx.is_connected():
        st = lx.get_status("status,name")
        print("✅ {}".format(st))
    else:
        print("⚠️  未连接")

    md = Path(Config.MUSIC_DIR)
    if md.exists():
        cnt = len([f for f in md.iterdir()
                   if f.is_file() and f.suffix.lower() in AUDIO_EXTS])
        print("[Check] 本地: ✅ {}首".format(cnt))

    sd = Path(Config.SEPARATED_DIR)
    if sd.exists():
        cc = len([d for d in sd.iterdir() if d.is_dir()])
        if cc:
            print("[Check] 缓存: ✅ {}首已分离".format(cc))

    queue.start()
        # ★ 修复：启动UDP发现服务
    discovery_thread = threading.Thread(
        target=start_discovery_service, daemon=True, name="DiscoveryService")
    discovery_thread.start()

    print("\n🚀 启动Flask服务器...\n")
    app.run(host=Config.SERVE_HOST, port=Config.SERVE_PORT,
            debug=False, threaded=True)

