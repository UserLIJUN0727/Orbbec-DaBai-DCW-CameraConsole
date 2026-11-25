"""Evaluate a YOLO checkpoint on a YOLO-format held-out split using IoU matching."""
from __future__ import annotations

import argparse
import collections
import json
import time
from pathlib import Path

import yaml
from ultralytics import YOLO


def iou_xyxy(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "dataset_subset" / "data.yaml")
    parser.add_argument("--split", choices=("train", "val", "test"), default="test")
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--iou", type=float, default=0.50)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--tag", default="candidate")
    args = parser.parse_args()
    data_cfg = yaml.safe_load(args.data.read_text(encoding="utf-8"))
    root = Path(data_cfg["path"])
    names = [str(name).strip().lower() for name in data_cfg["names"]]
    image_dir = root / data_cfg[args.split]
    label_dir = image_dir.parent / "labels"
    images = sorted(path for path in image_dir.iterdir() if path.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not images:
        raise RuntimeError(f"No images found in {image_dir}")

    gt_by_image = {}
    counts = {name: {"gt": 0, "pred": 0, "tp": 0, "fp": 0, "fn": 0} for name in names}
    for image in images:
        label_file = label_dir / f"{image.stem}.txt"
        if not label_file.is_file():
            raise FileNotFoundError(f"Missing label file: {label_file}")
        gt_by_class = collections.defaultdict(list)
        for line in label_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts:
                continue
            cls = int(parts[0])
            cx, cy, w, h = map(float, parts[1:5])
            if cls < 0 or cls >= len(names) or min(cx, cy, w, h) < 0 or max(cx, cy, w, h) > 1:
                raise ValueError(f"Invalid YOLO box in {label_file}: {line}")
            gt_by_class[names[cls]].append((cx, cy, w, h))
            counts[names[cls]]["gt"] += 1
        gt_by_image[image] = gt_by_class

    model = YOLO(str(args.model))
    model_names = getattr(model, "names", {}) or {}
    start = time.perf_counter()
    results = model.predict(source=[str(x) for x in images], imgsz=args.imgsz,
                            conf=args.conf, iou=0.7, device=args.device,
                            verbose=False, stream=True)
    image_results = []
    for image, result in zip(images, results):
        h_img, w_img = result.orig_shape
        gt_px = collections.defaultdict(list)
        for cls, boxes in gt_by_image[image].items():
            for cx, cy, bw, bh in boxes:
                gt_px[cls].append(((cx - bw/2)*w_img, (cy - bh/2)*h_img,
                                   (cx + bw/2)*w_img, (cy + bh/2)*h_img))
        pred_by_class = collections.defaultdict(list)
        if result.boxes is not None:
            for box in result.boxes:
                class_id = int(box.cls[0].item())
                raw_name = model_names.get(class_id, str(class_id)) if isinstance(model_names, dict) else model_names[class_id]
                label = str(raw_name).strip().lower()
                if label not in counts:
                    continue
                pred_by_class[label].append((float(box.conf[0].item()), tuple(float(v) for v in box.xyxy[0].tolist())))
        matched_by_class = {}
        for label in names:
            unmatched = set(range(len(gt_px[label])))
            tp = 0
            for _, pred_box in sorted(pred_by_class[label], reverse=True, key=lambda row: row[0]):
                best_i, best_score = None, 0.0
                for gt_i in unmatched:
                    score = iou_xyxy(pred_box, gt_px[label][gt_i])
                    if score > best_score:
                        best_i, best_score = gt_i, score
                if best_i is not None and best_score >= args.iou:
                    unmatched.remove(best_i)
                    tp += 1
            fp = len(pred_by_class[label]) - tp
            fn = len(gt_px[label]) - tp
            counts[label]["pred"] += len(pred_by_class[label])
            counts[label]["tp"] += tp
            counts[label]["fp"] += fp
            counts[label]["fn"] += fn
            if gt_px[label] or pred_by_class[label]:
                matched_by_class[label] = {"tp": tp, "fp": fp, "fn": fn}
        image_results.append({"image": image.name, "gt": {k: len(v) for k, v in gt_px.items()},
                              "pred": {k: len(v) for k, v in pred_by_class.items()},
                              "matched": matched_by_class})
    elapsed = time.perf_counter() - start

    overall = {key: sum(counts[name][key] for name in names) for key in ("gt", "pred", "tp", "fp", "fn")}
    for row in counts.values():
        row["recall_hit_rate"] = row["tp"] / row["gt"] if row["gt"] else 0.0
        row["precision"] = row["tp"] / row["pred"] if row["pred"] else 0.0
        row["f1"] = (2 * row["recall_hit_rate"] * row["precision"] /
                     (row["recall_hit_rate"] + row["precision"])) if row["recall_hit_rate"] + row["precision"] else 0.0
    overall["recall_hit_rate"] = overall["tp"] / overall["gt"] if overall["gt"] else 0.0
    overall["precision"] = overall["tp"] / overall["pred"] if overall["pred"] else 0.0
    overall["f1"] = (2 * overall["recall_hit_rate"] * overall["precision"] /
                     (overall["recall_hit_rate"] + overall["precision"])) if overall["recall_hit_rate"] + overall["precision"] else 0.0
    output = Path(__file__).resolve().parent / "evaluations"
    output.mkdir(exist_ok=True)
    payload = {"model": str(args.model.resolve()), "split": args.split,
               "images": len(images), "conf": args.conf, "iou": args.iou,
               "imgsz": args.imgsz, "device": args.device,
               "matching": "greedy one-to-one same-class IoU threshold",
               "per_class": counts, "overall": overall,
               "seconds": elapsed, "seconds_per_image": elapsed / len(images),
               "images_detail": image_results}
    json_path = output / f"{args.tag}_{args.split}_conf_{args.conf:.2f}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"REPORT={json_path}")
    for label, row in counts.items():
        print(f"{label}: GT={row['gt']} pred={row['pred']} TP={row['tp']} FP={row['fp']} FN={row['fn']} recall={row['recall_hit_rate']:.3%} precision={row['precision']:.3%}")
    print(f"overall: GT={overall['gt']} pred={overall['pred']} TP={overall['tp']} FP={overall['fp']} FN={overall['fn']} recall={overall['recall_hit_rate']:.3%} precision={overall['precision']:.3%} F1={overall['f1']:.3%}")
    print(f"images={len(images)} seconds={elapsed:.2f} mean={elapsed/len(images):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
