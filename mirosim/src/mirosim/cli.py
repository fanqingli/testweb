"""命令行（骨架）。

    python -m mirosim.cli run-panel --experiment examples/experiment.policy.yaml \\
        --personas examples/personas --out runs/demo [--replay] [--backend openai]
    python -m mirosim.cli contrast --run runs/demo --experiment examples/experiment.policy.yaml --measure pol_fair
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from datetime import date
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
from mirosim.persona.factory import draw_skeletons, write_personas
from mirosim.persona.probe import load_probes, run_probes, score

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
    factory = sub.add_parser("factory", help="把人物骨架写成完整人物卡")
    factory.add_argument("--frame", required=True, type=Path)
    factory.add_argument("--skeletons", required=True, type=Path)
    factory.add_argument("--into", required=True, type=Path)
    factory.add_argument("--out", required=True, type=Path)
    factory.add_argument("--per-segment", type=int, default=None)
    factory.add_argument("--backend", choices=["openai", "mock"], default="openai")
    factory.add_argument("--models", type=Path, default=ROOT / "config" / "model_tiers.yaml")
    factory.add_argument("--tier", required=True)
    factory.add_argument("--concurrency", type=int, default=1)
    factory.add_argument("--rpm", type=float, default=None)
    factory.add_argument("--max-calls", type=int, default=None)
    factory.add_argument("--replay", action="store_true", help="只读录制，不发调用")
    probe = sub.add_parser("probe", help="用 54 道情境题检验人物卡上的性格是否体现在选择里")
    probe.add_argument("--personas", required=True, type=Path)
    probe.add_argument("--out", required=True, type=Path)
    probe.add_argument("--max-calls", required=True, type=int, help="调用数上限；每人每遍 54 次，不合格的再加一次")
    probe.add_argument("--probes", type=Path, default=ROOT / "config" / "probes_v1.yaml")
    probe.add_argument("--frame", type=Path, default=None, help="给了就只在同一分群内比较人物之间的差异")
    probe.add_argument("--sessions", type=int, choices=[1, 2], default=1, help="2：整套题做两遍，算重测一致性")
    probe.add_argument("--limit", type=int, default=None, help="只测前 N 个人物")
    probe.add_argument("--seed", type=int, default=31)
    probe.add_argument("--as-of", default=str(date.today()), help="算年龄用的日期")
    probe.add_argument("--backend", choices=["mock", "openai"], default="mock")
    probe.add_argument("--models", type=Path, default=ROOT / "config" / "model_tiers.yaml")
    probe.add_argument("--tier", default=None)
    probe.add_argument("--pool", action="store_true", help="每个人物用 model_assignment.json 里分给他的模型")
    probe.add_argument("--concurrency", type=int, default=1)
    probe.add_argument("--rpm", type=float, default=None)
    probe.add_argument("--replay", action="store_true", help="只读录制，不发调用")
    args = ap.parse_args(argv)

    if args.cmd == "probe":
        if args.concurrency < 1 or (args.rpm is not None and args.rpm <= 0) or args.max_calls < 1:
            ap.error("--concurrency、--max-calls 至少为 1，--rpm 必须大于 0")
        if args.pool == (args.tier is not None) and args.backend == "openai":
            ap.error("--backend openai 时 --tier 与 --pool 必须且只能给一个")
        try:
            probes = load_probes(args.probes)
            personas = load_personas(args.personas)[:args.limit]
            assignments, common = None, dict(
                recording=args.out / "recording.jsonl", replay=args.replay, max_calls=args.max_calls,
                requests_per_minute=args.rpm)
            models_cfg = yaml.safe_load(args.models.read_text(encoding="utf-8"))
            if args.pool:
                document = json.loads((args.personas / "model_assignment.json").read_text(encoding="utf-8"))
                assignments = {p["persona_id"]: document["assignments"][p["persona_id"]] for p in personas}
                tiers_by = {item["member"]: item["tier"] for item in models_cfg["persona_pool"]
                            if item["member"] in set(assignments.values())}
                if set(tiers_by) != set(assignments.values()):
                    raise ValueError("model_assignment.json 里有不在 persona_pool 中的 member")
                backends = {}
                for member, tier in tiers_by.items():
                    if args.backend == "openai":
                        backends[member] = from_tier(models_cfg, tier, require_key=not args.replay)
                    else:
                        backends[member] = MockBackend()
                        backends[member].model_id = f"mock:{member}"
                gateway = Gateway(backends=backends, tiers_by=tiers_by, params_by={
                    member: getattr(backend, "key_params", {}) for member, backend in backends.items()}, **common)
            else:
                backend = (from_tier(models_cfg, args.tier, require_key=not args.replay)
                           if args.backend == "openai" else MockBackend())
                gateway = Gateway(backend, params=getattr(backend, "key_params", None), **common)
        except (ValueError, KeyError, FileNotFoundError) as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2
        try:
            rows = run_probes(personas, probes, gateway, seed=args.seed, as_of_date=args.as_of, sessions=args.sessions,
                              assignments=assignments, concurrency=args.concurrency)
        except (BackendError, BudgetExceeded, Stopped) as error:
            print(f"中断：{error}\n已完成的 {gateway.recorded_calls} 次调用留在录制里，原样重跑会从断点接着做，不重复计费。",
                  file=sys.stderr)
            return 1
        groups = None
        if args.frame is not None:
            frame = yaml.safe_load(args.frame.read_text(encoding="utf-8"))
            groups = {p["persona_id"]: segment_of(p, frame) for p in personas}
        report = score(rows, personas, groups)
        report["notice"], report["live_calls"], report["usage"] = NOTICE, gateway.live_calls, gateway.usage
        (args.out / "probe_answers.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
        (args.out / "probe_report.json").write_text(
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print("persona_id\ttrait_recovery\tretest\tdistinct\tinvalid")
        for pid, entry in report["personas"].items():
            print(f"{pid}\t{entry['trait_recovery']}\t{entry.get('retest_consistency')}\t{entry['distinctiveness']}\t{entry['invalid']}")
        print("separability\t" + "\t".join(f"{dim}={value}" for dim, value in report["separability"].items()))
        print(f"usage\tlive_calls={gateway.live_calls}\trecorded_calls={gateway.recorded_calls}")
        return 0

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

        assignment_json = json.dumps(
            {"version": 1, "assignments": assignments}, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=assignment_path.parent,
                prefix=f".{assignment_path.name}.", suffix=".tmp", delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                temporary_file.write(assignment_json)
            os.replace(temporary_path, assignment_path)
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
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
        region_config = yaml.safe_load((ROOT / "config" / "region_trait_shift.yaml").read_text(encoding="utf-8"))
        skeletons = draw_skeletons(frame, personas, behavior_map, args.seed,
                                   region_shifts=region_config.get("shifts", []))
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

    if args.cmd == "factory":
        if args.backend == "mock":
            print("错误：人物工厂不支持 --backend mock，请使用 --backend openai。", file=sys.stderr)
            return 2
        resolved_into = args.into.resolve()
        resolved_out = args.out.resolve()
        if (resolved_into == resolved_out or resolved_into in resolved_out.parents
                or resolved_out in resolved_into.parents):
            print("错误：--into 与 --out 不能相同或互相包含。", file=sys.stderr)
            return 2
        if args.concurrency < 1 or (args.rpm is not None and args.rpm <= 0):
            print("错误：--concurrency 至少为 1，--rpm 必须大于 0。", file=sys.stderr)
            return 2
        if args.max_calls is not None and args.max_calls < 1:
            print("错误：--max-calls 必须大于 0。", file=sys.stderr)
            return 2
        if args.per_segment is not None and args.per_segment < 0:
            print("错误：--per-segment 不能小于 0。", file=sys.stderr)
            return 2
        try:
            models_cfg = yaml.safe_load(args.models.read_text(encoding="utf-8"))
            backend = from_tier(models_cfg, args.tier, require_key=not args.replay)
        except ValueError as error:
            print(f"错误：{error}", file=sys.stderr)
            return 2

        frame = yaml.safe_load(args.frame.read_text(encoding="utf-8"))
        all_skeletons = [
            json.loads(line) for line in args.skeletons.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        if args.per_segment is not None:
            selected, segment_counts = [], Counter()
            for skeleton in all_skeletons:
                segment_id = skeleton["segment_id"]
                if segment_counts[segment_id] < args.per_segment:
                    selected.append(skeleton)
                    segment_counts[segment_id] += 1
            all_skeletons = selected

        args.into.mkdir(parents=True, exist_ok=True)
        args.out.mkdir(parents=True, exist_ok=True)
        factory_path = args.out / "factory.json"
        if factory_path.exists():
            factory_meta = json.loads(factory_path.read_text(encoding="utf-8"))
            created_at = factory_meta.get("created_at") if isinstance(factory_meta, dict) else None
            if not isinstance(created_at, str) or not created_at:
                print(f"错误：{factory_path} 缺少有效的 created_at。", file=sys.stderr)
                return 2
        else:
            created_at = date.today().isoformat()
            if args.replay:
                print("注意：回放目录没有 factory.json，创建日期使用今天，产物可能与原运行不同。",
                      file=sys.stderr)
            else:
                factory_path.write_text(
                    json.dumps({"created_at": created_at}, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8",
                )
        try:
            library = load_personas(args.into)
        except FileNotFoundError:
            library = []
        skeletons_to_write = [
            skeleton for skeleton in all_skeletons
            if not (args.into / f"{skeleton['persona_id']}.json").exists()
        ]
        gateway = Gateway(
            backend,
            args.out / "recording.jsonl",
            replay=args.replay,
            params=getattr(backend, "key_params", None),
            max_calls=args.max_calls,
            requests_per_minute=args.rpm,
        )
        try:
            cards, failures = write_personas(
                skeletons_to_write,
                gateway,
                library=library,
                frame=frame,
                created_at=created_at,
                concurrency=args.concurrency,
            )
        except (BackendError, BudgetExceeded, Stopped) as error:
            print(f"中断：{error}\n已完成的 {gateway.recorded_calls} 次调用留在录制里，原样重跑会从断点接着做，不重复计费。",
                  file=sys.stderr)
            return 1

        for card in cards:
            (args.into / f"{card['persona_id']}.json").write_text(
                json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (args.out / "failures.jsonl").write_text(
            "".join(json.dumps(failure, ensure_ascii=False, sort_keys=True) + "\n" for failure in failures),
            encoding="utf-8",
        )
        segment_for_id = {skeleton["persona_id"]: skeleton["segment_id"] for skeleton in skeletons_to_write}
        accepted = Counter(segment_for_id[card["persona_id"]] for card in cards)
        failed = Counter(failure["segment_id"] for failure in failures)
        print("segment_id\taccepted\tfailed")
        for segment in frame["segments"]:
            segment_id = segment["segment_id"]
            print(f"{segment_id}\t{accepted[segment_id]}\t{failed[segment_id]}")
        print(f"usage\tlive_calls={gateway.live_calls}\trecorded_calls={gateway.recorded_calls}"
              f"\tinput_tokens={gateway.usage['input_tokens']}\toutput_tokens={gateway.usage['output_tokens']}")
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
                    tiers_by = {member: configured[member] for member in pool_members}
                else:
                    backends = {}
                    for member in pool_members:
                        backend = MockBackend()
                        backend.model_id = f"mock:{member}"
                        backends[member] = backend
                    tiers_by = {member: member for member in pool_members}
            except ValueError as e:
                print(f"错误：{e}", file=sys.stderr)
                return 2
            gateway = Gateway(
                recording=args.out / "recording.jsonl",
                backends=backends,
                params_by={member: getattr(backend, "key_params", {}) for member, backend in backends.items()},
                tiers_by=tiers_by,
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
