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
| 网页界面重做（iOS 风格）：十六进制查看、坏块分布图、离线工具、历史、设置 | FastAPI 测试 + 无头浏览器截图检查 |
| 离线工具：OOB 去除/添加、ECC（Hamming/BCH）检查与纠错、UBI 解析与提取 | 与 Linux 内核实现逐字节对比；ubinize 生成的镜像 |
| 突发读写（NAND 每字节 2 时钟）、NAND 快速时序档、SPI NOR 四线读（6Bh） | RTL 仿真，芯片模型时序检查 0 违例 |
| FT232H 245 同步 FIFO（60 MHz 时钟域 + 双时钟 FIFO，自动切换） | RTL 仿真（含 TXE# 反压、RXF# 间隙、同步↔异步切换）；布局布线 60 MHz 域约 66 MHz |
| 免 Python 单文件程序（macOS / Windows / Linux） | GitHub Actions 构建 + 模拟器冒烟测试 |
| 引脚测试（固件 1.2）+ `nsprog doctor` 接线自检（空闲电平、对电源短路、线间短路、探针找断线）+ `nsprog pintest` | RTL 仿真（含人为注入的 IO2–IO3 短路）+ 模拟器 |
| 网页“接线”页：各封装接线图、逐根打勾清单、单线闪烁、实时电平、打印 | 无头浏览器截图检查 |
| 门级仿真（yosys 网表 + 高云单元模型） | `run_sim.py gate`，CI 每次运行 |
| ruff + mypy 检查、边界条件测试 | CI |
| macOS .app / .dmg（可选开发者签名 + 公证）、Windows 安装包（Inno Setup） | GitHub Actions 构建 |

## 下一步：实物验证（到货后）

| 步骤 | 验收标准 |
|---|---|
| M1 烧写固件 | LED0 闪烁；`nsprog info` 显示 gateware 1.2 |
| M2 UART 提速 | `nsprog info` 显示 3000000 baud；`nsprog selftest` 通过 |
| M3 接线检查 | 空座子 `nsprog doctor` 全部正常；`doctor --probe` 在座子上逐脚碰一遍都有反应 |
| M4 W29N02KV | 识别出芯片；两次整片读取一致；写入、校验、擦除通过 |
| M5 SPI NOR / SPI NAND | W25Q64、W25N01GV 读写校验通过 |
| M6 FT232H | EEPROM 设置成功；`link FT232H`；读取速度 ≥ 1.5 MB/s |
| M7 提速 | `--nand-timing fast` 读两遍一致；`--port ft232h-sync` 下 `selftest` 通过、NAND 读取 ≥ 8 MB/s；`--spi-quad on` 读出与单线一致 |

实物上遇到的问题会按实际情况修正，时序参数也可能需要调整。

## 以后

- 1.8V 电平转换转接板（SN74AVC8T245 等）
- Tang Nano 9K 底板 PCB（集成所有座子、上拉电阻、去耦电容、FT232H）
- rPLL 提高主时钟（NAND 时序粒度更细、SPI 更快）
- SPI NAND 四线读、SPI 四线写、16 位 NAND、多 CE
- 更多 SoC 的 ECC 布局预设（目前是 Linux 通用的 Hamming / BCH 布局，偏移可调）
- 正式板：GW1N-9 LQFP144 + 可调 VCCIO
