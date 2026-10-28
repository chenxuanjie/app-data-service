2026-10-28 移除已完成的旧资料导入启动检查：

- API 启动命令保留数据库结构升级、应用注册及维护变量清除，移除旧资料自动导入调用。配套生成器和打包器同步移除导入环境参数、只读源目录挂载及导入专用健康检查宽限；已发布资料卷、其他应用配置和凭据保持原值。
- ruff check、127 个 Python 文件的格式检查及真实 PostgreSQL 完整 **314 项 pytest 通过**，没有跳过数据库测试；保留已有 Starlette TestClient 弃用提示。新增回归覆盖长/短格式源挂载清理、自定义资料卷保留和重复执行不改变配置。
- 从现有交付 Compose 生成私有 `.local/board-library-runtime.yaml`，逐项确认除启动钩子清理外配置不变，真实 Docker Compose `config --quiet` 和 Linux `sh -n` 通过。保留现有镜像标签、数据库/签名凭据、应用存储和数据卷。未改写旧交付文件及其来源摘要。
- 本项仅更新 Compose 即可停用旧镜像的导入启动调用。未构建镜像、提交、上传或部署，未修改 NAS 或生产数据库。

2026-10-27 Compose 自动导入与小文件原文 SQL 保留：

- 最终 ruff check 和 115 个 Python 文件格式检查通过；真实 PostgreSQL 随机隔离库完整 **278 项 pytest 通过**，无数据库跳过，保留已有 Starlette TestClient 弃用提示。
- 覆盖启动自动导入/发布、逐文件/桌游日志、完成后不扫描源目录、并发导入锁、更换空资料卷恢复原件、SQL 大小阈值、源文件名参与导入身份、原始字节/GB18030 文本/JSON 保留、未知字段和缺文件保留，以及源/原文 RLS 和配置脱敏。
- Y 盘只读核对：`friend/apps/board_game_agent/board-game-lib` 含 24 个桌游、372 条原清单登记，49 份目录/游戏/清单 JSON 可读且身份对应无误；未对 NAS 大文件做全量摘要，也未修改或导入真实数据。
- 本机完整 CLI 预览：24 个条目、374 条资源、421 个去重文件对象，其中 402 个符合默认 4 MiB SQL 原文阈值，0 阻断错误。原始元数据 JSON 和 ASSET_SPEC.md 已纳入文件对象保留范围；实际 NAS 入库数量以新镜像运行日志为准。
- 本地 `.local/board-library-input.yaml` 保留 NAS 数据库/身份设置及待交付 Loomex 配置，使用只读资料源和专用命名卷。真实 Docker Compose `config --quiet` 和 Linux `/bin/sh -n` 验证通过，没有输出秘密；没有启动生产容器或构建镜像。
- Dockerfile 已为新卷准备 UID/GID 10001 所属目录，不改变 NAS ACL/所有者；实际新镜像启动、命名卷初始化及 NAS UID 读取权限尚未验收。源码、构建输入和文档未提交、未构建、未上传或部署。

2026-10-27 SQL 资料库与统一发布服务基础：

- ruff check、111 个 Python 文件的格式检查通过；真实 PostgreSQL 随机隔离测试库完整 **271 项 pytest 通过**，没有跳过数据库验证。保留已有 Starlette TestClient 弃用提示。
- 新增资料库验收覆盖元数据/历史保留、未知字段阻断、无写入预览、草稿不可见、失败不切换版本、幂等重试、陈旧草稿拒绝、独立条目合并、RLS 与应用停用、历史文件/撤下、PDF Range 和条件读取。原业务同步流没有变更，本轮没有重新执行 PowerSync SDK 探针。
- 使用实际 CLI 对本机桌游工程执行只读预览：24 个游戏/版次、48 份语言内容、374 条资源、371 份去重文件、49 份源 JSON，0 个阻断错误；2 个未登记文件作为归档保留，2 条原有缺失 PDF 登记保留为警告。仅预览，没有将真实资料写入 SQL 或内容存储。
- 未切换 Flutter 客户端，未执行真实 NAS 导入、平台/镜像构建、提交、推送、上传或部署。协议、维护命令及后续边界见 [SQL 资料库](library-catalog.md)。

