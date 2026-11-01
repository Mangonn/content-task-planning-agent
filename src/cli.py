"""CLI entry point. Run as python3 -m src.cli from the project root."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .agent import run
from .business import preflight
from .models import DeepSeekModel, MockPolicy, ModelUnavailableError


def load_local_env() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"):
            os.environ.setdefault(key, value.strip().strip("'\""))


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a content campaign within budget and deadline")
    parser.add_argument("input", type=Path, help="Request JSON file")
    parser.add_argument("--mode", choices=("mock", "deepseek"), default="mock")
    parser.add_argument("--db", type=Path, default=Path("work/plans.sqlite3"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fail-once", action="append", default=[],
                        choices=("script_standard", "script_compact", "save_plan"))
    parser.add_argument("--partial-search", action="store_true")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-tools", type=int, default=10)
    args = parser.parse_args()
    try:
        request = json.loads(args.input.read_text(encoding="utf-8"))
        early = preflight(request)
        if early:
            result = {**early, "mode": "preflight"}
        else:
            if args.mode == "deepseek":
                load_local_env()
            model = MockPolicy() if args.mode == "mock" else DeepSeekModel()
            args.db.parent.mkdir(parents=True, exist_ok=True)
            result = run(request, model=model, db_path=args.db, fail_once=set(args.fail_once),
                         partial_search=args.partial_search, max_steps=args.max_steps,
                         max_tools=args.max_tools)
        content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content, end="")
        return 0 if result["completion_status"] in ("complete", "needs_input") else 2
    except ModelUnavailableError as exc:
        print(json.dumps({"completion_status": "model_unavailable", "mode": args.mode,
                          "error": str(exc),
                          "hint": "Use --mode mock for offline workflow checks; mock does not prove model decisions."},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except sqlite3.Error as exc:
        print(json.dumps({"completion_status": "failed", "save_status": "failed",
                          "error": f"storage_unavailable:{type(exc).__name__}"},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"completion_status": "invalid_input", "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
