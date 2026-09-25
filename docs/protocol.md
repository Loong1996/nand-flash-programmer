# NSP 通信协议 v1（PC ↔ FPGA）

全新设计，与 NANDO 协议无关。FPGA 是一个**微操作执行引擎**：上位机把一串操作（发命令、发地址、读 N 字节、等待就绪、SPI 收发……）发下去，FPGA 按顺序执行，并按顺序返回结果。芯片识别、坏块、ECC、读写流程全部在上位机完成，所以**支持新芯片只需要改上位机，不用重烧 FPGA**。

## 1. 传输层

| 链路 | 说明 |
|---|---|
| UART（板载 BL702 USB 串口） | 8N1，上电默认 115200。可用 `SET_BAUD` 提速（带自动回退）。无硬件流控 |
| FT232H 245 异步 FIFO | 有硬件流控，约 2–3 MB/s |

两条链路可同时连接；FPGA 在**最近收到命令字节的那条链路**上回送结果。

协议是**无帧的字节流**：`操作码 + 参数 [+ 数据]`，结果也是纯字节流，长度由操作决定，上位机据此解析。

## 2. 操作码

多字节整数一律**小端**。`u16` 为 2 字节。

| 码 | 名称 | 参数 | 返回 |
|---|---|---|---|
| `00` | NOP | — | — |
| `01` | ECHO | `b` | `b` |
| `02` | INFO | — | 16 字节，见 §4（读取后清除错误标志） |
| `03` | SET_REG | `reg`, `value:u16` | — |
| `04` | DELAY_US | `us:u16` | — |
| `05` | SET_BAUD | `div:u16`（每位时钟数 = 时钟频率/波特率，时钟见 INFO：固件 ≥ 1.3 为 54 MHz，之前 27 MHz） | `55`（以旧波特率发出），随后切换 |
| `06` | GET_PINS | — | 2 字节：`flags`（bit0 R/B#，bit1 SPI DO，bit2 当前链路 1=FT，bit3 波特率待确认，bit4 FT232H OE# 电平，bit5 SIWU# 电平，bit6 FT232H CLKOUT 在翻转），`nand_io`（NAND 数据总线当前电平） |
| `07` | PIN_TEST | `pin`, `mode` | 3 字节：21 根测试引脚的焊盘电平（小端，bit i = 引脚 i；固件 ≥ 1.2） |
| `10` | NAND_CE | `on`（1=CE# 拉低） | — |
| `11` | NAND_CMD | `c` | — |
| `12` | NAND_ADDR | `n`(1–8), `a[n]` | — |
| `13` | NAND_WRITE | `len:u16`, `data[len]` | — |
| `14` | NAND_READ | `len:u16` | `data[len]` |
| `15` | NAND_WAIT_RB | `timeout_ms:u16` | 1 字节：0=就绪，1=超时 |
| `16` | NAND_POLL_STATUS | `mask`, `value`, `timeout_ms:u16` | 2 字节：`result`(0=满足,1=超时), `status` |
| `20` | SPI_CS | `on`（1=CS# 拉低） | — |
| `21` | SPI_WRITE | `len:u16`, `data[len]` | — |
| `22` | SPI_READ | `len:u16`（MOSI 发 `FF`） | `data[len]` |
| `23` | SPI_XFER | `len:u16`, `data[len]` | `data[len]`（全双工） |
| `24` | SPI_POLL | `n`(1–4), `cmd[n]`, `mask`, `value`, `timeout_ms:u16` | 2 字节：`result`, `last` |
| `25` | SPI_READ4 | `len:u16` | `data[len]`（四线输入，固件 ≥ 1.1） |
| `26` | SPI_WIDE | `len:u16`, `flags`, 写时再跟 `data[len]` | 读时 `data[len]`（双线 / 四线收发，固件 ≥ 1.3） |

