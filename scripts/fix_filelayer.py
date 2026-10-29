#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
fix_filelayer.py — 厂商量产态过滤层（mpszfilt）安装/卸载/校验

针对 Alcor 主控：量产态(MP Mode)下标准 SCSI WRITE 不被接受，必须由 mpszfilt.sys
翻译成厂商私有命令。缺了它 → 写入 ACK 成功但数据不落盘，或 104 秒超时 err=121。

用法:
    python fix_filelayer.py --status              # 查看类级 LowerFilters + 服务 + Enum 计数
    python fix_filelayer.py --install mpszfilt    # 写入类级 LowerFilters（需管理员）
    python fix_filelayer.py --uninstall mpszfilt  # 摘除
    python fix_filelayer.py --install mpszfilt --loaddrv "C:\\...\\LoadDrv.exe"
                                              # 用厂商官方工具一键安装（优先用这个）

踩坑备忘（勿删）:
  1. USB 类 GUID 是 {36fc9e60-c465-11cf-8056-444553540000}；
     {36D5F765-E261-11CE-AFC5-00AA00389B71} 是 DiskDrive 类，Alcor 不装那里。
     查错 GUID 会一直得到"滤镜没装"的假结论。
  2. REG_MULTI_SZ 必须传 list[str]，传 bytes 会抛
     ValueError: Could not convert the data to the specified type。
  3. DEVPKEY_Device_LowerFilters 只含实例级值，不含类级 → 看 Enum\\Count 判断。
  4. 装完滤镜必须让设备重新枚举，否则滤镜挂不到已有实例上。
