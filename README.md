# NAND / SPI Flash 编程器（自制）

一个在 **macOS 上原生可用**、**完全开源**的并口 NAND + SPI Flash 编程器项目。

当前阶段：**方案设计 / 采购准备**。代码与硬件尚未开始。

## 目标

- 支持 TSOP48 并口 NAND（8 位，ONFI/JEDEC 标准命令）
- 支持 SPI NOR、SPI NAND
- 上位机在 macOS 原生运行（同时兼顾 Linux / Windows）
- 最终支持 1.8V 与 3.3V 芯片
- 固件 / HDL / 上位机 / 硬件全部开源

## 当前选定路线

**FPGA 方案**：Sipeed Tang Nano 9K（高云 GW1NR-9）+ LiteX 软核（VexRiscv）+ “仿 FSMC” NAND 控制器，
移植开源项目 [bbogush/nand_programmer（NANDO）](https://github.com/bbogush/nand_programmer) 的固件逻辑，
USB 高速通道使用 FT232H 同步 FIFO。开发全程使用可在 macOS 运行的开源工具链（Yosys / nextpnr / Apicula / openFPGALoader）。

备选：MCU 方案（STM32F103 兼容 NANDO，或 CH32V307 USB 高速），见 [方案总览](docs/01-overview.md)。

## 文档目录

| 文档 | 内容 |
|---|---|
| [docs/01-overview.md](docs/01-overview.md) | 方案总览：芯片类型、两层协议、MCU/FPGA 各方案对比、速度分析、决策记录 |
| [docs/02-nando-and-market-boards.md](docs/02-nando-and-market-boards.md) | NANDO 开源项目分析、市售“NAND 编程器 4.1”板分析、在 Mac 上使用闭源板的办法、NANDO 代码复用评估 |
| [docs/03-fpga-design.md](docs/03-fpga-design.md) | FPGA 方案设计：选型、I/O 电压、系统架构、仿 FSMC 外设寄存器草案、FT232H 接口、移植清单 |
| [docs/04-hardware-bringup.md](docs/04-hardware-bringup.md) | 原型接线：Tang Nano 9K 引脚、TSOP48 引脚、接线表、上拉/去耦、首次读 ID 测试、排错 |
| [docs/05-shopping-list.md](docs/05-shopping-list.md) | 分阶段采购清单与 Mac 软件环境 |
| [docs/06-command-reference.md](docs/06-command-reference.md) | 并口 NAND / SPI NOR / SPI NAND 命令速查、坏块与 ECC 要点 |
| [docs/07-roadmap.md](docs/07-roadmap.md) | 里程碑与验收标准 |
| [fpga/constraints/tangnano9k_nand.cst](fpga/constraints/tangnano9k_nand.cst) | Tang Nano 9K 引脚约束草案（NAND 原型接线） |

## 许可证

计划采用 GPLv3（与 NANDO 保持一致，移植其代码时必须如此）。自行从零编写的 HDL 部分可另行选择许可证。
