# 桌游 Agent Checkpoint 部署准备

阶段 5 使用 `AsyncPostgresSaver` 保存 LangGraph 状态，并持久保存线程归属、运行与消息去重记录、待恢复中断。Flutter 通过本地会话身份和版本稳定识别线程。API 重启后保留已成功轮次和已提交的工具中断。启动时释放未完成运行。停止当前生成只丢弃未提交部分，显式清空才删除线程状态。

## 准备专用数据库

在现有 PostgreSQL 实例创建专用数据库，例如 `board_agent_checkpoint`。由数据库管理员创建迁移角色和运行角色。迁移角色拥有该数据库，运行角色只需要 `CONNECT`、目标 schema 的 `USAGE` 和初始化命令授予的表 DML。两个角色均使用非超级用户权限，运行角色不拥有数据库或 schema，也不授予 `CREATE`。数据库与 `public` schema 权限须按角色授予，限制其他应用账号访问。

在新 API 镜像的一次性初始化环境中注入 `APP_DATA_BOARD_AGENT_CHECKPOINT_MIGRATION_DSN`，然后运行：

```text
python -m app_data_service.cli board-init-agent-checkpoint --runtime-role board_agent_checkpoint_runtime
```

API 服务只注入运行角色的 `APP_DATA_BOARD_AGENT_CHECKPOINT_DSN`。两个 DSN 均使用 libpq 的 `postgresql://` 格式，并放入私有部署配置。初始化命令复用 LangGraph 的版本化迁移。升级前停掉占用该数据库的 Agent，初始化完成后启动单个 API worker。独占数据库锁阻止两个 API 实例同时接管线程。

验收时带桌游 Bearer Token 请求 `GET /v1/apps/board_game_agent/ai/agent/health`，确认 `checkpoint` 返回 `postgres`。缺少运行 DSN 时采用内存 Checkpoint。配置了运行 DSN 但连接或初始化检查失败时，Agent 返回 503，共用业务 API 可继续启动。

## 保留与删除

- 每线程最多 32 个用户轮及每轮最多 8 次恢复。最多 64 个在册线程。空闲 30 分钟后可回收，通过定时和新轮准入按批清理。中断结果有效期为 5 分钟。
- 已登录线程所有者可请求 `DELETE /v1/apps/board_game_agent/ai/agent/{threadId}`，清除该线程的 Checkpoint。Flutter 清空或删除本地会话时尝试删除该应用会话对应的服务端线程，离线时由到期清理收尾。
- 活动运行或待恢复工具中断可用原取消入口停止。已完成轮的相同末次回执重发会回放终态。跨账号访问和已取消的恢复会被拒绝。

依赖锁定 `langgraph-checkpoint-postgres 3.2.0`（MIT）与 `psycopg-pool 3.3.3`（LGPL）。本机 Windows 使用兼容 psycopg 的 Selector 事件循环测试，NAS 使用 Linux。连接、连接池、SQL 和锁等待均设有时限。升级时复验初始化、运行角色权限、中断恢复和线程清理。 [PostgreSQL Checkpointer](https://pypi.org/project/langgraph-checkpoint-postgres/3.2.0/)

2026-11-13 本地使用真实 PostgreSQL 的独立测试数据库、真实 HTTP 和固定模型响应，验证 FastAPI 重启后的追问和中断恢复、跨账号拒绝、重复回传拒绝、线程删除与过期清理。后台全量 395 项通过。Flutter 静态分析与全量 767 项通过。
