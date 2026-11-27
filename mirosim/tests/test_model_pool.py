import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from mirosim import ROOT
from mirosim.cli import main
from mirosim.core.gateway import BudgetExceeded, Gateway, MockBackend
from mirosim.core.manifest import ManifestMismatch
from mirosim.core.openai_backend import from_tier
from mirosim.engines.panel import load_personas, run_panel
from mirosim.experiment.model_pool import assign_models
from mirosim.experiment.sampling import draw_sample

EX = ROOT / "examples"
POOL = ["ds_flash", "kimi", "minimax"]
FRAME = {
    "frame_id": "f_assign",
    "segments": [
        {"segment_id": "male", "filter": {"identity.gender": ["male"]}},
        {"segment_id": "female", "filter": {"identity.gender": ["female"]}},
    ],
}


def _people(male=12, female=9):
    return [
        {"persona_id": f"p_{index:06d}", "identity": {"gender": "male"}}
        for index in range(male)
    ] + [
        {"persona_id": f"p_{index + male:06d}", "identity": {"gender": "female"}}
        for index in range(female)
    ]


def _pool_gateway(recording, *, replay=False, max_calls=None):
    class Backend:
        def __init__(self, model_id):
            self.model_id = model_id
            self.provider = "test"
            self.active = 0
            self.max_active = 0
            self.calls = 0
            self.lock = threading.Lock()

        def complete(self, system, user, *, seed, spec=None):
            with self.lock:
                self.active += 1
                self.calls += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.01)
            with self.lock:
                self.active -= 1
            return self.model_id

    backends = {"a": Backend("model-a"), "b": Backend("model-b")}
    gateway = Gateway(
        recording=recording, backends=backends, params_by={"a": {"temperature": 0.9}, "b": {}},
        replay=replay, max_calls=max_calls,
    )
    return gateway, backends


def _copy_personas(target):
    shutil.copytree(EX / "personas", target)
    return load_personas(target)


def _write_pool_assignment(personas_dir, assignments):
    path = personas_dir / "model_assignment.json"
    path.write_text(json.dumps({"version": 1, "assignments": assignments}, sort_keys=True), encoding="utf-8")
    return path


def _prepare_panel(tmp_path):
    personas_dir = tmp_path / "personas"
    personas = _copy_personas(personas_dir)
    frame_path = EX / "frame.delivery_policy.yaml"
    frame = yaml.safe_load(frame_path.read_text(encoding="utf-8"))
    assignments = assign_models(personas, frame, POOL, {}, seed=2026)
    _write_pool_assignment(personas_dir, assignments)
    experiment = yaml.safe_load((EX / "experiment.policy.yaml").read_text(encoding="utf-8"))
    experiment["models"]["persona_pool"] = POOL
    experiment_path = tmp_path / "experiment.yaml"
    experiment_path.write_text(yaml.safe_dump(experiment, allow_unicode=True), encoding="utf-8")
    return personas_dir, personas, frame, experiment_path


def _mock_backends(members):
    backends = {}
    for member in members:
        backend = MockBackend()
        backend.model_id = f"mock:{member}"
        backends[member] = backend
    return backends


def test_assignment_is_balanced_deterministic_and_sorted():
    people = _people()
    first = assign_models(people, FRAME, ["a", "b", "c"], {}, seed=4)
    second = assign_models(people, FRAME, ["a", "b", "c"], {}, seed=4)

    assert first == second
    assert list(first) == sorted(first)
    for segment, gender in (("male", "male"), ("female", "female")):
        counts = [sum(first[p["persona_id"]] == member for p in people if p["identity"]["gender"] == gender)
                  for member in ("a", "b", "c")]
        assert max(counts) - min(counts) <= 1


def test_added_member_keeps_old_assignments_and_removed_member_reassigns_only_its_personas():
    original_people = _people(12, 9)
    original = assign_models(original_people, FRAME, ["a", "b", "c"], {}, seed=17)
    with_new_people = original_people + [
        {"persona_id": f"p_new{index:02d}", "identity": {"gender": "male"}} for index in range(12)
    ]
    expanded = assign_models(with_new_people, FRAME, ["a", "b", "c", "d"], original, seed=17)
    assert all(expanded[pid] == member for pid, member in original.items())
    assert all(expanded[pid] != "d" for pid in original)
    assert any(expanded[persona["persona_id"]] == "d" for persona in with_new_people[len(original_people):])
    assert set(expanded.values()) <= {"a", "b", "c", "d"}

    removed = assign_models(original_people, FRAME, ["a", "b"], original, seed=17)
    for pid, member in original.items():
        if member in {"a", "b"}:
            assert removed[pid] == member
        else:
            assert removed[pid] in {"a", "b"}


