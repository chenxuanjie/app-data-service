# SQL 资料库、一次性导入与统一发布

2026-10-27。后台已支持 Compose 启动时自动导入、发布及逐项日志，小文件原文可存入 SQL。Flutter 客户端尚未切换，真实资料未导入，未构建或部署新镜像。不是资料实时同步、OCR、搜索索引或编辑后台的完成声明。

## 数据归属与发布边界

资料属于 `board_game_agent` 模块；公共认证核心和 Zheri 业务不增加桌游分支。迁移 `0014_board_library` 新建 `board_game_agent.library_*` 表，不转换、清理旧数据，不更改原有私人/社区表。

| 表组 | 保存内容 |
| --- | --- |
| `games`、`editions` | 游戏身份、版本、排序、启用状态、玩家数、展示颜色、外部评分原文 |
| `texts`、`categories` | 多语言展示、别名、设计师、出版商、关键词、分类及助手提示 |
| `resources`、`derivations`、`bindings` | 文件登记、来源/版次、状态、AI 许可、派生关系、图片角色/尺寸和封面/正文绑定 |
| `files`、`file_bodies` | 摘要、大小、存储键；小文件的原始字节、可解码文本、有效 JSON 副本 |
| `source_files`、`import_runs` | 原始路径与文件对象的对应、启动导入完成标记、资料卷身份和导入策略 |
| `batches`、`sources`、`validations` | 导入报告、源 JSON 审计快照、历史检查时间/问题；只供维护连接读取 |
| `releases`、`entries`、`head`、`changes` | 不可变发布版本、每版采用的条目快照、当前版本和持久变化记录 |

以上均以 `library_` 为表名前缀。类型化表是新接口的数据源；原始 JSON 快照仅用于审计，不作为客户端读取路径或通用业务 JSON 存储。

现有一张游戏卡片映射为一条游戏及一条版次，保留当前 `id` 和 `slug`，不根据相似标题猜测合并不同版本。表结构已经分开游戏和版次；以后如需归并，同样需要明确身份映射，不改动私人收藏/评论的关联身份。本阶段维护工具接受现有资料结构，尚未提供独立的多版次编辑入口。

## 导入保留范围

- 读取旧 `assets/catalog.json`、每个条目的 `game.json` 和 `manifest.json`，映射已知字段；未知附加字段原样保存在源 JSON 和文件原文中并记录警告。身份冲突、不支持的结构版本、重复 JSON 键、非法必需字段和越级路径仍会阻断。当前资料为 catalog 2 / manifest 3。
- 禁用、`missing`、`superseded`、`blocked`、`aiEnabled: false` 和历史 validation 都保留，状态与“这次源目录是否找到文件”分别保存。无法解析的历史检查日期仍保留原文。
- 旧 `cover/background/gallery` 类型转为图片加 `image_role`，同时保留原类型；尺寸、变体、来源 URL、备注、派生引用均保存。无法解析的派生引用保留原值并警告，不猜测替代。
- ZIP、`docs/others` 和游戏目录内未登记文件作为归档保留文件和登记，不自动加入公开资料或 AI 输入。HTML 可以登记，但下载使用 attachment、nosniff 和 sandbox，不能在 API 源下执行脚本。
- 缺文件仍保存登记、原始状态和缺失观察结果，并记录警告，不阻断整批入库；没有文件对象的资源不进入公开下载列表。工具不自动下载缺失文件，源目录的缺失观察不能扩大为对其他位置的判断。
- `documents: null` 表示无文档绑定，原值保留在审计快照；顶层本地化字段若存在没有对应 locale 的语言会阻断，避免遗漏。未登记文件不会凭文件名获得 AI 权限。

扫描仅发生在明确执行的一次性导入计划中，范围是目录登记的游戏目录；不遍历 NAS，不建立运行时周期扫描。应用自身的字体、UI 图标和构建缓存不属于资料迁移范围。

