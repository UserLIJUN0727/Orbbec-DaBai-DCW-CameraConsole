# YOLO open-vocabulary adapter release note

## Change

The browser console can now load a local Ultralytics YOLO-World checkpoint with an English comma-separated prompt list (up to 50 prompts, 60 characters each). It reports an explicit error if a normal closed-set YOLO checkpoint is given prompts. Common fruit filters include descriptive labels such as `green apple` and an expanded set of common fruit names.

No checkpoint or dataset is bundled, and the application does not download model files automatically. The existing COCO YOLOv8 checkpoint remains fixed to its trained 80 categories. A promptable checkpoint is required to select labels outside its fixed vocabulary; promptability alone is not an accuracy guarantee.

## Verification

- Python syntax check: passed.
- JavaScript syntax check: passed.
- `_test_yolo_generalization.py`: 4/4 passed; model-loading behavior is tested with a fake model object.
- `_test_full_simulation.py`: 22/22 passed.
- `_test_robot_projects.py`: 7/7 passed.
- Local camera server smoke test: page and new prompt input returned HTTP 200; the attached RGB camera delivered 640x480 frames at about 30 FPS. Default COCO YOLO inference returned one `person` box at confidence 0.518 in one live frame. This verifies the runtime path, not correct classification or an accuracy rate.
- Ordinary fruit checkpoint with custom prompts was rejected by the API without replacing the active default model.

## Acceptance boundary

The new prompt adapter has not been tested with an actual YOLO-World checkpoint. No YOLO-World weights, training dataset, captures, SDK binaries, or driver files are included. Real accuracy for target objects and robot picking remains unverified.
