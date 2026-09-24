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
gateware 1.0, protocol 1, board 1, clock 27.0 MHz, rx fifo 4096, link UART, flags 00
UART 3000000 baud
parallel NAND: no chip (ID FFFFFFFFFFFFFFFF)
SPI: no chip (ID FFFFFFFFFF)
```

- 上位机会自动把串口从 115200 提到 3M；如果不稳定，加 `--no-fast-uart`。
- 自动找不到串口时手动指定：macOS 用 `-p /dev/cu.usbserial-XXXX1`，Linux 用 `-p /dev/ttyUSB1`，Windows 用 `-p COM5`。Tang Nano 9K 会出现两个口，**编号大的那个**是串口。
- `nsprog selftest` 可以对链路做压力测试。

## 4. 接线

按 [wiring.md](wiring.md) 把 TSOP48 座、SOP8/WSON8/SOIC16 座（以及 FT232H）全部接好，并加上去耦电容。

接完先**不放芯片**，运行：

```bash
nsprog pins
```

`NAND IO[7:0]` 必须是 `11111111`，`R/B#` 必须是 high。

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

浏览器自动打开 http://127.0.0.1:8765 ，依次点：选择端口 → 连接 → 检测 → 读取/写入。
没有硬件时，可以在端口里选 “Software emulator” 体验全部功能。

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

## 9. 常见问题

| 现象 | 处理 |
|---|---|
| `programmer not responding` | LED0 不闪：重新烧固件。LED0 闪：换另一个串口号（`-p`），或加 `--no-fast-uart` |
| NAND ID 全是 FF | 芯片没放好或方向反了；CE#/RE# 接错；VCC、GND 没接 |
| NAND ID 全是 00 | 数据线短路到地；芯片没供电 |
| ID 每次读都不一样 | 缺去耦电容；地线太少；杜邦线太长 |
| `NAND stayed busy` | R/B# 没接或接错：检查 49 脚，或者加 `--no-rb` |
| 写入报 `write protected` | WP#（TSOP48 19 脚 → FPGA 80 脚）没接好 |
| SPI 识别成奇怪的大小 | 用 `--spi-mhz 1` 再试；检查 HOLD#（7 脚）是否为高电平 |
| 校验失败 | 降低 SPI 频率；NAND 换 `--bb skip`；看是否 MLC 芯片（原始数据会有位翻转） |
| LED5 常亮 | 通信出过错。运行一次 `nsprog info` 会清除；频繁出现说明链路不稳定 |
