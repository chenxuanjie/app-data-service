# 桌游 Agent 阶段 7 验证

默认运行参数为 Loomex `gpt-6.1-sol`、`reasoning.effort=low` 和标准速度。

## 本地回归

- `tools/run_checks.py` 运行 Ruff 和完整 pytest。HTTP 测试覆盖工具参数和结果续接、取消与断开、鉴权、跨用户隔离、重复恢复拒绝、真实本地 PostgreSQL Checkpoint 重启恢复与清理。
- Flutter 运行 `flutter analyze --no-pub` 和 `flutter test --no-pub`。覆盖工具分发、导航回执、账号切换、删除评论确认、投票幂等以及评论提交去重。
- `tests/fixtures/board_agent_stage7.json` 覆盖聊天、榜单、导航、收藏筛选后打开详情、目录筛选和详情、当前页面、最近浏览、历史计数、投票和撤票、评论提交与取消删除、规则证据及无需执行操作的请求。

## 真实模型验证

使用现有 Windows `.venv` 和已配置的 Loomex 环境变量，在后台工作树根目录运行。

```powershell
$env:PYTHONPATH = (Resolve-Path src).Path
.venv/Scripts/python.exe tests/board_agent_stage7_eval.py
.venv/Scripts/python.exe tests/board_agent_stage7_eval.py --live
```

第一条 Python 命令只列用例。加 `--live` 才调用供应商，可用 `--case <id>` 重复指定用例。请求计数、模型、推理档位和禁止供应商存储在实际出站前校验。独立 FastAPI 实例走当前 AG-UI 和 LangGraph，使用内存 Checkpoint、模拟账号和测试工具回执。成功条件包含对应的工具调用、关键参数、恢复结果和指定回复证据。

## 本轮结果

2026-11-14，Flutter 静态分析及 775 项测试通过；Ruff 与真实本地 PostgreSQL 上的 399 项后台测试通过。
真实 Loomex 全集通过 19 项，删除取消用例的工具链已执行，但回复措辞断言未命中。完善取消措辞与误报成功的断言后，定向复验通过。
该轮全量评估与定向复验分别使用 43 和 3 次实际模型请求。对 Loomex 模型列表的预检返回了 `gpt-6.1-sol`，未返回 `gpt-6-luna`。

## NAS 现场

部署配套 FastAPI 和 Flutter 后，先确认应用的 Agent 健康接口返回 `checkpoint: postgres`。使用测试账号按阶段 6 文档复验 HTTPS、刷新和重启后续聊，再用阶段 7 用例检查实际页面及写入结果。公开评论和投票操作使用专门测试条目，删除分别覆盖取消和确认。真实模型评估的模拟回执无法验证现场权限或 PowerSync 发布状态。
