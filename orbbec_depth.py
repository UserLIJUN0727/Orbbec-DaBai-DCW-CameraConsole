# -*- coding: utf-8 -*-
"""Orbbec DaBai DCW 深度相机封装 (ctypes 直调 OrbbecSDK v1.10.37 C API)。

来源: 复用 OrbbecCameraProject/main.py 的 OrbbecV1 封装思路。
依赖: sdk_runtime/OrbbecSDK.dll + OrbbecSDKConfig_v1.0.xml (已随本项目提供)。

注意: 深度管线与 cv2.VideoCapture 抢同一个 USB 设备,
      同一时刻只能有一方打开相机 —— 调用方要先停 RGB 再开深度。
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT / "sdk_runtime"
CONFIG_PATH = ROOT / "OrbbecSDKConfig_v1.0.xml"

# SDK 流类型 / 帧格式枚举 (OrbbecSDK v1.10.37)
OB_STREAM_COLOR = 2
OB_STREAM_DEPTH = 3
OB_FORMAT_MJPG = 5
OB_FORMAT_Y16 = 8
OB_FORMAT_Y10 = 10
OB_FORMAT_Y11 = 11
OB_FORMAT_RGB = 22
OB_FORMAT_BGR = 23
OB_FORMAT_BGRA = 25
OB_FORMAT_YUYV = 0
OB_FORMAT_NV12 = 3
OB_FORMAT_Z16 = 28


class OrbbecError(RuntimeError):
    pass


class OrbbecDepth:
    """RGB + 深度 双流采集 (ctypes 封装 OrbbecSDK)。"""

    def __init__(self) -> None:
        self.lib = None
        self.pipeline = None
        self.config = None
        self.started = False
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

        # 帧缓存 (BGR 彩色 / 16 位深度 / 深度比例)
        self.color_frame = None
        self.depth_frame = None
        self.depth_scale = 1.0
        self.frame_count = 0
        self.fps = 0.0
        self.width = 0
        self.height = 0
        self.depth_width = 0
        self.depth_height = 0
        self.error = ""
        self.started_at = 0.0

    # ---------- 加载 DLL ----------

    def _load(self) -> None:
        if self.lib is not None:
            return
        if not RUNTIME_DIR.is_dir():
            raise OrbbecError(f"缺少 SDK 运行时目录: {RUNTIME_DIR}")
        dll = RUNTIME_DIR / "OrbbecSDK.dll"
        if not dll.is_file():
            raise OrbbecError(f"缺少 SDK DLL: {dll}")
        if not CONFIG_PATH.is_file():
            raise OrbbecError(f"缺少 SDK 配置: {CONFIG_PATH}")
        # 只用 add_dll_directory (进程内、可控), 不改全局 PATH。
        # 改 PATH 会把 sdk_runtime 里的 msvcp140/vcruntime 等塞进 DLL 搜索路径,
        # 导致 onnxruntime / torch 的 DLL 初始化失败。
        try:
            self._dll_dir = os.add_dll_directory(str(RUNTIME_DIR))
        except Exception:
            self._dll_dir = None
        self.lib = ctypes.CDLL(str(dll))
        self._bind()

    def _bind(self) -> None:
        c_void_p = ctypes.c_void_p
        err_pp = ctypes.POINTER(c_void_p)
        lib = self.lib

        lib.ob_error_message.argtypes = [c_void_p]
        lib.ob_error_message.restype = ctypes.c_char_p
        lib.ob_error_function.argtypes = [c_void_p]
        lib.ob_error_function.restype = ctypes.c_char_p
        lib.ob_error_args.argtypes = [c_void_p]
        lib.ob_error_args.restype = ctypes.c_char_p
        lib.ob_delete_error.argtypes = [c_void_p]
        lib.ob_delete_error.restype = None

        lib.ob_create_pipeline.argtypes = [err_pp]
        lib.ob_create_pipeline.restype = c_void_p
        lib.ob_delete_pipeline.argtypes = [c_void_p, err_pp]
        lib.ob_delete_pipeline.restype = None
        lib.ob_create_config.argtypes = [err_pp]
        lib.ob_create_config.restype = c_void_p
        lib.ob_delete_config.argtypes = [c_void_p, err_pp]
        lib.ob_delete_config.restype = None
        lib.ob_config_enable_video_stream.argtypes = [
            c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, err_pp]
        lib.ob_config_enable_video_stream.restype = None
        lib.ob_pipeline_start_with_config.argtypes = [c_void_p, c_void_p, err_pp]
        lib.ob_pipeline_start_with_config.restype = None
        lib.ob_pipeline_wait_for_frameset.argtypes = [
            c_void_p, ctypes.c_uint32, err_pp]
        lib.ob_pipeline_wait_for_frameset.restype = c_void_p
        lib.ob_pipeline_stop.argtypes = [c_void_p, err_pp]
        lib.ob_pipeline_stop.restype = None

        lib.ob_frameset_color_frame.argtypes = [c_void_p, err_pp]
        lib.ob_frameset_color_frame.restype = c_void_p
        lib.ob_frameset_depth_frame.argtypes = [c_void_p, err_pp]
        lib.ob_frameset_depth_frame.restype = c_void_p
        lib.ob_video_frame_width.argtypes = [c_void_p, err_pp]
        lib.ob_video_frame_width.restype = ctypes.c_uint32
        lib.ob_video_frame_height.argtypes = [c_void_p, err_pp]
        lib.ob_video_frame_height.restype = ctypes.c_uint32
        lib.ob_frame_format.argtypes = [c_void_p, err_pp]
        lib.ob_frame_format.restype = ctypes.c_int
        lib.ob_frame_data.argtypes = [c_void_p, err_pp]
        lib.ob_frame_data_data = lib.ob_frame_data
        lib.ob_frame_data.restype = c_void_p
        lib.ob_frame_data_size.argtypes = [c_void_p, err_pp]
        lib.ob_frame_data_size.restype = ctypes.c_uint32
        lib.ob_depth_frame_get_value_scale.argtypes = [c_void_p, err_pp]
        lib.ob_depth_frame_get_value_scale.restype = ctypes.c_float
        lib.ob_delete_frame.argtypes = [c_void_p, err_pp]
        lib.ob_delete_frame.restype = None

    # ---------- 错误处理 ----------

    @staticmethod
    def _dec(v) -> str:
        return v.decode("utf-8", errors="replace") if v else ""

    def _check(self, err, action: str) -> None:
        if not err or not err.value:
            return
        fn = self._dec(self.lib.ob_error_function(err))
        args = self._dec(self.lib.ob_error_args(err))
        msg = self._dec(self.lib.ob_error_message(err))
        self.lib.ob_delete_error(err)
        raise OrbbecError(f"{action} 失败 (SDK={fn}, 参数={args}, 错误={msg})")

    def _call(self, action, func, *args):
        err = ctypes.c_void_p()
        r = func(*args, ctypes.byref(err))
        self._check(err, action)
        return r

    # ---------- 生命周期 ----------

    def start(self, w: int = 640, h: int = 360, fps: int = 30) -> None:
        """启动 RGB+Depth 双流。w/h 用 DaBai DCW 的原生深度分辨率。"""
        self._load()
        self.stop()  # 先确保干净

        self.pipeline = self._call("创建 Pipeline", self.lib.ob_create_pipeline)
        if not self.pipeline:
            raise OrbbecError("创建 Pipeline 返回空指针")
        self.config = self._call("创建 Config", self.lib.ob_create_config)
        if not self.config:
            raise OrbbecError("创建 Config 返回空指针")

        self._call("配置深度流", self.lib.ob_config_enable_video_stream,
                   self.config, OB_STREAM_DEPTH, w, h, fps, OB_FORMAT_Y11)
        self._call("配置彩色流", self.lib.ob_config_enable_video_stream,
                   self.config, OB_STREAM_COLOR, w, h, fps, OB_FORMAT_MJPG)
        self._call("启动 RGB+Depth Pipeline",
                   self.lib.ob_pipeline_start_with_config,
                   self.pipeline, self.config)
        self.started = True
        self.started_at = time.time()
        self.error = ""
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        print(f"[DEPTH] Orbbec 深度管线已启动 (RGB+Depth {w}x{h} @{fps}fps)")

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        if self.started and self.pipeline:
            try:
                self._call("停止 Pipeline", self.lib.ob_pipeline_stop, self.pipeline)
            except Exception as e:
                print(f"[WARN] 停止 Pipeline: {e}")
            self.started = False
        if self.config:
            try:
                self._call("释放 Config", self.lib.ob_delete_config, self.config)
            except Exception:
                pass
            self.config = None
        if self.pipeline:
            try:
                self._call("释放 Pipeline", self.lib.ob_delete_pipeline, self.pipeline)
            except Exception:
                pass
            self.pipeline = None
        with self.lock:
            self.color_frame = None
            self.depth_frame = None

    # ---------- 帧读取 ----------

    def _read_raw(self, frame) -> tuple:
        width = int(self._call("读帧宽", self.lib.ob_video_frame_width, frame))
        height = int(self._call("读帧高", self.lib.ob_video_frame_height, frame))
        fmt = int(self._call("读帧格式", self.lib.ob_frame_format, frame))
        size = int(self._call("读帧大小", self.lib.ob_frame_data_size, frame))
        ptr = self._call("读帧数据", self.lib.ob_frame_data, frame)
        if not ptr or size <= 0:
            raise OrbbecError("SDK 返回空帧数据")
        import numpy as np
        raw = np.ctypeslib.as_array(
            (ctypes.c_uint8 * size).from_address(ptr)).copy()
        return raw, fmt, width, height

    def _loop(self) -> None:
        import cv2
        import numpy as np
        t_last = time.time()
        n = 0
        while not self._stop_evt.is_set():
            if not self.started or not self.pipeline:
                break
            frameset = None
            color_f = None
            depth_f = None
            try:
                frameset = self._call(
                    "等待帧集",
                    self.lib.ob_pipeline_wait_for_frameset,
                    self.pipeline, 2000)
                if not frameset:
                    time.sleep(0.01)
                    continue
                color_f = self._call("取彩色帧", self.lib.ob_frameset_color_frame, frameset)
                depth_f = self._call("取深度帧", self.lib.ob_frameset_depth_frame, frameset)
                if not color_f or not depth_f:
                    continue

                c_raw, c_fmt, c_w, c_h = self._read_raw(color_f)
                d_raw, _d_fmt, d_w, d_h = self._read_raw(depth_f)
                scale = float(self._call(
                    "读深度比例",
                    self.lib.ob_depth_frame_get_value_scale, depth_f))

                # 彩色帧解码
                if c_fmt == OB_FORMAT_MJPG:
                    color = cv2.imdecode(c_raw, cv2.IMREAD_COLOR)
                elif c_fmt == OB_FORMAT_RGB:
                    color = cv2.cvtColor(c_raw.reshape(c_h, c_w, 3), cv2.COLOR_RGB2BGR)
                elif c_fmt == OB_FORMAT_BGR:
                    color = c_raw.reshape(c_h, c_w, 3).copy()
                elif c_fmt == OB_FORMAT_YUYV:
                    color = cv2.cvtColor(c_raw.reshape(c_h, c_w, 2), cv2.COLOR_YUV2BGR_YUY2)
                else:
                    color = None
                # 深度帧 -> uint16 (mm 需乘 scale)
                vals = d_raw.view(np.uint16)
                need = d_w * d_h
                if vals.size < need:
                    continue
                depth = vals[:need].reshape(d_h, d_w)

                if color is None:
                    continue

                with self.lock:
                    self.color_frame = color
                    self.depth_frame = depth
                    self.depth_scale = scale
                    self.width, self.height = c_w, c_h
                    self.depth_width, self.depth_height = d_w, d_h
                    self.frame_count += 1
                n += 1
                now = time.time()
                if now - t_last >= 1.0:
                    self.fps = n / (now - t_last)
                    t_last = now
                    n = 0
            except Exception as e:
                self.error = str(e)
                time.sleep(0.05)
            finally:
                for f in (color_f, depth_f, frameset):
                    if f:
                        try:
                            self._call("释放帧", self.lib.ob_delete_frame, f)
                        except Exception:
                            pass

    # ---------- 对外 ----------

    def get_color(self):
        with self.lock:
            return None if self.color_frame is None else self.color_frame.copy()

    def get_depth(self):
        """返回 (depth_u16, scale)。depth 单位是刻度, 乘 scale 得毫米。"""
        with self.lock:
            if self.depth_frame is None:
                return None, 1.0
            return self.depth_frame.copy(), self.depth_scale

    def measure_detail_mm(self, x: int, y: int, roi: int = 3) -> dict:
        """Return measured distance and local support so callers can show quality."""
        import numpy as np
        depth, scale = self.get_depth()
        if depth is None:
            return {"distance_mm": None, "valid_pixels": 0, "sample_pixels": 0,
                    "local_valid_ratio": 0.0, "fallback_used": False}
        h, w = depth.shape
        if not (0 <= x < w and 0 <= y < h):
            return {"distance_mm": None, "valid_pixels": 0, "sample_pixels": 0,
                    "local_valid_ratio": 0.0, "fallback_used": False}
        # 1) 优先原邻域中值
        x0, x1 = max(0, x - roi), min(w, x + roi + 1)
        y0, y1 = max(0, y - roi), min(h, y + roi + 1)
        patch = depth[y0:y1, x0:x1].astype(np.float32)
        valid = patch[patch > 0]
        if valid.size > 0:
            return {"distance_mm": float(np.median(valid) * scale),
                    "valid_pixels": int(valid.size), "sample_pixels": int(patch.size),
                    "local_valid_ratio": float(valid.size / patch.size),
                    "fallback_used": False}
        # 2) 空洞回退: 同心向外找最近一圈有效像素
        for ring in (5, 8, 12, 15, 20):
            rx0, rx1 = max(0, x - ring), min(w, x + ring + 1)
            ry0, ry1 = max(0, y - ring), min(h, y + ring + 1)
            ring_patch = depth[ry0:ry1, rx0:rx1].astype(np.float32)
            rv = ring_patch[ring_patch > 0]
            if rv.size > 0:
                return {"distance_mm": float(np.median(rv) * scale),
                        "valid_pixels": int(rv.size), "sample_pixels": int(ring_patch.size),
                        "local_valid_ratio": float(rv.size / ring_patch.size),
                        "fallback_used": True}
        return {"distance_mm": None, "valid_pixels": 0, "sample_pixels": int(patch.size),
                "local_valid_ratio": 0.0, "fallback_used": True}

    def measure_mm(self, x: int, y: int, roi: int = 3) -> float | None:
        """Measure the local median in millimeters; see measure_detail_mm for quality."""
        return self.measure_detail_mm(x, y, roi).get("distance_mm")

    def fill_holes(self, depth, max_gap: int = 7):
        """小空洞填充(仅用于可视化/兜底), 用形态学闭运算 + 邻域中值插值。

        真实测距仍走 measure_mm 的原值优先逻辑, 不受影响。
        """
        import cv2
        import numpy as np
        if depth is None:
            return None
        valid = (depth > 0).astype(np.uint8)
        # 闭运算把 ≤max_gap 的小洞补成有效区
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (max_gap, max_gap))
        filled_mask = cv2.morphologyEx(valid, cv2.MORPH_CLOSE, k)
        holes = (filled_mask > 0) & (valid == 0)
        if not holes.any():
            return depth
        out = depth.astype(np.float32).copy()
        # 对空洞区用邻域有效值的中值填充 (迭代 2 次让大洞边缘更平滑)
        for _ in range(2):
            blurred = cv2.medianBlur(out.astype(np.uint16), 5).astype(np.float32)
            out[holes & (out == 0)] = blurred[holes & (out == 0)]
        # 还有残留就用整体有效中值兜底
        still = (out == 0) & (filled_mask > 0)
        if still.any():
            vv = depth[depth > 0]
            if vv.size:
                out[still] = float(np.median(vv))
        return out.astype(np.uint16)

    def center_mm(self, roi: int = 8) -> float | None:
        depth, _s = self.get_depth()
        if depth is None:
            return None
        return self.measure_mm(depth.shape[1] // 2, depth.shape[0] // 2, roi)

    def colorize_depth(self, max_mm: float = 4000.0):
        """把深度图转成彩色(JET)可视化图 (BGR)。先做小空洞填充, 画面更干净。"""
        import cv2
        import numpy as np
        depth, scale = self.get_depth()
        if depth is None:
            return None
        # 小空洞填充 (仅可视化; 真实测距仍用原值优先的 measure_mm)
        filled = self.fill_holes(depth)
        mm = filled.astype(np.float32) * scale
        mm[filled == 0] = 0
        mm = np.clip(mm, 0, max_mm)
        norm = (mm / max_mm * 255).astype(np.uint8)
        color = cv2.applyColorMap(norm, cv2.COLORMAP_JET)
        color[filled == 0] = (12, 16, 20)   # 仍无效像素置暗
        return color

    def status(self) -> dict:
        with self.lock:
            running = self.started and self.depth_frame is not None
            df = None if self.depth_frame is None else self.depth_frame
        valid_ratio = 0.0
        if df is not None:
            import numpy as np
            valid_ratio = float((df > 0).sum()) / float(df.size) if df.size else 0.0
        with self.lock:
            return {
                "depth_running": running,
                "depth_pipeline_started": self.started,
                "depth_fps": round(self.fps, 1),
                "depth_frames": self.frame_count,
                "depth_scale": round(self.depth_scale, 4),
                "color_size": [self.width, self.height],
                "depth_size": [self.depth_width, self.depth_height],
                "valid_ratio": round(valid_ratio, 3),
                "depth_error": self.error,
                "uptime": round(time.time() - self.started_at, 1)
                          if self.started_at else 0,
            }
