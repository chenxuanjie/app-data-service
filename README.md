> 当前开发源码包含桌游个人/社区业务，以及 [SQL 资料库、一次性导入与统一发布](docs/library-catalog.md)。资料库需要 0014 迁移，Flutter 资料读取尚未切换；本轮没有构建或部署镜像。社区与私人同步见 [桌游业务契约](docs/board-game-business.md)。以下为此前阶段记录。

# Flutter 应用数据服务

GitHub Actions 自动构建 API 并发布到 GHCR，镜像标签及群晖拉取更新方式见
[GHCR 构建与更新](docs/ghcr.md)。本地离线镜像归档流程继续保留。

游客 APK/EXE 升级与独立机器发布权限见 [公开安装包发布](docs/public-releases.md)。修复后的契约需要迁移 0016、新 API 镜像和完整发布 Compose；通用 Android 架构统一为 any。WebDAV 备用发布保留，未自动构建或部署。

当前功能分支 `feature/multi-app-foundation` 已实现多应用模块化基础，尚未构建新镜像或部署。
公共账号、会话、权限和数据空间与各应用业务分离；Zheri 客户端不在本轮修改范围，桌游客户端已在独立分支接入账号和阶段 2、3 业务。
源码包元数据仍为 0.5.0，尚未形成新发布版本。新增迁移 0010/0011 和应用配置需与 API 镜像、同步规则配套上线。

- [架构与目录](docs/architecture.md)
- [应用/API/部署契约](docs/application-scope.md)
- [接入新应用](docs/adding-an-application.md)
- [本地验证范围](docs/validation.md)

此前版本新增[账号文件与更新 API](docs/account-files.md)及[同源浏览器会话](docs/browser-sessions.md)，尚未部署本轮源码。
已验收的六类业务 0.3.0 见 [全业务接口与升级说明](docs/complete-business-sync.md)。
折日六类业务统一采用 PostgreSQL + FastAPI + PowerSync，客户端保留 SQLite 离线副本；旧 WebDAV 业务同步已退役。

已完成 PostgreSQL 基础、PowerSync + FastAPI 容器链路与折日六类业务同步接入。
应用以离线使用为主，服务器负责同步；本项目不要求手机直接连接 PostgreSQL。

已实现：

- PostgreSQL 17.11、开发 Compose、群晖自包含 Compose 生成器。
- 固定支出业务表、个人账号/数据集、RLS、只读复制角色、数据库迁移。
- 旧固定支出 JSON 的严格转换、导入预览、同批幂等导入、事务和对账记录。
- PowerSync 的明确表 publication、逻辑复制配置，没有闲置复制槽。
- 独立的 PowerSync/Drift 离线兼容实验，不接触现有折日数据库。
- FastAPI 的 RS256/JWKS 验证、短期同步令牌、幂等上传、冲突双方版本与选择 API。
- PowerSync 1.27.0 + PostgreSQL bucket 存储的本地真实链路实验，包含两个同账号客户端及另一个账号。

已加入 DSM 登录、会话刷新/退出、单入口同步网关、三服务自包含 Compose 和镜像导入包。
本地完整 Docker 验证使用严格 TLS DSM 替身；真实 DSM 和 NAS 部署仍待验收。
已实现显式迁入与原地确认个人账本，不自动操作真实账号或 NAS。当前没有备份、共享账本或管理后台。
`seed-demo` 只创建本地测试身份，不代表通过了 DSM 登录验证。
Git 按 [Git Flow](docs/git-flow.md) 开发：`main` / `develop` / `feature/*`。
PostgreSQL 基础已在 `feature/postgres-foundation` 提交并合入 `develop`；
当前工作分支为 `feature/multi-app-foundation`，尚未创建生产发布版本。

## 本地开发

