# 桌游业务模块：公开资料与私人同步

2026-10-26，`feature/board-game-business` 开发源码；没有构建新镜像、上传或改动 NAS。账号仍使用公共核心，Zheri 客户端未修改。新增业务均属于 `modules/board_game_agent/`，安装其他应用不需要在认证核心增加业务分支。

## 表和接口

迁移 `0012_board_game_business` 只新增桌游表，不转换或清理旧资料。

- 私人表：`favorites`、`recent_games`、`preferences`、`conversations`、`messages`、`votes`。按 `dataset_id` 归属，RLS 同时检查应用启用状态、固定应用和成员权限；运行角色无 DDL 和硬删除权限。
- 社区表：`comments`、`comment_likes`、`mutation_receipts`。写入者由验证后的 Actor 决定；请求不能指定用户身份或发布昵称。公开作者 ID 是桌游应用的假名 ID，不公开全局账号 ID。
- 六个私人流加入 publication 和生成配置，只使用签名 `app_id`、用户及数据集成员条件。评论不是私人流，不将其他人的收藏、偏好或聊天复制到设备。

路径前缀为 `/v1/apps/board_game_agent`：

| 接口 | 身份与用途 |
| --- | --- |
| GET `/library` | 匿名，经过验证的公开发布清单 |
| GET/HEAD `/library/files/{path}` | 匿名，清单内文件，SHA-256/大小验证、ETag 和 Range |
| POST `/sync/upload` | 登录，`dataset_id` 与类型化 mutations；记录 ID、字段和应用须匹配 |
| GET `/community/comments/{game}` | 匿名可读，登录时附本人点赞状态；时间＋ID 游标分页 |
| POST `/community/commands` | 登录，发布/删除/点赞的幂等命令；只可删除自己的评论 |
| GET `/community/statistics` | 公开汇总；登录时仅额外返回本人的选择，便于客户端避免重复计数 |

命令/修改 ID 在账号和应用内唯一。同 ID 同内容返回原回执，不同内容返回 409。私人普通记录按服务端接受顺序覆盖；偏好 `changed_fields` 合并明确修改的字段，避免离线旧默认值覆盖另一设备的修改。删除是墓碑。单请求最多 100 条且受 512 KiB 请求体、锁和语句超时约束；桌游客户端按单条有界记录上传。

评论时间由服务器生成；每账号最新的带评分根评论参与平均分，删除该评论不会恢复旧评分。点赞表达目标开/关状态。公开响应不返回私人数据集、内部用户 ID 或凭据；含本人状态的响应禁止缓存。

## 公开快照

准备一个只包含要公开资料的本地源目录，内部含 `assets/catalog.json` 和显式资源；不要直接把未经整理的工程、账号目录或整个 NAS 共享目录设为输入。支持 JSON、PDF、MD、TXT、PNG/JPEG/WebP/SVG，拒绝 `docs/others`、隐藏/越级路径、软链接、空文件和超限文件。

```powershell
.venv/Scripts/app-data.exe board-publish-library --source C:/prepared/board-content --destination C:/prepared/board-public-new
```

目标目录必须不存在，且不能放在源目录内。工具校验、复制和复核后写 `publication.json`（版本 1，每项路径对应 SHA-256 和大小），不读取 DSM、不联网、不部署、不覆盖旧快照。失败的暂存目录不会成为正在提供的目录。资料的来源、版次和许可仍由整理发布者核实。

API 只读这个快照；不需要用户 NAS 凭据。一次文件版本校验可供后续 Range 请求复用，文件元数据变化即重新验证，不会为每段 PDF 重新哈希整本书。服务器安全响应头禁止资源脚本执行。

## 配置与下一次部署

在已有 `APP_DATA_APPLICATIONS` 中为桌游增加：

```json
{"board_game_agent":{"public_file_roots":{"library":"/data/public/board_game_agent"}}}
```

在 API 服务增加管理员准备的快照挂载；下面只是字段示例，不是可直接覆盖生产配置的完整 Compose：

```yaml
volumes:
  - type: bind
    source: /volume1/published/board-game-agent
    target: /data/public/board_game_agent
    read_only: true
    bind:
      create_host_path: false
```

开发用 `render_stack.py --applications <json> --public-mounts <json>` 支持声明式配置；后一个 JSON 将每个已声明容器目录映射到明确宿主机路径。生成器要求所有目录有匹配挂载，固定只读且不创建宿主目录。不要为了加挂载重新生成已有私有 Compose，从而意外轮换数据库密码/密钥；保留已有数据目录、秘密和 DSM 身份命名空间。

后续正式交付须配套：新 API 镜像、0012 迁移、应用注册、重新生成的 PowerSync 规则和只读公开目录。PostgreSQL/PowerSync 基础镜像版本未改变。API 仍是纯 API 镜像，不包含 Zheri 网页。所有 SDK 同步 endpoint 规范为以 `/` 结尾，避免 URI.resolve 丢掉 `/powersync/` 挂载路径。

Web 浏览器仍需要自己的 `/api/` 转发及 SQLite Worker 静态资源/响应头；用户已暂缓该部署问题，本轮不增加旧网关兼容。Loomex 模型服务、旧部署最终移除和实际设备验收不在阶段 2、3 的完成声明内。