说明：
- `NAND_POLL_STATUS`：发一次 `70h`，然后反复读状态直到 `(status & mask) == value` 或超时。不接 R/B# 也能用。读操作中用它等待后，需要再发 `00h` 回到数据输出模式。
- `NAND_WAIT_RB`：先等待 `T_WB` 个周期，再检测 R/B#。
- `SPI_POLL`：循环执行「CS 拉低 → 发送 cmd → 读 1 字节 → CS 拉高」直到满足条件或超时。用于 SPI NOR 的 `05h` 和 SPI NAND 的 `0Fh C0h`。
- `SPI_READ4`：四线读的数据阶段（每个 SCK 读 4 位，IO3..IO0，高半字节在前）。执行时 FPGA 释放 IO0/IO2/IO3，直到下一个 `SPI_CS 0` 才重新驱动。典型用法：`SPI_CS 1` → `SPI_WRITE 6B a2 a1 a0 00`（命令、地址、8 个空时钟）→ `SPI_READ4 n` → `SPI_CS 0`。芯片需先置 QE 位。
- `SPI_WIDE`：`flags` bit1..0 为线宽（0 = 单线，1 = 双线 IO1..IO0，2/3 = 四线 IO3..IO0），bit2 = 读。写：每个 SCK 发出 1/2/4 位（高位在前），数据线只在这个操作期间由 FPGA 驱动。读：FPGA 释放 IO0–IO3 并按线宽采样，直到下一个 `SPI_CS 0` 才重新驱动。用法举例：SPI NOR `EBh`（1-4-4）= `SPI_WRITE EB` → `SPI_WIDE 4 0 02` 发地址和模式字节 `a2 a1 a0 M` → `SPI_WIDE 2 0 02` 发 4 个空时钟 → `SPI_WIDE n 0 06` 读数据；SPI NAND `32h` 四线载入 = `SPI_WRITE 32 c1 c0` → `SPI_WIDE n 0 02` + 数据。芯片需先置 QE 位。
- `NAND_READ` / `NAND_WRITE` / `SPI_READ` / `SPI_READ4` / `SPI_WRITE` / `SPI_WIDE` 按突发方式执行：总线周期首尾相接，中间没有空闲时钟（NAND 最快每字节 2 个时钟 = 27 MB/s @54 MHz，实际受链路限制；SPI 单线每字节 16 个时钟，双线 8 个，四线 4 个；SCK 最高 = 时钟/2）。发送 FIFO 快满时自动暂停。
- `PIN_TEST`：接线诊断。`mode` 0 = 恢复正常；1 = 释放全部 21 根 Flash 引脚（只靠 FPGA 内部上下拉），只读电平；2 / 3 = 把 `pin` 拉低 / 拉高，其余释放；4 = `pin` 以 2 Hz 翻转（拿万用表或 LED 在座子上找线）。设置后等 10 µs 再采样返回。引脚编号：0–7 NAND IO0–7，8 CLE，9 ALE，10 WE#，11 RE#，12 CE#，13 WP#，14 R/B#，15 SPI CS#，16 SCK，17 IO0/DI，18 IO1/DO，19 IO2，20 IO3。模式一直保持到再次发送 `PIN_TEST x 0` 或复位。
- `timeout_ms = 0` 表示只检测一次。
- 未知操作码被当作 1 字节 NOP 跳过，并置错误标志。

## 3. 寄存器（SET_REG）

时序单位均为 FPGA 时钟周期，值为 0 时按 1 处理。固件 1.3 起时钟由 rPLL 倍频到 54 MHz（18.5 ns），之前是 27 MHz（37 ns）；上位机按 INFO 里的时钟频率把纳秒换算成周期数。下表“默认”是 27 MHz 下的值，54 MHz 固件复位后是它的 2 倍（SPI_DIV 为 7），时间不变。

