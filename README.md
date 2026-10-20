# Flutter 应用数据服务

当前为 PostgreSQL 基础阶段，优先服务折日，后续接入 PowerSync 与 FastAPI。
应用以离线使用为主，服务器负责同步；本项目不要求手机直接连接 PostgreSQL。

已实现：

- PostgreSQL 17.11、开发 Compose、群晖自包含 Compose 生成器。
- 固定支出业务表、个人账号/数据集、RLS、只读复制角色、数据库迁移。
- 旧固定支出 JSON 的严格转换、导入预览、同批幂等导入、事务和对账记录。
- PowerSync 的明确表 publication、逻辑复制配置，没有闲置复制槽。
- 独立的 PowerSync/Drift 离线兼容实验，不接触现有折日数据库。

当前没有：NAS 登录 API、在线双向同步、生产数据迁移、备份、共享账本或管理后台。
`seed-demo` 只创建本地测试身份，不代表通过了 DSM 登录验证。

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
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m ruff format --check .
```

若 Docker 位于 Ubuntu WSL，可将 Docker 命令改为：

```powershell
wsl -d Ubuntu --exec docker compose --env-file /mnt/c/Study/MyCode/MyProject/apps/app_data_service/.local/dev.env -f /mnt/c/Study/MyCode/MyProject/apps/app_data_service/deploy/compose.dev.yaml up -d --wait
```

Docker 必须先运行。部分 WSL 配置在无前台 Linux 命令时会停止发行版；运行 Windows
数据库工具期间可在另一个终端保持 WSL 活动，不需要修改系统代理或 WSL 设置。
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
没有连接真实服务的代码；它验证本地写入、队列持久化、重启和查询适配。
JavaScript 编译通过不能代替浏览器运行、Android 或完整 Windows 应用验收。

参见 [数据字典](docs/database.md) 与 [验证状态](docs/validation.md)。
