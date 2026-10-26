# 接入一个新应用

2026-10-26。目标是增加该应用自己的实现与迁移，公共认证、会话和其他应用业务无需改动。
这是一套显式安装的模块结构；新增业务仍需编写代码和重新发布 API 镜像。

## 1. 编写模块

在 `src/app_data_service/modules/<app_id>/` 放置业务模型、校验、服务与路由。
导出 `ApplicationDefinition`，声明 ID、名称、schema、个人空间需求及业务能力。
可选声明文件区域、同步流、复制表、router_factory 和 cli_factory。

路由使用 `application_router(app_id)` 绑定应用，路径位于 `/v1/apps/<app_id>/...`。
私人路由使用 `Actor`，在 `user_transaction(engine, actor.user_id, app_id=actor.app_id)` 中访问数据。
不需要登录的公开路由可省略 Actor，模块绑定和启用状态仍会检查。
不要把应用业务条件加进公共 auth/session/API 文件。

## 2. 提供数据库迁移

创建应用 schema、带类型和约束的业务表、必要索引、RLS 和最小权限。
私人数据表至少检查所属应用和 `core.can_access_dataset(dataset_id)`。
只有需要同步的表才授权 app_replication 并加入 powersync publication。
运行角色不能创建 schema、表或修改权限；登录不会替代这一步。

## 3. 登记并检查

将定义加入 `modules/registry.py` 的安装清单。
执行已审查的迁移后，用迁移角色运行 `app-data register-applications`。
注册只写模块元数据，不创建 schema；重复和并发注册均幂等，已有元数据不一致须显式迁移。
服务启动会验证数据库登记和实际 schema，未知或缺少实现的应用不会自动激活。

需要纯身份功能时设置 `personal_data=False`，登录结果的 dataset_id/dataset_kind 为 null。
需要个人数据时平台为每个 `(应用,账号)` 创建或恢复独立主空间，业务表内容仍由模块管理。
不要在另一个应用的表中加一组临时字段代替本应用模型。

## 4. 配置文件、浏览器和同步

把应用的 browser_origin、已声明 file_roots 写入 `APP_DATA_APPLICATIONS`。
多个 Web 应用使用不同 origin；现有网页来源保持稳定以保留浏览器本地数据分区。
只有已声明区域才能配置文件根，客户端不能指定任意 NAS 路径或上游。

需要同步时声明 sync_streams 和 replication_tables。
流按签名应用 ID、账号与数据集权限过滤，不能信任客户端订阅参数作为授权。
运行 `python tools/render_sync_config.py --output deploy/powersync/sync-config.yaml` 更新开发配置；
交付工具也从相同声明生成配置。不要把其他应用的字段、校验和冲突策略当作公共业务协议。

## 5. 客户端与验收

新 Flutter 应用设置 `AppJsonClient(applicationId: '<app_id>', ...)`。
请求 `/v1/auth/login` 等公共后缀会生成 `/v1/apps/<app_id>/auth/login`；文件请求也使用同一作用域。
路径、应用头、令牌身份不一致时拒绝请求。各宿主应用自行保存凭据并隔离本地工作区，
公开资料缓存与私人工作区的生命周期也由宿主应用管理。

至少验证：登录与刷新、重复登录不新建空间、跨应用/跨账号读写拒绝、独立退出、
浏览器 Cookie/CSRF、文件区域与清单应用标识、禁用应用、迁移升级、运行角色无 DDL 权限。
有同步时运行真实 PowerSync 下载验证，不能仅检查生成的 SQL 字符串。
最后重新构建并验证 API 镜像和客户端；更新镜像、数据库与同步配置属于配套上线步骤。

## 已完成的扩展性验证

`tests/notes_application.py` 提供阅读笔记测试模块：独立 typed 表、RLS、业务读写、附件区域、
更新清单和同步流。它只安装到随机测试数据库，不在生产安装清单中，也不是新交付的笔记产品。
`tests/test_application_modules.py` 验证它复用未修改的公共登录与会话；另覆盖纯身份应用。
`tools/run_sync_probe.py --docker native --application-scope-only` 使用该第三模块验证真实同步流隔离。
