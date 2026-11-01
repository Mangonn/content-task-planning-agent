# 03 效果验证

## 执行方式与结果

2026-11-01 在 Python 3.12 环境执行：

```bash
python3 -m unittest discover -s tests -v
python3 examples/run_examples.py
python3 examples/run_model_examples.py  # 需 DEEPSEEK_API_KEY 与网络
```

结果：17 项测试通过，13 组离线 CLI 输入、实际输出和 Trace 已生成；两组真实 DeepSeek 模型案例完成并保存草稿。测试日志见 [`examples/test-results.txt`](../examples/test-results.txt)。离线结果是流程与业务约束验证；真实模型记录才用于验证模型观察后改选动作，且只代表这两次运行。

## 验收点对应证据

| 验收点 | 输入、实际输出及 Trace | 实际结果 |
| --- | --- | --- |
| 正常完成 | [`normal` 输入](../examples/inputs/normal.json)、[`输出`](../examples/outputs/normal.json)、[`Trace`](../examples/traces/normal.json) | A-PRO，成本 12，耗时 115 分钟，草稿保存成功 |
| 缺预算、缺截止时间 | [`missing_budget`](../examples/outputs/missing_budget.json)、[`missing_deadline`](../examples/outputs/missing_deadline.json) | `needs_input`，无工具步骤、无草稿 |
| 预算超限并重规划 | [`预算输入`](../examples/inputs/budget_replan.json)、[`真实模型输出`](../examples/model_outputs/deepseek_budget_replan.json)、[`真实 Trace`](../examples/model_traces/deepseek_budget_replan.json) | 初选 A-PRO 估价 12 不可行，后选 A-LEAN 估价 8，可行并保存 |
| 标准脚本工具失败 | [`故障输入`](../examples/inputs/tool_failure.json)、[`真实模型输出`](../examples/model_outputs/deepseek_tool_failure.json)、[`真实 Trace`](../examples/model_traces/deepseek_tool_failure.json) | 标准脚本超时标记失败，保留成功检索，精简脚本成功，成本 11 |
| 部分成功结果 | [`partial_search` 输出](../examples/outputs/partial_search.json)、[`Trace`](../examples/traces/partial_search.json) | 高配素材索引不可用，使用保留下来的轻量素材，成本 8 |
| 全部超预算 | [`budget_impossible` 输出](../examples/outputs/budget_impossible.json)、[`Trace`](../examples/traces/budget_impossible.json) | 未保存，最少追加预算 2 |
| 截止时间不足 | [`deadline_impossible` 输出](../examples/outputs/deadline_impossible.json)、[`Trace`](../examples/traces/deadline_impossible.json) | 未保存，最少追加 15 分钟 |
| 重复执行 | [`duplicate` 输出](../examples/outputs/duplicate.json)、[`Trace`](../examples/traces/duplicate.json) | 返回原 `plan_id`，`already_exists`，数据库仅一份草稿 |
| 保存失败及续跑 | [`save_failure`](../examples/outputs/save_failure.json)、[`save_recovered`](../examples/outputs/save_recovered.json) | 第一次 `failed/incomplete`，无草稿；第二次复用已成功步骤后保存 |
| 失败步骤续跑 | [`resume_first`](../examples/outputs/resume_first.json)、[`resume_complete`](../examples/outputs/resume_complete.json) | 第一次到步数上限；续跑不重复检索，失败脚本仍记失败 |

上述两份真实模型 Trace 均逐步记录动作、参数、工具返回摘要、下一动作及简短依据。若模型返回并行调用，执行器先拒绝，Trace 留下 `parallel_calls_rejected`，之后再由模型单步选择。此控制事件不产生费用。

## 失败与边界核查

- 单元测试直接让假模型越过依赖调用保存，业务工具返回 `feasible_estimate_required`，草稿数为 0。
- 注入保存超时，第一次明确未完成；续跑从 SQLite 状态恢复。数据库不可用时 CLI 返回 `storage_unavailable`。
- 同一请求二次执行验证草稿数为 1，费用不重复计入。
- 模拟草稿先写入、进度状态后写入的中断：再次请求从 `plans` 表恢复 `completed/already_exists`，草稿仍只有一份。
- `max_steps` 与 `max_tools` 达限时分别返回 `step_limit`、`tool_limit`；不会把未完成写成成功。
- 没有模型密钥时 `--mode deepseek` 返回 `model_unavailable`，不会用 Mock 冒充真实模型结果。

## 局限

模型两次成功轨迹只是小样本，不代表稳定成功率；模型可能选择无效调用、耗尽上限或遇到网络错误。示例资料与故障都是合成的，费用单位不对应真实货币；固定规划窗口不使用实时日历。SQLite 适合单机演示，未验证高并发、跨机器恢复或真实审批系统。真实脚本质量没有人工内容评审。
