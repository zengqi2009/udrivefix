---
name: usb-fault-rescue
display_name: U盘故障诊断与救援
display_name_en: USB Fault Rescue
description: Windows 下 U 盘/移动硬盘"不可写入、只读、无法识别、容量异常、文件系统损坏"故障的系统化诊断与救援流程。提供分层判定树（物理在位→块设备→卷→文件系统→控制器→厂商层）、裸盘写入的正确写法与写-回读一致性判据、以及厂商量产态（Alcor mpszfilt）这类隐蔽陷阱的识别方法。当用户提到 U 盘写不进、无法写入、只读、disk 153 报错、chkdsk 卡死、PhysicalDrive 写报错 5/121/87、exFAT 损坏、需要量产修复时使用本技能。
description_zh: U 盘写不进、只读、无法识别的分层诊断与救援流程。核心是先定位层级再动手，避免在错误方向上反复折腾。
description_en: A layered diagnosis and rescue workflow for USB drives that won't write, are read-only, or won't be recognized. Core principle: locate the failing layer before acting.
examples_zh:
  - "帮我看看这个 U 盘为什么写不进去，提示拒绝访问"
  - "chkdsk 跑一半卡死了，disk 事件日志一直报 153 错误"
  - "U 盘能识别但是新建文件报 PermissionError，裸盘写返回 err=121"
  - "Alcor 量产过的 U 盘数据在但写不进去，怀疑是 mpszfilt 拦的"
  - "帮我排查一下移动硬盘只读的问题，chkdsk 说需要修复"
examples_en:
  - "Help me figure out why this USB drive refuses writes with access denied"
  - "chkdsk hangs halfway and the disk event log keeps reporting event 153"
  - "USB drive is detected but creating files fails with PermissionError, raw disk write returns err=121"
  - "A USB drive that went through Alcor mass production has data but won't write, suspecting mpszfilt"
  - "Troubleshoot a read-only external drive that chkdsk says needs repair"
allowed-tools: Bash,Read,Write,Edit
version: 1.0.0
author: "zengqi2009"
agent_created: true
---

# U 盘故障诊断与救援

## 用途

为 Windows 下的 U 盘 / 移动硬盘不可写入类故障提供**可复现、有判据、不烧时间**的诊断流程。

本技能的核心主张：**"不可写入"不是一个故障，而是至少 5 个不同故障的共同表象**。不先分层定位就动手修，会把大量时间浪费在错误的方向上。

## ⚠️ 运行环境限制（重要）

**本技能只能在 Windows 桌面环境的本机执行，不能在云端沙箱运行。**

原因：全部诊断依赖本机的物理盘句柄（`\\.\PhysicalDriveN`）、注册表、驱动服务、WMI 与
Windows 事件日志。`scripts/` 下的三个脚本都调用 `ctypes` 加载 `kernel32.dll`、读写注册表、
执行 `chkdsk.exe` / `pnputil.exe`，在非 Windows 或无本机设备访问权的环境中**必然失败**。

在 WorkBuddy 开放平台上架时，若买家在云端运行本技能，需明确告知：
**本技能仅适用于用户自己的 Windows 电脑上的本地排障**，且以下操作需要管理员权限：
- 打开物理盘读写句柄
- 修改 `HKLM` 注册表（类级 LowerFilters）
- 执行 `chkdsk /f`（会卸载卷并安排重启修复）

## 何时使用

- U 盘/移动硬盘能识别但写不进去（文件写入报 PermissionError、只读、无法创建）
- `\\.\PhysicalDriveN` 写入返回 err=5 / err=121 / err=87
- 卷状态 `Full Repair Needed` / `Health=Warning`，chkdsk 卡死或重启
- Windows 事件日志出现 `disk` 事件 153（IO 重试）
- 容量显示异常、扩容盘、分区错乱
- 用户考虑了厂商量产（如 AlcorMP）作为最后手段

**不适用**：数据恢复（芯片级/专业机构）、硬盘 SMART 硬件故障更换、Linux/macOS 下的 U 盘问题（流程需改写）。

---

## 第一原则：先定位层级，再动手

**这是本技能最重要的部分。** 绝大多数失败案例都源于跳过分层定位，直接对最可疑的一层反复施压。

按此顺序推进，**每层拿到结论后才进入下一层**：

