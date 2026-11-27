# -*- coding: utf-8 -*-
"""
USB 相机网页控制台 · 服务端

纯标准库实现 (http.server + threading + cv2), 无需安装 Flask。

用法:
  python webcam_server.py                      # 127.0.0.1:8000
  python webcam_server.py --port 8080          # 换端口
  python webcam_server.py --host 0.0.0.0       # 局域网可访问
  python webcam_server.py --index 1            # 指定相机

接口:
  GET  /                      网页控制台 (index.html)
  GET  /style.css /app.js     前端静态资源
  GET  /stream                MJPEG 实时流
  GET  /status                JSON 状态
  POST /start                 启动相机
  POST /stop                  停止相机
  GET  /snapshot              拍一张并下载
  POST /record/start          开始录像
  POST /record/stop           停止录像
  GET  /files                 文件列表 (截图/录像)
  GET  /file/<name>           预览文件 (支持 Range, 便于播放/拖动)
  GET  /file/<name>/download  下载文件
  DELETE /file/<name>         删除文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent
SAVE_DIR = ROOT / "captures"
WEB_DIR = ROOT                      # index.html / style.css / app.js 放在项目根目录
HTML_PATH = WEB_DIR / "index.html"
HTML_PATH_LEGACY = ROOT / "web" / "index.html"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv"}

# ---- 颜色识别 (Lab 感知色距 + 常见中文色名, 纯算法, 无需数据集) ----
# 用 Lab 色彩空间做最近邻匹配, 比 HSV 色相分档更贴近人眼感知。
# 色名表: (BGR, 中文名) —— 覆盖常见颜色

COLOR_NAMES: list[tuple[tuple[int, int, int], str]] = [
    # 红 / 粉 / 橙系
    ((0, 0, 220), "红色"), ((0, 0, 160), "深红色"), ((0, 40, 255), "朱红色"),
    ((0, 90, 255), "橙红色"), ((0, 140, 255), "橙色"), ((0, 180, 255), "橘色"),
    ((20, 200, 255), "金黄色"), ((0, 235, 255), "黄色"), ((60, 255, 255), "柠檬黄"),
    ((255, 175, 235), "粉色"), ((200, 90, 225), "桃红色"), ((215, 60, 215), "玫红色"),
    ((225, 40, 235), "品红色"), ((210, 60, 195), "紫红色"),
    # 黄绿 / 绿系
    ((0, 255, 190), "黄绿色"), ((30, 245, 120), "草绿色"), ((0, 215, 0), "绿色"),
    ((0, 165, 0), "深绿色"), ((60, 220, 90), "翠绿色"), ((0, 130, 45), "墨绿色"),
    ((50, 110, 95), "橄榄绿"),
    # 青 / 蓝系
    ((180, 220, 60), "青色"), ((220, 235, 120), "浅青色"), ((220, 195, 40), "天蓝色"),
    ((255, 130, 30), "浅蓝色"), ((255, 95, 0), "蓝色"), ((225, 70, 20), "宝蓝色"),
    ((185, 50, 20), "靛蓝色"), ((150, 45, 25), "深蓝色"), ((120, 40, 25), "藏青色"),
    # 紫系
    ((190, 45, 165), "紫色"), ((170, 55, 135), "紫罗兰"), ((155, 45, 125), "深紫色"),
    # 无彩色
    ((10, 10, 10), "黑色"), ((45, 45, 45), "深灰色"), ((110, 110, 110), "灰色"),
    ((175, 175, 175), "浅灰色"), ((225, 225, 225), "银灰色"), ((250, 250, 250), "白色"),
    ((235, 228, 215), "米白色"), ((205, 180, 150), "驼色"), ((150, 115, 85), "咖啡色"),
    ((175, 135, 95), "棕色"), ((110, 80, 55), "深棕色"),
    # 补充常见深棕/土黄样本，避免被近邻归入橄榄绿或橙色。
    ((40, 80, 120), "棕色"), ((60, 130, 160), "土黄色"),
]


def _bgr_to_lab(bgr):
    """BGR -> CIELAB (D65)。"""
    import numpy as np
    b, g, r = [v / 255.0 for v in bgr]
    # sRGB -> linear
    def lin(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = lin(r), lin(g), lin(b)
    # linear RGB -> XYZ (D65)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041
    # XYZ -> Lab (D65 white)
    def f(t):
        return t ** (1 / 3) if t > 0.008856 else (7.787 * t + 16 / 116)
    xn, yn, zn = 0.95047, 1.0, 1.08883
    fx, fy, fz = f(x / xn), f(y / yn), f(z / zn)
    L = 116 * fy - 16
    a = 500 * (fx - fy)
    bb = 200 * (fy - fz)
    return (L, a, bb)


# 预计算色名表的 Lab 值 (模块加载时一次)
_COLOR_NAMES_LAB = [
    (_bgr_to_lab(bgr), name, bgr) for bgr, name in COLOR_NAMES
]

# 色名 -> 绘制用 BGR (画检测框/标签时取代表色; 由色名表反向构建)
HUE_DRAW: dict[str, tuple] = {name: bgr for bgr, name in COLOR_NAMES}


def classify_color(h: int, s: int, v: int,
                   bgr: tuple | None = None) -> tuple[str, float]:
    """把颜色分类成常见中文色名 + 置信度 (0~1)。

    优先用 Lab 感知色距 (比 HSV 更准), 没有 bgr 时退回 HSV 色相分档。
    """
    import math
    # 无彩色优先 (S 低 → 黑/灰/白系)
    if s < 32:
        if v < 32:
            return "黑色", 0.95
        if v < 78:
            return "深灰色", 0.9
        if v < 175:
            return "灰色", 0.9
        if v < 218:
            return "浅灰色", 0.88
        return "白色", 0.92

    # Lab 最近邻
    if bgr is not None:
        L, A, B = _bgr_to_lab(tuple(int(c) for c in bgr))
        best_name, best_d = "灰色", 1e9
        for (l2, a2, b2), name, _c in _COLOR_NAMES_LAB:
            d = (L - l2) ** 2 * 1.0 + (A - a2) ** 2 * 2.0 + (B - b2) ** 2 * 2.0
            if d < best_d:
                best_d, best_name = d, name
        # 距离 -> 置信度 (CIEDE2000 量级: 0~100, 这里做归一)
        # 典型"接近"距离 ~5-20, "明显不同" > 40
        conf = max(0.05, min(0.99, 1.0 - math.sqrt(best_d) / 60.0))
        return best_name, round(conf, 2)

    # 兜底: HSV 色相分档
    h = max(0, min(180, int(h)))
    for lo, hi, name in PRIMARY_HUES:
        if lo <= h < hi:
            return name, 0.6
    return "未知", 0.3


# HSV 兜底用的基础色相档
PRIMARY_HUES = [
    (0, 6, "红色"), (6, 18, "橙色"), (18, 36, "黄色"),
    (36, 52, "黄绿色"), (52, 82, "绿色"), (82, 100, "青色"),
    (100, 128, "蓝色"), (128, 152, "紫色"), (152, 172, "粉红色"),
    (172, 181, "红色"),
]


def _shade_name(base: str, s: int, v: int) -> str:
    """给基础色名加明暗/浓淡修饰 (Lab 分类已含色名, 仅做兜底)。"""
    return base


def _make_placeholder_jpeg() -> bytes:
    """相机未启动时发给浏览器的占位图 (深灰 + NO SIGNAL)。"""
    try:
        import cv2
        import numpy as np
        img = np.full((480, 640, 3), (17, 21, 26), dtype=np.uint8)
        cv2.putText(img, "NO SIGNAL", (170, 250),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (60, 70, 82), 2, cv2.LINE_AA)
        cv2.putText(img, "press RUN to start camera", (150, 295),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (45, 53, 63), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
        if ok:
            return buf.tobytes()
    except Exception:
        pass
    return b""


# ============================================================
# 相机封装
# ============================================================

class Camera:
    def __init__(self) -> None:
        self.cap = None
        # 分两把锁: 状态锁只保护 cap/running (短持), 帧锁只保护画面数据 (短持)。
        # 绝不在持锁时做 cv2 I/O, 否则相机被占用时会把整个 HTTP 服务锁死。
        self._state_lock = threading.Lock()
        self._frame_lock = threading.Lock()
        self.running = False
        self._starting = False
        self.frame_jpeg: bytes | None = None
        self.frame_raw = None
        self.frame_display = None     # 带检测框的显示帧 (颜色识别开启时)
        self.index = 0
        self.width = 0
        self.height = 0
        self.fps = 0.0
        self.frame_count = 0
        self.started_at = 0.0
        self.last_error = ""
        self._thread: threading.Thread | None = None
        self.cached_props: dict = {}     # 采集线程里缓存的相机属性
        self._props_dirty = True         # 请求端标记需要刷新
        self._props_last = 0.0           # 上次刷新时间
        self.factory_defaults: dict = {} # 相机首次启动时捕获的出厂默认值
        self.defaults_captured = False
        # ---- 录像状态 (与采集循环共用同一个 cv2.VideoCapture, 不另开相机) ----
        self._record_lock = threading.Lock()
        self.recording = False
        self._record_starting = False
        self.video_writer = None
        self.record_path: Path | None = None
        self.record_started_at = 0.0
        self.record_frames = 0
        self.record_error = ""
        self.record_processing = False
        self.last_record_file = ""
        self.record_fourcc = ""

    # ---- 打开相机 (带超时保护, 且不持锁) ----

    @staticmethod
    def _open_capture(index: int):
        import cv2
        for backend, name in (
            (cv2.CAP_DSHOW, "DirectShow"),
            (cv2.CAP_MSMF, "MediaFoundation"),
            (cv2.CAP_ANY, "Auto"),
        ):
            try:
                cap = cv2.VideoCapture(index, backend)
                if cap.isOpened():
                    ok, _ = cap.read()
                    if ok:
                        return cap, name
                    cap.release()
            except Exception:
                pass
        return None, ""

    def _open_with_timeout(self, index: int, timeout: float = 8.0):
        """相机被别的进程占用时 cv2 可能卡住, 用工作线程 + 超时兜底。"""
        box: dict = {}

        def worker():
            box["ret"] = self._open_capture(index)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            return None, "", True  # 超时
        cap, name = box.get("ret", (None, ""))
        return cap, name, False

    # ---- 生命周期 ----

    def start_with_size(self, index: int = 0,
                        width: int = 0, height: int = 0) -> bool:
        """以指定分辨率启动 (用于切换分辨率, 需先 stop)。"""
        import cv2
        with self._state_lock:
            if self.running:
                self.last_error = ""
                return True
            if self._starting:
                return False
            self._starting = True
            self.last_error = ""

        # 自定义打开: 先设分辨率
        def _open():
            for backend, bname in ((cv2.CAP_DSHOW, "DirectShow"),
                                   (cv2.CAP_MSMF, "MediaFoundation"),
                                   (cv2.CAP_ANY, "Auto")):
                try:
                    cap = cv2.VideoCapture(index, backend)
                    if not cap.isOpened():
                        cap.release()
                        continue
                    if width:
                        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
                    if height:
                        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
                    ok, _ = cap.read()
                    if ok:
                        return cap, bname
                    cap.release()
                except Exception:
                    pass
            return None, ""

        box = {}
        def _worker():
            box["ret"] = _open()
        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(8.0)
        cap, name = box.get("ret", (None, ""))

        with self._state_lock:
            self._starting = False
            if cap is None:
                self.last_error = f"无法以 {width}x{height} 打开相机"
                return False
            self.cap = cap
            self.index = index
            self.running = True
            self.started_at = time.time()
            self.last_error = ""
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        with self._frame_lock:
            self.frame_jpeg = None
            self.frame_raw = None
            self.frame_display = None
            self.frame_count = 0
            self.fps = 0.0
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[OK] 相机已启动 index={index} ({name}) "
              f"{self.width}x{self.height}")
        return True

    def start(self, index: int = 0) -> bool:
        with self._state_lock:
            if self.running:
                self.last_error = ""          # 已在运行, 清掉历史错误
                return True
            if self._starting:
                # 已有启动流程在跑: 不污染 last_error, 交给调用方看 status.starting
                return False
            self._starting = True
            self.last_error = ""

        cap, name, timed_out = self._open_with_timeout(index)

        with self._state_lock:
            self._starting = False
            if timed_out:
                self.last_error = (
                    f"打开相机超时 (index={index}) — 可能被其它程序占用"
                )
                print(f"[ERR] {self.last_error}")
                return False
            if cap is None:
                self.last_error = f"无法打开相机 index={index}"
                print(f"[ERR] {self.last_error}")
                return False
            self.cap = cap
            self.index = index
            self.running = True
            self.started_at = time.time()
            self.last_error = ""              # 启动成功, 清掉历史错误
        with self._record_lock:
            self.record_error = ""            # 清掉历史录像报错, 避免误导

        import cv2
        self.width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

        with self._frame_lock:
            self.frame_jpeg = None
            self.frame_raw = None
            self.frame_count = 0
            self.fps = 0.0

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[OK] 相机已启动 index={index} ({name}) "
              f"{self.width}x{self.height}")
        return True

    def stop(self) -> None:
        # 先停录像, 保证文件被正确收尾 (释放 VideoWriter 才会写完 mp4 尾部索引)
        self.stop_record(reason="相机停止，录像自动结束")
        with self._state_lock:
            if not self.running:
                return
            self.running = False
            cap, self.cap = self.cap, None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        with self._frame_lock:
            self.frame_jpeg = None
            self.frame_raw = None
        print("[..] 相机已停止")

    # ---- 出厂默认值 ----

    def _capture_defaults(self, cap) -> None:
        """在采集线程里捕获出厂默认值 (相机刚启动、用户没改过时调用)。

        每次相机启动后只会捕获一次, 之后作为"默认值"基准。
        """
        if self.defaults_captured:
            return
        try:
            self._refresh_props(cap)
        except Exception:
            return
        if not self.cached_props:
            return
        self.factory_defaults = dict(self.cached_props)
        self.defaults_captured = True
        print(f"[CAM] 已捕获出厂默认值: {self.factory_defaults}")

    # ---- 采集循环 ----

    def _refresh_props(self, cap) -> None:
        """在采集线程里安全读取属性, 缓存起来供 UI 读取。

        注意: 部分 CAP_PROP_* 常量在 OpenCV 5.x 里不存在, 必须 getattr 兜底,
        否则 AttributeError 会把采集线程杀掉。
        """
        import cv2
        names = [
            ("FRAME_WIDTH", "CAP_PROP_FRAME_WIDTH"),
            ("FRAME_HEIGHT", "CAP_PROP_FRAME_HEIGHT"),
            ("FPS", "CAP_PROP_FPS"),
            ("BRIGHTNESS", "CAP_PROP_BRIGHTNESS"),
            ("CONTRAST", "CAP_PROP_CONTRAST"),
            ("SATURATION", "CAP_PROP_SATURATION"),
            ("HUE", "CAP_PROP_HUE"),
            ("GAIN", "CAP_PROP_GAIN"),
            ("EXPOSURE", "CAP_PROP_EXPOSURE"),
            ("AUTO_EXPOSURE", "CAP_PROP_AUTO_EXPOSURE"),
            ("WHITE_BALANCE", "CAP_PROP_WHITE_BALANCE"),
            ("SHARPNESS", "CAP_PROP_SHARPNESS"),
            ("BACKLIGHT", "CAP_PROP_BACKLIGHT_COMPENSATION"),
            ("FOCUS", "CAP_PROP_FOCUS"),
            ("AUTOFOCUS", "CAP_PROP_AUTOFOCUS"),
            ("PAN", "CAP_PROP_PAN"),
            ("TILT", "CAP_PROP_TILT"),
            ("ZOOM", "CAP_PROP_ZOOM"),
        ]
        vals = {}
        for nm, attr in names:
            pid = getattr(cv2, attr, None)
            if pid is None:
                vals[nm] = -1.0
                continue
            try:
                vals[nm] = float(cap.get(pid))
            except Exception:
                vals[nm] = -1.0
        self.cached_props = vals

    def _loop(self) -> None:
        import cv2
        t_last = time.time()
        n_window = 0
        n = 0
        while True:
            with self._state_lock:
                if not self.running or self.cap is None:
                    break
                cap = self.cap
            try:
                ok, frame = cap.read()
            except Exception:
                break
            if not ok:
                time.sleep(0.02)
                continue
            try:
                ok2, buf = cv2.imencode(".jpg", frame,
                                        [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            except Exception:
                continue
            if not ok2:
                continue

            # 注意: 不再自动叠加检测框 (改为"点击才识别", 画面保持干净)
            # 图像增强作用于显示帧 (视频流/拍照所见即所得); 录像仍写原始 frame
            display = ENH.apply(frame)

            now = time.time()
            n_window += 1
            if now - t_last >= 0.5:
                self.fps = n_window / (now - t_last)
                t_last = now
                n_window = 0
            with self._frame_lock:
                self.frame_jpeg = buf.tobytes()
                self.frame_raw = frame
                self.frame_display = display
                self.frame_count += 1

            # 录像写入: 就在采集线程里写, 复用同一帧, 不阻塞 MJPEG 输出
            with self._record_lock:
                if self.recording and self.video_writer is not None:
                    try:
                        self.video_writer.write(frame)
                        self.record_frames += 1
                    except Exception as e:
                        self.record_error = f"写入视频帧失败: {e}"
                        self.recording = False

            # 周期性缓存相机属性 (按需触发 + 最多 5 秒一次, 避免拖慢采集)
            n += 1
            if self._props_dirty and (now - self._props_last) >= 5.0:
                try:
                    self._refresh_props(cap)
                    self._props_last = now
                    self._props_dirty = False
                    # 首次拿到有效属性时, 抓取出厂默认值
                    if not self.defaults_captured and self.cached_props:
                        self.factory_defaults = dict(self.cached_props)
                        self.defaults_captured = True
                        print(f"[CAM] 已捕获出厂默认值 ({len(self.factory_defaults)} 项)")
                except Exception:
                    pass

    # ---- 对外 ----

    def status(self) -> dict:
        with self._state_lock:
            running = self.running
            starting = self._starting
            index = self.index
            width = self.width
            height = self.height
            err = self.last_error
            started = self.started_at
        with self._frame_lock:
            fps = self.fps
            frames = self.frame_count
        uptime = (time.time() - started) if running else 0.0
        data = {
            "running": running,
            "starting": starting,
            "index": index,
            "width": width,
            "height": height,
            "fps": round(fps, 1),
            "frames": frames,
            "uptime": round(uptime, 1),
            "last_error": err,
        }
        data.update(self.record_status())
        return data

    def get_jpeg(self) -> bytes | None:
        with self._frame_lock:
            return self.frame_jpeg

    def get_raw(self):
        with self._frame_lock:
            return None if self.frame_raw is None else self.frame_raw.copy()

    def get_display(self):
        """返回显示帧(带检测框), 无则退回原始帧。"""
        with self._frame_lock:
            src = self.frame_display if self.frame_display is not None else self.frame_raw
            return None if src is None else src.copy()

    def snapshot(self) -> Path | None:
        frame = self.get_display()      # "所见即所得": 保存画面里看到的(含检测框)
        if frame is None:
            return None
        import cv2
        SAVE_DIR.mkdir(exist_ok=True)
        path = SAVE_DIR / (
            f"web_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        )
        cv2.imwrite(str(path), frame)
        return path

    # ---- 录像 (复用采集循环的帧, 不另开相机实例) ----

    def start_record(self) -> bool:
        with self._record_lock:
            if self.recording:
                self.record_error = "已经在录像中"
                return False
            if self._record_starting:
                self.record_error = "正在启动录像, 请稍候"
                return False
            if self.record_processing:
                self.record_error = "上一段录像仍在生成 H.264，请稍候"
                return False
            self._record_starting = True
            self.record_error = ""

        with self._state_lock:
            running = self.running
            cap = self.cap
        if not running or cap is None:
            with self._record_lock:
                self._record_starting = False
                self.record_error = "相机未启动，请先点击 RUN 运行"
            return False

        import cv2
        SAVE_DIR.mkdir(exist_ok=True)
        name = f"video_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        path = SAVE_DIR / name
        try:
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        except Exception:
            w, h = self.width or 640, self.height or 480
        if w <= 0 or h <= 0:
            w, h = 640, 480
        fps = self.fps if self.fps >= 5 else 30.0

        writer = None
        try:
            # 浏览器只支持 H.264 (avc1), 不支持 mp4v/MPEG-4 Part 2。
            # 逐个尝试, 第一个能打开的就用; 打不开则报错而不是写一个浏览器放不了的文件。
            fourcc_used = ""
            for tag in ("avc1", "H264", "h264", "mp4v"):
                try:
                    fourcc = cv2.VideoWriter_fourcc(*tag)
                    w2 = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
                except Exception:
                    continue
                if w2.isOpened():
                    writer = w2
                    fourcc_used = tag
                    break
                try:
                    w2.release()
                except Exception:
                    pass
            if writer is None:
                with self._record_lock:
                    self._record_starting = False
                    self.record_error = (
                        f"无法创建视频文件 {name}（H.264 编码器不可用）"
                    )
                print(f"[ERR] {self.record_error}")
                return False
            if fourcc_used != "avc1":
                # 兜底编码器浏览器可能放不了, 明确提示
                print(f"[WARN] 使用了 {fourcc_used} 编码器, "
                      f"浏览器可能无法在线播放, 建议下载后观看")
        except Exception as e:
            with self._record_lock:
                self._record_starting = False
                self.record_error = f"创建录像失败: {e}"
            print(f"[ERR] {self.record_error}")
            return False

        with self._record_lock:
            self._record_starting = False
            self.video_writer = writer
            self.record_path = path
            self.record_started_at = time.time()
            self.record_frames = 0
            self.recording = True
            self.record_error = ""
            self.record_fourcc = fourcc_used
        print(f"[REC] 开始录像 -> {path}  {w}x{h} @{fps:.1f}fps")
        return True

    def stop_record(self, reason: str = "") -> bool:
        with self._record_lock:
            if not self.recording and self.video_writer is None:
                if reason:
                    return True
                self.record_error = "当前没有在录像"
                return False
            self.recording = False
            writer, self.video_writer = self.video_writer, None
            path = self.record_path
            frames = self.record_frames
            fourcc_used = self.record_fourcc
        try:
            if writer is not None:
                writer.release()
        except Exception as e:
            with self._record_lock:
                self.record_error = f"保存录像失败: {e}"
            print(f"[ERR] {self.record_error}")
            return False
        needs_transcode = path is not None and fourcc_used != "avc1"
        with self._record_lock:
            self.record_path = None
            if path is not None and path.is_file() and path.stat().st_size > 0:
                self.last_record_file = path.name
            self.record_processing = bool(needs_transcode)
            self.record_error = ""
        if needs_transcode:
            threading.Thread(target=self._finish_h264_transcode, args=(path,),
                             name="record-h264-transcode", daemon=True).start()
        msg = f"[REC] 录像已停止 -> {path.name if path else '?'} ({frames} 帧)"
        if reason:
            msg += f"  [{reason}]"
        print(msg)
        return True

    def _finish_h264_transcode(self, path: Path) -> None:
        warning = _transcode_record_h264(path)
        with self._record_lock:
            self.record_error = warning
            self.record_processing = False
        if warning:
            print(f"[WARN] {warning}")
        else:
            print(f"[REC] H.264 转码完成 -> {path.name}")

    def record_status(self) -> dict:
        with self._record_lock:
            rec = self.recording
            started = self.record_started_at
            frames = self.record_frames
            name = self.record_path.name if self.record_path else self.last_record_file
            err = self.record_error
            processing = self.record_processing
        elapsed = (time.time() - started) if rec and started else 0.0
        return {
            "recording": rec,
            "record_elapsed": round(elapsed, 1),
            "record_frames": frames,
            "record_file": name,
            "record_error": err,
            "record_processing": processing,
        }


def _find_ffmpeg() -> str | None:
    """Locate an H.264-capable FFmpeg without changing PATH or system settings."""
    import os
    configured = os.environ.get("ORBBEC_FFMPEG", "").strip().strip('"')
    candidates = [configured, shutil.which("ffmpeg")]
    candidates.append(str(Path(__file__).resolve().parent / "ffmpeg.exe"))
    candidates.append(str(Path.home() / "Open-Industry-Project" /
                          "node_modules" / "ffmpeg-static" / "ffmpeg.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _transcode_record_h264(path: Path) -> str:
    """Safely convert an OpenCV fallback recording to browser-friendly H.264.

    The original remains intact unless FFmpeg succeeds and the converted file
    can be decoded. Returns an empty string on success or an actionable warning.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return "录像文件为空，未执行 H.264 转码"
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return "当前录像采用非 H.264 编码；未找到 FFmpeg，原文件已保留。可设置 ORBBEC_FFMPEG。"
    temporary = path.with_name(f"{path.stem}.h264-tmp.mp4")
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path),
             "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "22", "-movflags", "+faststart", str(temporary)],
            capture_output=True, text=True, timeout=120, check=False,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            detail = (result.stderr or "").strip().splitlines()
            why = detail[-1][:240] if detail else f"FFmpeg 退出码 {result.returncode}"
            return f"H.264 转码失败，原录像已保留：{why}"
        import cv2
        reader = cv2.VideoCapture(str(temporary))
        try:
            ok, frame = reader.read()
        finally:
            reader.release()
        if not ok or frame is None:
            return "H.264 转码结果无法解码，原录像已保留"
        temporary.replace(path)
        return ""
    except subprocess.TimeoutExpired:
        return "H.264 转码超过 120 秒，原录像已保留"
    except Exception as exc:
        return f"H.264 转码异常，原录像已保留：{exc}"
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


