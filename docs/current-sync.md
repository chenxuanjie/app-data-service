# Zheri 业务同步契约（API 0.5.0）

本文描述 Zheri 模块，其他应用自行定义业务模型与同步策略。公共契约见 [多应用接口](application-scope.md)。

与折日 0.14.0+66 配套。结构化数据通过 PostgreSQL + PowerSync 复制，客户端 SQLite 离线优先。
多应用源码尚未部署。六类业务实体保持；回执归属、应用权限和生成的同步配置按 0010/0011 更新。

## 唯一业务协议

- `GET /v1/capabilities`：sync_protocols=[2]，六类实体，max_mutations=100。
- `POST /v2/sync/upload`：每个 mutation 必须带 entity_type、record_id、mutation_id、base_version、kind；保留 predecessor 链和完整预期记录。
- `GET /v2/conflicts?dataset_id=...`：按账号与应用授权，返回类型和当前记录/版本。
- `POST /v2/conflicts/{id}/resolve`：entity_type、choice(server/candidate)、expected_version。

实体为 expenses、assets、expense_categories、payroll_records、asset_allocations、business_preferences。
旧 `/v1/sync/upload`、`/v1/conflicts` 及转正路由已删除，不再支持旧业务客户端。
认证、文件、更新、能力发现等 `/v1/` 路由继续使用。

## 数据升级

迁移 `0008_personal_ledgers` 原地将既有 zheri 试用状态收敛为个人账本，并删除旧建账/转正函数。
新建默认 personal；既有 test-* ID 不改名、不换 scope，不清空本机队列或服务端数据。
既有 mutation receipts 的摘要不重写。客户端补充旧支出队列的 entity_type 后，
服务端只允许与原历史摘要完全相同的重放；换内容、跨实体、跨账号或跨数据集复用仍拒绝。
保留旧冲突记录及其乐观版本校验，不删除历史业务表。

API 升级和客户端应配套发布，不能让旧 APK 用本轮已退役的业务路由验收。
按原流程先运行数据库迁移再启动 API；不需要新端口、清库或重置密钥。

## 验证

ruff check / format --check 与完整 193 项 pytest 通过（真实 PostgreSQL、随机隔离测试库）。
独立 Dart 客户端 + PowerSync 服务 + API + PostgreSQL 的 11 项实测通过，
覆盖六类业务双向复制、离线重开、删除、冲突、响应丢失后的幂等重试及账号隔离。
仅本地验证；NAS、真实 DSM、Android 安装器未在本轮重新实机验收。
