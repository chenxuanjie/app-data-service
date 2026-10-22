# 同源浏览器会话（重构第一阶段）

2026-10-22，功能分支源码，尚未构建或部署到 NAS。整体进度见
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

只有配置 APP_DATA_BROWSER_ORIGIN 才启用浏览器会话。网页和 /api/ 同源，网关转发时移除 /api 前缀，
保留 Origin、Content-Type、X-CSRF-Token、Cookie、Set-Cookie；不得缓存认证响应。
FastAPI 不信任任意 X-Forwarded-* 或请求提供的 NAS 上游。

Cookie 使用 Secure/HttpOnly/SameSite=Strict/Path=/，不设置 Domain；生产必须 HTTPS。
CSRF token 使用服务器稳定密钥对随机 Cookie ID 的 HMAC，只通过同源响应交给页面内存。
数据库按 Cookie 哈希启用 RLS，刷新令牌密文保存。匿名十分钟、登录三十天过期，访问令牌十五分钟。
bootstrap 与登录有有界频率/并发限制，所有数据库事务、锁和 DSM 请求有超时。

退出抢先发生时，等待中的登录不能再绑定会话；生成的候选应用/DSM 会话会被撤销。
重新登录遇到离线退出遗留 Cookie 时，先结束旧会话再创建新会话，不恢复旧账号作为新账号。

不做旧浏览器 WebCrypto 凭据兼容，更新后需重新登录一次；客户端缓存和待上传队列保留。
文件 API、更新 API、board-game-agent 接入均不在这个已实现阶段内。