CAM = Camera()
PLACEHOLDER_JPEG = _make_placeholder_jpeg()


def _current_frame():
    """取当前帧: 优先 cv2 RGB; 深度模式下 cv2 已停, 用 Orbbec 彩色帧(与深度同坐标)。"""
    frame = CAM.get_raw()
    if frame is None and DEPTH is not None and DEPTH_MODE.get("active"):
        try:
            frame = DEPTH.get_color()
        except Exception:
            frame = None
    return frame


# ============================================================
# 图像增强 (作用于显示帧: 视频流/拍照"所见即所得"; 录像仍存原始帧)
# ============================================================

class ImageEnhancer:
    """实时图像增强: 锐化 / 去噪 / 直方图均衡 / 边缘检测, 可组合。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.enabled = False
        self.sharpen = 0        # 0-100 锐化强度
        self.denoise = 0        # 0-100 去噪强度
        self.equalize = False   # 直方图均衡 (CLAHE)
        self.edge = False       # 边缘检测叠加

    def apply(self, frame):
        import cv2
        import numpy as np
        with self.lock:
            if not self.enabled:
                return frame
            sh, dn = self.sharpen, self.denoise
            eq, ed = self.equalize, self.edge
        out = frame
        try:
            # 去噪 (双边滤波, 保边)
            if dn > 0:
                d = 3 + int(dn / 25)
                sc = 20 + int(dn * 1.5)
                out = cv2.bilateralFilter(out, d, sc, sc)
            # 直方图均衡 (CLAHE, 仅 L 通道, 不偏色)
            if eq:
                lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
                clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
                lab[:, :, 0] = clahe.apply(lab[:, :, 0])
                out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            # 锐化 (unsharp mask)
            if sh > 0:
                blur = cv2.GaussianBlur(out, (0, 0), 2.0)
                amt = 0.4 + (sh / 100.0) * 1.6
                out = cv2.addWeighted(out, 1.0 + amt, blur, -amt, 0)
            # 边缘检测叠加 (青黄色高亮)
            if ed:
                gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
                edges = cv2.Canny(gray, 60, 160)
                mask = edges > 0
                out = out.copy()
                out[mask] = (out[mask] * 0.4 +
                             np.array([220, 180, 40], dtype=np.float32) * 0.6
                             ).astype(np.uint8)
        except Exception:
            return frame
        return out

    def settings(self) -> dict:
        with self.lock:
            return {"enabled": self.enabled, "sharpen": self.sharpen,
                    "denoise": self.denoise, "equalize": self.equalize,
                    "edge": self.edge}

    def configure(self, data: dict) -> dict:
        with self.lock:
            if "enabled" in data:
                self.enabled = bool(data["enabled"])
            if "sharpen" in data:
                self.sharpen = max(0, min(100, int(data["sharpen"])))
            if "denoise" in data:
                self.denoise = max(0, min(100, int(data["denoise"])))
            if "equalize" in data:
                self.equalize = bool(data["equalize"])
            if "edge" in data:
                self.edge = bool(data["edge"])
            # 持锁时直接构造返回, 不调 settings() (避免重入死锁)
            return {"ok": True, "enabled": self.enabled,
                    "sharpen": self.sharpen, "denoise": self.denoise,
                    "equalize": self.equalize, "edge": self.edge}


ENH = ImageEnhancer()


# ============================================================
# 定时抓拍 (后台线程按间隔保存快照到 SAVE_DIR)
# ============================================================

class TimeLapser:
    """定时抓拍: 按间隔保存快照(所见即所得, 含增强)到 captures/。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.running = False
        self.interval = 5.0     # 秒
        self.count = 0          # 0=无限
        self.saved = 0
        self.last_file = ""
        self.error = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, interval: float = 5.0, count: int = 0) -> dict:
        with self.lock:
            if self.running:
                return {"ok": False, "error": "定时抓拍已在运行"}
            self.interval = max(1.0, float(interval))
            self.count = max(0, int(count))
            self.saved = 0
            self.error = ""
            self._stop.clear()
            self.running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            # 持锁时直接构造返回, 不调 status() (避免重入死锁)
            return {"ok": True, "running": True, "interval": self.interval,
                    "count": self.count, "saved": self.saved}

    def stop(self) -> dict:
        self._stop.set()
        with self.lock:
            self.running = False
        return {"ok": True, "saved": self.saved}

    def _loop(self) -> None:
        import cv2
        while not self._stop.is_set():
            try:
                frame = CAM.get_display()   # 所见即所得(含增强)
                if frame is not None:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    fn = f"timelapse_{ts}.jpg"
                    cv2.imwrite(str(SAVE_DIR / fn), frame)
                    with self.lock:
                        self.saved += 1
                        self.last_file = fn
                    if self.count and self.saved >= self.count:
                        self._stop.set()
                        with self.lock:
                            self.running = False
                        break
            except Exception as e:
                with self.lock:
                    self.error = str(e)
            self._stop.wait(self.interval)

    def status(self) -> dict:
        with self.lock:
            return {"running": self.running, "interval": self.interval,
                    "count": self.count, "saved": self.saved,
                    "last_file": self.last_file, "error": self.error}


TIME = TimeLapser()


# ============================================================
# 文件管理 (只允许访问 SAVE_DIR 内的文件)
# ============================================================

def _safe_file(name: str) -> Path | None:
    """把用户传来的文件名限制在 SAVE_DIR 内, 拒绝路径穿越。"""
    if not name:
        return None
    name = unquote(name)
    if any(c in name for c in ("/", "\\", "\x00")) or ".." in name:
        return None
    try:
        p = (SAVE_DIR / name).resolve()
        base = SAVE_DIR.resolve()
        if not str(p).startswith(str(base) + "\\") and p != base:
            return None
    except Exception:
        return None
    return p if p.is_file() else None


