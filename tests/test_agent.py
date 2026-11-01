import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agent import run
from src.business import RunStore, request_key
from src.models import DeepSeekModel, MockPolicy


ROOT = Path(__file__).resolve().parents[1]


class PlanningAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "plans.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def request(self, name):
        return json.loads((ROOT / "examples/inputs" / f"{name}.json").read_text())

    def case(self, name, **kwargs):
        return run(self.request(name), model=MockPolicy(), db_path=self.db, **kwargs)

    def plan_count(self):
        with sqlite3.connect(self.db) as connection:
            return connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    def test_normal_plan_is_saved_with_exact_cost_and_time(self):
        result = self.case("normal")
        self.assertEqual(result["completion_status"], "complete")
        self.assertEqual(result["plan"]["asset_id"], "A-PRO")
        self.assertEqual(result["plan"]["total_cost"], 12)
        self.assertEqual(result["cost_summary"]["spent_cost"], 12)
        self.assertEqual(result["elapsed_minutes"], 115)
        self.assertFalse(result["published"])
        self.assertEqual(self.plan_count(), 1)

    def test_budget_overrun_replans_from_12_to_8(self):
        result = self.case("budget_replan")
        self.assertEqual(result["completion_status"], "complete")
        self.assertEqual(result["tool_results"]["estimate_plan"]["A-PRO"]["estimated_total_cost"], 12)
        self.assertFalse(result["tool_results"]["estimate_plan"]["A-PRO"]["feasible"])
        self.assertEqual(result["tool_results"]["estimate_plan"]["A-LEAN"]["estimated_total_cost"], 8)
        self.assertEqual(result["plan"]["asset_id"], "A-LEAN")
        self.assertEqual(result["plan"]["total_cost"], 8)
        self.assertEqual(result["trace"][2]["next_action"], "estimate_plan")

    def test_missing_budget_or_deadline_needs_input_without_tools(self):
        for name, field in (("missing_budget", "budget"), ("missing_deadline", "deadline")):
            with self.subTest(name=name):
                result = self.case(name)
                self.assertEqual(result["completion_status"], "needs_input")
                self.assertIn(field, result["missing_fields"])
                self.assertEqual(result["steps"], [])
                self.assertFalse(self.db.exists())

    def test_script_failure_uses_compact_and_keeps_search(self):
        result = self.case("tool_failure", fail_once={"script_standard"})
        self.assertEqual(result["completion_status"], "complete")
        self.assertEqual(result["plan"]["script_template"], "compact")
        self.assertEqual(result["plan"]["total_cost"], 11)
        self.assertEqual(result["steps"][0]["action"], "search_assets")
        self.assertEqual(result["steps"][0]["status"], "succeeded")
        self.assertEqual(result["steps"][1]["action"], "generate_script")
        self.assertEqual(result["steps"][1]["status"], "failed")
        self.assertEqual(result["steps"][1]["tool_result"]["error"], "script_service_timeout")
        self.assertEqual(result["steps"][2]["arguments"]["template"], "compact")

    def test_partial_search_keeps_lean_result(self):
        result = self.case("partial_search", partial_search=True)
        self.assertEqual(result["completion_status"], "complete")
        self.assertTrue(result["tool_results"]["search_assets"]["partial"])
        self.assertEqual(result["plan"]["asset_id"], "A-LEAN")
        self.assertEqual(result["cost_summary"]["total_cost"], 8)

    def test_no_budget_feasible_returns_gap_without_save(self):
        result = self.case("budget_impossible")
        self.assertEqual(result["completion_status"], "incomplete")
        self.assertEqual(result["incomplete_reason"], "budget_shortfall")
        self.assertEqual(result["gaps"]["minimum_additional_budget"], 2)
        self.assertEqual(result["cost_summary"]["spent_cost"], 3)
        self.assertIsNone(result["plan"])
        self.assertEqual(self.plan_count(), 0)

    def test_no_deadline_feasible_returns_time_gap(self):
        result = self.case("deadline_impossible")
        self.assertEqual(result["completion_status"], "incomplete")
        self.assertEqual(result["incomplete_reason"], "deadline_shortfall")
        self.assertEqual(result["gaps"]["minimum_extra_minutes"], 15)
        self.assertEqual(self.plan_count(), 0)

    def test_duplicate_execution_reuses_one_plan_without_extra_cost(self):
        first = self.case("normal")
        again = self.case("normal")
        self.assertEqual(again["completion_status"], "complete")
        self.assertEqual(again["save_status"], "already_exists")
        self.assertEqual(again["plan"]["plan_id"], first["plan"]["plan_id"])
        self.assertEqual(again["cost_summary"]["total_cost"], 12)
        self.assertEqual(self.plan_count(), 1)

    def test_saved_plan_recovers_if_progress_snapshot_lags(self):
        first = self.case("normal")
        request = self.request("normal")
        key = request_key(request)
        store = RunStore(self.db)
        state = store.load_state(key)
        state["save"] = None
        state["spent_cost"] = 3
        state["elapsed_minutes"] = 70
        store.persist(key, request, state, "running")
        store.close()
        again = self.case("normal")
        self.assertEqual(again["save_status"], "already_exists")
        self.assertEqual(again["task_plan"][-1]["status"], "completed")
        self.assertEqual(again["tool_results"]["save_plan"]["confirmed_by"], "plans_table")
        self.assertEqual(again["cost_summary"]["spent_cost"], first["plan"]["total_cost"])
        self.assertEqual(again["elapsed_minutes"], first["plan"]["total_minutes"])
        self.assertEqual(self.plan_count(), 1)

    def test_failed_step_can_resume_without_repeating_successful_search(self):
        first = self.case("tool_failure", fail_once={"script_standard"}, max_steps=2)
        self.assertEqual(first["completion_status"], "incomplete")
        self.assertEqual(first["incomplete_reason"], "step_limit")
        self.assertEqual([x["status"] for x in first["steps"]], ["succeeded", "failed"])
        second = self.case("tool_failure")
        self.assertEqual(second["completion_status"], "complete")
        self.assertEqual(second["attempt"], 2)
        self.assertEqual([x["action"] for x in second["steps"]].count("search_assets"), 1)
        self.assertEqual(second["steps"][1]["status"], "failed")
        self.assertEqual(second["steps"][2]["arguments"]["template"], "compact")

    def test_save_failure_is_not_success_and_can_resume(self):
        first = self.case("normal", fail_once={"save_plan"})
        self.assertEqual(first["completion_status"], "incomplete")
        self.assertEqual(first["save_status"], "failed")
        self.assertEqual(first["incomplete_reason"], "save_failed:plan_store_timeout")
        self.assertEqual(first["steps"][-1]["status"], "failed")
        self.assertEqual(self.plan_count(), 0)
        second = self.case("normal")
        self.assertEqual(second["completion_status"], "complete")
        self.assertEqual(second["save_status"], "saved")
        self.assertEqual([x["action"] for x in second["steps"]].count("search_assets"), 1)
        self.assertEqual(self.plan_count(), 1)

    def test_model_cannot_save_without_dependencies(self):
        class AdversarialModel:
            name = "scripted_adversary"

            def decide(self, request, state):
                return {"name": "save_plan", "arguments": {"asset_id": "A-PRO"}, "reason": "attempt"} if state["save"] is None else {"name": "finish", "arguments": {}, "reason": "stop"}

            def observe(self, result):
                pass

        result = run(self.request("normal"), model=AdversarialModel(), db_path=self.db)
        self.assertEqual(result["save_status"], "failed")
        self.assertEqual(result["steps"][0]["tool_result"]["error"], "feasible_estimate_required")
        self.assertEqual(self.plan_count(), 0)

    def test_step_and_tool_limits_are_explicit(self):
        for keyword, expected in (({"max_steps": 2}, "step_limit"), ({"max_tools": 2}, "tool_limit")):
            with self.subTest(expected=expected):
                db = Path(self.temp.name) / f"{expected}.sqlite3"
                result = run(self.request("normal"), model=MockPolicy(), db_path=db, **keyword)
                self.assertEqual(result["completion_status"], "incomplete")
                self.assertEqual(result["incomplete_reason"], expected)

    def test_deepseek_adapter_returns_tool_observation(self):
        responses = [
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "search_assets",
                 "arguments": '{"query":"夏日新品","reason":"search"}'}}
            ]}}]},
            {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_2", "type": "function", "function": {"name": "finish",
                 "arguments": '{"reason":"done"}'}}
            ]}}]},
        ]
        payloads = []

        def fake_urlopen(http_request, timeout):
            payloads.append(json.loads(http_request.data))
            return io.BytesIO(json.dumps(responses[len(payloads) - 1]).encode())

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch(
            "urllib.request.urlopen", side_effect=fake_urlopen
        ):
            model = DeepSeekModel()
            first = model.decide(self.request("normal"), {"spent_cost": 0, "elapsed_minutes": 0,
                "search": None, "scripts": {}, "active_script": None, "estimates": {}, "save": None})
            model.observe({"ok": True})
            second = model.decide(self.request("normal"), {})
        self.assertEqual(first["name"], "search_assets")
        self.assertEqual(second["name"], "finish")
        self.assertEqual(payloads[1]["messages"][-1]["role"], "tool")
        self.assertEqual(json.loads(payloads[1]["messages"][-1]["content"]), {"ok": True})

    def test_parallel_model_actions_are_rejected(self):
        response = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "one", "type": "function", "function": {"name": "search_assets", "arguments": '{"query":"夏日新品","reason":"search"}'}},
            {"id": "two", "type": "function", "function": {"name": "generate_script", "arguments": '{"template":"standard","reason":"script"}'}}
        ]}}]}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-only"}), patch(
            "urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())
        ):
            result = run(self.request("normal"), model=DeepSeekModel(), db_path=self.db, max_steps=1)
        self.assertEqual(result["steps"][0]["action"], "parallel_calls_rejected")
        self.assertEqual(result["cost_summary"]["spent_cost"], 0)
        self.assertEqual(self.plan_count(), 0)

    def test_cli_missing_model_key_points_to_mock(self):
        env = os.environ.copy()
        env["DEEPSEEK_API_KEY"] = ""
        completed = subprocess.run(
            [sys.executable, "-m", "src.cli", "examples/inputs/normal.json", "--mode", "deepseek",
             "--db", str(self.db)], cwd=ROOT, env=env, capture_output=True, text=True,
        )
        result = json.loads(completed.stderr)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["completion_status"], "model_unavailable")
        self.assertIn("--mode mock", result["hint"])

    def test_cli_database_unavailable_is_explicit(self):
        completed = subprocess.run(
            [sys.executable, "-m", "src.cli", "examples/inputs/normal.json", "--db", "data"],
            cwd=ROOT, capture_output=True, text=True,
        )
        result = json.loads(completed.stderr)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(result["completion_status"], "failed")
        self.assertIn("storage_unavailable", result["error"])


if __name__ == "__main__":
    unittest.main()
