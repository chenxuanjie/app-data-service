# 多应用账号与数据契约

2026-10-26，模块化改造源码，尚未部署。工程结构见 [架构](architecture.md)。
本轮只修改共享服务、公共客户端包及说明；不修改 Zheri 客户端源码、真实 NAS 或生产数据库。

桌游新增业务接口、公开挂载及阶段边界见 [桌游业务](board-game-business.md)。

## 应用选择与会话

公共 API 的固定前缀为 `/v1/apps/{application_id}`，提供以下后缀：

- `/client-config`：规范 API 地址和浏览器会话配置状态；
- `/auth/login`、`/auth/refresh`、`/auth/logout`：原生应用会话；
- `/auth/browser/bootstrap`、`login`、`restore`、`logout`：同源浏览器会话；
- `/datasets`、`/capabilities`、`/services`：当前应用的数据空间及实际服务能力；
- `/sync/credentials`、`/files/{area}/{path}`、`/updates/manifest`：公共同步/文件接口。

例如 `/v1/apps/board_game_agent/auth/login`。可选 `X-App-ID` 必须与路径一致，
访问、刷新和同步令牌均携带签名 `app_id`。未知应用拒绝访问，安装模块与数据库登记须一致。
HTTP 请求不能注册应用、提供模块导入路径、创建 schema 或指定任意文件根。

`RequestScope` 携带验证后的 user_id、app_id、session_id。私人请求必须使用 Actor 依赖；
公共路由可不要求登录，但仍按模块绑定与启用状态检查。模块自行定义真实业务和校验规则。
一个 DSM 身份保留一个全局用户 ID；每个应用和设备的会话独立，退出一个会话不撤销其他会话。

Zheri 当前的 `/v1/...` 与 `/v2/...` 是其模块拥有的现用 API，不是全局默认应用。
它复用公共处理器，Zheri 客户端无需改请求方式。新应用不使用这些入口。
无应用签名的旧凭据不再接受；浏览器 Cookie 统一为 `__Host-app-session-<app_id>`，
CSRF 也绑定应用。更新后旧会话可能需要重新登录，本地账本与待上传队列不能清空。

## 注册与数据

0010 建立应用作用域，0011 完成通用注册和主个人空间映射，0012 新增桌游业务表。
`core.apps` 记录 schema、启用状态、是否需要个人空间、空间名称及必要 ID 前缀。
源码 `ApplicationDefinition` 描述受审查的实现与能力；数据库登记是部署状态。
服务启动检查两者一致，不由数据库字段动态加载 Python 代码或动态拼接 DDL。

`core.ensure_app_account` 仅按已启用的注册元数据初始化当前身份的当前应用空间，
没有 Zheri/桌游名称分支。`core.personal_spaces` 保留既有主数据集映射。
登录不执行应用业务表初始化；业务 schema 和权限由显式迁移创建。
0011 原地移动 Zheri 的三类导入/修改回执表到 zheri schema，不删除或重写其记录。

所有私人事务同时以事务局部设置绑定账号和应用。RLS 检查数据集、成员和启用状态；
归还连接后不残留身份。API 运行账号没有 DDL、超级用户、角色管理权限。

## 部署配置

所有应用配置集中到 `APP_DATA_APPLICATIONS` JSON，例如：

```json
{
  "zheri": {
    "browser_origin": "https://zheri.example.test",
    "file_roots": {"updates": "/YourShare/apps/zheri/updates"}
  },
  "board_game_agent": {"browser_origin": "https://games.example.test"}
}
```

文件区域必须先由模块声明，路径必须是 File Station 可见的绝对路径。
文件、更新服务按当前账号 DSM 权限读取；更新清单 appId 必须属于当前应用。
没有新增上传、删除、赋权或创建 NAS 目录的功能。桌游公开资料接口仍属于后续业务阶段。
不同 Web 应用必须使用不同 origin；等价的大小写、尾斜杠和默认端口不能绕过检查。

不再使用 `APP_DATA_BROWSER_ORIGIN`、`APP_DATA_BOARD_GAME_BROWSER_ORIGIN`、
`APP_DATA_UPDATES_ROOT`、`APP_DATA_RESOURCES_ROOT` 等应用特定环境变量。
新环境用 `tools/render_stack.py --applications <JSON文件>` 生成配置。
既有部署应在保留数据库凭据、签名密钥、身份命名空间、卷和既有网页来源的基础上更新配置，
不能用新生成的秘密覆盖原部署，也不能仅替换镜像标签而遗漏这些配置。

## PowerSync 与上线

同步流和复制表由各模块声明。生成器汇总成一份配置；部署生成器和镜像交付器使用同一生成逻辑，
不另维护一套业务表清单。同步授权使用签名 `auth.parameter('app_id')` 和数据集成员关系，
客户端参数或主动订阅不能扩大权限。

令牌租期最长 300 秒。API 会话撤销立即生效；直接 PowerSync 连接的已发租约可能持续至到期。
没有实现服务端即时踢除所有已发同步租约，不能声称此边界已完成。

正式上线需要配套完成：

1. 提交并构建新的 API 镜像，保留发布来源和摘要；
2. 审查迁移与备份，升级至 0011，保留既有数据和 ID；
3. 用迁移角色运行 `app-data register-applications`，随后移除迁移凭据再运行 API；
4. 更新 `APP_DATA_APPLICATIONS` 和模块生成的 PowerSync 配置；
5. 验证登录、同步、退出、文件和离线队列。

生成的启动命令会先迁移、注册，再以运行角色启动 API。
PostgreSQL 17.11 与 PowerSync 1.27.0 的镜像版本本轮不需要升级。
0.5.0 的交付镜像只包含 API，不内置任何应用的网页；各应用前端独立部署。
API 的路径从 `/v1` 等协议前缀开始；前端同源网关负责移除 `/api/` 中的 `/api` 再转发。
原先由合包镜像提供的首页在升级后返回 404。镜像来源、验证和是否部署以 `release/metadata/` 的本次记录为准；旧镜像不包含这些源码。
不提供丢弃应用隔离的 down migration；回退须先停止写入并审查备份/版本，不能删除数据库重试。
