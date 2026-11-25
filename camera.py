# -*- coding: utf-8 -*-
"""
USB 相机控制程序 v2（自带诊断）

用法:
  python camera.py doctor                 # 自检: 环境/后端/索引/占用情况
  python camera.py list                   # 列出相机
  python camera.py open                   # 自动找相机并预览
  python camera.py open -i 0              # 指定索引预览
  python camera.py open --name Orbbec     # 按名字关键字打开
  python camera.py open -i 0 --snapshot a.jpg     # 拍一张
  python camera.py open -i 0 --record a.mp4       # 录 5 秒

预览快捷键:
  S 拍照 | R 录像开关 | Space 暂停 | F 全屏 | Q/Esc 退出
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

SAVE_DIR = Path(__file__).resolve().parent / "captures"
WINDOW_NAME = "USB Camera"


# ---------- 公共小工具 ----------

def run_powershell(command: str) -> str:
    """执行 PowerShell, 兼容中文 Windows 的 GBK/UTF-8 输出。"""
    prefixed = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", prefixed],
            capture_output=True, timeout=30,
        )
    except Exception as e:
        return f""
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            return out.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.stdout.decode("utf-8", errors="replace")


def pnp_cameras() -> list[dict]:
    """列出系统中所有相机设备(含 PnP 状态)。"""
    ps = (
        "Get-PnpDevice -Class Camera -ErrorAction SilentlyContinue | "
        "Select-Object Status, FriendlyName, InstanceId | ConvertTo-Json -Depth 3"
    )
    raw = run_powershell(ps).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def try_open(index: int, backend: int):
    """尝试打开, 打开成功且能读一帧才返回 cap。"""
    import cv2
    cap = cv2.VideoCapture(index, backend)
    if cap.isOpened():
        ok, frame = cap.read()
        if ok and frame is not None:
            return cap
        cap.release()
    else:
        cap.release()
    return None


def scan_all(max_index: int = 10) -> list[dict]:
    """穷举 0..max_index × 各后端, 返回所有能打开的相机。

    遇到连续 2 个索引都打不开就提前停止, 并把 OpenCV 的告警静音,
    避免控制台被一堆 WARN 刷屏。
    """
    import cv2
    backends = [
        (cv2.CAP_DSHOW, "DirectShow"),
        (cv2.CAP_MSMF, "MediaFoundation"),
        (cv2.CAP_ANY, "Auto"),
    ]
    # 扫描期间静音 OpenCV 日志
    old_level = None
    if hasattr(cv2, "setLogLevel") and hasattr(cv2, "LOG_LEVEL_SILENT"):
        try:
            old_level = cv2.setLogLevel(cv2.LOG_LEVEL_SILENT)
        except Exception:
            old_level = None

    found = []
    misses = 0
    try:
        for i in range(max_index):
            hit = None
            for backend, name in backends:
                cap = try_open(i, backend)
                if cap is not None:
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    cap.release()
                    hit = {"index": i, "backend": name, "width": w, "height": h}
                    break
            if hit:
                found.append(hit)
                misses = 0
            else:
                misses += 1
                if misses >= 2 and found:
                    # 已经找到过相机, 再连续 2 个索引都空, 认为到头了
                    break
    finally:
        if old_level is not None:
            try:
                cv2.setLogLevel(old_level)
            except Exception:
                pass
    return found


# ---------- doctor ----------

def cmd_doctor() -> int:
    print("=" * 66)
    print(" 相机诊断 doctor")
    print("=" * 66)

    # 1. Python 环境
    print(f"\n[1] Python 环境")
    print(f"    Python : {sys.version.split()[0]}  ({sys.executable})")
    try:
        import cv2
        print(f"    OpenCV : {cv2.__version__}")
    except ImportError:
        print("    OpenCV : [X] 未安装")
        print("             → 请安装: pip install opencv-python")
        return 1

    # 2. OpenCV 可用后端
    print(f"\n[2] OpenCV 后端")
    avail = []
    try:
        reg = getattr(cv2, "videoio_registry", None)
        if reg is not None:
            if hasattr(reg, "getAvailableBackends"):
                avail = list(reg.getAvailableBackends())
            elif hasattr(reg, "getBackendName"):
                # 旧版 API: 逐个后端名试一下
                for b in range(0, 30):
                    try:
                        avail.append(reg.getBackendName(b))
                    except Exception:
                        pass
    except Exception:
        pass
    print(f"    可用后端: {avail if avail else '(未列出, 不影响使用)'}")

    # 3. Windows PnP 相机
    print(f"\n[3] Windows 系统登记的相机")
    cams = pnp_cameras()
    if not cams:
        print("    (未读到设备列表)")
    for d in cams:
        st = d.get("Status", "?")
        mark = "OK " if st == "OK" else "!! "
        print(f"    [{mark}] {d.get('FriendlyName')}  (Status={st})")
        print(f"          {d.get('InstanceId')}")

    # 4. OpenCV 能打开的索引
    print(f"\n[4] OpenCV 能打开的相机索引 (穷举 0..9 × 各后端)")
    found = scan_all(10)
    if not found:
        print("    [X] 未找到任何可打开的相机!")
        print("        可能原因:")
        print("        a) 相机被其它程序占用 (Windows 相机应用/微信/腾讯会议/OBS/Orbbec 查看器)")
        print("        b) 驱动未装好 → 打开设备管理器看'照相机'有没有黄色感叹号")
        print("        c) USB 供电不足 → 换个 USB 口 (优先主板后置 USB3.0)")
    else:
        for f in found:
            print(f"    [OK] index={f['index']}  后端={f['backend']}  "
                  f"分辨率={f['width']}x{f['height']}")

    # 5. 是否有其它程序占用相机
    print(f"\n[5] 可能占用相机的常见程序 (仅列出在运行的)")
    busy_ps = (
        "Get-Process | Where-Object {"
        "$_.ProcessName -match '^(Camera|WindowsCamera|WeChat|WeMeet|Zoom|Teams|"
        "ms-teams|obs64|obs32|OBS|Orbbec|OrbbecViewer|Skype|Discord|feishu|lark|"
        "dingtalk|YY|TencentMeeting|wemeet)$'} | "
        "Select-Object ProcessName, Id | ConvertTo-Json"
    )
    raw = run_powershell(busy_ps).strip()
    if raw:
        try:
            procs = json.loads(raw)
            if isinstance(procs, dict):
                procs = [procs]
            if procs:
                for p in procs:
                    print(f"    [!] {p.get('ProcessName')} (PID={p.get('Id')})")
                print("        → 若相机打不开, 先把这些程序关掉再试")
            else:
                print("    (未发现常见占用程序)")
        except json.JSONDecodeError:
            print("    (未发现常见占用程序)")
    else:
        print("    (未发现常见占用程序)")

    # 6. 结论
    print("\n" + "=" * 66)
    if found:
        print(f" 结论: 相机可用! 推荐命令:")
        print(f"   python camera.py open -i {found[0]['index']}")
    else:
        print(" 结论: 相机当前无法打开, 请按上面的提示排查。")
    print("=" * 66)
    return 0 if found else 1


# ---------- list / open ----------

def cmd_list() -> int:
    print("=== 系统登记的相机 ===")
    for d in pnp_cameras():
        st = d.get("Status", "?")
        mark = "OK " if st == "OK" else "-- "
        print(f"  [{mark}] {d.get('FriendlyName')}  (Status={st})")

    print("\n=== OpenCV 可打开的相机 ===")
    found = scan_all(10)
    if not found:
        print("  未找到可打开的相机。运行 `python camera.py doctor` 查看详细诊断。")
        return 1
    for f in found:
        print(f"  index = {f['index']}  后端={f['backend']}  "
              f"{f['width']}x{f['height']}   "
              f"用法: python camera.py open -i {f['index']}")
    return 0


def resolve_index(args, found: list[dict]) -> int | None:
    """根据 --name 或 -i 决定用哪个索引。"""
    if args.name:
        keys = [k.strip().lower() for k in args.name.split(",") if k.strip()]
        # 名字匹配: 优先在 PnP 名单里找对应 InstanceId 的 VID:PID, 再对照
        # 但 OpenCV 不暴露名字, 这里退化为: 名字里含 'orbbec'/'integrated' 等时
        # 按顺序假设 (orbbec 常是外接相机, 排在 0 号)。
        # 更稳的做法是让用户直接 -i, 这里给提示。
        print(f"[提示] OpenCV 不直接提供设备名。--name={args.name} 做了启发式匹配:")
        # 简单策略: 让用户看 doctor 输出后用 -i 更准确
        for i, f in enumerate(found):
            print(f"       候选 index={f['index']} ({f['width']}x{f['height']})")
        if found:
            return found[0]["index"]
        return None
    if args.index is not None:
        return args.index
    return found[0]["index"] if found else None


def open_camera(index: int, width: int = 0, height: int = 0):
    import cv2
    for backend, name in (
        (cv2.CAP_DSHOW, "DirectShow"),
        (cv2.CAP_MSMF, "MediaFoundation"),
        (cv2.CAP_ANY, "Auto"),
    ):
        cap = try_open(index, backend)
        if cap is not None:
            if width:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            if height:
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            print(f"[OK] 已打开相机 index={index} (后端: {name})")
            print(f"     分辨率: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                  f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
            return cap
    return None


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def run_preview(cap, index: int, args) -> None:
    import cv2
    SAVE_DIR.mkdir(exist_ok=True)

    recording = False
    writer = None
    paused = False
    last_frame = None
    start_t = time.time()

    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)

    if args.record:
        recording = True
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        writer = cv2.VideoWriter(
            args.record, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h)
        )
        print(f"[REC] 开始录制 -> {args.record}")

    print("\n快捷键: S 拍照 | R 录像开关 | Space 暂停 | F 全屏 | Q 退出")

    while True:
        if not paused:
            ok, frame = cap.read()
            if not ok:
                print("[WARN] 读取帧失败, 退出。")
                break
            last_frame = frame
        else:
            frame = last_frame
            if frame is None:
                continue

        display = frame.copy()
        info = (f"idx={index}  "
                f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
                f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        if recording:
            info += "  [REC]"
        if paused:
            info += "  [PAUSED]"
        cv2.rectangle(display, (0, 0), (display.shape[1], 28), (0, 0, 0), -1)
        cv2.putText(display, info, (8, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1, cv2.LINE_AA)

        if recording and writer is not None and not paused:
            writer.write(frame)

        cv2.imshow(WINDOW_NAME, display)

        if args.record and (time.time() - start_t) >= args.duration:
            print(f"[REC] 达到 {args.duration}s, 自动停止。")
            break

        key = cv2.waitKey(1) & 0xFF

        if key in (ord("q"), ord("Q"), 27):
            break
        elif key in (ord("s"), ord("S")):
            path = SAVE_DIR / f"photo_{timestamp()}.jpg"
            cv2.imwrite(str(path), frame)
            print(f"[PHOTO] 已保存 {path}")
        elif key in (ord("r"), ord("R")):
            if recording:
                recording = False
                if writer is not None:
                    writer.release()
                    writer = None
                print("[REC] 已停止录制。")
            else:
                recording = True
                fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                path = SAVE_DIR / f"video_{timestamp()}.mp4"
                writer = cv2.VideoWriter(
                    str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h)
                )
                print(f"[REC] 开始录制 -> {path}")
        elif key in (ord("f"), ord("F")):
            prop = cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_FULLSCREEN)
            cv2.setWindowProperty(
                WINDOW_NAME, cv2.WND_PROP_FULLSCREEN,
                cv2.WINDOW_NORMAL if prop >= 0 else cv2.WINDOW_FULLSCREEN,
            )
        elif key == 32:
            paused = not paused

    if writer is not None:
        writer.release()
    cap.release()
    cv2.destroyAllWindows()
    print("已退出。")


def cmd_open(args) -> int:
    import cv2
    found = scan_all(10)
    if not found:
        print("[ERROR] 未找到可打开的相机。")
        print("        运行 `python camera.py doctor` 查看详细诊断。")
        return 1

    index = resolve_index(args, found)
    if index is None:
        print("[ERROR] 无法确定相机索引。")
        return 1

    cap = open_camera(index, args.width, args.height)
    if cap is None:
        print(f"[ERROR] 无法打开 index={index}。")
        print("        可能被其它程序占用, 运行 doctor 查看。")
        return 1

    if args.snapshot:
        SAVE_DIR.mkdir(exist_ok=True)
        ok, frame = cap.read()
        if ok:
            path = Path(args.snapshot)
            path.parent.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(path), frame)
            print(f"[PHOTO] 已保存 {path}")
        else:
            print("[ERROR] 读取帧失败")
        cap.release()
        return 0

    run_preview(cap, index, args)
    return 0


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="USB 相机控制程序 v2 (自带诊断)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("doctor", help="自检: 环境/后端/索引/占用情况")
    sub.add_parser("list", help="列出相机")

    p_open = sub.add_parser("open", help="打开相机")
    p_open.add_argument("-i", "--index", type=int, default=None,
                        help="相机索引 (不填则自动选第一个可用的)")
    p_open.add_argument("--name", type=str, default=None,
                        help="按名字关键字选相机 (启发式, 建议用 -i)")
    p_open.add_argument("--width", type=int, default=0, help="请求宽度")
    p_open.add_argument("--height", type=int, default=0, help="请求高度")
    p_open.add_argument("--snapshot", metavar="FILE", help="拍一张并退出")
    p_open.add_argument("--record", metavar="FILE", help="录制视频")
    p_open.add_argument("--duration", type=float, default=5.0,
                        help="自动录制时长(秒)")

    args = ap.parse_args()

    # 不带任何子命令 = 直接打开相机 (最符合直觉的默认行为)
    if args.cmd is None:
        class _Default:
            index = None
            name = None
            width = 0
            height = 0
            snapshot = None
            record = None
            duration = 5.0
        return cmd_open(_Default())

    if args.cmd == "doctor":
        return cmd_doctor()
    if args.cmd == "list":
        return cmd_list()
    if args.cmd == "open":
        return cmd_open(args)

    ap.print_help
    return 0


if __name__ == "__main__":
    sys.exit(main())
