# Third-party components

- Orbbec SDK v1 and device drivers: vendor-distributed; not bundled. DaBai DCW is listed as an SDK v1 supported device. Obtain Windows SDK v1.10.37 from the [official release](https://github.com/orbbec/OrbbecSDK/releases/tag/v1.10.37) and driver through [Orbbec's SDK page](https://www.orbbec.com/developers/orbbec-sdk/). The SDK repository assigns a separate license to binary libraries; this project does not mirror DLLs or driver installers. Retain and follow all vendor notices.
- Ultralytics YOLO and model checkpoints: subject to their own software/model terms; review before redistribution.
- OpenCV, PyTorch, MediaPipe, RapidOCR, ONNX Runtime, Pillow, PyYAML, pyzbar, and qrcode: each remains under its upstream license.
- The public fruit dataset used in offline experiments is identified as CC0 by its source: https://github.com/lightly-ai/dataset_fruits_detection. Dataset files are not bundled; see `fruit_yolo_training/README.md` for reproducible retrieval instructions.
- The COCO validation subset used by the benchmark is fetched on demand; see `fruit_yolo_coco_benchmark/benchmark_summary.md` and the COCO dataset terms before reuse.

This file is a notice, not a grant of rights to third-party material.