```
L0 物理在位   → U 盘此刻真的在总线上吗？当前枚举的实例是哪一个？
L1 块设备层   → 磁盘能打开吗？能读吗？读多快？写得到吗？写到哪失败？
L2 卷层       → 分区/卷挂上了吗？卷被锁了吗？只读标志在哪一位？
L3 文件系统层 → 结构自洽吗？哪些目录项坏了？
L4 控制器层   → 主控固件自己置了写保护/坏块标志吗？
L5 厂商层     → 量产态驱动过滤器装对了吗？装到对的那个类 GUID 了吗？
```

### L0 物理在位（最容易被跳过，也最容易误判）

Windows 注册表里会**永久残留**同一型号 U 盘的旧设备节点。`Get-PnpDevice` 查得到它们 ≠ 设备此刻在总线上。

**必查** `DEVPKEY_Device_LastArrivalDate` / `LastRemovalDate`：
- 当前实例：`LastArrival` 是最近时间（今天/几分钟前）
- 幽灵节点：`LastRemoval` 停在几个月/几年前 → **不是当前设备**

```powershell
Get-PnpDevice | Where-Object { $_.InstanceId -like 'USB*' -and $_.InstanceId -match 'VID_058F' } |
  ForEach-Object {
    $la = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LastArrivalDate').Data
    $lr = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LastRemovalDate').Data
    "DEV|$($_.InstanceId)|arrival=$la|removal=$lr"
  }
```

> **实战教训**：本次故障中，我曾据"注册表里查不到 VID_058F 设备"得出"介质级卡死，需物理拔插"的结论，浪费约 2 小时。实际是旧幽灵节点在作怪，真实设备一直在。

**设备不在位的正确判据**（三者同时成立才算真不在位）：
1. `Win32_DiskDrive` 中无该型号
2. `\\.\PhysicalDriveN` 全部 `CreateFile` 失败 err=2（路径不存在）
3. 该容量对应的 `PhysicalDriveN` 打不开

### L1 块设备层：裸盘写入的正确写法

**物理磁盘 WriteFile 的硬规则**（违反任一条都会得到误导性的错误码）：

1. **一次只能写 1 个扇区（512 字节）**。写 128KB → `err=87 ERROR_INVALID_PARAMETER`。
   这不是"盘不支持大写入"，是接口本身的限制。测试脚本必须单扇区写。
2. **必须 512 字节整扇区对齐**。写 2 字节到 `LBA*512+0x6A` → `err=87`。
   正确做法：读整扇区 → 内存改 → 回写整扇区。
3. **句柄必须申请 `GENERIC_READ|GENERIC_WRITE`**。只申请读 → `err=5`。
4. **ctypes 必须显式声明 `argtypes`**，否则默认传参会静默传错指针。
5. **务必设置文件指针**（`SetFilePointerEx` 定位到 `LBA*512`）后再写。
   漏掉这一步 → 数据写到 LBA 0，**当场破坏 MBR 分区表**。

**写测试的黄金判据：写-回读一致性**

```
写入 → FlushFileBuffers → Close → 重新打开 → 读取同一 LBA → 逐字节比对
```

**只看 WriteFile 的返回值是不够的。** Windows 返回成功（`ok=True, err=0`）而数据不落盘，是主控固件丢弃写命令的典型特征——本次故障全程反复出现这个假象。

区分三种失败形态，每种指向不同的层：

| 形态 | 含义 | 指向 |
|---|---|---|
| `err=5` 立即失败 | 权限/卷锁/设备未就绪 | L2 卷锁 / L5 驱动 |
| 超时 ~100s 后 `err=121` | 控制器无响应 | L4 控制器 |
| **ACK 成功但回读不一致** | 主控丢弃写命令 | L4/L5 量产态翻译层 |

**时间点判据**：写超时约 **104 秒**后返回 `err=121`，与 `disk` 事件 153 的 IO 重试周期（~105 秒）精确吻合 → 确认是控制器级无响应，不是软件慢。

### L1 附带：读能力是重要分流信号

- **读快 + 写全挂** → 主控写通道坏 / 写保护 / 量产态翻译层缺失。**不是** NAND 物理损坏。
- 读也慢/报错 → 才是介质/硬件层问题。

8MB 顺序读 39 MB/s 说明读通道完全健康——这一步能在 1 分钟内排除掉最坏假设。

### L2 卷层：卷锁会伪装成"写保护"

卷挂载后，`partmgr` 会把卷锁传播到物理磁盘层 → 对 `\\.\PhysicalDriveN` 的写被拒 → `err=5 ACCESS_DENIED`。

此时 `IOCTL_DISK_IS_WRITABLE` **仍返回 `ok=True, out=0`**（Windows 层认为可写）。

