# 03 FPGA 设计（最终方案：纯硬件微操作引擎）

> 早期曾考虑“LiteX 软核 + 移植 NANDO 固件”。后来决定协议全部重写、不背 NANDO 的历史包袱，于是改为**纯硬件执行引擎**：FPGA 只执行微操作，所有智能放在上位机。旧方案的分析保留在 [02 文档](02-nando-and-market-boards.md) 第 4 节，供参考。

## 1. 为什么是“执行引擎”

- FPGA 逻辑小而固定：约 3000 个 LUT（占 35%）。固件 1.3 起 27 MHz 晶振经 rPLL 倍频为 54 MHz 主时钟（布局布线报告约 58–60 MHz）；FT232H 同步 FIFO 的 60 MHz 时钟域报告约 62–68 MHz。
- **支持新芯片只改上位机**：命令序列、地址格式、坏块、ECC、SFDP/ONFI 解析都在 Python 里，不用重新综合、不用重烧 FPGA。
- 可以完整仿真：RTL + Verilog 芯片模型 + 真实上位机驱动的端到端测试（`fpga/sim`）。

## 2. 结构

```
                 ┌──────────────────────── FPGA (GW1NR-9, 27 MHz ×2 = 54 MHz) ────────────────────────┐
USB ─ BL702 ─ UART ──┐                                                                              │
(板载, 115200→3M)    ├─► 端口选择 ─► RX FIFO 4 KiB ─► engine ─┬─► nand_bus ──► TSOP48 NAND           │
USB ─ FT232H ─ FIFO ─┘   (回复走最近    ◄── TX FIFO 4 KiB ◄──┤   (突发读写)                           │
  245 异步 (54 MHz)        收到命令的口)                      └─► spi_master ─► SOP8/WSON8/SOIC16    │
  或 245 同步 (CLKOUT ─ 调相 PLL ─ 60 MHz 时钟域       (单 / 双 / 四线收发)                  │
              + 双时钟 FIFO)                                                                      │
                 └──────────────────────────────────────────────────────────────────────────────────┘
```

| 文件 | 作用 |
|---|---|
| `fpga/rtl/top_tangnano9k.v` | 顶层：两个 rPLL（主时钟 ×2；CLKOUT 调相）、复位、同步器、FIFO、端口选择、三态引脚、LED |
| `fpga/rtl/engine.v` | 操作码解析与执行；时序寄存器；超时；波特率、同步 FIFO 相位切换与看门狗 |
| `fpga/rtl/nand_bus.v` | NAND 异步总线周期（命令 / 地址 / 写数据 / 读数据），自动满足 tWHR、tADL；请求/应答握手，可首尾相接连续执行 |
| `fpga/rtl/spi_master.v` | SPI 模式 0，分频可调，可选延迟采样；单线 / 双线 / 四线收发（每字节可切换线宽）；连续字节之间不停时钟 |
| `fpga/rtl/uart.v` | 8N1 UART，运行时可改分频 |
| `fpga/rtl/ft245.v` | FT232H 245 异步 FIFO 读写状态机；245 同步 FIFO 桥（CLKOUT 时钟域） |
| `fpga/rtl/common.v` | 两级同步器、BRAM FIFO、双时钟 FIFO（格雷码指针）、LED 脉冲展宽 |
| `fpga/constraints/tangnano9k.cst` | 引脚约束 |
| `fpga/constraints/tangnano9k.sdc` | 时钟约束（27 MHz 晶振、54 MHz 主时钟、60 MHz CLKOUT 及其调相输出） |

协议见 [protocol.md](protocol.md)。

## 3. 关键设计点

