"""Collect two genuine model feedback paths without overwriting good traces on failure."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "examples/model_outputs"
TRACES = ROOT / "examples/model_traces"
parser = argparse.ArgumentParser()
parser.add_argument("--cases", nargs="+", choices=("budget_replan", "tool_failure", "save_failure"),
                    default=("budget_replan", "tool_failure", "save_failure"))
selected = parser.parse_args().cases


def verify(case: str, result: dict) -> None:
    trace = result["trace"]
    if result["mode"] != "deepseek_chat" or result["completion_status"] != "complete":
        raise RuntimeError(f"{case}: real model did not finish a feasible plan")
    if case == "budget_replan":
        estimates = [x for x in trace if x["action"] == "estimate_plan"]
        if not (any(x["arguments"].get("asset_id") == "A-PRO" and
                    x["observation"].get("estimated_total_cost") == 12 and
                    x["observation"].get("feasible") is False for x in estimates)
                and any(x["arguments"].get("asset_id") == "A-LEAN" and
                        x["observation"].get("estimated_total_cost") == 8 and
                        x["observation"].get("feasible") is True for x in estimates)
                and result["plan"]["asset_id"] == "A-LEAN"):
            raise RuntimeError("budget_replan: model did not demonstrate 12-to-8 replanning")
    if case == "tool_failure":
        scripts = [x for x in trace if x["action"] == "generate_script"]
        if not (any(x["arguments"].get("template") == "standard" and
                    x["observation"].get("error") == "script_service_timeout" for x in scripts)
                and any(x["arguments"].get("template") == "compact" and
                        x["observation"].get("ok") for x in scripts)
                and result["plan"]["script_template"] == "compact"):
            raise RuntimeError("tool_failure: model did not demonstrate failure adaptation")
    if case == "save_failure":
        saves = [x for x in trace if x["action"] == "save_plan"]
        if not (any(x["observation"].get("error") == "plan_store_timeout" for x in saves)
                and any(x["observation"].get("saved") for x in saves)):
            raise RuntimeError("save_failure: no observed recovery after failed save")


results = {}
with tempfile.TemporaryDirectory() as directory:
    for case in selected:
        input_name = "normal" if case == "save_failure" else case
        command = [sys.executable, "-m", "src.cli", f"examples/inputs/{input_name}.json",
                   "--mode", "deepseek", "--db", str(Path(directory) / f"{case}.sqlite3")]
        if case == "tool_failure":
            command += ["--fail-once", "script_standard"]
        if case == "save_failure":
            command += ["--fail-once", "save_plan"]
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
        if not completed.stdout:
            raise RuntimeError(f"{case}: {completed.stderr}")
        result = json.loads(completed.stdout)
        if case == "save_failure":
            print("save_failure first run:", result["completion_status"], result["save_status"],
                  [item["action"] for item in result["trace"]], flush=True)
            if result["completion_status"] == "incomplete" and result["save_status"] == "failed":
                completed = subprocess.run(command[:-2], cwd=ROOT, capture_output=True, text=True, check=False)
                if not completed.stdout:
                    raise RuntimeError(f"{case} resume: {completed.stderr}")
                result = json.loads(completed.stdout)
        print(f"{case}: {result['completion_status']} ({len(result['trace'])} trace steps)")
        verify(case, result)
        results[case] = result

OUTPUTS.mkdir(exist_ok=True)
TRACES.mkdir(exist_ok=True)
for case, result in results.items():
    (OUTPUTS / f"deepseek_{case}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (TRACES / f"deepseek_{case}.json").write_text(json.dumps(result["trace"], ensure_ascii=False, indent=2) + "\n")
