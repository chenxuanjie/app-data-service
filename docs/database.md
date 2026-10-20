# 数据与同步边界

当前只实现固定支出最小数据模型，其余折日业务在下一阶段逐项加入。

| 表 | 主键 / 责任 |
| --- | --- |
| core.apps | 应用标识 |
| core.users | 稳定应用用户 ID；没有密码 |
| core.datasets | 归属账号与应用的数据集 |
| core.dataset_members | 用户与数据集的授权关系，首版仅使用个人数据 |
| zheri.expenses | 全表唯一 text id，保留已有固定支出 ID |
| core.import_batches | 数据集与规范化来源哈希唯一，避免重复导入 |
| core.import_records | 来源 ID 到目标 ID 的可追溯映射，限定同一数据集 |

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
`set_config('app.user_id', user_id, true)`，RLS 按成员关系和应用限定访问。
连接池返回后事务内身份会清除，无身份时读不到私有记录。
这不是身份验证接口：数据库运行凭据必须只留在可信服务端。

运行角色不能修改记录 id/dataset_id、不能建表、提权或硬删除。数据库触发器
管理 row_version 与 updated_at，客户端提交的版本值不会直接覆盖服务端计数。
后续 API 必须用预期 row_version 原子条件更新，并实现冲突候选记录与选择流程；
当前数据库测试只验证该条件更新机制，尚未实现冲突 UI 或完整上传协议。

## PowerSync 接入准备

PostgreSQL 17.11 开启 wal_level=logical，publication `powersync` 只包含：

- zheri.expenses
- core.datasets
- core.dataset_members

复制角色只读这些表，不读取 users 或导入审计，不使用 FOR ALL TABLES。
没有创建复制槽或 bucket 存储，避免无人消费的 WAL 无限制积压。

同步服务负责下行；客户端 uploadData 仍需经认证的业务 API 写入。
后续采用独立新本地库与增量业务写入，不能让旧 changeset 和新 SDK 同时跟踪
同一业务数据，不能用“整份快照缺失的 ID 就删除”的逻辑处理陈旧页面。

官方依据：

- [PostgreSQL 源库配置](https://docs.powersync.com/configuration/source-db/setup)
- [类型映射](https://docs.powersync.com/sync/types)
- [客户端主键](https://docs.powersync.com/sync/advanced/client-id)
- [Drift 适配](https://docs.powersync.com/client-sdks/orms/flutter-orm-support)