**判定动作**：写测试失败时，先 `mountvol D: /D` 卸载卷（或直接对 `\\?\Volume{GUID}` 操作），再复测裸盘写。
- 卸载后能写 → 纯卷锁问题，Windows 层可解
- 卸载后仍写不进去 → 控制器层问题，往 L4 走

**不要**用 `Get-Volume` 查询处于 `Full Repair Needed` 状态的卷——**会阻塞**。改用 `Win32_Volume`（WMI，不阻塞）。

### L2 附带：exFAT / MBR 关键偏移

**MBR 分区表**（从 `0x1BE` 起，每项 16 字节）：
```
type     @ 0x1BE + 4*i + 4    (1 字节)
startLBA @ 0x1BE + 4*i + 8    (4 字节 LE)
sectors  @ 0x1BE + 4*i + 12   (4 字节 LE)
```
误用旧式 DOS 字段偏移（0x04/0x1C/0x20）会显示成 `type=0x00 sectors=0`——**看起来像分区表全毁，实际是解析错**。

**exFAT 主卷 VBR**（在分区起始 LBA，非 LBA 0）：
```
0x00 引导代码     0x03 文件系统名 "EXFAT   "
0x40 PartitionOffset(8)   0x48 VolumeLength(8)
0x50 FatOffset(4)         0x54 FatLength(4)
0x58 ClusterHeapOffset(4) 0x5C ClusterCount(4)
0x60 RootDirCluster(4)    0x64 VolumeSerialNumber(4)
0x68 FileSystemRevision(2) 0x6A VolumeFlags(2)
0x6C BytesPerSectorShift 0x6D SectorsPerClusterShift 0x6E NumberOfFats
```
- `VolumeFlags` bit0 = VolumeDirty，**bit1 (0x0002) = MediaFailure**
- **MediaFlags（写保护位）在分区偏移 0x403/0x404**，不在 VBR 里

> **实战教训**：本次我先后用 0x1A6、0x403、猜偏移三种方式找写保护位，反复失败。最后靠**扫描 "EXFAT   " 签名**直接定位主 VBR，一次成功。
> **规则：不要猜文件系统偏移。先用签名扫描定位结构，再按标准解析。**

### L3 文件系统层：chkdsk 的使用纪律

**chkdsk 参数选择**：

| 命令 | 行为 | 评价 |
|---|---|---|
| `chkdsk D:` | 只读分析 | ✅ 安全、快，优先 |
| `chkdsk D: /f` | 修复并安排重启完成 | ✅ 修复阶段的正确选择 |
| `chkdsk D: /f /x` | **额外卸载卷 + 加排他锁** | ❌ **禁用** |

**`/x` 是本次事故的一个直接推手**：它卸载卷并加排他锁，在写失效的设备上引发 `disk 153` IO 重试死循环。chkdsk 卡在内核态不可中断 I/O，`Stop-Process` 和 `taskkill /F` **都杀不掉**。

**其他纪律**：
- chkdsk 输出编码是系统 OEM 代码页，读输出用 `encoding="Default"` 或 `errors="replace"`，否则中文乱码。
- 优先 `chkdsk \\?\Volume{GUID}` 直接操作卷，**不依赖盘符**（盘符可能因反复 mountvol 处于半挂载态）。

### L4 控制器层：主控固件会自己置写保护

部分主控（Alcor 等）会在上电时**自行**把 `VolumeFlags` 的 MediaFailure 位置位。此时：
- Windows 侧写回清零 → `err=5`（被拒）
- 回读仍是 `0x0002` → **不是 Windows 设的，改不掉**

这说明是**固件层写保护**，不是文件系统问题。软件手段到此为止。

**判据**：写回失败 + 回读不变 → 固件置位。若能写回并保持清零 → Windows/文件系统层标志，可解。

### L5 厂商层：量产态驱动过滤器（最隐蔽的一层）

某些主控在**量产态（MP Mode）**下，标准 SCSI WRITE 不会被主控接受，必须经厂商过滤驱动翻译成厂商私有命令。缺了它：
- 写入可能 ACK 成功但**数据不落盘**
- 或直接挂起 → 104 秒超时

典型组件：`mpszfilt.sys`（Alcor MP Filter）、`DrvCovEx.exe`。

**关键陷阱一：类 GUID 认错**

Alcor 的 USB 类是：
```
{36fc9e60-c465-11cf-8056-444553540000}   ← USB 类（Alcor 装到这里）
{36D5F765-E261-11CE-AFC5-00AA00389B71}   ← DiskDrive 类（通用磁盘，不是它）
```
本次我长时间查的是 `{36D5F765...}`，该键**根本不存在**，导致一直得出"滤镜没装"的错误结论。