默认 **4 MiB 以内的资料文件原始字节也进入 SQL**，包括小型图片、PDF、压缩包和归档文件；可解码内容另存 TEXT，合法 JSON 另存 JSONB，UTF-8/BOM、UTF-16 BOM 和常见文本的 GB18030 可识别。游戏元数据 JSON 与资料根目录的 `ASSET_SPEC.md` 等文件也保留。不能解码的文件仍保存原字节，不做 OCR 或 PDF 正文提取；保存原文不等于获得公开/AI 权限。

较大的文件继续保存在资料卷，数据库保存登记与摘要。全部原件仍复制到资料卷，以支持现有的 Range 下载与完整性检查；这是本阶段的完整保留策略，后续修剪另行实施，不能只备份数据库而遗漏大文件资料卷。

## 只通过 Compose 启动

使用包含本轮源码的新 API 镜像，以及已经接入启动钩子的配套 Compose；旧镜像不含命令，单独修改挂载不能替代更新镜像。维持 postgres、api、powersync 三个常驻服务，不创建一个已退出的迁移/导入服务。

已只读核对用户 Y 盘：实际资料根目录为 `Y:\friend\apps\board_game_agent\board-game-lib`，有 24 个桌游和 `assets/catalog.json`。按已记录的 NAS 共享卷映射，挂载源为 `/volume2/friend/apps/board_game_agent/board-game-lib`；现有数据库项目仍是 `/volume1/docker/PostgreSQL`。没有修改 Y 盘或 NAS 配置。

配套 Compose 在 `api.environment` 保存以下字段：

```yaml
APP_DATA_BOARD_LIBRARY_SOURCE: /import/board-library
APP_DATA_BOARD_LIBRARY_STORE: /data/board-library
APP_DATA_BOARD_LIBRARY_IMPORT_ID: initial
APP_DATA_BOARD_LIBRARY_SQL_MAX_BYTES: '4194304'
```

源目录用只读 bind 挂载至 `/import/board-library`，要求宿主路径已存在。新文件存储使用 `board_library_data` 命名卷，Docker 自动创建；镜像预先将挂载点归属设为 UID/GID 10001，不更改 NAS 原目录的权限或所有者。API 的 `APP_DATA_APPLICATIONS` 同时声明 `library_store=/data/board-library`，由配置工具合并，不能覆盖 Zheri 或原有应用配置。

启动顺序：升级数据库 → 注册应用 → 自动预览/导入/发布 → 清除维护连接变量 → 启动 API。迁移、注册及导入子进程使用维护连接，API 使用运行角色。新卷在导入阶段需要可写，API 不提供资料写入接口；其他公开目录继续保持只读挂载。健康检查初始宽限为 10 分钟，适用于首次导入。

用户只需更新镜像及 Compose，启动同一个 Container Manager 项目，然后打开 **api 容器日志**。日志含以下阶段，不打印正文、密码、Key 或连接字符串：

| stage | 含义 |
| --- | --- |
| `planning` / `plan_ready` | 正在读取资料及预览计数 |
| `warning` / `input_error` | 缺失文件、附加字段或阻断问题 |
| `sql_file_saved` | 一个小文件的原文已提交到 SQL，带路径、大小、类型 |
| `draft_saved` | 类型化草稿已入库 |
| `game_in_database` | 逐桌游显示名称、资源登记数、小文件入 SQL 数、缺失/归档数 |
| `completed` | 该编号已导入、发布并保存完成记录，含版本和总数 |
| `skipped_completed` | 普通重启复用完成记录，没有重新扫描源目录 |
| `failed` | 导入失败；保留已发布数据和重试条件，API 继续启动 |

