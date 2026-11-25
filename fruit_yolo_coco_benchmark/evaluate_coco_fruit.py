"""Evaluate the project's existing YOLOv8 COCO model on COCO val2017 fruit objects.

This is an inference benchmark, not a training job. It downloads only a deterministic
sample of images containing COCO apple/banana/orange annotations and stores them in
this benchmark directory. The project's application source and weights are read-only.
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TARGETS = ("apple", "banana", "orange")
ANNOTATIONS_URL = (
    "https://huggingface.co/datasets/merve/coco/resolve/main/"
    "annotations/instances_val2017.json"
)
IMAGE_BASE_URL = "https://huggingface.co/datasets/merve/coco/resolve/main/val2017/"


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "FruitYOLOBenchmark/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as out:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    temporary.replace(destination)


def box_iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    area_a = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    area_b = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, default=Path(__file__).resolve().parents[1] / "yolov8n.pt")
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "data")
    parser.add_argument("--images-per-class", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--iou", type=float, default=0.50)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    if not args.model.is_file():
        parser.error(f"model weights not found: {args.model}")
    if args.images_per_class < 1:
        parser.error("--images-per-class must be >= 1")

    args.data_dir.mkdir(parents=True, exist_ok=True)
    annotation_path = args.data_dir / "instances_val2017.json"
    if not annotation_path.is_file():
        print(f"Downloading COCO validation annotations: {ANNOTATIONS_URL}", flush=True)
        download(ANNOTATIONS_URL, annotation_path)
    with annotation_path.open("r", encoding="utf-8") as stream:
        coco = json.load(stream)

    cat_name_by_id = {row["id"]: row["name"] for row in coco["categories"]}
    cat_id_by_name = {name: cid for cid, name in cat_name_by_id.items() if name in TARGETS}
    missing = set(TARGETS) - cat_id_by_name.keys()
    if missing:
        raise RuntimeError(f"COCO annotation file lacks target categories: {sorted(missing)}")
    image_by_id = {row["id"]: row for row in coco["images"]}
    anns_by_image = collections.defaultdict(list)
    image_ids_by_class = collections.defaultdict(set)
    for ann in coco["annotations"]:
        label = cat_name_by_id.get(ann["category_id"])
        if label in TARGETS and ann.get("iscrowd", 0) == 0:
            anns_by_image[ann["image_id"]].append(ann)
            image_ids_by_class[label].add(ann["image_id"])

    rng = random.Random(args.seed)
    selected_ids = set()
    sampled_counts = {}
    for label in TARGETS:
        candidates = sorted(image_ids_by_class[label])
        rng.shuffle(candidates)
        picked = candidates[: args.images_per_class]
        sampled_counts[label] = {"available_images": len(candidates), "sampled_images": len(picked)}
        selected_ids.update(picked)
    selected_images = [image_by_id[i] for i in sorted(selected_ids)]

    image_dir = args.data_dir / "images"

    def ensure_image(image):
        path = image_dir / image["file_name"]
        valid = path.is_file() and path.stat().st_size > 0
        if valid:
            import cv2
            valid = cv2.imread(str(path)) is not None
        if not valid:
            download(IMAGE_BASE_URL + image["file_name"], path)
        return image["id"], path

    with ThreadPoolExecutor(max_workers=8) as pool:
        image_paths = dict(pool.map(ensure_image, selected_images))
    print(f"Images downloaded/ready: {len(image_paths)}/{len(selected_images)}", flush=True)

    from ultralytics import YOLO

    model = YOLO(str(args.model))
    predictions = {}
    start = time.perf_counter()
    results = model.predict(
        source=[str(image_paths[i]) for i in sorted(selected_ids)],
        imgsz=args.imgsz,
        conf=args.conf,
        iou=0.7,
        device=args.device,
        verbose=False,
        stream=True,
    )
    for image_id, result in zip(sorted(selected_ids), results):
        rows = []
        names = result.names
        if result.boxes is not None:
            for box in result.boxes:
                class_id = int(box.cls[0].item())
                label = names[class_id]
                if label not in TARGETS:
                    continue
                rows.append({
                    "label": label,
                    "confidence": float(box.conf[0].item()),
                    "xyxy": [float(v) for v in box.xyxy[0].tolist()],
                })
        predictions[image_id] = rows
    inference_seconds = time.perf_counter() - start

    counts = {label: {"gt": 0, "predictions": 0, "tp": 0, "fp": 0, "fn": 0} for label in TARGETS}
    image_rows = []
    for image_id in sorted(selected_ids):
        image = image_by_id[image_id]
        gt_by_class = collections.defaultdict(list)
        for ann in anns_by_image[image_id]:
            label = cat_name_by_id[ann["category_id"]]
            x, y, w, h = ann["bbox"]
            gt_by_class[label].append([x, y, x + w, y + h])
            counts[label]["gt"] += 1
        pred_by_class = collections.defaultdict(list)
        for pred in predictions[image_id]:
            pred_by_class[pred["label"]].append(pred)
            counts[pred["label"]]["predictions"] += 1
        row = {"image_id": image_id, "file_name": image["file_name"]}
        row["ground_truth"] = {label: len(gt_by_class[label]) for label in TARGETS if gt_by_class[label]}
        row["predictions"] = [
            {"class": p["label"], "confidence": round(p["confidence"], 4), "xyxy": [round(v, 1) for v in p["xyxy"]]}
            for p in predictions[image_id]
        ]
        row["matched"] = {}
        for label in TARGETS:
            unmatched_gt = set(range(len(gt_by_class[label])))
            tp = 0
            for pred in sorted(pred_by_class[label], key=lambda value: value["confidence"], reverse=True):
                best_index, best_iou = None, 0.0
                for gt_index in unmatched_gt:
                    score = box_iou(pred["xyxy"], gt_by_class[label][gt_index])
                    if score > best_iou:
                        best_index, best_iou = gt_index, score
                if best_index is not None and best_iou >= args.iou:
                    unmatched_gt.remove(best_index)
                    tp += 1
            fp = len(pred_by_class[label]) - tp
            fn = len(gt_by_class[label]) - tp
            counts[label]["tp"] += tp
            counts[label]["fp"] += fp
            counts[label]["fn"] += fn
            if len(gt_by_class[label]) or len(pred_by_class[label]):
                row["matched"][label] = {"tp": tp, "fp": fp, "fn": fn}
        image_rows.append(row)

    total = {key: sum(counts[label][key] for label in TARGETS) for key in ("gt", "predictions", "tp", "fp", "fn")}
    precision = total["tp"] / total["predictions"] if total["predictions"] else 0.0
    recall = total["tp"] / total["gt"] if total["gt"] else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    for label in TARGETS:
        row = counts[label]
        row["precision"] = row["tp"] / row["predictions"] if row["predictions"] else 0.0
        row["recall_hit_rate"] = row["tp"] / row["gt"] if row["gt"] else 0.0
        row["f1"] = (
            2 * row["precision"] * row["recall_hit_rate"] / (row["precision"] + row["recall_hit_rate"])
            if row["precision"] + row["recall_hit_rate"] else 0.0
        )

    report = {
        "dataset": "COCO 2017 validation split (instances_val2017.json)",
        "dataset_source": "https://cocodataset.org/#download",
        "annotation_file_source": ANNOTATIONS_URL,
        "image_file_source": IMAGE_BASE_URL,
        "model": str(args.model.resolve()),
        "model_type": "Existing Ultralytics YOLOv8n COCO-pretrained weights; no fine-tuning",
        "evaluation": {
            "target_classes": list(TARGETS), "images_per_class_seeded_sample": args.images_per_class,
            "seed": args.seed, "unique_images": len(selected_ids), "conf_threshold": args.conf,
            "iou_threshold": args.iou, "imgsz": args.imgsz, "device": args.device,
            "matching": "greedy one-to-one same-class box match at IoU >= threshold",
            "hit_rate_definition": "object recall = TP / (TP + FN) against annotated target-fruit instances",
        },
        "sampling": sampled_counts,
        "per_class": counts,
        "overall_target_fruit": {
            **total, "precision": precision, "recall_hit_rate": recall, "f1": f1,
        },
        "timing": {"inference_seconds_including_model_warmup": inference_seconds,
                   "mean_seconds_per_image": inference_seconds / len(selected_ids) if selected_ids else None},
        "image_results": image_rows,
        "limitations": [
            "This is a deterministic sampled benchmark, not a full COCO evaluation or COCO mAP.",
            "Only apple, banana, and orange ground-truth objects and predictions are scored.",
            "This measures the existing COCO-pretrained model; it does not train or improve weights.",
            "COCO benchmark performance does not guarantee accuracy on the user's camera, lighting, fruit varieties, or robot workspace.",
        ],
    }
    confidence_tag = f"{args.conf:.2f}".replace(".", "p")
    json_path = Path(__file__).resolve().parent / f"benchmark_results_conf_{confidence_tag}.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# YOLO 水果识别公开数据集测试",
        "",
        f"- 数据集：COCO 2017 validation split；COCO 官方下载页：{report['dataset_source']}",
        f"- 本地下载镜像：标注与验证图来自 Hugging Face 的 COCO 镜像（{ANNOTATIONS_URL}）。",
        f"- 模型：项目现有 YOLOv8n COCO 预训练权重 `{args.model.resolve()}`；本次未训练/微调。",
        f"- 样本：每个目标类别固定随机抽取最多 {args.images_per_class} 张正样本图，随机种子 {args.seed}；去重后 {len(selected_ids)} 张。",
        f"- 评价：置信度 ≥ {args.conf:.2f}；同类别 IoU ≥ {args.iou:.2f} 一对一匹配。命中率=目标实例召回率 TP/(TP+FN)。",
        "",
        "| 类别 | 标注实例 | 检出数 | TP | FP | FN | 命中率/召回率 | 精确率 | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label in TARGETS:
        row = counts[label]
        md.append(f"| {label} | {row['gt']} | {row['predictions']} | {row['tp']} | {row['fp']} | {row['fn']} | {row['recall_hit_rate']:.1%} | {row['precision']:.1%} | {row['f1']:.1%} |")
    md += [
        f"| 总计 | {total['gt']} | {total['predictions']} | {total['tp']} | {total['fp']} | {total['fn']} | {recall:.1%} | {precision:.1%} | {f1:.1%} |",
        "",
        f"推理耗时（含模型预热）：{inference_seconds:.2f} 秒；平均 {inference_seconds / len(selected_ids):.3f} 秒/张。",
        "",
        "## 边界与说明",
        "",
        "这是 COCO 验证集上的抽样目标检测评测，不是完整 COCO mAP；只统计苹果、香蕉、橙子。COCO 结果不能替代在用户摄像头、实际光照、品种和机械臂工作区中的验收。",
        "",
        f"逐图预测、标注和匹配计数见同目录 `{json_path.name}`。",
    ]
    md_path = Path(__file__).resolve().parent / f"benchmark_report_conf_{confidence_tag}.md"
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nREPORT={md_path}")
    for label in TARGETS:
        row = counts[label]
        print(f"{label}: GT={row['gt']} pred={row['predictions']} TP={row['tp']} FP={row['fp']} FN={row['fn']} hit_rate={row['recall_hit_rate']:.3%} precision={row['precision']:.3%}")
    print(f"overall: GT={total['gt']} pred={total['predictions']} TP={total['tp']} FP={total['fp']} FN={total['fn']} hit_rate={recall:.3%} precision={precision:.3%} F1={f1:.3%}")
    print(f"images={len(selected_ids)} elapsed={inference_seconds:.2f}s mean/image={inference_seconds / len(selected_ids):.3f}s")
    print(f"JSON={json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
