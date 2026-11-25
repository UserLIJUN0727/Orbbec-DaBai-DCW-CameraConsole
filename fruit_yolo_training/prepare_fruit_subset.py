"""Build a deterministic, class-balanced YOLO subset from the public CC0 fruit data."""
from __future__ import annotations

import argparse
import collections
import json
import random
import shutil
import subprocess
from pathlib import Path

import yaml

SPLIT_NAMES = {"train": "train", "valid": "val", "test": "test"}


def class_images(repo: Path, split: str) -> tuple[list[str], dict[int, set[str]]]:
    label_dir = repo / split / "labels"
    if not label_dir.is_dir():
        raise FileNotFoundError(f"Missing split labels: {label_dir}")
    names = []
    classes: dict[int, set[str]] = collections.defaultdict(set)
    for label_path in sorted(label_dir.glob("*.txt")):
        names.append(label_path.stem)
        for line in label_path.read_text(encoding="utf-8").splitlines():
            fields = line.split()
            if fields:
                classes[int(fields[0])].add(label_path.stem)
    return names, classes


def selected_stems(repo: Path, limits: dict[str, int], seed: int):
    yaml_data = yaml.safe_load((repo / "data.yaml").read_text(encoding="utf-8"))
    class_names = [str(x).strip().lower() for x in yaml_data["names"]]
    chosen_by_split = {}
    for split, limit in limits.items():
        _, by_class = class_images(repo, split)
        rng = random.Random(seed + (0 if split == "train" else 1 if split == "valid" else 2))
        chosen = set()
        for class_id in range(len(class_names)):
            candidates = sorted(by_class.get(class_id, set()))
            rng.shuffle(candidates)
            chosen.update(candidates[: min(limit, len(candidates))])
        chosen_by_split[split] = (class_names, chosen)
    return chosen_by_split


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "dataset_subset")
    parser.add_argument("--seed", type=int, default=20261125)
    parser.add_argument("--train-per-class", type=int, default=300)
    parser.add_argument("--valid-per-class", type=int, default=50)
    parser.add_argument("--test-per-class", type=int, default=50)
    parser.add_argument("--emit-git-patterns", action="store_true",
                        help="print sparse-checkout patterns for selected images; does not copy data")
    parser.add_argument("--fetch-git-images", action="store_true",
                        help="use Git sparse-checkout to fetch only selected image blobs")
    args = parser.parse_args()
    repo = args.repo.resolve()
    limits = {"train": args.train_per_class, "valid": args.valid_per_class,
              "test": args.test_per_class}
    if any(v < 1 for v in limits.values()):
        parser.error("all per-class limits must be positive")
    splits = selected_stems(repo, limits, args.seed)

    patterns = ["/train/labels/**", "/valid/labels/**", "/test/labels/**",
                "/data.yaml", "/README.md", "/LICENSE"]
    if args.emit_git_patterns or args.fetch_git_images:
        for split, (_, stems) in splits.items():
            for stem in sorted(stems):
                patterns.append(f"/{split}/images/{stem}.jpg")
        if args.fetch_git_images:
            subprocess.run(
                ["git", "-C", str(repo), "sparse-checkout", "set", "--no-cone", "--stdin"],
                input="\n".join(patterns) + "\n",
                text=True,
                check=True,
            )
            print(f"Sparse checkout fetched patterns for {sum(len(v[1]) for v in splits.values())} selected images.")
            return 0
        print("\n".join(patterns))
        return 0

    manifest = {
        "source": "lightly-ai/dataset_fruits_detection",
        "source_license": "CC0-1.0",
        "seed": args.seed,
        "per_class_image_limits": limits,
        "classes": splits["train"][0],
        "splits": {},
    }
    for source_split, (class_names, stems) in splits.items():
        target_split = SPLIT_NAMES[source_split]
        images_out = args.output / target_split / "images"
        labels_out = args.output / target_split / "labels"
        images_out.mkdir(parents=True, exist_ok=True)
        labels_out.mkdir(parents=True, exist_ok=True)
        box_counts = collections.Counter()
        copied = 0
        for stem in sorted(stems):
            source_label = repo / source_split / "labels" / f"{stem}.txt"
            source_image = next((p for suffix in (".jpg", ".jpeg", ".png")
                                 if (p := repo / source_split / "images" / f"{stem}{suffix}").is_file()), None)
            if source_image is None:
                raise FileNotFoundError(f"Selected image missing from sparse checkout: {source_split}/images/{stem}.*")
            shutil.copy2(source_image, images_out / source_image.name)
            shutil.copy2(source_label, labels_out / f"{stem}.txt")
            copied += 1
            for line in source_label.read_text(encoding="utf-8").splitlines():
                fields = line.split()
                if fields:
                    box_counts[class_names[int(fields[0])]] += 1
        manifest["splits"][target_split] = {
            "images": copied,
            "boxes_by_class": dict(box_counts),
        }

    data_cfg = {
        "path": str(args.output.resolve()),
        "train": "train/images",
        "val": "val/images",
        "test": "test/images",
        "names": splits["train"][0],
        "nc": len(splits["train"][0]),
    }
    (args.output / "data.yaml").write_text(yaml.safe_dump(data_cfg, sort_keys=False), encoding="utf-8")
    (args.output / "subset_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
