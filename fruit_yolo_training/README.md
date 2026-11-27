# Fruit detector experiments

The source code contains a sparse-subset builder, a YOLOv8 fine-tuning script, and a held-out evaluator. Data, cache files, checkpoints, and model weights are excluded from Git.

Source dataset: <https://github.com/lightly-ai/dataset_fruits_detection> (its repository includes a CC0 license and identifies the upstream Kaggle dataset). The data files are not duplicated in this project; these commands fetch them from the source repository.

From the project root, with Git and project dependencies installed, run:

```bat
git clone https://github.com/lightly-ai/dataset_fruits_detection.git fruit_yolo_training\fruit_dataset_source
python fruit_yolo_training\prepare_fruit_subset.py --repo fruit_yolo_training\fruit_dataset_source --output fruit_yolo_training\dataset_subset --seed 20261125 --train-per-class 300 --valid-per-class 50 --test-per-class 50
```

The subset builder deterministically selects the requested images and labels. To reproduce training, provide a local YOLOv8n base checkpoint whose terms permit your intended use, then run:

```bat
python fruit_yolo_training\train_fruit_model.py --data fruit_yolo_training\dataset_subset\data.yaml --base C:\path\to\yolov8n.pt --epochs 15 --imgsz 416 --batch 8 --seed 20261125
python fruit_yolo_training\evaluate_split.py --model C:\path\to\candidate.pt --data fruit_yolo_training\dataset_subset\data.yaml --split test --conf 0.35
```

Training writes timestamped checkpoints under `runs/` and `candidates/`; `.gitignore` excludes them. The dataset is CC0 per its source repository. Base model weights have separate terms and are not redistributed here. Compare a candidate against a held-out split before considering any deployment.

Current evidence is in `quality_improvement_report.md` and `evaluations/open_dataset_yolo_retest_20261127.md`: the experimental candidate did not beat the baseline and must not be treated as an accepted model. The separate YOLO-World adapter broadens selectable labels but needs a compatible local checkpoint; it does not change these measured results.