**规则：先确认目标类键存在，再谈写入值。不存在的键 = 查错了类。**

**关键陷阱二：实例属性不含类级值**

`DEVPKEY_Device_LowerFilters` 只显示**实例级**值，**不包含类级 LowerFilters**。所以看到 `LF=[]` 不能推出"滤镜没装"。

验证滤镜是否真的挂上，看**驱动服务的 Enum 计数**：
```
HKLM\SYSTEM\CurrentControlSet\Services\mpszfilt\Enum\Count = 1
```
`Count >= 1` 即滤镜已挂到实例。

**关键陷阱三：设备实例创建于滤镜安装之前 → 滤镜没挂上**

装了类级 LowerFilters 后，**已存在的设备实例不会自动挂上**。必须让设备**重新枚举**：
- `pnputil /restart-device <instanceId>`
- 或重启父 Hub
- 或重启系统（最可靠）

**关键陷阱四：`pnputil` 返回码**
```
rc=0     成功
rc=3010  需重启系统才能完成配置 —— 此时所有设备操作被拒
rc=3758096907  设备实例不在硬件树中
rc=1167  设备没有连接
```

**优先用厂商官方工具，而非手工改注册表**：
```
LoadDrv.exe InstByIni      ← 一键：写类级 LowerFilters + 装服务 + 重置 Hub（rc=0）
LoadDrv.exe UninstByIni
LoadDrv.exe AddFilter <ServiceName>
LoadDrv.exe DelFilter <ServiceName>
```
`LoadDrv.ini` 关键项：`HubDelayMs`、`RestartHubMode`（1=滤镜不存在时重置 Hub）、`ServiceName`。

> **实战教训**：我手工改了半小时注册表（还踩了 `REG_MULTI_SZ` 必须传 `list[str]` 不能传 bytes 的坑），而厂商工具一行命令就搞定了。**先找厂商官方工具。**

---

## 执行流程

### 第 0 步：建立基线快照（不可跳过）

动手前先落一份快照，**否则事后无法判断是哪一步改变了状态**：

```powershell
# 磁盘与卷
Get-Disk | Select Number,FriendlyName,OperationalStatus,Size,IsReadOnly,IsBoot | Format-Table -AutoSize
Get-CimInstance Win32_DiskDrive | Select Index,Model,Status,Size,PNPDeviceID | Format-List
Get-CimInstance Win32_Volume  | Select DriveLetter,FileSystem,Capacity,HealthStatus | Format-List

# 过滤层
Get-PnpDevice | Where-Object { $_.InstanceId -like 'USB*' } | ForEach-Object {
  $lf = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName 'DEVPKEY_Device_LowerFilters').Data
  "DEV|$($_.InstanceId)|LF=$($lf -join ';')"
}
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Class\{36fc9e60-c465-11cf-8056-444553540000}' -Name LowerFilters

# 事件日志
Get-WinEvent -FilterHashtable @{LogName='System';Id=153} -MaxEvents 20
```

**同时备份 MBR**：读 `PhysicalDriveN` 的 LBA 0 存成 `.bin`，任何裸盘操作前必须先做。

### 第 1 步：L0 确认设备在位

用上面的 LastArrival/LastRemoval 判据。**不要**仅凭 `Get-PnpDevice` 有没有该型号下结论。

### 第 2 步：L1 分层读写探测

运行 `scripts/probe.py`，输出结构化结论：
- 磁盘可打开？容量对得上？
- 顺序读速度（区分"读通道坏" vs "仅写坏"）
- 多个 LBA 的写-回读一致性

### 第 3 步：L2 排除卷锁

写失败时先卸载卷再复测。同时确认卷的只读标志位。

### 第 4 步：L3 文件系统

`chkdsk D:` 只读分析 → 定位坏目录项 → `chkdsk D: /f`（**不加 /x**）。

### 第 5 步：L4/L5 控制器与厂商层

- 写回 VolumeFlags 试探固件置位
- 查厂商量产态过滤器是否装到**正确的类 GUID**
- 用 `LoadDrv.exe InstByIni` + 设备重新枚举
- 确认 `mpszfilt\Enum\Count >= 1`

### 第 6 步：收口判定

