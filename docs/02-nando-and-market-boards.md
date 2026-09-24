# 02 NANDO 开源项目与市售“4.1”编程器分析

## 1. NANDO（bbogush/nand_programmer）

- 仓库：<https://github.com/bbogush/nand_programmer>
- 基于 STM32F103（FSMC 驱动 NAND）的开源并口 NAND + SPI Flash 编程器。
- USB CDC 虚拟串口；Qt 上位机（Linux / Windows）；KiCad 硬件。
- **最新版本 3.5.0**（硬件 ECC、剩余时间估算、串口选择、>4GB 芯片支持）。
- README 声明 “THE PROJECT IS NOT ACTIVELY SUPPORTED”。
- GitHub 上主要 fork（7134956、liqili、patrykbajos/nando、matthiasbock/nandopen）均**未发现 4.x 版本号**。
- 国内衍生：立创开源平台有改用立创元件库、改 Type-C 的版本，版本号沿用 NANDO。

### 版本历史摘要

| 版本 | 主要变化 |
|---|---|
| 3.5.0 | 硬件 ECC、时间估算、串口选择器、进度显示、读写文件、>4GB 芯片、校验 |
| 3.4.x | SPI Flash 支持、新芯片、Windows 编译、固件升级修复、初始化时 NAND 复位 |
| 3.3.0 | 单面贴装 PCB、新芯片 |
| 3.2.0 | 芯片自动识别、固件升级 |
| 3.1.0 | 读取含 spare 区、spare 大小可配置、BOOT0 开关 |
| 3.0.0 | 带转接板的 PCB 设计 |

### 源码结构

```
firmware/
  bootloader/  libs/spl/  usb_cdc/          ← STM32 专用（SPL 库、USB 栈）
  programmer/
    flash_hal.h        ← 统一 Flash 抽象接口 flash_hal_t
    fsmc_nand.c/.h     ← 并口 NAND 驱动（FSMC 内存映射）
    spi_flash.c/.h     ← SPI Flash 驱动
    nand_programmer.c  ← 命令处理主逻辑
    nand_bad_block.c   ← 坏块表
    cdc.c usb.c uart.c led.c clock.c jtag.c ...
qt/
  cmd.h                         ← PC↔编程器命令格式
  serial_port.cpp               ← 串口通信层
  programmer.cpp reader.cpp writer.cpp main_window.cpp ...
  nando_parallel_chip_db.csv    ← 并口 NAND 芯片数据库
  nando_spi_chip_db.csv         ← SPI 芯片数据库
kicad/                          ← 主板与转接板
```

### 关键实现细节（移植相关）

`flash_hal.h` 定义的接口：

```c
typedef struct {
    int (*init)(void *conf, uint32_t conf_size);
    void (*uninit)();
    void (*read_id)(chip_id_t *chip_id);
    uint32_t (*erase_block)(uint32_t page);
    uint32_t (*read_page)(uint8_t *buf, uint32_t page, uint32_t page_size);
    uint32_t (*read_spare_data)(uint8_t *buf, uint32_t page, uint32_t offset, uint32_t data_size);
    void (*write_page_async)(uint8_t *buf, uint32_t page, uint32_t page_size);
    uint32_t (*read_status)();
    bool (*is_bb_supported)();
    uint32_t (*enable_hw_ecc)(bool enable);
} flash_hal_t;
```

`fsmc_nand.c` 通过写特定地址产生 NAND 总线周期：

```c
#define CMD_AREA   (1<<16)   /* A16 = CLE */
#define ADDR_AREA  (1<<17)   /* A17 = ALE */
*(__IO uint8_t *)(Bank_NAND_ADDR | CMD_AREA)  = cmd;   // 命令周期
*(__IO uint8_t *)(Bank_NAND_ADDR | ADDR_AREA) = addr;  // 地址周期
data = *(__IO uint8_t *)(Bank_NAND_ADDR);              // 数据周期
```

- 芯片命令字与时序参数由上位机从 CSV 读出，通过 `init(conf)` 下发（`fsmc_conf_t`），固件内不存芯片表。
- 页读写数据由 CPU 逐字节循环搬运。
- 硬件 ECC 使用 STM32 FSMC 的汉明 ECC（`enable_hw_ecc`）。

### 许可证

- NANDO 自身代码 GPLv3；其中 STM32 SPL 与 USB CDC 部分受 ST 许可证限制。
- 移植 NANDO 代码的衍生作品发布时须 GPLv3 开源；FPGA 方案会替换掉 ST 相关部分。

## 2. 市售“NAND 编程器 4.1”板（闲鱼）分析

根据照片判断，**基本可确定是 NANDO 的国内闭源改版**，“4.1”多半是卖家自定的硬件版本号。

### 与 NANDO 吻合的特征

| 照片特征 | 对应 |
|---|---|
| 中间 100 脚 QFP 主控 | NANDO 使用 STM32F103（FSMC），丝印未能确认 |
| TSOP48 翻盖座直连主控，无电平转换芯片 | NANDO 同样 FSMC 直连 |
| BOOT0 跳线 | NANDO 3.1 起的 BOOT0 开关 |
| SPI 转接子板 | NANDO 3.4 起的 SPI 转接方式 |

