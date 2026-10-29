# 桌游业务模块：公开资料与私人同步

2026-10-26，`feature/board-game-business` 开发源码；没有构建新镜像、上传或改动 NAS。账号仍使用公共核心，Zheri 客户端未修改。新增业务均属于 `modules/board_game_agent/`，安装其他应用不需要在认证核心增加业务分支。

## 表和接口

迁移 `0012_board_game_business` 只新增桌游表，不转换或清理旧资料。

- 私人表：`favorites`、`recent_games`、`preferences`、`conversations`、`messages`、`votes`。按 `dataset_id` 归属，RLS 同时检查应用启用状态、固定应用和成员权限；运行角色无 DDL 和硬删除权限。
- 社区表：`comments`、`comment_likes`、`mutation_receipts`。写入者由验证后的 Actor 决定；请求不能指定用户身份或发布昵称。公开作者 ID 是桌游应用的假名 ID，不公开全局账号 ID。
- 六个私人流加入 publication 和生成配置，只使用签名 `app_id`、用户及数据集成员条件。评论不是私人流，不将其他人的收藏、偏好或聊天复制到设备。

路径前缀为 `/v1/apps/board_game_agent`：

| 接口 | 身份与用途 |
| --- | --- |
| GET `/catalog`、`/catalog/changes` | 匿名读取 SQL 发布的资料目录及增量 |
| GET/HEAD `/catalog/resources/{id}/content` | 匿名读取已发布的图片或文档，支持摘要及 Range |
| POST `/sync/upload` | 登录，`dataset_id` 与类型化 mutations；记录 ID、字段和应用须匹配 |
| GET `/community/comments/{game}` | 匿名可读，登录时附本人点赞状态；时间＋ID 游标分页 |
| POST `/community/commands` | 登录，发布/删除/点赞的幂等命令；只可删除自己的评论 |
| GET `/community/statistics` | 公开汇总；登录时仅额外返回本人的选择，便于客户端避免重复计数 |
| POST `/community/sync-credentials` | 匿名，5 分钟的公共 PowerSync 凭据，仅同步每游戏版本；不能用于私人读取或写入 |

命令/修改 ID 在账号和应用内唯一。同 ID 同内容返回原回执，不同内容返回 409。私人普通记录按服务端接受顺序覆盖；偏好 `changed_fields` 合并明确修改的字段，避免离线旧默认值覆盖另一设备的修改。删除是墓碑。单请求最多 100 条且受 512 KiB 请求体、锁和语句超时约束；桌游客户端按单条有界记录上传。

评论时间由服务器生成；每账号最新的带评分根评论参与平均分，删除该评论不会恢复旧评分。点赞表达目标开/关状态。公开响应不返回私人数据集、内部用户 ID 或凭据；含本人状态的响应禁止缓存。

## 资料发布入口（2026-10-27 更新）

新资料改用 [SQL 资料库与统一发布服务](library-catalog.md)：只读预览 → 类型化 SQL 草稿与内容寻址文件存储 → 原子发布。使用 `board-plan-library`、`board-import-library`、`board-publish-library --batch` 三条维护命令；旧的 `--source --destination` 快照生成实现已移除。

默认 4 MiB 内原件还保存为 SQL 原文/文本/JSON，缺失和未知附加字段保留并警告。已完成的一次性导入不在正常 API 启动时重新执行；维护导入和发布命令保留，具体配置和重启行为见上述协议。

客户端通过 `/catalog` 查询 SQL 发布资料；旧 `/library` 只读接口和快照配置已移除，不提供回退。原始资料和历史状态的保留、维护连接要求及 `library_store` 挂载配置见上述专项说明。

## 配置与下一次部署

在已有 `APP_DATA_APPLICATIONS` 中为桌游配置已发布资料卷和安装包卷：

```json
{"board_game_agent":{"public_file_roots":{"library_store":"/data/board-library","releases":"/data/app-releases"}}}
```

