# udrivefix

> Windows 下 U 盘 / 移动硬盘「不可写入、只读、无法识别、容量异常、文件系统损坏」故障的**分层诊断与救援**技能。

一个 WorkBuddy Skill（内部代号 `usb-fault-rescue`），同时可用作独立诊断手册。

---

## 这个技能解决什么问题

「U 盘写不进去」**不是一个故障，而是至少 5 个不同故障的共同表象**：

| 真正的根因 | 表面症状 | 关键判据 |
|---|---|---|
| 设备根本不在总线 | 完全不识别 | `Win32_DiskDrive` 无此型号 + `PhysicalDriveN` 全部 err=2 |
| 卷锁传播到物理盘 | 写报拒绝访问 | 卸载卷后裸盘写立刻成功 |
| 文件系统损坏 | chkdsk 报错误 50 | `chkdsk` 只读分析瞬间跑完 |
| 主控固件置写保护 | 写回标志失败且回读不变 | `VolumeFlags` 回读仍是 `0x0002` |
| 量产态翻译层缺失 | **ACK 成功但数据不落盘** | `mpszfilt\Enum\Count` 与写-回读比对 |

本技能的价值不在于"提供一招解决"，而在于**用 1 分钟的分层定位，把上面 5 种可能区分开**，
避免在错误方向上反复施压耗掉几个小时。

---

## 核心主张：先定位层级，再动手

```
L0 物理在位   → U 盘此刻真的在总线上吗？当前枚举的实例是哪一个？
L1 块设备层   → 磁盘能打开吗？能读吗？读多快？写得到吗？写到哪失败？
L2 卷层       → 分区/卷挂上了吗？卷被锁了吗？只读标志在哪一位？
L3 文件系统层 → 结构自洽吗？哪些目录项坏了？
L4 控制器层   → 主控固件自己置了写保护/坏块标志吗？
L5 厂商层     → 量产态驱动过滤器装对了吗？装到对的那个类 GUID 了吗？
```

**每层拿到结论后才进入下一层。** 这是整套流程唯一重要的一条。

---

## 快速开始

```bash
# 一键分层体检（只读）
python scripts/probe.py

# 指定盘号 + 追加写-回读一致性测试（会真实写数据）
python scripts/probe.py --disk 1 --write

# 裸盘操作前，务必先备份 MBR
python scripts/backup_mbr.py --disk 1 --backup

# 查看/安装厂商量产态过滤层（需管理员）
python scripts/fix_filelayer.py --status
python scripts/fix_filelayer.py --install mpszfilt --loaddrv "C:\path\to\LoadDrv.exe"
```

---

## 三条最容易踩的坑

### 1. 写成功 ≠ 写进去了

Windows 的 `WriteFile` 可能返回 `ok=True, err=0`，而主控固件**静默丢弃**了这条写命令。

**唯一有效的判据是回读逐字节比对：**

```
写入 → FlushFileBuffers → Close → 重新打开 → 读同一 LBA → 逐字节比对
```

### 2. 注册表里有 ≠ 设备此刻在总线上

Windows 会永久保留同型号 U 盘的旧设备节点。判断当前设备必须看：

```
DEVPKEY_Device_LastArrivalDate   → 是最近时间 = 当前设备
DEVPKEY_Device_LastRemovalDate   → 停在很久以前 = 注册表幽灵节点
```

### 3. `chkdsk /f /x` 会制造新故障

`/x` 会卸载卷并加排他锁，在写失效的设备上引发 `disk 153` IO 重试死循环，
且 chkdsk 卡在内核态连 `taskkill /F` 都杀不掉。

**修复阶段只用 `/f`，永远不要加 `/x`。**

---

## ⚠️ 运行环境限制

**本技能只能在 Windows 桌面环境的本机执行，不能在云端沙箱运行。**

全部诊断依赖本机的物理盘句柄（`\\.\PhysicalDriveN`）、注册表、驱动服务、WMI 与 Windows 事件日志。
`scripts/` 下的脚本通过 `ctypes` 加载 `kernel32.dll`、读写 `HKLM` 注册表、调用
`chkdsk.exe` / `pnputil.exe`，在非 Windows 或无设备访问权的环境中必然失败。

以下操作需要**管理员权限**：
- 打开物理盘读写句柄
- 修改 `HKLM` 注册表（类级 LowerFilters）
- 执行 `chkdsk /f`（会卸载卷并安排重启修复）

---

## 目录结构

```
udrivefix/
├── SKILL.md                      # 技能定义：分层判定树 + 执行流程 + 反模式清单
├── references/
│   ├── probe_recipes.md          # 可复制的探测片段（PnP 身份/写测试/exFAT 解析/chkdsk）
│   └── vendor_alcor_mp.md        # Alcor 量产态驱动全解 + 官方工具用法
└── scripts/
    ├── probe.py                  # 一键分层体检
    ├── backup_mbr.py             # MBR 备份/还原
    └── fix_filelayer.py          # 厂商过滤层装卸与校验
```

---

## 安装

**方式一：WorkBuddy 技能市场**

在 WorkBuddy 中搜索「U盘故障诊断与救援」安装，或直接下载本仓库的
`usb-fault-rescue.zip` 解压到 `~/.workbuddy/skills/usb-fault-rescue/`。

**方式二：克隆后手动安装**

```bash
git clone git@github.com:zengqi2009/udrivefix.git
# 把 SKILL.md / references/ / scripts/ 三个目录整体复制到
# ~/.workbuddy/skills/usb-fault-rescue/ 即可被 WorkBuddy 识别
```

**方式三：纯命令行使用（无需安装技能）**

直接克隆本仓库，把 `scripts/probe.py` 当作独立诊断工具跑：
`python scripts/probe.py --disk 1 --write`。

---

## 致谢

本技能的判定树、API 踩坑清单与反模式清单，来自一次真实的联想 thinkplus MU90 128GB
不可写入故障的完整排障过程（2026-10-29/30）。所有结论均有脚本日志佐证。

那次排障也留下了血的教训——把幽灵节点当当前设备、查错类 GUID、
手工改注册表而不用厂商工具、只看 API 返回值就宣布修复成功。
这些都已写进 SKILL.md 的反模式清单。

---

## License

MIT
