# Alcor 量产态（MP Mode）驱动全解

适用于 Alcor 主控的 U 盘，特别是被 AlcorMP 量产过、或主控处于量产态的设备。

---

## 什么是量产态

Alcor 的 U 盘主控在**量产模式（MP Mode / Product Mode）**下，标准 SCSI 写命令
**不被主控直接接受**。必须由内核过滤驱动 `mpszfilt.sys`（Alcor MP Filter）拦截，
翻译成 Alcor 私有命令后主控才执行。

`C:\MP_Test\AlcorMP.ini` 中的关键段：

```ini
[MAIN]
ProductMode=1        ; 量产态
WriteProtect=0
[Scan]
MPMode=1
[UFD]
UFDMode=1
WriteProtect=0
[BadBlock]
LoadDriver=1         ; 量产时加载驱动
SWWriteProtect=0
MBR=1
[ALREADY LOAD]
VIDPID=058F6387,     ; 已量产过的设备
```

**缺了 mpszfilt 的表现**：
- 写入 ACK 返回成功，但**数据不落盘**（主控静默丢弃命令）
- 或直接挂起 → 104 秒后 `err=121 ERROR_NO_MEDIA`
- 读完全正常（读命令不需要翻译）

**这个特征极具迷惑性**：软件层面看"写成功了"，实际数据没写进去。

---

## 类 GUID —— 最容易踩的坑

Alcor 把过滤器装到 **USB 类**：

```
{36fc9e60-c465-11cf-8056-444553540000}   ← USB 类 ✔ Alcor 装这里
{36D5F765-E261-11CE-AFC5-00AA00389B71}   ← DiskDrive 类 ✘ 不装这里
```

**实战教训**：本次排障中长时间查的是 `{36D5F765...}`，该键**根本不存在**，
于是反复得出"滤镜没装"的结论，而实际上厂商工具早已把它装到了 `{36fc9e60...}`。

**规则**：
1. 改注册表前先确认目标键**存在**。键不存在 = 查错了类，不是"没装"。
2. 枚举所有类 GUID 看哪个存在：
   ```powershell
   Get-ChildItem 'HKLM:\SYSTEM\CurrentControlSet\Control\Class' | ForEach-Object {
     $lf = (Get-ItemProperty $_.PSPath -Name LowerFilters -ErrorAction SilentlyContinue).LowerFilters
     if ($lf) { "$($_.PSChildName)|$($lf -join ';')" }
   }
   ```

---

## 三个判断陷阱

### 陷阱一：实例属性不含类级值

`DEVPKEY_Device_LowerFilters` 只显示**实例级**过滤层，**不含类级 LowerFilters**。

看到 `LF=[]` **不能**推出"滤镜没装"。

**正确的判断方法** —— 看驱动服务的 Enum 计数：
```
HKLM\SYSTEM\CurrentControlSet\Services\mpszfilt\Enum\Count
```
- `Count >= 1` → 滤镜已挂到至少一个设备实例
- `Enum` 键存在但无 `Count` 值 → 驱动已安装，但**当前没有设备绑定**
  （设备实例创建于滤镜安装之前 → 必须重新枚举设备）

### 陷阱二：装完滤镜要重新枚举设备

类级 LowerFilters 只对**之后创建**的设备实例生效。已存在的实例不会自动挂上。

必须：
```
pnputil /restart-device <实例ID>
```
或重启父 Hub，或重启系统（最可靠）。

**pnputil 返回码**：
```
rc=0                成功
rc=3010             需重启系统才能完成配置 —— 此时所有设备操作被拒
rc=3758096907       设备实例不在硬件树中
rc=1167             设备没有连接
```

### 陷阱三：驱动服务的属性

```
Services\mpszfilt\
  Start = 1     (1=System 启动，2=Auto，3=Manual，4=Disabled)
  Type  = 1     (1=内核驱动)
  Group= System Bus
  Tag  = 1
  Enum\Count    绑定实例数
```
驱动文件：`C:\Windows\System32\drivers\mpszfilt.sys`（x64 约 20KB）。

