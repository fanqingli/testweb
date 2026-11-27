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


def test_misrouted_filters_are_redrawn_until_segment_matches():
    frame = _overlapping_frame(["female", "male"])
    skeletons = draw_skeletons(frame, [], BEHAVIOR_MAP, seed=101)
    segment_b = next(s for s in skeletons if s["segment_id"] == "b")
    assert segment_b["identity"]["gender"] == "male"
    assert segment_of(segment_b, frame) == "b"


def test_fully_shadowed_segment_raises():
    frame = _overlapping_frame(["female"])
    with pytest.raises(ValueError, match="b.*遮蔽"):
        draw_skeletons(frame, [], BEHAVIOR_MAP, seed=103)
