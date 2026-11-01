# 内容任务规划 Agent（题目 2）

一个可运行的 CLI：按主题检索合成素材、生成脚本、核算预算与截止时间，并把可行方案保存为本地草稿。提供 DeepSeek 真实模型决策入口和明确标记的离线 Mock 入口。没有发布工具或发布状态。

## 环境与安装

- Python 3.11+；业务代码只用标准库，`requirements.txt` 无外部依赖。
- 在本项目根目录运行。离线模式无需账号或环境变量。

```bash
python3 --version
python3 -m unittest discover -s tests -v
```

## 最快复现

```bash
python3 -m src.cli examples/inputs/normal.json --mode mock --db work/demo.sqlite3
python3 -m src.cli examples/inputs/budget_replan.json --mode mock --db work/budget.sqlite3
python3 -m src.cli examples/inputs/tool_failure.json --mode mock --fail-once script_standard --db work/failure.sqlite3
```

CLI 输出 JSON；成功退出码 0，未完成或工具/输入错误退出码 2，缺少必填信息返回 `needs_input` 和退出码 0。`--output path.json` 可将结果写入文件。`--db` 指向 SQLite 状态库；**重复执行须使用相同输入和同一个数据库**。

```bash
python3 -m src.cli examples/inputs/normal.json --mode mock --db work/duplicate.sqlite3 --output work/first.json
python3 -m src.cli examples/inputs/normal.json --mode mock --db work/duplicate.sqlite3 --output work/again.json
python3 -m src.cli examples/inputs/normal.json --mode mock --fail-once save_plan --db work/save-failure.sqlite3 --output work/save-failure.json
```

`work/save-failure.json` 的 `save_status` 为 `failed`，`completion_status` 为 `incomplete`，不会出现已保存方案。用相同输入及数据库再次运行可续跑成功步骤。`--max-steps`、`--max-tools` 分别限制每次运行的模型决策步数与业务工具调用数；达到上限返回明确的 `step_limit` 或 `tool_limit`。

## 真实模型入口

把 `.env.example` 复制为 `.env`，填入自己的 `DEEPSEEK_API_KEY`，勿提交 `.env`。也可以在运行环境设置该变量。模型不可用时 CLI 返回 `model_unavailable`，**不会偷偷切到 Mock**。如本地 Python 证书链不完整，可设置 `SSL_CERT_FILE` 为系统证书路径。

```bash
cp .env.example .env
python3 -m src.cli examples/inputs/budget_replan.json --mode deepseek --db work/deepseek-budget.sqlite3
python3 -m src.cli examples/inputs/tool_failure.json --mode deepseek --fail-once script_standard --db work/deepseek-failure.sqlite3
```

DeepSeek 通过 Chat Completions 的 function calling 选择**一个**动作；工具结果作为 tool message 回传，再由模型选下一步。离线 `MockPolicy` 是显式的规则策略，仅用于验证流程、状态与边界，**不能证明真实模型决策效果**。真实模型记录见 [`examples/model_traces/`](examples/model_traces/)；复现脚本为 `python3 examples/run_model_examples.py`，需模型密钥和网络。

## 评审入口

| 内容 | 文件 |
| --- | --- |
| 输入、实际输出、逐步 Trace | [`examples/`](examples/) |
| 13 组离线案例重跑 | `python3 examples/run_examples.py` |
| 16 项自动测试与结果 | [`tests/test_agent.py`](tests/test_agent.py)、[`examples/test-results.txt`](examples/test-results.txt) |
| 成本与素材 | [`data/costs.json`](data/costs.json)、[`data/assets.json`](data/assets.json) |
| 需求、设计、评估、AI 使用、复盘 | [`docs/`](docs/) |

示例中的预算单位、耗时、素材、故障均为合成数据。示例输入使用固定 `start_at` 与 `deadline` 以便复核计算；系统将它们视为规划窗口，不读取真实日历。此项目不对接真实素材、费用、审批或发布系统。
