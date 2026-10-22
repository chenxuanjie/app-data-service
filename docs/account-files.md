# 账号文件与更新 API（第二阶段）

2026-10-22。源码版本 API 0.4.0，配套折日 0.13.0+64、app_client 0.2.0。
仅本地开发与验证；尚未打包或部署 NAS。原有 release 目录不是这次源码的交付物。

## 调用链

折日登录应用服务 → 应用访问令牌 → FastAPI 校验有效会话 → 解密该会话的 DSM SID →
固定 DSM HTTPS 上游的 File Station 下载接口。服务端不保存 NAS 密码，不使用管理员身份代读。
原生刷新凭据及浏览器 HttpOnly 会话继续沿用[第一阶段](browser-sessions.md)。
客户端不连接 WebDAV，也不再从 NAS 域名推导 5006 端口。

| 只读接口 | 返回内容 |
| --- | --- |
| GET /v1/services | 已配置的文件区域名、是否配置更新；不是实时健康检查，不返回目录 |
| GET /v1/updates/manifest?channel=stable | 当前折日 stable 更新清单，校验应用标识、构建号和工件路径/大小/摘要 |
| GET /v1/files/updates/{relative_path} | 更新目录中的文件，包括 APK；需要当前账号原有 DSM 读取权限 |
| GET /v1/files/resources/{relative_path} | 可选资料目录中的文件；本轮未给折日增加资料浏览页面 |

全部需要 Bearer 应用令牌；没有文件上传、发布或删除接口。发布工具的写权限继续独立。
旧发布工具仍可把 manifest.json 与 APK 放入原目录，只要配置的 File Station 路径指向同一位置。
这不要求更新 NAS 账号权限，也不自动移动已有文件。

## 服务端配置

在现有 API 服务 environment 中增加以下可选字段，保持原数据库 URL、密钥、身份命名空间不变：

```yaml
APP_DATA_UPDATES_ROOT: /YourShare/apps/zheri/updates
# 仅需要资料读取时才配置：
APP_DATA_RESOURCES_ROOT: /home/Apps/zheri
# 浏览器登录必须与网页来源完全一致：
APP_DATA_BROWSER_ORIGIN: https://your-web.example
```

路径是 **File Station API 可见的绝对路径**，不是容器挂载路径、WebDAV URL 或
`/volume1/...` 宿主路径的直接复制。例如共享文件夹 YourShare 对应 `/YourShare/...`。
上述路径仅为示例；部署前核对实际更新清单目录，不会自动创建目录或赋权。
`/home/...` 由 DSM 按当前用户解释；公共更新目录则由 NAS ACL 决定谁可读取。
不配置根目录时相应功能返回 503，不退回 WebDAV，也不影响业务同步。

`tools/render_stack.py` 为新环境提供 `--updates-root`、`--resources-root`、`--browser-origin`。
已有部署不要重新运行生成器覆盖私有凭据；之后正式交付时从原配置保留凭据并更新镜像和所需字段。
本轮不新增数据库迁移或 PowerSync 规则；未部署第一阶段的环境首次升级仍需执行 0007 会话迁移。

Web 仍遵守同源 `/api/` 网关契约，转发时移除前缀，保留 Authorization、Cookie、Origin、
X-CSRF-Token、Set-Cookie。文件下载还需保留 Range / Content-Range / Content-Length、
禁用响应缓存与代理缓冲，并允许最长十分钟的流式下载。普通 http.server 没有这层能力。
只需沿用应用 HTTPS 入口，无需向公网开放 PostgreSQL、DSM 或 WebDAV 新端口。

## 清单、下载与安装

更新目录保留既有 remote_update 清单格式：schemaVersion=1、appId=zheri、channel=stable，
versionName、buildNumber、releaseNotes 和 artifacts。每个工件给出 platform、architecture、
相对 path、size、sha256、mimeType；禁止 URL、父目录、双重编码和绝对路径。
APK 路径从更新根目录解析，与 manifest.json 同一目录树；客户端不能改选另一个 NAS 或上游 URL。

服务端最多四个并发文件传输，取得槽位最多一秒；上游头部十五秒、错误 JSON 十秒、
清单流十五秒、文件流十分钟，单文件上限 1 GiB、清单 128 KiB。这些是 I/O 保护，
没有添加 CPU 配额或新的容器资源限制。根目录读取权限由 NAS 决定。
DSM `mode=download` 使用 octet-stream；JSON 错误只返回映射后的错误码。

支持单段 `Range: bytes=N-`：验证上游 206 起点、终点、总长度；上游返回 200 时客户端从零重写，
返回 416 时重新请求完整文件。没有假定所有 DSM 版本都支持续传。
客户端复用 remote_update 校验文件大小和 SHA-256，再请求 Android 安装权限并启动系统安装器。
缓存按当前 server/user/dataset scope 隔离；账号切换、退出和页面关闭取消旧下载，
启动安装器前再次检查账号。取消后通过现有“重试”重新检查清单，再安全续传或重新下载。

401 提示重新登录；403 提示账号已登录但无更新文件权限；404 提示未发布或文件移除；
503 提示服务暂不可用。403 不退出账号，文件错误不覆盖业务同步状态。
Windows/Web 的应用内安装继续显示不支持；它们的账号、数据同步仍可使用。

## 本地验证与 NAS 验收

本地：ruff、完整真实 PostgreSQL 测试；HTTPX 严格 DSM 替身核对当前 SID、只读路径、
账号隔离、退出撤销、错误映射、分段响应及传输槽位释放。公共包与折日测试覆盖源适配、
取消/重试、账号变化、SHA-256 失败、安装权限失败；安装器使用替身，不启动系统安装。

NAS 待正式交付后验证：

1. 使用同一账号登录，检查更新清单；分别验证有权限/无权限的测试账号。
2. 在原更新目录发布高构建号的有效测试 APK，核对更新版本，下载、取消、重试和系统安装。
3. 退出或切换账号时不能继续安装原账号的候选包；重新检查后使用新账号权限。
4. 断开更新目录或 DSM 连接，确认文件错误明确、原有业务离线数据仍可编辑；恢复后重试。
5. Web 通过正式同源网关验证刷新恢复、退出、账号隔离及业务同步，不通过文件 API 更新网页自身。

真实 DSM 的文件路径、权限、Range 和 Android 安装结果尚未验收；本地测试通过不代表 NAS 已升级。

协议参考：[Synology File Station 官方 API 指南](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Package/FileStation/All/enu/Synology_File_Station_API_Guide.pdf)。
