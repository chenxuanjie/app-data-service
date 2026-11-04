# 同源浏览器会话（重构第一阶段）

2026-10-26 更新：下表中的 /v1 路由是 Zheri 当前模块接口；新应用使用 /v1/apps/<app_id> 前缀。
无应用签名的旧凭据不再接受，更新后可能需要重新登录一次。整体进度见
[折日统一应用服务重构](../../../zheri/docs/unified-service-refactor.md)。

迁移 `0007_browser_sessions` 只新增 core.browser_sessions，业务表及账号/账本 ID 不改变。
原生客户端继续使用系统安全存储与原生令牌接口；网页改为同源 HttpOnly Cookie，不返回刷新令牌。

| 路由 | 用途 |
| --- | --- |
| GET /v1/client-config | 公开应用服务规范地址与浏览器会话启用状态，不含 NAS 配置或秘密 |
| POST /v1/auth/browser/bootstrap | 校验来源后创建/恢复匿名 Cookie，并返回内存 CSRF token |
| POST /v1/auth/browser/login | CSRF 校验，DSM 验证后绑定服务器会话 |
| POST /v1/auth/browser/restore | 恢复 Cookie 会话、核验 DSM，返回短期访问令牌和用户标识 |
| POST /v1/auth/browser/logout | 先撤销浏览器会话，再撤销对应应用会话与 DSM 会话 |

当前每个应用在 APP_DATA_APPLICATIONS 中配置 browser_origin 才启用浏览器会话，见 [当前契约](application-scope.md)。网页和 /api/ 同源，网关转发时移除 /api 前缀，
保留 Origin、Content-Type、X-CSRF-Token、Cookie、Set-Cookie；不得缓存认证响应。
FastAPI 不信任任意 X-Forwarded-* 或请求提供的 NAS 上游。

代理后的客户端地址和认证限额见 [认证限流与可信代理链](trusted-proxies.md)。API 使用显式 `APP_DATA_TRUSTED_PROXIES` 名单；登录与 bootstrap 按应用及客户端隔离，登录另有账号维度限额，保留实例级 DSM 并发保护。

2026-11-01 增加可选 `browser_allowed_origins`：同一应用可显式配置最多 16 个附加来源，供本地 HTTPS 预览等独立入口使用；必须同时配置正式 `browser_origin`。来源经过完整 origin 校验、去重，并禁止与其他应用的正式/附加来源重叠。无通配符、无客户端传入上游，不重写浏览器 Origin；Cookie 的 Secure/HttpOnly/SameSite 和 CSRF 要求不降低。它只扩展浏览器会话来源检查，不自动修改 CORS。配置变更无需新增数据库迁移，但使用此字段的运行镜像必须包含本轮支持。
0.5.0 起 FastAPI 为纯 API 服务，网页与 `/api/` 的同源转发由独立前端网关提供；后端不再内置静态页面或移除 `/api` 前缀的中间件。

Cookie 名为 __Host-app-session-<app_id>，CSRF 绑定应用。Cookie 使用 Secure/HttpOnly/SameSite=Strict/Path=/，不设置 Domain；生产必须 HTTPS。
CSRF token 使用服务器稳定密钥对随机 Cookie ID 的 HMAC，只通过同源响应交给页面内存。
数据库按 Cookie 哈希启用 RLS，刷新令牌密文保存。匿名十分钟、登录三十天过期，访问令牌十五分钟。
bootstrap 与登录有有界频率/并发限制，所有数据库事务、锁和 DSM 请求有超时。

退出抢先发生时，等待中的登录不能再绑定会话；生成的候选应用/DSM 会话会被撤销。
重新登录遇到离线退出遗留 Cookie 时，先结束旧会话再创建新会话，不恢复旧账号作为新账号。

不做旧浏览器 WebCrypto 凭据兼容，更新后需重新登录一次；客户端缓存和待上传队列保留。
文件与更新 API 见[第二阶段](account-files.md)；桌游采用 `/v1/apps/board_game_agent/` 契约。Web 与原生共用业务服务，Cookie 与原生令牌的传输方式按平台区分。