上述仅为配置形状，不应覆盖完整私有 Compose。保留已发布资料卷、安装包卷、数据库、签名、应用配置及 DSM 身份命名空间；不要重新导入或清空数据。部署须配套新 API 镜像、当前迁移及 PowerSync 规则。API 仍是纯 API 镜像，不包含 Zheri 网页。

Web 浏览器仍需要自己的 `/api/` 转发及 SQLite Worker 静态资源/响应头；用户已暂缓该部署问题，本轮不增加旧网关兼容。Loomex 模型服务、旧部署最终移除和实际设备验收不在阶段 2、3 的完成声明内。

## Loomex 模型转发（2026-10-27，未验收源码）

桌游模块新增 `/ai/loomex/v1/models`（GET）和 `/ai/loomex/v1/{operation}`（POST），后者仅接受 `chat/completions`、`responses`、`responses/compact`。使用既有 Actor/应用会话验证，无需新增表、迁移或改认证核心。上游默认 `https://www.loomex.lol/v1`，默认允许模型 `gpt-6-sol`，推理 low、服务等级 default；Key 只从 API 容器的 `APP_DATA_BOARD_LOOMEX_API_KEY` 读取。

可用 `APP_DATA_BOARD_LOOMEX_BASE_URL` 和逗号分隔的 `APP_DATA_BOARD_LOOMEX_MODELS` 显式配置部署；模型集合必须包含默认模型。客户端不能覆盖上游/Key，不开放 provider 存储响应读取、跨账号 previous_response_id 或 background。客户端幂等键与账号绑定，流取消/错误释放连接，并限制容量、时长和大小。实现复用已锁定 HTTPX/Starlette，不新增依赖；上游错误正文不直接下发。

用户提供的凭据仅写入 Git 忽略的 `.local/loomex.private.json` 及既有私有构建输入 `.local/api-only-input.yaml`。未修改旧交付 Compose、数据库凭据、NAS 或真实数据。后续需更新 API 镜像、容器环境及客户端；无须重建 PostgreSQL/PowerSync 镜像。**本次用户明确要求不测试、不构建、不编译；没有执行 pytest、lint 检查、上游请求或容器运行，不能声明模型调用通过。**

## 公共实时通知（2026-10-27）

迁移 `0013_board_public_changes` 新增 `public_changes(id, statistics_version, comments_version)`。收藏、投票、评论、评论点赞的 AFTER 触发器与业务写入处在同一事务；回滚没有通知，幂等回执重放不重复修改版本。版本行保留，删除最后一票也能通知其他读者；新表初始汇总现有游戏 ID，不修改旧业务数据。

运行角色只可按桌游 RLS 读取该表，无版本写入权限。触发器函数固定搜索路径且撤销 PUBLIC 执行权限。表加入 publication，生成配置增加 `board_public_changes` 流，只接受签名 `app_id=board_game_agent` 和 `sync_scope=public`；六个私人流额外排除公共 scope。客户端请求参数、伪造账号 ID 或手动订阅不能扩大签名权限。

客户端保留独立公共数据库，只下载游戏 ID 和版本，收到变化后从已有 API 读取完整统计/正在查看的评论。统计与评论分开失效；没有复制其他用户的私人选择或把全量评论下发给游客。PowerSync 处理断线检查点与重连，客户端补读最新快照。

本轮修改源码与测试，不构建镜像或部署。正式更新需同时发布 API（运行迁移到 0013）和新生成的同步配置，再更新客户端；无需升级数据库/PowerSync 镜像。回退客户端可保留新表；若要撤掉通知机制，先停公共订阅并审查依赖，0013 不提供自动破坏性 downgrade。

验收：ruff 检查、格式检查和真实 PostgreSQL 完整 255 项 pytest 通过。`tools/run_sync_probe.py --docker wsl --board-flutter <flutter.bat>` 验证实际公共订阅的收藏汇总、评论新增/点赞/删除及暂停恢复，并用原始同步协议验证公共 scope 在伪造现有账号 subject、客户端参数和私人订阅时仍不返回私人行。一次性容器、数据库、角色和复制槽均已清理；真实 NAS 与安装产物不在该结论内。
