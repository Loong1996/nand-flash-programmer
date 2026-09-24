# 03 FPGA 方案设计

## 1. 为什么用 FPGA

优势：
1. 时序完全可控：ONFI 异步最高档（20ns 周期，8 位理论约 50MB/s），将来可扩展 NV-DDR / Toggle DDR；
2. **I/O bank 电压独立**：多数 FPGA bank 支持 1.2–3.3V，把连 NAND 的 bank 的 VCCIO 接可调 LDO 即可**免电平转换**直接支持 1.8V / 1.2V 芯片；
3. 16 位总线、多片并行、硬件 BCH 都可以做；
4. 引脚分配自由。

代价：HDL 开发、时序约束、仿真；开发量约为 MCU 的 3–5 倍；需要高速 USB 桥（FT232H 等）才有意义。

## 2. macOS 工具链约束

- Vivado（Xilinx）、Quartus（Intel）、高云官方 IDE **基本不支持 macOS**。
- 开源工具链 **Yosys + nextpnr + openFPGALoader**（打包为 oss-cad-suite）在 macOS 原生可用，支持：
  - Lattice ECP5（Project Trellis，最成熟）
  - 高云 GW1N 系列（Apicula）
  - Lattice iCE40（IceStorm）
- LiteX（`pip` 安装）可生成 VexRiscv 软核 SoC。

## 3. 开发板选型

| 板子 | 芯片 / 容量 | 大约价格 | 评价 |
|---|---|---|---|
| ⭐ **Sipeed Tang Nano 9K** | GW1NR-9，约 8.6K LUT | ¥60–80 | 便宜；Apicula 对 GW1N-9 支持成熟；LiteX 有板级支持；板载 USB 下载器 openFPGALoader 可用。容量/I/O 偏紧但够用 |
| ⭐ ECP5 板（ICESugar-Pro / Colorlight i5 + 扩展板 / ULX3S） | LFE5U-25F，约 24K LUT | ¥150–300+ | 开源工具链最成熟，LiteX 首选；但 ECP5 全为 BGA，正式板难手焊 |
| iCEBreaker（iCE40 UP5K） | 约 5.3K LUT | 约 ¥300 | 放软核后余量小，不推荐 |
| Tang Primer 20K | GW2A-18，约 20K LUT | ¥150–200 | GW2A 开源支持不如 GW1N 成熟，可能需虚拟机跑官方 IDE |

**选定 Tang Nano 9K**：正式板可用同家族 **GW1N-9 LQFP144**（可手焊），bank VCCIO 可单独供电，代码可平移。容量不够再换 ECP5（LiteX 代码基本可平移）。

资源估算：VexRiscv + NAND 控制器 + SPI 控制器 + FIFO 桥 ≈ 5–10K LUT；I/O：NAND 15 + SPI 4–6 + FT232H FIFO 约 15 + 电源控制 ≈ 40。

## 4. I/O 电压限制（Tang Nano 9K）

开发板上各 bank 的 VCCIO 已由板子电源连接固定，**不可软件修改**。
依据 LiteX `sipeed_tang_nano_9k.py`：

| Bank | 板上电压 | 用途 |
|---|---|---|
| Bank 3（左侧，引脚 3、4、10、11、13–16 等） | **1.8V** | 6 个 LED、2 个按键（LVCMOS18） |
| 其他 bank | **3.3V** | 串口、配置 Flash、SD 卡、LCD、HDMI、排针 GPIO（LVCMOS33） |

- 排针 J6/J7 引出的均为 3.3V bank 引脚（见 [04 文档](04-hardware-bringup.md)）。
- ECP5 开发板同样多为固定 3.3V。

应对：
1. 原型阶段只用 3.3V 芯片（如 W29N02KVSIAF）；
2. 需测 1.8V 芯片时，做电平转换小板（SN74AVC8T245 数据、SN74AVC4T245 / 74LVC1T45 控制、R/B# 芯片侧上拉后转回、芯片侧 LDO 1.8/3.3V 可切换）；
3. 正式板：GW1N-9 LQFP144，**专用一个 bank 只接 NAND/SPI**，其 VCCIO 接可切换 LDO（1.8/3.3V，可选 1.2V），FT232H、配置 Flash、时钟放 3.3V bank。画板前查 GW1N 数据手册：哪些 bank 可独立供电（与配置/JTAG 引脚的关系），以及切换 VCCIO 时 I/O 须先置高阻。