- **上电安全**：所有低有效输出都用“高有效寄存器取反”的方式输出，FPGA 上电寄存器全为 0 时，CE#/WE#/RE#/CS# 都是高电平；NAND WP# 默认拉低（写保护）。
- **流控**：FT232H 靠 RXF#/TXE# 自然反压；UART 没有流控，由上位机按窗口发送（每段末尾加 ECHO 标记，未确认字节数不超过接收 FIFO 容量）。
- **自恢复**：操作收了一半、100 ms 内没有新字节到达时，引擎放弃该操作并释放 CE#/CS#；上位机用 ECHO 标记重新同步。
- **波特率协商**：`SET_BAUD` 后 1 秒内收不到确认，引擎自动回到 115200，所以协商失败也不会失联。
- **54 MHz 主时钟（固件 1.3）**：rPLL 把 27 MHz 晶振倍频到 54 MHz（VCO 864 MHz）。所有以“周期”计的常量（复位时的时序寄存器、CS# 最小高电平、引脚测试等待、异步 FIFO 的选通脉宽）都按 `CLK_HZ / 27 MHz` 缩放，时间不变；上位机的 NAND 时序档以纳秒定义，按 INFO 里的时钟频率换算，所以老固件（27 MHz）也照样能用。
- **时序可选**：NAND 默认 `safe` 档（读周期约 185 ns，适合杜邦线），`--nand-timing medium|fast|turbo|auto` 可提到 111 / 74 / 55 ns（`turbo` 要 54 MHz 固件，`auto` 按 ONFI 参数页的时序模式选，模式 4 以上才选 `turbo`）；SPI 默认 6.75 MHz，最高 27 MHz（老固件 13.5 MHz）。
- **突发传输**：总线模块用请求/应答握手，引擎在上一个周期的最后一个时钟就交出下一个请求，所以连续读写没有空闲时钟：NAND `turbo` 档每字节 3 个 18.5 ns 时钟（18 MB/s），SPI 四线每字节 4 个时钟（27 MHz SCK 时 13.5 MB/s）。
- **SPI 双线 / 四线（`SPI_WIDE`，固件 1.3）**：一个操作码覆盖所有宽总线命令，按 `flags` 选 1/2/4 线、读或写。写时 IO 线只在该操作期间驱动；读时释放 IO0–IO3 直到 CS# 拉高。上位机据此实现 SPI NOR 的 `3Bh`/`6Bh`/`BBh`/`EBh` 读（从 SFDP 取命令、模式位和空时钟数）和 `32h` 四线页编程，SPI NAND 的 `3Bh`/`6Bh`/`BBh`/`EBh` 读和 `32h`/`34h` 四线载入。`--spi-io auto` 按 1-4-4 > 1-1-4 > 1-2-2 > 1-1-2 的顺序选芯片支持的最快方式，需要时自动置 QE 位。
- **同步 FIFO 调相（固件 1.3）**：FT232H 的 CLKOUT 先进第二个 rPLL，同步桥用它的 CLKOUTP（动态相位 PSDA，16 档 × 22.5°，60 MHz 下每档约 1.04 ns；DUTYDA 跟着设为 PSDA+8 保持 50% 占空比）。相位由寄存器 10 设置，像波特率一样要确认：1 秒内收不到 INFO 就退回上一个已确认的相位，所以试到坏相位也不会失联。`nsprog -p ft232h-sync ft232h-tune` 扫描 16 档，每档用 ECHO 图样来回测试，取最长可用窗口的中点并保存，之后每次以 `ft232h-sync` 连接时自动设置。给实物留足调试余量：线长、杜邦线、FT232H 模块不同导致的建立 / 保持时间偏移都能在上位机上调回来，不用改 FPGA。
- **同步 FIFO 自动切换**：FPGA 看到 CLKOUT 在翻转（上位机用 `--port ft232h-sync` 打开 FT232H 时才有）就把 FT232H 数据线交给 60 MHz 时钟域的同步桥，并开始驱动 OE#；CLKOUT 停止约 2.4 ms 后自动回到异步桥。两个时钟域之间用双时钟 FIFO（格雷码指针）；CLKOUT 停下时 60 MHz 一侧会冻结，所以主时钟一侧要等 CLKOUT 稳定 64 个周期、调相 PLL 锁定后才开始收发。
- **1.8V bank**：Tang Nano 9K 的 bank 3（79–86 脚，和 LED、按键同组）是 1.8V，所有 Flash 和 FT232H 信号都放在 3.3V 的 bank 1/2 上。CLKOUT 放在 36 脚而不是 35 脚（GCLKT_4），因为开源工具链无法把 35 脚连到全局时钟网络。
- **引脚测试（PIN_TEST，固件 1.2）**：21 根 NAND/SPI 信号线都接成双向 IO，平时由总线模块驱动；测试模式下改由引擎统一控制（全部释放 / 单根拉低 / 拉高 / 2 Hz 翻转），10 µs 后读回全部 21 根线的焊盘电平。`nsprog doctor` 靠它查短路和空闲电平，`nsprog pintest` 和网页“接线”页的“闪烁”按钮靠它逐根查线。上位机在任何普通读写前都会先退出测试模式（PIN_TEST 模式 0）。测试控制信号先打一拍寄存器，被释放的引脚保持原来的输出值、只撤掉输出使能（空闲电平和上下拉一致），所以进出测试模式时 WE#/RE#/CE# 不会出现毛刺。这个问题是门级仿真发现的：最初的写法在切换瞬间会让 RE#、CE# 出现零宽度低脉冲，芯片模型因此开始驱动数据线；现在的 NAND 模型会把小于 10 ns 的 RE#/CE# 脉冲记为违例。
- **双用途引脚**：FT232H 数据线用到的 53–57 脚是配置接口复用脚，打包时用 `--sspi_as_gpio --mspi_as_gpio --cpu_as_gpio` 把它们释放为普通 IO（与 LiteX 对该板的默认做法相同）。

