# Git Flow

本仓库按 Git Flow 开发，使用普通 Git 命令即可，不依赖额外扩展。

| 分支 | 用途 |
| --- | --- |
| main | 已验收发布；本轮收尾版本为 v0.3.0 |
| develop | 已完成阶段的集成分支 |
| feature/* | 从 develop 创建的功能分支，完成验证后以 --no-ff 合入 develop |
| release/* | 从 develop 创建的发布候选，验收后合入 main 和 develop，打 vX.Y.Z 标签 |
| hotfix/* | 从 main 创建的线上修复，完成后合入 main 和 develop |

数据库基础阶段使用 feature/postgres-foundation，最小同步阶段使用 feature/powersync-sync，
全业务阶段使用 feature/multi-entity-sync。2026-10-22 用户确认全业务版本验收通过，
按 release/0.3.0 收尾，详见 [验收记录](releases/0.3.0.md)。
阶段完成不等同于上线，不为未验收实验代码创建发布标签。

常规命令：

```powershell
git switch develop
git switch -c feature/功能名
# 修改、检查、按用户要求提交
git switch develop
git merge --no-ff feature/功能名 -m "chore(git): 合并已验证的功能"
```

提交采用中文 Conventional Commits；合并前检查差异、测试结果和工作区。
密钥、数据库卷、开发配置、构建产物不提交。没有远端仓库时只保留本地提交，
不自行创建远端、推送或发布。不要把尚未实测的 DSM 登录或跨端同步记为已完成。

## 提交与镜像来源（2026-10-21）

正式交付顺序为：完成检查 → 确定版本并提交相关改动 → 干净工作区构建 → 产物验证 → 来源校验 → 按授权推送/部署。
测试交付可以来自已提交的 feature 分支；不为构建强制合入 main。正式版本仍按 release 分支验收流程处理。

正式构建使用 [提交后构建与交付](C:/Users/win10/.codex/skills/flutter-release-publisher/references/committed-delivery.md)
中的 source gate 包裹本仓库现有构建脚本，记录本次新 API 分包及配套 Compose。
本地实验可保留 dirty 标识，但不能作为正式交付；提交源码后必须重新构建，不能只改镜像标签。
私有 Compose 只记录摘要，不提交其密码和私钥；凭据与产物均放入已忽略目录。
Git 远端推送和 NAS 部署不因完成本地提交而自动获准。