def test_unassigned_personas_have_their_own_balancing_group():
    people = _people(0, 0) + [
        {"persona_id": f"p_out{index:02d}", "identity": {"gender": "other"}} for index in range(5)
    ]
    assigned = assign_models(people, FRAME, ["a", "b"], {}, seed=1)
    counts = [sum(value == member for value in assigned.values()) for member in ("a", "b")]
    assert max(counts) - min(counts) <= 1


def test_assign_models_cli_persists_and_persona_loader_ignores_assignment(tmp_path, capsys):
    personas_dir = tmp_path / "personas"
    _copy_personas(personas_dir)
    assert main([
        "assign-models", "--personas", str(personas_dir),
        "--frame", str(EX / "frame.delivery_policy.yaml"), "--seed", "7",
    ]) == 0
    output = capsys.readouterr().out
    assert "segment_id\tmember\tpersonas" in output
    document = json.loads((personas_dir / "model_assignment.json").read_text(encoding="utf-8"))
    assert document["version"] == 1
    assert set(document["assignments"].values()) <= set(POOL)
    assert len(load_personas(personas_dir)) == len(list(personas_dir.glob("p_*.json")))


def test_assign_models_atomic_replace_preserves_previous_file(tmp_path, monkeypatch):
    personas_dir = tmp_path / "personas"
    personas = _copy_personas(personas_dir)
    assignments = {persona["persona_id"]: "kimi" for persona in personas}
    path = personas_dir / "model_assignment.json"
    original = {"version": 1, "assignments": assignments}
    original_text = json.dumps(original, sort_keys=True)
    path.write_text(original_text, encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr("mirosim.cli.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        main([
            "assign-models", "--personas", str(personas_dir),
            "--frame", str(EX / "frame.delivery_policy.yaml"), "--seed", "7",
        ])

    assert path.read_text(encoding="utf-8") == original_text
    assert not list(personas_dir.glob(".model_assignment.json.*.tmp"))


def test_model_pool_tiers_work_without_fallback():
    config = yaml.safe_load((ROOT / "config" / "model_tiers.yaml").read_text(encoding="utf-8"))
    expected = {"ds_flash": "deepseek-flash", "kimi": "kimi-k2.5", "minimax": "MiniMax-M2.5"}
    for item in config["persona_pool"]:
        backend = from_tier(config, item["tier"], require_key=False)
        assert backend.model_id == expected[item["member"]]
        assert backend.params["max_tokens"] == 2000


def test_multi_backend_gateway_uses_member_keys_and_shared_budget(tmp_path):
    key_gateway, _ = _pool_gateway(tmp_path / "keys.jsonl")
    key_a = key_gateway.call(system="s", user="same", purpose="persona", run_id="same",
                             seed=1, member="a").call_key
    key_b = key_gateway.call(system="s", user="same", purpose="persona", run_id="same",
                             seed=1, member="b").call_key
    assert key_a != key_b

    gateway, backends = _pool_gateway(tmp_path / "recording.jsonl", max_calls=2)

    def call(index):
        member = ("a", "b")[index % 2]
        try:
            return gateway.call(system="s", user=f"u{index}", purpose="persona", run_id="r",
                                seed=index, member=member)
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, range(10)))
    assert sum(result is not None for result in results) == 2
    assert gateway.recorded_calls == 2
    assert sum(backend.calls for backend in backends.values()) == 2
    records = [json.loads(line) for line in gateway.recording.read_text(encoding="utf-8").splitlines()]
    assert len({record["call_key"] for record in records}) == 2
    assert all(record["model_member"] in {"a", "b"} for record in records)

    replay, _ = _pool_gateway(gateway.recording, replay=True)
    replayed = replay.call(system="s", user=records[0]["request"]["user"], purpose="persona", run_id="r",
                           seed=records[0]["seed"], member=records[0]["model_member"])
    assert replayed.from_recording


