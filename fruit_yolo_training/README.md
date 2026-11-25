# Fruit detector experiments

The source code contains a sparse-subset builder, a YOLOv8 fine-tuning script, and a held-out evaluator. Data, cache files, checkpoints, and model weights are excluded from Git.

Source dataset: <https://github.com/lightly-ai/dataset_fruits_detection> (CC0 per source repository).

Recreate data locally by cloning the dataset, then run `prepare_fruit_subset.py` with explicit per-class limits and a fixed seed. The training script writes separate timestamped checkpoints under `runs/` and `candidates/`; `.gitignore` excludes them. Compare a candidate against a held-out split using `evaluate_split.py` before considering any deployment.

Current evidence is in `quality_improvement_report.md`: the experimental candidate did not beat the baseline and must not be treated as an accepted model.