### 相比上游的改动

1. 1.8V / 3.3V 电压跳线（上游仅 3.3V）
2. SPI 子板集成 SOIC8、SOIC16、WSON8（焊盘）与 2×4 排针接口、电源指示灯
3. 目标芯片电源开关（按键）
4. TX/RX 串口 + DIO/CLK（SWD）排针
5. 丝印“NAND编程器”

### ⚠️ 1.8V 档安全性存疑

板上看不到电平转换芯片。可能的两种实现：
- 整个主控降到 1.8V：STM32F103 最低 2.0V，不可能；
- 只切 NAND 的 VCC，数据线仍是 3.3V：**超过 1.8V NAND 的 I/O 耐压，有烧片风险**。

验证方法：
1. 看清主控丝印（STM32F103VET6 / GD32F103VE / STM32F407VE …）；
2. 跳到 1.8V，量 TSOP48 第 12、37 脚（VCC）；
3. 量数据线 IO0–IO7（29–32、41–44 脚）或 WE#（18 脚）的高电平，若仍为 3.3V 则 1.8V 档不安全。

## 3. 在 macOS 上使用该闭源板的办法

### 第一步：识别（5 分钟）

```bash
system_profiler SPUSBDataType | grep -B2 -A8 -i -E "stm|nand|0483"
ls /dev/cu.*
```

- VID `0x0483` / PID `0x5740` + 出现 `/dev/cu.usbmodemXXXX` → STM32 虚拟串口，很可能是 NANDO 系固件（最终需发 NANDO 格式命令验证）。
- 其他 VID/PID → 卖家自定义协议。

### 方案

| 方案 | 做法 | 备注 |
|---|---|---|
| 1. 虚拟机 | Apple 芯片：Parallels / UTM + Windows 11 ARM（x86 程序自动转译）；Intel：Parallels / VMware / Boot Camp；USB 直通 | 最快能用；Win10/11 自带 CDC 驱动 |
| 2. NANDO 开源上位机 | Qt 上位机理论上可在 macOS 编译（可能需小改）；或用 Python + pyserial 按 NANDO 命令格式写 CLI | 先写“探测脚本”发读 ID 命令验证兼容性 |
| 3. 抓包还原协议 | 虚拟机内用 Wireshark + USBPcap 或串口监视器抓原版上位机收发数据，再用 Python 实现 | 协议不兼容时使用 |
| 4. 刷 NANDO 开源固件 | BOOT0 + 串口：`brew install stm32flash` + USB-TTL；或 ST-Link + openocd / STM32CubeProgrammer | 不可逆，见下 |

刷固件注意：
- FSMC 的 NAND 引脚在 STM32F103 上是固定的，所以并口 NAND 接线大概率与 NANDO 相同；需对照 NANDO 原理图核对 SPI 引脚、LED、电源开关控制脚、USB 上拉、电压切换 GPIO。
- **先备份原厂固件**：闭源固件多开读保护（RDP），解除保护会擦空芯片，刷掉就回不去。最稳妥是先向卖家要固件 bin。
- NANDO 为 GPLv3，理论上卖家有义务提供修改后的源码。

## 4. NANDO 代码在 FPGA 方案中的复用评估

| NANDO 部分 | FPGA 方案能否复用 |
|---|---|
| `qt/*.csv` 芯片数据库 | ✅ 直接可用 |
| `qt/cmd.h` 命令格式 | ✅ 可保留 |
| `qt/` 上位机其他部分 | ⚠️ 大部分可用，需替换 `serial_port` 通信层为 libftdi / D2XX（FT232H 同步 FIFO 不是串口） |
| `nand_programmer.c`、`nand_bad_block.c`、`spi_flash.c`、`fsmc_nand.c` | ⚠️ 在 FPGA 内放软核即可几乎原样移植（需实现“仿 FSMC”外设） |
| `libs/spl`、`usb_cdc/`、`bootloader/`、`clock.c` 等 | ❌ STM32 专用，替换 |
| `kicad/` | ⚠️ TSOP48 / SPI 转接板引脚可参考 |
| HDL | ❌ 无，需从零写 |

详细设计见 [03 FPGA 设计](03-fpga-design.md)。

## 参考链接

- NANDO：<https://github.com/bbogush/nand_programmer>（Releases、Wiki、README）
- Fork：<https://github.com/7134956/nand_programmer>、<https://github.com/liqili/nand_programmer>、<https://github.com/patrykbajos/nando>、<https://github.com/matthiasbock/nandopen>
- NANDO v3.4 主板（OSHWLab）：<https://oshwlab.com/kharchenko.pm/nando-v3-4-main-board>
- 立创开源：<https://oshwhub.com/zj53523094/nand_programmer-bian-cheng-qi>、<https://oshwhub.com/myseil/nandopen>
- SPI NAND 相关：<https://github.com/981213/spi-nand-prog>
- 其他 STM32 NAND 编程器：<https://github.com/maximus64/STM32_NAND_Programmer>、<https://github.com/HamsterReserved/STM32-NAND-Programmer>
