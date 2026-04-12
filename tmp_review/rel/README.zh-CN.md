# foo 9.9.9 发布说明

这个目录用于分发已经构建好的 CodePilot 二进制文件。

## 包含内容

- `release.json`: 发布元数据清单
- `SHA256SUMS.txt`: 所有二进制和压缩包的校验值
- `AI_MANIFEST.json`: 给其他 AI 的机器可读命令清单
- `AI_USAGE.zh-CN.md`: 给其他 AI 的 Markdown 调用手册
- `<platform>/`: 平台对应的原始二进制和安装脚本
- Windows: `codepilot-<version>-<platform>.zip`
- Linux: `codepilot-<version>-<platform>.tar.gz`

## 安装方式

### Windows

1. 解压 `codepilot-<version>-windows-x86_64.zip`
2. 双击运行 `install-codepilot.cmd`
3. 重新打开终端后执行 `codepilot --help` 验证

### Linux

1. 解压 `codepilot-<version>-linux-x86_64.tar.gz`
2. 执行 `chmod +x install-codepilot.sh codepilot`
3. 执行 `./install-codepilot.sh`
4. 重新打开终端后执行 `codepilot --help` 验证

## 当前包含的平台

- `windows-x86_64` -> `foo.exe`

## 说明

- Windows 和 Linux 需要分别在各自系统上原生构建，不能直接交叉复用。
- 安装脚本默认安装到用户目录，不需要管理员权限。
