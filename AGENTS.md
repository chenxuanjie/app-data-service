# 项目约定

- 已完成并经用户确认验收 PostgreSQL + PowerSync + FastAPI 六类业务双向同步，客户端离线优先；版本与验证范围见 docs/releases/0.3.0.md。
- 后续从 develop 另开功能分支；备份、家庭共享、管理后台和自研同步引擎不在本轮范围。
- 不改真实 NAS、账号权限或生产数据，不自动上传或部署。
- 数据库验证必须使用真实 PostgreSQL；SQLite 不能代替。
- 每个请求/连接/锁等待必须有界。金额使用 Decimal/NUMERIC，禁止静默舍入。
- 客户端不能持有数据库账号。数据库运行角色无 DDL/超级用户权限。
- 账号与数据集隔离由服务端授权和 PostgreSQL RLS 共同验证。
- 修改后执行 ruff check、ruff format --check、完整 pytest；数据库测试不得跳过后声称通过。
- 普通开发不自动提交 Git；用户于 2026-10-21 要求正式交付前提交，后续正式发布请求包含本次交付必要的相关提交。采用中文 Conventional Commits，不夹带其他任务；修改约束本身不授权提交已有业务改动。
- 按 [Git Flow 约定](docs/git-flow.md) 开发：main 保留发布版本，develop 集成，feature/* 承载阶段工作；未验收功能不合入 main。

## 正式 Docker 交付

- 按 [提交后构建与交付](C:/Users/win10/.codex/skills/flutter-release-publisher/references/committed-delivery.md) 执行 source gate：先确定版本并提交业务代码、迁移、同步规则、公开配置模板与构建脚本，再从干净工作区构建并验证。
- 推送前检查原提交、源码状态及分包/配套 Compose 的摘要。源码、版本或规则变化后必须提交并重建；不得将现有 dirty 测试镜像补提交或改标签后当作正式包。
- 本地实验允许 dirty，必须标明未提交和测试用途；现有 0.2.0 dirty 包不满足正式交付门禁。秘密、实际私有 Compose、数据库数据和构建产物不入 Git。
- Git commit、Git push、上传和 NAS 部署按各自授权执行；不为通过门禁把未验收功能合入 main。
