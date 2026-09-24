# 07 路线图

## 已完成（未经实物验证）

| 项目 | 验证方式 |
|---|---|
| FPGA 微操作引擎（UART + FT232H 245 FIFO、NAND 总线、SPI、超时、波特率协商） | 综合 + 布局布线（27 MHz 时序通过，最高约 60 MHz）；cocotb 端到端仿真 |
| NSP v1 协议 | 仿真 + 软件模拟器 |
| 上位机：并口 NAND（大页/小页、ONFI、R/B# 或状态轮询）、SPI NOR（SFDP、4 字节地址、DataFlash）、SPI NAND | 模拟器单元测试 + RTL 仿真 |
| 读 / 写 / 擦 / 校验 / 查空 / 坏块（skip / keep / force），写前擦除、跳过全 FF 页、写后校验 | 同上 |
| 命令行、本地网页界面 | 模拟器 + FastAPI 测试 |
| 芯片库：NANDO + 电压表 + SPI NOR ID 表 + SPI NAND 表 | 单元测试 |
| 全封装接线文档、上手指南、采购清单 | — |

## 下一步：实物验证（到货后）

| 步骤 | 验收标准 |
|---|---|
| M1 烧写固件 | LED0 闪烁；`nsprog info` 显示 gateware 1.0 |
| M2 UART 提速 | `nsprog info` 显示 3000000 baud；`nsprog selftest` 通过 |
| M3 接线检查 | `nsprog pins`：IO 全 1、R/B# 为高 |
| M4 W29N02KV | 识别出芯片；两次整片读取一致；写入、校验、擦除通过 |
| M5 SPI NOR / SPI NAND | W25Q64、W25N01GV 读写校验通过 |
| M6 FT232H | EEPROM 设置成功；`link FT232H`；读取速度 ≥ 1.5 MB/s |

实物上遇到的问题会按实际情况修正，时序参数也可能需要调整。

## 以后

- 1.8V 电平转换转接板（SN74AVC8T245 等）
- Tang Nano 9K 底板 PCB（集成所有座子、上拉电阻、去耦电容、FT232H）
- FT232H 同步 FIFO（约 35 MB/s）
- Quad SPI 读取、16 位 NAND、多 CE
- 上位机 ECC 插件（按目标 SoC 的 BCH 布局生成或校验 OOB）
- 正式板：GW1N-9 LQFP144 + 可调 VCCIO
