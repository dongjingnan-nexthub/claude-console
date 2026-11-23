# 版本发布指南（1.0 / 2.0）

本指南只列出需要你**亲自在终端执行**的命令。执行 AI 不会自动提交、打标签、
推送或创建 Release。准备完成后，请按顺序逐步执行，每一步成功再继续。

## 已核实的事实

| 项目 | 值 |
|---|---|
| 仓库目录 | `/home/wyj/Applications/claude-console` |
| 远端 | `https://github.com/wyj0508/claude-console.git` |
| 分支 | `main` |
| 1.0 基线提交 | `69cd4b4feee27d8e4dfa94cf31f61f6021729fb6` |
| T1 检查时的远端状态 | main 为上述基线；无标签、无 Release，发布前需重新核对 |
| 2.0 待提交文件 | 见下方 9 个文件，不使用 `git add .` |

1.0 就是现有的 `69cd4b4` 提交，不需要复制源码或另建目录；`v1.0` 标签固定
指向它。2.0 是当前工作区整理后的新提交，`v2.0` 标签指向该提交。`main` 沿
现有历史正常前进，不强制推送、不覆盖或移动既有标签。

## 第 0 步：确认远端与本地状态

```bash
cd /home/wyj/Applications/claude-console

git status --short
git remote -v
git ls-remote --heads --tags origin
git log --oneline -3
```

预期：

- `origin` 为 `https://github.com/wyj0508/claude-console.git`
- `refs/heads/main` 为 `69cd4b4feee27d8e4dfa94cf31f61f6021729fb6`
- 没有 `refs/tags/*`
- `git log` 第一条为 `69cd4b4`

如果远端 `main` 已经不是 `69cd4b4`，或已经存在 `v1.0` / `v2.0` 标签，**先停下**：
检查远端新增内容，确认后再重新验证。不要用强制推送解决分歧。

同名标签若已存在，先比较其解引用后的提交 SHA（远端输出的 `^{}` 行）：
目标一致则跳过对应标签的创建，目标不同则停止，不覆盖标签。
另用 `git branch --show-current` 确认当前分支为 `main`。

## 第 1 步：审查将要提交的差异

```bash
git diff --check
git diff --stat
git diff -- README.md main.py
```

预期 `README.md`、`main.py` 有改动，`git diff --check` 无输出（无空白错误）。

## 第 2 步：只暂存这 9 个文件

```bash
git add -- \
  main.py \
  README.md \
  dual_ai.py \
  docs/dual-ai-protocol.md \
  tests/test_dual_ai.py \
  tests/test_dual_protocol.py \
  tests/test_dual_ui.py \
  CHANGELOG.md \
  docs/releasing.md

git diff --cached --stat
git diff --cached --check
git status --short
```

预期：暂存区恰好 9 个文件；工作区没有遗漏的相关源码/文档/测试。`vendor/`、
`__pycache__/`、`*.bak*`、`.agents/`、`.aws/`、`.codex/` 已被 `.gitignore`
排除，不会进入提交。

若 `git status --short` 仍显示其他未跟踪的相关文件，先判断是否属于 2.0，
不要直接提交无关文件，也不要清理用户数据。

## 第 3 步：提交 2.0

先确认当前候选已完成测试与启动验收，且验证后文件未再变化；未通过时不要发布。
暂存区若有这 9 个文件以外的内容，先停止核对。提交前运行
`git diff --cached` 审查全部内容（包括新增文件），并检查
`git diff --name-only` 无输出、`git ls-files --others --exclude-standard` 无遗漏。

```bash
git commit -m "2.0：双 AI 协作与进度/额度/流式刷新修复"

git log --oneline -3
git rev-parse HEAD
```

记下新的提交 SHA（下称 `<2.0-SHA>`）。

## 第 4 步：创建两个版本标签

```bash
git tag -a v1.0 69cd4b4feee27d8e4dfa94cf31f61f6021729fb6 \
  -m "1.0：单 AI 版本（原版）"

git tag -a v2.0 HEAD \
  -m "2.0：双 AI 协作版本"

git tag -n
git rev-parse v1.0^{} v2.0^{}
```

预期：

- `v1.0^{}` 为 `69cd4b4feee27d8e4dfa94cf31f61f6021729fb6`
- `v2.0^{}` 为 `<2.0-SHA>`

