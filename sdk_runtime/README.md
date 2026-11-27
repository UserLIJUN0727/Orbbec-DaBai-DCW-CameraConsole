# Orbbec SDK runtime files (vendor download; not mirrored here)

Download the Windows x64 SDK v1.10.37 from the [official Orbbec release page](https://github.com/orbbec/OrbbecSDK/releases/tag/v1.10.37). Orbbec lists DaBai DCW among the devices supported by SDK v1 on its [SDK downloads page](https://www.orbbec.com/developers/orbbec-sdk/). Install or extract the official package, then place the matching runtime DLLs in this directory:

- `OrbbecSDK.dll`
- `ob_usb.dll`
- `live555.dll`
- `depthengine_2_0.dll`
- `opencv_world430.dll` (only if required by the exact SDK package)

Do not mix DLLs from different SDK versions. `orbbec_depth.py` loads `OrbbecSDK.dll` from this directory and reads `../OrbbecSDKConfig_v1.0.xml`. The SDK library has a separate vendor license; this project does not mirror the DLLs or driver installer. Obtain them from Orbbec and retain the license/notices supplied with that release.

Expected DLLs for this adapter are `OrbbecSDK.dll`, `ob_usb.dll`, `live555.dll`, and `depthengine_2_0.dll`. `opencv_world430.dll` is needed only when required by the exact SDK package. Use files from one matching release; do not mix versions.
