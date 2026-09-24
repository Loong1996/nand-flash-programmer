# 到货后上手指南

按顺序做完，大约 30 分钟。命令以 macOS 为例，Windows、Linux 的差异单独标出。

> 本项目的 FPGA 设计和上位机已在仿真和软件模拟器中完整测试，但**还没有在实物上验证过**。第一次上电请按下面的步骤逐项检查；遇到问题，把命令输出发给我。

## 1. 安装上位机（一次性）

需要 Python 3.9 以上版本。

```bash
# macOS
brew install python pipx openfpgaloader
git clone https://github.com/Loong1996/nand-flash-programmer.git
cd nand-flash-programmer
pipx install ./host          # 或者：python3 -m pip install --user ./host
nsprog --version
```

- **Linux**：`pipx install ./host`；把用户加入 `dialout` 组，才能访问串口（`sudo usermod -aG dialout $USER`，重新登录后生效）。openFPGALoader 用发行版自带的包即可。
- **Windows**：从 python.org 安装 Python，执行 `py -m pip install .\host`。烧 FPGA 可以用 openFPGALoader 的 Windows 版，也可以用高云官方的 Gowin Programmer（选择 `host\src\nsprog\bitstream\nsprog_tangnano9k.fs`）。
- **不装 Python**：从 [Release](https://github.com/Loong1996/nand-flash-programmer/releases) 或 GitHub Actions 的 `apps` 工作流下载：
  - **macOS**：`nsprog-<版本>-macos-arm64.dmg`。打开后把 nsprog 拖进“应用程序”。没有 Apple 开发者签名时，第一次要**右键 → 打开**（或“系统设置 → 隐私与安全性 → 仍要打开”）；提示“已损坏”时执行 `xattr -dr com.apple.quarantine /Applications/nsprog.app`。双击打开网页界面，在设置页可以“退出程序”。命令行：`/Applications/nsprog.app/Contents/MacOS/nsprog info`。
  - **Windows**：`nsprog-<版本>-windows-setup.exe`，装在当前用户目录下，不需要管理员权限；会建开始菜单和桌面快捷方式，可选“加入 PATH”，勾上后命令行直接用 `nsprog`。SmartScreen 拦截时点“更多信息 → 仍要运行”。
  - **单文件**：`nsprog-macos-arm64`（先 `chmod +x`）、`nsprog-windows-x86_64.exe`、`nsprog-linux-x86_64`。双击打开网页界面，加参数就是命令行。
  - 用 FT232H 时 macOS 还需要 `brew install libusb`。重复双击不会再开一个服务，只会重新打开浏览器页面。

## 2. 烧写 FPGA 固件（一次性）

只插 Tang Nano 9K，先不接任何线：

```bash
nsprog fpga-flash          # 调用 openFPGALoader -b tangnano9k -f <内置固件>
```

完成后按一下板上的复位键或重新插拔 USB，**LED0 开始闪烁**就表示固件在运行。

想先临时试一下、不写入 Flash，可以用 `nsprog fpga-flash --sram`（断电后丢失）。

## 3. 检查连接（还不接芯片）

```bash
nsprog info
```

应该看到类似这样的输出：

```
gateware 1.2, protocol 1, board 1, clock 27.0 MHz, rx fifo 4096, link UART, flags 00
UART 3000000 baud
parallel NAND: no chip (ID FFFFFFFFFFFFFFFF)
SPI: no chip (ID FFFFFFFFFF)
```

- 上位机会自动把串口从 115200 提到 3M；如果不稳定，用 `nsprog --no-fast-uart info`（全局参数要写在子命令前面）。
- 自动找不到串口时手动指定：`nsprog -p /dev/cu.usbserial-XXXX1 info`（macOS）、`nsprog -p /dev/ttyUSB1 info`（Linux）、`nsprog -p COM5 info`（Windows）。Tang Nano 9K 会出现两个口，**编号大的那个**是串口。
- `nsprog selftest` 可以对链路做压力测试。

## 4. 接线

按 [wiring.md](wiring.md) 把 TSOP48 座、SOP8/WSON8/SOIC16 座（以及 FT232H）全部接好，并加上去耦电容。网页界面的**“接线”页**有每种封装的接线图和逐根打勾的清单（可以打印），每一行的“闪烁”按钮会让那根线以 2 Hz 翻转，用万用表或 LED 在座子上就能确认接对了没有。

接完先**不放芯片**，运行自检：

```bash
nsprog doctor
```

```
✓ 链路：UART，固件 1.2
✓ 空闲电平：21 根线都停在各自的上拉/下拉电平
✓ 对电源短路：每根线都能被拉高和拉低
✓ 线间短路：没有两根线连在一起
· 芯片：两个座子都没有识别到芯片
结论：全部正常
```

`doctor` 会把 21 根 NAND/SPI 信号线逐根拉低、拉高，找出**对 GND/3V3 短路**、**两线相碰**（例如 TSOP48 相邻引脚连锡）和空闲电平异常，每条问题都给出 FPGA 脚号和座子脚号。它只驱动 3.3V 弱信号，但**座子里有芯片时建议加 `--no-drive`**（只读电平，不驱动）。

- **断线**从 FPGA 一侧看不出来。用 `nsprog doctor --probe`：拿一根接 GND（或 3V3）的杜邦线，逐个碰座子引脚，终端会实时显示碰到的是哪根信号；碰了没反应就是断线或接错座子脚。
- **单根线**：`nsprog pintest --pin CE# --mode toggle`（2 Hz 翻转 30 秒），`--mode low/high` 保持电平，`nsprog pintest --list` 列出可测的线。适合配合万用表或 LED 查线。
- 旧的检查方法仍然可用：`nsprog pins` 显示 `NAND IO[7:0]` 必须是 `11111111`，`R/B#` 必须是 high。

## 5. 第一颗芯片：W29N02KV（TSOP48）

断电，放入芯片，合上翻盖，重新插 USB：

```bash
nsprog info                                # 应识别出 Winbond W29N02KV（ONFI）或数据库型号
nsprog badblocks -t nand                   # 扫描出厂坏块
nsprog read -t nand backup.bin             # 整片备份（含 OOB），先做这个！
nsprog verify -t nand backup.bin --oob     # 再读一遍比较，确认读取稳定
```

写入：

```bash
nsprog write -t nand image.bin             # 默认：先擦除、跳过坏块、写后校验
nsprog write -t nand image.bin --no-oob    # 镜像只有主数据区（不含 OOB）时
nsprog erase -t nand                       # 整片擦除（自动跳过出厂坏块）
```

常用选项：`--start-block N --blocks N`（或 `--offset 0x20000 --length 1M`）、`--bb skip|keep|force`、`--no-rb`（R/B# 没接时）。

## 6. SPI Flash（SOP8 / WSON8 / SOIC16）

```bash
nsprog info
nsprog read -t spi spi.bin
nsprog write -t spi firmware.bin           # 自动解除块保护、擦除、写入、校验
nsprog write -t spi part.bin --offset 0x10000
nsprog erase -t spi
```

- 默认 SPI 时钟 6.75 MHz。杜邦线较长或用测试夹时加 `--spi-mhz 2`。
- 不认识的芯片：有 SFDP 的会按 SFDP 自动识别；没有的话用 `nsprog chips <关键字>` 查型号，再用 `-c 型号` 指定。

## 7. 网页界面

```bash
nsprog web
```

浏览器自动打开 http://127.0.0.1:8765 。左侧（手机上是底部）的页面：

| 页面 | 用途 |
|---|---|
| 设备 | 选端口、连接；**引脚诊断**（8 根数据线的电平灯、R/B#、FT232H CLKOUT）；链路自检测速 |
| 芯片 | 两张芯片卡片（并口 NAND / SPI），点选操作对象；手动指定型号；检测日志 |
| 读写 | 范围、坏块处理、OOB、NAND 时序档、SPI 四线读；六个操作；把文件拖进来即可写入或校验；进度环、速度、剩余时间 |
| 数据 | 读出结果的**十六进制查看器**（OOB 标橙色，可跳到 `0x…` / `p页` / `b块`，文本或 HEX 查找）；**坏块分布图** |
| 工具 | 不接硬件也能用：镜像信息、去除/添加 OOB、ECC 检查/纠错、UBI 信息/提取 |
| 芯片库 / 历史 / 设置 | 搜索全部型号；每次操作的记录；主题（自动/浅色/深色）和默认参数 |

没有硬件时，可以在端口里选 “软件模拟器” 体验全部功能。设置和历史保存在 `~/.nsprog`。

## 8. FT232H 高速通道（可选）

1. **只插 FT232H 模块**（先不要接到 FPGA），把 EEPROM 改成 FIFO 模式：

   ```bash
   nsprog ft232h-setup                 # Linux / Windows
   sudo nsprog ft232h-setup --custom-pid   # macOS：同时改 PID，避免系统 FTDI 驱动占用
   ```
   然后拔插 FT232H。
   - macOS 用 sudo 仍然失败时，可以在 Linux、Windows 或 Windows 虚拟机里执行一次这一步，只需做一次。
   - Windows 需要先用 Zadig 给 FT232H 安装 WinUSB 驱动。
   - Linux 需要 udev 规则：`SUBSYSTEM=="usb", ATTR{idVendor}=="0403", MODE="0666"`。
2. 断电，按 [wiring.md](wiring.md) 第 4 节接线。
3. 两根 USB 都插上，运行 `nsprog info`，应显示 `link FT232H`。也可以用 `-p ft232h` 强制指定。
4. （可选，更快）同步 FIFO：`nsprog -p ft232h-sync selftest`。通过后读写都加 `-p ft232h-sync`，网页里选 “FT232H 同步 FIFO”。这个模式只在仿真里验证过，不稳定就回到 `-p ft232h`。

## 8.1 提速选项

| 选项 | 作用 | 什么时候用 |
|---|---|---|
| `--nand-timing safe` | 默认，读周期约 185 ns | 杜邦线、第一次用 |
| `--nand-timing medium` / `fast` | 111 ns / 74 ns（fast 总线上限 13.5 MB/s） | 线短（< 10 cm）、读两遍结果一致 |
| `--nand-timing auto` | 按 ONFI 参数页里的时序模式自动选 | ONFI 芯片 |
| `--spi-quad auto` | 默认；芯片已经打开 QE 位时用 `6Bh` 四线读 | — |
| `--spi-quad on` | 临时打开 QE 位，读完恢复原值 | 想让 SPI NOR 读得更快 |
| `--spi-mhz 13.5` | SPI 最高时钟 | 线短、芯片支持 |

只有配合 FT232H 才看得出区别：串口链路本身只有约 290 KB/s。

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `programmer not responding` | LED0 不闪：重新烧固件。LED0 闪：换另一个串口号（`nsprog -p 端口 info`），或者用 `nsprog --no-fast-uart info` |
| NAND ID 全是 FF | 芯片没放好或方向反了；CE#/RE# 接错；VCC、GND 没接 |
| NAND ID 全是 00 | 数据线短路到地；芯片没供电 |
| ID 每次读都不一样 | 缺去耦电容；地线太少；杜邦线太长 |
| `NAND stayed busy` | R/B# 没接或接错：检查 49 脚，或者加 `--no-rb` |
| 写入报 `write protected` | WP#（TSOP48 19 脚 → FPGA 69 脚）没接好 |
| SPI 识别成奇怪的大小 | 用 `--spi-mhz 1` 再试；检查 HOLD#（7 脚）是否为高电平 |
| 校验失败 | 降低 SPI 频率；NAND 换 `--bb skip`；看是否 MLC 芯片（原始数据会有位翻转） |
| `doctor` 报“线间短路” | 按提示的两个座子脚号查连锡、杜邦线相碰；TSOP48 相邻引脚最常见 |
| `doctor` 报“对电源短路” | 那根线接到了 GND/3V3，或者座子里有芯片在驱动它（用 `--no-drive`） |
| `doctor` 全绿但读不到芯片 | 断线查不出来：用 `nsprog doctor --probe` 在座子上逐脚碰一遍 |
| macOS 提示“已损坏，无法打开” | `xattr -dr com.apple.quarantine /Applications/nsprog.app`，再右键 → 打开 |
| LED5 常亮 | 通信出过错。运行一次 `nsprog info` 会清除；频繁出现说明链路不稳定 |
