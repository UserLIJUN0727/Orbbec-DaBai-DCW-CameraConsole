# Orbbec SDK runtime files (not bundled)

Download the Windows x64 runtime from the official Orbbec SDK v1.10.37 release and place the matching DLLs in this directory. The current local setup used:

- `OrbbecSDK.dll`
- `ob_usb.dll`
- `live555.dll`
- `depthengine_2_0.dll`
- `opencv_world430.dll` (only if required by the exact SDK package)

Do not mix DLLs from different SDK versions. `orbbec_depth.py` loads `OrbbecSDK.dll` from this directory and reads `../OrbbecSDKConfig_v1.0.xml`. Vendor binaries and drivers are intentionally excluded from GitHub; retain the SDK license and use the official package.
