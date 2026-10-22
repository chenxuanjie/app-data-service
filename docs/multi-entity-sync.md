# 多类型同步基础：API 0.2.0

> 历史阶段记录：当前版本为 API 0.3.0，六类业务、个人账本确认及升级步骤见 [全业务同步](complete-business-sync.md)。旧镜像、dirty 包及旧验收范围不能作为本轮交付依据。

2026-10-21，分支 `feature/multi-entity-sync`。已实现服务端与客户端协议基础，
折日原物品和分类页面的显式导入、数据源切换仍为下一阶段。
本轮已在本机构建并完整验证 API 0.2.0 镜像，更新 `release/fastapi-0.2.0.tar.gz` 和配套 Compose；PostgreSQL 17.11 与 PowerSync 1.27.0 镜像未变。未上传或部署到真实 NAS。

## 表与数据约束

| 实体 | 表 / 字段 |
| --- | --- |
| 物品 | `zheri.assets`：名称、NUMERIC 价格/残值/目标日成本、DATE 购买日期、分类文字、图标、BIGINT ARGB、备注、计入成本、排序 |
| 自定义支出分类 | `zheri.expense_categories`：名称、图标、排序 |
| 固定支出 | 增加可空 category_id，与 dataset_id 一起引用同账本分类；原分类文字、子分类快照保留 |
| 冲突 | 物品和分类使用 zheri.sync_conflicts；原 expense_conflicts 保留 |

新表均有 id、dataset_id、row_version、updated_at、deleted_at，复用版本/时间戳触发器，
禁止运行角色硬删除，按账号成员关系及应用进行 RLS 校验。分类软删除不删除关联支出。
金额最多十四位整数、六位小数，不使用定标 NUMERIC 静默舍入。残值不高于价格，目标日成本大于零。
购买日期为本地选定的日历日期，不在客户端转 UTC；HTTP 必须发送 YYYY-MM-DD。
颜色允许 0～4294967295，避免有符号 32 位溢出。

## 协议

- `GET /v1/capabilities`：需有效身份，返回 sync_protocols=[1,2]、三个实体名和 max_mutations=100。
- `POST /v2/sync/upload`：沿用 dataset_id、client_id、mutations，逐项增加
  entity_type=expenses / assets / expense_categories。单批最多 100 项、原子提交。
  mutation_id、record_id、base_version、predecessor、kind 的含义不变。
- `GET /v2/conflicts?dataset_id=...`：合并展示三类未解决冲突，附 entity_type、当前记录及当前版本，最多 100 项。
- `POST /v2/conflicts/{id}/resolve`：正文为 entity_type、choice（server/candidate）、expected_version。
  旧版本选择返回 409，重复相同选择可幂等确认。
- 旧 v1 上传、查询和解决接口继续使用原格式，不给旧队列补 entity_type，不改变原 payload_hash。
  回执新增实体类型（旧行默认 expenses），predecessor 必须匹配同一账本、实体与记录。
  mutation_id 仍在同账号、client ID 内唯一，跨实体复用同一个 ID 返回 409。

物品正文使用 id/name/price/purchaseDate/category/residualValue/targetDailyCost/icon/color，
可选 includeInLivingCost/note/sortOrder；三种金额均为十进制字符串。
分类正文使用 id/name/iconId，可选 sortOrder。
v2 支出沿用原正文，并可传 categoryId（含 null）；旧 v1 不传这个字段，保留已有引用。
未知字段、非法日期、非有限金额、超出精度的值明确拒绝，不丢弃未知字段后继续保存。

## PowerSync 与兼容升级

迁移 0005_multi_entity 新增表、索引、权限、分类外键和回执列，并将两张新业务表加入
现有 powersync publication。旧支出、回执哈希和未解决冲突不重写。
`sync-config.yaml` 继续 edition 3 / storage_version 2，增加 private_assets 和
private_expense_categories 自动订阅流，各自过滤软删除、应用和当前用户账本。
不增加数据库或同步服务的公网端口。

后续正式构建 API 更新包时，新 API 镜像在启动时先执行迁移；Compose 同时更新内嵌规则。
`build_docker_bundle.py --api-only` 在构建当前源码时只替换内嵌规则内容和 API 镜像，
保留原连接、密码、签名、路径和来源白名单；`--reuse-existing` 不把新规则错误配给旧镜像。
PostgreSQL 17.11 和 PowerSync 1.27.0 无需因此重打包。
当前只完成源码，不应使用旧 release 文件宣称已部署 0.2.0。

## 已验证范围

- ruff 检查和格式检查通过，真实 PostgreSQL 上完整 114 项 pytest 通过。
- 从真实 0004 数据库带旧支出、回执及未解决冲突升级，验证原 v1 重试不重复写入。
- 三个 Windows Dart SQLite 客户端连接真实 API、PostgreSQL 和 PowerSync：
  固定支出原链路、新实体离线重开、六位小数/日历日期/ARGB、账号与应用下行隔离、
  并发物品冲突及选择下行、分类和物品删除均通过；运行后清理本轮隔离资源。
- DSM 本次探测使用测试令牌，未重复真实登录；不等于 NAS 或完整 Flutter 三端验收。

折日客户端适配说明见工程 `docs/multi-entity-sync-foundation.md`。
