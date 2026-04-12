# foo 9.9.9 发布摘要

- 版本: `9.9.9`
- 生成时间: `2026-04-12T18:30:12.649561`
- 产物数量: `1`

## 产物列表

### windows-x86_64

- 二进制: `foo.exe`
- 压缩包: `foo-9.9.9-windows-x86_64.zip`
- 压缩格式: `zip`
- 二进制 SHA256: `51a1f05af85e342e3c849b47d387086476282d5f50dc240c19216d6edfb1eb5a`
- 压缩包 SHA256: `c54ea4aedfda5e66c12749e5551a513ffcc05f6b55d5819ebb9eb46c0e45b46d`

## 交付建议

- 对外分发时优先发送压缩包，不要直接发送裸二进制。
- 分发时附带 `README.zh-CN.md` 和 `SHA256SUMS.txt`。
- 如果接收方是其他 AI 或自动化系统，优先读取 `AI_MANIFEST.json` 和 `AI_USAGE.zh-CN.md`。
- 用户安装后可执行 `codepilot --help` 验证命令是否可用。