def _list_files() -> list[dict]:
    SAVE_DIR.mkdir(exist_ok=True)
    items: list[dict] = []
    for p in SAVE_DIR.iterdir():
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in IMAGE_EXTS:
            kind = "image"
        elif ext in VIDEO_EXTS:
            kind = "video"
        else:
            continue  # 只列出截图和录像, 不暴露其它文件
        try:
            st = p.stat()
        except OSError:
            continue
        items.append({
            "name": p.name,
            "kind": kind,
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mtime_str": dt.datetime.fromtimestamp(st.st_mtime)
                .strftime("%Y-%m-%d %H:%M:%S"),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


def _human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{n} B"


# ---- 中文绘制 (OpenCV putText 不支持中文, 用 Pillow) ----
_PIL_FONT = None
_PIL_FONT_TRIED = False


def _pil_font(size: int = 16):
    global _PIL_FONT, _PIL_FONT_TRIED
    if _PIL_FONT_TRIED:
        return _PIL_FONT
    _PIL_FONT_TRIED = True
    try:
        from PIL import ImageFont
        for p in (r"C:\Windows\Fonts\msyh.ttc",
                  r"C:\Windows\Fonts\simhei.ttf",
                  r"C:\Windows\Fonts\simsun.ttc"):
            try:
                if Path(p).is_file():
                    _PIL_FONT = ImageFont.truetype(p, size)
                    return _PIL_FONT
            except Exception:
                continue
    except Exception:
        pass
    return None


def put_text_cn(img, text: str, org, color, size: int = 16) -> None:
    """在 BGR ndarray 上画中文。Pillow 不可用时退回 cv2 的 ASCII。"""
    global _PIL_FONT
    import cv2
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        if _pil_font(size) is None:
            raise RuntimeError("no font")
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        draw = ImageDraw.Draw(pil)
        # 每次按 size 取字体 (缓存最后一种 size)
        f = _PIL_FONT
        if getattr(f, "size", size) != size:
            f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", size)
            _PIL_FONT = f
        b, g, r = color[0], color[1], color[2]
        draw.text(org, text, font=f, fill=(r, g, b))
        out = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
        img[:, :] = out
    except Exception:
        # 兜底: 退回 ASCII (中文会变 ?, 但不影响其它绘制)
        cv2.putText(img, text, org, cv2.FONT_HERSHEY_SIMPLEX,
                    size / 20.0, color, 1, cv2.LINE_AA)


# ============================================================
# 颜色识别 (HSV 阈值分割 · 无需数据集/无需训练)
# ============================================================

class ColorDetector:
    """HSV 颜色分割识别器。

    - 独立线程运行 (约 10Hz), 不阻塞 MJPEG 采集/输出
    - 只缓存检测结果(框/标签/统计), 由采集线程轻量绘制到显示帧上
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.enabled = False
        self.error = ""
        self.sensitivity = 50          # 0-100, 越高越宽松
        self.min_area = 600            # 最小连通域像素数
        self.max_area_ratio = 0.85     # 连通域占画面比例上限(滤掉整幅背景)
        self.detections: list[dict] = []
        self.stats: dict = {}
        self.analyzed_at = 0.0
        self.analyzed_fps = 0.0
        self.last_annotated: bytes | None = None   # 最近一次识别的标注图 JPEG
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- 灵敏度映射 ----

    def _thresholds(self) -> tuple[int, int]:
        """由灵敏度推出 S/V 下限: 灵敏度越高, 阈值越低, 检出越多。"""
        s = max(0, min(100, int(self.sensitivity)))
        s_min = max(15, 105 - int(s * 0.9))    # sens50→60  sens80→33
        v_min = max(15, 95 - int(s * 0.75))    # sens50→58  sens80→35
        return s_min, v_min

    # ---- 核心检测: 按连通域平均 HSV 自动分类, 覆盖所有颜色 ----

    def analyze(self, frame):
        """返回 (detections, stats)。detections 按面积降序。"""
        import cv2
        import numpy as np

        # 亮度归一化: 让不同光照下的颜色判定更稳
        try:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            frame_n = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        except Exception:
            frame_n = frame

        hsv = cv2.cvtColor(frame_n, cv2.COLOR_BGR2HSV)
        s_min, v_min = self._thresholds()
        s_ch, v_ch = hsv[:, :, 1], hsv[:, :, 2]
        img_area = frame.shape[0] * frame.shape[1]
        with self.lock:
            min_area = self.min_area
            max_area = int(img_area * self.max_area_ratio)

        # 三块区域: 暗无彩(黑) / 亮无彩(灰白) / 有彩 —— 暗亮分开,
        # 避免灰物体和深色背景连成一块被整块滤掉
        achro = s_ch < max(32, s_min)
        dark_mask = (achro & (v_ch < 60)).astype(np.uint8) * 255
        light_mask = (achro & (v_ch >= 60)).astype(np.uint8) * 255
        chroma_mask = ((~achro) & (v_ch >= v_min)).astype(np.uint8) * 255

        detections: list[dict] = []
        stats: dict = {}
        # 用大核做形态学, 把碎块合并成整块 (减少噪点)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))

        for mask in (dark_mask, light_mask, chroma_mask):
            self._collect(mask, hsv, frame_n, kernel, min_area, max_area,
                          detections, stats)

        # 同色相邻区域合并
        detections = self._merge_same_color(detections)
        detections.sort(key=lambda d: d["area"], reverse=True)
        return detections[:30], stats

    @staticmethod
    def _collect(mask, hsv, bgr_img, kernel, min_area, max_area,
                 detections, stats):
        import cv2
        import numpy as np
        cleaned = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = int(cv2.contourArea(c))
            if area < min_area or area > max_area:
                continue
            m = cv2.moments(c)
            if m["m00"] == 0:
                continue
            cx = int(m["m10"] / m["m00"])
            cy = int(m["m01"] / m["m00"])
            x, y, w, h = cv2.boundingRect(c)

            # 用连通域的平均 HSV 做分类 (比固定阈值准得多)
            region = np.zeros(mask.shape, np.uint8)
            cv2.drawContours(region, [c], -1, 255, -1)
            mean = cv2.mean(hsv, mask=region)
            mh, ms, mv = int(mean[0]), int(mean[1]), int(mean[2])
            # 平均 BGR → 供 Lab 最近邻分类 (比纯 HSV 色相准)
            mb = cv2.mean(bgr_img, mask=region)
            mean_bgr = (mb[0], mb[1], mb[2])

            name, conf = classify_color(mh, ms, mv, bgr=mean_bgr)
            color_bgr = HUE_DRAW.get(name, (200, 200, 200))

            detections.append({
                "color": name,
                "confidence": conf,
                "area": area,
                "cx": cx, "cy": cy,
                "bbox": [x, y, w, h],
                "hsv": [mh, ms, mv],
                "bgr": [int(mean_bgr[0]), int(mean_bgr[1]), int(mean_bgr[2])],
                "color_bgr": list(color_bgr),
            })
            stats[name] = stats.get(name, 0) + area

    # ---- 绘制 (由采集线程调用, 要快) ----

    @staticmethod
    def _merge_same_color(dets):
        """合并颜色相同且相邻的碎块, 减少噪点、提升区域准确度。"""
        import math
        merged = []
        used = [False] * len(dets)
        for i, a in enumerate(dets):
            if used[i]:
                continue
            group = [a]
            used[i] = True
            ax, ay, aw, ah = a["bbox"]
            a_cx, a_cy = a["cx"], a["cy"]
            for j in range(i + 1, len(dets)):
                if used[j]:
                    continue
                b = dets[j]
                if b["color"] != a["color"]:
                    continue
                bx, by, bw, bh = b["bbox"]
                # bbox 扩 20% 后相交判定
                if (ax - 20 < bx + bw and bx < ax + aw + 20 and
                        ay - 20 < by + bh and by < ay + ah + 20):
                    group.append(b)
                    used[j] = True
            if len(group) == 1:
                merged.append(a)
                continue
            # 合并 bbox 和面积
            x0 = min(g["bbox"][0] for g in group)
            y0 = min(g["bbox"][1] for g in group)
            x1 = max(g["bbox"][0] + g["bbox"][2] for g in group)
            y1 = max(g["bbox"][1] + g["bbox"][3] for g in group)
            area = sum(g["area"] for g in group)
            cx = int(sum(g["cx"] * g["area"] for g in group) / max(1, area))
            cy = int(sum(g["cy"] * g["area"] for g in group) / max(1, area))
            conf = sum(g.get("confidence", 0) for g in group) / len(group)
            merged.append({
                "color": a["color"],
                "confidence": round(conf, 2),
                "area": area,
                "cx": cx, "cy": cy,
                "bbox": [x0, y0, x1 - x0, y1 - y0],
                "hsv": a.get("hsv", [0, 0, 0]),
                "color_bgr": a.get("color_bgr", [200, 200, 200]),
            })
        return merged

    def draw(self, frame, detections: list[dict]) -> None:
        import cv2
        ih, iw = frame.shape[:2]
        font_size = 16
        try:
            from PIL import ImageFont
            _f = _pil_font(font_size)
        except Exception:
            _f = None

        sw = 11          # 标签里的色块小方标
        # 先算标签尺寸
        items = []
        for d in detections:
            x, y, w, h = d["bbox"]
            label = f"{d['color']} {d['confidence']:.2f}"
            tw = int(_f.getlength(label)) if _f is not None else len(label) * 9
            th = font_size + 5
            box_w = sw + 5 + tw + 7
            items.append((d, label, x, y, w, h, tw, th, box_w))

        # 先画框
        for d, _l, x, y, w, h, _tw, _th, _bw in items:
            c = tuple(d["color_bgr"])
            cv2.rectangle(frame, (x, y), (x + w, y + h), c, 2)

        # 再画标签: 框够大就放框内左上, 否则放框上方(带避让)
        placed: list[tuple[int, int, int, int]] = []
        for d, label, x, y, w, h, tw, th, box_w in items:
            c = tuple(d["color_bgr"])
            if w >= box_w + 8 and h >= th + 8:
                # 放框内左上
                tx, ty = x + 4, y + 4
            else:
                # 放框上方, 空间不够则下方/内侧, 并做重叠避让
                ty = y - th - 5
                if ty < 2:
                    ty = y + h + 3
                if ty + th > ih - 2:
                    ty = max(2, y + 2)
                tx = max(2, min(x, iw - box_w - 4))
                for _ in range(6):
                    hit = False
                    for (x0, y0, x1, y1) in placed:
                        if not (tx + box_w + 2 <= x0 or x1 <= tx - 2 or
                                ty + th <= y0 or y1 <= ty - 3):
                            hit = True
                            break
                    if not hit:
                        break
                    ty += th + 2
                    if ty + th > ih - 2:
                        ty = max(2, y + 2)
                        break
                placed.append((tx - 2, ty - 3, tx + box_w, ty + th))
            cv2.rectangle(frame, (tx - 3, ty - 3), (tx + box_w - 1, ty + th - 2),
                          (18, 22, 28), -1)
            cv2.rectangle(frame, (tx, ty + 3), (tx + sw, ty + th - 5), c, -1)
            put_text_cn(frame, label, (tx + sw + 5, ty),
                        (235, 235, 235), font_size)

        # 左下角状态条
        if detections:
            s_min, v_min = self._thresholds()
            txt = f"颜色识别 ON   S>={s_min}  V>={v_min}   检出 {len(detections)} 个"
            cv2.rectangle(frame, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
            put_text_cn(frame, txt, (8, ih - 20), (61, 220, 132), 16)

    # ---- 后台线程 ----

    def start(self, camera) -> bool:
        with self.lock:
            if self.enabled:
                return True
            self.enabled = True
            self.error = ""
            self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(camera,), daemon=True)
        self._thread.start()
        print("[COLOR] 颜色识别已开启")
        return True

    def stop(self) -> bool:
        with self.lock:
            if not self.enabled:
                return True
            self.enabled = False
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        with self.lock:
            self.detections = []
            self.stats = {}
        print("[COLOR] 颜色识别已关闭")
        return True

    def _loop(self, camera) -> None:
        t_last = time.time()
        n = 0
        while not self._stop.is_set():
            frame = camera.get_raw()
            if frame is None:
                time.sleep(0.15)
                continue
            try:
                dets, stats = self.analyze(frame)
            except Exception as e:
                with self.lock:
                    self.error = f"识别失败: {e}"
                time.sleep(0.3)
                continue
            now = time.time()
            n += 1
            if now - t_last >= 1.0:
                self.analyzed_fps = n / (now - t_last)
                t_last = now
                n = 0
            with self.lock:
                self.detections = dets
                self.stats = stats
                self.analyzed_at = now
                self.error = ""
            time.sleep(0.1)      # ~10Hz, 远快于 30fps 采集, 不会拖慢视频

    # ---- 对外 ----

    def detect_once(self) -> dict:
        """对当前帧做一次识别(不进线程), 生成标注图并返回结果。

        这是「点击才识别」的入口 —— 不持续叠加, 只在调用时算一次。
        """
        import cv2
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        try:
            dets, stats = self.analyze(frame)
        except Exception as e:
            return {"ok": False, "error": f"识别失败: {e}"}
        annotated = frame.copy()
        if dets:
            self.draw(annotated, dets)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None
        with self.lock:
            self.detections = dets
            self.stats = stats
            self.analyzed_at = time.time()
        s_min, v_min = self._thresholds()
        return {
            "ok": True,
            "count": len(dets),
            "stats": stats,
            "thresholds": {"s_min": s_min, "v_min": v_min},
            "sensitivity": self.sensitivity,
            "detections": dets,
        }

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def get(self) -> tuple[list[dict], dict]:
        with self.lock:
            return list(self.detections), dict(self.stats)

    def is_enabled(self) -> bool:
        with self.lock:
            return self.enabled

    def status(self) -> dict:
        with self.lock:
            dets = list(self.detections)
            stats = dict(self.stats)
            err = self.error
            enabled = self.enabled
            sens = self.sensitivity
            min_area = self.min_area
            fps = self.analyzed_fps
            at = self.analyzed_at
        s_min, v_min = self._thresholds()
        return {
            "color_enabled": enabled,
            "color_sensitivity": sens,
            "color_min_area": min_area,
            "color_thresholds": {"s_min": s_min, "v_min": v_min},
            "color_count": len(dets),
            "color_stats": stats,
            "color_fps": round(fps, 1),
            "color_error": err,
            "color_age": round(time.time() - at, 2) if at else None,
            "detections": dets,
        }

    def pick(self, x: int, y: int, roi: int = 10):
        """取画面某点(周围 roi×roi 平均)的 HSV 与中文颜色名 + 真实置信度。

        roi 默认 10 (更大采样区抗噪), 并做亮度归一化让判定更稳。
        """
        import cv2
        import numpy as np
        frame = _current_frame()
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            return {"error": f"坐标超出画面范围 ({w}x{h})"}
        x0, x1 = max(0, x - roi), min(w, x + roi + 1)
        y0, y1 = max(0, y - roi), min(h, y + roi + 1)
        patch = frame[y0:y1, x0:x1]
        # 色名分类使用未增强的原始 BGR，避免 CLAHE 改变色相/色度。
        bgr_med_raw = np.median(patch.reshape(-1, 3), axis=0)
        # 亮度归一化
        try:
            lab = cv2.cvtColor(patch, cv2.COLOR_BGR2LAB)
            clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            patch = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        except Exception:
            pass
        # 取中位数而非均值, 抗离群像素
        bgr_med = np.median(patch.reshape(-1, 3), axis=0)
        hsv_one = cv2.cvtColor(np.uint8([[bgr_med]]), cv2.COLOR_BGR2HSV)[0][0]
        hh, ss, vv = [int(v) for v in hsv_one]

        name, conf = classify_color(
            hh, ss, vv, tuple(int(c) for c in bgr_med_raw))
        # 用原始像素的均值给 hex (更真实)
        bgr_mean = frame[y0:y1, x0:x1].reshape(-1, 3).mean(axis=0)
        hex_code = "#%02X%02X%02X" % (
            int(bgr_mean[2]), int(bgr_mean[1]), int(bgr_mean[0]))
        return {
            "x": x, "y": y,
            "bgr": [int(v) for v in bgr_mean],
            "hex": hex_code,
            "hsv": [hh, ss, vv],
            "color": name,
            "confidence": conf,
        }


COLOR = ColorDetector()


# ============================================================
# 文字识别 (RapidOCR · 字母/数字/中文, 点击才识别)
# ============================================================

class OcrEngine:
    """字母/数字/中文文字识别。

    - 基于 RapidOCR (onnxruntime CPU), 点击触发, 不持续运行
    - 首次调用需加载模型 (~3-5 秒), 之后每次约 1 秒
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._engine = None
        self._loading = False
        self.last_annotated: bytes | None = None
        self.last_results: list[dict] = []
        self.elapsed_ms = 0
        self.error = ""
        self.last_variant = ""

    def _ensure(self):
        """懒加载 RapidOCR 引擎 (开角度分类 + 提分辨率 + 多线程)。"""
        if self._engine is not None:
            return self._engine
        with self.lock:
            if self._engine is not None:
                return self._engine
            if self._loading:
                return None
            self._loading = True
        try:
            from rapidocr_onnxruntime import RapidOCR
            eng = RapidOCR(
                use_cls=True,              # 开角度分类, 处理倒置/倾斜文字
                limit_side_len=960,        # 提高到 960, 小字/密集文字识别率更高
                intra_op_num_threads=4,
                inter_op_num_threads=2,
            )
            with self.lock:
                self._engine = eng
                self._loading = False
            print("[OCR] RapidOCR 引擎已加载 (字母/数字/中文 · 含角度分类)")
            return eng
        except Exception as e:
            with self.lock:
                self._loading = False
                self.error = f"OCR 引擎加载失败: {e}"
            print(f"[ERR] {self.error}")
            return None

    @staticmethod
    def _is_alnum(text: str) -> bool:
        """只保留包含字母或数字的文本块。"""
        return any(c.isalnum() for c in text)

    @staticmethod
    def _fix_oi(text: str) -> str:
        """保守修正 O/0、I/1 在明显语境下的混淆。

        仅当字母明显占优且数字只有'0'(或数字占优且字母只有'O')时才换, 避免误改纯数字。
        """
        out = []
        for tok in text.split(" "):
            if not tok:
                out.append(tok)
                continue
            letters = sum(c.isalpha() for c in tok)
            digits = sum(c.isdigit() for c in tok)
            if letters > 0 and digits == tok.count("0") and letters >= digits:
                # 如 "HELL0": 字母占优、数字仅'0' → 0 当 O
                tok = tok.replace("0", "O")
            elif digits > 0 and letters == tok.count("O") + tok.count("o") \
                    and digits >= letters:
                # 如 "C0DE" 里数字占优的情况较少, 仅 'O'→'0'
                tok = tok.replace("O", "0").replace("o", "0")
            out.append(tok)
        return " ".join(out)

    @staticmethod
    def _variants(frame):
        """生成多种预处理变体, 提升不同场景下的识别率。"""
        import cv2
        outs = [("原图", frame)]
        h, w = frame.shape[:2]
        # 放大 2x (小字关键)
        if max(w, h) * 2 <= 3000:
            outs.append(("放大2x", cv2.resize(frame, None, fx=2, fy=2,
                                             interpolation=cv2.INTER_CUBIC)))
        # CLAHE 对比度增强 (暗光/低对比场景)
        try:
            lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
            clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
            lab[:, :, 0] = clahe.apply(lab[:, :, 0])
            outs.append(("对比度增强", cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)))
        except Exception:
            pass
        # 锐化
        blur = cv2.GaussianBlur(frame, (0, 0), 2.5)
        sharp = cv2.addWeighted(frame, 1.8, blur, -0.8, 0)
        outs.append(("锐化", sharp))
        # 灰度 + CLAHE (对纯黑白数字效果好)
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            gray2 = clahe.apply(gray)
            outs.append(("灰度增强", cv2.cvtColor(gray2, cv2.COLOR_GRAY2BGR)))
        except Exception:
            pass
        return outs

    def detect_once(self, only_alnum: bool = False) -> dict:
        """对当前帧做一次文字识别 (点击触发, 不持续)。

        多种预处理并行尝试, 选识别段数最多/置信度最高的结果。
        """
        import cv2
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        if self._engine is None and self._loading:
            return {"ok": False, "error": "OCR 模型正在首次加载，请稍候再试"}
        eng = self._ensure()
        if eng is None:
            return {"ok": False,
                    "error": self.error or "OCR 引擎不可用，请确认已安装 rapidocr-onnxruntime"}

        t0 = time.time()
        merged: list[dict] = []      # 合并所有变体的检出 (取并集)
        tried = []

        def _overlap(a, b):
            ax, ay, aw, ah = a; bx, by, bw, bh = b
            ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
            iy = max(0, min(ay + ah, by + bh) - max(ay, by))
            inter = ix * iy
            if inter <= 0:
                return 0.0
            return inter / float(min(aw * ah, bw * bh) + 1e-6)

        def _norm(t):
            return t.replace(" ", "").replace("　", "").upper()

        def _merge_item(new):
            # 归一化文本相同 + 框重叠 → 合并(去重), 优先保留含空格/高置信者
            n_new = _norm(new["text"])
            for it in merged:
                if _norm(it["text"]) == n_new and _overlap(it["bbox"], new["bbox"]) > 0.3:
                    if (new["confidence"] > it["confidence"] or
                            (" " in new["text"] and " " not in it["text"])):
                        it.update(new)
                    return
            merged.append(new)

        try:
            for name, img in self._variants(frame):
                tried.append(name)
                try:
                    result, _ = eng(img)
                except Exception:
                    continue
                sx = frame.shape[1] / img.shape[1]
                sy = frame.shape[0] / img.shape[0]
                for box, text, score in (result or []):
                    if not text:
                        continue
                    text = self._fix_oi(text)
                    xs = [p[0] * sx for p in box]
                    ys = [p[1] * sy for p in box]
                    x0, y0 = int(min(xs)), int(min(ys))
                    x1, y1 = int(max(xs)), int(max(ys))
                    _merge_item({
                        "text": text,
                        "confidence": round(float(score), 3),
                        "bbox": [x0, y0, x1 - x0, y1 - y0],
                        "is_alnum": self._is_alnum(text),
                    })
                # 至少跑满 2 个变体(原图+放大)后, 已得高置信结果才提前收尾
                if (merged and len(tried) >= 2 and
                        max(i["confidence"] for i in merged) >= 0.85):
                    break
        except Exception as e:
            return {"ok": False, "error": f"OCR 识别失败: {e}"}
        self.elapsed_ms = int((time.time() - t0) * 1000)
        self.last_variant = "合并 " + "+".join(tried[:6])

        # 合并后按面积/置信度排序, 面积大的(整行文字)优先
        merged.sort(key=lambda d: (d["confidence"], d["bbox"][2] * d["bbox"][3]),
                    reverse=True)
        items = merged
        shown = [it for it in items if it["is_alnum"]] if only_alnum else items

        # 标注图 (画框 + 文字)
        annotated = frame.copy()
        for it in shown:
            x, y, w, h = it["bbox"]
            c = (0, 220, 120) if it["is_alnum"] else (220, 180, 60)
            cv2.rectangle(annotated, (x, y), (x + w, y + h), c, 2)
            label = f"{it['text']} {it['confidence']:.2f}"
            put_text_cn(annotated, label, (x + 2, max(2, y - 20)), c, 16)
        # 底部状态条
        ih, iw = annotated.shape[:2]
        txt = (f"文字识别 ON   耗时 {self.elapsed_ms} ms   "
               f"检出 {len(shown)} 段" + ("  [仅字母/数字]" if only_alnum else ""))
        cv2.rectangle(annotated, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
        put_text_cn(annotated, txt, (8, ih - 20), (0, 220, 120), 16)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None

        with self.lock:
            self.last_results = shown

        return {
            "ok": True,
            "count": len(shown),
            "total": len(items),
            "elapsed_ms": self.elapsed_ms,
            "only_alnum": only_alnum,
            "variant": self.last_variant,
            "results": shown,
        }

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def status(self) -> dict:
        with self.lock:
            return {
                "ocr_ready": self._engine is not None,
                "ocr_loading": self._loading,
                "ocr_error": self.error,
                "ocr_last_count": len(self.last_results),
                "ocr_last_ms": self.elapsed_ms,
            }


OCR = OcrEngine()


# ============================================================
# 二维码识别 (OpenCV QRCodeDetector · 零依赖, 点击才识别)
# ============================================================

class QrEngine:
    """二维码识别 (支持一张图内多个码)。

    - 基于 OpenCV 内置 QRCodeDetector, 零额外依赖
    - 点击触发, 不持续运行
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._dets = None
        self.last_annotated: bytes | None = None
        self.last_results: list[dict] = []
        self.elapsed_ms = 0

    def _detectors(self):
        """返回 (名称, 检测器) 列表。plain 最稳为主, WeChat 对小/糊强, Aruco 兜底。

        注意: QRCodeDetectorAruco 对小码常漏检, 不能做首选。
        """
        import cv2
        if getattr(self, "_dets", None) is not None:
            return self._dets
        dets = []
        try:
            dets.append(("qr", cv2.QRCodeDetector()))
        except Exception:
            pass
        try:
            if hasattr(cv2, "wechat_qrcode_WeChatQRCode"):
                dets.append(("wechat", cv2.wechat_qrcode_WeChatQRCode()))
        except Exception:
            pass
        try:
            if hasattr(cv2, "QRCodeDetectorAruco"):
                dets.append(("aruco", cv2.QRCodeDetectorAruco()))
        except Exception:
            pass
        if not dets:
            dets.append(("qr", cv2.QRCodeDetector()))
        self._dets = dets
        return dets

    @staticmethod
    def _variants(img):
        """多尺度 + 二值化预处理, 提升小/远/模糊/低对比码的检出率。"""
        import cv2
        outs = [("raw", img)]
        h, w = img.shape[:2]
        # 放大 (小码关键): 2x / 3x 立方插值
        for s in (2.0, 3.0):
            if max(w, h) * s <= 3600:
                outs.append((f"up{s:.0f}", cv2.resize(
                    img, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)))
        # 锐化
        blur = cv2.GaussianBlur(img, (0, 0), 3)
        outs.append(("sharp", cv2.addWeighted(img, 1.6, blur, -0.6, 0)))
        # 放大后再自适应二值化 (对低对比/光照不均有用; 小图直接二值化会毁码)
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        big = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        try:
            bi = cv2.adaptiveThreshold(big, 255,
                                       cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 31, 2)
            outs.append(("up2bin", cv2.cvtColor(bi, cv2.COLOR_GRAY2BGR)))
        except Exception:
            pass
        # Otsu 二值化
        try:
            _, ot = cv2.threshold(big, 0, 255,
                                  cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            outs.append(("up2otsu", cv2.cvtColor(ot, cv2.COLOR_GRAY2BGR)))
        except Exception:
            pass
        return outs

    def detect_once(self) -> dict:
        import cv2
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        dets = self._detectors()
        t0 = time.time()
        items: list[dict] = []
        found_texts = set()

        def _bbox_from_pts(box, img_shape, frame_shape):
            sx = frame_shape[1] / img_shape[1]
            sy = frame_shape[0] / img_shape[0]
            xs = [p[0] * sx for p in box]
            ys = [p[1] * sy for p in box]
            return [int(min(xs)), int(min(ys)),
                    int(max(xs) - min(xs)), int(max(ys) - min(ys))]

        def _add(txt, box, img_shape):
            if not txt or txt in found_texts:
                return
            found_texts.add(txt)
            if box is not None and len(box) >= 4:
                bbox = _bbox_from_pts(box, img_shape, frame.shape)
            else:
                bbox = [0, 0, frame.shape[1], frame.shape[0]]
            items.append({"text": txt, "type": self._guess_type(txt),
                          "bbox": bbox})

        # 遍历所有检测器 × 所有预处理变体, 取并集 (recall 最大化)
        variants = self._variants(frame)
        for dname, det in dets:
            for vname, img in variants:
                try:
                    # 多码
                    try:
                        multi = det.detectAndDecodeMulti(img)
                        ok, texts, pts, _ = multi
                        if ok and texts:
                            for i, txt in enumerate(texts):
                                box = pts[i] if pts is not None and i < len(pts) else None
                                _add(txt, box, img.shape)
                    except Exception:
                        pass
                    # 单码
                    try:
                        r = det.detectAndDecode(img)
                        txt = r[0]
                        if isinstance(txt, (list, tuple)):
                            txt = txt[0] if txt else ""
                        pts = r[1] if len(r) > 1 else None
                        if txt:
                            box = None
                            try:
                                if pts is not None:
                                    import numpy as _np
                                    arr = _np.array(pts).reshape(-1, 2)
                                    if arr.shape[0] >= 4:
                                        box = [(float(a), float(b))
                                               for a, b in arr[:4]]
                            except Exception:
                                box = None
                            _add(txt, box, img.shape)
                    except Exception:
                        pass
                except Exception:
                    pass
                # 继续扫描剩余检测器和预处理变体，补齐多码图中的小码/模糊码；
                # _add 会按解码文本去重。
        self.elapsed_ms = int((time.time() - t0) * 1000)

        # 标注图
        annotated = frame.copy()
        for it in items:
            x, y, w, h = it["bbox"]
            c = (0, 220, 120)
            cv2.rectangle(annotated, (x, y), (x + w, y + h), c, 2)
            label = it["text"]
            if len(label) > 28:
                label = label[:28] + "…"
            put_text_cn(annotated, label, (x + 2, max(2, y - 20)), c, 15)
        ih, iw = annotated.shape[:2]
        txt = f"二维码识别 ON   耗时 {self.elapsed_ms} ms   检出 {len(items)} 个"
        cv2.rectangle(annotated, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
        put_text_cn(annotated, txt, (8, ih - 20), (0, 220, 120), 16)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None

        with self.lock:
            self.last_results = items

        return {
            "ok": True,
            "count": len(items),
            "elapsed_ms": self.elapsed_ms,
            "results": items,
        }

    @staticmethod
    def _guess_type(text: str) -> str:
        t = text.strip().lower()
        if t.startswith(("http://", "https://", "www.")):
            return "网址"
        if t.startswith(("wifi:", "wifi;")):
            return "WiFi"
        if t.startswith(("mailto:", "tel:", "sms:")):
            return "联系方式"
        if t.startswith(("BEGIN:",)):
            return "日历/名片"
        if t.isdigit():
            return "数字"
        if any(c.isalpha() for c in t) and t.isascii():
            return "文本"
        return "文本"

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def status(self) -> dict:
        with self.lock:
            ready = self._dets is not None
            return {
                "qr_ready": ready,
                "qr_last_count": len(self.last_results),
                "qr_last_ms": self.elapsed_ms,
                "note": "多检测器集成(OpenCV QR + WeChat + Aruco)，支持二维码(QR)；商品条形码需另装 pyzbar",
            }


QR = QrEngine()


# ============================================================
# 商品条形码识别 (pyzbar · EAN/UPC/Code128 等 · 点击才识别)
# ============================================================

class BarcodeEngine:
    """商品条形码识别 (pyzbar)。支持 EAN-8/13、UPC-A/E、Code39/128、ITF 等。"""

    _SYMBOLS = None

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_annotated: bytes | None = None
        self.last_results: list[dict] = []
        self.elapsed_ms = 0
        self.error = ""

    @classmethod
    def _symbols(cls):
        if cls._SYMBOLS is not None:
            return cls._SYMBOLS
        try:
            from pyzbar.pyzbar import ZBarSymbol
            cls._SYMBOLS = [ZBarSymbol.EAN13, ZBarSymbol.EAN8,
                            ZBarSymbol.UPCA, ZBarSymbol.UPCE,
                            ZBarSymbol.CODE128, ZBarSymbol.CODE39,
                            ZBarSymbol.ITF]
        except Exception:
            cls._SYMBOLS = []
        return cls._SYMBOLS

    @staticmethod
    def _variants(img):
        import cv2
        outs = [img]
        h, w = img.shape[:2]
        for s in (2.0, 3.0):
            if max(w, h) * s <= 3600:
                outs.append(cv2.resize(img, None, fx=s, fy=s,
                                       interpolation=cv2.INTER_CUBIC))
        return outs

    def detect_once(self) -> dict:
        import cv2
        try:
            from pyzbar import pyzbar
        except Exception as e:
            return {"ok": False,
                    "error": f"pyzbar 不可用(请安装 pyzbar): {e}"}
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        t0 = time.time()
        items: list[dict] = []
        found = set()
        syms = self._symbols()
        for img in self._variants(frame):
            try:
                res = (pyzbar.decode(img, symbols=syms) if syms
                       else pyzbar.decode(img))
            except Exception:
                continue
            sx = frame.shape[1] / img.shape[1]
            sy = frame.shape[0] / img.shape[0]
            for r in res:
                try:
                    data = r.data.decode("utf-8", "replace")
                except Exception:
                    data = ""
                if not data or data in found:
                    continue
                found.add(data)
                x, y, bw, bh = (r.rect.left, r.rect.top,
                                r.rect.width, r.rect.height)
                items.append({
                    "text": data,
                    "type": str(r.type),
                    "bbox": [int(x * sx), int(y * sy),
                             int(bw * sx), int(bh * sy)],
                })
            if items:
                break
        self.elapsed_ms = int((time.time() - t0) * 1000)

        annotated = frame.copy()
        for it in items:
            x, y, bw, bh = it["bbox"]
            cv2.rectangle(annotated, (x, y), (x + bw, y + bh), (0, 220, 120), 2)
            label = f"{it['text']} ({it['type']})"
            if len(label) > 30:
                label = label[:30] + "…"
            put_text_cn(annotated, label, (x + 2, max(2, y - 20)),
                        (0, 220, 120), 15)
        ih, iw = annotated.shape[:2]
        txt = (f"条形码识别 ON   耗时 {self.elapsed_ms} ms   "
               f"检出 {len(items)} 个")
        cv2.rectangle(annotated, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
        put_text_cn(annotated, txt, (8, ih - 20), (0, 220, 120), 16)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None

        with self.lock:
            self.last_results = items
        return {"ok": True, "count": len(items),
                "elapsed_ms": self.elapsed_ms, "results": items}

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def status(self) -> dict:
        with self.lock:
            return {
                "barcode_ready": True,
                "barcode_last_count": len(self.last_results),
                "barcode_last_ms": self.elapsed_ms,
                "note": "pyzbar 识别 EAN/UPC/Code39/128/ITF 等商品条码",
            }


BARCODE = BarcodeEngine()


# ============================================================
# 手势识别 (MediaPipe HandLandmarker · 点击才识别)
# ============================================================

class GestureEngine:
    """手势识别: MediaPipe HandLandmarker 检测手部 21 关键点 + 手势分类。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._lm = None
        self._loading = False
        self.error = ""
        self.last_annotated: bytes | None = None
        self.last_results: list[dict] = []
        self.elapsed_ms = 0
        self.model_path = Path(__file__).resolve().parent / "hand_landmarker.task"

    def _ensure(self):
        if self._lm is not None:
            return self._lm
        with self.lock:
            if self._lm is not None:
                return self._lm
            if self._loading:
                return None
            self._loading = True
        try:
            import mediapipe as mp
            from mediapipe.tasks.python import vision
            from mediapipe.tasks.python.core.base_options import BaseOptions
            if not self.model_path.is_file():
                raise RuntimeError(f"缺少模型 {self.model_path.name}")
            opts = vision.HandLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=str(self.model_path)),
                running_mode=vision.RunningMode.IMAGE,
                num_hands=2, min_hand_detection_confidence=0.5)
            lm = vision.HandLandmarker.create_from_options(opts)
            with self.lock:
                self._lm = lm
                self._loading = False
            print("[GESTURE] HandLandmarker 已加载")
            return lm
        except Exception as e:
            with self.lock:
                self._loading = False
                self.error = f"手势模型加载失败: {e}"
            print(f"[ERR] {self.error}")
            return None

    @staticmethod
    def _finger_state(pts) -> list[bool]:
        """返回 5 指伸直状态 [拇指,食指,中指,无名指,小指]。"""
        import math

        def d(a, b):
            return math.dist(pts[a][:2], pts[b][:2])

        def ext(tip, mcp, pip):
            # 指尖比 PIP 离掌指关节更远 → 手指伸直
            return d(tip, mcp) > d(pip, mcp) * 1.08

        # 拇指: 双重判据(离腕/离食指掌关节都更远) → 抗弯曲误判
        thumb = ((d(4, 0) > d(3, 0) * 1.1) and (d(4, 5) > d(3, 5) * 1.05))
        return [thumb, ext(8, 5, 6), ext(12, 9, 10), ext(16, 13, 14), ext(20, 17, 18)]

    @classmethod
    def _classify(cls, pts) -> tuple[str, float]:
        """按 21 关键点几何关系分类手势。pts: [(x,y,z)] 归一化。"""
        import math

        def d(a, b):
            return math.dist(pts[a][:2], pts[b][:2])

        thumb, index, middle, ring, pinky = cls._finger_state(pts)

        # OK: 拇指尖与食指尖接近成圈, 其余伸直
        if d(4, 8) < 0.07 and middle and ring and pinky:
            return "ok", 0.9
        flags = [thumb, index, middle, ring, pinky]
        if all(flags):
            return "open_palm", 0.85
        if not any(flags):
            return "fist", 0.85
        # 手指数量手势(菜单选择用): 1=指向 2=V 3=三指 4=四指
        if index and not middle and not ring and not pinky:
            return "point", 0.85
        if index and middle and not ring and not pinky:
            return "victory", 0.85
        if index and middle and ring and not pinky:
            return "three", 0.85
        if not thumb and index and middle and ring and pinky:
            return "four", 0.85
        if thumb and not any(flags[1:]):
            return "thumbs_up", 0.85
        if pinky and not any(flags[:4]):
            return "pinky", 0.8
        return "unknown", 0.5

    def detect_once(self) -> dict:
        import cv2
        import mediapipe as mp
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        lm = self._ensure()
        if lm is None:
            return {"ok": False, "error": self.error or "手势引擎不可用"}
        t0 = time.time()
        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            res = lm.detect(mp_img)
        except Exception as e:
            return {"ok": False, "error": f"手势识别失败: {e}"}
        self.elapsed_ms = int((time.time() - t0) * 1000)
        h, w = frame.shape[:2]
        items: list[dict] = []
        try:
            for hand_lms, handed in zip(res.hand_landmarks, res.handedness):
                pts = [(p.x, p.y, p.z) for p in hand_lms]
                name, conf = self._classify(pts)
                fingers = int(sum(self._finger_state(pts)))
                wx, wy = int(pts[0][0] * w), int(pts[0][1] * h)
                label = handed[0].category_name if handed else "?"
                items.append({
                    "gesture": name, "confidence": round(conf, 2),
                    "fingers": fingers,
                    "hand": label, "wrist": [wx, wy],
                    "landmarks": [[int(x * w), int(y * h)] for x, y, _ in pts],
                })
        except Exception:
            pass

        # 标注: 画关键点 + 手势名
        annotated = frame.copy()
        for it in items:
            for (px, py) in it["landmarks"]:
                cv2.circle(annotated, (px, py), 3, (0, 220, 120), -1)
            wx, wy = it["wrist"]
            put_text_cn(annotated,
                        f"{it['gesture']} {it['confidence']} ({it['hand']})",
                        (wx + 8, max(16, wy - 12)), (0, 220, 120), 16)
        ih, iw = annotated.shape[:2]
        txt = (f"手势识别 ON   耗时 {self.elapsed_ms} ms   "
               f"检出 {len(items)} 只手")
        cv2.rectangle(annotated, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
        put_text_cn(annotated, txt, (8, ih - 20), (0, 220, 120), 16)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None

        with self.lock:
            self.last_results = items
        return {"ok": True, "count": len(items),
                "elapsed_ms": self.elapsed_ms, "results": items}

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def status(self) -> dict:
        with self.lock:
            return {
                "gesture_ready": self._lm is not None,
                "gesture_loading": self._loading,
                "gesture_error": self.error,
                "gesture_last_count": len(self.last_results),
                "gesture_last_ms": self.elapsed_ms,
                "model": self.model_path.name if self.model_path.is_file() else "缺模型",
                "note": "MediaPipe HandLandmarker 手部 21 关键点 + 手势分类",
            }


GESTURE = GestureEngine()
# 手势控制机械臂防抖: 记录上次触发的手势(连续模式下同手势不重复动作)
_GESTURE_CTRL_LAST = {"gesture": None}

def _px_to_3d(x: float, y: float):
    """像素→3D 坐标(毫米)。深度不可用返回 None。"""
    d = DEPTH
    if d is None or not DEPTH_MODE.get("active"):
        return None
    try:
        depth, scale = d.get_depth()
    except Exception:
        return None
    if depth is None:
        return None
    h, w = depth.shape
    ux = min(w - 1, max(0, int(x)))
    uy = min(h - 1, max(0, int(y)))
    z = int(depth[uy, ux])
    if z <= 0:
        mm = d.measure_mm(ux, uy, 3)
        if not mm:
            return None
        z_mm = float(mm)
    else:
        z_mm = z * scale
    fx = fy = 600.0   # 近似内参(与点云一致)
    X = (ux - w / 2.0) * z_mm / fx
    Y = (uy - h / 2.0) * z_mm / fy
    return (round(X, 1), round(Y, 1), round(z_mm, 1))


def _grasp_angle_3d(cx: float, cy: float, w: float, h: float) -> float:
    """用物体区域像素主方向(PCA)估算抓取角(度, -90..90]。"""
    try:
        import numpy as np
        d = DEPTH
        pts = []
        if d is not None and DEPTH_MODE.get("active"):
            depth, scale = d.get_depth()
            if depth is not None:
                dh, dw = depth.shape
                x0 = max(0, int(cx - w / 2)); x1 = min(dw, int(cx + w / 2))
                y0 = max(0, int(cy - h / 2)); y1 = min(dh, int(cy + h / 2))
                for v in range(y0, y1, 4):
                    for u in range(x0, x1, 4):
                        if int(depth[v, u]) > 0:
                            pts.append((u, v))
        if len(pts) >= 20:
            arr = np.array(pts, dtype=np.float64)
            arr -= arr.mean(axis=0)
            cov = arr.T @ arr
            _vals, vecs = np.linalg.eigh(cov)
            v = vecs[:, -1]
            ang = float(np.degrees(np.arctan2(v[1], v[0]))) % 180.0
            if ang > 90.0:
                ang -= 180.0
            return round(ang, 1)
    except Exception:
        pass
    return 0.0 if w >= h else 90.0


# 手势菜单: 1~5 指选择功能模式, 握拳确认执行, OK 取消(联动多种能力)
_GESTURE_MENU = {
    "state": "idle",          # idle | armed | done
    "armed_index": None,
    "armed_name": "",
    "last_action": "",
    "result": None,
    "last_gesture": None,
    "modes": ["颜色识别", "文字识别", "二维码", "目标检测", "机械臂控制"],
}

# 空中手势控制: 手掌监控/握拳截图/挥手切画面/手入3D区域触发
_AIR = {
    "running": False,
    "region": {"x": [-150.0, 150.0], "y": [-150.0, 150.0], "z": [250.0, 550.0]},
    "action": "match_score",
    "wrist_hist": [],
    "in_region": False,
    "last_gesture": None,
    "last_fire": 0.0,
    "log": [],
}
_AIR_ACTION_CN = {"match_score": "比赛+1", "arm_grasp": "抓取Z-50",
                  "arm_home": "机械臂回零", "snapshot": "截图"}


# ============================================================
# 目标检测 / 水果识别 (YOLOv8 · 预训练模型, 无需自备数据集)
# ============================================================

# 水果过滤支持 COCO 类别与兼容的自定义水果模型。
# 注意：默认 COCO 权重仍只识别苹果、香蕉、橙子。
FRUIT_CLASSES = {"banana", "apple", "orange", "grape", "pineapple", "watermelon"}
# 常见物体中文名映射 (COCO 80 类, 常用的)
COCO_CN = {
    "person": "人", "bicycle": "自行车", "car": "汽车", "motorcycle": "摩托车",
    "airplane": "飞机", "bus": "公交车", "train": "火车", "truck": "卡车",
    "boat": "船", "traffic light": "红绿灯", "stop sign": "停止标志",
    "bench": "长椅", "bird": "鸟", "cat": "猫", "dog": "狗", "horse": "马",
    "sheep": "羊", "cow": "牛", "elephant": "大象", "bear": "熊",
    "zebra": "斑马", "giraffe": "长颈鹿", "backpack": "背包", "umbrella": "伞",
    "handbag": "手提包", "tie": "领带", "suitcase": "行李箱",
    "frisbee": "飞盘", "skis": "滑雪板", "snowboard": "滑雪单板",
    "sports ball": "球", "kite": "风筝", "baseball bat": "棒球棒",
    "baseball glove": "棒球手套", "skateboard": "滑板",
    "surfboard": "冲浪板", "tennis racket": "网球拍", "bottle": "瓶子",
    "wine glass": "酒杯", "cup": "杯子", "fork": "叉子", "knife": "刀",
    "spoon": "勺子", "bowl": "碗", "banana": "香蕉", "apple": "苹果",
    "sandwich": "三明治", "orange": "橙子", "grape": "葡萄",
    "pineapple": "菠萝", "watermelon": "西瓜", "broccoli": "西兰花",
    "carrot": "胡萝卜", "hot dog": "热狗", "pizza": "披萨", "donut": "甜甜圈",
    "cake": "蛋糕", "chair": "椅子", "couch": "沙发", "potted plant": "盆栽",
    "bed": "床", "dining table": "餐桌", "toilet": "马桶", "tv": "电视",
    "laptop": "笔记本电脑", "mouse": "鼠标", "remote": "遥控器",
    "keyboard": "键盘", "cell phone": "手机", "microwave": "微波炉",
    "oven": "烤箱", "toaster": "烤面包机", "sink": "水槽",
    "refrigerator": "冰箱", "book": "书", "clock": "时钟", "vase": "花瓶",
    "scissors": "剪刀", "teddy bear": "泰迪熊", "hair drier": "吹风机",
    "toothbrush": "牙刷",
}


def _normalize_yolo_label(value) -> str:
    """Canonicalize labels from COCO and custom checkpoints consistently."""
    return str(value).strip().casefold()


class YoloEngine:
    """YOLOv8 目标检测 / 水果识别。

    - 用 ultralytics 预训练权重 (COCO 80 类), 无需自备数据集
    - 点击触发, 不持续运行
    - 支持"仅水果"过滤 (香蕉/苹果/橙子) 或全部物体
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._model = None
        self._loading = False
        self.last_annotated: bytes | None = None
        self.last_results: list[dict] = []
        self.elapsed_ms = 0
        self.error = ""
        self.model_name = ""

    def _ensure(self):
        if self._model is not None:
            return self._model
        with self.lock:
            if self._model is not None:
                return self._model
            if self._loading:
                return None
            self._loading = True
        weights = ROOT / "yolov8n.pt"
        if not weights.is_file():
            with self.lock:
                self._loading = False
                self.error = (
                    f"本地 YOLO 权重不存在: {weights}。为避免自动下载占用磁盘，"
                    "请手动放入模型文件后重试。"
                )
            return None
        try:
            from ultralytics import YOLO
            import torch
            torch.set_num_threads(4)
            m = YOLO(str(weights))
            with self.lock:
                self._model = m
                self.model_name = "yolov8n (COCO 80类)"
                self._loading = False
            print("[YOLO] yolov8n 模型已加载 (80 类, 含香蕉/苹果/橙子)")
            return m
        except Exception as e:
            with self.lock:
                self._loading = False
                self.error = f"YOLO 模型加载失败: {e}"
            print(f"[ERR] {self.error}")
            return None

    def load_custom_model(self, path: str) -> dict:
        """加载自定义 YOLO 模型 (如自训练的水果模型 .pt), 扩充识别类别。"""
        from pathlib import Path
        p = Path(path)
        if not p.is_file():
            return {"ok": False, "error": f"模型文件不存在: {path}"}
        try:
            from ultralytics import YOLO
            import torch
            torch.set_num_threads(4)
            m = YOLO(str(p))
            names = getattr(m, "names", {}) or {}
            cls_list = [names[k] for k in sorted(names)] if isinstance(names, dict) else list(names)
            with self.lock:
                self._model = m
                self.model_name = f"{p.name} ({len(cls_list)}类)"
                self._loading = False
                self.error = ""
            print(f"[YOLO] 自定义模型已加载: {p.name} ({len(cls_list)} 类)")
            return {"ok": True, "model": self.model_name,
                    "classes": cls_list[:80]}
        except Exception as e:
            return {"ok": False, "error": f"自定义模型加载失败: {e}"}

    def _get_frame(self):
        """取当前帧: 优先 cv2 RGB; 深度模式下 cv2 已停, 用 Orbbec 彩色帧(与深度同坐标)。"""
        frame = _current_frame()
        if frame is None and DEPTH is not None and DEPTH_MODE.get("active"):
            try:
                frame = DEPTH.get_color()
            except Exception:
                frame = None
        return frame

    def _measure_box(self, bbox):
        """用深度估测检测框中心距离(mm)与实际宽高(cm)。深度不可用返回 None。"""
        d = DEPTH
        if d is None or not DEPTH_MODE.get("active"):
            return None
        try:
            depth, scale = d.get_depth()
        except Exception:
            return None
        if depth is None:
            return None
        x, y, w, h = bbox
        cx, cy = x + w // 2, y + h // 2
        dist = d.measure_mm(cx, cy, 4)
        if not dist:
            return None
        # 近似针孔: 实际尺寸 ≈ 像素尺寸 × 距离 / 焦距 (640 宽约 600px)
        fx = fy = 600.0
        w_cm = w * dist / fx / 10.0
        h_cm = h * dist / fy / 10.0
        return {"distance_mm": round(dist, 1),
                "width_cm": round(w_cm, 1), "height_cm": round(h_cm, 1)}

    def detect_once(self, only_fruit: bool = False,
                    conf: float = 0.35) -> dict:
        """对当前帧做一次目标检测。

    only_fruit=True 时保留已知水果类别；默认 COCO 权重仅有苹果/香蕉/橙子。
        """
        import cv2
        frame = self._get_frame()
        use_measure = DEPTH is not None and DEPTH_MODE.get("active")
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        if self._model is None and self._loading:
            return {"ok": False, "error": "YOLO 模型正在首次加载，请稍候再试"}
        m = self._ensure()
        if m is None:
            return {"ok": False,
                    "error": self.error or "YOLO 不可用，请确认已安装 ultralytics 且 yolov8n.pt 存在"}

        conf = max(0.05, min(0.95, float(conf)))
        t0 = time.time()
        try:
            res = m.predict(frame, conf=conf, verbose=False, imgsz=640)
        except Exception as e:
            return {"ok": False, "error": f"检测失败: {e}"}
        self.elapsed_ms = int((time.time() - t0) * 1000)

        items: list[dict] = []
        r0 = res[0] if res else None
        if r0 is not None and r0.boxes is not None:
            names = r0.names
            boxes = r0.boxes
            for i in range(len(boxes)):
                cls_id = int(boxes.cls[i].item())
                en = _normalize_yolo_label(names.get(cls_id, str(cls_id)))
                if only_fruit and en not in FRUIT_CLASSES:
                    continue
                xyxy = boxes.xyxy[i].tolist()
                score = float(boxes.conf[i].item())
                bbox = [int(xyxy[0]), int(xyxy[1]),
                        int(xyxy[2] - xyxy[0]), int(xyxy[3] - xyxy[1])]
                item = {
                    "label_en": en,
                    "label_cn": COCO_CN.get(en, en),
                    "confidence": round(score, 3),
                    "bbox": bbox,
                    "is_fruit": en in FRUIT_CLASSES,
                }
                if use_measure:
                    meas = self._measure_box(bbox)
                    if meas:
                        item.update(meas)
                items.append(item)
        items.sort(key=lambda x: x["confidence"], reverse=True)

        # 统计
        stats: dict[str, int] = {}
        for it in items:
            key = it["label_cn"]
            stats[key] = stats.get(key, 0) + 1

        # 标注图
        annotated = frame.copy()
        for it in items:
            x, y, w, h = it["bbox"]
            c = (0, 220, 120) if it["is_fruit"] else (240, 180, 40)
            cv2.rectangle(annotated, (x, y), (x + w, y + h), c, 2)
            label = f"{it['label_cn']} {it['confidence']:.2f}"
            if it.get("distance_mm"):
                label += (f"  {it['distance_mm']:.0f}mm "
                          f"{it.get('width_cm', 0)}x{it.get('height_cm', 0)}cm")
            put_text_cn(annotated, label, (x + 2, max(2, y - 20)), c, 16)
        ih, iw = annotated.shape[:2]
        mode = "仅水果" if only_fruit else "全部物体"
        txt = (f"目标检测 ON [{mode}]   置信度≥{conf:.2f}   "
               f"耗时 {self.elapsed_ms} ms   检出 {len(items)} 个")
        cv2.rectangle(annotated, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
        put_text_cn(annotated, txt, (8, ih - 20), (0, 220, 120), 16)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None

        with self.lock:
            self.last_results = items

        fruit_n = sum(1 for it in items if it["is_fruit"])
        return {
            "ok": True,
            "count": len(items),
            "fruit_count": fruit_n,
            "stats": stats,
            "elapsed_ms": self.elapsed_ms,
            "only_fruit": only_fruit,
            "conf": conf,
            "model": self.model_name,
            "results": items,
        }

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def status(self) -> dict:
        with self.lock:
            return {
                "yolo_ready": self._model is not None,
                "yolo_loading": self._loading,
                "yolo_error": self.error,
                "yolo_model": self.model_name,
                "yolo_last_count": len(self.last_results),
                "yolo_last_ms": self.elapsed_ms,
                "fruit_classes": sorted(FRUIT_CLASSES),
                "note": "COCO 预训练 80 类；水果含香蕉/苹果/橙子。更多水果需自备数据集微调",
            }


YOLO = YoloEngine()


# ============================================================
# 机械臂控制器 (关节/运动/比赛状态机 + 真实驱动接口层)
# 安全约定: 默认 simulation 模式, 不向硬件发送任何指令;
#   real 模式必须先配置 型号/串口/协议 并显式启用 real_enabled, 且带急停。
# ============================================================

class ArmSerialDriver:
    """真实机械臂串口/舵机驱动适配器(接口框架)。

    仅在配置具体 型号/端口/协议 后才连接并发送; 默认不连接、不发送。
    """

    def __init__(self, port: str = "", baud: int = 115200,
                 protocol: str = "pca9685", model: str = "") -> None:
        self.port = port
        self.baud = baud
        self.protocol = protocol   # pca9685(舵机) / grbl / serial-ascii
        self.model = model
        self.connected = False
        self.error = ""

    def connect(self) -> dict:
        """尝试连接真实机械臂。未配置端口时拒绝, 不做任何硬件操作。"""
        if not self.port:
            return {"ok": False,
                    "error": "未配置串口(如 COM3); 真实驱动需先填 端口/型号/协议"}
        if not self.model:
            return {"ok": False,
                    "error": "未配置机械臂型号; 请先填写型号与协议"}
        # TODO: 按 self.protocol 实现真实连接(需具体硬件 SDK/协议)。
        # 当前仅记录配置, 不真正连接, 保持安全。
        self.connected = False
        self.error = "真实驱动尚未绑定具体硬件协议(需按型号实现)"
        return {"ok": False, "error": self.error}

    def send_joints(self, joints, position) -> dict:
        """向真实机械臂发送关节/坐标指令。未连接时拒绝。"""
        if not self.connected:
            return {"ok": False, "error": "真实机械臂未连接; 指令未发送"}
        # TODO: 按协议封装并发送。当前禁用。
        return {"ok": False, "error": "发送通道未实现(需按型号实现)"}

    def emergency(self) -> dict:
        self.connected = False
        return {"ok": True, "note": "已断开/急停(真实通道)"}

    def info(self) -> dict:
        return {"port": self.port, "baud": self.baud,
                "protocol": self.protocol, "model": self.model,
                "connected": self.connected, "error": self.error}


class ArmController:
    """机械臂控制器: 关节/运动 + 比赛状态机 + 真实驱动接口层。

    默认 simulation(只更新本地状态); real 模式需配置并显式启用才发指令。
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.mode = "simulation"          # simulation | real
        self.real_enabled = False         # 真实驱动开关(默认关)
        self.driver = ArmSerialDriver()   # 真实驱动适配器(默认未连接)
        self.joints = [90.0] * 6          # 6 关节角度(度)
        self.position = [0.0, 0.0, 0.0]   # XYZ mm
        self.state = "idle"               # idle/moving/homed/stopped/emergency
        self.last_command = ""
        self.match = {"running": False, "task": "", "score": 0,
                      "started_at": 0.0, "elapsed": 0.0,
                      "goals": 0, "log": []}

    # ---- 底层动作(仿真更新状态; 真实仅在启用时发送) ----
    def _apply(self) -> None:
        if self.mode == "real" and self.real_enabled:
            self.driver.send_joints(self.joints, self.position)

    def set_joints(self, angles) -> dict:
        with self.lock:
            if self.state == "emergency":
                return {"ok": False, "error": "急停中, 请先复位"}
            try:
                j = [max(0.0, min(180.0, float(a))) for a in list(angles)[:6]]
            except Exception:
                return {"ok": False, "error": "关节角度无效"}
            if len(j) < 6:
                j += [self.joints[k] for k in range(len(j), 6)]
            self.joints = j
            self.state = "moving"
            self.last_command = "set_joints"
            self._apply()
            return {"ok": True, **self.get_state()}

    def move_to(self, x, y, z) -> dict:
        with self.lock:
            if self.state == "emergency":
                return {"ok": False, "error": "急停中, 请先复位"}
            try:
                self.position = [max(-500.0, min(500.0, float(x))),
                                 max(-500.0, min(500.0, float(y))),
                                 max(0.0, min(500.0, float(z)))]
            except Exception:
                return {"ok": False, "error": "坐标无效"}
            self.state = "moving"
            self.last_command = f"move {self.position}"
            self._apply()
            return {"ok": True, **self.get_state()}

    def home(self) -> dict:
        with self.lock:
            self.joints = [90.0] * 6
            self.position = [0.0, 0.0, 0.0]
            self.state = "homed"
            self.last_command = "home"
            self._apply()
            return {"ok": True, **self.get_state()}

    def stop(self) -> dict:
        with self.lock:
            self.state = "stopped"
            self.last_command = "stop"
            return {"ok": True, **self.get_state()}

    def emergency(self) -> dict:
        with self.lock:
            self.state = "emergency"
            self.real_enabled = False
            self.driver.emergency()
            self.last_command = "emergency"
            return {"ok": True, **self.get_state()}

    def reset(self) -> dict:
        with self.lock:
            self.state = "idle"
            self.last_command = "reset"
            return {"ok": True, **self.get_state()}

    # ---- 真实驱动配置 ----
    def configure_real(self, port="", model="", protocol="pca9685",
                       baud=115200, enable=False) -> dict:
        with self.lock:
            self.driver = ArmSerialDriver(port=port, baud=baud,
                                          protocol=protocol, model=model)
            if enable:
                r = self.driver.connect()
                # 仅当真实连接成功才启用真实模式; 否则保持仿真(安全)
                if r.get("ok"):
                    self.real_enabled = True
                    self.mode = "real"
                else:
                    self.real_enabled = False
                    self.mode = "simulation"
            else:
                self.real_enabled = False
                self.mode = "simulation"
                r = {"ok": True, "note": "仿真模式"}
            return {"ok": True, "real_enabled": self.real_enabled,
                    "mode": self.mode, "driver": self.driver.info(),
                    "connect": r}

    # ---- 比赛 ----
    def match_start(self, task: str) -> dict:
        with self.lock:
            if self.match["running"]:
                return {"ok": False, "error": "比赛已在进行"}
            self.match.update(running=True, task=task or "pick",
                              score=0, goals=0, started_at=time.time(),
                              elapsed=0.0,
                              log=[f"比赛开始: {task or 'pick'}"])
            return {"ok": True, **self.match_state()}

    def match_stop(self) -> dict:
        with self.lock:
            if not self.match["running"]:
                return {"ok": False, "error": "比赛未在进行"}
            self.match["running"] = False
            self.match["elapsed"] = round(time.time() - self.match["started_at"], 1)
            self.match["log"].append(f"比赛结束 用时{self.match['elapsed']}s "
                                     f"得分{self.match['score']}")
            return {"ok": True, **self.match_state()}

    def match_score(self, delta: int = 1) -> dict:
        with self.lock:
            if not self.match["running"]:
                return {"ok": False, "error": "比赛未在进行"}
            self.match["score"] = max(0, self.match["score"] + int(delta))
            self.match["goals"] += 1
            self.match["elapsed"] = round(time.time() - self.match["started_at"], 1)
            self.match["log"].append(f"+{delta} 分 (累计{self.match['score']})")
            return {"ok": True, **self.match_state()}

    def match_state(self) -> dict:
        with self.lock:
            m = dict(self.match)
            if m["running"]:
                m["elapsed"] = round(time.time() - m["started_at"], 1)
            m["log"] = list(m["log"])[-10:]
            return {"match": m}

    def get_state(self) -> dict:
        with self.lock:
            return {
                "mode": self.mode,
                "real_enabled": self.real_enabled,
                "state": self.state,
                "joints": [round(j, 1) for j in self.joints],
                "position": [round(p, 1) for p in self.position],
                "last_command": self.last_command,
                "driver": self.driver.info(),
            }


ARM = ArmController()


# 指令窗口只接受白名单命令。机械臂当前仅提供虚拟状态机，不连接任何硬件。
_ROBOT_SIM_LOCK = threading.Lock()
_ROBOT_SIM_STATE = {"mode": "simulation-only", "state": "idle",
                    "position_mm": [0.0, 0.0, 0.0], "last_command": ""}


def execute_console_command(command: str) -> dict:
    """执行固定白名单命令；绝不调用 shell，也绝不向机械臂发送指令。"""
    text = " ".join(str(command or "").strip().split())
    lower = text.lower()
    if not text:
        return {"ok": False, "error": "请输入命令。输入 help 查看命令。"}
    if lower in {"help", "帮助"}:
        return {"ok": True, "mode": "safe-console", "output": [
            "yolo detect [fruit] [conf=0.35] — 对当前相机帧执行一次 YOLOv8 检测",
            "robot status — 查看机械臂虚拟状态（真实设备未连接）",
            "robot simulate home | stop — 虚拟回零/停止",
            "robot simulate move X Y Z — 虚拟坐标演示，单位 mm，范围 X/Y ±500、Z 0..500",
            "视觉项目请用页面中的 1→4 项目卡片；只更新仿真状态，不驱动实体机械臂。",
            "所有 robot simulate 命令仅更新本地模拟状态，不驱动硬件。",
        ]}
    if lower == "status":
        camera = CAM.status()
        return {"ok": True, "mode": "read-only", "output": [
            f"RGB: {'running' if camera.get('running') else 'idle'}",
            f"YOLO: {YOLO.status().get('yolo_model') or ('loading' if YOLO.status().get('yolo_loading') else 'not loaded')}",
            "Robot: simulation-only; hardware adapter disabled",
        ], "data": {"camera": camera, "yolo": YOLO.status()}}
    if lower.startswith("yolo detect"):
        parts = lower.split()
        if parts[0:2] != ["yolo", "detect"] or any(
                p not in {"fruit"} and not p.startswith("conf=") for p in parts[2:]):
            return {"ok": False, "error": "格式：yolo detect [fruit] [conf=0.35]"}
        only_fruit = "fruit" in parts[2:]
        confidence = 0.35
        for item in parts[2:]:
            if item.startswith("conf="):
                try:
                    confidence = float(item.split("=", 1)[1])
                except ValueError:
                    return {"ok": False, "error": "conf 必须是 0.05 到 0.95 的数字。"}
        if not 0.05 <= confidence <= 0.95:
            return {"ok": False, "error": "conf 范围为 0.05 到 0.95。"}
        result = YOLO.detect_once(only_fruit=only_fruit, conf=confidence)
        return {"ok": bool(result.get("ok")), "mode": "yolo", "output":
                ([f"YOLO 检测完成：{result.get('count', 0)} 个目标，耗时 {result.get('elapsed_ms', 0)} ms。"]
                 if result.get("ok") else [result.get("error", "YOLO 检测失败")]),
                "data": result}
    if lower == "robot status":
        with _ROBOT_SIM_LOCK:
            state = dict(_ROBOT_SIM_STATE)
        return {"ok": True, "mode": "simulation-only", "output": [
            "机械臂硬件：未连接；真实控制：禁用",
            f"模拟状态：{state['state']}；位置(mm)：{state['position_mm']}",
        ], "data": state}
    if lower in {"robot simulate home", "robot simulate stop"}:
        action = lower.rsplit(" ", 1)[1]
        with _ROBOT_SIM_LOCK:
            _ROBOT_SIM_STATE["state"] = "homed" if action == "home" else "stopped"
            if action == "home":
                _ROBOT_SIM_STATE["position_mm"] = [0.0, 0.0, 0.0]
            _ROBOT_SIM_STATE["last_command"] = lower
            state = dict(_ROBOT_SIM_STATE)
        return {"ok": True, "mode": "simulation-only", "output":
                [f"虚拟机械臂已执行 {action}；未连接或驱动真实机械臂。"], "data": state}
    move = re.fullmatch(r"robot simulate move (-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?) (-?\d+(?:\.\d+)?)", lower)
    if move:
        x, y, z = (float(v) for v in move.groups())
        if abs(x) > 500 or abs(y) > 500 or not 0 <= z <= 500:
            return {"ok": False, "error": "模拟工作范围：X/Y 为 -500..500 mm，Z 为 0..500 mm。"}
        with _ROBOT_SIM_LOCK:
            _ROBOT_SIM_STATE.update(state="simulated-position",
                                    position_mm=[x, y, z], last_command=lower)
            state = dict(_ROBOT_SIM_STATE)
        return {"ok": True, "mode": "simulation-only", "output":
                [f"虚拟坐标已更新至 X={x:g}, Y={y:g}, Z={z:g} mm；没有向硬件发送命令。"],
                "data": state}
    return {"ok": False, "error": "未知命令或格式错误。输入 help 查看允许的命令。"}


# ============================================================
# 运动检测 / 入侵报警 (背景差分 · 纯算法, 无数据集)
# ============================================================

class MotionDetector:
    """运动检测 / 入侵报警。

    - 用背景差分 (MOG2) 检测画面变化
    - 独立线程, 开启后持续监测, 不阻塞视频流
    - 检测到运动: 自动抓拍 + 写入报警日志
    - 灵敏度/最小面积/冷却时间可调
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.enabled = False
        self.sensitivity = 50          # 0-100, 越高越灵敏
        self.min_area_ratio = 0.5      # 运动区域占画面百分比阈值
        self.cooldown_s = 3.0          # 两次报警最小间隔
        self.save_snapshot = True      # 报警时自动抓拍
        self.smart_recognize = True    # 报警时自动跑颜色/文字识别记入日志
        self._bgs = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.motion_level = 0.0        # 当前运动量 (0-100)
        self.motion_area = 0
        self.alarm_count = 0
        self.alarms: list[dict] = []   # 报警日志 (最近 50 条)
        self.last_alarm_at = 0.0
        self.last_motion_jpeg: bytes | None = None
        self.started_at = 0.0
        self.frames_analyzed = 0
        self.error = ""

    # ---- 灵敏度映射 ----

    def _thresholds(self) -> tuple[int, int]:
        """灵敏度 -> (MOG2 阈值, 最小面积像素)。越灵敏阈值越低。"""
        s = max(0, min(100, int(self.sensitivity)))
        # MOG2 varThreshold: 低=灵敏; 默认 16
        mog = max(4, 60 - int(s * 0.55))          # s=50→32  s=90→10
        # 最小面积: 灵敏度越高, 越小的运动也算
        img_area = CAM.width * CAM.height or 307200
        min_area = int(img_area * self.min_area_ratio / 100.0 * (110 - s) / 100.0)
        min_area = max(200, min_area)
        return mog, min_area

    # ---- 启停 ----

    def start(self) -> bool:
        with self.lock:
            if self.enabled:
                return True
            self.enabled = True
            self.error = ""
        try:
            import cv2
            self._bgs = cv2.createBackgroundSubtractorMOG2(
                history=300, varThreshold=32, detectShadows=True)
        except Exception as e:
            with self.lock:
                self.enabled = False
                self.error = f"运动检测初始化失败: {e}"
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        with self.lock:
            self.started_at = time.time()
        print("[MOTION] 运动检测已开启")
        return True

    def stop(self) -> bool:
        with self.lock:
            if not self.enabled:
                return True
            self.enabled = False
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None
        self._bgs = None
        print("[MOTION] 运动检测已关闭")
        return True

    # ---- 主循环 ----

    def _loop(self) -> None:
        import cv2
        import numpy as np
        while not self._stop.is_set():
            frame = _current_frame()
            if frame is None:
                time.sleep(0.15)
                continue
            try:
                self._analyze(frame)
            except Exception as e:
                with self.lock:
                    self.error = str(e)
            time.sleep(0.1)     # 约 10 Hz, 不影响视频流

    def _analyze(self, frame) -> None:
        import cv2
        import numpy as np
        if self._bgs is None:
            return
        mog_thresh, min_area = self._thresholds()
        # MOG2 前景掩码
        fg = self._bgs.apply(frame)
        # 去噪
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        fg = cv2.morphologyEx(fg, cv2.MORPH_OPEN, kernel)
        fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, kernel)
        # 阴影像素 (127) 归到背景
        mask = (fg > 200).astype(np.uint8) * 255

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        total_area = 0
        boxes = []
        for c in contours:
            a = int(cv2.contourArea(c))
            if a < min_area:
                continue
            total_area += a
            x, y, w, h = cv2.boundingRect(c)
            boxes.append((x, y, w, h))

        img_area = frame.shape[0] * frame.shape[1]
        level = min(100.0, 100.0 * total_area / max(1, img_area) * 10)

        with self.lock:
            self.motion_level = round(level, 1)
            self.motion_area = total_area
            self.frames_analyzed += 1

        # 报警判定: 有运动框且不在冷却期
        if boxes:
            now = time.time()
            if now - self.last_alarm_at >= self.cooldown_s:
                self._trigger_alarm(frame, boxes, total_area, level)
                self.last_alarm_at = now

    def _analyze_alarm_frame(self, frame) -> dict:
        """智能抓拍识别: 对报警帧跑颜色+文字识别(不生成标注图), 供报警日志记录。"""
        out: dict = {}
        try:
            dets, _stats = COLOR.analyze(frame)
            out["colors"] = [{"color": d["color"], "conf": d["confidence"]}
                             for d in dets[:3]]
        except Exception:
            pass
        try:
            eng = OCR._ensure()
            if eng:
                res, _ = eng(frame)
                out["texts"] = [{"text": OCR._fix_oi(t), "conf": round(float(s), 2)}
                                for _b, t, s in (res or [])[:3]]
        except Exception:
            pass
        return out

    def _trigger_alarm(self, frame, boxes, area, level) -> None:
        import cv2
        vis = frame.copy()
        for (x, y, w, h) in boxes:
            cv2.rectangle(vis, (x, y), (x + w, y + h), (0, 0, 255), 2)
        ih, iw = vis.shape[:2]
        ts = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        txt = f"! 运动报警  {ts}   区域 {area} px   运动量 {level:.1f}"
        cv2.rectangle(vis, (0, ih - 26), (iw, ih), (120, 20, 20), -1)
        put_text_cn(vis, txt, (8, ih - 20), (255, 235, 235), 16)

        fname = ""
        if self.save_snapshot:
            SAVE_DIR.mkdir(exist_ok=True)
            fname = f"motion_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            try:
                cv2.imwrite(str(SAVE_DIR / fname), vis)
            except Exception:
                fname = ""

        try:
            ok, buf = cv2.imencode(".jpg", vis,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 85])
            self.last_motion_jpeg = buf.tobytes() if ok else None
        except Exception:
            pass

        rec: dict = {}
        if self.smart_recognize:
            rec = self._analyze_alarm_frame(frame)   # 锁外跑(颜色/文字), 不阻塞状态

        with self.lock:
            self.alarm_count += 1
            self.alarms.insert(0, {
                "time": ts,
                "area": int(area),
                "level": round(level, 1),
                "boxes": len(boxes),
                "snapshot": fname,
                **rec,
            })
            if len(self.alarms) > 50:
                self.alarms = self.alarms[:50]
        print(f"[ALERT] 运动报警 #{self.alarm_count}: {area} px, {len(boxes)} 个区域")

    # ---- 对外 ----

    def status(self) -> dict:
        with self.lock:
            return {
                "motion_enabled": self.enabled,
                "motion_level": self.motion_level,
                "motion_area": self.motion_area,
                "alarm_count": self.alarm_count,
                "motion_sensitivity": self.sensitivity,
                "motion_min_area": self.min_area_ratio,
                "motion_cooldown": self.cooldown_s,
                "motion_save": self.save_snapshot,
                "smart_recognize": self.smart_recognize,
                "motion_frames": self.frames_analyzed,
                "motion_error": self.error,
                "uptime": round(time.time() - self.started_at, 1)
                          if self.started_at else 0,
                "recent_alarms": self.alarms[:8],
            }

    def get_motion_jpeg(self):
        with self.lock:
            return self.last_motion_jpeg


MOTION = MotionDetector()


# ============================================================
# Haar 特征目标检测 (人脸/猫脸/微笑/眼睛/车牌/全身 等)
# 依赖: opencv-contrib-python 的 haarcascade 级联模型, 无需数据集
# ============================================================

import os as _os
_CV2_DATA = _os.path.join(_os.path.dirname(_os.path.join(_os.path.dirname(
    __import__("cv2").__file__), "cv2")), "data")


def _cascade_dir() -> str:
    import cv2
    return _os.path.join(_os.path.dirname(cv2.__file__), "data")


# 可选检测目标: (英文key, 中文名, 级联文件, 最小邻居数, 缩放因子)
HAAR_TARGETS = [
    ("face_front", "人脸(正面)", "haarcascade_frontalface_default.xml", 5, 1.1),
    ("face_alt", "人脸(高精度)", "haarcascade_frontalface_alt2.xml", 5, 1.1),
    ("face_profile", "人脸(侧面)", "haarcascade_profileface.xml", 5, 1.1),
    ("cat_face", "猫脸", "haarcascade_frontalcatface.xml", 5, 1.1),
    ("smile", "微笑", "haarcascade_smile.xml", 20, 1.8),
    ("eye", "眼睛", "haarcascade_eye.xml", 6, 1.1),
    ("plate", "汽车牌照", "haarcascade_russian_plate_number.xml", 5, 1.1),
    ("fullbody", "全身人体", "haarcascade_fullbody.xml", 5, 1.1),
    ("upperbody", "上半身", "haarcascade_upperbody.xml", 5, 1.1),
    ("lowerbody", "下半身", "haarcascade_lowerbody.xml", 5, 1.1),
]


class HaarDetector:
    """基于 Haar 级联的目标检测 (人脸/猫脸/微笑/眼睛/车牌/人体)。

    - 点击触发, 不持续运行
    - 零额外依赖 (opencv-contrib 自带级联模型)
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self._cascades: dict = {}
        self.last_annotated: bytes | None = None
        self.last_results: list[dict] = []
        self.elapsed_ms = 0

    def _load(self, key: str):
        """按需加载级联分类器 (缓存)。"""
        if key in self._cascades:
            return self._cascades[key]
        import cv2
        meta = next((m for m in HAAR_TARGETS if m[0] == key), None)
        if meta is None:
            return None
        path = _os.path.join(_cascade_dir(), meta[2])
        if not _os.path.isfile(path):
            self._cascades[key] = None
            return None
        try:
            cascade = cv2.CascadeClassifier(path)
            if cascade.empty():
                self._cascades[key] = None
                return None
        except Exception:
            self._cascades[key] = None
            return None
        self._cascades[key] = cascade
        return cascade

    def detect_once(self, target: str = "face_front") -> dict:
        import cv2
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未启动或无画面"}
        meta = next((m for m in HAAR_TARGETS if m[0] == target), None)
        if meta is None:
            return {"ok": False,
                    "error": f"未知检测目标 '{target}'",
                    "available": [m[0] for m in HAAR_TARGETS]}
        cascade = self._load(target)
        if cascade is None:
            return {"ok": False,
                    "error": f"级联模型缺失或加载失败: {meta[2]}"}

        t0 = time.time()
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            min_n, scale = meta[3], meta[4]
            rects = cascade.detectMultiScale(
                gray, scaleFactor=scale, minNeighbors=min_n,
                minSize=(30, 30))
        except Exception as e:
            return {"ok": False, "error": f"检测失败: {e}"}
        self.elapsed_ms = int((time.time() - t0) * 1000)

        items = []
        for (x, y, w, h) in rects:
            items.append({
                "bbox": [int(x), int(y), int(w), int(h)],
                "confidence": None,   # Haar 不直接给置信度
            })

        # 标注图
        annotated = frame.copy()
        for it in items:
            x, y, w, h = it["bbox"]
            cv2.rectangle(annotated, (x, y), (x + w, y + h), (0, 220, 120), 2)
            put_text_cn(annotated, meta[1], (x, max(2, y - 20)), (0, 220, 120), 16)
        ih, iw = annotated.shape[:2]
        txt = (f"{meta[1]}检测   耗时 {self.elapsed_ms} ms   "
               f"检出 {len(items)} 个")
        cv2.rectangle(annotated, (0, ih - 25), (iw, ih), (18, 22, 28), -1)
        put_text_cn(annotated, txt, (8, ih - 20), (0, 220, 120), 16)
        try:
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 88])
            self.last_annotated = buf.tobytes() if ok else None
        except Exception:
            self.last_annotated = None

        with self.lock:
            self.last_results = items

        return {
            "ok": True,
            "target": target,
            "target_cn": meta[1],
            "count": len(items),
            "elapsed_ms": self.elapsed_ms,
            "results": items,
        }

    def get_annotated_jpeg(self):
        with self.lock:
            return self.last_annotated

    def status(self) -> dict:
        return {
            "haar_ready": True,
            "targets": [{"key": m[0], "cn": m[1]} for m in HAAR_TARGETS],
            "haar_last_count": len(self.last_results),
            "haar_last_ms": self.elapsed_ms,
        }


