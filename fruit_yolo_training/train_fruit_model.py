"""Fine-tune a separate YOLOv8n candidate; never overwrite the project's base weights."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import datetime
from pathlib import Path

import torch
from ultralytics import YOLO
from ultralytics.engine.trainer import BaseTrainer


def _read_results_csv_compat(self):
    """Read training metrics without importing Polars' optional CPU dispatcher.

    Polars 1.42.1 on this host raises ``unknown feature flag: 'sse3'`` while
    Ultralytics serializes each checkpoint. The CSV is only checkpoint
    metadata, so the standard library is sufficient and avoids changing the
    shared Python environment used by the live camera servers.
    """
    try:
        with Path(self.csv).open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        return {key: [row.get(key, "") for row in rows] for key in (rows[0] if rows else [])}
    except (OSError, csv.Error):
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "dataset_subset" / "data.yaml")
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parents[1] / "yolov8n.pt")
    parser.add_argument("--epochs", type=int, default=15)
    parser.add_argument("--imgsz", type=int, default=416)
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()
    if not args.data.is_file():
        parser.error(f"dataset yaml not found: {args.data}")
    if not args.base.is_file():
        parser.error(f"base weights not found: {args.base}")
    if args.epochs < 1 or args.batch < 1 or args.imgsz < 320:
        parser.error("epochs/batch must be positive and imgsz must be >= 320")

    root = Path(__file__).resolve().parent
    runs_dir = root / "runs"
    candidate_dir = root / "candidates"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"yolov8n_fruit_{stamp}"
    torch.set_num_threads(min(8, torch.get_num_threads()))
    # Ultralytics imports Polars only for this checkpoint metadata read.
    # Keep training functional without upgrading/downgrading shared packages.
    BaseTrainer.read_results_csv = _read_results_csv_compat
    model = YOLO(str(args.base))
    model.train(
        data=str(args.data.resolve()),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device="cpu",
        workers=args.workers,
        project=str(runs_dir),
        name=run_name,
        exist_ok=False,
        seed=args.seed,
        deterministic=True,
        pretrained=True,
        patience=5,
        close_mosaic=3,
        cache=False,
        # Ultralytics' optional plot_results imports Polars, whose current
        # wheel fails CPU feature parsing on this Python installation.
        # Validation metrics are still recorded in results.csv.
        plots=False,
        verbose=True,
    )
    run_path = Path(model.trainer.save_dir)
    best_path = Path(model.trainer.best)
    if not best_path.is_file():
        raise FileNotFoundError(f"training completed without best weights: {best_path}")
    candidate_path = candidate_dir / f"fruit_candidate_yolov8n_{stamp}.pt"
    shutil.copy2(best_path, candidate_path)

    # Keep the source test split independent from both training and validation.
    test_model = YOLO(str(candidate_path))
    test_metrics = test_model.val(
        data=str(args.data.resolve()), split="test", imgsz=args.imgsz,
        batch=args.batch, device="cpu", workers=args.workers,
        project=str(root / "test_runs"), name=f"{run_name}_heldout_test",
        exist_ok=False, plots=False, verbose=True,
    )
    box_metrics = getattr(test_metrics, "box", None)
    metrics = {}
    if box_metrics is not None:
        for key in ("mp", "mr", "map50", "map75", "map"):
            value = getattr(box_metrics, key, None)
            if value is not None:
                metrics[key] = float(value)
    report = {
        "base_weights": str(args.base.resolve()),
        "candidate_weights": str(candidate_path.resolve()),
        "dataset_yaml": str(args.data.resolve()),
        "dataset_license": "CC0-1.0; source: https://github.com/lightly-ai/dataset_fruits_detection",
        "training": {"epochs_requested": args.epochs, "imgsz": args.imgsz,
                     "batch": args.batch, "device": "cpu", "seed": args.seed,
                     "run_dir": str(run_path.resolve())},
        "heldout_test_metrics": metrics,
        "test_results_dir": str((root / "test_runs" / f"{run_name}_heldout_test").resolve()),
        "acceptance_note": "Candidate is not activated in webcam_server.py. Compare against the unchanged baseline before deployment.",
    }
    report_path = candidate_dir / f"{candidate_path.stem}_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"CANDIDATE={candidate_path}")
    print(f"REPORT={report_path}")
    print(f"HELDOUT_TEST={json.dumps(metrics, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