需要 Python 3.12～3.14、Docker Compose。先建立独立虚拟环境并安装锁定依赖：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python tools/bootstrap_dev.py
```

生成的 `.local/dev.env` 和 `.local/dev-config.json` 含随机开发凭据，均被 Git 忽略。
工具拒绝覆盖已有凭据。不要把开发配置发送给其他人或作为生产配置。

```powershell
docker compose --env-file .local/dev.env -f deploy/compose.dev.yaml up -d --wait
.venv\Scripts\app-data --local migrate
.venv\Scripts\app-data --local check
.venv\Scripts\python tools/run_checks.py
```

若 Docker 位于 Ubuntu WSL，可将 Docker 命令改为：

```powershell
wsl -d Ubuntu --exec docker compose --env-file /mnt/c/Study/MyCode/MyProject/apps/app_data_service/.local/dev.env -f /mnt/c/Study/MyCode/MyProject/apps/app_data_service/deploy/compose.dev.yaml up -d --wait
```

Docker 必须先运行。`run_checks.py` 固定使用 Windows 项目 `.venv` 运行，并持有 Ubuntu WSL 保活租约，依次检查
Docker、数据库 TCP/认证、ruff 和完整 pytest；本机 Docker 固定使用 Ubuntu WSL，不临时改用 WSL Python 跑 pytest。
数据库不可达立即停止，不自动修改连接地址或切换数据库。每阶段输出与耗时保存在
`.local/task-logs/`，失败时保留日志，pytest 默认首错停止；完整测试预算 2 小时，ruff 每阶段 10 分钟；不因几分钟无输出取消仍在运行的任务。
入口分开检查 WSL 命令通道、Docker API、开发 PostgreSQL 健康、WSL 内部端口、Windows
连接及数据库认证。Docker 停止时仅启动本机已安装服务；开发数据库停止时校验 Compose
身份后仅启动现有容器，不新建数据库卷。冷启动等待健康检查，而不是看到 WSL 握手就
立即连接。若 WSL 内部 55432 可达而 Windows 原生转发不可用，且 Windows 端口空闲，
本次任务会建立 `127.0.0.1:55432` 到同一 WSL 数据库的 stdio 转发，任务结束清理；
不修改数据库 URL、持久配置、防火墙或 WSL 网络模式。端口被占用、服务启动权限不足、
容器身份不符或 WSL 本身无响应时，明确报告故障层，不杀未知进程、不重启整台 WSL。

直接执行 `pytest` 也会在数据库前置条件失败时结束会话；需要收集所有业务失败时可显式
使用 `--maxfail=0`。运行其他 Windows 数据库工具时仍需保持 WSL 活动。
若当前网络的 Docker 拉取受限，先通过已有可信镜像导入方式取得同版本官方镜像。

`pytest` 会在这个本地实例创建随机命名的临时测试数据库，完成后仅删除该测试库，
不会清空 `app_data`。没有真实 PostgreSQL 时数据库测试会失败，不会静默跳过。

## 固定支出样本导入

```powershell
.venv\Scripts\app-data --local seed-demo
.venv\Scripts\app-data preview-expenses tests/fixtures/expenses.json
.venv\Scripts\app-data --local import-expenses tests/fixtures/expenses.json --user-id dev-alice --dataset-id dev-zheri
```

预览不会写数据库。相同批次重跑返回 `created: false`；记录 ID 已存在但内容变化时
导入会失败并整体回滚，不覆盖已有业务记录。这里只导入固定支出，不能用它宣称整个
折日数据集已经迁移。输入上限 8 MiB/10000 条，字段未知或金额精度超限会明确拒绝。

导入在数据库事务内运行；`--user-id` 只接受持有维护凭据的操作者输入，不能作为
未来 HTTP API 的身份参数。API 必须先验证账号，再设置事务内用户作用域。

## 群晖部署配置

本阶段请使用 [群晖测试部署与验收](docs/synology-test.md) 的完整服务包。
其中包含 PostgreSQL、API 和 PowerSync；数据库迁移由API启动命令先行执行，不单列停止容器。
不需要在 NAS 安装 Python。
下面保留的 `render_synology.py` 是早期仅数据库配置，不用于本轮完整同步测试。

```powershell
.venv\Scripts\python tools/render_synology.py --data-directory /volume1/docker/app-data-service/postgres
```

路径是示例，实际使用前核对 NAS 卷和目标目录。生成的 `deploy/compose.synology.yaml`
内含随机部署凭据，已被 Git 忽略；不依赖 `.env`、源码挂载或 NAS 上的 Python。
重复执行不会覆盖已有部署配置或旋转数据库密码。

管理员事先创建指定目录，然后可在 Container Manager 项目中使用该 YAML。
数据库端口默认不对宿主机开放，凭据与迁移通过授权维护环境管理。当前配置负责
数据库启动，业务表仍需执行本仓库迁移；尚未提供 NAS 上的一键业务服务安装包。
不得将开发连接或未完成的同步功能描述为生产部署。

角色：

| 角色 | 用途 |
| --- | --- |
| app_admin | 仅初始化和本地隔离测试 |
| app_migrator | 结构升级和受控测试数据准备 |
| app_runtime | 已验证用户作用域内业务读写，无 DDL 或硬删除权限 |
| app_replication | 只读指定业务表、复制，无业务写权限 |

持久数据与容器分离，普通容器重建保留数据。首版按用户要求不实现备份功能。

## 客户端离线兼容实验

```powershell
cd experiments/powersync_probe
dart pub get
dart analyze
dart test
dart compile js bin/web_compile_probe.dart -o ../../.local/web_compile_probe.js
```

实验锁定 PowerSync 2.5.0、Drift 2.34.3、drift_sqlite_async 0.3.1、SQLite3 3.5.1。
离线测试验证本地写入、队列持久化、重启和查询适配；真实链路按下一节运行。
JavaScript 编译通过不能代替浏览器运行、Android 或完整 Windows 应用验收。

## 最小双向同步实验

先启动开发 PostgreSQL、安装更新后的锁定依赖，并在实验目录执行 `dart pub get`。
本地 Docker 需要固定版本的官方镜像 `journeyapps/powersync-service:1.27.0`。
容器使用现有开发网络，PowerSync 的 source / bucket 是两个隔离临时数据库。
FastAPI 在宿主机回环地址运行，不要求 Docker 中另装 Python。

```powershell
docker pull journeyapps/powersync-service:1.27.0
.venv\Scripts\python tools/run_sync_probe.py --docker native --dart dart
# Docker 位于本机 Ubuntu WSL 时，改用 --docker wsl
```

实验端口为 `127.0.0.1:18280`（API）和 `127.0.0.1:18290`（PowerSync），
占用时明确报错，不停止其他程序。只绑定回环地址，不启动折日 Web 预览。
工具生成随机签名密钥和临时应用令牌；没有“提交任意用户 ID 就登录”的 HTTP 端点。
PowerSync 实验使用静态公钥，避免容器访问宿主机的额外网络配置；正常服务配置
保留 JWKS URI。正式服务已提供 DSM 验证后换取应用会话的路径；此旧探测工具仍使用维护端测试身份。

验证：离线新增与连续编辑 → 关闭重开 → API 上传 → PostgreSQL 逻辑复制 →
另一个 SQLite 客户端。还模拟已提交响应丢失、重复上传、同条记录两端并发、
保留双方版本、选择候选后下行、离线删除重连，以及账号/应用下行隔离。
上传金额必须是十进制字符串；数据库负责版本和修改时间。

实验结束清理本次临时容器、两库、存储角色和复制槽，保留 `.local/` 下日志、
结果与本地 SQLite 供排查。不会删除 `app_data` 或开发卷，也不接触真实 NAS。
`result.json` 是结构化结果，日志和令牌均不得提交。

手动运行 API 的环境契约和上传规则见 [同步 API](docs/sync-api.md)。
该实验不是折日的最终数据层，也不是已交付的群晖一键安装包。

## 完整容器验证与交付

`tools/render_stack.py` 生成完整测试配置，`tools/build_docker_bundle.py` 构建镜像、
分别导出三个镜像为独立 tar.gz、逐包重新导入并运行 `tools/verify_stack.py`。只有验证通过才替换交付文件。
交付集中在 `release/` 根目录，包名为 `fastapi-<API服务版本>.tar.gz`、`postgresql-17.11.tar.gz`、`powersync-1.27.0.tar.gz`，报告收在 `metadata/`，后续仅API更新使用 `--api-only`，无需重传未变化的数据库与同步镜像。
验证器使用随机隔离的容器、卷、数据库及严格 TLS DSM 替身，不访问真实 NAS。
API 镜像以 UID 10001 运行，构建上下文不含 `.local`、密钥或本地数据库。
0.5.0 起为纯 API 镜像，不包含折日或其他应用的网页，不要求 Flutter/Web 构建输入。
各应用 Web 独立部署；同源网关将 `/api/` 转发到后台并移除该前缀，API 自身不再提供页面或此别名。
当前交付操作和升级边界见 [纯 API 交付说明](docs/release-guide.md)。
本次具体文件、重建方法和手动验收步骤见 [群晖测试部署与验收](docs/synology-test.md)。

参见 [数据字典](docs/database.md)、[同步 API](docs/sync-api.md) 与 [验证状态](docs/validation.md)。