## 5. 系统架构

```
Mac ── USB2 高速 ── FT232H（同步 FIFO，60MHz）──┐
                                              │
┌──────────────────── FPGA ───────────────────┼──────────┐
│  VexRiscv 软核（LiteX）                      │          │
│   └ 运行移植后的 NANDO 固件                   │          │
│      nand_programmer.c / nand_bad_block.c / spi_flash.c│
│  ├─ FT232H FIFO 桥  ←────────────────────────┘          │
│  ├─ 仿 FSMC NAND 控制器 ──(可调 VCCIO)── TSOP48         │
│  ├─ SPI/QSPI 控制器 ─────(可调 VCCIO)── SOP8/WSON8      │
│  ├─ 数据直通通道（DMA）：NAND ⇄ FIFO，CPU 不逐字节搬运  │
│  └─ 电源控制 GPIO（目标芯片供电开关、VCCIO 选择）        │
└────────────────────────────────────────────────────────┘
```

另一种做法是 FPGA 只做纯硬件状态机、逻辑全放 PC 端 —— 更快更简单，但 NANDO 代码基本不能复用。**本项目采用软核方案。**

## 6. 仿 FSMC NAND 控制器（寄存器草案）

目标：让 NANDO 的 `fsmc_nand.c` 只改初始化和基地址即可运行。

### 6.1 内存映射数据窗口

基地址 `NAND_BASE`（例如 LiteX 总线上的 `0x8000_0000`，窗口 256KB）：

| 访问地址 | 产生的总线周期 |
|---|---|
| `NAND_BASE \| (1<<16)` 写 | 命令周期：CLE=1，WE# 脉冲 |
| `NAND_BASE \| (1<<17)` 写 | 地址周期：ALE=1，WE# 脉冲 |
| `NAND_BASE` 写 | 数据写周期：WE# 脉冲 |
| `NAND_BASE` 读 | 数据读周期：RE# 脉冲，采样 IO |
| 32 位读 | 连续 4 个数据读周期（兼容 NANDO 读 ID 时的 32 位访问） |

总线访问在 NAND 周期完成前挂起（Wishbone ack 延迟），保证软件语义与 FSMC 一致。

### 6.2 控制/状态寄存器（CSR）

| 寄存器 | 位 | 说明 |
|---|---|---|
| `TIMING0` | `setup[7:0]` `wp[15:8]` `hold[23:16]` `hiz[31:24]` | 以系统时钟周期为单位，对应 FSMC SetupTime/WaitSetupTime/HoldSetupTime/HiZSetupTime |
| `TIMING1` | `tclr[7:0]` `tar[15:8]` `trea_sample[23:16]` | CLE→RE#、ALE→RE# 延迟；读数据采样点 |
| `CTRL` | `ce_force[0]` `wp_n[1]` `bus16[2]` `enable[3]` | CE# 手动控制、WP# 输出、16 位总线（预留）、使能 |
| `STATUS` | `rb[0]` `dma_busy[1]` `dma_err[2]` | R/B# 同步后状态（1=就绪） |
| `DMA_LEN` | `[15:0]` | 本次直通传输字节数（页 + spare） |
| `DMA_CTRL` | `start[0]` `dir[1]` | dir=0：NAND→FIFO（读页）；dir=1：FIFO→NAND（写页） |
| `ECC_CTRL` / `ECC_VAL` | — | 预留：兼容 FSMC 汉明 ECC，或先不实现（由 PC 计算） |

### 6.3 数据直通通道

- `read_page`：CPU 发 `00` + 地址 + `30`，等 R/B#，然后启动 `DMA_CTRL(start, dir=0)`，硬件连续产生 RE# 周期并把数据直接推入 FT232H 发送 FIFO。
- `write_page_async`：CPU 发 `80` + 地址，启动 `DMA_CTRL(start, dir=1)`，硬件从 FT232H 接收 FIFO 取数据产生 WE# 周期，完成后 CPU 发 `10`。
- 这两个函数是 NANDO 驱动中**唯一需要重写**的部分；否则软核逐字节搬运会比 STM32 还慢。

