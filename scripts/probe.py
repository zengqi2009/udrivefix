#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
probe.py — U 盘故障分层体检（只读为主，写测试可选）

用法:
    python probe.py                    # 全自动：定位设备 + 只读体检
    python probe.py --write            # 追加写-回读一致性测试（会写数据！）
    python probe.py --disk 1 --write   # 指定物理盘号
    python probe.py --model MU90       # 按型号关键字定位

分层输出:
    L0 物理在位 / L1 块设备读写 / L2 卷与过滤器 / L3 事件日志

设计要点（源自实战踩坑，勿删）:
  - 物理盘一次只能写 1 个扇区(512B)，写多扇区会 err=87
  - 写前必须 SetFilePointerEx 定位，否则会写坏 LBA0 的 MBR
  - 写成功必须靠"回读逐字节比对"判定，WriteFile 返回值不作数
  - 用 Win32_Volume 不用 Get-Volume（坏卷上 Get-Volume 会阻塞）
"""
import argparse
import ctypes
import os
import subprocess
import sys
import time

# ---------- Win32 常量 ----------
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
IOCTL_DISK_GET_LENGTH_INFO = 0x00000007

k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong, ctypes.c_ulong,
                             ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
k32.CreateFileW.restype = ctypes.c_void_p
k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                         ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
k32.ReadFile.restype = ctypes.c_int
k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                          ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
k32.WriteFile.restype = ctypes.c_int
k32.SetFilePointerEx.argtypes = [ctypes.c_void_p, ctypes.c_longlong,
                                 ctypes.POINTER(ctypes.c_longlong), ctypes.c_ulong]
k32.SetFilePointerEx.restype = ctypes.c_int
k32.FlushFileBuffers.argtypes = [ctypes.c_void_p]
k32.FlushFileBuffers.restype = ctypes.c_int
k32.CloseHandle.argtypes = [ctypes.c_void_p]
k32.CloseHandle.restype = ctypes.c_int
k32.DeviceIoControl.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                 ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong,
                                 ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
k32.DeviceIoControl.restype = ctypes.c_int


class DISK_LENGTH_INFO(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_longlong)]


def hdr(t):
    print("\n" + "=" * 72)
    print("  " + t)
    print("=" * 72)


def open_disk(n, write=False):
    """打开物理盘。write=True 时申请读写权限。"""
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    share = FILE_SHARE_READ | FILE_SHARE_WRITE
    h = k32.CreateFileW(r"\\.\PhysicalDrive%d" % n, access, share, None,
                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
        return None, ctypes.get_last_error()
    return h, 0


def disk_size(h):
    dli = DISK_LENGTH_INFO()
    n = ctypes.c_ulong(0)
    if k32.DeviceIoControl(h, IOCTL_DISK_GET_LENGTH_INFO, None, 0,
                           ctypes.byref(dli), ctypes.sizeof(dli), ctypes.byref(n), None):
        return dli.Length
    return -1


def read_sectors(h, lba, count=1):
    """按 LBA 读，返回 (bytes, err)。"""
    if k32.SetFilePointerEx(h, ctypes.c_longlong(lba * 512), None, 0) == 0:
        return None, ctypes.get_last_error()
    buf = ctypes.create_string_buffer(512 * count)
    n = ctypes.c_ulong(0)
    if not k32.ReadFile(h, buf, 512 * count, ctypes.byref(n), None):
        return None, ctypes.get_last_error()
    return buf.raw[:n.value], 0


def write_sector(h, lba, payload512):
    """单扇区写。必须 512 字节。返回 (ok, written, err)。"""
    assert len(payload512) == 512, "物理盘一次只能写 1 个扇区(512字节)"
    if k32.SetFilePointerEx(h, ctypes.c_longlong(lba * 512), None, 0) == 0:
        return False, 0, ctypes.get_last_error()
    buf = ctypes.create_string_buffer(payload512, 512)
    n = ctypes.c_ulong(0)
    ok = k32.WriteFile(h, buf, 512, ctypes.byref(n), None)
    if ok:
        k32.FlushFileBuffers(h)
    return bool(ok), n.value, ctypes.get_last_error()


def ps(cmd, timeout=45):
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return (p.stdout or "").strip()
    except Exception as e:
        return "(查询失败: %r)" % (e,)


def ps_file(path, timeout=120):
    """执行 .ps1 文件。复杂脚本用文件而非 -Command，避免引号嵌套地狱。"""
    try:
        p = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        return (p.stdout or "").strip()
    except Exception as e:
        return "(查询失败: %r)" % (e,)


# ---------------- L0 物理在位 ----------------
def l0_identify(model_kw):
    hdr("L0  物理在位 —— 设备此刻真的在总线上吗？")
    out = ps("Get-CimInstance Win32_DiskDrive | ForEach-Object { "
             "\"$($_.Index)|$($_.Model)|$($_.Status)|$($_.Size)|$($_.PNPDeviceID)\" }")
    print("  Win32_DiskDrive:")
    disks = []
    for line in out.splitlines():
        if "|" in line:
            print("    " + line)
            parts = line.split("|")
            try:
                disks.append((int(parts[0]), parts[1], int(parts[3])))
            except Exception:
                pass
    if not disks:
        print("    (无磁盘)")
        return None

    target = None
    if model_kw:
        for idx, model, size in disks:
            if model_kw.lower() in model.lower():
                target = idx
                break
    if target is None:
        # 选唯一的非系统盘，或容量最大的非系统盘
        cands = [d for d in disks if d[0] != 0]
        if len(cands) == 1:
            target = cands[0][0]
        elif cands:
            target = max(cands, key=lambda d: d[2])[0]
    print("\n  选定 PhysicalDrive%s" % (target if target is not None else "?"))

    # 幽灵节点判据
    print("\n  PnP 身份（含 LastArrival/LastRemoval —— 判断幽灵节点的关键）:")
    # 注意：Get-PnpDevice + 逐设备 Get-PnpDeviceProperty 在设备多时极慢（实测可 >45s），
    # 因此先取列表再批量取属性，并把超时放宽。宁可慢，不要错。
    ps_script = (
        "$ErrorActionPreference='SilentlyContinue'\n"
        "$ids = Get-PnpDevice | Where-Object { $_.InstanceId -like 'USB*' } | "
        "Select-Object -ExpandProperty InstanceId\n"
        "foreach ($id in $ids) {\n"
        "  $la = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_LastArrivalDate').Data\n"
        "  $lr = (Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_LastRemovalDate').Data\n"
        "  \"$id|arrival=$la|removal=$lr\"\n"
        "}\n"
    )
    tmp = os.path.join(os.environ.get("TEMP", "."), "_wb_pnp_id.ps1")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(ps_script)
    q = ps_file(tmp, timeout=240)
    for line in q.splitlines():
        if "|" in line:
            print("    " + line)
    print("\n  ★ 解读：arrival 是最近时间的才是当前设备；removal 停在很久以前的=注册表幽灵节点。")
    return target


# ---------------- L1 块设备 ----------------
def l1_probe(disk, do_write):
    hdr("L1  块设备层 —— 能打开吗？能读吗？能写吗？")
    h, err = open_disk(disk, write=False)
    if h is None:
        print("  读句柄打开失败 err=%d（设备可能不在位）" % err)
        return
    size = disk_size(h)
    print("  只读句柄: OK   容量=%d 字节 (%.2f GiB)" % (size, size / 1024 ** 3))
    total_sectors = size // 512

    # 顺序读
    t0 = time.time()
    data, rerr = read_sectors(h, 2048, 16)   # 8MB
    dt = time.time() - t0
    if data:
        print("  顺序读 8MB: OK  %.2fs  (%.2f MB/s)" % (dt, 8 / max(dt, 1e-6)))
        print("    ★ 读通道健康 → 排除介质/硬件损坏，问题在写侧")
    else:
        print("  顺序读 8MB: FAIL err=%d" % rerr)
    k32.CloseHandle(h)

    if not do_write:
        print("\n  (未执行写测试，加 --write 开启；写测试会真实写入数据)")
        return

    # 写-回读一致性
    print("\n  写-回读一致性测试（多点）:")
    targets = [max(2048, total_sectors // 4), max(2048, total_sectors // 2),
               max(2048, total_sectors - 2048)]
    ok_cnt = 0
    for lba in targets:
        orig, e1 = read_sectors(h, lba, 1) if False else (None, 0)
        # 重新开读写句柄
        hw, e = open_disk(disk, write=True)
        if hw is None:
            print("    LBA %-10d 读写句柄失败 err=%d" % (lba, e))
            k32.CloseHandle(h)
            return
        orig, e1 = read_sectors(hw, lba, 1)
        if orig is None:
            print("    LBA %-10d 读原数据失败 err=%d" % (lba, e1))
            k32.CloseHandle(hw)
            continue
        tag = b"WBPROBE" + b"\x00" * (512 - 7)
        t1 = time.time()
        ok, written, werr = write_sector(hw, lba, tag)
        dt = time.time() - t1
        k32.CloseHandle(hw)

        if not ok:
            print("    LBA %-10d 写失败 err=%d 耗时=%.1fs" % (lba, werr, dt))
            continue
        # 重新打开回读
        hr, _ = open_disk(disk, write=False)
        back, berr = read_sectors(hr, lba, 1)
        k32.CloseHandle(hr)
        if back is None:
            print("    LBA %-10d 回读失败 err=%d" % (lba, berr))
            continue
        same = (back == tag)
        if same:
            ok_cnt += 1
        print("    LBA %-10d 写ACK=%s 回读一致=%s 耗时=%.2fs" % (lba, ok, same, dt))
        # 还原
        hr2, _ = open_disk(disk, write=True)
        if hr2:
            write_sector(hr2, lba, orig)
            k32.CloseHandle(hr2)
    print("\n  成功点 %d / %d" % (ok_cnt, len(targets)))
    if ok_cnt == len(targets):
        print("  ★ 写通道正常")
    elif ok_cnt == 0:
        print("  ★ 全挂 → 控制器级写失效（继续看 L4/L5）")
    else:
        print("  ★ 部分可写 → 疑似坏块/局部失效")


# ---------------- L2 卷与过滤器 ----------------
def l2_volumes():
    hdr("L2  卷层与过滤层")
    print("  卷（Win32_Volume，不阻塞）:")
    out = ps("Get-CimInstance Win32_Volume | ForEach-Object { "
             "\"$($_.DriveLetter)|$($_.FileSystem)|$($_.Capacity)|Health=$($_.HealthStatus)|Label=$($_.Label)\" }")
    for line in out.splitlines():
        if "|" in line:
            print("    " + line)

    print("\n  USB 类 LowerFilters:")
    for guid, tag in [("{36fc9e60-c465-11cf-8056-444553540000}", "USB 类(Alcor 用这个)"),
                      ("{36D5F765-E261-11CE-AFC5-00AA00389B71}", "DiskDrive 类")]:
        v = ps("(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\%s' "
               "-Name LowerFilters -ErrorAction SilentlyContinue).LowerFilters" % guid)
        if v.strip():
            print("    %-22s %s = %s" % (tag, guid[:12], v))
        else:
            print("    %-22s %s = (无此键或无值)" % (tag, guid[:12]))

    print("\n  驱动服务 Enum 计数（判定滤镜是否真挂到实例）:")
    out = ps("Get-ChildItem 'HKLM:\\SYSTEM\\CurrentControlSet\\Services' | Where-Object { "
             "$_.PSChildName -like 'mpszfilt*' -or $_.PSChildName -like '*zfilt*' } | ForEach-Object { "
             "\"$($_.PSChildName)|State=$((Get-ItemProperty $_.PSPath).Start)|EnumCount=$((Get-ItemProperty "
             "($_.PSPath + '\\Enum') -Name Count -ErrorAction SilentlyContinue).Count)\" }")
    for line in out.splitlines():
        if "|" in line:
            print("    " + line)
    print("    ★ Enum\\Count >= 1 表示滤镜已挂到至少一个设备实例")


# ---------------- L3 事件日志 ----------------
def l3_events():
    hdr("L3  事件日志 —— 磁盘与 PnP")
    for eid, desc in [(153, "IO 重试（控制器无响应）"), (157, "磁盘被移除"), (225, "进程停止删除/弹出设备")]:
        out = ps("Get-WinEvent -FilterHashtable @{LogName='System';Id=%d} -MaxEvents 5 "
                 "-ErrorAction SilentlyContinue | ForEach-Object { "
                 "\"$($_.TimeCreated)|$($_.Message -replace '\\s+',' ')\" }" % eid)
        lines = [l for l in out.splitlines() if l.strip() and "|" in l]
        print("  disk/Kernel-PnP 事件 %d (%s): %d 条" % (eid, desc, len(lines)))
        for l in lines[:5]:
            print("    " + l[:150])


def main():
    ap = argparse.ArgumentParser(description="U 盘故障分层体检")
    ap.add_argument("--disk", type=int, help="指定物理盘号")
    ap.add_argument("--model", help="按型号关键字定位（如 MU90）")
    ap.add_argument("--write", action="store_true", help="执行写-回读测试（会写数据）")
    args = ap.parse_args()

    disk = args.disk
    if disk is None:
        disk = l0_identify(args.model)
    if disk is None:
        print("\n无法定位目标磁盘，请用 --disk 指定。")
        return 1

    l1_probe(disk, args.write)
    l2_volumes()
    l3_events()

    hdr("结论提示")
    print("""
  · 读正常 + 写全挂            → 控制器级写失效 / 量产态翻译层缺失 → 查 L4、L5
  · 卸载卷后能写                → 卷锁 → 重新挂载并修文件系统
  · 部分 LBA 能写                → 局部坏块
  · 事件 153 反复出现           → 控制器无响应
  · chkdsk /f 后修复            → 文件系统层问题
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
