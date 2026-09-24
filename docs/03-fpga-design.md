# 03 FPGA 设计（最终方案：纯硬件微操作引擎）

> 早期曾考虑“LiteX 软核 + 移植 NANDO 固件”。后来决定协议全部重写、不背 NANDO 的历史包袱，于是改为**纯硬件执行引擎**：FPGA 只执行微操作，所有智能放在上位机。旧方案的分析保留在 [02 文档](02-nando-and-market-boards.md) 第 4 节，供参考。

## 1. 为什么是“执行引擎”

- FPGA 逻辑小而固定：约 3000 个 LUT（占 35%），27 MHz 主时钟下时序余量很大（布局布线报告约 60 MHz）；FT232H 同步 FIFO 的 60 MHz 时钟域报告约 66 MHz。
- **支持新芯片只改上位机**：命令序列、地址格式、坏块、ECC、SFDP/ONFI 解析都在 Python 里，不用重新综合、不用重烧 FPGA。
- 可以完整仿真：RTL + Verilog 芯片模型 + 真实上位机驱动的端到端测试（`fpga/sim`）。

## 2. 结构

```
                 ┌──────────────────────────── FPGA (GW1NR-9, 27 MHz) ─────────────────────────────┐
USB ─ BL702 ─ UART ──┐                                                                              │
(板载, 115200→3M)    ├─► 端口选择 ─► RX FIFO 4 KiB ─► engine ─┬─► nand_bus ──► TSOP48 NAND           │
USB ─ FT232H ─ FIFO ─┘   (回复走最近    ◄── TX FIFO 4 KiB ◄──┤   (突发读写)                           │
  245 异步 (27 MHz)        收到命令的口)                      └─► spi_master ─► SOP8/WSON8/SOIC16    │
  或 245 同步 (CLKOUT 60 MHz 时钟域 + 双时钟 FIFO)                  (单线 / 四线读)                  │
                 └──────────────────────────────────────────────────────────────────────────────────┘
```

| 文件 | 作用 |
|---|---|
| `fpga/rtl/top_tangnano9k.v` | 顶层：复位、同步器、FIFO、端口选择、三态引脚、LED |
| `fpga/rtl/engine.v` | 操作码解析与执行；时序寄存器；超时；波特率切换与看门狗 |
| `fpga/rtl/nand_bus.v` | NAND 异步总线周期（命令 / 地址 / 写数据 / 读数据），自动满足 tWHR、tADL；请求/应答握手，可首尾相接连续执行 |
| `fpga/rtl/spi_master.v` | SPI 模式 0，分频可调，可选延迟采样；单线或四线输入；连续字节之间不停时钟 |
| `fpga/rtl/uart.v` | 8N1 UART，运行时可改分频 |
| `fpga/rtl/ft245.v` | FT232H 245 异步 FIFO 读写状态机；245 同步 FIFO 桥（CLKOUT 时钟域） |
| `fpga/rtl/common.v` | 两级同步器、BRAM FIFO、双时钟 FIFO（格雷码指针）、LED 脉冲展宽 |
| `fpga/constraints/tangnano9k.cst` | 引脚约束 |
| `fpga/constraints/tangnano9k.sdc` | 时钟约束（27 MHz、60 MHz） |

协议见 [protocol.md](protocol.md)。

## 3. 关键设计点

- **上电安全**：所有低有效输出都用“高有效寄存器取反”的方式输出，FPGA 上电寄存器全为 0 时，CE#/WE#/RE#/CS# 都是高电平；NAND WP# 默认拉低（写保护）。
- **流控**：FT232H 靠 RXF#/TXE# 自然反压；UART 没有流控，由上位机按窗口发送（每段末尾加 ECHO 标记，未确认字节数不超过接收 FIFO 容量）。
- **自恢复**：操作收了一半、100 ms 内没有新字节到达时，引擎放弃该操作并释放 CE#/CS#；上位机用 ECHO 标记重新同步。
- **波特率协商**：`SET_BAUD` 后 1 秒内收不到确认，引擎自动回到 115200，所以协商失败也不会失联。
- **时序可选**：NAND 默认 `safe` 档（读周期约 185 ns，适合杜邦线），`--nand-timing medium|fast|auto` 可提到 111 / 74 ns（`auto` 按 ONFI 参数页的时序模式选）；SPI 默认 6.75 MHz，最高 13.5 MHz。
- **突发传输**：总线模块用请求/应答握手，引擎在上一个周期的最后一个时钟就交出下一个请求，所以连续读写没有空闲时钟：NAND `fast` 档每字节 2 个时钟（13.5 MB/s），SPI 四线读每字节 4 个时钟（6.75 MB/s）。
- **同步 FIFO 自动切换**：FPGA 看到 CLKOUT 在翻转（上位机用 `--port ft232h-sync` 打开 FT232H 时才有）就把 FT232H 数据线交给 60 MHz 时钟域的同步桥，并开始驱动 OE#；CLKOUT 停止约 2.4 ms 后自动回到异步桥。两个时钟域之间用双时钟 FIFO（格雷码指针）；CLKOUT 停下时 60 MHz 一侧会冻结，所以 27 MHz 一侧要等 CLKOUT 稳定 64 个周期后才开始收发。
- **1.8V bank**：Tang Nano 9K 的 bank 3（79–86 脚，和 LED、按键同组）是 1.8V，所有 Flash 和 FT232H 信号都放在 3.3V 的 bank 1/2 上。CLKOUT 放在 36 脚而不是 35 脚（GCLKT_4），因为开源工具链无法把 35 脚连到全局时钟网络。
- **引脚测试（PIN_TEST，固件 1.2）**：21 根 NAND/SPI 信号线都接成双向 IO，平时由总线模块驱动；测试模式下改由引擎统一控制（全部释放 / 单根拉低 / 拉高 / 2 Hz 翻转），10 µs 后读回全部 21 根线的焊盘电平。`nsprog doctor` 靠它查短路和空闲电平，`nsprog pintest` 和网页“接线”页的“闪烁”按钮靠它逐根查线。上位机在任何普通读写前都会先退出测试模式（PIN_TEST 模式 0）。测试控制信号先打一拍寄存器，被释放的引脚保持原来的输出值、只撤掉输出使能（空闲电平和上下拉一致），所以进出测试模式时 WE#/RE#/CE# 不会出现毛刺。这个问题是门级仿真发现的：最初的写法在切换瞬间会让 RE#、CE# 出现零宽度低脉冲，芯片模型因此开始驱动数据线；现在的 NAND 模型会把小于 10 ns 的 RE#/CE# 脉冲记为违例。
- **双用途引脚**：FT232H 数据线用到的 53–57 脚是配置接口复用脚，打包时用 `--sspi_as_gpio --mspi_as_gpio --cpu_as_gpio` 把它们释放为普通 IO（与 LiteX 对该板的默认做法相同）。

