from copy import deepcopy

import pytest
import yaml

from mirosim import ROOT
from mirosim.experiment.sampling import segment_of
from mirosim.persona.factory import draw_skeletons

DELIVERY_FRAME = yaml.safe_load((ROOT / "examples" / "frame.delivery_policy.yaml").read_text(encoding="utf-8"))
BEHAVIOR_MAP = yaml.safe_load((ROOT / "config" / "mbti_behavior_map.yaml").read_text(encoding="utf-8"))


def _overlapping_frame(second_gender_filter):
    frame = deepcopy(DELIVERY_FRAME)
    frame["segments"] = [
        {"segment_id": "a", "label": "A", "filter": {"identity.gender": ["female"]}, "share": 0.5, "n": 1},
        {"segment_id": "b", "label": "B", "filter": {"identity.gender": second_gender_filter},
         "share": 0.5, "n": 1},
    ]
    return frame


def test_misrouted_target_filter_is_exhaustively_searched():
    frame = _overlapping_frame(["female", "male"])
    skeletons = draw_skeletons(frame, [], BEHAVIOR_MAP, seed=101)
    segment_b = next(s for s in skeletons if s["segment_id"] == "b")
    assert segment_b["identity"]["gender"] == "male"
    assert segment_of(segment_b, frame) == "b"


def test_earlier_income_filter_is_rerouted_even_without_target_filter():
    frame = deepcopy(DELIVERY_FRAME)
    frame["segments"] = [
        {"segment_id": "earlier", "label": "Earlier",
         "filter": {"identity.income_band": ["lt_50k"]}, "share": 0.5, "n": 0},
        {"segment_id": "target", "label": "Target", "filter": {}, "share": 0.5, "n": 30},
    ]
    skeletons = draw_skeletons(frame, [], BEHAVIOR_MAP, seed=149)
    target_skeletons = [s for s in skeletons if s["segment_id"] == "target"]
    assert len(target_skeletons) == 30
    assert all(s["identity"]["income_band"] != "lt_50k" for s in target_skeletons)
    assert all(segment_of(s, frame) == "target" for s in target_skeletons)
    assert any(s["priors"]["identity.income_band"] == "rerouted" for s in target_skeletons)


def test_one_viable_target_filter_value_is_found_for_every_skeleton():
    frame = deepcopy(DELIVERY_FRAME)
    blocked = [f"岗位{i}" for i in range(9)]
    viable = "唯一可达岗位"
    frame["segments"] = [
        {"segment_id": "earlier", "label": "Earlier",
         "filter": {"identity.occupation.title": blocked}, "share": 0.5, "n": 0},
        {"segment_id": "target", "label": "Target",
         "filter": {"identity.occupation.title": blocked + [viable]}, "share": 0.5, "n": 30},
    ]
    skeletons = draw_skeletons(frame, [], BEHAVIOR_MAP, seed=151)
    assert len(skeletons) == 30
    assert all(s["identity"]["occupation"]["title"] == viable for s in skeletons)
    assert all(segment_of(s, frame) == "target" for s in skeletons)


def test_fully_shadowed_segment_raises():
    frame = _overlapping_frame(["female"])
    with pytest.raises(ValueError, match="b.*遮蔽"):
        draw_skeletons(frame, [], BEHAVIOR_MAP, seed=103)
