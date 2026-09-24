# nsprog · 自制 NAND / SPI Flash 编程器

基于 **Sipeed Tang Nano 9K（高云 GW1NR-9 FPGA）** 的开源 Flash 编程器：

- **并口 NAND**：TSOP48，以及经转接板的 BGA63/BGA48，8 位
- **SPI NOR / SPI NAND**：SOP8、WSON8、USON8、DIP8、SOIC16、测试夹在板读写
- 所有转接座**同时接好**，换芯片不用改线
- 上位机 **Python 跨平台**（macOS / Windows / Linux），提供**命令行**和 **iOS 风格的本地网页界面**；也有 macOS .app、Windows 安装包和单文件程序
- **接线自检**：`nsprog doctor` 找短路、连锡和接错的线；网页“接线”页有各封装接线图、打勾清单，可以让单根线闪烁来查线
- **离线工具**：OOB 去除/添加、ECC 检查与纠错（与 Linux 内核的 Hamming / BCH 逐字节一致）、UBI 卷解析与提取、十六进制查看、坏块分布图
- 芯片库：直接使用 [bbogush/nand_programmer](https://github.com/bbogush/nand_programmer) 的数据库，再加上 ONFI（并口 NAND）、SFDP（SPI NOR）自动识别，以及 SPI NOR/NAND 补充表
- 全新协议：FPGA 是微操作执行引擎，**支持新芯片只改上位机**
- 链路：板载 USB 串口（自动提速到 3 Mbaud，约 290 KB/s）；可选 FT232H：异步 FIFO 约 2 MB/s，同步 FIFO 仿真约 13 MB/s
- 提速选项：54 MHz 主时钟，NAND 突发读写与快速时序（`--nand-timing fast|turbo`），SPI NOR / SPI NAND 双线、四线读写（`--spi-io`、`--spi-quad-write`），FT232H 同步 FIFO 相位自动调节（`ft232h-tune`）

> 状态：FPGA 设计、上位机、仿真和软件模拟器测试已完成；**尚未在实物上验证**。第一次使用请按 [上手指南](docs/quickstart.md) 逐步检查。
>
> ⚠️ **接线表在 0.2 版改过**：Tang Nano 9K 的 79–86 脚是 1.8V，NAND 控制线和 SPI DI 已改到 69–75 脚，FT232H CLKOUT/SIWU# 对调到 36/35 脚。以 [docs/wiring.md](docs/wiring.md) 为准。

![读写页面](docs/img/ui-rw.png)

## 快速开始

```bash
brew install python pipx openfpgaloader      # macOS；Windows/Linux 见上手指南
pipx install ./host
nsprog fpga-flash                            # 烧写 Tang Nano 9K（一次性）
nsprog doctor                                # 接线自检：短路、空闲电平、芯片识别
nsprog info                                  # 检测编程器和芯片
nsprog read -t nand backup.bin               # 备份并口 NAND（含 OOB）
nsprog write -t spi firmware.bin             # 擦除 + 写入 + 校验 SPI Flash
nsprog web                                   # 打开网页界面
```

没有硬件也能体验：`nsprog -p emu info`、`nsprog web` 后在端口里选 “Software emulator”。

**不想装 Python**：到 [Release](https://github.com/Loong1996/nand-flash-programmer/releases) 下载 macOS 的 `.dmg`（nsprog.app）、Windows 安装包 `nsprog-<版本>-windows-setup.exe`，或单文件程序（macOS / Windows / Linux）。双击打开网页界面，单文件程序加参数就是命令行。未签名时的打开方法见[上手指南](docs/quickstart.md#1-安装上位机一次性)。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/quickstart.md](docs/quickstart.md) | **到货后上手指南**：安装、烧固件、接线检查、第一次读写、FT232H、常见问题 |
| [docs/wiring.md](docs/wiring.md) | **接线表**：TSOP48、SOP8/WSON8/DIP8、SOIC16、测试夹、FT232H |
| [docs/05-shopping-list.md](docs/05-shopping-list.md) | 采购清单（含 WSON8、SOIC16 座） |
| [docs/protocol.md](docs/protocol.md) | PC ↔ FPGA 协议 NSP v1 |
| [docs/03-fpga-design.md](docs/03-fpga-design.md) | FPGA 设计、构建与仿真 |
| [docs/04-hardware-bringup.md](docs/04-hardware-bringup.md) | 实物调试与逻辑分析仪 |
| [docs/06-command-reference.md](docs/06-command-reference.md) | NAND / SPI 命令速查、坏块与 ECC |
| [docs/01-overview.md](docs/01-overview.md) | 方案对比与决策记录 |
| [docs/02-nando-and-market-boards.md](docs/02-nando-and-market-boards.md) | NANDO 与市售“4.1”板分析 |
| [docs/07-roadmap.md](docs/07-roadmap.md) | 路线图 |

## 目录

```
fpga/rtl/            Verilog：引擎、NAND 总线、SPI、UART、FT245、顶层
fpga/constraints/    Tang Nano 9K 引脚约束与时钟约束
fpga/sim/            cocotb + Icarus 端到端仿真（Verilog 芯片模型 + 真实上位机驱动）
fpga/build.py        构建比特流（yosys / nextpnr-himbaechel / apycula，pip 可装）
host/                Python 上位机 nsprog（命令行、网页界面、驱动、芯片库、模拟器、测试）
host/src/nsprog/bitstream/   预编译好的比特流
host/packaging/      打包：单文件程序、macOS .app/.dmg、Windows 安装包（PyInstaller、Inno Setup）
docs/                文档
```

## 开发

```bash
pip install -e "host[test]" && pytest host                     # 上位机测试（用软件模拟器）
pip install ruff "mypy==2.3.1" && ruff check host && (cd host && mypy src packaging)   # 代码检查
python fpga/sim/run_sim.py                                     # RTL 仿真（需要 iverilog、cocotb）
pip install yowasp-yosys yowasp-nextpnr-himbaechel-gowin apycula
python fpga/sim/run_sim.py gate                                # 门级仿真（综合后网表）
python fpga/build.py --install                                 # 重新生成比特流
pip install "./host[fs]" pyinstaller pillow
python host/packaging/build_app.py              # 单文件程序
python host/packaging/build_app.py --app        # macOS：nsprog.app + .dmg（见 host/packaging/README）
python host/packaging/build_app.py --installer  # Windows：Inno Setup 安装包
```

## 许可证

GPLv3（见 [LICENSE](LICENSE)）。芯片数据库 `host/src/nsprog/data/nando_*.csv` 来自 bbogush/nand_programmer（GPLv3）。