**`sc stop` 对 NOT_STOPPABLE 驱动返回 1052；`sc delete` 对运行中的内核驱动返回 1072**（标记删除，需重启才真正移除）。

---

## 官方工具

目录：`ALCOR MP_v16.02.19.00\DRIVERS\`

| 工具 | 用途 |
|---|---|
| `LoadDrv.exe` | 安装/卸载过滤器、重置 Hub（**首选**） |
| `LoadDriver.exe` | 驱动加载 |
| `DrvCovEx.exe` | 设备覆盖工具 |
| `KillBaboon.exe` | 进程清理 |
| `AmWinK.exe` | 内核辅助 |
| `VerifyCat.exe` | 签名校验 |
| `x32/` `x64/` | 32/64 位驱动文件 |

### LoadDrv.exe 子命令

```
LoadDrv.exe InstByIni       一键安装：写类级 LowerFilters + 装服务 + 重置 Hub
LoadDrv.exe UninstByIni     卸载
LoadDrv.exe AddFilter <ServiceName>
LoadDrv.exe DelFilter <ServiceName>
LoadDrv.exe SetIgnoreHwID <VidPid>
LoadDrv.exe ClearIgnoreHwID <VidPid>
```

> 注意：`RetartHub`（厂商自己的拼写，非 RestartHub）**不是有效子命令**，会打印帮助并返回 rc=2。
> Hub 重置是 `InstByIni` 的附带行为。

### LoadDrv.ini

```ini
[Setting]
HubDelayMs = 2000
RestartHubMode = 1   ; 0=不重置 Hub, 1=滤镜不存在时重置 Hub, 2=总是重置
ServiceName = mpszfilt
LoadDriverForWin7 = 1
```

---

## 优先用官方工具

**实战教训**：手工改注册表折腾了半小时，还踩了 `REG_MULTI_SZ` 的类型坑，
而厂商工具 `LoadDrv.exe InstByIni` 一行就搞定了（rc=0）。

`REG_MULTI_SZ` 写入规则：
```python
# ✔ 传 list[str]
winreg.SetValueEx(k, "LowerFilters", 0, winreg.REG_MULTI_SZ, ["mpszfilt"])
# ✘ 传 bytes → ValueError: Could not convert the data to the specified type
```

**先找厂商官方工具，再考虑手工改注册表。**

---

## 量产（MP）操作纪律

量产会：
1. **清空数据**
2. **重新安装驱动过滤器**（重写类级 LowerFilters + 重装 mpszfilt）

**风险**：
- 量产工具运行**期间**执行 `pnputil /remove-device` → 设备可能永久不可枚举
- 量产与过滤器安装并发 → 状态错乱

**事后清理**（若不再需要量产态）：
```
1. 摘除类级 LowerFilters:  fix_filelayer.py --uninstall mpszfilt
2. 删除服务:              sc delete mpszfilt
3. 删除驱动文件:          del C:\Windows\System32\drivers\mpszfilt.sys
4. 重启系统
```

---

## 排障决策速查

| 现象 | 结论 | 动作 |
|---|---|---|
| 读正常，写 ACK 成功但不落盘 | 缺翻译层 | `LoadDrv.exe InstByIni` → 重新枚举 → 复测 |
| 写 104 秒超时 err=121 | 控制器无响应 | 查事件 153；软件层基本到头 |
| `Enum\Count = 0` 但服务在跑 | 没绑到设备 | 重新枚举设备 |
| 类键 `{36D5F765...}` 不存在 | 查错了类 | 查 `{36fc9e60...}` |
| 实例 `LF=[]` | **不能**说明滤镜没装 | 看 `Enum\Count` |
| 写回 VolumeFlags 失败且回读不变 | 固件置写保护 | 软件到头 → 物理复位或量产 |