标签必须指向不同提交：`v1.0` 保留原版，`v2.0` 包含当前完整功能。

## 第 5 步：推送 `main` 和两个标签

```bash
git push origin main
git push origin v1.0 v2.0
```

不要使用 `--force` / `-f`。如果推送被拒绝，先执行
`git fetch origin && git log --oneline --decorate -5` 查看差异，再决定处理方式。

## 第 6 步：核对远端结果

```bash
git ls-remote --heads --tags origin
git rev-parse HEAD
```

预期 `refs/heads/main` 为 `<2.0-SHA>`，两个附注标签均已存在。
`refs/tags/v1.0`、`refs/tags/v2.0` 行是标签对象 SHA，不能直接与提交 SHA 比较；
应核对 `refs/tags/v1.0^{}` 为 `69cd4b4feee27d8e4dfa94cf31f61f6021729fb6`，
`refs/tags/v2.0^{}` 为 `<2.0-SHA>`。

也可以打开：

- 标签页：`https://github.com/wyj0508/claude-console/tags`
- 提交页：`https://github.com/wyj0508/claude-console/commits/main`

在标签页点 `v1.0` / `v2.0` 的「Browse files」，确认 1.0 没有 `dual_ai.py`、
`docs/`、`tests/`，2.0 包含它们。

## 可选：创建 GitHub Release

本机当前未安装 `gh`。可以在网页操作，或先安装并登录 `gh`。

### 方式 A：网页

1. 打开 `https://github.com/wyj0508/claude-console/releases/new`
2. 先选择标签 `v1.0`，标题 `1.0 · 单 AI 版本（原版）`，粘贴下方
   「1.0 发布说明」，取消设为最新版本后发布。
3. 再选择标签 `v2.0`，标题 `2.0 · 双 AI 协作版本`，粘贴「2.0 发布说明」，
   勾选设为最新版本后发布。若 Release 已存在，先核对内容，不重复创建。

### 方式 B：安装 `gh` 后

```bash
sudo apt install gh
gh auth login
```

然后执行（先发布 1.0，再发布 2.0）：

```bash
gh release create v1.0 \
  --verify-tag --latest=false \
  --title "1.0 · 单 AI 版本（原版）" \
  --notes "原版单 AI 版本，包含界面内对话、项目研读、代码开发、统一工作历史、图片 OCR 等能力。此标签固定指向 69cd4b4，未被 2.0 覆盖。"

gh release create v2.0 \
  --verify-tag --latest \
  --title "2.0 · 双 AI 协作版本" \
  --notes-file CHANGELOG.md
```

## 2.0 发布说明（可粘贴）

```markdown
## 2.0 · 双 AI 协作版本

在 1.0 单 AI 基础上加入双 AI 协作，并修复协作运行时的进度、额度和流式渲染问题。

### 新增
- 主 AI / 执行 AI 协作，模型可来自 Claude 或 Codex，支持交换主从
- L0–L4 按难度分工档次，默认 L2 中高档·主从配合
- 交接协议 v3：压缩指令、DUAL_ASK 询问、共享状态卡与本地运行记录
- 协作模式双模型额度显示，运行中可刷新
- 进度浮窗显示协作档次、角色、任务 ID、工具次数和最近活动

### 修复
- 协作运行时进度查询误报「没有运行中的任务」
- 运行中额度刷新按钮被禁用、查询可能停在「查询中…」（25 秒超时恢复）
- 高频流式输出导致界面卡顿（改为 80ms 批量刷新）

### 兼容
- 单模型对话、项目研读、代码开发、统一工作历史等功能保留
- 配置路径、run.sh、requirements.txt 不变
```

## 1.0 发布说明（可粘贴）

```markdown
## 1.0 · 单 AI 版本（原版）

单 AI 模型工作模式的完整版本，包含界面内对话、图片 OCR、项目研读与学习、
代码开发、统一工作历史、权限选择、模型与思考强度切换等能力。

本版本保留为历史版本，后续的 2.0 不会覆盖其内容。
```

## 发布后再核对

```bash
git ls-remote --tags origin
git tag -n
```

如果标签或提交与预期不符，先停下核对；不要删除或移动远端标签，也不要强制
推送。需要修正时，先确认远端实际内容，再决定新建标签或另行提交。
