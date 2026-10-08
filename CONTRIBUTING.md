# Contributing to Cyrene Plugins / 为 Cyrene Plugins 贡献

1. Keep Product semantics and Platform lifecycle authority outside this repository.
2. Treat the versioned contracts under `contracts/` as the public compatibility boundary.
3. Every independently packaged Plugin must declare an SPDX license expression or include a local license file.
4. Preserve protected protocol research documents and their recorded digests unless a dedicated review approves a revision.
5. Never commit credentials, private endpoints, tenant data, model weights, datasets, or developer-specific absolute paths.
6. Do not commit generated build output, caches, virtual environments, or test reports.
7. Keep configured runtimes fail-closed when a binding, endpoint, authentication fact, or required dependency is unavailable.
8. Update tests and `source-manifest.json` through the clean export process; do not copy private Git history into this repository.

贡献必须遵守 Product、Platform 与 Plugin 的权威边界；缺少 binding、endpoint、认证事实
或必要依赖时必须 fail-closed。不得把私有 Git 历史、凭据、私有 endpoint、租户数据、
模型权重、数据集或生成构建物带入公开仓库。

The authoritative checks are defined in `.github/workflows/public-ci.yml`.
At minimum, run the focused checks for the languages and contracts changed,
plus:

```bash
python3 tools/ci/verify_source_manifest.py --root . --allow-git-metadata
python3 tools/ci/check_public_repository.py
```

Changes merge through normal review and must pass exact-SHA hosted CI. An
unrun, skipped, or simulated check is not a pass.

变更必须通过正常评审合并，并以 exact-SHA hosted CI 为准；未运行、跳过或模拟检查均不
算通过。

## Task delivery and cleanup / 任务交付与清理

Read the repository-root [`AGENTS.md`](AGENTS.md) before starting work. It applies to human contributors and every coding agent. The shared task lifecycle policy and guarded cleanup procedure are maintained in [Cyrene-Workspace `docs/TASK_LIFECYCLE.md`](https://github.com/DoHorizon-AI/Cyrene-Workspace/blob/develop/docs/TASK_LIFECYCLE.md). Inspect live refs, open pull requests, worktrees, active work, and working-tree state before editing, and preserve work you do not own. Deliver through the repository's normal pull-request and integration path, then confirm the merged commit from the remote. After merge, clean only that task's temporary worktree and task branches through the guarded procedure in `AGENTS.md`; preserve open pull requests, unmerged or dirty work, active worktrees, protected refs, and private state.

开始任务前请先阅读仓库根目录 [`AGENTS.md`](AGENTS.md)，该要求适用于人类贡献者和所有 coding agent。共享任务生命周期政策与受保护的清理流程维护在 [Cyrene-Workspace 的 `docs/TASK_LIFECYCLE.md`](https://github.com/DoHorizon-AI/Cyrene-Workspace/blob/develop/docs/TASK_LIFECYCLE.md)。修改前先检查远端分支、开放 PR、worktree、活跃任务和工作区状态，并保留不属于自己的工作。通过仓库正常 PR 和集成流程交付，再从远端确认合并提交。合并后按 `AGENTS.md` 的受保护流程，只清理本任务的临时 worktree 和任务分支；保留开放 PR、未合并或未提交的工作、活跃 worktree、受保护引用及私有状态。
