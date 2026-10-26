# 数据与同步边界

当前公共模型覆盖多应用账号、数据空间与会话；Zheri 模块包含六类业务。
结构和扩展流程见 [架构](architecture.md) 与 [接入应用](adding-an-application.md)。

| 表 | 主键 / 责任 |
| --- | --- |
| core.apps | 应用、schema、启用状态和个人数据空间元数据 |
| core.users | 跨应用稳定账号身份；没有密码 |
| core.datasets | 归属账号与应用的数据集 |
| core.dataset_members | 用户与数据集的授权关系 |
| core.personal_spaces | 每个应用/账号的主个人空间映射，复合外键验证所属应用与所有者 |
| core.browser_sessions | 应用隔离的匿名/登录浏览器会话，刷新凭据只存密文 |
| zheri.expenses | 全表唯一 text id，保留已有固定支出 ID |
| zheri.import_batches | 数据集与规范化来源哈希唯一，避免重复导入 |
| zheri.import_records | 来源 ID 到目标 ID 的可追溯映射，限定同一数据集 |
| zheri.mutation_receipts | 用户/客户端/修改 UUID 主键，保存提交结果和内容哈希 |
| core.app_sessions | 用户作用域内的会话、刷新令牌哈希、加密 DSM 会话和撤销状态，不参与复制 |
| zheri.expense_conflicts | 冲突候选、当时服务器版本、选择结果，按用户和数据集隔离 |

## 固定支出映射

| 折日 JSON | PostgreSQL | PowerSync 客户端 |
| --- | --- | --- |
| id | text 主键，不自动换号 | text id |
| name | text | text |
| amount | NUMERIC，非负、最多 6 位小数和 14 位整数 | text，保留十进制表示 |
| cycle | 六种已存在的周期名称 | text |
| category / subcategory | text | text |
| iconId | icon_id，可空 | text |
| includeInLivingCost | boolean | integer 0/1 |
| note | text，最长 4000 字符 | text |
| 列表顺序 / sortOrder | sort_order，非负整数 | integer |
| 后续业务版本 | row_version，服务器生成并递增 | integer |
| 服务端修改时间 | updated_at，timestamptz | text |
| 后续软删除 | deleted_at，timestamptz 可空 | text |

金额转换拒绝 NaN、Infinity、负数、布尔值、超出范围和超出精度的数值，不自动舍入。
既有 double 不能恢复其已经丢失的十进制精度，导入以明确导出的十进制内容核对。
导入时 source id 缺失、未知字段或重复 id 必须处理，不能生成替代 ID 掩盖问题。

## 权限与事务

运行连接使用 app_runtime。服务端验证用户后，在同一个事务内使用
`app.user_id` 与 `app.app_id` 的事务局部设置，RLS 按成员关系、应用和启用状态限定访问。
连接池返回后事务内身份会清除，无身份时读不到私有记录。
这不是身份验证接口：数据库运行凭据必须只留在可信服务端。

运行角色不能修改记录 id/dataset_id、不能建表、提权或硬删除。数据库触发器
管理 row_version 与 updated_at，客户端提交的版本值不会直接覆盖服务端计数。
API 在数据集事务锁和记录行锁内比较预期版本，原子更新；同设备的未确认修改通过
已提交前序回执衔接。其他设备插入修改后，该链不能跨越新的版本继续覆盖。
发生冲突时保留候选及当时服务器快照，返回持久化冲突回执；折日测试入口提供版本选择 UI。
事务总时限 20 秒、单条 SQL 15 秒、锁等待 5 秒、连接 5 秒；超时回滚并返回错误。

## PowerSync 接入准备

PostgreSQL 17.11 开启 wal_level=logical，publication `powersync` 只包含：

- zheri.expenses
- core.datasets
- core.dataset_members

复制角色只读这些表，不读取 users 或导入审计，不使用 FOR ALL TABLES。
开发业务库不常驻复制槽。真实链路实验在临时源库创建槽，并在结束时清理；
bucket 存储使用独立临时 PostgreSQL 数据库及专用角色，不进入业务 publication。
完整部署为持久业务库与独立 powersync_storage 库；业务库中的复制槽随同步服务持续使用。
修改回执与冲突表仅允许运行角色按 RLS 访问，复制角色无读取权限；
由业务 API 处理冲突选择，不让内部表直接同步。

0004 增加会话表和受限的 `core.ensure_test_account` SECURITY DEFINER 函数。
固定 search_path，依据事务内已验证的 64 位十六进制身份创建 `test-<身份>` 数据集；
运行角色只获函数执行权，没有用户表或数据集表的直接新增权限。

同步服务负责下行；客户端 uploadData 经认证的业务 API 写入。当前使用 Sync Streams
edition 3、storage_version 2，筛选成员关系及 `app_id = 'zheri'`，排除软删除记录。
后续采用独立新本地库与增量业务写入，不能让旧 changeset 和新 SDK 同时跟踪
同一业务数据，不能用“整份快照缺失的 ID 就删除”的逻辑处理陈旧页面。

官方依据：

- [PostgreSQL 源库配置](https://docs.powersync.com/configuration/source-db/setup)
- [类型映射](https://docs.powersync.com/sync/types)
- [客户端主键](https://docs.powersync.com/sync/advanced/client-id)
- [Drift 适配](https://docs.powersync.com/client-sdks/orms/flutter-orm-support)