def test_mock_pool_panel_summary_replay_and_by_model_contrast(tmp_path, capsys):
    personas_dir, personas, frame, experiment_path = _prepare_panel(tmp_path)
    run_dir = tmp_path / "run"
    assert main([
        "run-panel", "--experiment", str(experiment_path), "--personas", str(personas_dir),
        "--out", str(run_dir), "--backend", "mock", "--concurrency", "2",
    ]) == 0
    capsys.readouterr()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    response_records = [json.loads(line) for line in (run_dir / "responses.jsonl").read_text().splitlines()]
    assert all(record["model_member"] in POOL for record in response_records)
    assert all(record["model_id"] == f"mock:{record['model_member']}" for record in response_records)
    response_schema = json.loads((ROOT / "schemas" / "response.schema.json").read_text(encoding="utf-8"))
    assert all(not list(Draft202012Validator(response_schema).iter_errors(record))
               for record in response_records)
    assert summary["model_families_run"] == len(summary["models"])
    assert sum(model["personas"] for model in summary["models"].values()) == len(summary["personas"])

    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    shutil.copyfile(run_dir / "recording.jsonl", replay_dir / "recording.jsonl")
    assert main([
        "run-panel", "--experiment", str(experiment_path), "--personas", str(personas_dir),
        "--out", str(replay_dir), "--backend", "mock", "--replay",
    ]) == 0
    capsys.readouterr()
    for filename in ("responses.jsonl", "sample.jsonl", "summary.json"):
        assert (run_dir / filename).read_bytes() == (replay_dir / filename).read_bytes()

    assert main([
        "contrast", "--run", str(run_dir), "--experiment", str(experiment_path),
        "--measure", "pol_fair", "--by-model",
    ]) == 0
    contrast = json.loads(capsys.readouterr().out)
    assert set(contrast["by_model"]) == set(summary["models"])

    sample_ids = set(summary["personas"])
    assignment_path = personas_dir / "model_assignment.json"
    document = json.loads(assignment_path.read_text(encoding="utf-8"))
    selected_id = min(sample_ids)
    old_member = document["assignments"][selected_id]
    document["assignments"][selected_id] = next(member for member in POOL if member != old_member)
    assignment_path.write_text(json.dumps(document), encoding="utf-8")
    changed_gateway = Gateway(
        recording=run_dir / "recording.jsonl", backends=_mock_backends(POOL),
        params_by={member: {} for member in POOL}, max_calls=18000,
    )
    with pytest.raises(ManifestMismatch):
        run_panel(experiment_path, personas_dir, run_dir, changed_gateway)
    assert changed_gateway.live_calls == 0


def test_panel_missing_assignment_errors_before_any_call(tmp_path):
    personas_dir, personas, frame, experiment_path = _prepare_panel(tmp_path)
    experiment = yaml.safe_load(experiment_path.read_text(encoding="utf-8"))
    sample_ids = {
        member["persona_id"]
        for member in draw_sample(frame, personas, experiment["design"]["seed"])["members"]
    }
    path = personas_dir / "model_assignment.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["assignments"].pop(next(iter(sample_ids)))
    path.write_text(json.dumps(document), encoding="utf-8")
    gateway = Gateway(
        recording=tmp_path / "missing" / "recording.jsonl", backends=_mock_backends(POOL),
        params_by={member: {} for member in POOL},
    )

    with pytest.raises(ValueError, match="mirosim assign-models"):
        run_panel(experiment_path, personas_dir, tmp_path / "missing", gateway)
    assert gateway.live_calls == 0
    assert not gateway.recording.exists()


def test_experiment_schema_accepts_pool_and_rejects_duplicate_members():
    schema = json.loads((ROOT / "schemas" / "experiment.schema.json").read_text(encoding="utf-8"))
    experiment = yaml.safe_load((EX / "experiment.policy.yaml").read_text(encoding="utf-8"))
    experiment["models"]["persona_pool"] = ["ds_flash", "kimi"]
    assert not list(Draft202012Validator(schema).iter_errors(experiment))
    experiment["models"]["persona_pool"] = ["ds_flash", "ds_flash"]
    assert list(Draft202012Validator(schema).iter_errors(experiment))
