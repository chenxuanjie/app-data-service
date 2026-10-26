# 验证状态

2026-10-26 多应用模块化收尾验证：

- ruff check、ruff format --check 通过；完整 **243 项 pytest** 通过，无跳过数据库测试，使用真实 PostgreSQL 17 随机隔离测试库。
- app_client 格式、flutter analyze 与完整 **39 项测试** 通过。
- PowerSync 1.27.0 真实 NDJSON 检查 **8 项**通过，含第三应用自身下载、跨应用主动订阅拒绝及跨账号隔离。
- 第三“阅读笔记”测试模块覆盖独立业务表/读写、共同身份、独立空间、Cookie/CSRF、文件、更新清单及退出。
- 覆盖模块禁用、错误路由绑定、元数据不一致、并发幂等注册和不需要个人数据的应用。
- 旧数据库升级覆盖主数据集 ID、会话密文、业务记录、回执哈希与重试去重保留；回执表原地归入 zheri schema。

临时数据库、容器、角色与复制槽已清理。测试保留一条 Starlette TestClient/httpx 弃用警告。
未修改 Zheri 客户端、真实 NAS、生产数据或权限；未推送、构建交付镜像、上传或部署。
2026-10-26 阶段 0 提交前复审：ruff check/format、243 项真实 PostgreSQL 测试及 app_client 分析/39 项测试再次通过。
桌游三端尚未接入新后端。新架构、扩展步骤和上线条件见 [架构](architecture.md)、
[新增应用](adding-an-application.md)、[应用契约](application-scope.md)。

下文为历史阶段记录，包含后来已移除的无应用签名令牌兼容行为，不代表当前协议。

2026-10-24 多应用基础：ruff check、ruff format --check、完整 233 项 pytest 通过，
数据库使用真实 PostgreSQL 17 的随机隔离测试库。包含同账号跨应用 RLS、独立刷新/退出、
浏览器 Cookie/CSRF、既有 Zheri 无应用头与历史令牌、0009 → 0010 的原地升级。
app_client 分析及 39 项测试通过。PowerSync 1.27.0 真实 NDJSON 下载验证 5 项通过，
包含伪造应用参数与主动订阅拒绝越界；测试临时数据库、容器、复制槽已清理。
本轮对 Zheri 源码的试验性改动已全部撤回，其他任务改动原样保留，未将其列为本轮实现。
仍有一条 Starlette TestClient/httpx 弃用警告。
未修改 NAS、生产数据或权限，未提交、打包交付、上传或部署；桌游三端尚未接入新协议。

> 下文保留早期阶段记录。已验收业务版本为 API 0.3.0，见 [全业务同步](complete-business-sync.md)。当前功能分支源码为 0.5.0，统一业务协议，见[当前同步契约](current-sync.md)，尚未打包或部署 NAS。

2026-10-22 残留清理：ruff check / format --check、完整 193 项真实 PostgreSQL 测试、
独立 Dart + API + PowerSync + PostgreSQL 的 11 项链路检查通过。迁移至 0008；旧数据身份、历史回执和队列保留。
折日 192 项测试、Web Release，新 UI 公共包 11 项测试，以及浏览器 IndexedDB/OPFS 持久化检查通过。
本轮无 NAS 部署或 APK/Docker 交付；下文为历史记录。

2026-10-22 第二阶段本地验证：ruff check、ruff format --check 和完整 174 项 pytest 全部通过，
数据库使用独立随机测试库中的真实 PostgreSQL，没有跳过数据库测试。DSM 使用 HTTPX 替身，
覆盖按当前账号 SID 读取、权限拒绝、退出撤销、只读路径、Range/错误响应和槽位释放。
测试工具存在一项 Starlette TestClient/httpx 弃用警告，不影响本次结果。
客户端验证：app_client 分析与 18 项测试，折日分析、完整 174 项测试及 Web Release 构建通过。
真实 NAS 文件服务、同源网关、Android 系统安装器本轮未实机验证。

## 以下为历史记录

更新：2026-10-21。PostgreSQL 基础已合入 develop；完整同步与部署工作位于
feature/powersync-sync。折日位于 codex/data-service-sync，新增独立测试入口。
真实 NAS、真实 DSM 和三端运行验收仍待完成；此记录不代表生产迁移完成。

## 已通过

