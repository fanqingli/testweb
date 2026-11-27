import json
import shutil
from pathlib import Path

import pytest

from mirosim import ROOT
from mirosim.cli import main
from mirosim.core.gateway import Gateway
from mirosim.engines.panel import load_personas
from mirosim.persona.behavior import normalized_inputs
from mirosim.persona.probe import DIMS, card_sign, load_probes, run_probes, score, spearman

PROBES = ROOT / "data" / "probes" / "v1.yaml"
PERSONAS = ROOT / "examples" / "personas"


class CardBackend:
    """按提示词里的人物卡方向作答：卡上为正就选正向做法。用来检验计分，不是模型。"""
    model_id, provider = "card", "test"

    def __init__(self, personas, probes, flip=()):
        self.by_name = {p["identity"]["name"]: p for p in personas}
        self.probes, self.flip = probes, set(flip)

    def complete(self, system, user, *, seed, spec=None):
        persona = next(p for name, p in self.by_name.items() if name in system)
        probe = next(q for q in self.probes if q["situation"] in user)
        want = 1 if normalized_inputs(persona)[probe["dim"]] >= 0 else -1
        if probe["id"] in self.flip:
            want = -want
        text = next(o["text"] for o in probe["options"].values() if o["sign"] == want)
        return json.dumps({"choice": "A" if ("A. " + text) in user else "B"})


def test_bank_has_six_items_per_dimension_and_no_trait_words():
    probes = load_probes(PROBES)
    assert len(probes) == 54
    text = PROBES.read_text(encoding="utf-8").split("probes:")[1]
    for word in ("内向", "外向", "MBTI", "性格", "理性", "感性"):
        assert word not in text


def test_bank_rejects_unbalanced_dimensions(tmp_path):
    lines = PROBES.read_text(encoding="utf-8").splitlines()
    (tmp_path / "bad.yaml").write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="每个维度"):
        load_probes(tmp_path / "bad.yaml")


def test_card_following_answers_recover_fully_and_order_is_shuffled(tmp_path):
    personas, probes = load_personas(PERSONAS), load_probes(PROBES)
    gateway = Gateway(CardBackend(personas, probes), tmp_path / "rec.jsonl")
    rows = run_probes(personas, probes, gateway, seed=7, as_of_date="2026-11-22", sessions=2)
    assert len(rows) == 2 * 54 * len(personas)
    report = score(rows, personas)
    for pid, entry in report["personas"].items():
        assert entry["trait_recovery"] == 1.0 and entry["retest_consistency"] == 1.0 and entry["invalid"] == 0
    firsts = [json.loads(line)["request"]["user"] for line in (tmp_path / "rec.jsonl").read_text(encoding="utf-8").splitlines()]
    positive_first = sum(any(("A. " + q["options"]["a"]["text"]) in user for q in probes) for user in firsts)
    assert 0.3 < positive_first / len(firsts) < 0.7  # 正向做法不总排在 A


def test_borderline_dimensions_are_excluded_from_recovery(tmp_path):
    personas, probes = load_personas(PERSONAS)[:1], load_probes(PROBES)
    persona = personas[0]
    persona["psyche"]["mbti"]["axes"]["ei"] = 0.1
    persona["psyche"]["traits"]["conformity"] = 0.5
    assert card_sign(persona, "ei") == 0 and card_sign(persona, "conformity") == 0
    flip = [q["id"] for q in probes if q["dim"] in ("ei", "conformity")]
    gateway = Gateway(CardBackend(personas, probes, flip), tmp_path / "rec.jsonl")
    report = score(run_probes(personas, probes, gateway, seed=7, as_of_date="2026-11-22"), personas)
    assert report["personas"][persona["persona_id"]]["trait_recovery"] == 1.0


def test_invalid_answer_retries_once_then_counts_invalid(tmp_path):
    personas, probes = load_personas(PERSONAS)[:1], load_probes(PROBES)[:1]

    class Bad:
        model_id, provider, calls = "bad", "test", 0

        def complete(self, system, user, *, seed, spec=None):
            self.calls += 1
            return '{"choice": "C"}'

    backend = Bad()
    rows = run_probes(personas, probes, Gateway(backend, tmp_path / "rec.jsonl"), seed=7, as_of_date="2026-11-22")
    assert backend.calls == 2 and rows[0]["sign"] is None


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert spearman([1, 2], [1, 2]) is None and spearman([1, 2, 3], [5, 5, 5]) is None


def test_cli_mock_run_replays_byte_identically_and_respects_budget(tmp_path, capsys):
    out = tmp_path / "run"
    args = ["probe", "--personas", str(PERSONAS), "--out", str(out), "--max-calls", "400", "--as-of", "2026-11-22",
            "--frame", str(ROOT / "examples" / "frame.delivery_policy.yaml")]
    assert main(args) == 0
    report = json.loads((out / "probe_report.json").read_text(encoding="utf-8"))
    assert set(report["separability"]) == set(DIMS) and len(report["personas"]) == 3
    answers = (out / "probe_answers.jsonl").read_bytes()
    assert main(args + ["--replay"]) == 0
    assert (out / "probe_answers.jsonl").read_bytes() == answers
    assert main(["probe", "--personas", str(PERSONAS), "--out", str(tmp_path / "small"), "--max-calls", "10",
                 "--as-of", "2026-11-22"]) == 1


def test_cli_pool_uses_each_personas_member(tmp_path):
    library = tmp_path / "lib"
    shutil.copytree(PERSONAS, library)
    ids = [p["persona_id"] for p in load_personas(library)]
    (library / "model_assignment.json").write_text(json.dumps(
        {"version": 1, "assignments": dict(zip(ids, ["ds_flash", "kimi", "kimi"]))}), encoding="utf-8")
    out = tmp_path / "run"
    assert main(["probe", "--personas", str(library), "--out", str(out), "--max-calls", "400", "--pool",
                 "--as-of", "2026-11-22"]) == 0
    report = json.loads((out / "probe_report.json").read_text(encoding="utf-8"))
    assert {k: v["personas"] for k, v in report["by_model"].items()} == {"ds_flash": 1, "kimi": 2}
