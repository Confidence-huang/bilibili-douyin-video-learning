# 发布流程

本仓库 `main` 的历史是线性的、带必需检查。**所有改动（包括纯文档）都通过 PR 进入**，不要直推 `main`。

这份文档存在的原因：分支保护里 `enforce_admins=false`，管理员技术上能直推 —— 曾经因为"推得上去"而绕过 PR，
所以把流程写死在仓库里，而不是靠记忆。

## 硬约束（来自 `main` 的分支保护）

| 规则 | 值 | 含义 |
|---|---|---|
| `required_status_checks` | `ubuntu-source-and-offline-tests`、`windows-source-and-offline-tests`，`strict=true` | 两个检查都必须通过；分支还需先与最新 `main` 同步 |
| `required_linear_history` | `true` | 只能 rebase / squash 合并，**不能**产生 merge commit |
| `allow_force_pushes` | `false` | 已推送的分支不能强推 |
| `allow_deletions` | `false` | `main` 不能被删 |
| `enforce_admins` | `false` | 管理员技术上能直推 —— **不要这么做** |

## 一次改动的标准流程

```bash
git switch main && git pull --ff-only
git switch -c codex/<topic>            # 历史惯例：codex/ 前缀
# … 改动 + 本地验证（见下）…
git add <本轮明确文件>                   # 不要 git add . / -A
git commit -m "<type>(<scope>): <说明>"
git push -u origin codex/<topic>
gh pr create --base main --fill
gh pr checks --watch                   # 等两个必需检查
gh pr merge --rebase --delete-branch   # 线性历史：不用 merge commit
```

合并之后：

```bash
git switch main && git pull --ff-only
git tag vX.Y.Z && git push origin vX.Y.Z
gh release create vX.Y.Z --notes-file <notes.md>
```

**tag 一定要在合并之后打** —— 它必须落在 `main` 上被检查过的那个提交，而不是分支上的草稿。

## 发版专用约定

- **版本号四处锁步**：`pyproject.toml`、`agent-harness/setup.py`、
  `agent-harness/cli_anything/video_learning/__init__.py`、`uv.lock`。
  `tools/validate_repository.py` 会拒绝不一致，漏一处就报错。
- **`CHANGELOG.md` 顶部加一节**，标题格式 `## X.Y.Z - YYYY-MM-DD`，逐条写清问题、根因、改法。
- **README 的离线用例数**必须与 CHANGELOG 里那句一致 —— 跑一次测试拿真实数字，不要照抄上一版。
- **纯文档改动也走同一流程**，可以单独开 PR；它与发版是两件事，不要为了省一次 PR 而直接推 `main`。
- Release 正文至少包含：改了什么、验证命令与输出、`vX.Y.Z-1...vX.Y.Z` 对比链接。
- 版本号语义与既往一致：`feat` 走 minor，纯 `fix` 走 patch，纯文档走 patch。

## 发布前本地验证

```bash
# 仓库边界、隐私、版本锁步、宿主清单（CI 同一入口）
python tools/validate_repository.py

# 仓库布局全量测试（Linux）
./verify_linux.sh --skill-root "$PWD/.agents/skills/bilibili-video-learning"

# 仓库布局全量测试（Windows）
pwsh -NoProfile -File .\verify.ps1
```

安装态副本跑 `verify.ps1` 时加 `-SkipTests`：`test_repository_validator.py` 依赖仓库根的 `tools/`，
安装器不随包分发它，全量跑必然挂 —— 这是安装布局的固有边界，不是缺陷（见 CHANGELOG 1.30.3）。

## 回退

- **单次改动**：`git revert <sha>`，照常走 PR 合回。**不要** `reset` / `rebase` `main`。
- **发错版本**：补一个 patch 发布。**不要挪已推送的 tag** —— 那是改写历史。
- **分支写坏了**：直接删分支重开（`allow_force_pushes=false`，别指望强推覆盖）。
