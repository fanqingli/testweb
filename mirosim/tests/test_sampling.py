import copy
import json
import shutil

import pytest
import yaml

from mirosim import ROOT
from mirosim.analysis.contrast import paired_contrast
from mirosim.core.gateway import Gateway, MockBackend
from mirosim.engines.panel import load_personas, run_panel
from mirosim.experiment.sampling import draw_sample, matches, n_eff, segment_of

EX = ROOT / "examples"
FRAME = yaml.safe_load((EX / "frame.delivery_policy.yaml").read_text(encoding="utf-8"))
RIDER, OWNER, OFFICE = (p for p in load_personas(EX / "personas"))  # p_0a31f7、p_7c20de、p_e58b02


def clones(persona, n, prefix):
    out = []
    for i in range(n):
        p = copy.deepcopy(persona)
        p["persona_id"] = f"p_{prefix}{i:04x}"
        out.append(p)
    return out


def frame_with(**n):
    frame = copy.deepcopy(FRAME)
    for s in frame["segments"]:
        s["n"] = n.get(s["segment_id"], s["n"])
    return frame


def test_example_personas_land_in_their_segments():
    assert [segment_of(p, FRAME) for p in (RIDER, OWNER, OFFICE)] == ["rider", "franchisee", "consumer_heavy"]


def test_filter_needs_every_condition_and_accepts_any_overlap_on_list_fields():
    assert matches(OFFICE, {"situation.consumption.categories": ["外卖", "生鲜"]})
    assert not matches(RIDER, {"situation.consumption.categories": ["外卖"]})
    assert not matches(OFFICE, {"identity.occupation.job_family": ["F2"], "identity.occupation.industry_code": ["I0603"]})
    assert not matches(OFFICE, {"identity.no_such_field": ["x"]})


def test_overlapping_segments_resolve_to_the_first_in_the_frame():
    both = copy.deepcopy(RIDER)
    both["situation"]["consumption"]["categories"] = ["外卖"]  # 也符合 consumer_heavy
    assert segment_of(both, FRAME) == "rider"


def test_sample_takes_n_per_segment_and_weights_restore_shares():
    library = clones(RIDER, 10, "a") + clones(OWNER, 2, "b") + clones(OFFICE, 5, "c")
    sample = draw_sample(frame_with(rider=4, franchisee=3, consumer_heavy=5), library, seed=1)
    seg = {s["segment_id"]: s for s in sample["segments"]}
    assert [seg[k]["n_sampled"] for k in ("rider", "franchisee", "consumer_heavy")] == [4, 2, 5]
    assert sample["n_sampled"] == len(sample["members"]) == 11
    assert seg["rider"]["weight"] == pytest.approx(0.04 / (4 / 11), abs=1e-6)
    assert seg["merchant"]["weight"] is None and seg["merchant"]["n_pool"] == 0
    assert sample["covered_share"] == pytest.approx(0.04 + 0.005 + 0.30)
    # 加权后各分群的占比回到框架给的比例
    total = sum(m["weight"] for m in sample["members"])
    riders = sum(m["weight"] for m in sample["members"] if m["segment_id"] == "rider")
    assert riders / total == pytest.approx(0.04 / sample["covered_share"], abs=1e-4)
    assert sample["shortfall"] == sum(s["n"] for s in frame_with(rider=4, franchisee=3, consumer_heavy=5)["segments"]) - 11


def test_sample_depends_on_seed_but_not_on_library_order():
    library = clones(RIDER, 30, "a")
    frame = frame_with(rider=5)
    ids = lambda s: [m["persona_id"] for m in s["members"]]
    first = draw_sample(frame, library, seed=1)
    assert ids(first) == ids(draw_sample(frame, library[::-1], seed=1))
    assert ids(first) != ids(draw_sample(frame, library, seed=2))


def test_people_outside_every_segment_are_reported_and_left_out():
    outsider = copy.deepcopy(OFFICE)
    outsider["persona_id"] = "p_ffffff"
    outsider["identity"]["occupation"]["job_family"] = "F9"
    sample = draw_sample(FRAME, [RIDER, outsider], seed=1)
    assert sample["unmatched"] == ["p_ffffff"]
    assert [m["persona_id"] for m in sample["members"]] == [RIDER["persona_id"]]


