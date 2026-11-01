"""Bounded model-action / tool-observation loop with persisted progress."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path

from .business import BusinessTools, RunStore, new_state, preflight, request_key
from .business import _timestamp


def _task_plan(state: dict) -> list[dict]:
    search = state["search"]
    scripts = state["scripts"]
    estimates = state["estimates"]
    save = state["save"]
    return [
        {"step": "search_assets", "status": "completed" if search and search.get("ok") else "failed" if search else "pending"},
        {"step": "generate_script", "status": "completed" if state["active_script"] else "failed" if scripts else "pending"},
        {"step": "estimate_plan", "status": "completed" if any(x.get("feasible") for x in estimates.values())
         else "infeasible" if estimates else "pending"},
        {"step": "save_plan", "status": "completed" if save and save.get("saved") else "failed" if save else "pending"},
    ]


def _reason(state: dict) -> tuple[str, dict]:
    if state["save"] and not state["save"].get("saved"):
        return "save_failed:" + state["save"].get("error", "unknown"), {}
    if state["search"] and not state["search"].get("ok"):
        return state["search"].get("error", "search_failed"), {}
    if state["scripts"] and not state["active_script"]:
        return "script_generation_failed", {}
    estimates = [x for x in state["estimates"].values() if x.get("ok")]
    assets = state["search"]["assets"] if state["search"] and state["search"].get("ok") else []
    if assets and len(estimates) >= len(assets) and not any(x["feasible"] for x in estimates):
        budget_gap = min(x["budget_gap"] for x in estimates)
        deadline_gap = min(x["deadline_gap_minutes"] for x in estimates)
        reason = "budget_shortfall" if budget_gap and not deadline_gap else "deadline_shortfall" if deadline_gap and not budget_gap else "no_feasible_plan"
        return reason, {"minimum_additional_budget": budget_gap,
                        "minimum_extra_minutes": deadline_gap}
    return "not_saved", {}


def _result(request: dict, state: dict, mode: str, *, forced_reason: str | None = None,
            existing_plan: dict | None = None) -> dict:
    plan = existing_plan or (state["save"] or {}).get("plan")
    save = state["save"]
    if existing_plan:
        save_status = "already_exists"
    elif save and save.get("saved"):
        save_status = "already_exists" if save.get("duplicate") else "saved"
    elif save:
        save_status = "failed"
    else:
        save_status = "not_attempted"
    if forced_reason:
        completion, incomplete_reason, gaps = "incomplete", forced_reason, {}
    elif plan:
        completion, incomplete_reason, gaps = "complete", None, {}
    else:
        incomplete_reason, gaps = _reason(state)
        completion = "incomplete"
    deadline = _timestamp(request["deadline"], "deadline")
    start = _timestamp(state["start_at"], "start_at")
    available = int((deadline - start).total_seconds() // 60)
    estimates = [x for x in state["estimates"].values() if x.get("ok")]
    trace = deepcopy(state["trace"])
    for index, entry in enumerate(trace):
        entry["next_action"] = trace[index + 1]["action"] if index + 1 < len(trace) else None
    return {
        "topic": request["topic"], "budget": request["budget"],
        "start_at": state["start_at"], "deadline": request["deadline"],
        "completion_status": completion, "incomplete_reason": incomplete_reason,
        "gaps": gaps, "save_status": save_status, "plan": plan,
        "task_plan": _task_plan(state), "steps": state["steps"],
        "tool_results": {"search_assets": state["search"], "generate_script": state["scripts"],
                         "estimate_plan": state["estimates"], "save_plan": state["save"]},
        "cost_summary": {"spent_cost": state["spent_cost"],
                         "total_cost": plan["total_cost"] if plan else state["spent_cost"],
                         "minimum_estimated_total": min((x["estimated_total_cost"] for x in estimates), default=None),
                         "remaining_budget": request["budget"] - (plan["total_cost"] if plan else state["spent_cost"])},
        "elapsed_minutes": state["elapsed_minutes"], "available_minutes": available,
        "attempt": state["attempt"], "mode": mode, "published": False, "trace": trace,
    }


def _record(state: dict, name: str, args: dict, observation: dict, reason: str,
            model_turn: int | None, actor: str) -> None:
    step_number = len(state["trace"]) + 1
    entry = {"step": step_number, "attempt": state["attempt"], "actor": actor,
             "action": name, "arguments": args, "observation": observation,
             "decision_basis": reason}
    if model_turn is not None:
        entry["model_turn"] = model_turn
    state["trace"].append(entry)
    state["steps"].append({"step": step_number, "attempt": state["attempt"],
                           "action": name, "arguments": args,
                           "status": "succeeded" if observation.get("ok") else "failed",
                           "tool_result": observation})


def run(request: dict, *, model, db_path: Path, fail_once: set[str] | None = None,
        partial_search: bool = False, max_steps: int = 12, max_tools: int = 10,
        data_dir: Path | None = None) -> dict:
    initial = preflight(request)
    if initial:
        return {**initial, "mode": "preflight"}
    if max_steps < 1 or max_tools < 1:
        raise ValueError("max_steps and max_tools must be positive")
    store = RunStore(db_path)
    key = request_key(request)
    try:
        state = store.load_state(key) or new_state(request)
        existing = store.get_plan(key)
        if existing:
            shown = deepcopy(state)
            if not shown["save"] or not shown["save"].get("saved"):
                shown["save"] = {"ok": True, "saved": True, "duplicate": True,
                                 "plan": existing, "confirmed_by": "plans_table",
                                 "charged_cost": 0, "duration_minutes": 0}
                shown["spent_cost"] = max(shown["spent_cost"], existing["total_cost"])
                shown["elapsed_minutes"] = max(shown["elapsed_minutes"], existing["total_minutes"])
            shown["trace"].append({"step": len(shown["trace"]) + 1, "attempt": shown["attempt"],
                                   "actor": "system", "action": "idempotency_lookup",
                                   "arguments": {"request_key": key},
                                   "observation": {"found": True, "plan_id": existing["plan_id"]},
                                   "decision_basis": "相同请求已保存，返回原方案"})
            return _result(request, shown, model.name, existing_plan=existing)
        state["attempt"] += 1
        if state["attempt"] > 1:
            state["trace"].append({"step": len(state["trace"]) + 1, "attempt": state["attempt"],
                                   "actor": "system", "action": "resume",
                                   "arguments": {},
                                   "observation": {"successful_search_retained": bool(state["search"] and state["search"].get("ok")),
                                                   "successful_script_retained": bool(state["active_script"])},
                                   "decision_basis": "从持久化状态继续，不重做成功步骤"})
        store.persist(key, request, state, "running")
        tools = BusinessTools(store, fail_once=fail_once, partial_search=partial_search, data_dir=data_dir)
        tool_count = 0
        for _ in range(max_steps):
            try:
                decision = model.decide(request, state)
            except Exception as exc:
                state["trace"].append({"step": len(state["trace"]) + 1, "attempt": state["attempt"],
                                       "actor": model.name, "action": "model_error",
                                       "arguments": {}, "observation": str(exc),
                                       "decision_basis": "模型不可用或返回格式异常"})
                store.persist(key, request, state, "incomplete")
                return _result(request, state, model.name, forced_reason="model_error")
            name = decision.get("name")
            args = decision.get("arguments", {})
            reason = str(decision.get("reason", ""))[:200]
            if not isinstance(args, dict):
                store.persist(key, request, state, "incomplete")
                return _result(request, state, model.name, forced_reason="invalid_model_arguments")
            clean_args = {k: v for k, v in args.items() if k != "reason"}
            if name == "finish":
                state["trace"].append({"step": len(state["trace"]) + 1, "attempt": state["attempt"],
                                       "actor": model.name, "action": "finish", "arguments": {},
                                       "observation": "stopped", "decision_basis": reason,
                                       **({"model_turn": decision["model_turn"]} if "model_turn" in decision else {})})
                result = _result(request, state, model.name)
                store.persist(key, request, state, result["completion_status"])
                return result
            if name == "parallel_calls_rejected":
                observation = {"ok": False, "error": "parallel_calls_rejected",
                               "charged_cost": 0, "duration_minutes": 0}
                _record(state, name, clean_args, observation, reason,
                        decision.get("model_turn"), model.name)
                store.persist(key, request, state, "running")
                model.observe(observation)
                continue
            if tool_count >= max_tools:
                state["trace"].append({"step": len(state["trace"]) + 1, "attempt": state["attempt"],
                                       "actor": "system", "action": "tool_limit", "arguments": {},
                                       "observation": "not executed", "decision_basis": "工具调用上限"})
                store.persist(key, request, state, "incomplete")
                return _result(request, state, model.name, forced_reason="tool_limit")
            tool_count += 1
            if name == "search_assets" and isinstance(args.get("query"), str):
                observation = tools.search_assets(args["query"], request, state)
            elif name == "generate_script" and isinstance(args.get("template"), str):
                observation = tools.generate_script(args["template"], request, state)
            elif name == "estimate_plan" and isinstance(args.get("asset_id"), str):
                observation = tools.estimate_plan(args["asset_id"], request, state)
            elif name == "save_plan" and isinstance(args.get("asset_id"), str):
                observation = tools.save_plan(args["asset_id"], request, state, key)
                observation.setdefault("asset_id", args["asset_id"])
            else:
                observation = {"ok": False, "error": "unknown_tool_or_arguments",
                               "charged_cost": 0, "duration_minutes": 0}
            state["spent_cost"] += observation.get("charged_cost", 0)
            state["elapsed_minutes"] += observation.get("duration_minutes", 0)
            if name == "search_assets":
                state["search"] = observation
            elif name == "generate_script" and isinstance(args.get("template"), str):
                state["scripts"][args["template"]] = observation
                if observation.get("ok"):
                    state["active_script"] = observation
            elif name == "estimate_plan" and isinstance(args.get("asset_id"), str):
                state["estimates"][args["asset_id"]] = observation
            elif name == "save_plan":
                state["save"] = observation
                state["save_attempt"] = state["attempt"]
            _record(state, name, clean_args, observation, reason, decision.get("model_turn"), model.name)
            store.persist(key, request, state, "running")
            model.observe(observation)
        store.persist(key, request, state, "incomplete")
        return _result(request, state, model.name, forced_reason="step_limit")
    finally:
        store.close()
