# Optional model assets

- `yolov8n.pt`: Ultralytics can download this checkpoint on first use; do not commit model weights.
- `hand_landmarker.task`: optional MediaPipe hand landmark asset. Place it in the project root next to `webcam_server.py` when enabling the hand-landmark feature. This file is excluded from Git.

Keep any trained fruit checkpoints outside the repository until they pass a held-out evaluation. The experimental candidate in the local project did not pass and is not part of this package.
