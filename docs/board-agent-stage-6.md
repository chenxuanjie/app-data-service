# 阶段 6 NAS 现场验收

候选后台版本为 0.6.0。升级前保留当前 Compose、API 镜像标签和可恢复备份，并核对现有数据库迁移版本。候选 Compose 含现有部署配置，应保存在私有位置。

1. 将本次 API 归档导入 Synology Container Manager。在原 Compose 项目中采用新 API 镜像标签，保留实际项目名、数据库和 PowerSync 版本、卷、网络、端口、认证和签名配置。核对候选 Compose 的同步规则和应用配置与当前部署的差异。
2. 在现有 PostgreSQL 实例创建专用库与两个登录角色。例如使用交互式 `psql`，用 `\password` 设置密码：

   ```sql
   CREATE ROLE board_agent_checkpoint_migrator LOGIN;
   CREATE ROLE board_agent_checkpoint_runtime LOGIN;
   CREATE DATABASE board_agent_checkpoint OWNER board_agent_checkpoint_migrator;
   REVOKE ALL ON DATABASE board_agent_checkpoint FROM PUBLIC;
   GRANT CONNECT ON DATABASE board_agent_checkpoint TO board_agent_checkpoint_runtime;
   \connect board_agent_checkpoint
   REVOKE ALL ON SCHEMA public FROM PUBLIC;
   GRANT USAGE ON SCHEMA public TO board_agent_checkpoint_runtime;
   ```

3. 用新 API 镜像在原 Compose 网络运行一次性初始化。先在当前 shell 安全注入并导出迁移角色的 `APP_DATA_BOARD_AGENT_CHECKPOINT_MIGRATION_DSN`，然后执行：

   ```sh
   docker compose run --rm --no-deps -e APP_DATA_BOARD_AGENT_CHECKPOINT_MIGRATION_DSN api \
     python -m app_data_service.cli board-init-agent-checkpoint \
     --runtime-role board_agent_checkpoint_runtime
   ```

   初始化后清除当前 shell 中的迁移 DSN。DSN 使用 libpq 的 `postgresql://` 格式，主机名使用 Compose 网络内的数据库服务名。将运行角色 DSN 写入 API 的 `APP_DATA_BOARD_AGENT_CHECKPOINT_DSN`。确认 API 环境已配置 Loomex，随后只更新 API 容器。
4. 从平时使用的 HTTPS 入口，带桌游应用 Bearer Token 请求 `/v1/apps/board_game_agent/ai/agent/health`，确认返回 `{"status":"ready","checkpoint":"postgres"}`。Web 同源入口在该路径前加 `/api`。若返回 `memory`，检查 API 的运行 DSN。若返回 503，查看 Agent 初始化日志。
5. 解压 Windows 测试包，从同一目录运行 `board_game_agent.exe`。登录并选择默认 Loomex、低推理强度和标准速度，在独立助手中关闭当前桌游资料，使用「知识不足时智能补充」模式。测试「打开排行榜」「查询收藏排行榜前三项」「打开排行榜，然后告诉我第一名」，并在输出中途按停止。
6. 完成一次排行查询后，保持客户端和同一对话打开，重启 API 容器，再追问第一名的收藏数。确认账号登录、资料目录和 PowerSync 也能正常使用。

如需回退，先停止新 API，再恢复已备份的 Compose 和上一版 API 镜像。保留专用 Checkpoint 库和现有业务卷。若这次还推进了业务数据库迁移，先按原版本的迁移与备份策略确认兼容性，再恢复旧 API。

本地已验证交付镜像的归档导入、隔离启动、Checkpoint 初始化、运行角色读写及建表拒绝。真实 NAS、HTTPS 反代及完整客户端账号流程由现场验收确认。
