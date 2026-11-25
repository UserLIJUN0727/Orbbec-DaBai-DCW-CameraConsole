# -*- coding: utf-8 -*-
"""Offline regression checks. Uses synthetic frames and mocks; never opens a camera."""
from __future__ import annotations

import sys
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import webcam_server as W  # noqa: E402
from orbbec_depth import OrbbecDepth  # noqa: E402


class SyntheticCapture:
    def __init__(self, *_args):
        self.width, self.height = 640, 480
        self.opened = True
        self.counter = 0

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            self.width = int(value)
        elif prop == cv2.CAP_PROP_FRAME_HEIGHT:
            self.height = int(value)
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return self.width
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return self.height
        return 50.0

    def read(self):
        if not self.opened:
            return False, None
        time.sleep(0.02)  # simulate a bounded camera frame rate
        self.counter += 1
        frame = np.zeros((self.height, self.width, 3), np.uint8)
        x = (self.counter * 7) % max(1, self.width - 80)
        cv2.rectangle(frame, (x, 100), (x + 60, 160), (0, 180, 255), -1)
        return True, frame

    def release(self):
        self.opened = False


class FakeTracker:
    def __init__(self):
        self.updated = threading.Event()

    def init(self, _frame, _bbox):
        return None  # OpenCV 5-compatible void return

    def update(self, _frame):
        self.updated.set()
        return True, (50, 50, 80, 80)


