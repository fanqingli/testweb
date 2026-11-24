import json
import shutil

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from mirosim import ROOT
from mirosim.cli import main
from mirosim.core.gateway import BudgetExceeded, Gateway, MockBackend
from mirosim.core.manifest import ManifestMismatch, code_version
from mirosim.engines.panel import run_panel

EX = ROOT / "examples"
SCHEMA = json.loads((ROOT / "schemas" / "run_manifest.schema.json").read_text(encoding="utf-8"))


@pytest.fixture
def root(tmp_path):
    """一份可以随便改的 mirosim 目录副本。"""
    for sub in ("config", "schemas", "prompts", "examples"):
        shutil.copytree(ROOT / sub, tmp_path / "root" / sub)
    return tmp_path / "root"


def run(root, out, **kw):
    personas = kw.pop("personas", root / "examples" / "personas")
    gateway = Gateway(MockBackend(), out / "recording.jsonl", **kw)
    return run_panel(root / "examples" / "experiment.policy.yaml", personas, out, gateway, root=root), gateway


def manifest(out):
    return json.loads((out / "manifest.json").read_text(encoding="utf-8"))


def rewrite(path, old, new):
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_manifest_follows_the_schema_and_describes_the_run(root, tmp_path):
    out = tmp_path / "run"
    run(root, out)
    m = manifest(out)
    assert not list(Draft202012Validator(SCHEMA, format_checker=FormatChecker()).iter_errors(m))
    assert len(m["matrix"]) == 5 * 3 and {r["status"] for r in m["matrix"]} == {"done"}
    assert m["models"] == [{"tier": "tier-economy", "provider": "mock", "model_id": "mock-v1", "params": {}, "seed_supported": False}]
    assert [p["id"] for p in m["prompts"]] == ["panel_reaction", "persona_system"]
    assert m["recording"]["calls"] == len((out / "recording.jsonl").read_text(encoding="utf-8").splitlines()) == 45
    lock = json.loads((out / "personas.lock.json").read_text(encoding="utf-8"))
    assert [p["persona_id"] for p in lock] == sorted(p["persona_id"] for p in lock) and len(lock) == 3
    assert all(set(p) == {"persona_id", "version", "hash"} for p in lock)


def test_rerun_and_replay_in_the_same_directory_leave_the_manifest_unchanged(root, tmp_path):
    out = tmp_path / "run"
    run(root, out)
    first = (out / "manifest.json").read_bytes()
    _, gateway = run(root, out)
    assert gateway.live_calls == 0 and (out / "manifest.json").read_bytes() == first
    run(root, out, replay=True)
    assert (out / "manifest.json").read_bytes() == first


@pytest.mark.parametrize("path, old, new, named", [
    ("examples/frame.delivery_policy.yaml", "share: 0.04", "share: 0.05", "frame_hash"),
    ("examples/stimulus.policy.yaml", "as_of_date", "as_of_date ", "stimulus_hash"),
    ("examples/experiment.policy.yaml", "seed: 20261122", "seed: 7", "experiment_hash"),
    ("config/measures.yaml", "scales:", "scales: ", "measures"),
    ("prompts/panel_reaction.md", "```text", "```text\n（补充一句）", "提示词模板"),
])
def test_changed_inputs_are_refused_before_anything_is_written(root, tmp_path, path, old, new, named):
    out = tmp_path / "run"
    run(root, out)
    before = {p.name: p.read_bytes() for p in out.iterdir()}
    rewrite(root / path, old, new)
    for replay in (False, True):
        with pytest.raises(ManifestMismatch, match=named) as e:
            run(root, out, replay=replay)
        assert "换一个输出目录" in str(e.value)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == before
    _, gateway = run(root, tmp_path / "elsewhere")  # 换目录就是一次新的运行
    assert gateway.live_calls > 0


def test_a_persona_edited_in_place_is_noticed(root, tmp_path):
    out = tmp_path / "run"
    run(root, out)
    card = sorted((root / "examples" / "personas").glob("*.json"))[0]
    data = json.loads(card.read_text(encoding="utf-8"))
    data["quality"]["note"] = "原地改了一处，没有升版本"
    card.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ManifestMismatch, match="population_lock_hash"):
        run(root, out)


def test_another_backend_is_refused(root, tmp_path):
    class Other(MockBackend):
        model_id = "mock-v2"

    out = tmp_path / "run"
    run(root, out)
    with pytest.raises(ManifestMismatch, match="模型"):
        run_panel(root / "examples" / "experiment.policy.yaml", root / "examples" / "personas", out,
                  Gateway(Other(), out / "recording.jsonl"), root=root)


def test_budget_is_not_locked_and_an_interrupted_run_is_marked(root, tmp_path):
    out = tmp_path / "run"
    with pytest.raises(BudgetExceeded):
        run(root, out, max_calls=10)
    m = manifest(out)
    statuses = [r["status"] for r in m["matrix"]]
    assert statuses.count("partial") == 1 and set(statuses) <= {"done", "partial", "pending"}
    assert statuses.index("partial") == statuses.count("done") and m["recording"]["calls"] == 10
    rewrite(root / "examples" / "experiment.policy.yaml", "max_calls: 18000", "max_calls: 20000")
    _, gateway = run(root, out)
    after = manifest(out)
    assert gateway.live_calls == 35 and {r["status"] for r in after["matrix"]} == {"done"}
    assert after["created_at"] == m["created_at"] and after["inputs"] == m["inputs"]


def test_directory_without_a_manifest_is_adopted(root, tmp_path):
    out = tmp_path / "run"
    run(root, out)
    (out / "manifest.json").unlink()
    _, gateway = run(root, out)
    assert gateway.live_calls == 0 and manifest(out)["recording"]["calls"] == 45


def test_cli_reports_a_mismatch_and_exits_2(tmp_path, capsys):
    args = ["run-panel", "--personas", str(EX / "personas"), "--out", str(tmp_path / "run"), "--experiment"]
    assert main(args + [str(EX / "experiment.policy.yaml")]) == 0
    other = tmp_path / "experiment.yaml"
    x = yaml.safe_load((EX / "experiment.policy.yaml").read_text(encoding="utf-8"))
    x["design"]["replicates"] = 2
    other.write_text(yaml.safe_dump(x, allow_unicode=True), encoding="utf-8")
    capsys.readouterr()
    assert main(args + [str(other)]) == 2
    assert "experiment_hash" in capsys.readouterr().err


def test_code_version(tmp_path):
    assert code_version(tmp_path) == "unknown"
    assert len(code_version(ROOT).removesuffix("+dirty")) == 40


def test_inputs_edited_while_the_run_is_in_progress_do_not_change_what_the_manifest_records(root, tmp_path):
    frame = root / "examples" / "frame.delivery_policy.yaml"

    class EditsTheFrameMidRun(MockBackend):
        def complete(self, system, user, *, seed, spec=None):
            frame.write_text(frame.read_text(encoding="utf-8").replace("share: 0.04", "share: 0.05"), encoding="utf-8")
            return super().complete(system, user, seed=seed, spec=spec)

    out, reference = tmp_path / "run", tmp_path / "reference"
    run(root, reference)
    experiment, personas = root / "examples" / "experiment.policy.yaml", root / "examples" / "personas"
    run_panel(experiment, personas, out, Gateway(EditsTheFrameMidRun(), out / "recording.jsonl"), root=root)
    assert manifest(out)["inputs"] == manifest(reference)["inputs"]
    with pytest.raises(ManifestMismatch, match="frame_hash"):
        run(root, out, replay=True)