"""
import argparse
import subprocess
import sys
import winreg

USB_CLASS = r"{36fc9e60-c465-11cf-8056-444553540000}"
DISK_CLASS = r"{36D5F765-E261-11CE-AFC5-00AA00389B71}"
CLASS_ROOT = r"SYSTEM\CurrentControlSet\Control\Class"


def read_lf(guid):
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CLASS_ROOT + "\\" + guid, 0, winreg.KEY_READ)
    except OSError:
        return None, "键不存在"
    try:
        v, _ = winreg.QueryValueEx(k, "LowerFilters")
        return list(v), "ok"
    except OSError:
        return [], "无 LowerFilters 值"
    finally:
        winreg.CloseKey(k)


def write_lf(guid, items):
    """写 REG_MULTI_SZ。items 必须是 list[str] —— 传 bytes 会报 ValueError。"""
    k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CLASS_ROOT + "\\" + guid, 0, winreg.KEY_ALL_ACCESS)
    try:
        winreg.SetValueEx(k, "LowerFilters", 0, winreg.REG_MULTI_SZ, list(items))
        return True, None
    finally:
        winreg.CloseKey(k)


def svc_status(name):
    try:
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                           r"SYSTEM\CurrentControlSet\Services\%s" % name, 0, winreg.KEY_READ)
    except OSError as e:
        return {"exists": False, "err": str(e)}
    d = {}
    try:
        for field in ("Start", "Type", "ErrorControl", "Group", "Tag"):
            try:
                d[field] = winreg.QueryValueEx(k, field)[0]
            except OSError:
                d[field] = None
        try:
            ek = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Services\%s\Enum" % name)
            try:
                d["EnumCount"] = winreg.QueryValueEx(ek, "Count")[0]
            except OSError:
                # Enum 键存在但没有 Count 值 = 驱动已装但当前没有设备绑定
                # （Count 是 REG_DWORD；某些版本写成字符串，兼容两种）
                d["EnumCount"] = 0
                for idx in range(0, 32):
                    try:
                        winreg.QueryValueEx(ek, "Count%d" % idx)
                        d["EnumCount"] = idx + 1
                    except OSError:
                        break
            winreg.CloseKey(ek)
        except OSError:
            d["EnumCount"] = None
    finally:
        winreg.CloseKey(k)
    d["exists"] = True
    return d


def run(cmd, timeout=120):
    print("  执行: %s" % " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
        print("  rc=%s" % p.returncode)
        for ln in (p.stdout or "").splitlines():
            if ln.strip():
                print("  OUT| %s" % ln.rstrip())
        for ln in (p.stderr or "").splitlines():
            if ln.strip():
                print("  ERR| %s" % ln.rstrip())
        return p.returncode
    except Exception as e:
        print("  EXC: %r" % (e,))
        return -1


def status(name):
    print("=== 类级 LowerFilters ===")
    for guid, tag in ((USB_CLASS, "USB 类（Alcor 用这个）"), (DISK_CLASS, "DiskDrive 类")):
        v, msg = read_lf(guid)
        if v is None:
            print("  %-24s %s → %s" % (tag, guid[:14], msg))
        else:
            print("  %-24s %s → %s" % (tag, guid[:14], v))

    print("\n=== 驱动服务 %s ===" % name)
    st = svc_status(name)
    if not st.get("exists"):
        print("  服务不存在: %s" % st.get("err"))
    else:
        print("  Start=%s (1=System, 2=Auto, 3=Manual, 4=Disabled)" % st.get("Start"))
        print("  Type=%s (1=Kernel Driver)" % st.get("Type"))
        print("  Enum\\Count=%s  ★ >=1 表示滤镜已挂到至少一个设备实例" % st.get("EnumCount"))
        if st.get("EnumCount") == 0:
            print("    ⚠ Enum 键存在但无绑定实例 → 驱动已装但没挂到任何设备上。")
            print("      常见原因：设备实例创建于滤镜安装之前 → 必须重新枚举设备。")

    print("\n=== 驱动文件 ===")
    for p in (r"C:\Windows\System32\drivers\mpszfilt.sys",):
        import os
        print("  %s : %s" % (p, "存在 (%d 字节)" % os.path.getsize(p)
                             if os.path.exists(p) else "缺失"))


def main():
    ap = argparse.ArgumentParser(description="厂商过滤层管理")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--install", metavar="SVC")
    ap.add_argument("--uninstall", metavar="SVC")
    ap.add_argument("--loaddrv", metavar="EXE", help="LoadDrv.exe 路径，配合 --install 一键安装")
    args = ap.parse_args()

    name = args.install or args.uninstall or "mpszfilt"

    if args.loaddrv and args.install:
        # 厂商官方工具优先：一条命令完成 写类级LowerFilters + 装服务 + 重置Hub
        rc = run([args.loaddrv, "InstByIni"])
        print("  LoadDrv rc=%s (0=成功)" % rc)
        print("  ★ 下一步必须让设备重新枚举，否则滤镜挂不到已有实例：")
        print("     pnputil /restart-device <实例ID>  或重启系统")
        status(name)
        return 0

    if args.install:
        cur, msg = read_lf(USB_CLASS)
        if cur is None:
            print("USB 类键不存在，先确认是不是查错了类 GUID。当前: %s" % msg)
            return 1
        items = list(cur)
        if name not in items:
            items.append(name)
        try:
            write_lf(USB_CLASS, items)
        except ValueError as e:
            print("写 REG_MULTI_SZ 失败: %s" % e)
            print("  ★ 提醒：REG_MULTI_SZ 必须传 list[str]，不能传 bytes")
            return 1
        print("已写入 %s → %s" % (USB_CLASS, items))
        print("★ 装完必须让设备重新枚举（pnputil /restart-device 或重启系统）")
        status(name)
        return 0

    if args.uninstall:
        cur, msg = read_lf(USB_CLASS)
        if cur is None:
            print("USB 类键不存在: %s" % msg)
            return 1
        items = [x for x in cur if x != name]
        write_lf(USB_CLASS, items)
        print("已从 %s 摘除 %s → %s" % (USB_CLASS, name, items))
        print("★ 之后需 sc delete %s + 删 .sys + 重启" % name)
        return 0

    status(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