| 项目 | 实际覆盖 |
| --- | --- |
| PostgreSQL | Ubuntu WSL Docker，17.11 bookworm，linux/amd64；新装/升级至 0004，重复迁移 |
| Python | 86 项 pytest 通过，包含真实数据库，没有跳过数据库测试；ruff 检查及格式检查通过 |
| 权限 | RLS 账号/应用隔离、无身份拒绝、连接池作用域清除、跨账号导入拒绝、会话表不复制 |
| 事务 | 失败回滚、并发幂等导入、精确十进制、版本递增、过时修改保护、响应丢失重试 |
| 身份实现 | DSM 发现/登录/OTP/UID验证/退出，HTTPS固定源、请求级Cookie和Token；HTTP替身与严格TLS容器替身，未用真实DSM |
| 应用会话 | 加密DSM会话、刷新哈希、过期/撤销、刷新验证身份、容器重启保留会话及签名密钥 |
| 完整部署 | PostgreSQL → API内先迁移后启动 → PowerSync，单来源流式网关和JWKS；三个镜像分别gzip压缩/逐包重新导入后验证 |
| NAS创建阶段反馈 | 用户截图先报绑定源目录不存在，再报CPU CFS配额不支持；已对齐目录并移除CPU配额；后续截图显示三个业务容器运行，旧迁移容器停止导致项目部分运行，新三服务布局待NAS确认 |
| 同步服务 | PowerSync 1.27.0，独立PostgreSQL bucket库，Sync Streams edition 3 / storage version 2 |
| 同步客户端 | 三个 Windows Dart SQLite 客户端：同账号两个、另账号一个，经真实API/数据库/复制/网关 |
| 持久化 | PostgreSQL容器强制重建，版本5软删除仍存在；API重启会话有效；退出后旧API令牌401 |
| Flutter | 新PowerSync独立本地库、待上传队列重启、账号本地作用域、退出隐藏并保留队列；完整92项测试 |
| Flutter检查/构建 | flutter analyze、完整flutter test、Web JavaScript Release、本地Android Release APK通过 |

pytest 有一条 Starlette TestClient 关于 HTTPX2 的依赖弃用警告，未屏蔽。
Flutter Web 的 Wasm dry run 提示既有 flutter_secure_storage_web 的 dart:html 等依赖不兼容
Dart Wasm；本次通过的是 JavaScript Release，不宣称 Wasm 模式通过。

## 两层真实同步验证

早期 `tools/run_sync_probe.py` 在开发实例创建临时源库/bucket库，API运行于本机。
本次 `tools/verify_stack.py` 另外验证完整镜像链路，DSM使用带独立CA的严格TLS替身，
其要求Cookie与Token头，模拟只在正文传会话会失败的真实DSM差异。
两者都使用真实 PowerSync、PostgreSQL 和本地 SQLite，未模拟复制。

1. 离线新增、关闭重开、连续修改后重连上传和下行，金额 59.10 保持精度。
2. 服务器已提交但响应丢失，SDK重试不重复增加业务版本。
3. 同设备操作链正确衔接，其他设备修改打断旧版本覆盖，保存双方候选。
4. 查看当前服务器版本后选择候选，两端收到选定内容。
5. 离线删除重连，两端收敛；数据库版本5且保留软删除标记。
6. 其他账号不能下载该记录；原探测还覆盖其他应用数据集过滤。
7. 完整容器重建、会话恢复与退出撤销的实际HTTP验证。

工具清理自身随机容器/卷/网络；早期探测还清理临时数据库、角色和复制槽。
交付方式已调整为三个独立tar.gz；每个文件验证标签、架构、镜像ID、SHA-256并实际导入。
部署包的 sourceTreeSha256 指向镜像实际源码；构建时 dirty 标记属实，不伪造干净提交来源。
交付文件与验收矩阵见 [群晖测试部署与验收](synology-test.md)。

## 尚待验收或实现

- 真实 NAS 的镜像运行、目录权限、资源消耗、反向代理、证书和 DSM 真实登录/OTP/刷新。
- Android设备实际同步，完整Windows Flutter应用及浏览器离线/多标签运行。
- UI的真实软键盘、TalkBack、系统手势返回与对比度测量；widget测试不能代替设备证据。
- Drift watch 经下载事件自动刷新的完整旧业务集成。本次测试页直接使用PowerSync查询，不代表全量Drift迁移。
- 工资、物品、历史、排序等其余业务与正式数据迁移。
- 生产密钥轮换、复制中断/WAL上限触发后的维护流程。

首版不实现备份。折日旧本地数据库、业务页面和WebDAV继续使用原路径；
新增入口只写独立测试库，没有自动导入或新旧双写。
本次生成本地APK/TAR，不上传制品、不启动Web预览、不部署NAS；三端通过前不切换正式数据。
