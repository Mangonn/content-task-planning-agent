"""DeepSeek tool decisions and an explicitly non-model offline policy."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


class ModelUnavailableError(RuntimeError):
    pass


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": {
                **properties, "reason": {"type": "string", "description": "Brief factual decision basis, not hidden reasoning."}},
                "required": [*required, "reason"], "additionalProperties": False}}}


TOOLS = [
    _tool("search_assets", "Find synthetic materials for the requested topic; a partial result can still be useful.",
          {"query": {"type": "string"}}, ["query"]),
    _tool("generate_script", "Create a standard or compact synthetic script. Requires successful asset search.",
          {"template": {"type": "string", "enum": ["standard", "compact"]}}, ["template"]),
    _tool("estimate_plan", "Calculate full cost and completion time for one asset, using the successful script and sunk tool costs.",
          {"asset_id": {"type": "string"}}, ["asset_id"]),
    _tool("save_plan", "Save a feasible plan as a draft only. Requires a successful estimate; never publishes.",
          {"asset_id": {"type": "string"}}, ["asset_id"]),
    _tool("finish", "Stop when the plan is saved or no feasible path remains. Explain only the factual reason.",
          {}, []),
]


INSTRUCTIONS = """You are a content operations planning agent. Select exactly ONE function tool per model response.
Goal: save one content plan within both budget and deadline, or stop with a clear gap. Never claim a failed tool step succeeded.
Use search_assets for the exact topic first. A partial search can be used if it returns assets.
Generate a script only after search. Prefer standard first; if that tool fails, try compact while retaining the successful search.
Estimate an asset before saving. Tool cost and elapsed time already spent are sunk; do not reset them when replanning.
If premium is over budget or too slow, estimate the lean asset. After a feasible estimate, save only that asset.
If all available assets are infeasible, finish. If save_plan fails, observe the failure and either choose a safe next action or finish; do not claim saved.
The provided prior_state may contain successful steps from an earlier attempt. Reuse them instead of restarting.
Put a short factual basis in the reason argument. Never output hidden chain of thought.
"""


class MockPolicy:
    """Observation-driven offline flow check, not a language model."""

    name = "mock_policy"

    def decide(self, request: dict, state: dict) -> dict:
        if state["search"] is None:
            return {"name": "search_assets", "arguments": {"query": request["topic"]},
                    "reason": "先检索与主题匹配的合成素材"}
        if not state["search"].get("ok"):
            return {"name": "finish", "arguments": {}, "reason": "未找到可用素材"}
        if not state["active_script"]:
            standard = state["scripts"].get("standard")
            compact = state["scripts"].get("compact")
            if standard is None:
                return {"name": "generate_script", "arguments": {"template": "standard"},
                        "reason": "先尝试标准脚本"}
            if standard.get("ok"):
                return {"name": "finish", "arguments": {}, "reason": "脚本状态不一致，停止以避免误记成功"}
            if compact is None:
                return {"name": "generate_script", "arguments": {"template": "compact"},
                        "reason": "标准脚本工具失败，保留检索结果并改用精简脚本"}
            return {"name": "finish", "arguments": {}, "reason": "两种脚本均不可用"}
        if state["save"]:
            if state["save"].get("saved"):
                return {"name": "finish", "arguments": {}, "reason": "已观察到方案保存成功"}
            if state["save_attempt"] == state["attempt"]:
                return {"name": "finish", "arguments": {}, "reason": "保存工具失败，保持成功步骤并等待续跑"}
            asset_id = state["save"].get("asset_id")
            if asset_id:
                return {"name": "save_plan", "arguments": {"asset_id": asset_id},
                        "reason": "续跑时复用已有可行估算，重新尝试保存"}
        assets = state["search"]["assets"]
        ordered = sorted(assets, key=lambda asset: (asset["quality"] != "premium", asset["license_cost"]))
        for asset in ordered:
            estimate = state["estimates"].get(asset["asset_id"])
            if estimate is None or estimate.get("script_id") != state["active_script"]["script_id"]:
                return {"name": "estimate_plan", "arguments": {"asset_id": asset["asset_id"]},
                        "reason": "核算该素材的总成本与截止时间"}
            if estimate["feasible"]:
                return {"name": "save_plan", "arguments": {"asset_id": asset["asset_id"]},
                        "reason": "该方案满足预算与时间约束，仅保存草稿"}
        return {"name": "finish", "arguments": {}, "reason": "全部候选方案已估算且均不可行"}

    def observe(self, result: dict) -> None:
        pass


class DeepSeekModel:
    """One model action per response; parallel batches are rejected before tool execution."""

    name = "deepseek_chat"

    def __init__(self):
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key:
            raise ModelUnavailableError("DEEPSEEK_API_KEY is required for --mode deepseek")
        self.model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")
        self.messages = None
        self.rejected_call_ids = []
        self.turn = 0

    def decide(self, request: dict, state: dict) -> dict:
        if self.messages is None:
            context = {"request": request, "prior_state": {
                "spent_cost": state["spent_cost"], "elapsed_minutes": state["elapsed_minutes"],
                "search": state["search"], "scripts": state["scripts"],
                "active_script": state["active_script"], "estimates": state["estimates"],
                "save": state["save"]}}
            self.messages = [{"role": "system", "content": INSTRUCTIONS},
                             {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]
        payload = {"model": self.model, "messages": self.messages, "tools": TOOLS,
                   "tool_choice": "required", "thinking": {"type": "disabled"}, "max_tokens": 1000}
        http_request = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(http_request, timeout=45) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"model_http_{exc.code}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"model_transport_error:{type(exc).__name__}") from exc
        self.turn += 1
        calls = data["choices"][0]["message"].get("tool_calls") or []
        if not calls:
            raise RuntimeError("model_returned_no_tool_call")
        self.messages.append({"role": "assistant", "content": data["choices"][0]["message"].get("content"),
                              "tool_calls": calls})
        if len(calls) > 1:
            self.rejected_call_ids = [call["id"] for call in calls]
            return {"name": "parallel_calls_rejected", "arguments": {},
                    "reason": "多个动作会绕过步骤依赖，要求模型单独重选", "model_turn": self.turn}
        call = calls[0]
        self.call_id = call["id"]
        try:
            args = json.loads(call["function"]["arguments"])
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid_model_tool_arguments") from exc
        return {"name": call["function"]["name"], "arguments": args,
                "reason": str(args.get("reason", "模型选择此动作"))[:200], "model_turn": self.turn}

    def observe(self, result: dict) -> None:
        if self.rejected_call_ids:
            for call_id in self.rejected_call_ids:
                self.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "content": json.dumps({"error": "parallel_calls_rejected"})})
            self.rejected_call_ids = []
            return
        self.messages.append({"role": "tool", "tool_call_id": self.call_id,
                              "content": json.dumps(result, ensure_ascii=False)})
