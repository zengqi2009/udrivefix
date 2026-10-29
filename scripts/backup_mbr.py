#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
backup_mbr.py — 备份 / 还原 PhysicalDrive 的 MBR

任何裸盘写操作之前必须先跑这个。实战教训：一次写测试漏了 SetFilePointerEx，
数据直接落到 LBA 0，把分区表写坏，只能靠这份备份救回来。

用法:
    python backup_mbr.py --disk 1 --backup            # 备份到 mbr_backup_dN.bin
    python backup_mbr.py --disk 1 --restore           # 从备份还原
    python backup_mbr.py --disk 1 --info              # 只看分区表

注意：MBR 只有一个扇区(512B)。不要试图备份整盘。
"""
import argparse
import ctypes
import os
import sys
import time

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02

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


def open_disk(n, write):
    access = GENERIC_READ | (GENERIC_WRITE if write else 0)
    h = k32.CreateFileW(r"\\.\PhysicalDrive%d" % n, access,
                        FILE_SHARE_READ | FILE_SHARE_WRITE, None,
                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if h == -1 or h == 0xFFFFFFFFFFFFFFFF:
        return None, ctypes.get_last_error()
    return h, 0


def read_lba0(h):
    if k32.SetFilePointerEx(h, ctypes.c_longlong(0), None, 0) == 0:
        return None, ctypes.get_last_error()
    buf = ctypes.create_string_buffer(512)
    n = ctypes.c_ulong(0)
    if not k32.ReadFile(h, buf, 512, ctypes.byref(n), None):
        return None, ctypes.get_last_error()
    return buf.raw[:512], 0


def write_lba0(h, data):
    assert len(data) == 512
    if k32.SetFilePointerEx(h, ctypes.c_longlong(0), None, 0) == 0:
        return False, ctypes.get_last_error()
    buf = ctypes.create_string_buffer(data, 512)
    n = ctypes.c_ulong(0)
    ok = k32.WriteFile(h, buf, 512, ctypes.byref(n), None)
    if ok:
        k32.FlushFileBuffers(h)
    return bool(ok), ctypes.get_last_error()


def parse_mbr(mbr):
    """按正确偏移解析分区表：项从 0x1BE 起，type@+4 startLBA@+8 sectors@+12"""
    print("  MBR 签名: %s" % ("0x%02X%02X (期望 55aa)" % (mbr[510], mbr[511])))
    print("  分区表:")
    for i in range(4):
        e = mbr[0x1BE + i * 16: 0x1BE + (i + 1) * 16]
        ptype = e[4]
        start = int.from_bytes(e[8:12], "little")
        sect = int.from_bytes(e[12:16], "little")
        if ptype == 0:
            print("    [%d] type=0x00 (空)" % i)
        else:
            print("    [%d] type=0x%02X  startLBA=%d  sectors=%d  (%.2f GiB)" % (
                i, ptype, start, sect, sect * 512 / 1024 ** 3))


def main():
    ap = argparse.ArgumentParser(description="MBR 备份/还原")
    ap.add_argument("--disk", type=int, required=True)
    ap.add_argument("--backup", action="store_true")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--info", action="store_true")
    args = ap.parse_args()

    h, err = open_disk(args.disk, write=args.restore)
    if h is None:
        print("打开 PhysicalDrive%d 失败 err=%d" % (args.disk, err))
        return 1

    mbr, rerr = read_lba0(h)
    if mbr is None:
        print("读 LBA0 失败 err=%d" % rerr)
        k32.CloseHandle(h)
        return 1

    if args.backup:
        path = "mbr_backup_d%d.bin" % args.disk
        with open(path, "wb") as f:
            f.write(mbr)
        print("已备份 MBR -> %s (%d 字节)" % (path, len(mbr)))
        parse_mbr(mbr)

    elif args.restore:
        path = "mbr_backup_d%d.bin" % args.disk
        if not os.path.exists(path):
            print("找不到备份文件: %s" % path)
            k32.CloseHandle(h)
            return 1
        with open(path, "rb") as f:
            data = f.read()
        if len(data) != 512:
            print("备份文件大小异常: %d" % len(data))
            k32.CloseHandle(h)
            return 1
        ok, werr = write_lba0(h, data)
        print("还原 MBR: ok=%s err=%d" % (ok, werr))
        if ok:
            h2, _ = open_disk(args.disk, write=False)
            chk, _ = read_lba0(h2)
            k32.CloseHandle(h2)
            print("回读校验: %s" % ("一致" if chk == data else "不一致 ★"))
            parse_mbr(chk)

    else:  # --info
        parse_mbr(mbr)

    k32.CloseHandle(h)
    return 0


if __name__ == "__main__":
    sys.exit(main())
