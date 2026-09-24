# 04 实物调试

接线以 [wiring.md](wiring.md) 为准，上手步骤见 [quickstart.md](quickstart.md)。本文是出问题时的深入排查手册。

## 1. 分层排查顺序

1. **FPGA 在不在运行**：LED0 闪烁。不闪 → 重新执行 `nsprog fpga-flash`，然后按复位。
2. **链路通不通**：`nsprog info` 能打印 gateware 版本，`nsprog selftest` 不报错。
3. **接线对不对**：不放芯片时运行 `nsprog doctor`（短路、空闲电平），再用 `nsprog doctor --probe` 在座子上逐脚碰一遍（断线）。单根线可以用 `nsprog pintest --pin 名称 --mode toggle` 让它 2 Hz 翻转，用万用表或 LED 看。
4. **芯片能否识别**：`nsprog info`。
5. **读取是否稳定**：读两遍比较（`read` 之后 `verify --oob`）。
6. **最后才写**。

## 2. 逻辑分析仪（可选）

不是必需品。第 1 节的步骤走完还查不出原因时才用得到。

用 16 通道的（如正点原子 DL16，ATK-Logic 软件）可以把并口 NAND 的 15 根线（IO0–IO7 + CE#、CLE、ALE、WE#、RE#、WP#、R/B#）一次全接上，用“并行总线”解码器直接看出命令、地址和数据。

只有 8 通道时建议这样接：

| 通道 | 并口 NAND | SPI |
|---|---|---|
| CH0 | CE#（FPGA 74） | CS#（39） |
| CH1 | CLE（70） | CLK（63） |
| CH2 | ALE（71） | DI（75） |
| CH3 | WE#（72） | DO（77） |
| CH4 | RE#（73） | — |
| CH5 | R/B#（49） | — |
| CH6 | IO0（25） | — |
| CH7 | IO1（26） | — |

默认时序下，NAND 每个 WE#/RE# 脉冲约 111 ns，24 MHz 采样能看清楚。SPI 观察时先用 `--spi-mhz 1`。

读 ID 的正确波形：CE# 拉低 → CLE 期间一个 WE# 脉冲（90h）→ ALE 期间一个 WE# 脉冲（00h）→ 若干个 RE# 脉冲读出 ID。

## 3. 调整时序

上位机默认使用 FPGA 的保守时序（约 ONFI 模式 0）。需要更慢时，可以在 Python 里改寄存器（见 [protocol.md](protocol.md) §3）：

```python
from nsprog.device import connect
from nsprog import protocol as P
dev = connect()
b = P.Batch()
b.set_reg(P.REG_T_WP, 6); b.set_reg(P.REG_T_RP, 6)   # WE#/RE# 低电平各约 222 ns
dev.run(b)
```

## 4. 常见现象对照

见 [quickstart.md](quickstart.md) 第 9 节。
