# CodePilot 独立二进制 CI/CD 发布方案

维护日期：2026-05-31

本文固化"让用户无需安装 Python 即可使用 CodePilot"的待做事项。当前项目通过 PyInstaller 将 Python 运行时 + 所有依赖打包成独立 `.exe`（`codepilot binary build`），但缺少自动化构建和公开下载渠道。

## 背景

- 项目 `requires-python = ">=3.10"`，pip 安装版依赖用户自行安装 Python。
- `codepilot binary build` 已能产出零依赖独立二进制（自带 Python 解释器）。
- `codepilot binary release` 已能生成 zip + SHA256 发布包。
- 当前缺少：CI 自动构建 → Release 自动发布 → 用户一键下载安装的完整链路。

## 目标

让 Windows / macOS / Linux 用户不需要安装任何运行时环境，下载一个文件就能直接使用 CodePilot。

## 非目标

- 不做包管理器发布（Homebrew、Chocolatey、apt 等），那是后续迭代。
- 不做自动更新（auto-update），先做到手动下载安装。
- 不改变当前 PyInstaller 构建逻辑。

## 阶段一：CI 自动构建

目标：每次推送 tag 或合并到 main 时，GitHub Actions 自动在 Windows、macOS、Linux 三平台上运行 `codepilot binary build`。

涉及工作：

- 编写 `.github/workflows/release.yml`。
- Windows 用 `windows-latest`，macOS 用 `macos-latest`，Linux 用 `ubuntu-latest`。
- 每个平台安装 Python 3.10（用于构建，产物自带运行时）。
- 构建完成后上传产物作为 workflow artifact。

验收标准：

- 推送 `v*` tag 后 CI 自动触发。
- 三个平台均成功产出 `codepilot` / `codepilot.exe` 二进制。
- 产物可从 GitHub Actions Artifacts 下载。

## 阶段二：Release 自动发布

目标：CI 构建完成后自动创建 GitHub Release，附加三平台二进制 + SHA256。

涉及工作：

- 在 `release.yml` 中集成 `softprops/action-gh-release`。
- 调用 `codepilot binary release` 或直接打包 zip。
- 生成 Release Notes（基于 git log 或手动 CHANGELOG）。

验收标准：

- GitHub Release 页面显示三平台下载链接。
- 每个二进制附带 SHA256 校验值。
- Release Notes 包含版本号和变更摘要。

## 阶段三：一键安装脚本

目标：用户通过终端一行命令完成下载和安装。

Windows（PowerShell）：
```powershell
irm https://xxx/install.ps1 | iex
```

macOS / Linux：
```bash
curl -fsSL https://xxx/install.sh | bash
```

涉及工作：

- 编写 `install.ps1`（Windows）和 `install.sh`（macOS/Linux）。
- 脚本自动检测系统架构，下载对应二进制，放到用户 PATH 目录。
- `codepilot binary install --register-path` 已在项目中实现注册 PATH 的逻辑，脚本可复用。

验收标准：

- 脚本在干净系统上执行后，终端可直接运行 `codepilot --version`。
- 脚本对网络错误、权限不足等情况给出可读提示。

## 阶段四：兼容性验证矩阵（Python 版本无关性保障）

目标：确保独立二进制在各种环境下行为一致，且不被 Python 版本兼容性问题（如 `tomllib` 缺失）影响。

说明：PyInstaller 打包时已包含 `tomli` 依赖，即使 Python < 3.11 也能正常解析 TOML。这是因为 `pyproject.toml` 已声明 `tomli>=2.0.1; python_version < '3.11'`，PyInstaller 的 `collect_all('codepilot')` 会自动收集安装的 `tomli` 包。但以下保障措施仍建议落实：

- CI 构建矩阵中包含 Python 3.10（最低支持版本）和 Python 3.12。
- 烟雾测试：二进制构建后在 CI 中运行 `codepilot config sync`、`codepilot doctor` 等核心命令。
- 新增静态检查：扫描源码中裸 `import tomllib` 无 fallback 的情况（本次已手动修复 13 个文件，但建议加入 lint 规则防止回归）。

验收标准：

- 所有平台的产物在 CI 中通过核心命令烟雾测试。
- 新增 lint 规则能检测缺少 fallback 的 `import tomllib`。

## 风险与注意事项

- PyInstaller 构建的二进制体积较大（~30-80 MB），下载需要时间。
- macOS 二进制需要 Apple 公证（notarization）才能无警告运行，短期可跳过。
- Windows 二进制可能被 Defender 误报，需要提交误报申诉或购买代码签名证书。
- 构建用的 Python 版本决定了打包的运行时版本，建议最低版本（3.10）构建以最大化兼容性。

## 后续迭代（不在本次范围）

- 包管理器发布（Homebrew、Chocolatey、scoop、apt）。
- 自动更新检查（`codepilot update`）。
- 增量更新 / delta 补丁。
- Apple 公证和 Windows 代码签名。
