# 群晖统一应用服务 0.5.0：纯 API

本版本包含共用账号、应用隔离、模块注册、账号文件与同步 API，不包含折日或其他应用的网页。只生成本地交付文件；上传及真实 NAS 部署分别需要明确授权。

## 交付文件

- `fastapi-0.5.0.tar.gz`：本次新构建的 Linux amd64 API 镜像。名称中的版本是应用服务版本，不是 FastAPI 框架版本。
- `postgresql-17.11.tar.gz`、`powersync-1.27.0.tar.gz`：复用已验证的既有镜像；本轮不升级或重新构建。
- `docker-compose.yml`：保留原数据库凭据、签名密钥、身份命名空间、卷及外部端口；更新 API 标签、启动命令、应用配置和同步规则。含秘密，不公开。
- `metadata/`：完整提交、镜像 ID、SHA-256、导入与隔离容器验收结果。

## 构建

这是 Python 工程，使用项目构建器，不走要求 `pubspec.yaml` 的 Flutter 包装入口。先按源码门禁提交并检查本仓库，再用门禁 `run` 包裹以下本地命令：

```powershell
.venv/Scripts/python.exe tools/build_docker_bundle.py --compose .local/api-only-input.yaml --api-only --docker wsl --dart C:/Users/win10/tools/flutter/bin/dart.bat
```

输入 Compose 必须采用当前 [应用配置契约](application-scope.md)。已有部署只做显式配置转换，不能重新生成其密钥或密码。构建器离线安装锁定的 Linux wheel，构建、导出 gzip 归档、校验平台和镜像身份、重新导入，再运行真实 PostgreSQL/PowerSync/API 的隔离验证。通过后才替换交付目录；失败保留旧交付文件。

不再接受 Web 工程、网页压缩包或网页来源凭据等参数，不需要编译 Flutter。验收中的 Dart 只运行同步测试客户端，不参与 API 镜像打包。

### 固定入口与失败定位

- 打包入口固定为 `tools/build_docker_bundle.py`，容器验收入口固定为 `tools/verify_stack.py`。不要每次手写另一套 build/save/load 命令或临时跳过验收。
- 流程依次为：输入检查 → wheel 准备 → 镜像构建/load → 临时归档/save、校验及重新导入 → 隔离容器验收 → 更新 `release/`。不同阶段可以有不同错误，报告必须指出失败阶段和日志路径。
- “镜像已构建并载入 Docker”不等于交付成功。验收前已经可能在 `.local/bundle-*/delivery/` 生成临时 TAR；只有全部通过才晋升至 `release/`，失败保持原交付包和配套文件不变。
- Windows 使用 WSL Docker 时，三个固定入口（打包、容器验收、同步探针）都用 `tools/docker_runtime.py` 保持附着的 WSL 进程，覆盖 Windows 侧的压缩、Dart 客户端及等待阶段。任务结束或失败后关闭自己持有的管道并回收进程，不修改 `.wslconfig`，不关闭整台 WSL，也不依赖人工一直开着终端。
- WSL 冷启动就绪握手最多 45 秒，辅助进程最多一小时，清理等待有界。它不能阻止用户显式关机或外部强制终止 Docker；这类故障仍须报告，不能标成业务测试通过。此启动预算与业务收敛超时分别管理。
- 容器验收失败会打印 `.local/stack-<随机ID>/failure.json` 的完整路径，记录阶段、容器状态、退出码、OOM 标记和重启次数；同目录保留 `dart.log` 和 `docker.log`。状态快照不包含容器环境变量、密码或私钥。诊断记录在测试清理前采集。

2026-10-27 排查：两次本机验收分别停在 `payroll_records deleted in personal ledger` 与 `business_preferences winner`。WSL journal 显示 Docker 引擎先于客户端超时被 systemd 停止（10 月 26 日 23:34:55、23:48:41，Asia/Shanghai），客户端随后持续收到 502。该证据定位到本机验收环境提前关机，不能据此认定某张业务表的删除失败，也不能用来解释真实 NAS 上的 API 容器意外重启。[Microsoft 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd) 明确说明 systemd 服务本身不会维持 WSL 实例运行；固定入口须管理任务期间的运行时生命周期。

## 更新原项目

1. 在受控维护窗口审查迁移和备份；本次本地构建不会访问或备份真实 NAS。
2. 导入新的 FastAPI 镜像包，用配套 Compose 更新原项目。PostgreSQL、PowerSync 的版本、凭据和数据卷保持不变，不删除卷重试。
3. API 启动先迁移至本版本的 `head`（阶段 2、3 为 `0012`），再执行 `app-data register-applications`，移除迁移凭据后以运行角色启动。旧会话可能需要重新登录，不清空客户端缓存或离线队列。
4. 应用配置统一使用 `APP_DATA_APPLICATIONS`。各 Web 应用使用不同 origin；沿用原配置中仍适用的网页来源与文件根，不能把多个应用浏览器来源混成一个。
5. 同步配置由已安装模块生成，包含应用签名与数据集成员校验，不能沿用缺少应用隔离的旧规则。

API 服务的 `/health` 用于健康检查；新应用使用 `/v1/apps/<app_id>/...`。根路径、`/index.html`、`/main.dart.js` 及网页路由均返回 404，这是纯 API 的预期行为。

原先直接打开 API 地址看到的折日页面不再存在。后续独立 Web 服务应尽量保留原网页 origin，避免改变浏览器本地存储分区，并提供同源 `/api/` 网关：移除 `/api` 前缀后转发到 API，保留 Cookie、Set-Cookie、Origin、CSRF 和流式响应，不缓存认证请求。独立前端部署不在本次镜像构建范围。

## 验收与边界

本地验证使用临时容器、临时卷、真实 PostgreSQL、PowerSync、SQLite 同步客户端和严格 TLS DSM 替身，覆盖迁移失败阻止启动、应用注册、登录/刷新/退出、跨账号及跨应用隔离、文件权限、离线同步、冲突及容器重启持久化，并检查镜像中没有 `/app/web`。

模拟 DSM 和 HTTPX 协议验证不能替代真实 NAS 登录、浏览器同源网关或 Android/Windows 实机验收。容器镜像构建成功、已上传、已部署分别报告。不要将导入镜像描述为数据库自动回滚；迁移后回退需要先停止写入并审查备份与版本。