class OfflineSimulationTests(unittest.TestCase):
    def test_command_console_yolo_allowlist_and_arguments(self):
        expected = {"ok": True, "count": 1, "elapsed_ms": 12,
                    "results": [{"label_cn": "苹果", "confidence": 0.91}]}
        with patch.object(W.YOLO, "detect_once", return_value=expected) as detect:
            result = W.execute_console_command("yolo detect fruit conf=0.60")
        self.assertTrue(result["ok"], result)
        detect.assert_called_once_with(only_fruit=True, conf=0.6)
        self.assertEqual(result["data"]["count"], 1)
        self.assertFalse(W.execute_console_command("python -c print(1)")["ok"])
        self.assertFalse(W.execute_console_command("yolo detect conf=1.5")["ok"])

    def test_robot_console_is_simulation_only_and_checks_bounds(self):
        start = W.execute_console_command("robot status")
        self.assertEqual(start["mode"], "simulation-only")
        self.assertTrue(W.execute_console_command("robot simulate move 120 -40 200")["ok"])
        moved = W.execute_console_command("robot status")["data"]
        self.assertEqual(moved["position_mm"], [120.0, -40.0, 200.0])
        self.assertFalse(W.execute_console_command("robot simulate move 600 0 20")["ok"])
        self.assertTrue(W.execute_console_command("robot simulate stop")["ok"])
        self.assertEqual(W.execute_console_command("robot status")["data"]["state"], "stopped")

    def test_camera_rgb_snapshot_record_pipeline_with_synthetic_capture(self):
        original_save = W.SAVE_DIR
        real_capture = cv2.VideoCapture
        try:
            with tempfile.TemporaryDirectory(prefix="orbbec_sim_") as td:
                W.SAVE_DIR = Path(td) / "captures"
                cam = W.Camera()
                record = None
                with patch.object(cv2, "VideoCapture", side_effect=SyntheticCapture):
                    self.assertTrue(cam.start_with_size(0, 640, 480))
                    deadline = time.time() + 2
                    while cam.frame_count < 5 and time.time() < deadline:
                        time.sleep(0.03)
                    self.assertGreaterEqual(cam.frame_count, 5)
                    self.assertTrue(cam.get_jpeg().startswith(b"\xff\xd8"))
                    snap = cam.snapshot()
                    self.assertTrue(snap and snap.is_file())
                    self.assertTrue(cam.start_record())
                    time.sleep(0.35)
                    self.assertTrue(cam.stop_record())
                    record = W.SAVE_DIR / cam.record_status()["record_file"]
                    self.assertTrue(record.is_file() and record.stat().st_size > 0)
                    self.assertFalse(cam.record_status()["recording"])
                    deadline = time.time() + 10
                    while cam.record_status()["record_processing"] and time.time() < deadline:
                        time.sleep(0.03)
                    self.assertFalse(cam.record_status()["record_processing"])
                cam.stop()
                reader = real_capture(str(record))
                ok, frame = reader.read()
                reader.release()
                self.assertTrue(ok)
                self.assertEqual(frame.shape[:2], (480, 640))
                ffmpeg = W._find_ffmpeg()
                if ffmpeg:
                    probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(record)],
                                            capture_output=True, text=True, timeout=15)
                    self.assertIn("video: h264", probe.stderr.lower().replace("h.264", "h264"))
        finally:
            W.SAVE_DIR = original_save

    def test_file_listing_and_path_traversal_guard(self):
        original_save = W.SAVE_DIR
        try:
            with tempfile.TemporaryDirectory(prefix="orbbec_files_sim_") as td:
                W.SAVE_DIR = Path(td)
                (W.SAVE_DIR / "ok.jpg").write_bytes(b"image")
                (W.SAVE_DIR / "ok.mp4").write_bytes(b"video")
                (W.SAVE_DIR / "secret.txt").write_text("not listed")
                names = {item["name"] for item in W._list_files()}
                self.assertEqual(names, {"ok.jpg", "ok.mp4"})
                self.assertIsNotNone(W._safe_file("ok.jpg"))
                self.assertIsNone(W._safe_file("..\\outside.txt"))
                self.assertIsNone(W._safe_file("missing.jpg"))
                handler = W.Handler.__new__(W.Handler)
                handler.path = "/file/ok.jpg"
                sent = []
                handler._send_json = lambda payload, code=200: sent.append((payload, code))
                handler.do_DELETE()
                self.assertEqual(sent[-1][0]["deleted"], "ok.jpg")
                self.assertFalse((W.SAVE_DIR / "ok.jpg").exists())
        finally:
            W.SAVE_DIR = original_save

    def test_readonly_diagnostics_endpoint_does_not_start_hardware(self):
        with patch.object(W.CAM, "start", side_effect=AssertionError("不应启动相机")), \
             patch.object(W, "_make_depth_module", side_effect=AssertionError("不应加载 SDK")):
            result = W._build_diagnostics()
            handler = W.Handler.__new__(W.Handler)
            handler.path = "/diagnostics"
            sent = []
            handler._send_json = lambda payload, code=200: sent.append((payload, code))
            handler.do_GET()
        checks = {item["key"]: item for item in result["checks"]}
        self.assertEqual(sent[-1][1], 200)
        self.assertIn("opencv", checks)
        self.assertIn("rgb", checks)
        self.assertEqual(checks["rgb"]["state"], "idle")
        self.assertIn("depth", checks)
        self.assertIn("ffmpeg", checks)
        self.assertIn("storage", checks)
        self.assertIn("不枚举 USB", result["privacy_note"])

    def test_depth_math_and_visualization_with_synthetic_depth(self):
        d = OrbbecDepth()
        depth = np.full((41, 41), 1500, np.uint16)
        depth[20, 20] = 0
        d.depth_frame = depth
        d.depth_scale = 1.2
        self.assertAlmostEqual(d.measure_mm(20, 20, roi=0), 1800.0, places=3)
        detail = d.measure_detail_mm(20, 20, roi=0)
        self.assertAlmostEqual(detail["distance_mm"], 1800.0, places=2)
        self.assertTrue(detail["fallback_used"])
        self.assertGreater(detail["valid_pixels"], 0)
        self.assertIsNone(d.measure_mm(-1, 20))
        self.assertAlmostEqual(d.center_mm(), 1800.0, places=3)
        filled = d.fill_holes(depth)
        self.assertEqual(filled.shape, depth.shape)
        color = d.colorize_depth()
        self.assertEqual(color.shape, (41, 41, 3))
        self.assertAlmostEqual(d.status()["valid_ratio"], round(float((depth > 0).mean()), 3))

    def test_depth_measure_api_warns_on_hole_fallback_and_sparse_frame(self):
        d = OrbbecDepth()
        depth = np.zeros((41, 41), np.uint16)
        depth[20, 20] = 1500
        d.depth_frame = depth
        d.depth_scale = 1.0
        old_depth, old_active = W.DEPTH, W.DEPTH_MODE["active"]
        try:
            W.DEPTH, W.DEPTH_MODE["active"] = d, True
            handler = W.Handler.__new__(W.Handler)
            sent = []
            handler._send_json = lambda payload, code=200: sent.append((payload, code))
            handler._depth_measure("x=18&y=20&roi=0")
            payload = sent[-1][0]
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["fallback_used"])
            self.assertIn("空洞", payload["quality_warning"])
            self.assertLess(payload["global_valid_ratio"], 0.05)
        finally:
            W.DEPTH, W.DEPTH_MODE["active"] = old_depth, old_active

    def test_mp4v_recording_transcodes_to_h264_when_ffmpeg_is_available(self):
        ffmpeg = W._find_ffmpeg()
        if not ffmpeg:
            self.skipTest("本机未配置 FFmpeg")
        with tempfile.TemporaryDirectory(prefix="orbbec_h264_sim_") as td:
            path = Path(td) / "fallback.mp4"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"),
                                     12.0, (160, 120))
            if not writer.isOpened():
                self.skipTest("当前 OpenCV 不支持生成 mp4v 测试输入")
            for idx in range(12):
                frame = np.full((120, 160, 3), idx * 12, np.uint8)
                writer.write(frame)
            writer.release()
            warning = W._transcode_record_h264(path)
            self.assertEqual(warning, "")
            probe = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)],
                                    capture_output=True, text=True, timeout=15)
            self.assertIn("video: h264", probe.stderr.lower())
            reader = cv2.VideoCapture(str(path))
            ok, frame = reader.read()
            reader.release()
            self.assertTrue(ok)
            self.assertEqual(frame.shape[:2], (120, 160))

    def test_record_stop_returns_while_background_transcode_runs(self):
        class FakeWriter:
            def release(self): pass

        cam = W.Camera()
        done = threading.Event()
        with tempfile.TemporaryDirectory(prefix="orbbec_async_rec_") as td:
            path = Path(td) / "record.mp4"
            path.write_bytes(b"synthetic video placeholder")
            cam.recording = True
            cam.video_writer = FakeWriter()
            cam.record_path = path
            cam.record_frames = 1
            cam.record_fourcc = "mp4v"
            def slow_transcode(_path):
                done.wait(2)
                return ""
            with patch.object(W, "_transcode_record_h264", side_effect=slow_transcode):
                started = time.monotonic()
                self.assertTrue(cam.stop_record())
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertTrue(cam.record_status()["record_processing"])
                self.assertFalse(cam.start_record())
                done.set()
                deadline = time.time() + 2
                while cam.record_status()["record_processing"] and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(cam.record_status()["record_processing"])

    def test_pixel_measurement_distance_angle_area(self):
        handler = W.Handler.__new__(W.Handler)
        sent = []
        handler._send_json = lambda payload, code=200: sent.append((payload, code))
        handler._real_length_mm = lambda _a, _b: None
        handler._measure_distance("points=0,0;3,4")
        self.assertEqual(sent[-1][0]["pixel_distance"], 5.0)
        handler._measure_angle("points=1,0;0,0;0,1")
        self.assertEqual(sent[-1][0]["angle_deg"], 90.0)
        handler._measure_area("points=0,0;4,0;4,3;0,3")
        self.assertEqual(sent[-1][0]["area_px2"], 12.0)

    def test_camera_controls_and_range_probe_restore_fake_values(self):
        class FakeProps:
            def __init__(self):
                self.values = {}
            def get(self, prop):
                if prop == cv2.CAP_PROP_FRAME_WIDTH:
                    return 640.0
                if prop == cv2.CAP_PROP_FRAME_HEIGHT:
                    return 480.0
                return self.values.get(prop, 50.0)
            def set(self, prop, value):
                self.values[prop] = max(0.0, min(100.0, float(value)))
                return True

        cap = FakeProps()
        original_cap = W.CAM.cap
        original_defaults = dict(W.CAM.factory_defaults)
        original_ranges = dict(W.KNOWN_RANGES)
        try:
            W.CAM.cap = cap
            with patch.object(W.CAM, "_refresh_props"), patch.object(W.CAM, "status", return_value={"running": True}):
                prop = next(p for p in W.CAMERA_PROPS if p[3] != "resolution" and getattr(cv2, p[1], None) is not None)
                key, attr = prop[0], prop[1]
                result = W.apply_camera_settings({key: 25})
                self.assertTrue(result["results"][key]["ok"])
                cap.values[getattr(cv2, attr)] = 50.0
                probe = W.probe_ranges()
                self.assertTrue(probe["ok"])
                self.assertAlmostEqual(cap.values[getattr(cv2, attr)], 50.0)
                W.CAM.factory_defaults = {key: 50.0, "FRAME_WIDTH": 640, "FRAME_HEIGHT": 480}
                cap.values[getattr(cv2, attr)] = 25.0
                restored = W.reset_camera_defaults()
                self.assertTrue(restored["ok"])
                self.assertAlmostEqual(cap.values[getattr(cv2, attr)], 50.0)
        finally:
            W.CAM.cap = original_cap
            W.CAM.factory_defaults = original_defaults
            W.KNOWN_RANGES.clear()
            W.KNOWN_RANGES.update(original_ranges)

    def test_motion_detection_with_synthetic_change(self):
        motion = W.MotionDetector()
        motion.sensitivity = 100
        motion.min_area_ratio = 0.01
        motion.cooldown_s = 0
        motion._bgs = cv2.createBackgroundSubtractorMOG2(history=10, varThreshold=8, detectShadows=False)
        alarms = []
        motion._trigger_alarm = lambda *_args: alarms.append(True)
        blank = np.zeros((240, 320, 3), np.uint8)
        for _ in range(5):
            motion._analyze(blank.copy())
        changed = blank.copy()
        cv2.rectangle(changed, (80, 70), (240, 190), (255, 255, 255), -1)
        motion._analyze(changed)
        self.assertEqual(motion.frames_analyzed, 6)
        self.assertTrue(alarms)

    def test_color_detector_toggle_with_synthetic_camera(self):
        image = np.zeros((120, 160, 3), np.uint8)
        cv2.rectangle(image, (20, 20), (140, 100), (0, 0, 255), -1)
        class FakeCamera:
            def get_raw(self):
                return image
        detector = W.ColorDetector()
        self.assertTrue(detector.start(FakeCamera()))
        time.sleep(0.15)
        self.assertTrue(detector.stop())
        self.assertFalse(detector.is_enabled())
        self.assertFalse(detector.status()["color_error"])

    def test_haar_detector_runs_on_synthetic_blank_frame(self):
        blank = np.zeros((240, 320, 3), np.uint8)
        with patch.object(W.CAM, "get_raw", return_value=blank):
            result = W.HAAR.detect_once("face_front")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)
        self.assertTrue(W.HAAR.get_annotated_jpeg().startswith(b"\xff\xd8"))

    def test_tracker_start_update_stop_with_fake_tracker(self):
        frame = np.zeros((240, 320, 3), np.uint8)
        fake = FakeTracker()
        with patch.object(W.CAM, "get_raw", return_value=frame), patch.object(cv2, "TrackerCSRT_create", return_value=fake, create=True):
            result = W.TRACK.start(90, 90, 80)
            self.assertTrue(result["ok"], result)
            self.assertTrue(fake.updated.wait(1.0))
            W.TRACK.stop()
        self.assertFalse(W.TRACK.status()["track_enabled"])

    def test_depth_rgb_switch_lifecycle_with_mock_pipeline(self):
        class FakeDepth:
            started = False
            def start(self, *_args): self.started = True
            def stop(self): self.started = False
            def status(self): return {"depth_running": self.started}

        fake = FakeDepth()
        old_depth = W.DEPTH
        old_active = W.DEPTH_MODE["active"]
        try:
            W.DEPTH = fake
            W.DEPTH_MODE["active"] = False
            with patch.object(W.CAM, "stop") as stop_rgb, patch.object(W.CAM, "start", return_value=True) as start_rgb:
                opened = W._depth_start()
                self.assertTrue(opened["ok"])
                self.assertTrue(W.DEPTH_MODE["active"])
                stop_rgb.assert_called_once()
                closed = W._depth_stop()
                self.assertTrue(closed["ok"])
                self.assertFalse(W.DEPTH_MODE["active"])
                start_rgb.assert_called_once()
        finally:
            W.DEPTH = old_depth
            W.DEPTH_MODE["active"] = old_active

    def test_yolo_empty_scene_inference_with_synthetic_frame(self):
        blank = np.zeros((480, 640, 3), np.uint8)
        with patch.object(W.CAM, "get_raw", return_value=blank):
            result = W.YOLO.detect_once(only_fruit=False, conf=0.35)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 0)
        self.assertTrue(W.YOLO.get_annotated_jpeg().startswith(b"\xff\xd8"))

    def test_custom_fruit_labels_are_case_normalized_and_supported(self):
        expected = {
            "apple": "苹果", "banana": "香蕉", "orange": "橙子",
            "grape": "葡萄", "pineapple": "菠萝", "watermelon": "西瓜",
        }
        for label, chinese in expected.items():
            with self.subTest(label=label):
                self.assertEqual(W._normalize_yolo_label(f"  {label.title()}  "), label)
                self.assertIn(label, W.FRUIT_CLASSES)
                self.assertEqual(W.COCO_CN[label], chinese)


if __name__ == "__main__":
    unittest.main(verbosity=2)
