"""命令行（骨架）。

    python -m mirosim.cli run-panel --experiment examples/experiment.policy.yaml \\
        --personas examples/personas --out runs/demo [--replay] [--backend openai]
    python -m mirosim.cli contrast --run runs/demo --experiment examples/experiment.policy.yaml --measure pol_fair
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import yaml

from mirosim import NOTICE, ROOT
from mirosim.analysis.contrast import contrast_table
from mirosim.core.gateway import BudgetExceeded, Gateway, MockBackend, Stopped
from mirosim.core.manifest import ManifestMismatch
from mirosim.core.openai_backend import BackendError, from_tier
from mirosim.experiment.model_pool import assign_models
from mirosim.experiment.sampling import segment_of
from mirosim.engines.panel import load_personas, run_panel
from mirosim.persona.factory import draw_skeletons

MOCK_WARNING = "后端是 mock：答案是按种子生成的随机数，只说明流程走得通，不是模拟结果。"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mirosim")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run-panel", help="跑一次群体反应实验")
    run.add_argument("--experiment", required=True, type=Path)
    run.add_argument("--personas", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--replay", action="store_true", help="只读录制，不发调用")
    run.add_argument("--backend", choices=["mock", "openai"], default="mock",
                     help="openai：按模型配置调 OpenAI 兼容接口，会产生费用")
    run.add_argument("--concurrency", type=int, default=1, help="同时在途的调用数，缺省 1（串行）")
    run.add_argument("--rpm", type=float, default=None, help="每分钟最多发多少次调用，缺省不限")
    run.add_argument("--models", type=Path, default=ROOT / "config" / "model_tiers.yaml",
                     help="模型配置文件，缺省为 config/model_tiers.yaml")
    con = sub.add_parser("contrast", help="对一次运行做配对比较")
    con.add_argument("--run", required=True, type=Path)
    con.add_argument("--experiment", required=True, type=Path)
    con.add_argument("--measure", required=True)
    con.add_argument("--by-model", action="store_true")
    skeletons = sub.add_parser("skeletons", help="按人群框架抽取人物骨架")
    skeletons.add_argument("--frame", required=True, type=Path)
    skeletons.add_argument("--personas", required=True, type=Path)
    skeletons.add_argument("--seed", required=True, type=int)
    skeletons.add_argument("--out", required=True, type=Path)
    assign = sub.add_parser("assign-models", help="按分群均衡分配人物思考模型")
    assign.add_argument("--personas", required=True, type=Path)
    assign.add_argument("--frame", required=True, type=Path)
    assign.add_argument("--seed", required=True, type=int)
    assign.add_argument("--models", type=Path, default=ROOT / "config" / "model_tiers.yaml")
    args = ap.parse_args(argv)

    if args.cmd == "assign-models":
        try:
            models_cfg = yaml.safe_load(args.models.read_text(encoding="utf-8"))
            pool = models_cfg.get("persona_pool")
            if not isinstance(pool, list) or not pool:
                raise ValueError("模型配置必须包含非空 persona_pool")
            members = [item["member"] for item in pool]
            personas = load_personas(args.personas)
            frame = yaml.safe_load(args.frame.read_text(encoding="utf-8"))
            assignment_path = args.personas / "model_assignment.json"
            previous = {}
            if assignment_path.exists():
                document = json.loads(assignment_path.read_text(encoding="utf-8"))
                if (not isinstance(document, dict) or document.get("version") != 1
                        or not isinstance(document.get("assignments"), dict)):
                    raise ValueError("model_assignment.json 格式无效")
                previous = document["assignments"]
                if any(
                    not isinstance(pid, str) or not isinstance(member, str)
                    for pid, member in previous.items()
                ):
                    raise ValueError("model_assignment.json 格式无效")
            assignments = assign_models(personas, frame, members, previous, args.seed)
        except (KeyError, TypeError, ValueError) as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2

        assignment_path.write_text(json.dumps(
            {"version": 1, "assignments": assignments}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        counts = Counter()
        for persona in personas:
            segment_id = segment_of(persona, frame)
            counts[(segment_id if segment_id is not None else "_unassigned",
                    assignments[persona["persona_id"]])] += 1
        segments = [segment["segment_id"] for segment in frame["segments"]]
        if any(segment_id == "_unassigned" for segment_id, _ in counts):
            segments.append("_unassigned")
        print("segment_id\tmember\tpersonas")
        for segment_id in segments:
            for member in members:
                print(f"{segment_id}\t{member}\t{counts[(segment_id, member)]}")
        kept = sum(pid in previous and previous[pid] in members for pid in assignments)
        reassigned = sum(pid in previous and previous[pid] not in members for pid in assignments)
        print(f"assignments\tkept={kept}\tnew={len(assignments) - kept - reassigned}\treassigned={reassigned}")
        return 0

    if args.cmd == "skeletons":
        frame = yaml.safe_load(args.frame.read_text(encoding="utf-8"))
        personas = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(args.personas.glob("p_*.json"))]
        behavior_map = yaml.safe_load((ROOT / "config" / "mbti_behavior_map.yaml").read_text(encoding="utf-8"))
        skeletons = draw_skeletons(frame, personas, behavior_map, args.seed)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n"
                                    for item in skeletons), encoding="utf-8")
        print("segment_id\tn_target\texisting\tnew")
        for segment in frame["segments"]:
            segment_id = segment["segment_id"]
            existing = sum(segment_of(persona, frame) == segment_id for persona in personas)
            new = sum(skeleton["segment_id"] == segment_id for skeleton in skeletons)
            print(f"{segment_id}\t{segment['n']}\t{existing}\t{new}")
        return 0

    x = yaml.safe_load(args.experiment.read_text(encoding="utf-8"))
    if args.cmd == "run-panel":
        if args.concurrency < 1 or (args.rpm is not None and args.rpm <= 0):
            ap.error("--concurrency 至少为 1，--rpm 必须大于 0")
        pool_members = x["models"].get("persona_pool")
        if pool_members is not None:
            models_cfg = yaml.safe_load(args.models.read_text(encoding="utf-8"))
            try:
                configured = {item["member"]: item["tier"] for item in models_cfg.get("persona_pool", [])}
                missing = [member for member in pool_members if member not in configured]
                if missing:
                    raise ValueError(f"model_tiers.yaml persona_pool 缺少 member：{', '.join(missing)}")
                if args.backend == "openai":
                    backends = {
                        member: from_tier(
                            models_cfg, configured[member], temperature=x["models"].get("temperature"),
                            require_key=not args.replay)
                        for member in pool_members
                    }
                else:
                    backends = {}
                    for member in pool_members:
                        backend = MockBackend()
                        backend.model_id = f"mock:{member}"
                        backends[member] = backend
            except ValueError as e:
                print(f"错误：{e}", file=sys.stderr)
                return 2
            gateway = Gateway(
                recording=args.out / "recording.jsonl",
                backends=backends,
                params_by={member: getattr(backend, "key_params", {}) for member, backend in backends.items()},
                replay=args.replay,
                max_calls=x["budget"]["max_calls"],
                max_input_tokens=x["budget"].get("max_input_tokens"),
                max_output_tokens=x["budget"].get("max_output_tokens"),
                requests_per_minute=args.rpm,
            )
        else:
            if args.backend == "openai":
                models_cfg = yaml.safe_load(args.models.read_text(encoding="utf-8"))
                try:
                    backend = from_tier(models_cfg, x["models"]["persona_tier"], temperature=x["models"].get("temperature"),
                                        require_key=not args.replay)
                except ValueError as e:
                    print(f"错误：{e}", file=sys.stderr)
                    return 2
            else:
                backend = MockBackend()
            budget = x["budget"]
            gateway = Gateway(backend, args.out / "recording.jsonl", replay=args.replay, params=getattr(backend, "key_params", None),
                              max_calls=budget["max_calls"], max_input_tokens=budget.get("max_input_tokens"),
                              max_output_tokens=budget.get("max_output_tokens"), requests_per_minute=args.rpm)
        try:
            result = run_panel(args.experiment, args.personas, args.out, gateway, concurrency=args.concurrency)
        except ManifestMismatch as e:
            print(f"错误：{e}", file=sys.stderr)
            return 2
        except (BackendError, BudgetExceeded, Stopped) as e:
            print(f"中断：{e}\n已完成的 {gateway.recorded_calls} 次调用留在录制里，原样重跑会从断点接着做，不重复计费。", file=sys.stderr)
            return 1
        result["live_calls"], result["usage"] = gateway.live_calls, gateway.usage
    else:
        responses = [json.loads(line) for line in (args.run / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
        stimulus = yaml.safe_load((ROOT / x["stimulus"]).read_text(encoding="utf-8"))
        members = [json.loads(line) for line in (args.run / "sample.jsonl").read_text(encoding="utf-8").splitlines()]
        result = contrast_table(responses, stimulus, args.measure, seed=x["design"]["seed"],
                                weights={m["persona_id"]: m["weight"] for m in members})
        summary = json.loads((args.run / "summary.json").read_text(encoding="utf-8"))
        result["sample"] = summary["sample"]
        if "model_id" in summary:
            result["model_id"] = summary["model_id"]
        if "models" in summary:
            result["models"] = summary["models"]
        if args.by_model:
            all_weights = {m["persona_id"]: m["weight"] for m in members}
            result["by_model"] = {}
            for member in summary.get("models", {}):
                model_responses = [response for response in responses if response.get("model_member") == member]
                model_personas = {response["persona_id"] for response in model_responses}
                model_weights = {pid: weight for pid, weight in all_weights.items() if pid in model_personas}
                result["by_model"][member] = contrast_table(
                    model_responses, stimulus, args.measure, seed=x["design"]["seed"], weights=model_weights)
        result["notice"] = NOTICE
    print(json.dumps(result, ensure_ascii=False, indent=2))
    mock_models = [model["model_id"] for model in result.get("models", {}).values()]
    if result.get("model_id") == MockBackend.model_id or any(model.startswith("mock:") for model in mock_models):
        print(MOCK_WARNING, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
