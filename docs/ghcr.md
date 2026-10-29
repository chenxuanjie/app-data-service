# GitHub Actions 与 GHCR 镜像更新

源仓库：`https://github.com/chenxuanjie/app-data-service`。
自有 API 镜像：`ghcr.io/chenxuanjie/app-data-service`，目标架构 `linux/amd64`。
PostgreSQL 与 PowerSync 继续使用配套 Compose 中已固定的上游版本。

## 自动构建

`.github/workflows/docker-publish.yml` 在 main、develop、feature、release、hotfix
分支推送及 `v*` 标签推送时构建并发布 API 镜像；Actions 页面也可手动运行。
PR 只构建，不登录或推送 GHCR。构建不附带完整业务测试。

工作流使用 GitHub 自动提供的 `GITHUB_TOKEN`，只声明 contents: read、packages: write，
不需要单独保存 GHCR 发布密码。首次运行前确认仓库 Settings → Actions → General
已启用 Actions，并允许本工作流使用的 GitHub/Docker 官方 actions。

现有 Dockerfile 保留离线 wheel 安装模式。工作流在 Python 3.14.2 Linux 容器中按
`requirements-runtime.lock` 下载 wheel，再用 Buildx 构建当前检出的路径上下文；
依赖不升级，运行镜像不包含测试、开发配置或本地 Compose。BuildKit 缓存存于 Actions。

## 镜像标签

- 每次构建都有 `sha-<完整提交号>`，可追溯源码；部署优先选此标签或镜像 digest。
- 分支构建更新 `branch-<分支名>`，例如 `branch-feature-board-game-business`。
- main 构建更新 latest，但不自动覆盖某个版本号标签。
- `v0.5.0` 一类正式标签须匹配 pyproject.toml，发布 `0.5.0` 和 latest。
  未验收功能分支不自动合入 main，也不自动创建正式版本标签。

## 在群晖更新 API

1. 在 GitHub Actions 确认该次构建成功，复制输出中的镜像 digest 或 sha 标签。
2. 首次发布的 GHCR package 可能为私有；若希望群晖匿名拉取，确认 package 的
   Package settings → Change visibility 为 Public。源码仓库公开不等于镜像包自动公开。
3. 使用原 NAS 项目和实际私有 Compose，只将 api.image 换为本次 GHCR 引用。
   若该服务有 `pull_policy: never`，移除或改为 `always`，使远端镜像可以拉取。
4. 在 Container Manager 拉取新镜像，再更新原项目、重建 API 容器；命令行等效为
   `docker compose pull api` 和 `docker compose up -d --no-deps api`。
   不更改数据库/PowerSync 标签、账号、密钥、挂载和项目名，不删除数据卷。
5. API 启动仍执行配套 Compose 规定的迁移及应用注册，核对健康状态和日志。
   数据库迁移需单独考虑兼容性，不能把切回镜像称为回退数据库。

实际私有 Compose 保留在本机忽略目录，不提交 GitHub。本次云端发布不代表 NAS 已更新。

## 固定镜像的本地验收与 Compose 交付

云端构建成功后，拉取 Actions 输出的具体 digest，再用既有容器验证器验收该镜像。
`--smoke-only` 包含启动、非 root 安装包卷权限、上传/SQL 发布、匿名下载摘要、Range
及 API 重启后的持久化；不会自动执行全量业务同步。`--release-artifact` 可指定已有 APK，
测试只上传到隔离容器和临时数据库，不向 NAS 或正式发布服务上传。

```powershell
.venv/Scripts/python.exe tools/verify_stack.py --docker wsl --api-image '<GHCR digest 引用>' --smoke-only --release-artifact '<本机已有 APK>' --report release/ghcr/validation.json
.venv/Scripts/python.exe tools/prepare_ghcr_compose.py --compose .local/p1-release-build-input.yaml --image '<同一 digest 引用>' --validation release/ghcr/validation.json --output release/ghcr/docker-compose.yml --docker wsl
```

正式交付用 source gate 包裹这些本地步骤并记录 Compose、验证报告。准备工具核对镜像的
linux/amd64 架构、源码提交和本地验证记录，仅更换 API 镜像及其拉取策略。实际配置中的
数据库、密钥、资料和安装包挂载保留；旧离线压缩包不作为 GHCR 更新的必要步骤。
