# 多应用服务架构

2026-10-26，功能分支源码，尚未构建新镜像或部署。服务使用 FastAPI、PostgreSQL 和 PowerSync；
同一账号可使用多个应用，每个应用拥有独立业务模型和数据空间。

## 目录与责任

| 位置 | 责任 |
| --- | --- |
| `src/app_data_service/api.py` | 组装服务、安装模块与公共中间件；不写应用业务条件分支 |
| `applications.py` / `registration.py` | 模块定义、登记校验、应用配置及管理侧注册 |
| `auth.py` / `sessions.py` / `auth_routes.py` | 签名令牌、可撤销会话与统一 DSM 身份验证 |
| `dependencies.py` | 验证后的账号/应用/会话上下文及固定应用路由 |
| `models.py` / `store.py` | core 共享模型和账号＋应用事务 |
| `platform_routes.py` | 数据空间、能力、文件、更新、同步凭据等公共接口 |
| `modules/registry.py` | 安装清单：显式装配经过审查的模块 |
| `modules/zheri/` | Zheri 的业务表、校验、冲突合并、导入、命令行及 API |
| `modules/board_game_agent/` | 桌游类型化私人表、公开资料、社区命令、统计与同步声明 |
| `tools/render_sync_config.py` | 从安装模块的声明生成统一 PowerSync 配置 |
| `tests/notes_application.py` | 仅测试使用的第三个应用，包含独立业务表、接口与同步流 |

公共核心不按应用名称选择业务。模块声明自己的 ID、schema、个人数据能力、私人/公开文件区域、
业务能力、路由、命令行和复制表/同步流。业务路由必须通过 `application_router(app_id)`
固定所属应用；服务启动拒绝未绑定或绑定到其他应用的路由。

应用安装清单与 `core.apps` 中的登记必须一致。缺少登记、schema 或元数据不一致时启动失败，
不会由登录请求创建 schema、表或权限。运行角色不持有迁移权限。

## 持久化边界

`core` 保存应用、用户、数据集、成员、主个人空间映射和会话。
每个应用的业务表属于独立 schema。迁移 0011 将 Zheri 的导入批次、导入映射和修改回执
从 core 原地归入 zheri；保留行、ID、哈希、外键与 RLS。

`core.personal_spaces` 按 `(app_id,user_id)` 映射唯一主个人空间，复合外键检查应用与所有者。
应用可声明不创建个人空间；身份验证仍可使用。既有数据空间 ID 由迁移登记，不换号或重建。

API 校验、数据库 RLS、PowerSync 签名过滤分别保护各自的访问链路；schema 名字本身不代替授权。
应用禁用会阻止 API 与数据库私人数据访问。已签发 PowerSync 凭据的剩余有效期上限仍为 300 秒。

## 协议与发布

新应用使用 `/v1/apps/{application_id}/...`。路径、可选应用头和令牌必须一致。
Zheri 当前已交付的 `/v1/...` 与 `/v2/...` 接口由 Zheri 模块明确持有，共用同一套核心处理器；
公共核心没有“未指定应用就默认 Zheri”的逻辑。不再接受无应用签名的旧令牌。

所有应用仍由一套 API 镜像部署。增加业务模块需要代码、数据库迁移、注册、配置生成、
测试及重新构建镜像；无需修改公共登录或其他应用业务实现。
新增应用流程见 [接入应用](adding-an-application.md)，上线条件见 [应用契约](application-scope.md)。

参考：[FastAPI APIRouter](https://fastapi.tiangolo.com/tutorial/bigger-applications/)、
[PostgreSQL schema](https://www.postgresql.org/docs/current/ddl-schemas.html)、
[行级权限](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)。
