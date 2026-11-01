"""Regenerate offline CLI outputs and traces from synthetic fixtures."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = ROOT / "examples/outputs"
TRACES = ROOT / "examples/traces"
EXPECTED = {
    "normal": "complete", "budget_replan": "complete", "tool_failure": "complete",
    "partial_search": "complete", "budget_impossible": "incomplete",
    "deadline_impossible": "incomplete", "missing_budget": "needs_input",
    "missing_deadline": "needs_input", "duplicate": "complete",
    "resume_first": "incomplete", "resume_complete": "complete",
    "save_failure": "incomplete", "save_recovered": "complete",
}


def invoke(input_name: str, db: Path, **options) -> dict:
    command = [sys.executable, "-m", "src.cli", f"examples/inputs/{input_name}.json",
               "--mode", "mock", "--db", str(db)]
    if options.get("fail_once"):
        command += ["--fail-once", options["fail_once"]]
    if options.get("partial_search"):
        command.append("--partial-search")
    if options.get("max_steps"):
        command += ["--max-steps", str(options["max_steps"])]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if not completed.stdout:
        raise RuntimeError(f"{input_name}: {completed.stderr}")
    return json.loads(completed.stdout)


results = {}
with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    for name in ("normal", "budget_replan", "tool_failure", "partial_search",
                 "budget_impossible", "deadline_impossible", "missing_budget", "missing_deadline"):
        options = {"fail_once": "script_standard"} if name == "tool_failure" else {}
        if name == "partial_search":
            options["partial_search"] = True
        results[name] = invoke(name, base / f"{name}.sqlite3", **options)
    results["duplicate"] = invoke("normal", base / "normal.sqlite3")
    resume_db = base / "resume.sqlite3"
    results["resume_first"] = invoke("tool_failure", resume_db, fail_once="script_standard", max_steps=2)
    results["resume_complete"] = invoke("tool_failure", resume_db)
    save_db = base / "save_failure.sqlite3"
    results["save_failure"] = invoke("normal", save_db, fail_once="save_plan")
    results["save_recovered"] = invoke("normal", save_db)

for name, result in results.items():
    if result["completion_status"] != EXPECTED[name] or result["mode"] not in ("mock_policy", "preflight"):
        raise RuntimeError(f"{name}: unexpected result; artifacts were not replaced")
OUTPUTS.mkdir(exist_ok=True)
TRACES.mkdir(exist_ok=True)
for name, result in results.items():
    (OUTPUTS / f"{name}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (TRACES / f"{name}.json").write_text(json.dumps(result["trace"], ensure_ascii=False, indent=2) + "\n")
print(f"Generated {len(results)} offline CLI outputs and traces")
