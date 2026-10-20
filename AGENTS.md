# 项目约定

- 已完成 PostgreSQL 基础；当前验证 PowerSync + FastAPI 最小双向链路，客户端离线优先。
- 从固定支出最小功能开始，不实现备份、家庭共享、管理后台或自研同步引擎。
- 不改真实 NAS、账号权限或生产数据，不自动上传或部署。
- 数据库验证必须使用真实 PostgreSQL；SQLite 不能代替。
- 每个请求/连接/锁等待必须有界。金额使用 Decimal/NUMERIC，禁止静默舍入。
- 客户端不能持有数据库账号。数据库运行角色无 DDL/超级用户权限。
- 账号与数据集隔离由服务端授权和 PostgreSQL RLS 共同验证。
- 修改后执行 ruff check、ruff format --check、完整 pytest；数据库测试不得跳过后声称通过。
- 不自动提交 Git；用户明确要求提交时采用中文 Conventional Commits。
- 按 [Git Flow 约定](docs/git-flow.md) 开发：main 保留发布版本，develop 集成，feature/* 承载阶段工作；未验收功能不合入 main。