普通重启不会重复扫描、重复发布。需要重新读取更新后的 NAS 资料时，只修改 Compose 的 `APP_DATA_BOARD_LIBRARY_IMPORT_ID`，如 `initial` 改为 `library-2`，重建 API 容器即可。修改小文件大小阈值也会触发重新核对；阈值可设 0–16 MiB，降低阈值不会自动删除之前的 SQL 原文。更换/丢失资料卷会自动重新复制原件，避免只看数据库标记就跳过一个空卷。

工具 `tools/configure_board_library.py --compose <现有私有YAML> --source <NAS实际路径> --output <新的私有YAML>` 可准备构建输入，保留数据库凭据、签名密钥和其他服务配置，拒绝覆盖原文件。它只在本地生成文件；不上传、启动或部署。不要重新生成整套随机密码来增加挂载。

## 可选维护命令

以下是排障和开发使用的底层命令；日常 NAS 启动已经自动编排，不要求用户进入终端手动执行。

先准备源目录和**另外一个已存在的内容存储目录**。两者不得相同或互为父子，不接受软链接/junction、隐藏/越级路径。单文件非空且不超过 512 MiB，单 JSON 不超过 4 MiB，每批 JSON 合计不超过 64 MiB，每批不超过 10000 个资源或文件；超出时显式分批。

以下是本机开发连接示例；`--local` 只使用被 Git 忽略的本机开发配置。生产维护必须另行显式提供维护连接，运行时 API 凭据不能执行导入/发布。不要将数据库 URL 或密码放入公开文档、客户端或命令日志。

```powershell
# 只读预览：不需要数据库，不写源文件、存储目录或 SQL
.venv/Scripts/app-data.exe board-plan-library --source C:/prepared/board-content

# 导入：复制到内容寻址存储，并将类型化元数据写为不可见草稿
.venv/Scripts/app-data.exe --local board-import-library --source C:/prepared/board-content --store C:/prepared/board-library-store

# 发布：使用上一条输出的 batch_id
.venv/Scripts/app-data.exe --local board-publish-library --batch <batch_id> --store C:/prepared/board-library-store
```

预览输出 `ready`、计数、错误、警告、SQL 小文件候选数和 `source_hash`；必须查看 `ready`，预览命令的正常退出本身不表示资料合格。导入不合格计划会拒绝写入。`--games slug1 slug2` 可用于明确的局部批次，`--sql-file-max-bytes` 配置小文件阈值；省略条目不会删除当前条目。撤下条目应显式设置 `enabled: false` 后重新导入、发布该条目。

导入器只负责转换和草稿入库，`catalog_service.publish_library` 是统一发布入口，依据 SQL 草稿发布，不要求客户端读取或生成任何旧 JSON。后续其他整理工具应写入同一类型化草稿并走该服务，不能自己修改 `head` 或另写第二套发布逻辑。旧的 `board-publish-library --source --destination` 已删除，无参数兼容分支。

## 一致性与失败处理

1. 文件按 SHA-256 写入 `blobs/<前两位>/<完整摘要>`；流式复制、重新核对摘要和大小，再独占放入存储，不覆盖已有对象。每个小文件在独立有界事务之前读取，逐份写入 SQL，不将全库原文堆进内存。失败可以留下未被草稿引用的对象，但它们不公开，不自动清理。
2. 导入使用来源摘要和基准发布版本确定批次，同一基准重跑返回已有草稿；相同资料已经发布且仍是当前条目时返回原批次。每个批次保留自己的原始资料与审计信息。
3. 发布先复核该批次引用的所有 blob，再取得应用维护事务锁。SQL 事务内一次写入 release、条目映射、变化记录并切换 head；读请求使用只读 REPEATABLE READ，一次响应只见一个完整已提交版本。
4. 局部批次合并未触及条目。同一条目在草稿生成后被别人发布过则拒绝陈旧草稿，必须重新导入；不同条目的并行草稿可顺序发布并合并。资源 ID 不能跨版次挪用，最终快照不能出现 slug 被另一条目占用。
5. 已发布 batch 重试返回原 release，不重新激活旧版本、不增加变化记录。失败回滚 SQL，当前版本不变；已经复制但未被引用的文件不自动清理。后续清理须另行审查和授权。

