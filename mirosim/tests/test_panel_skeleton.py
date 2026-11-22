import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from mirosim import ROOT
from mirosim.analysis.contrast import contrast_table, paired_contrast
from mirosim.core.gateway import BudgetExceeded, Gateway, MockBackend, ReplayMiss
from mirosim.core.rng import u01
from mirosim.engines.panel import exposure, load_personas, run_panel

EX = ROOT / "examples"
POLICY, MARKETING = EX / "experiment.policy.yaml", EX / "experiment.marketing.yaml"


def run(experiment, out, **kw):
    gateway = Gateway(kw.pop("backend", MockBackend()), out / "recording.jsonl", **kw)
    return run_panel(experiment, EX / "personas", out, gateway), gateway


def rows(out):
    return [json.loads(line) for line in (out / "responses.jsonl").read_text(encoding="utf-8").splitlines()]


def test_u01_is_stable_and_shared_with_tools():
    assert u01(1, "a", 2) == u01(1, "a", 2) != u01(1, "a", 3)
    assert 0 <= u01(20261122, "exposure", "p_0a31f7", 1) < 1
    sys.path.insert(0, str(ROOT / "tools"))
    import derive_behavior
    assert derive_behavior.u01 is u01


def test_responses_follow_schema(tmp_path):
    summary, gateway = run(POLICY, tmp_path)
    schema = json.loads((ROOT / "schemas" / "response.schema.json").read_text(encoding="utf-8"))
    out = rows(tmp_path)
    assert len(out) == summary["responses"] == 3 * 5 * 3  # 人物 × 版本 × 重复
    for r in out:
        assert not list(Draft202012Validator(schema).iter_errors(r))
    assert summary["invalid"] == 0 and gateway.live_calls == 45


def test_replay_is_byte_identical_and_makes_no_calls(tmp_path):
    run(POLICY, tmp_path / "a")
    (tmp_path / "b").mkdir()
    shutil.copy(tmp_path / "a" / "recording.jsonl", tmp_path / "b" / "recording.jsonl")
    _, gateway = run(POLICY, tmp_path / "b", replay=True)
    assert gateway.live_calls == 0
    for name in ("responses.jsonl", "summary.json", "recording.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_replay_without_matching_record_fails(tmp_path):
    run(POLICY, tmp_path)
    lines = (tmp_path / "recording.jsonl").read_text(encoding="utf-8").splitlines()
    (tmp_path / "recording.jsonl").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ReplayMiss):
        run(POLICY, tmp_path, replay=True)


def test_placebo_prompt_is_identical_but_call_is_separate(tmp_path):
    run(POLICY, tmp_path)
    calls = [json.loads(line) for line in (tmp_path / "recording.jsonl").read_text(encoding="utf-8").splitlines()]
    b = {c["request"]["system"]: c for c in calls if c["run_id"].endswith("_B_r1_fam1")}
    placebo = {c["request"]["system"]: c for c in calls if c["run_id"].endswith("_B_PLACEBO_r1_fam1")}
    assert len(b) == len(placebo) == 3
    for system, c in b.items():
        assert placebo[system]["request"] == c["request"]
        assert placebo[system]["call_key"] != c["call_key"]


def test_natural_exposure_uses_common_random_numbers(tmp_path):
    summary, _ = run(MARKETING, tmp_path)
    seen = {}
    for r in rows(tmp_path):
        seen.setdefault((r["persona_id"], r["replicate"]), set()).add(json.dumps(r["exposure"], sort_keys=True))
        assert (r["call_id"] is None) == (not r["exposure"]["exposed"])
    assert all(len(v) == 1 for v in seen.values())
    assert 0 < summary["exposed"] < summary["responses"]


def test_exposure_probability_matches_formula():
    persona = load_personas(EX / "personas")[0]
    plan = [{"channel": persona["situation"]["media_diet"][0]["channel"], "reach": 0.25}]
    p_seen = min(1.0, 0.25 * persona["situation"]["media_diet"][0]["share"] * 2.0)
    hits = sum(exposure(7, persona, r, "natural", plan)["exposed"] for r in range(4000))
    assert abs(hits / 4000 - p_seen) < 0.03
    assert not exposure(7, persona, 1, "natural", [{"channel": "no_such_channel", "reach": 1.0}])["exposed"]


