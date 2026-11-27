import json
import re
from collections import Counter

import pytest
import yaml

from mirosim import ROOT
from mirosim.cli import main
from mirosim.engines.panel import load_personas
from mirosim.experiment.sampling import segment_of
from mirosim.persona.factory import draw_skeletons

EX = ROOT / "examples"
PERSONAS = load_personas(EX / "personas")
BEHAVIOR_MAP = yaml.safe_load((ROOT / "config" / "mbti_behavior_map.yaml").read_text(encoding="utf-8"))
PERSONA_SCHEMA = json.loads((ROOT / "schemas" / "persona.schema.json").read_text(encoding="utf-8"))
FRAME_PATHS = [EX / "frame.delivery_policy.yaml", EX / "frame.urban_consumers.yaml"]
DELIVERY_FRAME = yaml.safe_load(FRAME_PATHS[0].read_text(encoding="utf-8"))


def _frame(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _existing_counts(frame):
    return Counter(segment_of(persona, frame) for persona in PERSONAS)


def test_delivery_policy_fills_each_segment_shortfall():
    expected = {segment["segment_id"]: max(0, segment["n"] - _existing_counts(DELIVERY_FRAME)[segment["segment_id"]])
                for segment in DELIVERY_FRAME["segments"]}
    skeletons = draw_skeletons(DELIVERY_FRAME, PERSONAS, BEHAVIOR_MAP, seed=31)
    actual = Counter(skeleton["segment_id"] for skeleton in skeletons)
    assert len(skeletons) == 297
    assert actual == expected


def test_skeletons_are_deterministic_and_seeded():
    first = draw_skeletons(DELIVERY_FRAME, PERSONAS, BEHAVIOR_MAP, seed=31)
    assert first == draw_skeletons(DELIVERY_FRAME, PERSONAS, BEHAVIOR_MAP, seed=31)
    assert first != draw_skeletons(DELIVERY_FRAME, PERSONAS, BEHAVIOR_MAP, seed=32)


@pytest.mark.parametrize("frame_path", FRAME_PATHS)
def test_skeletons_match_segments_and_schema_ranges(frame_path):
    frame = _frame(frame_path)
    skeletons = draw_skeletons(frame, PERSONAS, BEHAVIOR_MAP, seed=47)
    existing_ids = {persona["persona_id"] for persona in PERSONAS}
    ids = [skeleton["persona_id"] for skeleton in skeletons]
    assert len(ids) == len(set(ids)) and not (set(ids) & existing_ids)

    identity_schema = PERSONA_SCHEMA["properties"]["identity"]["properties"]
    psyche_schema = PERSONA_SCHEMA["properties"]["psyche"]["properties"]
    for skeleton in skeletons:
        segment = next(s for s in frame["segments"] if s["segment_id"] == skeleton["segment_id"])
        assert segment_of(skeleton, frame) == skeleton["segment_id"]
        assert re.fullmatch(r"p_[0-9a-z]{6,}", skeleton["persona_id"])
        assert skeleton["identity"]["gender"] in ("male", "female")
        assert skeleton["identity"]["gender"] in identity_schema["gender"]["enum"]
        for field in ("education", "region_tier", "income_band"):
            assert skeleton["identity"][field] in identity_schema[field]["enum"]
        year = skeleton["identity"]["birth_year"]
        allowed_years = segment["filter"].get("identity.birth_year")
        assert year in allowed_years if allowed_years else 1960 <= year <= 2004

        mbti = skeleton["psyche"]["mbti"]
        assert mbti["type"] in {a + b + c + d for a in "EI" for b in "SN" for c in "TF" for d in "JP"}
        for axis, letter in (("ei", mbti["type"][0]), ("sn", mbti["type"][1]),
                             ("tf", mbti["type"][2]), ("jp", mbti["type"][3])):
            value = mbti["axes"][axis]
            assert -1 <= value <= 1
            positive = letter in {"E", "N", "T", "J"}
            assert (value > 0) == positive
            assert (abs(value) < 0.2) == (axis in mbti["borderline_axes"])
        assert mbti["borderline_axes"] == [axis for axis in ("ei", "sn", "tf", "jp")
                                            if abs(mbti["axes"][axis]) < 0.2]

        for trait in ("emotional_stability", "risk_tolerance", "institutional_trust", "conformity", "price_sensitivity"):
            assert 0 <= skeleton["psyche"]["traits"][trait] <= 1
        assert skeleton["psyche"]["motive_type"] in psyche_schema["motive_type"]["enum"]
        assert len(skeleton["psyche"]["values_top3"]) == 3
        assert len(set(skeleton["psyche"]["values_top3"])) == 3
        assert all(value in psyche_schema["values_top3"]["items"]["enum"] for value in skeleton["psyche"]["values_top3"])
        assert skeleton["initial_lean"] in ("support", "oppose", "conditional", "indifferent")
        assert set(skeleton["priors"].values()) <= {"uniform", "filter", "table", "stratified_uniform"}


def test_rotation_coverage_stance_spread_and_combo_cap():
    skeletons = draw_skeletons(DELIVERY_FRAME, PERSONAS, BEHAVIOR_MAP, seed=53)
    existing_counts = _existing_counts(DELIVERY_FRAME)
    max_combo = BEHAVIOR_MAP["sampling"]["max_same_combo_per_segment"]
    min_stance = DELIVERY_FRAME["diversity"]["min_stance_spread_per_segment"]

    for segment in DELIVERY_FRAME["segments"]:
        segment_id = segment["segment_id"]
        group = [s for s in skeletons if s["segment_id"] == segment_id]
        shortfall = max(0, segment["n"] - existing_counts[segment_id])
        assert len({s["identity"]["gender"] for s in group}) == min(shortfall, 2)
        assert len({s["identity"]["education"] for s in group}) == min(shortfall, len(PERSONA_SCHEMA["properties"]["identity"]["properties"]["education"]["enum"]))
        assert len({s["identity"]["region_tier"] for s in group}) == min(shortfall, len(PERSONA_SCHEMA["properties"]["identity"]["properties"]["region_tier"]["enum"]))
        assert len({s["identity"]["income_band"] for s in group}) == min(shortfall, len(PERSONA_SCHEMA["properties"]["identity"]["properties"]["income_band"]["enum"]))
        if shortfall >= 16 and DELIVERY_FRAME["mbti_prior"]["mode"] == "uniform":
            assert len({s["psyche"]["mbti"]["type"] for s in group}) == 16
        assert len({s["initial_lean"] for s in group}) >= min(min_stance, shortfall)

        combos = Counter((persona["psyche"]["mbti"]["type"], persona["psyche"]["motive_type"])
                         for persona in PERSONAS if segment_of(persona, DELIVERY_FRAME) == segment_id)
        combos.update((s["psyche"]["mbti"]["type"], s["psyche"]["motive_type"]) for s in group)
        assert max(combos.values(), default=0) <= max_combo


@pytest.mark.parametrize("frame_path", FRAME_PATHS)
def test_every_generated_filter_routes_to_its_segment(frame_path):
    frame = _frame(frame_path)
    skeletons = draw_skeletons(frame, PERSONAS, BEHAVIOR_MAP, seed=61)
    for skeleton in skeletons:
        segment = next(s for s in frame["segments"] if s["segment_id"] == skeleton["segment_id"])
        assert segment_of(skeleton, frame) == segment["segment_id"]


def test_skeletons_cli_writes_jsonl_and_segment_counts(tmp_path, capsys):
    out = tmp_path / "nested" / "skeletons.jsonl"
    assert main(["skeletons", "--frame", str(FRAME_PATHS[0]), "--personas", str(EX / "personas"),
                 "--seed", "71", "--out", str(out)]) == 0
    records = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    table = capsys.readouterr().out.splitlines()
    assert len(records) == 297
    assert table[0] == "segment_id\tn_target\texisting\tnew"
    assert "rider\t60\t1\t59" in table