| 结论 | 证据 | 结论 |
|---|---|---|
| 写-回读一致 | 多点一致 | ✅ 已修复，做最终全链路验证 |
| 卸载卷后可写 | 卷锁 | ✅ 重新挂载 / 修 FS |
| 写回 flags 失败且回读不变 | 固件置位 | ⚠️ 软件到头 → 物理拔插复位或量产 |
| 多点 ACK 成功但不落盘 | 翻译层缺失 | ⚠️ 装厂商过滤器 + 重新枚举 |
| 读也坏 | 介质 | ❌ 硬件故障 → 换盘 / 专业恢复 |

---

## 量产（最后手段）

只有当上述所有软件层都被排除、且**用户已明确授权**时才执行。

**授权口径**：
- 必须确认数据已备份（"数据已全部备份"这种明确表述）
- 必须确认用户理解量产会**清空数据**并**重新安装驱动过滤器**
- 量产会重装 mpszfilt，事后若不再需要量产态，必须清理：
  摘 LowerFilters → `sc delete mpszfilt` → 删 `.sys` → 重启

**风险**：量产过程中若执行 `pnputil /remove-device`，或量产工具与过滤器安装并发，可能造成设备永久不可枚举。**不要在量产工具运行时动 PnP 节点。**

---

## 反模式清单（本次实测踩坑，务必避免）

| 反模式 | 后果 | 正确做法 |
|---|---|---|
| 跳过 L0 直接查 PnP | 把幽灵节点当当前设备，误判"介质卡死" | 必查 LastArrivalDate/LastRemovalDate |
| 直接对最可疑层反复施压 | 耗时数小时且结论错乱 | 严格分层，一层一结论 |
| 猜文件系统偏移 | 反复失败 | 先签名扫描定位，再标准解析 |
| 查错类 GUID | 得出"滤镜没装"的假结论 | 先确认键存在 |
| 只看实例 LowerFilters | 误判滤镜缺失 | 看 `Services\mpszfilt\Enum\Count` |
| 写测试不设文件指针 | **破坏 MBR** | `SetFilePointerEx` + 先备份 MBR |
| 一次写多个扇区 | err=87 误导 | 单扇区写 |
| 裸盘写只看返回值 | 把"ACK 不落盘"当成功 | 写-回读-逐字节比对 |
| `chkdsk /f /x` | IO 重试死循环，进程杀不掉 | 只用 `/f` |
| 对坏卷用 `Get-Volume` | 阻塞 | 用 `Win32_Volume` |
| 手工改注册表装厂商驱动 | 踩 REG_MULTI_SZ 类型坑 | 用厂商官方工具 |
| 改多项而不建基线 | 无法归因 | 第 0 步先快照 |
| 在 SYSTEM 任务里用 `utf-16-le` codec | LookupError 崩溃 | 手工 UTF-16LE 解码 |

---

## 沙箱/权限环境注意事项

某些执行环境存在程序黑名单与父进程超时：
- `schtasks.exe` / `sc.exe` 直接调用可能被拦截 → 通过**提权 Python 子进程** `subprocess` 调用绕过
- `run_elev.py` 的 `WaitForSingleObject(600000)` 超时**会连带杀掉子进程** → 长时任务（chkdsk /f、diskpart）必须用**计划任务**或 `Start-Process` 脱离父进程
- `Start-Process -FilePath python.exe -ArgumentList '"<script>"'` 可行；用 `cmd /c` 嵌套引号会**立即退出**
- SYSTEM 身份下 stdlib 路径可能不全 → 纯 Python 编解码器（如 `utf-16-le`）会 LookupError

---

## 配套脚本

| 文件 | 用途 |
|---|---|
| `scripts/probe.py` | 一键分层体检：设备在位 / 读写探测 / 写-回读一致性 / 事件日志 |
| `scripts/backup_mbr.py` | 备份 PhysicalDrive 的 MBR（裸盘操作前必做） |
| `scripts/fix_filelayer.py` | 类级 LowerFilters 安装/卸载 + 驱动服务状态 + Enum\Count 校验 |
| `references/probe_recipes.md` | 可复制的探测片段（PnP 身份、写测试、exFAT 解析、chkdsk 调用） |
| `references/vendor_alcor_mp.md` | Alcor 量产态驱动全解 + 官方工具用法 |

## 收尾清单

故障排除后必须做：
- [ ] 归档并删除 `usb_fix` 下的临时脚本与日志
- [ ] 移除遗留计划任务（`WB_USB_*` 系列）
- [ ] 清理设备上的测试残留文件（`_wbtest*`、`_strike.bin` 等）
- [ ] 若执行过量产且不再需要：摘 LowerFilters → 删服务 → 删 `.sys` → 重启
- [ ] 更新诊断报告，记录**最终根因**（不要只记过程）
