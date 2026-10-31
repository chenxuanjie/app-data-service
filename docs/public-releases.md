# 游客升级与发布

阶段 P1：程序安装包与桌游资料分别发布。升级入口无需用户登录，不使用共享 NAS 账号；客户端不能连接 PostgreSQL，也不能持有发布密钥。WebDAV 实现保留为显式备用，不自动回退。

2026-10-29 修复：通用 Android 工件统一使用 `any`，补充发布配置交付检查及上传前的匿名清单预检。源码修复和本机测试不代表 NAS 已更新或 APK 已发布。

## 存储与权限

迁移 `0015_public_releases` 创建 `core.release_blobs` 和 `core.public_releases`。版本、应用、通道、平台、架构、大小、摘要、更新说明及不可变来源记录保存在 SQL；APK/EXE 按摘要保存在独立内容卷。运行角色无 DDL、UPDATE 或 DELETE 权限；RLS 按已确认应用隔离，只有通过发布密钥校验的事务可插入记录。

迁移 `0016_release_architecture` 将旧索引中的 universal 修正为 any，保留原始不可变审计记录及安装包字节；提交前恢复 FORCE RLS。不修改已经交付的 0015 迁移，不在新 API 接受两种别名。

模块 `public_file_roots.releases` 声明卷根；桌游模块已声明该能力，其他模块不自动启用。不同应用的对象分别存入 `<root>/<app_id>/objects/`，未发布对象不能被游客读取。

`APP_DATA_RELEASE_PUBLISH_KEY_HASHES` 是应用 ID 到 SHA-256 密钥摘要的私有配置。随机密钥只保存在发布电脑的私有文件中，通过 HTTPS 的 `X-Release-Publish-Key` 头发送。它属于机器发布权限，没有用户、DSM SID、个人空间或普通登录会话。游客 GET/HEAD 不携带该凭据。

## API

前缀 `/v1/apps/<app_id>/releases`：

| 接口 | 用途 |
| --- | --- |
| GET `/manifest?platform=android&architecture=arm64&channel=stable` | 游客读取目标最新清单；未发布返回 404 |
| GET/HEAD `/artifacts/<sha256>.apk` 或 `.exe` | 只允许下载已发布对象；支持 Range，ETag 为摘要 |
| PUT `/objects/<sha256>` | 需要应用发布密钥；流式上传并校验摘要和长度 |
| POST `/batches` | 应用发布密钥；幂等申请一次构建编号 |
| GET `/batches/<batch-id>` | 应用发布密钥；查看批次来源及各平台结果 |
| PUT `/batches/<batch-id>/source` | 应用发布密钥；提交后冻结应用及依赖 commit |
| PUT `/batches/<batch-id>/targets` | 应用发布密钥；登记镜像结果或安装包失败；成功安装包必须走已验证发布接口 |
| POST `/publish` | 需要发布密钥及 batchId；复核文件后原子提交一个目标的版本、来源及批次目标 |

发布 JSON 复用 remote_update 的 schemaVersion=1 清单，单次一个 Android/Windows 目标；`releaseRecord` 保存 publisher 不可变来源记录。公开清单不返回私有记录。Android 架构为 any、arm64、arm、x64、x86；any 为通用包，与客户端 remote_update 及现有 publisher 记录一致。Windows 仅 x64。按通道与目标分别选择，相同构建号优先具体架构；较新通用包可以覆盖旧具体架构包。相同版本重试内容必须相同；较低版本或同版本不同内容返回 409。历史已发布对象仍可通过摘要 URL 下载。

## 统一程序批次（0018）

新增 `core.release_counters`、`core.release_batches` 和 `core.release_targets`，以 app_id 隔离，并强制 RLS 与应用机器发布权限；普通账号和游客不能读写。表不加入 PowerSync，桌游资料、账号与已有升级下载不受影响。历史安装包记录不重写，计数时纳入其最大构建号；各应用独立计数，各平台共用批次编号。

`POST /batches` 接收 requestKey（16..100 位安全标识）、明确的 versionName（三段语义版本）与 minimumBuild（本机已声明编号）。事务按应用串行分配 `max(历史发布号, 已分配号, minimumBuild)+1`，上限 2147483647；同请求键同版本重试返回原批次，不回收失败编号。事务有界等待，不在数据库里推断语义版本应升哪一位。

