# 03 FPGA 设计（最终方案：纯硬件微操作引擎）

> 早期曾考虑“LiteX 软核 + 移植 NANDO 固件”。后来决定协议全部重写、不背 NANDO 的历史包袱，于是改为**纯硬件执行引擎**：FPGA 只执行微操作，所有智能放在上位机。旧方案的分析保留在 [02 文档](02-nando-and-market-boards.md) 第 4 节，供参考。

## 1. 为什么是“执行引擎”

- FPGA 逻辑小而固定：约 2300 个 LUT（占 26%）、4 块 BSRAM，27 MHz 下时序余量很大（最高约 60 MHz）。
- **支持新芯片只改上位机**：命令序列、地址格式、坏块、ECC、SFDP/ONFI 解析都在 Python 里，不用重新综合、不用重烧 FPGA。
- 可以完整仿真：RTL + Verilog 芯片模型 + 真实上位机驱动的端到端测试（`fpga/sim`）。

## 2. 结构

```
                 ┌──────────────────────────── FPGA (GW1NR-9, 27 MHz) ─────────────────────────────┐
USB ─ BL702 ─ UART ──┐                                                                              │
(板载, 115200→3M)    ├─► 端口选择 ─► RX FIFO 4 KiB ─► engine ─┬─► nand_bus ──► TSOP48 NAND           │
USB ─ FT232H ─ FIFO ─┘   (回复走最近    ◄── TX FIFO 4 KiB ◄──┤                                        │
  (245 异步, 可选)         收到命令的口)                      └─► spi_master ─► SOP8/WSON8/SOIC16    │
                 └──────────────────────────────────────────────────────────────────────────────────┘
```

| 文件 | 作用 |
|---|---|
| `fpga/rtl/top_tangnano9k.v` | 顶层：复位、同步器、FIFO、端口选择、三态引脚、LED |
| `fpga/rtl/engine.v` | 操作码解析与执行；时序寄存器；超时；波特率切换与看门狗 |
| `fpga/rtl/nand_bus.v` | NAND 异步总线周期（命令 / 地址 / 写数据 / 读数据），自动满足 tWHR、tADL |
| `fpga/rtl/spi_master.v` | SPI 模式 0，分频可调，可选延迟采样 |
| `fpga/rtl/uart.v` | 8N1 UART，运行时可改分频 |
| `fpga/rtl/ft245.v` | FT232H 245 异步 FIFO 读写状态机 |
| `fpga/rtl/common.v` | 两级同步器、BRAM FIFO、LED 脉冲展宽 |
| `fpga/constraints/tangnano9k.cst` | 引脚约束 |

协议见 [protocol.md](protocol.md)。

## 3. 关键设计点

- **上电安全**：所有低有效输出都用“高有效寄存器取反”的方式输出，FPGA 上电寄存器全为 0 时，CE#/WE#/RE#/CS# 都是高电平；NAND WP# 默认拉低（写保护）。
- **流控**：FT232H 靠 RXF#/TXE# 自然反压；UART 没有流控，由上位机按窗口发送（每段末尾加 ECHO 标记，未确认字节数不超过接收 FIFO 容量）。
- **自恢复**：操作收了一半、100 ms 内没有新字节到达时，引擎放弃该操作并释放 CE#/CS#；上位机用 ECHO 标记重新同步。
- **波特率协商**：`SET_BAUD` 后 1 秒内收不到确认，引擎自动回到 115200，所以协商失败也不会失联。
- **时序保守**：NAND 默认每个读写周期约 185 ns，远低于芯片极限，适合杜邦线；SPI 默认 6.75 MHz，最高 13.5 MHz。
- **双用途引脚**：FT232H 数据线用到的 53–57 脚是配置接口复用脚，打包时用 `--sspi_as_gpio --mspi_as_gpio --cpu_as_gpio` 把它们释放为普通 IO（与 LiteX 对该板的默认做法相同）。

## 4. 速度估算

| 链路 | 读 | 说明 |
|---|---|---|
| UART 115200 | 约 11 KB/s | 仅作兜底 |
| UART 3 Mbaud（自动协商） | 约 290 KB/s | 256 MiB NAND 约 15 分钟 |
| FT232H 245 异步 FIFO | 约 2–3 MB/s | 256 MiB NAND 约 2 分钟 |

写入速度通常受芯片编程时间限制（NAND 每页 200–700 µs），另外每个段落有一次往返延迟。

## 5. 构建与仿真

```bash
pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula   # 全平台，含 macOS
python fpga/build.py --install      # 综合 + 布局布线 + 打包，并复制到上位机包内

sudo apt install iverilog && pip install cocotb                     # 仿真依赖
python fpga/sim/run_sim.py          # RTL 端到端测试（SPI NOR 与 SPI NAND 两种配置）
```

仿真里的 Verilog 芯片模型会检查 WE# 脉宽、数据建立时间、tWHR、tADL、忙时访问、CS# 中途拉高等违例，测试断言违例数为 0。

## 6. 以后可以做的

- FT232H **同步** FIFO（60 MHz，约 35 MB/s）：需要 FT232H 的 CLKOUT 接到全局时钟脚（例如 35 脚 GCLKT_4），链路模块要重写。
- Quad SPI 读取。
- 1.8V 支持：电平转换转接板，或者正式 PCB 上给 NAND/SPI 所在 bank 单独做可调 VCCIO。
- 16 位 NAND、多 CE 芯片。
