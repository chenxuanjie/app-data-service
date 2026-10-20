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
| APP_DATA_SIGNING_KEY_B64 | 容器内使用的同一私钥 Base64 内容，与文件路径二选一 |
| APP_DATA_ISSUER | 应用令牌签发者，API 严格验证 |
| APP_DATA_SYNC_ENDPOINT | 客户端实际可达的 PowerSync URL |
| APP_DATA_DSM_ORIGIN | 固定 DSM HTTPS 地址 |
| APP_DATA_DSM_IDENTITY_ORIGIN | 身份哈希命名空间，首次部署后保持稳定 |
| APP_DATA_POWERSYNC_UPSTREAM | 容器网络内固定 PowerSync 上游地址 |
| APP_DATA_ALLOWED_ORIGINS | 逗号分隔的浏览器来源；不使用通配 CORS |

API 启动不会自动升级数据库。Compose 的一次性迁移服务先由迁移角色执行至 0004。
应用令牌限定 RS256、kid、iss、aud=app-data-api、sub、iat、exp，最长一小时；
同步令牌 audience 单独为 app-data-powersync，有效期五分钟。两种令牌不能混用。
DSM 会话签发的应用访问令牌有效期 15 分钟，刷新令牌 30 天，刷新时重新验证 DSM UID。
原生客户端刷新令牌存安全存储；Web 仅保留在内存，刷新页面后需重新登录同步，离线数据仍可读取。
退出先撤销应用会话，再尽力注销 DSM 会话；旧 API 令牌立即失效，已签发 PowerSync 令牌最多再有效五分钟。
实验的维护端签发测试身份，不能当成真实登录验收。

| 路由 | 作用 |
| --- | --- |
| GET /health | 数据库连接健康，无用户数据 |
| GET /.well-known/jwks.json | 仅签名公钥 |
| POST /v1/auth/login | account、password、可选 otp；验证 DSM UID 后初始化个人测试数据集 |
| POST /v1/auth/refresh | refresh_token 换取新访问令牌，并重新验证 DSM 会话 |
| POST /v1/auth/logout | refresh_token 撤销应用会话及上游 DSM 会话 |
| /powersync/{path} | 同源流式代理；同步 endpoint 必须以 /powersync/ 结尾 |
| GET /v1/datasets | 当前账号的折日数据集 |
| POST /v1/sync/credentials | 已验证应用令牌换取短期同步令牌 |
| POST /v1/sync/upload | 一批业务修改，整体事务提交后才响应 |
| GET /v1/conflicts?dataset_id=... | 最多 100 条未解决冲突、审计快照及当前记录/版本 |
| POST /v1/conflicts/{id}/resolve | 选择 server / candidate，检查 expected_version |

业务私有路由要求 Bearer 令牌，用户 ID 来自签名 sub，不能从请求指定。
登录使用 DSM 凭据，刷新和退出使用刷新令牌；PowerSync 代理由上游校验同步令牌。
权限由 API 的数据集检查和数据库 RLS 双重限定。
请求体最多 512 KiB、读取限时 10 秒、每批最多 100 项；客户端超时不会清队列。
完整测试栈只发布 API 端口 18280，数据库和 PowerSync 不直接对宿主机开放。

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
候选选择也包括删除候选，审计记录继续保留。折日测试入口已提供选择界面，列表暂不分页。

该策略只实现首版保留版本并选择；没有用设备时间静默覆盖，也没有实现 CRDT。
实验客户端每次操作最多产生少量修改；批量业务接入时需遵守事务大小限制。

## 部署与待完成边界

PowerSync 1.27.0，PostgreSQL 存储版本 2，业务源与 bucket 为独立数据库。
实验源只读角色读取 publication 的三张表；bucket 专用角色不能访问业务库。
同步过滤来自签名身份与服务器成员关系，不信任客户端传来的账号或数据集列表。
PostgreSQL 连接的 sslmode=disable 仅用于本地私有容器网络。

DSM 请求通过固定 HTTPS 地址及请求级 Cookie / X-SYNO-TOKEN，不在 URL 或跨账号共享 Cookie 中保存会话。
用户 ID 为 synology-direct-v1、固定身份地址和 DSM UID 的 SHA-256；当前身份地址保留
https://cznas.dpdns.org:5001，请求地址为 https://cznas.pp.ua:5001。不能因域名更新随意修改身份地址。
数据库仅保存刷新令牌哈希和加密 DSM 会话；加密依赖固定签名私钥，容器重建必须保留配置。

尚未验证真实 DSM/NAS、Android/Windows/Web 三端实际运行，尚未实现生产密钥轮换和正式数据迁移。
折日测试入口使用独立本地库，不改变原业务数据与 WebDAV。备份仍按用户要求后置。

官方依据：[自部署配置](https://docs.powersync.com/configuration/powersync-service/self-hosted-instances)、
[Sync Streams](https://docs.powersync.com/sync/streams/examples)、
[自定义身份验证](https://docs.powersync.com/configuration/auth/custom)、
[PostgreSQL 17 超时](https://www.postgresql.org/docs/17/runtime-config-client.html)。