def test_duplicate_segment_ids_are_refused():
    frame = copy.deepcopy(FRAME)
    frame["segments"][1]["segment_id"] = "rider"
    with pytest.raises(ValueError, match="rider"):
        draw_sample(frame, [RIDER, OWNER], seed=1)


def test_n_eff():
    assert n_eff([1, 1, 1, 1]) == 4
    assert n_eff([3, 1]) == 1.6
    assert n_eff([]) is None and n_eff([0.0, 0.0]) is None
    assert n_eff([2, 0.0, 2]) == 2


def test_sample_from_a_zero_share_segment_only():
    frame = copy.deepcopy(FRAME)
    frame["segments"][0]["share"] = 0.0  # rider
    sample = draw_sample(frame, [RIDER], seed=1)
    assert sample["members"][0]["weight"] == 0.0 and sample["n_eff"] is None and sample["covered_share"] == 0.0


def test_run_writes_the_sample_and_skips_people_outside_the_frame(tmp_path):
    personas = tmp_path / "personas"
    shutil.copytree(EX / "personas", personas)
    outsider = copy.deepcopy(OFFICE)
    outsider["persona_id"] = "p_ffffff"
    outsider["identity"]["occupation"]["job_family"] = "F9"
    (personas / "p_ffffff.json").write_text(json.dumps(outsider, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "run"
    gateway = Gateway(MockBackend(), out / "recording.jsonl")
    summary = run_panel(EX / "experiment.policy.yaml", personas, out, gateway)
    members = [json.loads(line) for line in (out / "sample.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [m["segment_id"] for m in members] == ["rider", "franchisee", "consumer_heavy"]
    assert gateway.live_calls == 3 * 5 * 3 and "p_ffffff" not in summary["personas"]
    assert summary["sample"]["unmatched"] == ["p_ffffff"]
    assert summary["sample"]["shortfall"] == 300 - 3 and summary["sample"]["covered_share"] == pytest.approx(0.345)


def test_run_fails_when_nobody_fits_the_frame(tmp_path):
    personas = tmp_path / "personas"
    personas.mkdir()
    outsider = copy.deepcopy(OFFICE)
    outsider["identity"]["occupation"]["job_family"] = "F9"
    (personas / "p.json").write_text(json.dumps(outsider, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        run_panel(EX / "experiment.policy.yaml", personas, tmp_path / "run", Gateway(MockBackend(), tmp_path / "rec.jsonl"))


def response(pid, variant, score):
    return {"persona_id": pid, "variant_id": variant, "valid": True, "payload": {"answers": {"m": score}}}


def test_weighted_contrast_reports_both_numbers():
    rs = [response("a", "v", 4), response("a", "base", 3), response("b", "v", 5), response("b", "base", 2)]
    plain = paired_contrast(rs, "m", "v", "base", seed=1, n_boot=200)
    assert plain["mean_diff"] == 2.0 and "weighted" not in plain
    out = paired_contrast(rs, "m", "v", "base", seed=1, n_boot=200, weights={"a": 3.0, "b": 1.0})
    assert (out["mean_diff"], out["ci95"]) == (plain["mean_diff"], plain["ci95"])
    assert out["weighted"]["mean_diff"] == 1.5 and out["weighted"]["n_eff"] == 1.6
    lo, hi = out["weighted"]["ci95"]
    assert 1.0 <= lo <= 1.5 <= hi <= 3.0


def test_people_without_a_weight_stay_out_of_the_weighted_number():
    rs = [response("a", "v", 4), response("a", "base", 3), response("b", "v", 5), response("b", "base", 2)]
    out = paired_contrast(rs, "m", "v", "base", seed=1, n_boot=200, weights={"a": 2.0})
    assert out["n_pairs"] == 2 and out["weighted"]["n_pairs"] == 1 and out["weighted"]["mean_diff"] == 1.0
    none = paired_contrast(rs, "m", "v", "base", seed=1, n_boot=200, weights={})
    assert none["weighted"] == {"n_pairs": 0, "mean_diff": None, "ci95": None, "n_eff": None}