准备工具先将申请结果写到 pubspec 与不含 commit 的 release-batch.json；提交后，`PUT /source` 接收完整 sourceRevision 及 dependencyRevisions 映射，冻结后不可更改。新 `/publish` 请求必须带 batchId，版本、构建号及 releaseRecord.gitCommit 必须与冻结记录一致；安装包发布记录与目标结果在同一事务提交。旧发布器缺 batchId 将被拒绝，需要配套更新；游客 GET/HEAD 无需升级协议。

镜像目标记录 component、platform、architecture、channel、status（built/published/failed）、不可变 reference 与 sha256、sourceRevision；复用辅助镜像另带 reused 与 inputDigest，保留原始来源。已发布内容不可替换，相同内容可重试且不会因重复登记 built 降级；失败登记只接收安全 errorCode。GHCR 与数据库之间没有分布式事务，渠道更新及状态回写中断后应核对重试，不宣称 NAS 已部署。

部署这组接口需要新 API 镜像及 `0018_release_batches` 迁移；沿用已有发布密钥配置，无需更换 PostgreSQL/PowerSync 镜像或重新导入资料。依据实际 Compose 的迁移开关执行一次升级，不能仅凭改文档认定已在 NAS 生效。

安装包上限 1 GiB，两个并发上传，上传预算 15 分钟；八个并发文件响应，每次最多 10 分钟。普通 JSON 仍有 512 KiB/10 秒限制；仅精确匹配的安装包 PUT 走独立流式入口，读正文前鉴权。失败不切换发布版本，不自动删除历史对象。外部 HTTPS 反向代理须允许相应大小和上传时长。

## 本机配置准备

从已有私有 Compose 生成新配置，保留数据库、签名及其他应用配置，不重新生成部署身份：

```powershell
.venv/Scripts/python.exe tools/configure_public_releases.py --compose release/docker-compose.yml --output .local/p1-release-build-input.yaml --app-id board_game_agent --publisher-config C:/Users/win10/.codex/skills/flutter-release-publisher/runtime/board_game_agent-release-service.json
```

该命令只写本机文件，生成发布密钥及 publisher 配置，向 Compose 增加密钥摘要和专用命名卷。已有密钥可复用，不隐式轮换；输出 Compose 必须是新文件。命名卷由新镜像的 `/data/app-releases` 非 root 所有者初始化，不修改 NAS 原始目录权限。

该文件是构建输入，保留的旧镜像标签不是本次修复镜像。后续正式交付通过来源门禁后，向固定打包器传入此文件：

```powershell
.venv/Scripts/python.exe tools/build_docker_bundle.py --compose .local/p1-release-build-input.yaml --docker wsl --api-only
```

打包器在镜像/网络构建前、生成最终交付 Compose 时检查公共内容应用的 releases 根、独立可写卷和发布密钥摘要。缺少配置时直接失败，不再交付一个会返回 release_storage_not_configured 的文件；不会自动生成密钥或替换部署身份。构建成功后使用 `release/docker-compose.yml`，不能用旧输入中的镜像标签代替新交付结果。

P1 的架构修复要求迁移 `0016_release_architecture`；P2 通知还要求其后的 `0017_publication_versions`，统一程序批次要求 `0018_release_batches`。按实际 Compose 的迁移开关显式升级数据库结构，不重跑旧资料导入。仅更换 YAML 不会使旧镜像出现新接口。PostgreSQL 和 PowerSync 镜像无需升级；P2 须配套更新 PowerSync 配置，见 [公开发布通知](publication-notifications.md)。配置正确、尚未发布时清单应返回 404 release_not_published；503 表示配置或服务仍有故障。

## 发布器

新增 `publishDestination: service` 与 `releaseService.configFile`。桌游 profile 默认选择 service；其他工程保持原目的地。`--destination webdav` 显式选择原发布器，原凭据和目录不变。新目的地缺配置时明确失败，不自动回退。

流程：统一准备批次 → 提交版本及来源 → 冻结 → 本地产物与来源校验 → 流式上传并由服务端验摘要 → 提交 batchId、版本和来源记录 → 匿名 HEAD 验证下载。修改功能不授权实际申请编号、上传、部署或构建。

流式上传之前先匿名 GET 目标清单；404 允许首次发布，503/422 等错误在发送大包前终止。发布器同时核对 profile、清单、不可变记录及服务端回执的架构，输出允许公开的安全错误码，不打印原始响应或密钥。
