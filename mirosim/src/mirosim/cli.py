"""命令行（骨架）。

    python -m mirosim.cli run-panel --experiment examples/experiment.policy.yaml \\
        --personas examples/personas --out runs/demo [--replay]
    python -m mirosim.cli contrast --run runs/demo --experiment examples/experiment.policy.yaml --measure pol_fair
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from mirosim import NOTICE, ROOT
from mirosim.analysis.contrast import contrast_table
from mirosim.core.gateway import Gateway, MockBackend
from mirosim.engines.panel import run_panel

MOCK_WARNING = "后端是 mock：答案是按种子生成的随机数，只说明流程走得通，不是模拟结果。"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mirosim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run-panel", help="跑一次群体反应实验")
    run.add_argument("--experiment", required=True, type=Path)
    run.add_argument("--personas", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--replay", action="store_true", help="只读录制，不发调用")
    con = sub.add_parser("contrast", help="对一次运行做配对比较")
    con.add_argument("--run", required=True, type=Path)
    con.add_argument("--experiment", required=True, type=Path)
    con.add_argument("--measure", required=True)
    args = ap.parse_args(argv)

    x = yaml.safe_load(args.experiment.read_text(encoding="utf-8"))
    if args.cmd == "run-panel":
        gateway = Gateway(MockBackend(), args.out / "recording.jsonl", replay=args.replay,
                          max_calls=x["budget"]["max_calls"])
        result = run_panel(args.experiment, args.personas, args.out, gateway)
        result["live_calls"] = gateway.live_calls
    else:
        responses = [json.loads(line) for line in (args.run / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
        stimulus = yaml.safe_load((ROOT / x["stimulus"]).read_text(encoding="utf-8"))
        members = [json.loads(line) for line in (args.run / "sample.jsonl").read_text(encoding="utf-8").splitlines()]
        result = contrast_table(responses, stimulus, args.measure, seed=x["design"]["seed"],
                                weights={m["persona_id"]: m["weight"] for m in members})
        result["sample"] = json.loads((args.run / "summary.json").read_text(encoding="utf-8"))["sample"]
        result["notice"] = NOTICE
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(MOCK_WARNING, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
