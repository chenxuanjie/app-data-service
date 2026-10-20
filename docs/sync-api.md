# 最小同步 API

本阶段验证固定支出闭环。FastAPI 是认证与业务写入口，PowerSync 管理上传队列、
下行、检查点与本地 SQLite。应用保存先提交本地，不等待服务器。

## 服务运行契约

通过可信运行环境设置以下变量，再运行：

```text
python -m uvicorn app_data_service.api:from_environment --factory --host 127.0.0.1 --port 18280
```

| 环境变量 | 含义 |
| --- | --- |
| APP_DATA_DATABASE_URL | app_runtime 的 postgresql+psycopg URL |
| APP_DATA_SIGNING_KEY | 至少 2048 位 RSA 私钥 PEM 路径，不进入客户端或 Git |
| APP_DATA_ISSUER | 应用令牌签发者，API 严格验证 |
| APP_DATA_SYNC_ENDPOINT | 客户端实际可达的 PowerSync URL |

启动不会自动升级数据库或创建用户。先由迁移角色执行 Alembic 至 0003。
应用令牌限定 RS256、kid、iss、aud=app-data-api、sub、iat、exp，最长一小时；
同步令牌 audience 单独为 app-data-powersync，有效期五分钟。两种令牌不能混用。
正式 DSM 账号兑换、刷新、撤销和客户端账号生命周期仍待接入。
实验的维护端签发测试身份，不能当成真实登录验收。

| 路由 | 作用 |
| --- | --- |
| GET /health | 数据库连接健康，无用户数据 |
| GET /.well-known/jwks.json | 仅签名公钥 |
| GET /v1/datasets | 当前账号的折日数据集 |
| POST /v1/sync/credentials | 已验证应用令牌换取短期同步令牌 |
| POST /v1/sync/upload | 一批业务修改，整体事务提交后才响应 |
| GET /v1/conflicts?dataset_id=... | 最多 100 条当前账号未解决冲突，选择 UI 尚未实现 |
| POST /v1/conflicts/{id}/resolve | 选择 server / candidate，检查 expected_version |

所有私有路由要求 Bearer 令牌，用户 ID 来自签名 sub，不能从请求指定。
权限由 API 的数据集检查和数据库 RLS 双重限定。
请求体最多 512 KiB、读取限时 10 秒、每批最多 100 项；客户端超时不会清队列。
服务配置只为开发实验开放本机回环地址；正式部署还需 API 容器和网关集成。

## 上传与冲突

上传体包括 dataset_id、稳定的 PowerSync client_id、mutations。
每项包含 mutation_id UUID、record_id、base_version、可空 predecessor、kind 及 record。
put 使用完整的**单条记录**候选；delete 的 record=null，服务器软删除。
这是对单条用户意图的版本检测，不是对整个数据集快照做替换。

record 沿用既有 JSON 字段，但 amount 必须为十进制字符串，拒绝浮点数输入。
客户端不提交服务器生成的 row_version/updated_at。金额最多六位小数，不静默舍入。
实验 connector 把完整意图写入 PowerSync 的 _metadata，与本地数据和前序 ID
在同一 SQLite 事务中提交。local-only 的 mutation_heads 不上传、不复制。

服务器在同一数据集锁下处理整批修改，并持有目标记录行锁：

1. 同用户/客户端/修改 ID 已有回执：内容一致则返回原结果；不同则 409，整批回滚。
2. base_version 等于当前版本：接受。版本始终由 PostgreSQL 触发器递增。
3. 离线连续修改仍基于旧版本时，允许前序回执属于同用户/客户端/数据集/记录，且
   前序写入后的版本仍是当前版本、来源版本相同。其他设备的修改会打断该条件。
4. 过时修改：存储双方版本及冲突回执，不覆盖当前记录。客户端收到确认后可以
   清除该项上传队列，其他记录继续同步，冲突候选保留在服务器。
5. 网络、身份、格式或数据库错误：不确认队列，失败事务整体回滚。

选择冲突时重新比较当前版本。服务器又变更则 409，先重新查看；不能覆盖未看过的
新版本。重复提交同一选择返回原处理结果；更改已经完成的选择明确拒绝。
候选选择也包括删除候选，审计记录继续保留。冲突界面和分页在折日接入阶段完成。

该策略只实现首版保留版本并选择；没有用设备时间静默覆盖，也没有实现 CRDT。
实验客户端每次操作最多产生少量修改；批量业务接入时需遵守事务大小限制。

## 部署与待完成边界

PowerSync 1.27.0，PostgreSQL 存储版本 2，业务源与 bucket 为独立数据库。
实验源只读角色读取 publication 的三张表；bucket 专用角色不能访问业务库。
同步过滤来自签名身份与服务器成员关系，不信任客户端传来的账号或数据集列表。
PostgreSQL 连接的 sslmode=disable 仅用于本地私有容器网络。

目前未完成真实 DSM 登录、生产密钥轮换、API 容器交付、折日页面与数据层、三端运行
和正式数据迁移；现有 WebDAV 和折日本地库不受该实验影响。备份仍按用户要求后置。

官方依据：[自部署配置](https://docs.powersync.com/configuration/powersync-service/self-hosted-instances)、
[Sync Streams](https://docs.powersync.com/sync/streams/examples)、
[自定义身份验证](https://docs.powersync.com/configuration/auth/custom)、
[PostgreSQL 17 超时](https://www.postgresql.org/docs/17/runtime-config-client.html)。
