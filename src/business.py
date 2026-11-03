"""Synthetic business tools, cost arithmetic, and SQLite run state."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def request_key(request: dict) -> str:
    fields = {key: request.get(key) for key in ("topic", "budget", "deadline", "start_at")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone offset")
    return parsed


def preflight(request: dict) -> dict | None:
    """Return an action-free result for missing required input; reject malformed input."""
    if not isinstance(request, dict):
        raise ValueError("input must be a JSON object")
    missing = [field for field in ("topic", "budget", "deadline") if request.get(field) is None or request.get(field) == ""]
    if missing:
        return {"topic": request.get("topic"), "budget": request.get("budget"),
                "deadline": request.get("deadline"), "completion_status": "needs_input",
                "missing_fields": missing,
                "incomplete_reason": "missing_required_input", "save_status": "not_attempted",
                "plan": None, "gaps": {}, "cost_summary": {"spent_cost": 0,
                "total_cost": 0, "minimum_estimated_total": None,
                "remaining_budget": request.get("budget")}, "task_plan": [],
                "steps": [], "tool_results": {}, "trace": [], "failed_step_minutes": 0,
                "published": False}
    if not isinstance(request["topic"], str) or not request["topic"].strip():
        raise ValueError("topic must be a nonempty string")
    if isinstance(request["budget"], bool) or not isinstance(request["budget"], int) or request["budget"] < 0:
        raise ValueError("budget must be a nonnegative integer")
    deadline = _timestamp(request["deadline"], "deadline")
    if "start_at" in request and request["start_at"] is not None:
        start = _timestamp(request["start_at"], "start_at")
        if deadline <= start:
            return {"completion_status": "incomplete", "missing_fields": [],
                    "incomplete_reason": "deadline_not_after_start", "save_status": "not_attempted",
                    "topic": request["topic"], "budget": request["budget"],
                    "deadline": request["deadline"], "plan": None, "gaps": {},
                    "cost_summary": {"spent_cost": 0, "total_cost": 0,
                    "minimum_estimated_total": None,
                    "remaining_budget": request["budget"]}, "task_plan": [],
                    "steps": [], "tool_results": {}, "trace": [], "failed_step_minutes": 0,
                    "published": False}
    return None


def new_state(request: dict) -> dict:
    start_at = request.get("start_at") or datetime.now().astimezone().isoformat(timespec="seconds")
    return {"start_at": start_at, "spent_cost": 0, "elapsed_minutes": 0,
            "search": None, "scripts": {}, "active_script": None, "estimates": {},
            "save": None, "save_attempt": None, "steps": [], "trace": [], "attempt": 0}


class RunStore:
    def __init__(self, db_path: Path):
        self.db = sqlite3.connect(db_path, timeout=10)
        self.db.execute("""CREATE TABLE IF NOT EXISTS runs (
            request_key TEXT PRIMARY KEY, request_json TEXT NOT NULL,
            state_json TEXT NOT NULL, status TEXT NOT NULL
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS plans (
            request_key TEXT PRIMARY KEY, plan_json TEXT NOT NULL
        )""")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def load_state(self, key: str) -> dict | None:
        row = self.db.execute("SELECT state_json FROM runs WHERE request_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def persist(self, key: str, request: dict, state: dict, status: str) -> None:
        self.db.execute("""INSERT INTO runs VALUES (?, ?, ?, ?)
            ON CONFLICT(request_key) DO UPDATE SET state_json=excluded.state_json, status=excluded.status""",
            (key, json.dumps(request, ensure_ascii=False), json.dumps(state, ensure_ascii=False), status),
        )
        self.db.commit()

    def get_plan(self, key: str) -> dict | None:
        row = self.db.execute("SELECT plan_json FROM plans WHERE request_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_plan(self, key: str, plan: dict) -> tuple[dict, bool]:
        """Insert once; return the existing plan if a concurrent request won."""
        try:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.get_plan(key)
            if existing:
                self.db.rollback()
                return existing, True
            self.db.execute("INSERT INTO plans VALUES (?, ?)", (key, json.dumps(plan, ensure_ascii=False)))
            self.db.commit()
            return plan, False
        except sqlite3.Error:
            self.db.rollback()
            raise


class BusinessTools:
    def __init__(self, store: RunStore, *, fail_once: set[str] | None = None,
                 partial_search: bool = False, data_dir: Path | None = None):
        base = data_dir or ROOT / "data"
        self.assets = json.loads((base / "assets.json").read_text(encoding="utf-8"))
        self.costs = json.loads((base / "costs.json").read_text(encoding="utf-8"))
        self.store = store
        self.fail_once = set(fail_once or ())
        self.partial_search = partial_search

    def search_assets(self, query: str, request: dict, state: dict) -> dict:
        if query != request["topic"]:
            return {"ok": False, "error": "query_must_match_topic", "charged_cost": 0, "duration_minutes": 0}
        if state["search"] and state["search"].get("ok"):
            return {**state["search"], "reused": True, "charged_cost": 0, "duration_minutes": 0}
        matches = [asset for asset in self.assets if query in asset["title"] or query in asset["tags"]]
        warnings = []
        if self.partial_search and any(asset["quality"] == "premium" for asset in matches):
            matches = [asset for asset in matches if asset["quality"] != "premium"]
            warnings.append("premium_index_unavailable; lean results retained")
        fee = self.costs["search_assets"]
        return {"ok": bool(matches), "query": query, "assets": matches,
                "partial": bool(warnings), "warnings": warnings,
                "error": None if matches else "no_assets_found",
                "charged_cost": fee["cost"], "duration_minutes": fee["minutes"]}

    def generate_script(self, template: str, request: dict, state: dict) -> dict:
        if not state["search"] or not state["search"].get("ok"):
            return {"ok": False, "error": "search_required", "charged_cost": 0, "duration_minutes": 0}
        if template not in ("standard", "compact"):
            return {"ok": False, "error": "unknown_template", "charged_cost": 0, "duration_minutes": 0}
        existing = state["scripts"].get(template)
        if existing and existing.get("ok"):
            return {**existing, "reused": True, "charged_cost": 0, "duration_minutes": 0}
        if f"script_{template}" in self.fail_once:
            self.fail_once.remove(f"script_{template}")
            return {"ok": False, "template": template, "error": "script_service_timeout",
                    "charged_cost": 0, "duration_minutes": self.costs["generate_script"]["failure_minutes"]}
        fee = self.costs["generate_script"][template]
        digest = hashlib.sha256(f"{request['topic']}:{template}".encode()).hexdigest()[:8]
        script = (f"{request['topic']}：展示主视觉、卖点与行动号召。" if template == "standard"
                  else f"{request['topic']}：一句卖点与行动号召。")
        return {"ok": True, "template": template, "script_id": f"S-{digest}", "script": script,
                "charged_cost": fee["cost"], "duration_minutes": fee["minutes"]}

    def estimate_plan(self, asset_id: str, request: dict, state: dict) -> dict:
        script = state["active_script"]
        search = state["search"]
        if not script or not search or not search.get("ok"):
            return {"ok": False, "error": "search_and_script_required", "charged_cost": 0, "duration_minutes": 0}
        asset = next((item for item in search["assets"] if item["asset_id"] == asset_id), None)
        if asset is None:
            return {"ok": False, "error": "asset_not_in_search_results", "charged_cost": 0, "duration_minutes": 0}
        fee = self.costs["estimate_plan"]
        save_minutes = self.costs["save_plan"]["minutes"]
        window_minutes = int((_timestamp(request["deadline"], "deadline") -
                              _timestamp(state["start_at"], "start_at")).total_seconds() // 60)
        total_cost = state["spent_cost"] + fee["cost"] + asset["license_cost"]
        total_minutes = state["elapsed_minutes"] + fee["minutes"] + asset["production_minutes"] + save_minutes
        budget_gap = max(0, total_cost - request["budget"])
        deadline_gap = max(0, total_minutes - window_minutes)
        return {"ok": True, "asset_id": asset_id, "script_id": script["script_id"],
                "estimated_total_cost": total_cost, "projected_total_minutes": total_minutes,
                "available_minutes": window_minutes, "budget_gap": budget_gap,
                "deadline_gap_minutes": deadline_gap, "feasible": budget_gap == 0 and deadline_gap == 0,
                "charged_cost": fee["cost"], "duration_minutes": fee["minutes"]}

    def save_plan(self, asset_id: str, request: dict, state: dict, key: str) -> dict:
        estimate = state["estimates"].get(asset_id)
        script = state["active_script"]
        search = state["search"]
        if not estimate or not estimate.get("feasible") or not script or not search or not search.get("ok"):
            return {"ok": False, "saved": False, "error": "feasible_estimate_required",
                    "charged_cost": 0, "duration_minutes": 0}
        if estimate["script_id"] != script["script_id"]:
            return {"ok": False, "saved": False, "error": "estimate_stale_script",
                    "charged_cost": 0, "duration_minutes": 0}
        asset = next((item for item in search["assets"] if item["asset_id"] == asset_id), None)
        if asset is None:
            return {"ok": False, "saved": False, "error": "asset_not_in_search_results",
                    "charged_cost": 0, "duration_minutes": 0}
        total_cost = state["spent_cost"] + asset["license_cost"] + self.costs["save_plan"]["cost"]
        total_minutes = state["elapsed_minutes"] + asset["production_minutes"] + self.costs["save_plan"]["minutes"]
        available = int((_timestamp(request["deadline"], "deadline") -
                         _timestamp(state["start_at"], "start_at")).total_seconds() // 60)
        if total_cost > request["budget"] or total_minutes > available:
            return {"ok": False, "saved": False, "error": "estimate_no_longer_feasible",
                    "budget_gap": max(0, total_cost - request["budget"]),
                    "deadline_gap_minutes": max(0, total_minutes - available),
                    "charged_cost": 0, "duration_minutes": 0}
        if "save_plan" in self.fail_once:
            self.fail_once.remove("save_plan")
            return {"ok": False, "saved": False, "error": "plan_store_timeout",
                    "charged_cost": 0, "duration_minutes": self.costs["save_plan"]["minutes"]}
        plan = {"plan_id": key[:12], "topic": request["topic"], "asset_id": asset_id,
                "script_id": script["script_id"], "script_template": script["template"],
                "script": script["script"], "total_cost": total_cost,
                "total_minutes": total_minutes, "budget": request["budget"],
                "deadline": request["deadline"], "status": "saved_draft", "published": False}
        try:
            saved, duplicate = self.store.save_plan(key, plan)
        except sqlite3.Error as exc:
            return {"ok": False, "saved": False, "error": f"storage_error:{type(exc).__name__}",
                    "charged_cost": 0, "duration_minutes": 0}
        return {"ok": True, "saved": True, "duplicate": duplicate, "plan": saved,
                "charged_cost": 0 if duplicate else asset["license_cost"],
                "duration_minutes": 0 if duplicate else asset["production_minutes"] + self.costs["save_plan"]["minutes"]}
