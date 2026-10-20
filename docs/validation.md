# 验证状态

日期：2026-10-20。PostgreSQL 基础已提交并合入 develop；当前在
feature/powersync-sync 验证固定支出的最小双向链路。

## 已验证

| 项目 | 结果 |
| --- | --- |
| 真实数据库 | Ubuntu WSL 内 Docker，PostgreSQL 17.11 bookworm，Linux amd64 |
| 数据库迁移 | 新装至 0003、已有开发库从 0002 升级、重复执行至 head |
| Python 检查 | ruff 检查和格式检查通过 |
| Python 测试 | 70 项通过，没有跳过数据库测试 |
| 数据隔离 | 未认证、其他账号、其他应用、连接池身份残留和跨账号导入映射负例通过 |
| 事务与幂等 | 中途失败全部回滚，同批/并发重复导入只有一次创建 |
| 精度与版本 | 十进制原值、非法金额、服务器版本递增和过时条件更新保护通过 |
| 持久化 | 开发容器强制重建后，样本及导入批次仍存在，重导返回 created=false |
| 群晖配置生成逻辑 | 在隔离 Linux 容器验证自包含启动和三类角色创建；未访问 NAS |
| Dart 依赖 | PowerSync 2.5.0、Drift 2.34.3、drift_sqlite_async 0.3.1、SQLite3 3.5.1 |
| Dart 分析 | 无问题 |
| 客户端离线实验 | Windows Dart VM 的 4 项测试通过 |
| Web 编译 | 最小实验成功编译 JavaScript，未进行浏览器运行验收 |
| Git Flow | main 初始化、feature 开发、--no-ff 合入 develop，下一阶段使用新 feature |
| 实际同步服务 | PowerSync 1.27.0；独立 PostgreSQL bucket 库；Sync Streams edition 3 / storage version 2 |
| 真实同步客户端 | Windows Dart VM；三个独立 SQLite 文件：同账号两个，另一个账号一个 |
| API | RS256/JWKS、短期同步令牌、幂等上传、冲突双方版本和选择接口 |

pytest 有一条 Starlette TestClient 建议迁移 HTTPX2 的依赖弃用警告，不影响当前测试。
依赖均已锁定，没有通过过滤警告代替兼容性验证。

4 项客户端实验分别验证：

1. 无登录/无服务连接时写入，关闭再打开保留数据、客户端 ID 与待上传队列。
2. 离线编辑和删除，关闭再打开仍保留待上传操作。
3. Drift 经连接适配读取 PowerSync 的表视图，避免重复建表；关闭 Drift 不误关 SDK。
4. 未连接服务时，PowerSync watch 查询仍响应本地更改。

## 真实链路结果

通过 tools/run_sync_probe.py 实际运行 API 和 PowerSync，不以 mock 代替复制：

1. 离线新增，关闭重开保留数据、client_id 和队列，再继续离线修改。
2. 上传经过 FastAPI → PostgreSQL → PowerSync → 另一客户端，金额保留 59.10。
3. 故意在服务端提交后丢弃响应，SDK 重试，回执保证业务版本只增加一次。
4. 同设备的离线连续修改正确串联；两端基于旧版本并发编辑，保存双方内容。
5. API 选择候选后，两端收到版本 4 及选定内容。
6. 离线删除重连后，两端记录消失；PostgreSQL 为版本 5，保留软删除标记。
7. 另一账号始终没有收到这条记录；同账号未收到其他应用数据集中的样本。

实验结束清理本次临时容器、源库、bucket 库、存储角色和复制槽。
实际使用的官方镜像 amd64 manifest digest：
`sha256:dd237fb1388ede384d8cb7aa5964bce8bf408fd7181ed52c9483d0a507378aa8`。

## 尚未验证 / 尚未实现

- 真实群晖部署、NAS CPU 镜像架构和目录权限。
- DSM 真实账号登录/换令牌、会话刷新、退出与重新登录后的本地账号隔离。
- API 容器、网关和完整群晖交付。
- Android SDK 运行、完整 Windows Flutter 应用和浏览器离线/多标签运行。
- Drift watch 通过下载事件自动刷新的完整路径。
- 现有折日全部业务接入、冲突选择 UI、正式数据迁移。
- 工资/历史/排序等其余业务，生产复制中断与 WAL 上限触发后的维护流程。

备份按用户要求暂不实现，不列为首版阻塞。

折日现有应用代码、本地库和 WebDAV 路径均未修改。本阶段不生成 APK/EXE、
不启动 Web 预览、不上传制品或部署 NAS。三端同步完成前不切换正式数据集。
验证结束已停止本任务的开发/探测容器及临时 WSL 保活进程，保留数据库卷和
本地开发配置；下次可按 README 的 Compose 命令重新启动。

## 后续接入

独立最小链路已通过，下一步进入折日新数据层、设置入口、现有身份适配和三端运行验证。
实验不等于折日已经接入，不把实验库直接当作当前业务库。
