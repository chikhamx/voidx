# Voidx 发布流程

> **维护者**: 每次发版前按此清单操作，避免遗漏版本号。

## 1. 版本号文件清单

发版时需同步修改以下 **5 个文件** 中的版本号：

| # | 文件 | 字段/位置 | 说明 |
|---|------|----------|------|
| 1 | `pyproject.toml` | `version = "X.Y.Z"` | Python 包元数据，构建入口 |
| 2 | `src/voidx/__init__.py` | `__version__ = "X.Y.Z"` | 运行时版本，`voidx --version` 读取 |
| 3 | `npm/package.json` | `"version": "X.Y.Z"` | npm 包元数据 |
| 4 | `scripts/install.sh` | `VERSION="${VOIDX_VERSION:-X.Y.Z}"` | Bash 安装脚本默认版本 |
| 5 | `scripts/install.ps1` | `$Version = ... else { "X.Y.Z" }` | PowerShell 安装脚本默认版本 |

## 2. 发版步骤

```bash
# ① 修改版本号（5 个文件）
# ② 构建验证
rm -rf build dist
.venv/bin/python scripts/package.py

# ③ 运行测试
.venv/bin/python -m pytest tests/ -x -q

# ④ 提交 & 打 tag
git add -A
git commit -m "chore: bump version to X.Y.Z"
git tag vX.Y.Z
git push && git push origin vX.Y.Z

# ⑤ 发布 PyPI
.venv/bin/python -m twine upload dist/voidx-X.Y.Z-py3-none-any.whl

# ⑥ 发布 npm
cd npm && npm publish

# ⑦ 验证
.venv/bin/python -m pip index versions voidx
npm view @chikhamx/voidx version
```

## 3. 版本号规则

- **Patch** (`X.Y.Z+1`): bug 修复、小改进
- **Minor** (`X.Y+1.0`): 新功能、新技能、新工具
- **Major** (`X+1.0.0`): 架构变更、不兼容改动

## 4. 常见遗漏

| 遗漏 | 后果 |
|------|------|
| 只改 `pyproject.toml` 没改 `__init__.py` | `voidx --version` 显示旧版本 |
| 只改 Python 没改 `npm/package.json` | `scripts/package.py` 构建报错（版本不一致校验） |
| 只改包文件没改安装脚本 | 新用户 `curl | bash` 安装到旧版本 |
