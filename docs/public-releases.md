# 游客升级与发布

阶段 P1：程序安装包与桌游资料分别发布。升级入口无需用户登录，不使用共享 NAS 账号；客户端不能连接 PostgreSQL，也不能持有发布密钥。WebDAV 实现保留为显式备用，不自动回退。

## 存储与权限

迁移 `0015_public_releases` 创建 `core.release_blobs` 和 `core.public_releases`。版本、应用、通道、平台、架构、大小、摘要、更新说明及不可变来源记录保存在 SQL；APK/EXE 按摘要保存在独立内容卷。运行角色无 DDL、UPDATE 或 DELETE 权限；RLS 按已确认应用隔离，只有通过发布密钥校验的事务可插入记录。

模块 `public_file_roots.releases` 声明卷根；桌游模块已声明该能力，其他模块不自动启用。不同应用的对象分别存入 `<root>/<app_id>/objects/`，未发布对象不能被游客读取。

`APP_DATA_RELEASE_PUBLISH_KEY_HASHES` 是应用 ID 到 SHA-256 密钥摘要的私有配置。随机密钥只保存在发布电脑的私有文件中，通过 HTTPS 的 `X-Release-Publish-Key` 头发送。它属于机器发布权限，没有用户、DSM SID、个人空间或普通登录会话。游客 GET/HEAD 不携带该凭据。

## API

前缀 `/v1/apps/<app_id>/releases`：

| 接口 | 用途 |
| --- | --- |
| GET `/manifest?platform=android&architecture=arm64&channel=stable` | 游客读取目标最新清单；未发布返回 404 |
| GET/HEAD `/artifacts/<sha256>.apk` 或 `.exe` | 只允许下载已发布对象；支持 Range，ETag 为摘要 |
| PUT `/objects/<sha256>` | 需要应用发布密钥；流式上传并校验摘要和长度 |
| POST `/publish` | 需要发布密钥；复核文件后原子提交一个目标的版本及来源记录 |

发布 JSON 复用 remote_update 的 schemaVersion=1 清单，单次一个 Android/Windows 目标；`releaseRecord` 保存 publisher 不可变来源记录。公开清单不返回私有记录。Windows 仅 x64；按通道与目标分别选择，universal Android 工件可匹配设备。相同版本重试内容必须相同；较低版本或同版本不同内容返回 409。历史已发布对象仍可通过摘要 URL 下载。

安装包上限 1 GiB，两个并发上传，上传预算 15 分钟；八个并发文件响应，每次最多 10 分钟。普通 JSON 仍有 512 KiB/10 秒限制；仅精确匹配的安装包 PUT 走独立流式入口，读正文前鉴权。失败不切换发布版本，不自动删除历史对象。外部 HTTPS 反向代理须允许相应大小和上传时长。

## 本机配置准备

从已有私有 Compose 生成新配置，保留数据库、签名及其他应用配置，不重新生成部署身份：

```powershell
.venv/Scripts/python.exe tools/configure_public_releases.py --compose .local/board-library-runtime.yaml --output .local/public-releases-compose.yaml --app-id board_game_agent --publisher-config C:/Users/win10/.codex/skills/flutter-release-publisher/runtime/board_game_agent-release-service.json
```

该命令只写本机文件，生成发布密钥及 publisher 配置，向 Compose 增加密钥摘要和专用命名卷。已有密钥可复用，不隐式轮换；输出 Compose 必须是新文件。命名卷由新镜像的 `/data/app-releases` 非 root 所有者初始化，不修改 NAS 原始目录权限。

必须先构建包含本轮源码的新 API 镜像，匹配镜像标签后更新 Compose。仅更换 YAML 不会使旧镜像出现新接口。PostgreSQL 和 PowerSync 镜像无需升级，本阶段不修改同步流。首次发布前清单返回 404 属于正常状态。

## 发布器

新增 `publishDestination: service` 与 `releaseService.configFile`。桌游 profile 默认选择 service；其他工程保持原目的地。`--destination webdav` 显式选择原发布器，原凭据和目录不变。新目的地缺配置时明确失败，不自动回退。

流程：本地产物与来源校验 → 流式上传并由服务端验摘要 → 提交版本和来源记录 → 匿名 HEAD 验证下载。修改功能不授权实际上传、部署或构建。
