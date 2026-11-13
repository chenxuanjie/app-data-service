# 桌游 Agent 本地运行

阶段 2 提供一个可在本地执行的 LangGraph。使用共用后台的只读目录快照和公开收藏聚合，结果包含目录版本、桌游、收藏数及并列名次。每个 CLI 进程使用一个内存线程。

## 本地运行

按后台开发流程配置 Windows `.venv` 和本地 PostgreSQL，在工作树根目录执行。使用已配置的 `APP_DATA_BOARD_LOOMEX_API_KEY`，可用 `APP_DATA_BOARD_LOOMEX_BASE_URL` 指定固定 HTTPS 上游。凭据由调用环境传入。排行需要本地数据库已运行对应迁移，并有已发布的目录。

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.\.venv\Scripts\python.exe -m app_data_service.modules.board_game_agent.agent_cli "查询收藏排行榜前三项" "第一名有多少人收藏？"
```

省略参数可逐条输入，输入空行退出。多条提示共用一次会话。单次运行限时 90 秒、最多 12 个 Graph 步骤。模型读取空闲超时 45 秒，排行榜读取使用现有有界 PostgreSQL 事务。模型失败后保留上次完成的上下文。会话随进程结束。

## 实现与依赖

| 依赖 | 锁定版本 | 用途 |
| --- | --- | --- |
| LangChain | 1.5.0 | `create_agent` 编排模型与工具 |
| LangGraph | 1.2.21 | 状态图、流式消息、内存 Checkpoint |
| LangChain OpenAI | 1.7.4 | Loomex 无状态 Responses，关闭上游存储和 `previous_response_id` |

以上三项均为 MIT，完整版本见仓库锁文件。升级时复核 Responses 流式工具结果续接及内存 Checkpoint 行为。Windows 开发环境使用 Python 3.14，Linux x86-64 运行锁已解析 wheel。Agent 运行日志记录状态、标识和耗时。 [LangChain Agents](https://docs.langchain.com/oss/python/langchain/agents) · [LangGraph](https://pypi.org/project/langgraph/1.2.21/) · [ChatOpenAI](https://docs.langchain.com/oss/python/integrations/chat/openai)

现有 FastAPI 路由继续服务当前客户端。阶段 3 使用同一 Graph 接入 AG-UI，线程鉴权和 SSE 取消属于 HTTP 接入层。

## 本地验收

2026-11-13 使用实际 Loomex 和本地隔离 PostgreSQL 测试库。发布三个测试条目后，工具返回 Cabo 第 1 名、1 人收藏，Azul 和 Splendor 并列第 2 名、0 人收藏。同一线程的下一轮回答正确引用了第一名和收藏数，工具只执行一次。

自动测试覆盖分片工具参数、按调用 ID 回传工具结果、模型流式错误、数据库失败和运行超时。后台全量测试通过 391 项，包含真实 PostgreSQL 测试。AG-UI、Flutter 与 NAS 验收在后续阶段进行。
