# Git Flow

本仓库按 Git Flow 开发，使用普通 Git 命令即可，不依赖额外扩展。

| 分支 | 用途 |
| --- | --- |
| main | 仓库初始化基线和已验收发布；当前尚无生产版本 |
| develop | 已完成阶段的集成分支 |
| feature/* | 从 develop 创建的功能分支，完成验证后以 --no-ff 合入 develop |
| release/* | 从 develop 创建的发布候选，验收后合入 main 和 develop，打 vX.Y.Z 标签 |
| hotfix/* | 从 main 创建的线上修复，完成后合入 main 和 develop |

当前数据库基础使用 feature/postgres-foundation；下一阶段使用
feature/powersync-sync。阶段完成不等同于上线，不为实验代码创建发布标签。
main 的初始化提交只是 Git 历史起点，不代表业务服务可用于生产。

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