HAAR = HaarDetector()


# ============================================================
# 物体追踪 (CSRT 算法 · opencv-contrib 自带, 无数据集)
# ============================================================

class ObjectTracker:
    """基于 CSRT 的单物体追踪。

    - 点击视频选目标 → 开始追踪
    - 独立线程更新目标位置, 绘制追踪框
    - 目标丢失时自动停止并提示
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tracker = None
        self.enabled = False
        self.bbox: list[int] | None = None       # [x, y, w, h]
        self.initial_bbox: list[int] | None = None
        self.confidence = 0.0
        self.lost = False
        self.error = ""
        self.started_at = 0.0
        self.frames_tracked = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, cx: int, cy: int, size: int = 120) -> dict:
        """以 (cx, cy) 为中心选一块 size×size 区域作为追踪目标。"""
        import cv2
        frame = _current_frame()
        if frame is None:
            return {"ok": False, "error": "相机未运行"}
        h, w = frame.shape[:2]
        half = max(20, size) // 2
        x = max(0, min(cx - half, w - 1))
        y = max(0, min(cy - half, h - 1))
        bw = min(size, w - x)
        bh = min(size, h - y)
        bbox = (int(x), int(y), int(bw), int(bh))

        # 优先 CSRT, 不行用 KCF
        tracker = None
        try:
            if hasattr(cv2, "TrackerCSRT_create"):
                tracker = cv2.TrackerCSRT_create()
            elif hasattr(cv2, "legacy") and hasattr(cv2.legacy, "TrackerCSRT_create"):
                tracker = cv2.legacy.TrackerCSRT_create()
        except Exception:
            tracker = None
        if tracker is None:
            try:
                tracker = cv2.TrackerKCF_create()
            except Exception:
                return {"ok": False, "error": "本 OpenCV 版本无可用追踪器"}
        try:
            # OpenCV 5.x 的 tracker.init() 返回 None (void), 只有异常才算失败
            tracker.init(frame, bbox)
        except Exception as e:
            return {"ok": False, "error": f"追踪器初始化失败: {e}"}

        self.stop()
        with self.lock:
            self.tracker = tracker
            self.bbox = list(bbox)
            self.initial_bbox = list(bbox)
            self.enabled = True
            self.lost = False
            self.confidence = 1.0
            self.error = ""
            self.started_at = time.time()
            self.frames_tracked = 0
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[TRACK] 开始追踪 bbox={bbox}")
        return {"ok": True, "bbox": list(bbox), "size": [w, h]}

    def stop(self) -> None:
        with self.lock:
            self.enabled = False
            self.tracker = None
            self.bbox = None
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            frame = _current_frame()
            if frame is None:
                time.sleep(0.1)
                continue
            with self.lock:
                trk = self.tracker
            if trk is None:
                break
            try:
                ok, bbox = trk.update(frame)
            except Exception as e:
                with self.lock:
                    self.error = str(e)
                break
            with self.lock:
                if ok:
                    self.bbox = [int(v) for v in bbox]
                    self.frames_tracked += 1
                    self.lost = False
                    self.confidence = max(0.0, min(1.0,
                        1.0 - (time.time() - self.started_at) / 120.0))
                else:
                    self.lost = True
                    self.enabled = False
                    print("[TRACK] 目标丢失, 追踪结束")
                    break
            time.sleep(0.05)   # 约 20Hz

    def draw(self, frame) -> None:
        with self.lock:
            if not self.enabled or self.bbox is None:
                return
            x, y, w, h = self.bbox
            lost = self.lost
        import cv2
        color = (0, 0, 255) if lost else (0, 220, 120)
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        label = f"追踪中 {w}x{h}"
        put_text_cn(frame, label, (x, max(2, y - 20)), color, 16)

    def status(self) -> dict:
        with self.lock:
            return {
                "track_enabled": self.enabled,
                "track_lost": self.lost,
                "track_bbox": self.bbox,
                "track_initial": self.initial_bbox,
                "track_frames": self.frames_tracked,
                "track_confidence": round(self.confidence, 2),
                "track_error": self.error,
                "uptime": round(time.time() - self.started_at, 1)
                          if self.started_at else 0,
            }


TRACK = ObjectTracker()


# ============================================================
# 摄像头参数控制 (探测实际支持项, 不支持的禁用并说明)
# ============================================================

# (属性名, cv2 常量名, 中文名, UI 类型, 单位)
# 注意: 用常量名字符串 + getattr 取, 避免 OpenCV 5.x 缺常量时 AttributeError
CAMERA_PROPS = [
    ("FRAME_WIDTH", "CAP_PROP_FRAME_WIDTH", "宽度", "resolution", "px"),
    ("FRAME_HEIGHT", "CAP_PROP_FRAME_HEIGHT", "高度", "resolution", "px"),
    ("FPS", "CAP_PROP_FPS", "帧率", "fps", "fps"),
    ("BRIGHTNESS", "CAP_PROP_BRIGHTNESS", "亮度", "slider", ""),
    ("CONTRAST", "CAP_PROP_CONTRAST", "对比度", "slider", ""),
    ("SATURATION", "CAP_PROP_SATURATION", "饱和度", "slider", ""),
    ("HUE", "CAP_PROP_HUE", "色调", "slider", ""),
    ("GAIN", "CAP_PROP_GAIN", "增益", "slider", ""),
    ("EXPOSURE", "CAP_PROP_EXPOSURE", "曝光", "slider", ""),
    ("AUTO_EXPOSURE", "CAP_PROP_AUTO_EXPOSURE", "自动曝光", "toggle", ""),
    ("WHITE_BALANCE", "CAP_PROP_WHITE_BALANCE", "白平衡", "slider", ""),
    ("SHARPNESS", "CAP_PROP_SHARPNESS", "锐度", "slider", ""),
    ("BACKLIGHT", "CAP_PROP_BACKLIGHT_COMPENSATION", "背光补偿", "slider", ""),
    ("FOCUS", "CAP_PROP_FOCUS", "对焦", "slider", ""),
    ("AUTOFOCUS", "CAP_PROP_AUTOFOCUS", "自动对焦", "toggle", ""),
    ("PAN", "CAP_PROP_PAN", "水平旋转", "slider", "°"),
    ("TILT", "CAP_PROP_TILT", "垂直旋转", "slider", "°"),
    ("ZOOM", "CAP_PROP_ZOOM", "变焦", "slider", "x"),
]

# 分辨率候选 (Orbbec DaBai DCW 实测支持)
RESOLUTIONS = [
    [320, 240], [640, 360], [640, 480], [800, 600],
    [1280, 720], [1280, 960], [1440, 1080], [1920, 1080],
]

CAP_DEFAULTS = {
    "FRAME_WIDTH": 640, "FRAME_HEIGHT": 480, "FPS": 30,
    "AUTO_EXPOSURE": 1, "AUTOFOCUS": 1,
}

# 各参数实测有效范围 (超出会被相机拒绝, apply 会报错不伪装成功)
# 由 probe_ranges() 实测校准, 不同相机可能不同
KNOWN_RANGES = {
    "BRIGHTNESS": (0, 50),
    "CONTRAST": (0, 64),
    "SATURATION": (0, 128),
    "HUE": (0, 255),
    "GAIN": (0, 100),
    "SHARPNESS": (0, 5),
    "FOCUS": (0, 100),
    "AUTOFOCUS": (0, 2),
    "AUTO_EXPOSURE": (0, 3),
    "FPS": (5, 60),
}


def probe_ranges() -> dict:
    """实测标定每个参数的有效范围 (写极大/极小值, 看相机夹到哪)。

    标定后会写回 KNOWN_RANGES 供 UI 用。调用时相机会被短暂改参数。
    """
    import cv2
    global KNOWN_RANGES
    with CAM._state_lock:
        cap = CAM.cap
    if cap is None:
        return {"ok": False, "error": "相机未运行"}
    out = {"ok": True, "ranges": {}, "originals": {}}
    for name, attr, cn, ui, unit in CAMERA_PROPS:
        pid = getattr(cv2, attr, None)
        if pid is None or ui == "resolution":
            continue
        try:
            orig = float(cap.get(pid))
        except Exception:
            continue
        out["originals"][name] = orig
        probe_failed = False
        try:
            cap.set(pid, 1e6)
            hi = float(cap.get(pid))
            cap.set(pid, -1e6)
            lo = float(cap.get(pid))
        except Exception:
            probe_failed = True
        finally:
            # 即使读取上限/下限失败，也必须尝试恢复探测前值，避免
            # 相机参数停留在刚写入的极端值。
            try:
                restored = cap.set(pid, orig)
                if restored is False:
                    out.setdefault("restore_errors", {})[name] = "相机拒绝恢复原值"
                    out["ok"] = False
            except Exception as e:
                out.setdefault("restore_errors", {})[name] = str(e)
                out["ok"] = False
        if probe_failed:
            continue
        if lo <= hi and hi > -0.5:
            KNOWN_RANGES[name] = (round(lo, 3), round(hi, 3))
            out["ranges"][name] = [round(lo, 3), round(hi, 3)]
    return out


def probe_capabilities() -> dict:
    """报告相机参数 (值来自采集线程的缓存, 避免并发读 cap.get 出错)。

    支持性判定: OpenCV 对不支持的属性返回 -1 或 0。
    可写性: 由 apply_camera_settings 实时反馈, 这里只标注"待验证"。
    """
    # 标记需要刷新缓存, 由采集线程在下一帧做 (最多 5 秒延迟)
    CAM._props_dirty = True
    props = CAM.cached_props or {}
    with CAM._state_lock:
        running = CAM.running
    result = {"ok": True, "params": [], "notes": []}
    if not running:
        result["notes"].append("相机未运行, 显示的是上次采集缓存值")

    for name, attr, cn, ui, unit in CAMERA_PROPS:
        v = props.get(name, -1.0)
        supported = v is not None and v > -0.5
        note = ""
        writable = supported
        if not supported:
            note = "该相机不支持此参数"
            writable = False
        elif name in ("WHITE_BALANCE", "PAN", "TILT"):
            note = "该相机只读, 无法修改"
            writable = False
        default = CAM.factory_defaults.get(name)
        result["params"].append({
            "name": name, "cn": cn, "ui": ui, "unit": unit,
            "supported": supported,
            "writable": writable,
            "current": round(float(v), 3) if supported else None,
            "default": round(float(default), 3) if default is not None else None,
            "changed": (supported and default is not None
                        and abs(float(v) - float(default)) > 1e-3),
            "range": list(KNOWN_RANGES.get(name, (0, 100)))
                    if ui not in ("resolution",) and name in KNOWN_RANGES
                    else None,
            "note": note,
        })

    # 分辨率候选 (实际测试 Orbbec DaBai DCW 支持这些)
    result["resolutions"] = RESOLUTIONS
    cur_w = int(props.get("FRAME_WIDTH", 0) or 0)
    cur_h = int(props.get("FRAME_HEIGHT", 0) or 0)
    result["current_resolution"] = [cur_w, cur_h]
    df_w = int(CAM.factory_defaults.get("FRAME_WIDTH", 640) or 640)
    df_h = int(CAM.factory_defaults.get("FRAME_HEIGHT", 480) or 480)
    result["default_resolution"] = [df_w, df_h]
    result["notes"].append("不支持的参数已禁用, 设置失败会明确报错, 不伪装成功")
    if CAM.factory_defaults:
        result["notes"].append("「默认值」为相机首次启动时的原厂设置")
    else:
        result["notes"].append("尚未捕获默认值 (相机刚启动, 稍等 5 秒)")
    return result


def apply_camera_settings(settings: dict) -> dict:
    """应用参数。分辨率变更需重启采集循环。"""
    import cv2
    results = {}
    need_restart = False
    new_w = new_h = 0

    with CAM._state_lock:
        cap = CAM.cap
    if cap is None:
        return {"ok": False, "error": "相机未运行, 请先点 RUN"}

    for name, attr, cn, ui, unit in CAMERA_PROPS:
        if name not in settings:
            continue
        val = settings[name]
        if ui == "resolution":
            continue  # 分辨率单独处理
        pid = getattr(cv2, attr, None)
        if pid is None:
            results[name] = {"ok": False, "error": "此 OpenCV 版本不支持该参数"}
            continue
        try:
            v = float(val)
        except Exception:
            results[name] = {"ok": False, "error": "参数值无效"}
            continue
        try:
            before = cap.get(pid)
            ok = cap.set(pid, v)
            after = cap.get(pid)
            if ok and abs((after or 0) - v) < max(1e-6, abs(v) * 0.05):
                results[name] = {"ok": True, "value": round(float(after), 3)}
            else:
                results[name] = {
                    "ok": False,
                    "error": f"设置被拒绝 (实际={after}, 期望={v})",
                }
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}

    # 分辨率: 需要重启采集
    if "FRAME_WIDTH" in settings and "FRAME_HEIGHT" in settings:
        try:
            new_w = int(settings["FRAME_WIDTH"])
            new_h = int(settings["FRAME_HEIGHT"])
            need_restart = True
        except Exception:
            results["FRAME_WIDTH"] = {"ok": False, "error": "分辨率无效"}

    if need_restart:
        try:
            CAM.stop()
            time.sleep(0.6)
            # 用新分辨率重启
            ok = CAM.start_with_size(CAM.index, new_w, new_h)
            if ok:
                results["FRAME_WIDTH"] = {"ok": True, "value": CAM.width}
                results["FRAME_HEIGHT"] = {"ok": True, "value": CAM.height}
            else:
                results["FRAME_WIDTH"] = {
                    "ok": False, "error": "分辨率切换失败, 已回退到 640x480"}
                CAM.start(CAM.index)
        except Exception as e:
            results["FRAME_WIDTH"] = {"ok": False, "error": str(e)}
            try:
                CAM.start(CAM.index)
            except Exception:
                pass

    ok_all = all(r.get("ok") for r in results.values()) if results else True
    # 改完立即刷新缓存, 让下次探测拿到最新值
    with CAM._state_lock:
        cap2 = CAM.cap
    if cap2 is not None:
        try:
            CAM._refresh_props(cap2)
            CAM._props_dirty = False
            CAM._props_last = time.time()
        except Exception:
            CAM._props_dirty = True
    return {"ok": ok_all, "results": results, **CAM.status()}


def reset_camera_defaults() -> dict:
    """把所有参数恢复到出厂默认值 (相机首次启动时捕获)。

    若未捕获默认, 退回 CAP_DEFAULTS。
    """
    import cv2
    with CAM._state_lock:
        cap = CAM.cap
    if cap is None:
        return {"ok": False, "error": "相机未运行, 请先点 RUN"}

    # 目标值: 优先出厂默认, 兜底 CAP_DEFAULTS
    target = {}
    for name, _attr, _cn, ui, _u in CAMERA_PROPS:
        if ui == "resolution":
            continue
        if name in CAM.factory_defaults:
            target[name] = CAM.factory_defaults[name]
        elif name in CAP_DEFAULTS:
            target[name] = CAP_DEFAULTS[name]
    # 分辨率
    df_w = int(CAM.factory_defaults.get("FRAME_WIDTH", CAP_DEFAULTS["FRAME_WIDTH"]))
    df_h = int(CAM.factory_defaults.get("FRAME_HEIGHT", CAP_DEFAULTS["FRAME_HEIGHT"]))

    results = {}
    for name, attr, cn, ui, _u in CAMERA_PROPS:
        if ui == "resolution" or name not in target:
            continue
        pid = getattr(cv2, attr, None)
        if pid is None:
            continue
        try:
            v = float(target[name])
            before = cap.get(pid)
            ok = cap.set(pid, v)
            after = cap.get(pid)
            if ok and abs((after or 0) - v) < max(1e-6, abs(v) * 0.05):
                results[name] = {"ok": True, "value": round(float(after), 3)}
            else:
                results[name] = {
                    "ok": False,
                    "error": f"恢复失败 (实际={after}, 期望={v})",
                }
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}

    # 分辨率: 需要重启采集
    need_res = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) != df_w or
                int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) != df_h)
    if need_res:
        try:
            CAM.stop()
            time.sleep(0.6)
            ok = CAM.start_with_size(CAM.index, df_w, df_h)
            if ok:
                results["FRAME_WIDTH"] = {"ok": True, "value": CAM.width}
                results["FRAME_HEIGHT"] = {"ok": True, "value": CAM.height}
            else:
                results["FRAME_WIDTH"] = {"ok": False,
                                          "error": "分辨率恢复失败"}
                CAM.start(CAM.index)
        except Exception as e:
            results["FRAME_WIDTH"] = {"ok": False, "error": str(e)}
            try:
                CAM.start(CAM.index)
            except Exception:
                pass

    ok_all = all(r.get("ok") for r in results.values()) if results else True
    # 恢复完立即刷新缓存
    with CAM._state_lock:
        cap2 = CAM.cap
    if cap2 is not None:
        try:
            CAM._refresh_props(cap2)
            CAM._props_dirty = False
            CAM._props_last = time.time()
        except Exception:
            CAM._props_dirty = True
    return {"ok": ok_all, "results": results, "restored": target,
            **CAM.status()}


# ============================================================
# 深度图 / 测距 (OrbbecSDK · 复用 OrbbecCameraProject 的 ctypes 封装)
# ============================================================
# 注意: 深度管线与 cv2.VideoCapture 抢同一个 USB 设备, 同一时刻只能一方打开。
#       开启深度模式时会自动停 RGB 采集, 关闭后恢复。

def _make_depth_module():
    """懒加载 orbbec_depth (SDK DLL 未就绪时也不影响其它功能)。"""
    global DEPTH
    if DEPTH is not None:
        return DEPTH
    try:
        from orbbec_depth import OrbbecDepth, OrbbecError
        DEPTH = OrbbecDepth()
        return DEPTH
    except Exception as e:
        print(f"[WARN] 深度模块不可用: {e}")
        return None


DEPTH = None
DEPTH_MODE = {"active": False}


def _depth_start() -> dict:
    """开深度模式: 先停 RGB(cv2), 再开 Orbbec RGB+Depth 管线。"""
    d = _make_depth_module()
    if d is None:
        return {"ok": False,
                "error": "深度模块不可用（缺少 sdk_runtime/OrbbecSDK.dll）"}
    if DEPTH_MODE["active"]:
        return {"ok": True, **d.status()}
    # 释放 cv2 的 RGB 采集 (同一设备)
    try:
        CAM.stop()
    except Exception:
        pass
    try:
        d.start(640, 360, 30)
    except Exception as e:
        # 深度启动失败要把 RGB 恢复回去, 否则相机停在半路
        # 设备刚被释放, DirectShow 需要一点时间才能重新打开, 带重试
        for i in range(5):
            time.sleep(0.6)
            try:
                if CAM.start(CAM.index):
                    break
            except Exception:
                pass
        return {"ok": False,
                "error": f"深度管线启动失败: {e}。"
                         f"常见原因: 相机插在 USB2.0 口(需换 USB3.0 蓝色口)"}
    DEPTH_MODE["active"] = True
    return {"ok": True, **d.status()}


def _depth_stop() -> dict:
    d = DEPTH
    if d is not None:
        try:
            d.stop()
        except Exception:
            pass
    DEPTH_MODE["active"] = False
    # 恢复 RGB 采集
    try:
        CAM.start(CAM.index)
    except Exception:
        pass
    return {"ok": True, "depth_running": False}


# 机械臂视觉项目只更新本地仿真状态；此处没有任何实体机械臂驱动通道。
ROBOT_PROJECTS = {
    "yolo-sort": {"name": "YOLO 视觉分拣", "stage": 1,
                  "description": "检测目标并模拟抓取、分类放置"},
    "color-sort": {"name": "颜色分拣工作站", "stage": 2,
                   "description": "按 HSV 颜色检测结果模拟分拣"},
    "qr-route": {"name": "二维码工单分流", "stage": 3,
                 "description": "解析 SORT:A/B/C 指令并模拟分流"},
    "depth-pick": {"name": "深度相机定点抓取", "stage": 4,
                   "description": "读取真实深度距离并生成未标定仿真点"},
}
_ROBOT_PROJECT_HISTORY: list[dict] = []


def _project_no_target(project_id: str, message: str, data=None) -> dict:
    return {"ok": False, "project": project_id,
            "project_name": ROBOT_PROJECTS[project_id]["name"],
            "mode": "simulation-only", "error": message,
            "robot_state": dict(_ROBOT_SIM_STATE), "data": data}


def _project_pick_place(project_id: str, target_name: str, bin_name: str,
                        cx: int, cy: int, frame_width: int, frame_height: int,
                        z_mm: float = 35.0, source: str = "RGB") -> dict:
    """Map a pixel to an explicitly uncalibrated demo point and simulate a cycle."""
    width = max(1, int(frame_width))
    height = max(1, int(frame_height))
    cx = min(max(0, int(cx)), width - 1)
    cy = min(max(0, int(cy)), height - 1)
    # Demo-only linear image-plane mapping; never valid as a real robot transform.
    x_mm = round((cx / max(1, width - 1) - 0.5) * 600.0, 1)
    y_mm = round((0.5 - cy / max(1, height - 1)) * 400.0, 1)
    bin_positions = {
        "A": [220.0, -120.0, 35.0],
        "B": [0.0, -200.0, 35.0],
        "C": [-220.0, -120.0, 35.0],
        "OTHER": [0.0, 220.0, 35.0],
    }
    bin_key = bin_name.rsplit(" ", 1)[-1].upper()
    place = bin_positions.get(bin_key, bin_positions["OTHER"])
    pick = [x_mm, y_mm, round(float(z_mm), 1)]
    operation = {
        "project": project_id,
        "project_name": ROBOT_PROJECTS[project_id]["name"],
        "target": target_name,
        "destination": bin_name,
        "source": source,
        "pixel": [cx, cy],
        "pick_point_mm_demo": pick,
        "place_point_mm_demo": place,
        "calibrated": False,
        "steps": ["识别目标", "映射演示抓取点", "模拟接近", "模拟夹取",
                  "模拟搬运", "模拟放置", "模拟松开"],
        "mode": "simulation-only",
    }
    with _ROBOT_SIM_LOCK:
        _ROBOT_SIM_STATE.update(
            state="simulated-pick-place", position_mm=place,
            last_command=f"project {project_id}: {target_name} -> {bin_name}",
            last_project=project_id, last_target=target_name,
            last_destination=bin_name, calibrated=False)
        operation["robot_state"] = dict(_ROBOT_SIM_STATE)
        _ROBOT_PROJECT_HISTORY.append(operation)
        del _ROBOT_PROJECT_HISTORY[:-50]
    return {"ok": True, **operation}


def run_robot_project(project_id: str) -> dict:
    """Run one vision-to-robot demonstration against live sensors, sim only."""
    project_id = str(project_id or "").strip().lower()
    if project_id not in ROBOT_PROJECTS:
        return {"ok": False, "error": "未知项目编号。"}
    spec = ROBOT_PROJECTS[project_id]

    if project_id == "yolo-sort":
        result = YOLO.detect_once(only_fruit=False, conf=0.35)
        if not result.get("ok"):
            return _project_no_target(project_id, result.get("error", "YOLO 检测失败"), result)
        items = result.get("results") or []
        if not items:
            return _project_no_target(project_id, "当前画面未检出目标；未生成抓取动作。", result)
        item = items[0]
        label = item.get("label_en", "")
        route = {"apple": "A", "banana": "B", "orange": "C"}.get(label, "OTHER")
        destination = f"料箱 {route}"
        bbox = item.get("bbox") or [0, 0, 0, 0]
        x, y, w, h = (list(bbox) + [0, 0, 0, 0])[:4]
        cam = CAM.status()
        out = _project_pick_place(project_id, item.get("label_cn", label), destination,
                                  x + w // 2, y + h // 2,
                                  cam.get("width", 640), cam.get("height", 480),
                                  source="YOLOv8")
        out["detection"] = item
        out["note"] = "像素到机械臂坐标为未标定演示映射；无实体动作。"
        return out

    if project_id == "color-sort":
        result = COLOR.detect_once()
        if not result.get("ok"):
            return _project_no_target(project_id, result.get("error", "颜色检测失败"), result)
        items = result.get("detections") or []
        if not items:
            return _project_no_target(project_id, "当前画面未检出合格颜色区域；未生成抓取动作。", result)
        item = max(items, key=lambda d: int(d.get("area", 0)))
        key = str(item.get("color", "")).lower()
        if "红" in key or key == "red":
            route = "A"
        elif "绿" in key or key == "green":
            route = "B"
        elif any(token in key for token in ("蓝", "靛")) or key == "blue":
            route = "C"
        else:
            route = "OTHER"
        cam = CAM.status()
        out = _project_pick_place(project_id, key or "颜色目标", f"料箱 {route}",
                                  item.get("cx", 0), item.get("cy", 0),
                                  cam.get("width", 640), cam.get("height", 480),
                                  source="HSV 颜色区域")
        out["detection"] = item
        out["note"] = "颜色识别使用当前摄像头帧；像素映射未做机械臂标定。"
        return out

    if project_id == "qr-route":
        result = QR.detect_once()
        if not result.get("ok"):
            return _project_no_target(project_id, result.get("error", "二维码识别失败"), result)
        items = result.get("results") or []
        if not items:
            return _project_no_target(project_id, "未解出二维码；请使用内容 SORT:A、SORT:B 或 SORT:C 的测试码。", result)
        item = items[0]
        text = str(item.get("text", "")).strip()
        match = re.fullmatch(r"(?:SORT|BIN)\s*[:=_-]\s*([ABC])", text, re.IGNORECASE)
        if not match and text.upper() in {"A", "B", "C"}:
            route = text.upper()
        elif match:
            route = match.group(1).upper()
        else:
            return _project_no_target(project_id,
                f"二维码已读到「{text[:80]}」，但分流规则只接受 SORT:A/B/C 或 A/B/C；未移动模拟机械臂。", result)
        bbox = item.get("bbox") or [0, 0, 0, 0]
        x, y, w, h = (list(bbox) + [0, 0, 0, 0])[:4]
        cam = CAM.status()
        out = _project_pick_place(project_id, f"工单 {text[:80]}", f"料箱 {route}",
                                  x + w // 2, y + h // 2,
                                  cam.get("width", 640), cam.get("height", 480),
                                  source="二维码工单")
        out["decoded_text"] = text
        out["note"] = "二维码可解码不等于真实 PLC/机械臂工单确认；本流程仅模拟。"
        return out

    # Stage 4: only consume an already-running depth stream. Do not steal the
    # shared USB camera from RGB or stop the currently active YOLO loop.
    if not DEPTH_MODE.get("active") or DEPTH is None:
        return {"ok": False, "project": project_id, "project_name": spec["name"],
                "mode": "simulation-only", "blocked": True,
                "error": "深度管线未运行。为避免中断当前 RGB/YOLO，本项目不会自动切换相机模式。",
                "next_step": "先在“深度图/测距”面板手动开启深度模式，再运行本项目。",
                "robot_state": dict(_ROBOT_SIM_STATE)}
    depth, _scale = DEPTH.get_depth()
    if depth is None:
        return _project_no_target(project_id, "深度管线已开启但尚无有效深度帧。")
    h, w = depth.shape[:2]
    cx, cy = w // 2, h // 2
    detail = DEPTH.measure_detail_mm(cx, cy, roi=8)
    distance = detail.get("distance_mm")
    if distance is None or float(distance) <= 0:
        return _project_no_target(project_id, "深度图中心没有有效距离；未生成抓取动作。", detail)
    out = _project_pick_place(project_id, "深度图中心候选点", "料箱 A",
                              cx, cy, w, h, z_mm=float(distance), source="Orbbec 深度帧")
    out["depth"] = {"distance_mm": round(float(distance), 1),
                    "valid_pixels": detail.get("valid_pixels", 0),
                    "local_valid_ratio": detail.get("local_valid_ratio", 0),
                    "fallback_used": detail.get("fallback_used", False),
                    "depth_size": [w, h]}
    out["note"] = "距离来自真实深度帧；XY 到机器人基座的外参/手眼标定未配置，抓取点仅为仿真。"
    return out


def _build_diagnostics() -> dict:
    """Read-only local health summary; deliberately does not enumerate or open devices."""
    checks: list[dict] = []

    def add(key: str, label: str, state: str, detail: str) -> None:
        checks.append({"key": key, "label": label, "state": state, "detail": str(detail)})

    try:
        import cv2
        cv_version = cv2.__version__
        add("opencv", "OpenCV", "ok", f"已加载 · {cv_version}")
    except Exception as exc:
        add("opencv", "OpenCV", "error", f"导入失败 · {exc}")

    camera = CAM.status()
    if camera.get("running"):
        add("rgb", "RGB 采集", "ok",
            f"运行中 · {camera.get('width')}×{camera.get('height')} · "
            f"{camera.get('fps')} fps · {camera.get('frames')} 帧")
    elif camera.get("starting"):
        add("rgb", "RGB 采集", "warn", "正在启动；本诊断不会更改采集状态")
    else:
        add("rgb", "RGB 采集", "idle", "当前未启动；诊断不会尝试访问摄像头")
    if camera.get("last_error"):
        add("rgb_error", "最近采集错误", "error", camera["last_error"])

    if DEPTH is None:
        add("depth", "Orbbec 深度模块", "idle",
            "尚未按需加载；未探测 USB、未启动 SDK 管线")
    else:
        depth = DEPTH.status()
        if depth.get("depth_running") and depth.get("depth_data_valid"):
            add("depth", "Orbbec 深度模块", "ok",
                f"采集正常 · {depth.get('depth_fps', 0)} fps · 有效像素 "
                f"{depth.get('valid_ratio', 0) * 100:.0f}% · 格式 {depth.get('depth_format')}")
        elif depth.get("depth_running"):
            add("depth", "Orbbec 深度模块", "warn",
                f"SDK 已收帧但没有有效深度值 · {depth.get('depth_fps', 0)} fps · "
                f"格式 {depth.get('depth_format')} · 原始范围 "
                f"{depth.get('depth_raw_min')}..{depth.get('depth_raw_max')} · "
                "当前无法测距；检查 USB 连接/数据线，并用 OrbbecViewer 独立复测")
        else:
            add("depth", "Orbbec 深度模块", "idle",
                "已加载但未运行；诊断不会启动 SDK 管线")
        if depth.get("depth_error"):
            add("depth_error", "最近深度错误", "error", depth["depth_error"])

    def module_present(name: str) -> bool:
        try:
            return importlib.util.find_spec(name) is not None
        except (ImportError, ValueError, AttributeError):
            return False

    ocr_ready = module_present("rapidocr_onnxruntime")
    add("ocr", "OCR 依赖", "ok" if ocr_ready else "warn",
        "RapidOCR 可用" if ocr_ready else "未安装 rapidocr_onnxruntime；其它功能不受影响")
    yolo_ready = module_present("ultralytics") and (ROOT / "yolov8n.pt").is_file()
    add("yolo", "YOLO 依赖", "ok" if yolo_ready else "warn",
        "ultralytics 与本地 yolov8n.pt 均可用" if yolo_ready
        else "缺少 ultralytics 或项目内 yolov8n.pt；其它功能不受影响")
    add("qr", "二维码识别", "ok" if "cv2" in locals() else "error",
        "使用 OpenCV QRCodeDetector；无需额外训练模型")

    ffmpeg = _find_ffmpeg()
    add("ffmpeg", "H.264 转码", "ok" if ffmpeg else "warn",
        f"可用 · {ffmpeg}" if ffmpeg else "未找到 FFmpeg；非 H.264 录像将保留原文件并提示")

    storage_root = SAVE_DIR if SAVE_DIR.exists() else ROOT
    writable = os.access(storage_root, os.W_OK)
    try:
        free_gb = shutil.disk_usage(storage_root).free / (1024 ** 3)
        storage_detail = (f"捕获目录 {'存在' if SAVE_DIR.exists() else '尚未创建'} · "
                          f"{'可写' if writable else '不可写'} · 剩余 {free_gb:.1f} GB · {SAVE_DIR}")
    except OSError as exc:
        storage_detail = f"无法读取存储空间 · {exc} · {SAVE_DIR}"
        writable = False
    add("storage", "截图与录像存储", "ok" if writable else "error", storage_detail)

    blocking = any(item["state"] == "error" for item in checks)
    warnings = any(item["state"] == "warn" for item in checks)
    return {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "overall": "error" if blocking else ("warn" if warnings else "ok"),
        "checks": checks,
        "privacy_note": "仅采集本机软件状态与存储路径；不枚举 USB、不启动相机、不包含图像。",
    }


# ============================================================
# HTTP 处理器
# ============================================================

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # -- 工具 --

    def _send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 --

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(load_html())
        elif path in ("/style.css", "/app.js"):
            self._send_static(path.lstrip("/"))
        elif path == "/status":
            self._send_json(CAM.status())
        elif path == "/diagnostics":
            self._send_json(_build_diagnostics())
        elif path == "/stream":
            self._stream()
        elif path == "/snapshot":
            self._snapshot()
        elif path == "/files":
            self._send_json({"files": _list_files()})
        elif path == "/color/status":
            self._send_json(COLOR.status())
        elif path == "/color/annotated":
            data = COLOR.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未识别"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/color/pick":
            self._color_pick(urlparse(self.path).query)
        elif path == "/ocr/status":
            self._send_json(OCR.status())
        elif path == "/ocr/annotated":
            data = OCR.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未识别"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/qr/status":
            self._send_json(QR.status())
        elif path == "/qr/annotated":
            data = QR.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未识别"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/barcode/status":
            self._send_json(BARCODE.status())
        elif path == "/barcode/annotated":
            data = BARCODE.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未识别"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/gesture/status":
            self._send_json({"ok": True, **GESTURE.status()})
        elif path == "/gesture/menu":
            m = _GESTURE_MENU
            self._send_json({"ok": True, "menu": {
                k: m[k] for k in ("state", "armed_index", "armed_name",
                                  "last_action", "result", "modes")}})
        elif path == "/gesture/air/status":
            a = _AIR
            self._send_json({"ok": True, "air": {
                "region": a["region"], "action": a["action"],
                "action_cn": _AIR_ACTION_CN.get(a["action"], a["action"]),
                "log": list(a["log"])[:12], "in_region": a["in_region"]}})
        elif path == "/gesture/annotated":
            data = GESTURE.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未识别"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/arm/state":
            self._send_json({"ok": True, **ARM.get_state(), **ARM.match_state()})
        elif path == "/arm/match":
            self._send_json({"ok": True, **ARM.match_state()})
        elif path == "/yolo/status":
            self._send_json(YOLO.status())
        elif path == "/yolo/annotated":
            data = YOLO.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未识别"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/depth/status":
            d = DEPTH
            if d is None:
                # 模块还没加载 (懒加载), 也返回 200 让前端能正常显示"未启动"
                self._send_json({
                    "ok": True,
                    "mode": False,
                    "depth_running": False,
                    "depth_pipeline_started": False,
                    "depth_fps": 0.0,
                    "depth_frames": 0,
                    "depth_scale": 1.0,
                    "color_size": [0, 0],
                    "depth_size": [0, 0],
                    "depth_error": "",
                    "note": "点「开启深度模式」后才加载 Orbbec SDK",
                })
            else:
                self._send_json({"ok": True, "mode": DEPTH_MODE["active"],
                                 **d.status()})
        elif path == "/depth/frame":
            self._depth_frame()
        elif path == "/depth/color-frame":
            self._depth_color_frame()
        elif path == "/depth/pointcloud":
            self._depth_pointcloud(urlparse(self.path).query)
        elif path == "/depth/measure":
            self._depth_measure(urlparse(self.path).query)
        elif path == "/measure/distance":
            self._measure_distance(urlparse(self.path).query)
        elif path == "/measure/angle":
            self._measure_angle(urlparse(self.path).query)
        elif path == "/measure/area":
            self._measure_area(urlparse(self.path).query)
        elif path == "/camera/capabilities":
            self._send_json(probe_capabilities())
        elif path == "/enhance/status":
            self._send_json({"ok": True, **ENH.settings()})
        elif path == "/timelapse/status":
            self._send_json({"ok": True, **TIME.status()})
        elif path == "/haar/status":
            self._send_json(HAAR.status())
        elif path == "/haar/annotated":
            data = HAAR.get_annotated_jpeg()
            if data is None:
                self._send_json({"error": "尚未检测"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path == "/track/status":
            self._send_json(TRACK.status())
        elif path == "/motion/status":
            self._send_json(MOTION.status())
        elif path == "/robot/projects/status":
            with _ROBOT_SIM_LOCK:
                robot_state = dict(_ROBOT_SIM_STATE)
                history = list(_ROBOT_PROJECT_HISTORY[-8:])
            depth_status = DEPTH.status() if DEPTH is not None else {
                "depth_running": False, "depth_frames": 0, "depth_error": ""}
            self._send_json({"ok": True, "mode": "simulation-only",
                             "projects": [{"id": key, **value}
                                          for key, value in ROBOT_PROJECTS.items()],
                             "robot_state": robot_state,
                             "history": history,
                             "depth_mode_active": bool(DEPTH_MODE.get("active")),
                             "depth": depth_status})
        elif path == "/motion/snapshot":
            data = MOTION.get_motion_jpeg()
            if data is None:
                self._send_json({"error": "暂无报警截图"}, 404)
            else:
                self._send_bytes(data, "image/jpeg")
        elif path.startswith("/file/"):
            rest = path[len("/file/"):]
            download = rest.endswith("/download")
            if download:
                rest = rest[: -len("/download")]
            self._serve_file(rest, download=download)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/console":
            body = self._read_json_body()
            result = execute_console_command(body.get("command", ""))
            self._send_json(result, 200 if result.get("ok") else 400)
        elif path == "/robot/projects/run":
            body = self._read_json_body()
            result = run_robot_project(body.get("project", ""))
            self._send_json(result, 200 if result.get("ok") else 409 if result.get("blocked") else 400)
        elif path == "/start":
            ok = CAM.start(CAM.index)
            self._send_json({"ok": ok, **CAM.status()})
        elif path == "/stop":
            CAM.stop()
            self._send_json({"ok": True, **CAM.status()})
        elif path == "/record/start":
            ok = CAM.start_record()
            self._send_json({"ok": ok, **CAM.status()})
        elif path == "/record/stop":
            ok = CAM.stop_record()
            self._send_json({"ok": ok, **CAM.status()})
        elif path == "/color/toggle":
            self._color_toggle()
        elif path == "/color/detect":
            self._send_json(COLOR.detect_once())
        elif path == "/ocr":
            body = self._read_json_body()
            only = bool(body.get("only_alnum", False))
            self._send_json(OCR.detect_once(only_alnum=only))
        elif path == "/qr":
            self._send_json(QR.detect_once())
        elif path == "/barcode":
            self._send_json(BARCODE.detect_once())
        elif path == "/gesture":
            self._send_json(GESTURE.detect_once())
        elif path == "/gesture/control":
            self._send_json(self._gesture_control())
        elif path == "/gesture/menu":
            self._send_json(self._gesture_menu())
        elif path == "/gesture/air":
            self._send_json(self._gesture_air())
        elif path == "/gesture/air/config":
            body = self._read_json_body()
            a = _AIR
            try:
                rg = body.get("region") or {}
                for k in ("x", "y", "z"):
                    if isinstance(rg.get(k), list) and len(rg[k]) == 2:
                        a["region"][k] = [float(rg[k][0]), float(rg[k][1])]
                if "action" in body:
                    a["action"] = str(body["action"])
            except Exception:
                pass
            self._send_json({"ok": True, "region": a["region"],
                             "action": a["action"],
                             "action_cn": _AIR_ACTION_CN.get(a["action"], a["action"])})
        elif path == "/arm/autosort":
            self._send_json(self._arm_autosort())
        elif path == "/arm/joints":
            body = self._read_json_body()
            self._send_json(ARM.set_joints(body.get("angles", [])))
        elif path == "/arm/move":
            body = self._read_json_body()
            self._send_json(ARM.move_to(body.get("x", 0), body.get("y", 0),
                                        body.get("z", 0)))
        elif path == "/arm/home":
            self._send_json(ARM.home())
        elif path == "/arm/stop":
            self._send_json(ARM.stop())
        elif path == "/arm/emergency":
            self._send_json(ARM.emergency())
        elif path == "/arm/reset":
            self._send_json(ARM.reset())
        elif path == "/arm/real":
            body = self._read_json_body()
            self._send_json(ARM.configure_real(
                port=str(body.get("port", "")), model=str(body.get("model", "")),
                protocol=str(body.get("protocol", "pca9685")),
                baud=int(body.get("baud", 115200)),
                enable=bool(body.get("enable", False))))
        elif path == "/arm/match/start":
            body = self._read_json_body()
            self._send_json(ARM.match_start(str(body.get("task", "pick"))))
        elif path == "/arm/match/stop":
            self._send_json(ARM.match_stop())
        elif path == "/arm/match/score":
            body = self._read_json_body()
            self._send_json(ARM.match_score(int(body.get("delta", 1))))
        elif path == "/yolo":
            body = self._read_json_body()
            only_fruit = bool(body.get("only_fruit", False))
            conf = float(body.get("conf", 0.35))
            self._send_json(YOLO.detect_once(only_fruit=only_fruit, conf=conf))
        elif path == "/yolo/model":
            body = self._read_json_body()
            self._send_json(YOLO.load_custom_model(str(body.get("path", ""))))
        elif path == "/depth/start":
            self._send_json(_depth_start())
        elif path == "/depth/stop":
            self._send_json(_depth_stop())
        elif path == "/camera/settings":
            body = self._read_json_body()
            self._send_json(apply_camera_settings(body))
        elif path == "/camera/reset":
            self._send_json(reset_camera_defaults())
        elif path == "/camera/reset-param":
            body = self._read_json_body()
            name = body.get("name", "")
            with CAM._state_lock:
                default = CAM.factory_defaults.get(name)
            if default is None:
                self._send_json({"ok": False,
                                 "error": f"{name} 无默认值记录"}, 400)
            else:
                self._send_json(apply_camera_settings({name: default}))
        elif path == "/camera/probe-ranges":
            self._send_json(probe_ranges())
        elif path == "/enhance/settings":
            body = self._read_json_body()
            self._send_json(ENH.configure(body))
        elif path == "/timelapse/start":
            body = self._read_json_body()
            try:
                interval = float(body.get("interval", 5.0))
                count = int(body.get("count", 0))
            except Exception:
                interval, count = 5.0, 0
            self._send_json(TIME.start(interval, count))
        elif path == "/timelapse/stop":
            self._send_json(TIME.stop())
        elif path == "/motion/start":
            body = self._read_json_body()
            if "sensitivity" in body:
                try:
                    MOTION.sensitivity = max(0, min(100, int(body["sensitivity"])))
                except Exception:
                    pass
            ok = MOTION.start()
            self._send_json({"ok": ok, **MOTION.status()})
        elif path == "/motion/stop":
            MOTION.stop()
            self._send_json({"ok": True, **MOTION.status()})
        elif path == "/motion/settings":
            body = self._read_json_body()
            with MOTION.lock:
                if "sensitivity" in body:
                    try:
                        MOTION.sensitivity = max(0, min(100, int(body["sensitivity"])))
                    except Exception:
                        pass
                if "min_area" in body:
                    try:
                        MOTION.min_area_ratio = max(0.05, min(20.0, float(body["min_area"])))
                    except Exception:
                        pass
                if "cooldown" in body:
                    try:
                        MOTION.cooldown_s = max(0.5, min(60.0, float(body["cooldown"])))
                    except Exception:
                        pass
                if "save_snapshot" in body:
                    MOTION.save_snapshot = bool(body["save_snapshot"])
                if "smart_recognize" in body:
                    MOTION.smart_recognize = bool(body["smart_recognize"])
            self._send_json({"ok": True, **MOTION.status()})
        elif path == "/motion/clear":
            with MOTION.lock:
                MOTION.alarms = []
                MOTION.alarm_count = 0
            self._send_json({"ok": True, **MOTION.status()})
        elif path == "/haar/detect":
            body = self._read_json_body()
            target = body.get("target", "face_front")
            self._send_json(HAAR.detect_once(target=target))
        elif path == "/track/start":
            body = self._read_json_body()
            try:
                cx = int(float(body.get("x", 0)))
                cy = int(float(body.get("y", 0)))
                size = int(float(body.get("size", 120)))
            except Exception:
                self._send_json({"ok": False, "error": "坐标无效"}, 400)
                return
            self._send_json(TRACK.start(cx, cy, size))
        elif path == "/track/stop":
            TRACK.stop()
            self._send_json({"ok": True, **TRACK.status()})
        elif path == "/color/settings":
            self._color_settings()
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/file/"):
            name = path[len("/file/"):]
            p = _safe_file(name)
            if p is None:
                self._send_json({"ok": False, "error": "文件不存在或非法文件名"}, 404)
                return
            try:
                p.unlink()
                print(f"[DEL] 已删除 {p.name}")
                self._send_json({"ok": True, "deleted": p.name})
            except Exception as e:
                self._send_json({"ok": False, "error": f"删除失败: {e}"}, 500)
        else:
            self._send_json({"error": "not found"}, 404)

    # -- 具体实现 --

    def _send_static(self, name: str) -> None:
        """服务项目根目录下的前端静态文件 (index.html / style.css / app.js)。"""
        p = (WEB_DIR / name).resolve()
        if not str(p).startswith(str(WEB_DIR.resolve())) or not p.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_file(self, name: str, download: bool = False) -> None:
        """服务 captures/ 内的文件, 支持 HTTP Range (视频拖动/续传必需)。"""
        p = _safe_file(name)
        if p is None:
            self._send_json({"error": "文件不存在或非法文件名"}, 404)
            return
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        try:
            file_size = p.stat().st_size
        except OSError as e:
            self._send_json({"error": f"读取文件失败: {e}"}, 500)
            return

        range_header = self.headers.get("Range")
        start, end = 0, file_size - 1
        is_partial = False
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = int(m.group(2))
                if start >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, file_size - 1)
                is_partial = True

        length = max(0, end - start + 1)
        self.send_response(206 if is_partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if is_partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        if download:
            self.send_header(
                "Content-Disposition", f'attachment; filename="{p.name}"'
            )
        else:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()

        try:
            with open(p, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    # -- 颜色识别 --

    def _read_json_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > 65536:
            return {}
        try:
            raw = self.rfile.read(n)
            return json.loads(raw.decode("utf-8", errors="replace")) or {}
        except Exception:
            return {}

    def _color_toggle(self) -> None:
        body = self._read_json_body()
        want = body.get("enable")
        if want is None:            # 未指定则取反
            want = not COLOR.is_enabled()
        want = bool(want)
        if want:
            if not CAM.status()["running"]:
                self._send_json({
                    "ok": False,
                    "error": "相机未启动，请先点击 RUN",
                    **COLOR.status(),
                })
                return
            ok = COLOR.start(CAM)
        else:
            ok = COLOR.stop()
        self._send_json({"ok": ok, **COLOR.status()})

    def _color_settings(self) -> None:
        body = self._read_json_body()
        with COLOR.lock:
            if "sensitivity" in body:
                try:
                    COLOR.sensitivity = max(0, min(100, int(body["sensitivity"])))
                except Exception:
                    pass
            if "min_area" in body:
                try:
                    COLOR.min_area = max(50, min(200000, int(body["min_area"])))
                except Exception:
                    pass
        self._send_json({"ok": True, **COLOR.status()})

    def _color_pick(self, query: str) -> None:
        from urllib.parse import parse_qs
        q = parse_qs(query)
        try:
            x = int(q.get("x", ["0"])[0])
            y = int(q.get("y", ["0"])[0])
            roi = int(q.get("roi", ["6"])[0])
        except Exception:
            self._send_json({"ok": False, "error": "坐标参数无效"}, 400)
            return
        if not CAM.status()["running"]:
            self._send_json({"ok": False, "error": "相机未启动"}, 400)
            return
        res = COLOR.pick(x, y, roi)
        if res is None:
            self._send_json({"ok": False, "error": "暂无画面"}, 400)
        elif "error" in res:
            self._send_json({"ok": False, "error": res["error"]}, 400)
        else:
            self._send_json({"ok": True, "pick": res})

    def _stream(self) -> None:
        import cv2
        self.send_response(200)
        self.send_header(
            "Content-Type", "multipart/x-mixed-replace; boundary=frame"
        )
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                frame = None
                # 深度模式: 直接出彩色化深度图
                if DEPTH_MODE.get("active") and DEPTH is not None:
                    try:
                        cm = DEPTH.colorize_depth()
                        if cm is not None:
                            center = DEPTH.center_mm()
                            txt = "深度模式 (JET)  "
                            if center:
                                txt += f"中心 {center:.0f} mm"
                            put_text_cn(cm, txt, (10, cm.shape[0] - 22),
                                        (235, 235, 235), 17)
                            ok, buf = cv2.imencode(
                                ".jpg", cm,
                                [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                            if ok:
                                frame = buf.tobytes()
                    except Exception:
                        frame = None
                if frame is None:
                    frame = CAM.get_jpeg()
                if frame is None:
                    # 相机未启动: 周期性发占位图, 让 <img> 不会一直挂住
                    frame = PLACEHOLDER_JPEG
                    time.sleep(0.5)
                else:
                    # 追踪框叠加到显示帧 (不改原始帧, 录像仍是干净的)
                    if TRACK.enabled:
                        try:
                            display = CAM.get_display()
                            if display is not None:
                                TRACK.draw(display)
                                ok3, buf3 = cv2.imencode(
                                    ".jpg", display,
                                    [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                                if ok3:
                                    frame = buf3.tobytes()
                        except Exception:
                            pass
                    time.sleep(0.03)
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(
                    f"Content-Length: {len(frame)}\r\n\r\n".encode()
                )
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass
        except Exception:
            pass

    def _snapshot(self) -> None:
        path = CAM.snapshot()
        if path is None:
            self._send_json({"ok": False,
                             "error": "相机未运行或无画面"}, 400)
            return
        data = path.read_bytes()
        self._send_bytes(data, "image/jpeg")

    def _depth_frame(self) -> None:
        """返回彩色化深度图 (JET 伪彩) JPEG。"""
        import cv2
        d = DEPTH
        if d is None:
            self._send_json({"error": "深度模块未加载"}, 503)
            return
        if not DEPTH_MODE["active"]:
            self._send_json({"error": "深度模式未开启"}, 400)
            return
        cm = d.colorize_depth()
        if cm is None:
            self._send_json({"error": "暂无深度帧"}, 503)
            return
        # 左下角叠加测距信息
        center = d.center_mm()
        st = d.status()
        vr = st.get("valid_ratio")
        txt = "深度图 (JET)   蓝=近 红=远   有效范围 0~4000mm"
        if center:
            txt += f"   中心 {center:.0f} mm"
        if vr is not None:
            txt += f"   有效 {vr*100:.0f}%"
        put_text_cn(cm, txt, (10, cm.shape[0] - 22), (235, 235, 235), 17)
        ok, buf = cv2.imencode(".jpg", cm, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            self._send_json({"error": "深度图编码失败"}, 500)
            return
        self._send_bytes(buf.tobytes(), "image/jpeg")

    def _depth_color_frame(self) -> None:
        """返回深度模式下的彩色帧 (Orbbec RGB) JPEG, 用于双窗口。"""
        import cv2
        d = DEPTH
        if d is None:
            self._send_json({"error": "深度模块未加载"}, 503)
            return
        if not DEPTH_MODE["active"]:
            self._send_json({"error": "深度模式未开启"}, 400)
            return
        color = d.get_color()
        if color is None:
            self._send_json({"error": "暂无彩色帧"}, 503)
            return
        put_text_cn(color, "彩色 (RGB)", (10, color.shape[0] - 22),
                    (235, 235, 235), 17)
        ok, buf = cv2.imencode(".jpg", color, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        if not ok:
            self._send_json({"error": "彩色帧编码失败"}, 500)
            return
        self._send_bytes(buf.tobytes(), "image/jpeg")

    def _depth_pointcloud(self, query: str) -> None:
        """把深度图转 3D 点云 (X,Y,Z 毫米), 降采样后返回 JSON。"""
        from urllib.parse import parse_qs
        import numpy as np
        d = DEPTH
        if d is None or not DEPTH_MODE["active"]:
            self._send_json({"ok": False, "error": "深度模式未开启"}, 400)
            return
        depth, scale = d.get_depth()
        if depth is None:
            self._send_json({"ok": False, "error": "暂无深度帧"}, 503)
            return
        h, w = depth.shape
        q = parse_qs(query)
        try:
            step = max(2, int(q.get("step", ["8"])[0]))
        except Exception:
            step = 8
        # 近似针孔内参 (可视化用途; 真实标定内参可后续替换)
        fx = fy = 600.0
        cx, cy = w / 2.0, h / 2.0
        pts = []
        for v in range(0, h, step):
            for u in range(0, w, step):
                z = int(depth[v, u])
                if z <= 0:
                    continue
                zm = z * scale
                X = (u - cx) * zm / fx
                Y = (v - cy) * zm / fy
                pts.append([round(X, 1), round(Y, 1), round(zm, 1)])
        self._send_json({"ok": True, "count": len(pts), "step": step,
                         "size": [w, h], "points": pts})

    def _gesture_control(self) -> dict:
        """手势控制机械臂(联动): 识别手势并映射为机械臂动作。

        映射: 张掌=回零 / 握拳=抓取(下降Z 或 比赛+1) / 拇指=比赛+1
              V字=停止 / 指向=停止
        连续模式防抖: 同一手势不重复触发动作(换手势或松开后才再次触发)。
        """
        g = GESTURE.detect_once()
        if not g.get("ok"):
            return {"ok": False, "error": g.get("error", "手势识别失败")}
        gesture = g["results"][0]["gesture"] if g.get("results") else None
        # 防抖: 与上次相同手势 → 不重复执行机械臂动作
        if gesture is not None and gesture == _GESTURE_CTRL_LAST["gesture"]:
            return {"ok": True, "gesture": gesture, "count": g.get("count", 0),
                    "elapsed_ms": g.get("elapsed_ms"),
                    "results": g.get("results", []),
                    "arm": {"ok": True, "action": "同一手势保持中(未重复触发)"},
                    "note": "张掌=回零 握拳=抓取/计分 拇指=+1 V/指向=停止"}
        _GESTURE_CTRL_LAST["gesture"] = gesture
        arm = {"ok": True, "action": "none"}
        if gesture == "open_palm":
            arm = ARM.home()
            arm["action"] = "home 回零"
        elif gesture == "fist":
            if ARM.match["running"]:
                arm = ARM.match_score(1)
                arm["action"] = "抓取成功 比赛+1"
            else:
                arm = ARM.move_to(ARM.position[0], ARM.position[1],
                                  max(0.0, ARM.position[2] - 50.0))
                arm["action"] = "抓取 Z-50mm"
        elif gesture == "thumbs_up":
            if ARM.match["running"]:
                arm = ARM.match_score(1)
                arm["action"] = "确认 比赛+1"
            else:
                arm = ARM.home()
                arm["action"] = "确认 回零"
        elif gesture in ("victory", "point"):
            arm = ARM.stop()
            arm["action"] = "停止"
        return {"ok": True, "gesture": gesture, "count": g.get("count", 0),
                "elapsed_ms": g.get("elapsed_ms"), "results": g.get("results", []),
                "arm": arm, "note": "张掌=回零 握拳=抓取/计分 拇指=+1 V/指向=停止"}

    def _gesture_menu(self) -> dict:
        """手势菜单(联动多能力): 1~5 指选择功能模式, 握拳确认执行, OK 取消。"""
        try:
            body = self._read_json_body()
            modes = body.get("modes")
            if isinstance(modes, list) and modes:
                mlist = [str(x) for x in modes][:6]
                _GESTURE_MENU["modes"] = (mlist +
                    _GESTURE_MENU["modes"][len(mlist):])[:6]
        except Exception:
            pass
        g = GESTURE.detect_once()
        if not g.get("ok"):
            return {"ok": False, "error": g.get("error", "手势识别失败")}
        results = g.get("results", [])
        gesture = results[0]["gesture"] if results else None
        fingers = results[0].get("fingers") if results else None
        m = _GESTURE_MENU
        action = ""
        # 防抖: 同一手势保持时不重复触发(松开/换手势后再触发)
        repeated = (gesture is not None and gesture == m["last_gesture"])
        if not repeated:
            if gesture == "ok":
                m.update(state="idle", armed_index=None, armed_name="")
                action = "取消选择"
            elif gesture == "fist" and m["armed_index"] is not None:
                idx = m["armed_index"]
                m["result"] = self._run_menu_mode(idx)
                m["state"] = "done"
                m["last_action"] = f"执行模式{idx + 1}: {m['modes'][idx]}"
                action = m["last_action"]
            elif fingers and 1 <= fingers <= 5:
                idx = fingers - 1
                m.update(state="armed", armed_index=idx,
                         armed_name=m["modes"][idx],
                         last_action=f"预选模式{fingers}: {m['modes'][idx]}")
                action = m["last_action"]
        m["last_gesture"] = gesture
        menu = {k: m[k] for k in ("state", "armed_index", "armed_name",
                                  "last_action", "result", "modes")}
        return {"ok": True, "gesture": gesture, "fingers": fingers,
                "repeated": bool(repeated),
                "action": action or m.get("last_action") or "",
                "elapsed_ms": g.get("elapsed_ms"), "results": results,
                "menu": menu}

    def _run_menu_mode(self, idx: int) -> dict:
        """执行手势菜单选中的功能(识别类/机械臂)。"""
        name = _GESTURE_MENU["modes"][idx]
        try:
            if name == "颜色识别":
                r = COLOR.detect_once()
                keys = list((r.get("stats") or {}).keys())[:5]
                return {"mode": name, "count": r.get("count", 0),
                        "summary": f"颜色 {r.get('count', 0)} 块: " + ",".join(keys)}
            if name == "文字识别":
                r = OCR.detect_once()
                txts = " ".join(x["text"] for x in (r.get("results") or [])[:3])
                return {"mode": name, "count": r.get("count", 0),
                        "summary": f"文字 {r.get('count', 0)} 段: {txts}"}
            if name == "二维码":
                r = QR.detect_once()
                txts = ",".join(x["text"] for x in (r.get("results") or [])[:3])
                return {"mode": name, "count": r.get("count", 0),
                        "summary": f"二维码 {r.get('count', 0)} 个: {txts}"}
            if name == "目标检测":
                r = YOLO.detect_once()
                names = ",".join(x["label_cn"] for x in (r.get("results") or [])[:3])
                return {"mode": name, "count": r.get("count", 0),
                        "summary": f"目标 {r.get('count', 0)} 个: {names}"}
            if name == "机械臂控制":
                if ARM.match["running"]:
                    ARM.match_score(1)
                    return {"mode": name, "count": 1,
                            "summary": "抓取成功 比赛+1"}
                ARM.move_to(ARM.position[0], ARM.position[1],
                            max(0.0, ARM.position[2] - 50.0))
                return {"mode": name, "count": 1, "summary": "抓取 Z-50mm"}
            return {"mode": name, "summary": "未映射"}
        except Exception as e:
            return {"mode": name, "summary": f"执行失败: {e}"}

    def _gesture_air(self) -> dict:
        """空中手势控制: 张掌启动监控 / 握拳截图 / 左右挥手切换画面 / 手入3D区域触发。"""
        g = GESTURE.detect_once()
        if not g.get("ok"):
            return {"ok": False, "error": g.get("error", "手势识别失败")}
        results = g.get("results", [])
        a = _AIR
        now = time.time()
        actions: list[str] = []
        gesture = results[0]["gesture"] if results else None
        wx = results[0]["wrist"][0] if results else None
        fingers = results[0].get("fingers") if results else None

        # 挥手检测: 腕部 x 在 1.2s 内横移 > 200px
        wave = None
        if wx is not None:
            a["wrist_hist"].append((now, float(wx)))
            a["wrist_hist"] = [(t, x) for (t, x) in a["wrist_hist"]
                               if now - t <= 1.2]
            xs = [x for (_t, x) in a["wrist_hist"]]
            if len(xs) >= 3 and (max(xs) - min(xs)) > 200:
                wave = "right" if xs[-1] > xs[0] else "left"
                a["wrist_hist"] = []
        else:
            a["wrist_hist"] = []

        # 手势边沿触发(换手势才动作)
        edge = gesture is not None and gesture != a["last_gesture"]
        if edge and now - a["last_fire"] > 1.2:
            if gesture == "open_palm":
                ok = MOTION.start()
                actions.append(f"张掌 → 启动监控({'已启动' if ok else '已运行/失败'})")
                a["last_fire"] = now
            elif gesture == "fist":
                p = CAM.snapshot()
                actions.append(f"握拳 → 截图: {p}" if p else "握拳 → 截图失败(相机未运行)")
                a["last_fire"] = now
        if wave and now - a["last_fire"] > 1.2:
            try:
                if wave == "right":
                    _depth_start()
                    actions.append("挥手向右 → 切换到深度画面")
                else:
                    _depth_stop()
                    actions.append("挥手向左 → 切换到 RGB 画面")
                a["last_fire"] = now
            except Exception as e:
                actions.append(f"切换画面失败: {e}")
        a["last_gesture"] = gesture

        # 3D 区域触发(手进入配置的空间区域 → 设备动作)
        pos = None
        in_region = False
        if results:
            pos = _px_to_3d(results[0]["wrist"][0], results[0]["wrist"][1])
            if pos:
                rx, ry, rz = a["region"]["x"], a["region"]["y"], a["region"]["z"]
                in_region = (rx[0] <= pos[0] <= rx[1] and
                             ry[0] <= pos[1] <= ry[1] and
                             rz[0] <= pos[2] <= rz[1])
        if in_region and not a["in_region"] and now - a["last_fire"] > 1.2:
            act = a["action"]
            note = ""
            if act == "match_score":
                r = ARM.match_score(1)
                note = "比赛+1" if r.get("ok") else f"计分失败: {r.get('error')}"
            elif act == "arm_grasp":
                ARM.move_to(ARM.position[0], ARM.position[1],
                            max(0.0, ARM.position[2] - 50.0))
                note = "抓取 Z-50mm"
            elif act == "arm_home":
                ARM.home()
                note = "机械臂回零"
            elif act == "snapshot":
                p = CAM.snapshot()
                note = f"截图: {p}" if p else "截图失败"
            actions.append(f"手进入 3D 区域 → {note}")
            a["last_fire"] = now
        a["in_region"] = in_region

        for s in actions:
            a["log"].insert(0, f"{time.strftime('%H:%M:%S')} {s}")
        a["log"] = a["log"][:12]
        return {"ok": True, "gesture": gesture, "fingers": fingers,
                "wave": wave, "actions": actions, "wrist_3d": pos,
                "in_region": in_region, "elapsed_ms": g.get("elapsed_ms"),
                "results": results,
                "air": {"region": a["region"], "action": a["action"],
                        "action_cn": _AIR_ACTION_CN.get(a["action"], a["action"]),
                        "log": list(a["log"]), "in_region": in_region}}

    def _arm_autosort(self) -> dict:
        """AI 视觉引导机械臂自动分拣: 识别物体→3D位置+抓取角度→坐标→分拣(仿真)。"""
        try:
            body = self._read_json_body()
        except Exception:
            body = {}
        sort_by = str(body.get("sort_by", "color"))
        try:
            max_objects = max(1, min(6, int(body.get("max_objects", 3))))
        except Exception:
            max_objects = 3

        items: list[dict] = []
        if sort_by == "class":
            det = YOLO.detect_once(conf=0.30)
            if det.get("ok"):
                for it in det.get("results", []):
                    x, y, w, h = it["bbox"]
                    items.append({"label": it["label_cn"], "cx": x + w // 2,
                                  "cy": y + h // 2, "w": w, "h": h,
                                  "score": it["confidence"]})
        else:
            det = COLOR.detect_once()
            if det.get("ok"):
                for it in det.get("detections", []):
                    x, y, w, h = it["bbox"]
                    items.append({"label": it["color"], "cx": x + w // 2,
                                  "cy": y + h // 2, "w": w, "h": h,
                                  "score": it.get("confidence", 0.5)})
        if not items:
            return {"ok": False,
                    "error": "未检出可分拣物体(请把物体放入镜头; 深度模式下可得真实 3D)",
                    "sort_by": sort_by}

        items.sort(key=lambda z: z["score"], reverse=True)
        items = items[:max_objects]
        labels = sorted(set(i["label"] for i in items))
        bins = {lab: [round(-200.0 + i * 100.0, 1), -220.0, 20.0]
                for i, lab in enumerate(labels[:5])}

        report = []
        steps_all = []
        if ARM.state == "emergency":
            ARM.reset()
        for it in items:
            p3 = _px_to_3d(it["cx"], it["cy"])
            if p3:
                px, py, pz = p3
                source = "depth"
            else:
                px = round((it["cx"] - 320) * 0.8, 1)
                py = round((it["cy"] - 240) * 0.8, 1)
                pz = 250.0
                source = "demo"
            ang = _grasp_angle_3d(it["cx"], it["cy"], it["w"], it["h"])
            binpos = bins.get(it["label"], [0.0, -220.0, 20.0])
            seq = [
                ("移动到目标上方", [px, py, pz + 60]),
                ("下降至抓取高度", [px, py, max(5.0, pz - 20)]),
                ("闭合夹爪", None),
                ("提起物体", [px, py, pz + 80]),
                ("移动到料箱", [binpos[0], binpos[1], 80.0]),
                ("释放物体", None),
            ]
            obj_steps = []
            for desc, pos in seq:
                if pos:
                    ARM.move_to(pos[0], pos[1], pos[2])
                obj_steps.append(desc)
                steps_all.append(f"{desc} ({it['label']})")
                time.sleep(0.12)
            if ARM.match["running"]:
                ARM.match_score(1)
            report.append({"label": it["label"],
                           "pixel": [it["cx"], it["cy"]],
                           "position_mm": [px, py, pz],
                           "grasp_angle_deg": ang,
                           "bin_mm": [round(v, 1) for v in binpos],
                           "source": source,
                           "steps": obj_steps})
        return {"ok": True, "sort_by": sort_by, "count": len(report),
                "bins": bins, "objects": report, "steps": steps_all,
                "arm": ARM.get_state(),
                "note": "深度模式下为真实 3D 坐标; 否则为演示映射。真实驱动需在控制器配置。"}

    def _depth_measure(self, query: str) -> None:
        from urllib.parse import parse_qs
        d = DEPTH
        if d is None:
            self._send_json({"ok": False, "error": "深度模块未加载"}, 503)
            return
        if not DEPTH_MODE["active"]:
            self._send_json({"ok": False, "error": "深度模式未开启"}, 400)
            return
        q = parse_qs(query)
        try:
            x = int(q.get("x", ["0"])[0])
            y = int(q.get("y", ["0"])[0])
            roi = int(q.get("roi", ["3"])[0])
        except Exception:
            self._send_json({"ok": False, "error": "坐标参数无效"}, 400)
            return
        depth, scale = d.get_depth()
        if depth is None:
            self._send_json({"ok": False, "error": "暂无深度帧"}, 503)
            return
        h, w = depth.shape
        if not (0 <= x < w and 0 <= y < h):
            self._send_json({
                "ok": False,
                "error": f"坐标超出深度图范围 ({w}x{h})"}, 400)
            return
        detail = d.measure_detail_mm(x, y, roi)
        mm = detail["distance_mm"]
        center = d.center_mm()
        valid_ratio = d.status().get("valid_ratio", 0.0)
        quality_warning = ""
        if mm is None:
            quality_warning = "该点附近没有有效深度像素，请点击有颜色的深度区域"
        elif detail["fallback_used"]:
            quality_warning = "点击处为空洞，读数来自邻近像素；仅供参考"
        elif detail["local_valid_ratio"] < 0.2:
            quality_warning = "测量邻域有效像素较少，读数可能不稳定"
        elif valid_ratio < 0.05:
            quality_warning = "整幅深度有效像素低于 5%，读数可能不稳定"
        self._send_json({
            "ok": True,
            "x": x, "y": y,
            "distance_mm": round(mm, 1) if mm else None,
            "center_mm": round(center, 1) if center else None,
            "depth_size": [w, h],
            "depth_scale": round(scale, 4),
            "valid_pixels": detail["valid_pixels"],
            "sample_pixels": detail["sample_pixels"],
            "local_valid_ratio": round(detail["local_valid_ratio"], 3),
            "global_valid_ratio": valid_ratio,
            "fallback_used": detail["fallback_used"],
            "quality_warning": quality_warning,
        })

    # ---- 图像测量 (像素距离/角度/面积, 配合深度得真实尺寸) ----

    @staticmethod
    def _parse_points(qs: str) -> list[tuple[float, float]]:
        """解析 x1=&y1=&x2=&y2=... 或 points=x1,y1;x2,y2;..."""
        from urllib.parse import parse_qs, unquote
        q = parse_qs(qs)
        pts = []
        if "points" in q:
            raw = unquote(q["points"][0])
            for seg in raw.split(";"):
                seg = seg.strip()
                if not seg:
                    continue
                try:
                    xs, ys = seg.split(",")[:2]
                    pts.append((float(xs), float(ys)))
                except Exception:
                    pass
            return pts
        i = 1
        while f"x{i}" in q and f"y{i}" in q:
            try:
                pts.append((float(q[f"x{i}"][0]), float(q[f"y{i}"][0])))
            except Exception:
                pass
            i += 1
        return pts

    def _real_length_mm(self, p1, p2) -> float | None:
        """用深度图把两点像素距离换算成真实毫米 (需要深度模式)。"""
        import math
        d = DEPTH
        if d is None or not DEPTH_MODE.get("active"):
            return None
        depth, _scale = d.get_depth()
        if depth is None:
            return None
        z1 = d.measure_mm(int(p1[0]), int(p1[1]), 3)
        z2 = d.measure_mm(int(p2[0]), int(p2[1]), 3)
        if z1 is None or z2 is None:
            return None
        z_mean = (z1 + z2) / 2.0
        # 简化针孔模型 (DaBai DCW 深度 FOV 约 62°)
        h, w = depth.shape
        fx = w / (2.0 * math.tan(math.radians(62.0) / 2.0))
        dx = p2[0] - p1[0]
        dy = p2[1] - p1[1]
        pixel_dist = math.hypot(dx, dy)
        return pixel_dist * z_mean / fx

    def _measure_distance(self, query: str) -> None:
        pts = self._parse_points(query)
        if len(pts) < 2:
            self._send_json({"ok": False, "error": "需要 2 个点"}, 400)
            return
        import math
        p1, p2 = pts[0], pts[1]
        pixel_dist = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        real = self._real_length_mm(p1, p2)
        self._send_json({
            "ok": True,
            "p1": [round(p1[0], 1), round(p1[1], 1)],
            "p2": [round(p2[0], 1), round(p2[1], 1)],
            "pixel_distance": round(pixel_dist, 2),
            "real_mm": round(real, 1) if real else None,
            "depth_available": real is not None,
        })

    def _measure_angle(self, query: str) -> None:
        pts = self._parse_points(query)
        if len(pts) < 3:
            self._send_json({"ok": False, "error": "需要 3 个点 (顶点为第2点)"}, 400)
            return
        import math
        p1, p2, p3 = pts[0], pts[1], pts[2]
        v1 = (p1[0] - p2[0], p1[1] - p2[1])
        v2 = (p3[0] - p2[0], p3[1] - p2[1])
        n1 = math.hypot(*v1)
        n2 = math.hypot(*v2)
        if n1 == 0 or n2 == 0:
            self._send_json({"ok": False, "error": "顶点与端点重合"}, 400)
            return
        cosang = max(-1.0, min(1.0, (v1[0]*v2[0] + v1[1]*v2[1]) / (n1*n2)))
        angle_deg = math.degrees(math.acos(cosang))
        self._send_json({
            "ok": True,
            "vertex": [round(p2[0], 1), round(p2[1], 1)],
            "angle_deg": round(angle_deg, 2),
            "legs_pixel": [round(n1, 2), round(n2, 2)],
        })

    def _measure_area(self, query: str) -> None:
        pts = self._parse_points(query)
        if len(pts) < 3:
            self._send_json({"ok": False, "error": "至少 3 个点"}, 400)
            return
        import math
        n = len(pts)
        area = 0.0
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            area += x1 * y2 - x2 * y1
        area = abs(area) / 2.0
        perim = 0.0
        for i in range(n):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % n]
            perim += math.hypot(x2 - x1, y2 - y1)
        self._send_json({
            "ok": True,
            "vertices": len(pts),
            "area_px2": round(area, 1),
            "perimeter_px": round(perim, 2),
            "points": [[round(p[0], 1), round(p[1], 1)] for p in pts],
        })

    def log_message(self, fmt: str, *args) -> None:
        # 只打印非 stream / 非文件预览的请求, 避免刷屏
        try:
            line = args[0] if args else ""
            if "GET /stream" in line or "GET /file/" in line:
                return
        except Exception:
            pass
        super().log_message(fmt, *args)


def load_html() -> str:
    for p in (HTML_PATH, HTML_PATH_LEGACY):
        try:
            if p.is_file():
                return p.read_text(encoding="utf-8")
        except Exception:
            continue
    return "<h1>index.html not found</h1>"


# ============================================================
# 入口
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="USB 相机网页控制台")
    ap.add_argument("--host", default="127.0.0.1",
                    help="监听地址 (默认 127.0.0.1; 局域网用 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8000, help="端口")
    ap.add_argument("--index", type=int, default=0, help="相机索引")
    ap.add_argument("--autostart", action="store_true",
                    help="启动服务时自动开相机")
    args = ap.parse_args()

    if not HTML_PATH.exists() and not HTML_PATH_LEGACY.exists():
        print(f"[ERR] 缺少页面文件: {HTML_PATH}")
        return 1

    CAM.index = args.index
    if args.autostart:
        CAM.start(args.index)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True

    url_local = f"http://127.0.0.1:{args.port}/"
    print("=" * 60)
    print(" USB 相机网页控制台已启动")
    print("=" * 60)
    print(f"  本地访问:   {url_local}")
    if args.host == "0.0.0.0":
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            print(f"  局域网访问: http://{ip}:{args.port}/")
        except Exception:
            pass
    print(f"  相机索引:   {args.index}  (启动时{'自动开' if args.autostart else '手动开'})")
    print("  Ctrl+C 停止")
    print("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[..] 正在关闭...")
    finally:
        MOTION.stop()
        COLOR.stop()
        CAM.stop()
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