## 4. 速度估算

| 链路 | 读 | 说明 |
|---|---|---|
| UART 115200 | 约 11 KB/s | 仅作兜底 |
| UART 3 Mbaud（自动协商） | 约 290 KB/s | 256 MiB NAND 约 15 分钟 |
| FT232H 245 异步 FIFO | 约 2 MB/s | 256 MiB NAND 约 2 分钟 |
| FT232H 245 同步 FIFO + NAND `fast` | 约 13 MB/s（仿真） | 256 MiB NAND 约 20 秒；实物未验证 |

仿真实测（`fpga/sim`，Icarus + cocotb，数据经过真实的上位机驱动）：

| 场景 | 结果 |
|---|---|
| 同步 FIFO，`NAND_READ` 60000 字节连续读 | 13.50 MB/s（总线上限：每字节 2 个 37 ns 时钟） |
| 同步 FIFO，整块读取（含页加载、坏块检查） | 12.96 MB/s（模型 tR = 3 µs；实际芯片 tR ≈ 25 µs，约 11 MB/s） |
| 异步 FIFO，SPI NOR 13.5 MHz 单线读 | 1.68 MB/s |
| 异步 FIFO，SPI NOR 13.5 MHz 四线读（6Bh） | 2.43 MB/s（此时瓶颈在异步 FIFO 链路） |

写入速度通常受芯片编程时间限制（NAND 每页 200–700 µs），另外每个段落有一次往返延迟。

## 5. 构建与仿真

```bash
pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula   # 全平台，含 macOS
python fpga/build.py --install      # 综合 + 布局布线 + 打包，并复制到上位机包内

sudo apt install iverilog && pip install cocotb                     # 仿真依赖
python fpga/sim/run_sim.py          # RTL 端到端测试（SPI NOR 与 SPI NAND 两种配置）
```

```bash
pip install yowasp-yosys            # 或系统自带的 yosys
python fpga/sim/run_sim.py gate     # 门级仿真：yosys 综合后的网表 + 高云单元模型
```

门级仿真用和 `build.py` 相同的 `synth_gowin` 流程综合出网表，再跑一组覆盖所有模块的端到端测试（UART、异步/同步 FIFO、并口 NAND、SPI 四线、引脚测试），用来发现“RTL 仿真对、综合后不对”的问题（例如三态没变成 IOBUF、初始值丢失）。两点说明：

- yosys 自带的 `gowin/cells_sim.v` 里 IOBUF 模型有错（写成了 `assign I = IO;`，输出 O 永远是 Z），`run_sim.py` 会复制一份并改正后再用。
- 块 RAM（DPB / DPX9B）没有可用的开源仿真模型，`fpga/sim/gowin_bram_sim.v` 是按高云文档写的行为模型，只覆盖本设计用到的模式。

门级仿真比 RTL 慢约 10 倍（6 个测试约 1 小时，CI 里只跑其中较快的几个）。

仿真里的 Verilog 芯片模型会检查 WE# 脉宽、数据建立时间、tWHR、tADL、忙时访问、CS# 中途拉高等违例，测试断言违例数为 0。

## 6. 以后可以做的

- 用 rPLL 把主时钟从 27 MHz 提到 50–80 MHz，NAND 总线周期可以更细（现在的 `fast` 档受 37 ns 时钟粒度限制）。
- 同步 FIFO 的 IO 时序（FT232H 要求 8 ns 建立时间）目前只能在实物上验证；如不稳定，可把 FT232H 相关输出放进 IOB 寄存器，或用 PLL 调相位。
- SPI NAND 四线读（`6Bh`/`EBh`），SPI 四线写（`32h`）。
- 1.8V 支持：电平转换转接板，或者正式 PCB 上给 NAND/SPI 所在 bank 单独做可调 VCCIO。
- 16 位 NAND、多 CE 芯片。
