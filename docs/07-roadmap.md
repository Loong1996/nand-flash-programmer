# 07 路线图

| 里程碑 | 内容 | 验收标准 | 所需硬件 |
|---|---|---|---|
| **M0 环境** | Mac 安装 oss-cad-suite、PulseView；Tang Nano 9K 跑通 LED 闪烁 + 串口 Hello | openFPGALoader 烧录成功，串口能收到字符 | Tang Nano 9K |
| **M1 读 ID** | 纯 Verilog 状态机：`FF` 复位 → `90 00` → 读 5 字节 → 串口输出 | 第 1 字节 `0xEF`；逻辑分析仪波形与手册时序一致 | + TSOP48 座、W29N02KV、杜邦线、电容、逻辑分析仪 |
| **M2 读参数页与整页** | `90 20` 读 `"ONFI"`；`EC` 读参数页并校验 CRC；读第 0 块第 0 页（含 OOB） | 参数页解析正确；整页数据可重复读出一致 | 同上 |
| **M3 LiteX 软核 + 仿 FSMC 外设** | LiteX 生成 VexRiscv SoC；实现 6 节寄存器草案的 NAND 控制器；移植 `fsmc_nand.c` | 软核上跑通读 ID、读页、擦除、编程、读状态 | 同上 |
| **M4 移植 NANDO 主逻辑** | `nand_programmer.c`、`nand_bad_block.c`；通信先用板载串口 | Python 上位机（Mac）完成整片读、擦、写、校验、坏块扫描（慢速） | 同上 |
| **M5 FT232H 高速通道** | FT232H 同步 FIFO 桥 + 数据直通 DMA；上位机换 libftdi 通信层 | 读速度 ≥ 10MB/s，整片读写校验一致 | + FT232H 模块 |
| **M6 SPI Flash** | SPI 控制器 + 移植 `spi_flash.c`；SPI NOR，随后 SPI NAND | W25Q64 读写校验；SPI NAND 读写 + 片上 ECC 开关 + 解保护 | + SOP8 座、测试夹、测试片 |
| **M7 底板** | 画 Tang Nano 9K 底板：TSOP48 翻盖座、上拉、去耦、FT232H、SOP8 座 | 不再飞线，M1–M6 全部复测通过 | 嘉立创打样 |
| **M8 1.8V 支持** | 电平转换小板，或正式板可调 VCCIO | 1.8V NAND / SPI 芯片读写校验通过 | 电平转换器件或正式板 |
| **M9 正式板** | GW1N-9 LQFP144 + FT232H + 可调 VCCIO + 目标电源开关/限流 | 全功能复测 | 正式 PCB |
| **M10 上位机完善** | ECC 布局插件、坏块策略选项、芯片数据库、图形界面（可选：NANDO Qt 上位机移植到 macOS） | — | — |

## 目录规划（后续）

```
docs/                 设计文档（本目录）
fpga/
  constraints/        引脚约束（.cst）
  rtl/                Verilog：NAND 控制器、SPI 控制器、FT232H FIFO 桥、DMA
  litex/              LiteX SoC 目标脚本
  sim/                仿真测试
firmware/             软核固件（移植自 NANDO）
host/                 Python 上位机（macOS / Linux / Windows）
hardware/             底板与正式板（KiCad / 立创 EDA）
```

## 备选路线

若 FPGA 进展受阻：回退 MCU 方案 —— STM32F103VET6 直接使用 NANDO 固件 + 自写 Mac 上位机，或 CH32V307（USB 高速）移植。见 [01 方案总览](01-overview.md)。