API 运行角色对资料公开表只有 SELECT，并经应用 RLS 过滤草稿、禁用版次及不可公开资源；原文同样受资源 RLS 控制，源文件映射、导入完成标记和审计表不授予运行角色访问权。事务上限 20 秒，连接、锁和语句等待沿用公共有界配置。导入期间的长会话锁不跨源文件 IO 保持 SQL 事务，防止同时启动两个导入任务。

内容对象不可变；文件元数据变化时重新验证，完整性失败返回 503。启动自动导入使用专用可写卷，正常 API 业务仅提供读取。新版本 PDF 可继续用已发布的 `version=sha256` 完成旧版本读取，但资源/版次撤下后返回 404；不能用已知摘要访问草稿或归档。

## 公开 API 契约

前缀 `/v1/apps/board_game_agent/catalog`，匿名可读，响应协议 `protocol: 1`。Web 后续仍通过自己的同源 `/api` 转发，不在本阶段部署。

| 请求 | 用途 |
| --- | --- |
| GET 空路径 | `locale`、`q`、`category` 筛选，`limit` 1–200、`cursor` 分页；返回 revision、items、next_cursor |
| GET `/revision` | 当前发布版本及发布时间 |
| GET `/categories` | 当前公开版次的分类计数 |
| GET `/games/{id}` | 游戏下当前可见版次 |
| GET `/editions/{id}` | 多语言内容、分类、可公开资源、派生引用及封面/正文绑定 |
| GET `/changes` | `after` 后的 upsert/unpublish；有界分页及完成水位 |
| GET/HEAD `/resources/{id}/content` | 由 ID 定位已发布文件；可指定 `version=sha256`；支持 Range、ETag、304 |
| GET `/resources/{id}/text` | 从 SQL 读取当前可公开资源的解码文本、有效 JSON 和编码；不做文件扫描/OCR |

列表按排序值和稳定 ID 分页，游标绑定筛选条件和 revision；分页期间版本变化返回 409，客户端重新读取列表，不能拼出跨版本半份目录。多语言按请求 locale、en、zhHans、第一种可用语言回退；详情仍返回全部语言。

变化分页固定第一次读取时的目标 revision；`latest_revision` 可在翻页期间前进，但 `revision` 仍是这轮目标。必须取得所有页、应用变更后，才将 `through_revision` 保存为已处理版本；中间页返回原 after，不能提前跳过同一发布中的剩余条目。变化记录持久保存，断线后可补读，不依赖进程内事件。

**本阶段没有接入资料 PowerSync 通知或客户端订阅。** `head/changes` 为后续通知、断线补齐和客户端版本缓存提供基础；此处不增加 PDF 二进制同步流，也不改变社区的现有通知。

## 后续接线与验证

下次部署需要包含本轮尚未发布的 0014 定义和新启动命令的 API 镜像及配套 Compose。新增表仍属于未交付的 0014：`file_bodies`、`source_files`、`import_runs`，没有对 NAS 执行迁移。PostgreSQL / PowerSync 基础镜像及当前同步流没有修改。现有 Compose 的密码、密钥、数据目录不得重新生成。

当前客户端仍读旧 `/library`，该只读服务暂留至下一阶段的客户端替换；新 `/catalog` 不回读旧清单、没有新旧双写。新存储配置缺失会明确返回文件不可用，不回退到旧目录。下一阶段统一切换客户端目录、详情、文档选择及缓存版本，再移除已被替换的旧读取实现。

`tests/test_board_catalog.py` 在随机命名的真实 PostgreSQL 测试库验证导入保留、草稿隐藏、原子发布、幂等、冲突、局部合并、RLS、文件版本/撤下、PDF Range 和失败保留。实际 NAS 导入、产物构建和设备验收必须另外记录，不以测试库结果代替。
