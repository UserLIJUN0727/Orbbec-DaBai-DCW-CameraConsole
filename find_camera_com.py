# -*- coding: utf-8 -*-
"""
查找"相机对应的 COM 口"

原理: USB 设备的 InstanceId 里都带 VID(厂商标识) 和 PID(产品标识)。
      把相机的 VID:PID 和串口的 VID:PID 对一下, 对得上才是同一个物理设备的串口通道。

用法:
  python find_camera_com.py            查询并打印结果
"""

from __future__ import annotations

import re
import subprocess
import sys

PS = (
    "Get-PnpDevice -ErrorAction SilentlyContinue | "
    "Select-Object Status, Class, FriendlyName, InstanceId | "
    "ConvertTo-Json -Depth 3"
)


def run_powershell(command: str) -> str:
    """执行 PowerShell 并稳妥地解码输出。

    中文 Windows 上 PowerShell 默认按 GBK 输出, 直接按 UTF-8 解码会抛
    UnicodeDecodeError。这里强制 PowerShell 以 UTF-8 输出, 并对残留的
    非法字节用 U+FFFD 替换, 保证脚本任何环境下都能跑。
    """
    prefixed = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", prefixed],
        capture_output=True, timeout=30,
    )
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936"):
        try:
            return out.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
    return out.stdout.decode("utf-8", errors="replace")


def query_pnp() -> list[dict]:
    import json
    raw = run_powershell(PS).strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


VIDPID_RE = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.I)


def vid_pid(instance_id: str) -> str | None:
    m = VIDPID_RE.search(instance_id or "")
    return f"{m.group(1).upper()}:{m.group(2).upper()}" if m else None


def main() -> int:
    devices = query_pnp()
    if not devices:
        print("[ERROR] 未能读取系统设备列表")
        return 1

    cameras = [d for d in devices if (d.get("Class") or "").lower() == "camera"]
    ports = [d for d in devices if (d.get("Class") or "").lower() == "ports"]

    print("=" * 62)
    print("【1】系统中的相机")
    print("=" * 62)
    if not cameras:
        print("  (未发现相机设备)")
    cam_vidpids = {}
    for d in cameras:
        vp = vid_pid(d.get("InstanceId", ""))
        print(f"  [{d.get('Status'):<8}] {d.get('FriendlyName')}")
        print(f"             VID:PID = {vp}   InstanceId = {d.get('InstanceId')}")
        if vp:
            cam_vidpids.setdefault(vp, []).append(d.get("FriendlyName"))

    print()
    print("=" * 62)
    print("【2】系统中的串口(COM)")
    print("=" * 62)
    if not ports:
        print("  (未发现任何串口)")
    for d in ports:
        vp = vid_pid(d.get("InstanceId", ""))
        print(f"  [{d.get('Status'):<8}] {d.get('FriendlyName'):<40} VID:PID = {vp}")

    print()
    print("=" * 62)
    print("【3】匹配结果: 相机里的串口")
    print("=" * 62)
    hit = False
    for d in ports:
        vp = vid_pid(d.get("InstanceId", ""))
        if vp and vp in cam_vidpids:
            hit = True
            print(f"  >>> 命中! {d.get('FriendlyName')}")
            print(f"        VID:PID = {vp}  属于相机: {cam_vidpids[vp]}")
    if not hit:
        print("  没有任何 COM 口属于相机。")
        print("  说明: 相机只走了图像通道(UVC/厂商驱动), 没有暴露串口。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
