# Orbbec DaBai DCW Camera Console

A Windows/Python browser console for USB camera preview, capture, image tools, optional YOLO/OCR/barcode features, and an Orbbec SDK v1 depth adapter.

## Contents

- `webcam_server.py`, `index.html`, `style.css`, `app.js`: local HTTP server and browser UI.
- `orbbec_depth.py`, `OrbbecSDKConfig_v1.0.xml`: ctypes adapter and SDK configuration for Orbbec SDK v1.
- `camera.py`, `find_camera_com.py`: camera inspection utilities.
- `fruit_yolo_training/`, `fruit_yolo_coco_benchmark/`: reproducible evaluation/training scripts, sanitized reports, final aggregate JSON results, and the candidate training curve. Dataset images and model weights are intentionally not included.
- `_test_full_simulation.py`, `_test_robot_projects.py`, `_test_yolo_generalization.py`: offline and simulation regression tests, including the optional open-vocabulary adapter.

## Windows setup

Tested dependency versions on Windows 11 with Python 3.10.11. Python 3.11 and other devices are not yet verified.

1. Install 64-bit Python 3.10 and the official Orbbec driver for DaBai DCW.
2. Obtain OrbbecSDK v1.10.37 from Orbbec and copy the required runtime DLLs into `sdk_runtime\` as described in `sdk_runtime/README.md`.
3. Double-click `setup.bat` once. It creates a project-local `.venv` and installs `requirements.txt`.
4. Double-click `run.bat`. The server binds to `127.0.0.1:8000`; open <http://127.0.0.1:8000/>.

Model weights are deliberately excluded from Git. The default COCO `yolov8n.pt` must be provided locally; the app does not download it automatically. Optional hand-landmark support needs `hand_landmarker.task`; see `models/README.md`.

Depth mode is available only when the matching SDK v1 DLLs, driver, and device firmware initialize correctly. This source package does not contain a driver installer or SDK binaries and does not change system drivers/firmware.

## Tests

From the package directory, with `.venv` created:

```bat
.venv\Scripts\python.exe _test_full_simulation.py
.venv\Scripts\python.exe _test_robot_projects.py
.venv\Scripts\python.exe _test_yolo_generalization.py
```

These use synthetic frames and simulation state. Passing them does not prove a physical camera stream, depth frames, or robot operation.

## Fruit model notes

The stock YOLOv8 COCO model recognizes apple, banana, and orange among its 80 classes. For other targets, the UI can load a local Ultralytics YOLO-World checkpoint and a comma-separated English vocabulary (up to 50 prompts); ordinary closed-set YOLO checkpoints cannot accept prompt classes. YOLO-World weights are not bundled or auto-downloaded, and prompt-based support does not guarantee accuracy: validate target classes on representative images before use. A separate experimental six-class fruit candidate did not outperform the baseline on the held-out set and is not included. Review `fruit_yolo_training/quality_improvement_report.md` before using any model for sorting. The public fruit dataset used for experiments is CC0: <https://github.com/lightly-ai/dataset_fruits_detection>.

## Privacy and network scope

The server defaults to localhost. Do not set `--host 0.0.0.0` on an untrusted network. Captures and logs are local and ignored by Git. No credentials or personal capture files are included in this package.

## License status

No project-wide license is asserted in this package. Confirm ownership and select a license before public redistribution. Third-party SDKs, models, and Python packages remain under their respective licenses; see `THIRD_PARTY_NOTICES.md`.
