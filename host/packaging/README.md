# 打包

```bash
pip install "./host[fs]" pyinstaller pillow
python host/packaging/build_app.py              # 单文件程序 dist/nsprog-<系统>-<架构>[.exe]
python host/packaging/build_app.py --app        # macOS：dist/nsprog.app + dist/nsprog-<版本>-macos-<架构>.dmg
python host/packaging/build_app.py --installer  # Windows：dist/nsprog-<版本>-windows-setup.exe（需要 Inno Setup 6 的 iscc）
```

每种产物打包后都会用软件模拟器跑一遍 `selftest`、`info`、`doctor` 冒烟测试。GitHub Actions 的 `apps` 工作流在三个系统上都构建；打 `v*` 标签，或者手动运行时填写 `release_tag`，会把产物附到 Release 上。

| 文件 | 作用 |
|---|---|
| `entry.py` | 程序入口。没有参数（双击打开）时启动网页界面并设置 `NSPROG_APP=1`：设置页出现“退出程序”，重复双击只会重新打开浏览器页面。没有控制台时日志写到 `~/.nsprog/nsprog.log` |
| `icon.py` | 用 Pillow 画图标，生成 `nsprog.png` / `.icns` / `.ico` |
| `nsprog.iss` | Inno Setup 脚本：按用户安装（不需要管理员），开始菜单和桌面快捷方式，可选加入 PATH |

## macOS 签名与公证

没有配置证书时，`.app` 和 `.dmg` 只做 ad-hoc 签名（`codesign --sign -`）。Apple Silicon 上可以正常运行，但 Gatekeeper 会拦：用户第一次要**右键 → 打开**，或者执行 `xattr -dr com.apple.quarantine /Applications/nsprog.app`。

要让用户双击直接打开，需要 Apple 开发者账号（每年 99 美元），并在仓库的 Settings → Secrets and variables → Actions 里配置：

| Secret | 内容 |
|---|---|
| `MACOS_CERT_P12` | “Developer ID Application” 证书连同私钥导出的 .p12，再做 `base64` 编码 |
| `MACOS_CERT_PASSWORD` | 导出 .p12 时设的密码 |
| `MACOS_SIGN_IDENTITY` | 签名身份，例如 `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID` | 用于公证的 Apple ID |
| `APPLE_TEAM_ID` | 10 位 Team ID |
| `APPLE_APP_PASSWORD` | 在 appleid.apple.com 生成的 App 专用密码 |

配置了 `MACOS_SIGN_IDENTITY` 时，`build_app.py --app` 用 hardened runtime 签名 .app 和 .dmg。三个 `APPLE_*` 也都配置了时，还会用 `notarytool` 提交公证，再用 `stapler` 把公证票据钉到 .dmg 上。本地构建时设置同名环境变量即可，证书放在登录钥匙串里就行，不需要 `MACOS_CERT_*`。

## Windows

安装包和 exe 都没有签名，SmartScreen 会提示“Windows 已保护你的电脑”，点“更多信息 → 仍要运行”即可。以后如果要签名，可以在 `build_windows_installer()` 里对单文件 exe 和生成的安装包调用 `signtool sign`，或者在 `nsprog.iss` 里配置 `SignTool=`。

FT232H 在 Windows 上需要用 Zadig 把驱动换成 WinUSB，见 [docs/quickstart.md](../../docs/quickstart.md)。
