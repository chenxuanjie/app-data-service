# 桌游 Agent 的 AG-UI 接口

本地联调入口为 `POST /v1/apps/board_game_agent/ai/agent`。浏览器同源入口加 `/api` 前缀。依赖锁定 `ag-ui-langgraph 0.0.47` 和 `ag-ui-protocol 1.0.0`，沿用 FastAPI `0.148.0`。适配器依赖 MIT 许可，版本升级时应复验中断、流关闭和事件转换。 [AG-UI Python 适配器](https://pypi.org/project/ag-ui-langgraph/0.0.47/)

- 原生端使用现有桌游应用访问令牌。Web 可以使用现有浏览器流程签发的访问令牌，也可以发送同源 Cookie 和 `X-CSRF-Token`。带 Bearer 的 `GET /health` 检查 Agent 是否已初始化，路径相对于上述入口。
- 每次新回合只提交一条纯文本用户消息，带稳定的消息 ID、新的 `runId` 和同一个 `threadId`。SSE 输出文本、工具和运行终止事件。客户端可用 `tools` 广告 `open_rankings`，服务器使用自己的工具定义。
- 仅收到 `RUN_FINISHED.outcome.type == "interrupt"` 且中断原因为 `client_tool` 时才触发导航。用新 `runId` 和相同 `threadId` 重新 POST，`messages: []`，`resume: [{"interruptId":"…","status":"resolved","payload":{"status":"opened"}}]`。结果也可为 `already_open` 或 `unavailable`。`status: "cancelled"` 可用于将工具取消结果交给模型。
- 停止 Run 使用 `POST /{threadId}/runs/{runId}/cancel`，路径相对于 Agent 入口。断开活动 SSE 或显式停止会取消模型请求并关闭当前线程。迟到的恢复请求会被拒绝。HTTP 410 后可用新线程重试。
- 内存线程供本地单进程联调使用。每线程只允许一个活动 Run，最多 32 次请求，空闲 30 分钟后可回收。暂停结果在 5 分钟内有效。PostgreSQL Checkpoint 在阶段 5 接入。

本地测试通过真实 HTTP 验证服务端工具事件、导航中断恢复、取消与断开、线程归属和重复恢复。真实 Loomex 验证了 `open_rankings` 暂停，收到模拟客户端结果后答复「收藏排行榜已打开」。Flutter 实际导航在阶段 4 联调。

当前适配器与锁定的 LangGraph/Pydantic 组合会产生配置接口弃用和运行上下文序列化警告。本地协议测试覆盖了受影响的中断和恢复路径。
