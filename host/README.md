# nsprog（上位机）

Tang Nano 9K NAND / SPI Flash 编程器的 Python 上位机：命令行 `nsprog` 和本地网页界面 `nsprog web`。

```bash
pipx install .          # 或 pip install .
nsprog --help
```

说明文档见仓库根目录的 `docs/quickstart.md`。

模块：

| 模块 | 作用 |
|---|---|
| `protocol.py` | NSP v1 操作码与批处理构建 |
| `device.py` | 连接、分段窗口执行、重新同步、波特率协商 |
| `link.py` | 串口（pyserial）与 FT232H FIFO（pyftdi） |
| `chipdb.py` + `data/` | 芯片库（NANDO CSV + 补充表） |
| `onfi.py` / `sfdp.py` | ONFI 参数页、SFDP 解析 |
| `flash.py` | 并口 NAND、SPI NOR、SPI NAND 驱动与自动识别 |
| `jobs.py` | 读 / 写 / 擦 / 校验 / 查空 / 坏块 |
| `emulator.py` | 软件模拟的编程器与芯片（测试、演示用） |
| `cli.py` | 命令行 |
| `web/` | 本地网页界面（FastAPI + WebSocket） |
| `ft232h.py` | FT232H EEPROM 一次性设置 |
