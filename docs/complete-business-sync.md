> 当前 API 0.5.0：六类业务统一 `/v2/sync/upload` 与 `/v2/conflicts`；能力协议仅 [2]。迁移 0008 保留 ID 和队列并收敛个人账本状态；已移除转正接口。最新契约见[当前同步说明](current-sync.md)。下文为阶段实现记录，旧接口不再提供。

# 全业务同步：API 0.3.0

2026-10-22：用户确认本轮验收通过并要求合并，版本来源与收尾见 [0.3.0 验收记录](releases/0.3.0.md)。

本阶段补齐折日剩余业务，取代旧文档中的“仅固定支出/物品/分类”“试用账本迁入后置”边界。

## 数据库与协议

迁移 `0006_complete_business` 新增 `zheri.payroll_records`、`zheri.asset_allocations`、`zheri.business_preferences`。
共同字段：id、dataset_id、name、payload JSONB、sort_order、row_version、updated_at、deleted_at。
工资 name 为 YYYY-MM；`UNIQUE(dataset_id,name)` 保证每月唯一，设置也按数据集与设置名唯一。资产配置按记录 ID 区分。
JSONB 不是任意快照：API 和 PostgreSQL CHECK 同时严格验证字段、嵌套结构及类型；拒绝未知字段。
金额为十进制文本，最多 14 位整数、6 位小数；比例为 0～1、最多 18 位小数。不得静默舍入或将浮点数写入金额字段。

工资 payload：monthKey、salary、personalConsumption、foodBudget、rentProperty、parentSupport、allocations、recordedAt、fixedSavings、investmentRatio、savingsRatio、note、updatedAt。
每项 allocations 保存 categoryId、categoryName、ratio、amount、colorValue，保留当时分配快照。
资产配置 payload：color、currentAmount、ruleType、ruleRatio、historicalHighPrice、addPositionSteps；加仓步骤保存 id、drawdownRatio、amount。
设置限定为 include_asset_costs（布尔）、allocation_baseline（金额）、allocation_order（ID 列表）、payroll_defaults（严格结构的工资默认值）。
详细规则见 `business_documents.py`；迁移冻结相同 schema，不依赖运行时最新版结构升级旧库。

新表沿用 app_id + dataset membership 的 RLS、行版本触发器、软删除及只读复制授权。运行角色无 DDL、超级用户或物理删除权限。
三表加入明确 publication 和 PowerSync 私有桶，规则沿用同一账号/应用过滤及 deleted_at IS NULL。
六类业务复用 `/v2/sync/upload`、`/v2/sync/conflicts` 和已有解决接口，版本链、幂等回执、冲突双方保留及有界批次不变。
旧固定支出 v1 队列继续提交；这是同一 PowerSync 协议升级，不提供 WebDAV 桥接。

## 个人账本

`core.datasets.kind` 区分 trial/personal。DSM 验证后，`core.ensure_personal_account()` 为新账号创建 personal-uid；已有账号优先复用原 test-uid，保留 ID、权限和全部记录。
`GET /v1/datasets` 返回 kind，登录响应返回 dataset_kind。
`GET /v1/capabilities` 返回六种实体和 dataset_promotion=true。
`POST /v2/datasets/{dataset_id}/promote` 仅允许当前账号拥有的 zheri 数据集，返回 dataset_id 与 dataset_kind=personal；权限外返回 404。
原地改 kind/名称，不复制、不删除数据，不改行版本、同步队列或回执。重复调用幂等，锁等待受请求事务预算限制。

客户端先预览、逐条选版本，在一个 SQLite 事务中保存导入、回执和启用标记，再调用 promote。跨本地和远端失败时允许重试，不伪称分布式原子事务。
工资同月与同设置不能保留两个有效版本。资产配置保留两份时生成稳定副本 ID，并调整新导入工资的引用和排序；重试不重复创建。
试用期已有记录保留，由用户在确认前核对，不自动清理测试数据。

## 升级

1. 导入干净构建的 `fastapi-0.3.0.tar.gz`，替换同批 `docker-compose.yml`，重建现有项目。
2. 配套 Compose 同时包含新 API 标签和六表 PowerSync 规则，不能只改其中一部分。
3. 保留原密码、签名密钥、身份命名空间、数据挂载及反向代理；API 启动先迁移，成功后启动服务。
4. PostgreSQL 17.11、PowerSync 1.27.0 镜像本轮不变；三个常驻服务，不加退出式迁移容器，不新增端口。
5. 更新客户端后按 [迁入说明](../../../zheri/docs/complete-business-sync.md) 完成每台设备确认。

客户端离线可继续修改；服务端能力未升级时不丢弃队列。不要通过删卷、改数据集 ID 或降级客户端解决同步问题。
迁移不提供破坏性 downgrade；回退运行版本必须评估新队列与类型支持。旧版本不显示某类数据不表示数据不存在。
备份、共享账本和管理后台不在本轮范围。

## 验证与交付

完整 141 项 pytest 使用真实 PostgreSQL，覆盖 API/直接 SQL 双层校验、RLS、无物理删除权限、版本冲突、回执、原地确认及旧账号重新登录；ruff check 和 format 也须通过。
Windows 上真实 Dart SQLite + PowerSync + PostgreSQL 的 11 项探测覆盖新增三类双向复制、重启、账号隔离、选择冲突及确认后的离线删除。
Docker 构建器对实际导入的压缩镜像启动隔离完整栈；DSM 使用严格 TLS 替身，报告不能代替真实 DSM 验收。
正式交付先提交，再用 source gate 记录来源；release 元数据 dirtySource 必须为 false。
真实 NAS、个人账号和 Android/Windows/Web 人工验收按实际结果确认，本机探测不代表已部署。
