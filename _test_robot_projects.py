# -*- coding: utf-8 -*-
"""Simulation-only regression tests for the four vision-to-robot projects."""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.request import Request, urlopen

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import webcam_server as W  # noqa: E402


class RobotProjectTests(unittest.TestCase):
    def setUp(self):
        self.old_state = dict(W._ROBOT_SIM_STATE)
        self.old_history = list(W._ROBOT_PROJECT_HISTORY)
        self.old_depth_mode = dict(W.DEPTH_MODE)
        self.old_depth = W.DEPTH
        W._ROBOT_PROJECT_HISTORY.clear()
        W._ROBOT_SIM_STATE.update(state="idle", position_mm=[0.0, 0.0, 0.0],
                                  last_command="", last_project="")
        W.DEPTH_MODE["active"] = False
        W.DEPTH = None

    def tearDown(self):
        W._ROBOT_SIM_STATE.clear()
        W._ROBOT_SIM_STATE.update(self.old_state)
        W._ROBOT_PROJECT_HISTORY[:] = self.old_history
        W.DEPTH_MODE.clear()
        W.DEPTH_MODE.update(self.old_depth_mode)
        W.DEPTH = self.old_depth

    def test_yolo_sort_maps_detected_fruit_to_simulated_bin(self):
        yolo = {"ok": True, "count": 1, "results": [{
            "label_en": "apple", "label_cn": "苹果", "confidence": 0.91,
            "bbox": [300, 200, 40, 60]}]}
        with patch.object(W.YOLO, "detect_once", return_value=yolo), \
                patch.object(W.CAM, "status", return_value={"width": 640, "height": 480}):
            result = W.run_robot_project("yolo-sort")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["destination"], "料箱 A")
        self.assertEqual(result["pick_point_mm_demo"], [0.5, 7.9, 35.0])
        self.assertFalse(result["calibrated"])
        self.assertEqual(W._ROBOT_SIM_STATE["mode"], "simulation-only")

    def test_color_sort_uses_largest_color_region(self):
        variants = [("深红色", "料箱 A"), ("草绿色", "料箱 B"),
                    ("宝蓝色", "料箱 C"), ("橙色", "料箱 OTHER")]
        for color_name, destination in variants:
            color = {"ok": True, "detections": [
                {"color": "红色", "area": 400, "cx": 80, "cy": 60},
                {"color": color_name, "area": 1200, "cx": 200, "cy": 100}]}
            with patch.object(W.COLOR, "detect_once", return_value=color), \
                    patch.object(W.CAM, "status", return_value={"width": 640, "height": 480}):
                result = W.run_robot_project("color-sort")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["target"], color_name)
            self.assertEqual(result["destination"], destination)

    def test_qr_route_accepts_whitelisted_sort_payload(self):
        qr = {"ok": True, "results": [{"text": "SORT:C", "bbox": [10, 20, 80, 80]}]}
        with patch.object(W.QR, "detect_once", return_value=qr), \
                patch.object(W.CAM, "status", return_value={"width": 640, "height": 480}):
            result = W.run_robot_project("qr-route")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["destination"], "料箱 C")
        self.assertEqual(result["decoded_text"], "SORT:C")

    def test_qr_unknown_route_does_not_move_simulated_robot(self):
        qr = {"ok": True, "results": [{"text": "DELETE EVERYTHING", "bbox": [0, 0, 30, 30]}]}
        with patch.object(W.QR, "detect_once", return_value=qr):
            result = W.run_robot_project("qr-route")
        self.assertFalse(result["ok"])
        self.assertEqual(W._ROBOT_SIM_STATE["state"], "idle")
        self.assertEqual(W._ROBOT_PROJECT_HISTORY, [])

    def test_depth_project_is_blocked_without_switching_rgb(self):
        with patch.object(W, "_depth_start") as start_depth:
            result = W.run_robot_project("depth-pick")
        self.assertTrue(result["blocked"])
        self.assertIn("不会自动切换", result["error"])
        start_depth.assert_not_called()
        self.assertEqual(W._ROBOT_SIM_STATE["state"], "idle")

    def test_depth_project_uses_active_sensor_distance_but_marks_uncalibrated(self):
        fake = MagicMock()
        fake.get_depth.return_value = (np.ones((360, 640), np.uint16), 1.0)
        fake.measure_detail_mm.return_value = {
            "distance_mm": 742.5, "valid_pixels": 49,
            "local_valid_ratio": 1.0, "fallback_used": False}
        W.DEPTH_MODE["active"] = True
        W.DEPTH = fake
        result = W.run_robot_project("depth-pick")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["depth"]["distance_mm"], 742.5)
        self.assertEqual(result["pick_point_mm_demo"][2], 742.5)
        self.assertFalse(result["calibrated"])

    def test_http_status_exposes_four_projects_and_post_runs_allowlisted_id(self):
        server = HTTPServer(("127.0.0.1", 0), W.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/robot/projects/status", timeout=5) as response:
                status = json.loads(response.read().decode("utf-8"))
            self.assertEqual([p["stage"] for p in status["projects"]], [1, 2, 3, 4])
            yolo = {"ok": True, "count": 1, "results": [{
                "label_en": "apple", "label_cn": "苹果", "confidence": 0.91,
                "bbox": [300, 200, 40, 60]}]}
            body = json.dumps({"project": "yolo-sort"}).encode("utf-8")
            request = Request(base + "/robot/projects/run", data=body,
                              headers={"Content-Type": "application/json"}, method="POST")
            with patch.object(W.YOLO, "detect_once", return_value=yolo), \
                    patch.object(W.CAM, "status", return_value={"width": 640, "height": 480}), \
                    urlopen(request, timeout=5) as response:
                result = json.loads(response.read().decode("utf-8"))
            self.assertTrue(result["ok"])
            self.assertEqual(result["destination"], "料箱 A")
            body = json.dumps({"project": "not-allowed"}).encode("utf-8")
            request = Request(base + "/robot/projects/run", data=body,
                              headers={"Content-Type": "application/json"}, method="POST")
            with self.assertRaises(Exception):
                urlopen(request, timeout=5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