| reg | 名称 | 默认 | 含义 |
|---|---|---|---|
| 0 | T_SETUP | 2 | CLE/ALE/数据 建立时间（WE# 下降前） |
| 1 | T_WP | 3 | WE# 低电平宽度 |
| 2 | T_WH | 2 | WE# 高电平 / 保持时间 |
| 3 | T_RP | 3 | RE# 低电平宽度（最后一个周期采样） |
| 4 | T_REH | 2 | RE# 高电平宽度 |
| 5 | T_WHR | 6 | WE# 上升到 RE# 下降的最小间隔 |
| 6 | T_ADL | 8 | 最后一个地址周期到第一个数据写入的最小间隔 |
| 7 | T_WB | 6 | 检测 R/B# 前的等待 |
| 8 | SPI_DIV | 3 | SCK 半周期 = (div+1) 个时钟；默认 3.375 MHz，0 → 时钟/2（54 MHz 固件 27 MHz） |
| 9 | PIN_CTRL | `0x06` | bit0 NAND WP# 电平（0=写保护，**默认保护**）；bit1 SPI IO2(WP#)；bit2 SPI IO3(HOLD#)；bit3 NAND 引脚全部高阻；bit4 SPI 引脚全部高阻；bit5 SPI 在 SCK 下降沿前采样 |
| 10 | FT_PHASE | 0 | 同步 FIFO 时钟相位（固件 ≥ 1.3）：FT232H CLKOUT 经 PLL 后延迟 value × 22.5°（60 MHz 下每步 1.04 ns）。**需要确认**：写入后 1 秒内必须收到 INFO，否则自动退回上一个已确认的相位并置 INFO 标志 bit4。`nsprog ft232h-tune` 扫描全部 16 个相位并取可用窗口的中点 |

## 4. INFO 返回（16 字节）

| 偏移 | 内容 |
|---|---|
| 0–3 | 魔数 `"NSPG"` |
| 4 | 协议版本（1） |
| 5–6 | 固件版本 major, minor |
| 7 | 板卡 ID（1 = Tang Nano 9K） |
| 8–11 | 时钟频率 Hz（u32） |
| 12 | 接收 FIFO 大小 log2（12 → 4096 字节） |
| 13 | 能力位：bit0 NAND8，bit1 SPI，bit2 SPI_WIDE，bit3 FT245，bit4 UART，bit5 SPI_READ4，bit6 FT232H 245 同步 FIFO，bit7 PIN_TEST |
| 14 | 错误标志：bit0 未知操作码/参数错，bit1 字节间超时中止，bit2 UART 接收溢出，bit3 波特率回退，bit4 FT_PHASE 回退 |
| 15 | 当前链路（0=UART，1=FT232H，异步或同步） |

## 5. 可靠性规则

- **字节间超时**：某个操作的参数或数据还没收完，却超过 100 ms 没有新字节到达，FPGA 放弃该操作，拉高 CE#/CS#，回到等待操作码状态。
- **重新同步**（上位机）：停止发送 → 丢弃输入直到静默 150 ms → 发送 `ECHO 5A`、`ECHO A5`、`INFO` → 在返回流中找到 `5A A5 "NSPG"`。
- **流控窗口**：UART 没有流控，上位机保证“已发送但尚未被 FPGA 消费”的字节数不超过接收 FIFO 大小减去余量（默认窗口 3584 字节）。做法：把操作流按操作边界切成段，每段末尾加一个 `ECHO` 标记；收到标记即代表该段已被消费。
- **相位切换**（FT_PHASE）：与波特率切换同理，新相位不可用时 FPGA 1 秒后自己退回。上位机扫描时每个相位先用 ECHO 图样测试，全部正确才发 INFO 确认；失败则等 1.3 秒再重新同步。
- **波特率切换**：`SET_BAUD` 返回 `55` 后切换；FPGA 在 1 秒内必须收到一个完整的 `ECHO` 或 `INFO`，否则自动回到 115200。上位机依次尝试 3M → 1.5M → 1M → 460800，失败即回退。

## 6. 安全默认值

上电后：CE#/CS#/WE#/RE# 为高，CLE/ALE 为低，**NAND WP# 为低（写保护）**。上位机只在编程或擦除期间拉高 WP#，结束后恢复。