2026-10-27 固定 Docker 入口运行时修复：

- 两次失败记录与 WSL journal 对照：本机 Docker 在客户端超时前被 systemd 停止，所有测试容器同时关闭，随后 Dart 持续收到 502；分别停在工资删除收敛和偏好冲突胜出检查。该证据不用于判定真实 NAS 的重启原因。
- 打包、容器验收和独立同步探针使用任务所属的附着式 WSL 进程；退出时释放管道并有界回收，不改 WSL 全局配置、不终止其他任务。并行冷启动首次遇到 20 秒就绪超时，启动握手预算调整为独立的 45 秒；业务收敛预算保持不变。
- 新增真实子进程 EOF 清理、异常清理、启动无响应/错误、原生 Docker 不启动 WSL、诊断失败及脱敏快照测试。
- 最终 ruff 检查、101 个 Python 文件的格式检查及真实 PostgreSQL 完整 **252 项 pytest** 通过；保留一条已有 Starlette TestClient 弃用提示。
- 使用本机已有 `app-data-service:0.5.0-30d5f3f-20261026154731` 镜像验证修复后的容器验收脚本，完整 11 项实际 SDK 同步场景通过，含工资/资产分配/业务偏好冲突与删除收敛；迁移失败拦截、应用隔离、账号文件、容器重建及会话保留通过。报告 `.local/wsl-runtime-stack-validation.json`；未编译新镜像、未晋升交付包。
- 修改仅为构建/测试工具、测试和说明；尚未提交、上传或部署，未修改真实 NAS、运行时业务源码或生产数据。

下列为此前阶段记录。

2026-10-26 桌游阶段 2、3 开发验证：

- ruff check、ruff format --check 通过；真实 PostgreSQL 随机隔离库完整 **245 项 pytest** 通过，无数据库跳过；保留一条已有 Starlette TestClient 弃用提示。
- 0012 新增类型化私人表、RLS、回执、社区/统计和公开资料清单；验证账号/应用拒绝越界、重放幂等、偏好字段合并、评分删除与同时间游标分页。
- 实际 PowerSync 1.27.0 + API + 桌游客户端三个独立 SDK 数据库联调通过；同账号两份工作区离线收敛、另一账号隔离、UTC 时间和聊天同步通过，不同步本机 Run checkpoint。
- 桌游静态分析无问题、完整 573 项回归通过。临时容器、库、角色与复制槽已清理。
- 本记录随阶段 2、3 源码提交保存；未构建镜像、上传或部署，未修改 Zheri 客户端、真实 NAS、账号权限或生产数据。当前安装包和 API 镜像不包含本轮代码。契约与部署前提见 [桌游业务](board-game-business.md)。

下列为此前阶段记录。

# 验证状态

## 2026-10-26：首次登录冷响应预算修复（源码修复，未打包）

- 原实现真实 HTTPS 隔离复现：模拟 DSM 首次登录响应 9 秒，首次约 8.1 秒返回 503 `provider_unavailable`，紧接着第二次约 0.02 秒成功。该复现确认现有预算能产生用户报告的模式，不代表已取得真实 NAS 的失败阶段日志。
- 登录期间的 DSM 读取预算改为 20 秒、单步总预算 25 秒；认证路由仍保留 30 秒整体预算，客户端 HTTP/认证仍为 40/45 秒。普通刷新、文件与退出维持原先短预算；不自动重发密码、不重试登录 POST。
- 超时明确返回 504 `provider_timeout`，其他服务不可用保持原分类。阶段日志只记录发现、登录、身份校验、退出或创建应用会话，以及安全原因和耗时，不输出账号、密码、SID、Token、URL 或原始异常消息。
- ruff check/format 通过，完整 **238 项 pytest** 通过（真实 PostgreSQL 随机隔离库，无跳过），包含真正的回环 HTTPS 9 秒冷登录/冷身份校验、硬超时、正常请求预算不变、30 秒整次预算与诊断脱敏。
- 桌游客户端补充 `provider_timeout` 分类，16 项账号相关测试及静态检查通过。现有 `0.5.0` 镜像和客户端包不包含本次修复；尚未构建、上传或部署，真实 NAS 首次登录需更新后复测。

下列为此前阶段记录。

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