## 4. 速度估算

| 链路 | 读 | 说明 |
|---|---|---|
| UART 115200 | 约 11 KB/s | 仅作兜底 |
| UART 3 Mbaud（自动协商） | 约 290 KB/s | 256 MiB NAND 约 15 分钟 |
| FT232H 245 异步 FIFO | 约 2 MB/s | 256 MiB NAND 约 2 分钟 |
| FT232H 245 同步 FIFO + NAND `turbo` | 约 17 MB/s（仿真） | 256 MiB NAND 约 16 秒；实物未验证 |

仿真实测（`fpga/sim`，Icarus + cocotb，数据经过真实的上位机驱动）：

| 场景 | 结果 |
|---|---|
| 同步 FIFO，`NAND_READ` 60000 字节连续读，`turbo` 档 | 17.99 MB/s（每字节 3 个 18.5 ns 时钟） |
| 同步 FIFO，`NAND_READ` 60000 字节连续读，`fast` 档 | 13.50 MB/s |
| 同步 FIFO，整块读取（含页加载、坏块检查），`fast` 档 | 13.07 MB/s（模型 tR = 3 µs；实际芯片 tR ≈ 25 µs） |
| 异步 FIFO，SPI NOR 27 MHz，单线 / 1-1-2 / 1-2-2 / 1-1-4 / 1-4-4 读 | 均约 2.8 MB/s（瓶颈在异步 FIFO 链路；同步 FIFO 下四线读的总线上限 13.5 MB/s） |
| 同步 FIFO 调相扫描（`ft232h-tune`，FT232H 模型：输出在 CLKOUT 上升沿后 4 ns 变化，在下降沿采样 FPGA 输出） | 16 档中 12 档可用（相位 4–7 失败），选中 13；每个坏相位由看门狗退回，上位机重新同步 |

写入速度通常受芯片编程时间限制（NAND 每页 200–700 µs），另外每个段落有一次往返延迟。

## 5. 构建与仿真

```bash
pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula   # 全平台，含 macOS
python fpga/build.py --install      # 综合 + 布局布线 + 打包，并复制到上位机包内

sudo apt install iverilog && pip install cocotb                     # 仿真依赖
python fpga/sim/run_sim.py          # RTL 端到端测试（SPI NOR、SPI NAND、W29N02KV 三种配置）
python fpga/sim/run_sim.py stress   # 长时间压力测试：随机 / 截断命令流、UART 帧错误、同步 FIFO 满和停顿
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

- 同步 FIFO 的 IO 时序（FT232H 要求 8 ns 建立时间）仍要在实物上验证。已经可以用 `ft232h-tune` 调相位；如果窗口仍然很窄，下一步是把 FT232H 数据 / RD# / WR# / OE# 放进 IO 单元的寄存器（IOLOGIC），让输出时刻不受布线影响。
- SPI NOR 的 QPI（4-4-4）和 DTR 模式。
- 1.8V 支持：电平转换转接板，或者正式 PCB 上给 NAND/SPI 所在 bank 单独做可调 VCCIO。
- 16 位 NAND、多 CE 芯片。
