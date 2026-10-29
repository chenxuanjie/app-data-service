# 公开发布通知（P2）

2026-10-29，源码阶段，未构建新镜像或部署 NAS。通知用于运行中的应用自动发现发布；手机/Windows 系统通知栏推送属于后续 P4。

## 事务与公开字段

迁移 `0017_publication_versions` 在 `0016_release_architecture` 之后新增共用核心表 `core.publication_versions`。每个应用和发布目标只保留一个最新水位；字段仅有 `id`、`app_id`、`topic`、`revision`、`channel`、`platform`、`architecture`，不包含安装包、私有来源记录、凭据或用户数据。Android 通用包沿用 P1 修复后的 `any`，不重新引入 `universal`。

- 平台 `core.public_releases` 的 INSERT 触发器在同一事务更新 `release` 水位，revision 为构建号。上传对象、发布校验失败、幂等重试与回滚不会产生新版本通知。该机制适用于所有应用，无桌游名称分支。
- 桌游模块在统一资料发布最后更新 `library_head` 时，同一事务更新 `catalog` 水位。草稿、导入原文或复制文件不通知。模块只负责声明自己的发布时点，资料差异仍由 `/catalog/changes` 提供。
- 迁移回填已有资料和安装包最新水位，不需要重新导入或重新发布。安装包表原有 FORCE RLS 在事务提交前恢复；运行角色只读通知表，没有写入或调用触发器函数权限。

数据库 WAL 只在事务提交后进入 PowerSync，不需要独立广播进程，也不会出现 SQL 已提交但通知进程崩溃导致永久漏发。这里只承诺版本收敛；未在线时发生多次发布，客户端补齐最终水位，不要求逐条弹出所有历史通知。

## 公共同步与客户端

统一规则增加 `public_publication_versions`：同时满足 JWT `sync_scope=public` 和行 `app_id=auth.parameter('app_id')` 才订阅。客户端伪造请求参数或显式订阅不能跨应用、越权读私人流。复用现有游客 300 秒公共同步凭据，不建立升级账号，不持有数据库账号。

桌游客户端在原公共 PowerSync 数据库中增加只读本地表，不清空原缓存。启动、重连、回前台补读最新水位；通知合并后读取现有资料/更新 API。社区、资料和程序读取独立执行，失败保留缓存并最多追加五次指数退避；新事件或重新连接可再试。隐藏/后台时停止连接和刷新计时器。程序通知需通过实际平台更新检查，只在应用消息中心提示，不自动下载或安装。

## 部署与验证

1. 构建包含本阶段迁移和模块配置的新 API 镜像。
2. 使用同一次交付生成的 Compose：现有 `tools/build_docker_bundle.py` 会嵌入 `tools/render_sync_config.py` 生成的新规则，保留数据库密码、签名密钥、挂载与其他应用配置。
3. 更新 API 后应用配套 PowerSync 配置；PostgreSQL、PowerSync 镜像版本本身不必升级。新规则不能先连接尚无新表的旧数据库。更新原项目并保留数据卷。
4. 新 APK/EXE 匿名打开，在后台通过正式发布工具提交资料或安装包；资料自动补齐元数据，适用程序版本出现应用内通知。离线时发布后再连接，应补齐当前版本；重复重连不重复提示同一版本。

恢复 P2 并衔接 P1 后，本机使用真实 PostgreSQL 运行完整 pytest（334 项通过），ruff 检查和格式检查通过，Alembic 唯一迁移头为 `0017_publication_versions`。固定 PowerSync 1.27.0 的隔离实例再次验证匿名快照、事务水位 WAL 收敛、重连和跨应用隔离。探针 `tools/run_sync_probe.py --docker wsl --application-scope-only` 仅操作本轮随机测试库、容器和复制槽，不访问 NAS。客户端分析和 572 项回归通过，记录及尚未覆盖的现场验收见桌游工程 `docs/public-updates.md`。本轮不构建、上传或部署产物。