class BadThenGood(MockBackend):
    model_id = "bad-then-good"

    def __init__(self, bad_ordinals):
        self.bad, self.n = bad_ordinals, 0

    def complete(self, system, user, *, seed):
        self.n += 1
        return "我觉得还行" if (self.n - 1) % 2 in self.bad else super().complete(system, user, seed=seed)


def test_invalid_answer_is_retried_once(tmp_path):
    summary, gateway = run(POLICY, tmp_path / "retry", backend=BadThenGood({0}))
    assert summary["invalid"] == 0 and gateway.live_calls == 90
    summary, gateway = run(POLICY, tmp_path / "fail", backend=BadThenGood({0, 1}))
    assert summary["invalid"] == 45 and gateway.live_calls == 90
    bad = rows(tmp_path / "fail")[0]
    assert bad["valid"] is False and "payload" not in bad and "JSON" in bad["invalid_reason"]


def test_budget_stops_live_calls(tmp_path):
    with pytest.raises(BudgetExceeded):
        run(POLICY, tmp_path, max_calls=10)
    assert len((tmp_path / "recording.jsonl").read_text(encoding="utf-8").splitlines()) == 10


def test_interrupted_run_resumes_from_recording(tmp_path):
    with pytest.raises(BudgetExceeded):
        run(POLICY, tmp_path, max_calls=10)
    with pytest.raises(BudgetExceeded):  # 续跑不会重新得到一份预算
        run(POLICY, tmp_path, max_calls=10)
    assert len((tmp_path / "recording.jsonl").read_text(encoding="utf-8").splitlines()) == 10
    _, gateway = run(POLICY, tmp_path, max_calls=45)
    assert gateway.live_calls == 35


def resp(pid, variant, replicate, value):
    return {"persona_id": pid, "variant_id": variant, "replicate": replicate, "valid": True,
            "payload": {"answers": {"m": value}}}


def test_paired_contrast_arithmetic():
    data = [resp("p1", "S0", 1, 2), resp("p1", "S0", 2, 4), resp("p1", "A", 1, 5), resp("p1", "A", 2, 5),
            resp("p2", "S0", 1, 3), resp("p2", "A", 1, 2),
            resp("p3", "S0", 1, 1), resp("p3", "A", 1, "dk")]
    out = paired_contrast(data, "m", "A", "S0", seed=1, n_boot=200)
    assert out["n_pairs"] == 2 and out["mean_diff"] == 0.5  # p1: 5−3=2；p2: 2−3=−1；p3 无数值答案
    assert out["ci95"][0] <= 0.5 <= out["ci95"][1]
    assert paired_contrast(data, "m", "A", "S0", seed=1, n_boot=200) == out
    assert paired_contrast(data, "m", "ZZ", "S0", seed=1)["mean_diff"] is None


def test_contrast_table_separates_effects_from_noise_floor(tmp_path):
    run(POLICY, tmp_path)
    stimulus = yaml.safe_load((EX / "stimulus.policy.yaml").read_text(encoding="utf-8"))
    table = contrast_table(rows(tmp_path), stimulus, "pol_fair", seed=1, n_boot=100)
    assert table["baseline"] == "S0"
    assert [e["variant"] for e in table["effects"]] == ["A", "B", "C"]
    assert [(n["variant"], n["baseline"]) for n in table["noise_floor"]] == [("B_PLACEBO", "B")]


def test_changed_seed_does_not_reuse_recorded_answers(tmp_path):
    run(POLICY, tmp_path)
    reseeded = tmp_path / "experiment.yaml"
    reseeded.write_text(POLICY.read_text(encoding="utf-8").replace("seed: 20261122", "seed: 7"), encoding="utf-8")
    _, gateway = run(reseeded, tmp_path)
    assert gateway.live_calls == 45