## 7. SPI 控制器

- 可用 LiteX 的 LiteSPI 或自写简单 SPI master（支持 Mode 0/3、可调分频、CS 手动控制；后续加 Dual/Quad）。
- 移植 `spi_flash.c`：把 SPL 的 SPI 调用替换为 CSR 读写；大块读写同样走直通通道。

## 8. FT232H 同步 FIFO 接口

- EEPROM 需配置为 “245 FIFO” 模式；PC 端用 libftdi 设置同步 FIFO bitmode（`BITMODE_SYNCFF`）。
- 信号（以 FT232H 数据手册为准）：

| FT232H 引脚 | 信号 | 方向（相对 FPGA） |
|---|---|---|
| ADBUS0–7 | D0–D7 | 双向 |
| ACBUS0 | RXF#（有数据可读） | 输入 |
| ACBUS1 | TXE#（可写） | 输入 |
| ACBUS2 | RD# | 输出 |
| ACBUS3 | WR# | 输出 |
| ACBUS4 | SIWU#（立即发送） | 输出 |
| ACBUS5 | CLKOUT 60MHz | 输入（应接 FPGA 全局时钟输入脚） |
| ACBUS6 | OE# | 输出 |

- FPGA 内是 60MHz 时钟域，需与系统时钟域之间做异步 FIFO。
- macOS 自带 FTDI 驱动可能抢占设备导致 libftdi 打不开：可改 EEPROM 中 VID/PID 或阻止系统驱动绑定。
- 原型阶段 60MHz 信号用杜邦线容易出错：线要短；正式板同板布线。
- 过渡方案：先用 Tang Nano 9K 板载 USB 串口通信（慢但足以验证读 ID / 读页）。

## 9. 上位机

- 第一版：Python CLI（macOS 原生），子命令 `id / read / write / erase / verify`，参数 `--raw`、`--oob`、`--skip-bad`。
- 通信层可插拔：串口（开发期）/ libftdi 同步 FIFO（高速）。
- 保留 NANDO `cmd.h` 命令格式与 CSV 芯片数据库；芯片参数优先 ONFI 参数页自动识别。
- 后续可选：让 NANDO Qt 上位机在 macOS 编译，并替换其 `serial_port` 层。

## 10. NANDO 移植清单

| 文件 | 处理 |
|---|---|
| `nand_programmer.c` | 保留主体；替换 USB CDC 收发为 FIFO 桥接口 |
| `nand_bad_block.c` | 保留 |
| `fsmc_nand.c` | 保留命令/地址序列；重写 `nand_gpio_init`/`nand_fsmc_init`（写 TIMING/CTRL CSR）、基地址、`read_page`/`write_page_async`（走 DMA）；`enable_hw_ecc` 暂返回不支持或实现 ECC 模块 |
| `spi_flash.c` | 替换 SPL SPI 调用为 SPI 核 CSR |
| `flash_hal.h`、`chip.h`、`chip_info.h` | 保留 |
| `cdc.c`、`usb.c`、`uart.c`、`clock.c`、`led.c`、`jtag.c`、`libs/spl`、`usb_cdc/`、`bootloader/`、启动文件与链接脚本 | 删除 / 用 LiteX BIOS 与自写驱动替代 |
| `qt/serial_port.cpp` | 增加 libftdi 实现 |

## 11. 原型硬件（阶段性）

- Tang Nano 9K + TSOP48 转 DIP48 翻盖座（杜邦线直连）+ 逻辑分析仪 → 读 ID；
- 加 FT232H 模块 → 高速读页；
- 加 SOP8 座 → SPI；
- 验证后画底板：Tang Nano 9K 插座 + TSOP48 翻盖座 + 上拉 + 去耦 + FT232H 插座 + SOP8 座；
- 最终正式板：GW1N-9 LQFP144 + FT232H + 可调 VCCIO。
