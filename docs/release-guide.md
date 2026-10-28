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

输入 Compose 必须采用当前 [应用配置契约](application-scope.md)。已有部署只做显式配置转换，不能重新生成其密钥或密码。构建器离线安装锁定的 Linux wheel，构建、导出 gzip 归档、校验平台和镜像身份、重新导入，再执行 PostgreSQL/PowerSync/API 的隔离启动冒烟。通过这些交付检查后才替换交付目录；失败保留旧交付文件。

构建和推送不强制完整测试集通过，默认不执行 pytest 或 Dart 全业务同步验收，报告记录 `businessTests: not_run`。需要完整业务验收时，在上述命令显式增加 `--full-validation`；该选项选择完整验收并要求它通过。独立运行 `tools/verify_stack.py` 默认仍表示请求完整容器验收；`--smoke-only` 只执行发布所需的启动检查。

不再接受 Web 工程、网页压缩包或网页来源凭据等参数，不需要编译 Flutter。默认打包不需要 Dart；只有显式完整验收时，Dart 才运行同步测试客户端，不参与 API 镜像打包。

完整打包默认复用身份与摘要一致的 PostgreSQL/PowerSync 归档：检查旧清单的平台、镜像 ID、既有导入记录、文件名、大小与 SHA-256，并在晋升前复查。完整安装集仍含三张镜像；`reusedArchives` 与 `rebuiltArchives` 说明本次实际处理范围。依赖缺失或校验不符时重新导出；需要强制重新导出所有组件时使用 `--force-reexport`，不能与 `--api-only` 同用。

WSL 的归档阶段由 Windows 调度 `tools/docker_archive_worker.py`，在 Ubuntu `/tmp` 完成 save、gzip、校验、load 与摘要计算，再将最终 gzip 归档复制到 Windows 暂存区并复核摘要。中间原始 TAR 不经过 Windows 文件系统；临时目录只清理本次自建目录。gzip 等级保持原值，pytest 环境不变。`metadata/release.json` 的 `archiveActions` 包含每组件工作区、缓存原因和压缩/验证/复制的独立耗时。

### 固定入口与失败定位

构建器先检查 Docker/Buildx、实际 Docker HTTPS 代理到 Registry 鉴权接口，再下载 wheel。
WSL 握手不代表 Docker 就绪：构建器先等待实际 Docker API，已安装的 Docker/已登记的
本机代理服务若停止则尝试非交互启动；启动失败报告权限或服务故障，不安装新服务、
重启整台 WSL 或修改 NAS。数据库就绪与 Windows 转发恢复仅属于开发检查入口，
不因镜像构建启动开发数据库或执行 pytest。
每阶段的完整输出与耗时写入 `.local/task-logs/<本次任务>/` 的日志和 `stages.json`，
失败、超时仍保留。报告不记录命令参数、代理凭据或鉴权响应；含秘密的 Compose 输出
仍只进入原私有校验文件。开发检查另用 `tools/run_checks.py`，不与构建串联完整 pytest。

- 打包入口固定为 `tools/build_docker_bundle.py`，容器验收入口固定为 `tools/verify_stack.py`。不要每次手写另一套 build/save/load 命令或临时跳过验收。
- 流程依次为：输入检查 → wheel 准备 → 镜像构建/load → 临时归档/save、校验及重新导入 → 隔离容器验收 → 更新 `release/`。不同阶段可以有不同错误，报告必须指出失败阶段和日志路径。
- “镜像已构建并载入 Docker”不等于交付成功。验收前已经可能在 `.local/bundle-*/delivery/` 生成临时 TAR；只有全部通过才晋升至 `release/`，失败保持原交付包和配套文件不变。
- Windows 使用 WSL Docker 时，三个固定入口（打包、容器验收、同步探针）都用 `tools/docker_runtime.py` 保持附着的 WSL 进程，覆盖 Windows 侧的压缩、Dart 客户端及等待阶段。任务结束或失败后关闭自己持有的管道并回收进程，不修改 `.wslconfig`，不关闭整台 WSL，也不依赖人工一直开着终端。
- WSL 冷启动就绪握手最多 180 秒，辅助进程最多 24 小时，清理等待有界。它不能阻止用户显式关机或外部强制终止 Docker；这类故障仍须报告，不能标成业务测试通过。此启动预算与业务收敛超时分别管理。
- 容器验收失败会打印 `.local/stack-<随机ID>/failure.json` 的完整路径，记录阶段、容器状态、退出码、OOM 标记和重启次数；同目录保留 `dart.log` 和 `docker.log`。状态快照不包含容器环境变量、密码或私钥。诊断记录在测试清理前采集。

2026-10-27 排查：两次本机验收分别停在 `payroll_records deleted in personal ledger` 与 `business_preferences winner`。WSL journal 显示 Docker 引擎先于客户端超时被 systemd 停止（10 月 26 日 23:34:55、23:48:41，Asia/Shanghai），客户端随后持续收到 502。该证据定位到本机验收环境提前关机，不能据此认定某张业务表的删除失败，也不能用来解释真实 NAS 上的 API 容器意外重启。[Microsoft 文档](https://learn.microsoft.com/en-us/windows/wsl/systemd) 明确说明 systemd 服务本身不会维持 WSL 实例运行；固定入口须管理任务期间的运行时生命周期。

## 更新原项目

1. 在受控维护窗口审查迁移和备份；本次本地构建不会访问或备份真实 NAS。
2. 导入新的 FastAPI 镜像包，用配套 Compose 更新原项目。PostgreSQL、PowerSync 的版本、凭据和数据卷保持不变，不删除卷重试。
3. API 启动先迁移至本版本的 `head`，再执行 `app-data register-applications`，移除迁移凭据后以运行角色启动。旧资料一次性导入已退出启动流程，打包器清理旧导入参数和源目录挂载，保留已发布资料卷及应用存储配置。旧会话可能需要重新登录，不清空客户端缓存或离线队列。
4. 应用配置统一使用 `APP_DATA_APPLICATIONS`。各 Web 应用使用不同 origin；沿用原配置中仍适用的网页来源与文件根，不能把多个应用浏览器来源混成一个。
5. 同步配置由已安装模块生成，包含应用签名与数据集成员校验，不能沿用缺少应用隔离的旧规则。

API 服务的 `/health` 用于健康检查；新应用使用 `/v1/apps/<app_id>/...`。根路径、`/index.html`、`/main.dart.js` 及网页路由均返回 404，这是纯 API 的预期行为。

原先直接打开 API 地址看到的折日页面不再存在。后续独立 Web 服务应尽量保留原网页 origin，避免改变浏览器本地存储分区，并提供同源 `/api/` 网关：移除 `/api` 前缀后转发到 API，保留 Cookie、Set-Cookie、Origin、CSRF 和流式响应，不缓存认证请求。独立前端部署不在本次镜像构建范围。

## 验收与边界

默认本地验收使用临时容器与临时卷，检查启动前迁移失败拦截、应用注册、三个服务健康状态、API 健康和公钥、未登录访问边界及纯 API 镜像不含 `/app/web`。完整验收需显式选择，另行覆盖登录/刷新/退出、跨账号及跨应用隔离、文件权限、SQLite 客户端离线同步、冲突及容器重启持久化。报告区分 `artifact-and-startup` 与 `full-business`；业务测试未运行不能称为通过。

模拟 DSM 和 HTTPX 协议验证不能替代真实 NAS 登录、浏览器同源网关或 Android/Windows 实机验收。容器镜像构建成功、已上传、已部署分别报告。不要将导入镜像描述为数据库自动回滚；迁移后回退需要先停止写入并审查备份与版本。
