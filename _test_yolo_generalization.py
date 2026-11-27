"""Offline regression tests for YOLO-World prompt loading and fruit filtering."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import numpy as np

import webcam_server as W


class _Boxes:
    def __init__(self):
        self.cls = np.array([0, 1], dtype=np.float32)
        self.xyxy = np.array([[10, 12, 90, 100], [110, 20, 180, 120]], dtype=np.float32)
        self.conf = np.array([0.91, 0.88], dtype=np.float32)

    def __len__(self):
        return len(self.cls)


class YoloGeneralizationTests(unittest.TestCase):
    def test_prompts_are_trimmed_deduplicated_and_bounded(self):
        self.assertEqual(W._parse_yolo_prompts(" apple, banana, APPLE "),
                         ["apple", "banana"])
        with self.assertRaises(ValueError):
            W._parse_yolo_prompts(",".join(f"class{i}" for i in range(51)))

    def test_world_model_receives_custom_prompts(self):
        class FakeWorld:
            names = {0: "apple", 1: "bicycle"}

            def set_classes(self, classes):
                self.received = list(classes)
                self.names = dict(enumerate(classes))

        model = FakeWorld()
        fake_ultralytics = ModuleType("ultralytics")
        fake_ultralytics.YOLO = lambda _path: model
        engine = W.YoloEngine()
        fake_torch = ModuleType("torch")
        fake_torch.set_num_threads = lambda _count: None
        with tempfile.TemporaryDirectory() as folder:
            fake_weights = Path(folder) / "fake-world.pt"
            fake_weights.touch()
            with patch.dict(sys.modules, {"ultralytics": fake_ultralytics,
                                          "torch": fake_torch}):
                result = engine.load_custom_model(str(fake_weights), "mango, bicycle")
        self.assertTrue(result["ok"], result)
        self.assertEqual(model.received, ["mango", "bicycle"])
        self.assertTrue(result["open_vocabulary"])
        self.assertEqual(engine.active_prompts, ["mango", "bicycle"])

    def test_fixed_vocabulary_model_refuses_custom_prompts(self):
        fake_ultralytics = ModuleType("ultralytics")
        fake_ultralytics.YOLO = lambda _path: SimpleNamespace(names={0: "apple"})
        engine = W.YoloEngine()
        fake_torch = ModuleType("torch")
        fake_torch.set_num_threads = lambda _count: None
        with tempfile.TemporaryDirectory() as folder:
            fake_weights = Path(folder) / "fake-yolo.pt"
            fake_weights.touch()
            with patch.dict(sys.modules, {"ultralytics": fake_ultralytics,
                                          "torch": fake_torch}):
                result = engine.load_custom_model(str(fake_weights), "mango")
        self.assertFalse(result["ok"])
        self.assertIn("YOLO-World", result["error"])
        self.assertIsNone(engine._model)

    def test_fruit_only_keeps_open_vocabulary_fruit_and_hides_person(self):
        fake_model = SimpleNamespace(
            predict=lambda *_args, **_kwargs: [SimpleNamespace(
                names={0: "green apple", 1: "person"}, boxes=_Boxes())])
        engine = W.YoloEngine()
        engine._model = fake_model
        engine.model_name = "fake-world"
        engine._get_frame = lambda: np.zeros((160, 240, 3), dtype=np.uint8)
        with patch.object(W, "DEPTH", None):
            result = engine.detect_once(only_fruit=True, conf=0.35)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["label_cn"], "苹果")
        self.assertTrue(result["results"][0]["is_fruit"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
