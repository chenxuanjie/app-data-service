# 桌游 Agent 的 AG-UI 接口

入口为 `POST /v1/apps/board_game_agent/ai/agent`。Web 同源入口加 `/api` 前缀。采用 `ag-ui-langgraph 0.0.47` 和 `ag-ui-protocol 1.0.0`。原生端使用应用 Bearer，Web 支持现有 Bearer 或同源 Cookie + CSRF。

- 新轮提交一条用户消息和稳定 `threadId`，每个请求有独立 `runId`。新轮允许提交模型、推理档、服务档和资料模式。后台使用已登记的工具描述，不信任客户端上报的 schema。
- 新线程可用 `history` 导入最近的已完成纯文本消息，最多 12 条和 24 KB。已有检查点继续使用服务端历史。模型上下文有独立预算，保留当前用户轮及完整工具调用链。
- 客户端核对 `RUN_FINISHED.outcome` 中的中断与本轮工具 ID。执行本地操作后，使用 `resume[]` 回同一线程。已完成轮的相同末次回执重发会回放终态 SSE。不会再次执行工具。
- `GET /{threadId}` 可查询当前用户轮的状态、待恢复中断或成功结果。`POST /{threadId}/runs/{runId}/cancel` 停止当前轮。保留之前成功的检查点。`DELETE /{threadId}` 清除会话。达到 32 个用户轮后，可调 `POST /{threadId}/renew` 重建已达上限且无挂起动作的线程。
- 炸弹猫原有索引和引用验证仍在应用侧完成。限定的一次性规则请求可携带已验证的 PDF 内容交由 LangGraph 生成，不提供客户端或业务工具。账号旧生成转发已退役。

带鉴权的 `GET /health` 返回 Agent 是否准备就绪和检查点类型。适配器升级时复验中断、流关闭和事件转换。 [AG-UI Python 适配器](https://pypi.org/project/ag-ui-langgraph/0.0.47/)
