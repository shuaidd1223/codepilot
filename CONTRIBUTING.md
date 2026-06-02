# Contributing to CodePilot

感谢你对 CodePilot 的关注！

## 开发环境

```bash
git clone https://gitee.com/shuai_dd/CodePilot.git
cd CodePilot
pip install -e ".[dev]"
```

## 测试

```bash
# 日常开发（跳过慢速测试）
pytest -q -m "not slow"

# 全量回归
pytest -q
```

## 代码规范

- 使用 ruff 进行格式化和 lint：`ruff check codepilot/`
- 遵循 TDD：先写测试，再实现，最后重构
- 所有改动需通过 CI 检查

## 提交规范

- 使用中文或英文提交信息
- 推荐格式：`type(scope): 描述`
- 类型：feat, fix, refactor, docs, test, chore

## Pull Request 流程

1. 从 `dev` 分支创建特性分支
2. 开发并确保测试通过
3. 提交 PR 到 `dev` 分支
4. CI 自动化检查（lint + test）
5. 代码审查

## 项目结构

详见 `AGENTS.md` 和 `CLAUDE.md`。
