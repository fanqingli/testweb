import json
import re
import shutil
from datetime import date as real_date
from copy import deepcopy

import yaml
import pytest
from jsonschema import Draft202012Validator

from mirosim import ROOT
from mirosim.cli import main
from mirosim.core.gateway import Gateway
from mirosim.experiment.sampling import segment_of
from mirosim.persona.factory import draw_skeletons, write_personas

BEHAVIOR_MAP = yaml.safe_load((ROOT / "config" / "mbti_behavior_map.yaml").read_text(encoding="utf-8"))


def _frame(n=1, filters=None):
    return {
        "frame_id": "f_factory_test",
        "mbti_prior": {"mode": "uniform"},
        "segments": [{
            "segment_id": "target",
            "label": "测试分群",
            "filter": filters or {},
            "share": 1.0,
            "n": n,
        }],
    }


def _generated(name="林测试"):
    return {
        "name": name,
        "title": "运营专员",
        "industry_code": "I1234",
        "job_family": "F2",
        "employment_type": "employee",
        "years": 4,
        "marital": "single",
        "children": 0,
        "living_with": "和朋友合租",
        "life_events": [
            {"year": 2018, "event": "第一次独自搬家", "impact": "开始记账", "core": True},
            {"year": 2021, "event": "换了份工作", "impact": "更愿意提前问清规则"},
            {"year": 2024, "event": "帮邻居照看孩子", "impact": "习惯把时间安排留些余地"},
        ],
        "stakes": ["每月房租", "工作排班"],
        "constraints": ["通勤时间"],
        "media_diet": [
            {"channel": "short_video", "share": 0.5},
            {"channel": "group_chat", "share": 0.3},
        ],
        "consumption": {
            "decision_role": "decider",
            "categories": ["固定品类", "日用品"],
            "habits": ["大件先比较价格", "熟人推荐也会再查一遍"],
        },
        "knowledge_bounds": {
            "knows": ["附近几条公交线路"],
            "does_not_know": ["行业协会内部流程"],
        },
        "language_style": "说话直接，常带具体时间和价格",
        "catchphrase": "先看看再说",
    }


class FakeBackend:
    model_id = "fake-persona-writer"

    def __init__(self, outputs=None):
        self.outputs = list(outputs or [])
        self.prompts = []

    def complete(self, system, user, *, seed, spec=None):
        self.prompts.append({"system": system, "user": user, "seed": seed, "spec": spec})
        if self.outputs:
            output = self.outputs.pop(0)
        else:
            match = re.search(r'"birth_year":\s*(\d+)', user)
            output = _generated(f"林{match.group(1) if match else '测试'}")
        return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)


def _skeletons(frame, seed=17):
    return draw_skeletons(frame, [], BEHAVIOR_MAP, seed=seed)


def _writer(skeletons, frame, backend, recording, *, library=None, concurrency=1):
    gateway = Gateway(backend, recording)
    cards, failures = write_personas(
        skeletons, gateway, library=library or [], frame=frame,
        created_at="2026-01-02", concurrency=concurrency)
    return cards, failures, gateway


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writer_happy_path_builds_schema_valid_routed_card_and_region_prompt(tmp_path):
    frame = _frame(filters={
        "identity.home_region": ["northeast"],
        "identity.residence_region": ["shanghai"],
    })
    frame["segments"][0]["label"] = ""
    skeleton = _skeletons(frame)[0]
    backend = FakeBackend()
    cards, failures, gateway = _writer([skeleton], frame, backend, tmp_path / "recording.jsonl")

    assert failures == []
    assert len(cards) == 1
    card = cards[0]
    schema = json.loads((ROOT / "schemas" / "persona.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(card)
    assert segment_of(card, frame) == "target"
    assert card["persona_id"] == skeleton["persona_id"]
    assert card["version"] == 1 and card["synthetic"] is True and card["status"] == "active"
    assert card["disclaimer"] == "本人物为模拟构造，不代表任何真实个人。"
    assert card["provenance"]["created_by"] == "factory"
    assert card["provenance"]["priors"] == skeleton["priors"]
    assert "expression_intensity" in card["psyche"]["expression"]
    assert backend.prompts[0]["system"] == "你是写虚构人物档案的作者，只输出 JSON"
    assert backend.prompts[0]["spec"] is None
    assert "【他属于哪类人】target。" in backend.prompts[0]["user"]
    assert "这批人物的范围：" not in backend.prompts[0]["user"]
    assert "籍贯：东北；现居：上海。" in backend.prompts[0]["user"]
    record = _records(gateway.recording)[0]
    assert record["purpose"] == "factory"
    assert record["run_id"] == f"factory:{frame['frame_id']}"
    assert record["ordinal"] == 0


def test_writer_prompt_includes_delivery_rider_label_and_frame_universe(tmp_path):
    frame = yaml.safe_load(
        (ROOT / "examples" / "frame.delivery_policy.yaml").read_text(encoding="utf-8"))
    rider = deepcopy(next(segment for segment in frame["segments"] if segment["segment_id"] == "rider"))
    rider["n"] = 1
    frame["segments"] = [rider]
    skeleton = _skeletons(frame)[0]
    backend = FakeBackend()
    cards, failures, _ = _writer([skeleton], frame, backend, tmp_path / "recording.jsonl")

    assert len(cards) == 1 and failures == []
    prompt = backend.prompts[0]["user"]
    assert "【他属于哪类人】一线配送员。" in prompt
    assert f"这批人物的范围：{frame['universe']}。" in prompt
    assert "「初始倾向」指他对与这批人相关的事务（见上面的范围）最初的态度。" in prompt


def test_writer_retries_invalid_json_once_then_succeeds(tmp_path):
    frame = _frame()
    backend = FakeBackend(["不是 JSON", _generated("林重试")])
    cards, failures, gateway = _writer(
        _skeletons(frame), frame, backend, tmp_path / "recording.jsonl")

    assert len(cards) == 1 and failures == []
    assert gateway.recorded_calls == 2
    assert "你上一次的输出不合格：不是合法的 JSON" in backend.prompts[1]["user"]


def test_writer_records_exhausted_json_failure(tmp_path):
    frame = _frame()
    backend = FakeBackend(["坏 JSON", "仍然坏 JSON"])
    cards, failures, gateway = _writer(
        _skeletons(frame), frame, backend, tmp_path / "recording.jsonl")

    assert cards == []
    assert len(failures) == 1
    assert "不是合法的 JSON" in failures[0]["reason"]
    assert gateway.recorded_calls == 2


def test_writer_retries_non_finite_model_values(tmp_path):
    frame = _frame()
    payload = _generated("林非有限")
    payload["media_diet"][0]["share"] = float("nan")
    backend = FakeBackend([payload, payload])
    cards, failures, gateway = _writer(
        _skeletons(frame), frame, backend, tmp_path / "recording.jsonl")

    assert cards == []
    assert failures[0]["reason"] == "输出含非有限数值"
    assert "输出含非有限数值" in backend.prompts[1]["user"]
    assert gateway.recorded_calls == 2


def test_writer_rejects_mbti_leak_after_retry(tmp_path):
    frame = _frame()
    payload = _generated("林泄漏")
    payload["language_style"] = "说话时总提自己是 INTP"
    backend = FakeBackend([payload, payload])
    cards, failures, _ = _writer(_skeletons(frame), frame, backend, tmp_path / "recording.jsonl")

    assert cards == []
    assert "MBTI" in failures[0]["reason"]


def test_writer_retries_name_already_in_library(tmp_path):
    frame = _frame()
    library = [{"identity": {"name": "林已存在"}, "history": {"life_events": []}}]
    backend = FakeBackend([_generated("林已存在"), _generated("林新名字")])
    cards, failures, gateway = _writer(
        _skeletons(frame), frame, backend, tmp_path / "recording.jsonl", library=library)

    assert len(cards) == 1 and failures == []
    assert cards[0]["identity"]["name"] == "林新名字"
    assert "姓名已在人物库中" in backend.prompts[1]["user"]
    assert gateway.recorded_calls == 2


def test_writer_preserves_filter_values_and_unions_array_fields(tmp_path):
    frame = _frame(filters={
        "identity.occupation.employment_type": ["self_employed", "gig"],
        "situation.consumption.categories": ["固定品类"],
    })
    skeleton = _skeletons(frame)[0]
    backend = FakeBackend([_generated("林保留")])
    cards, failures, _ = _writer([skeleton], frame, backend, tmp_path / "recording.jsonl")

    assert failures == []
    card = cards[0]
    assert card["identity"]["occupation"]["employment_type"] in {"self_employed", "gig"}
    assert card["situation"]["consumption"]["categories"] == ["固定品类", "日用品"]
    assert segment_of(card, frame) == "target"


def test_writer_retries_when_generated_field_breaks_segment_routing(tmp_path):
    frame = _frame(filters={"identity.occupation.employment_type": ["gig"]})
    skeleton = _skeletons(frame)[0]
    del skeleton["identity"]["occupation"]["employment_type"]
    backend = FakeBackend([_generated("林路由"), _generated("林路由")])
    backend.outputs[0]["employment_type"] = "employee"
    backend.outputs[1]["employment_type"] = "gig"
    cards, failures, gateway = _writer([skeleton], frame, backend, tmp_path / "recording.jsonl")

    assert len(cards) == 1 and failures == []
    assert cards[0]["identity"]["occupation"]["employment_type"] == "gig"
    assert "分群路由不匹配" in backend.prompts[1]["user"]
    assert gateway.recorded_calls == 2


@pytest.mark.parametrize("invalid_media,reason", [
    ([{"channel": "unknown_channel", "share": 0.5}], "渠道"),
    ([{"channel": "short_video", "share": 0.8}, {"channel": "group_chat", "share": 0.3}], "share"),
])
def test_writer_rejects_invalid_media_diet(tmp_path, invalid_media, reason):
    frame = _frame()
    payload = _generated("林媒体")
    payload["media_diet"] = invalid_media
    backend = FakeBackend([payload, payload])
    cards, failures, _ = _writer(_skeletons(frame), frame, backend, tmp_path / "recording.jsonl")

    assert cards == []
    assert reason in failures[0]["reason"]


def test_duplicate_names_are_resolved_in_skeleton_order_without_retry(tmp_path):
    frame = _frame(n=2)
    skeletons = _skeletons(frame)
    backend = FakeBackend([_generated("林重名"), _generated("林重名")])
    cards, failures, gateway = _writer(
        skeletons, frame, backend, tmp_path / "recording.jsonl", concurrency=2)

    assert len(cards) == 1
    assert len(failures) == 1
    assert failures[0]["persona_id"] == skeletons[1]["persona_id"]
    assert failures[0]["reason"] == "重名"
    assert gateway.recorded_calls == 2


def test_writer_prompts_do_not_depend_on_concurrency(tmp_path):
    frame = _frame(n=2)
    skeletons = _skeletons(frame)
    library = [json.loads((ROOT / "examples" / "personas" / "p_0a31f7.json").read_text(encoding="utf-8"))]
    _, _, serial_gateway = _writer(
        skeletons, frame, FakeBackend(), tmp_path / "serial.jsonl", library=library, concurrency=1)
    _, _, concurrent_gateway = _writer(
        skeletons, frame, FakeBackend(), tmp_path / "concurrent.jsonl", library=library, concurrency=2)

    serial_prompts = sorted(record["request"]["user"] for record in _records(serial_gateway.recording))
    concurrent_prompts = sorted(record["request"]["user"] for record in _records(concurrent_gateway.recording))
    assert serial_prompts == concurrent_prompts


def test_writer_sibling_summaries_are_same_segment_and_capped(tmp_path):
    frame = _frame(n=1)
    frame["segments"] = [
        {"segment_id": "male", "label": "男", "filter": {"identity.gender": ["male"]}, "share": 0.5, "n": 0},
        {"segment_id": "female", "label": "女", "filter": {"identity.gender": ["female"]}, "share": 0.5, "n": 1},
    ]
    skeleton = _skeletons(frame)[0]
    example = json.loads((ROOT / "examples" / "personas" / "p_0a31f7.json").read_text(encoding="utf-8"))
    library = []
    for index in range(9):
        persona = deepcopy(example)
        persona["persona_id"] = f"p_same{index:02d}"
        persona["identity"]["gender"] = "female"
        persona["identity"]["name"] = f"同段人物{index}"
        persona["history"]["life_events"][0]["event"] = f"同段事件{index}"
        library.append(persona)
    other_segment = deepcopy(example)
    other_segment["persona_id"] = "p_other00"
    other_segment["identity"]["name"] = "异段人物"
    other_segment["history"]["life_events"][0]["event"] = "异段事件"
    library.append(other_segment)
    backend = FakeBackend([_generated("林新人物")])

    cards, failures, _ = _writer(
        [skeleton], frame, backend, tmp_path / "recording.jsonl", library=library)

    assert len(cards) == 1 and failures == []
    prompt = backend.prompts[0]["user"]
    assert "同段人物0" in prompt and "同段人物7" in prompt
    assert "同段人物8" not in prompt
    assert "异段人物" not in prompt


def _factory_args(frame_path, skeleton_path, into, out, *, replay=False, per_segment=None):
    args = [
        "factory", "--frame", str(frame_path), "--skeletons", str(skeleton_path),
        "--into", str(into), "--out", str(out), "--tier", "fake",
        "--models", str(ROOT / "config" / "model_tiers.yaml"),
    ]
    if replay:
        args.append("--replay")
    if per_segment is not None:
        args.extend(["--per-segment", str(per_segment)])
    return args


@pytest.mark.parametrize("relationship", ["same", "out_inside_into", "into_inside_out"])
def test_factory_cli_rejects_overlapping_into_and_out_directories(tmp_path, capsys, relationship):
    base = tmp_path / "directories"
    if relationship == "same":
        into = out = base
    elif relationship == "out_inside_into":
        into, out = base / "personas", base / "personas" / "run"
    else:
        into, out = base / "run" / "personas", base / "run"

    result = main(_factory_args(tmp_path / "frame.yaml", tmp_path / "skeletons.jsonl", into, out))

    captured = capsys.readouterr()
    assert result == 2
    assert "--into 与 --out 不能相同或互相包含" in captured.err
    assert not base.exists()


def test_factory_cli_replay_writes_byte_identical_persona_files(tmp_path, monkeypatch, capsys):
    frame = _frame()
    skeletons = _skeletons(frame)
    frame_path, skeleton_path = tmp_path / "frame.yaml", tmp_path / "skeletons.jsonl"
    frame_path.write_text(yaml.safe_dump(frame, allow_unicode=True), encoding="utf-8")
    skeleton_path.write_text(
        "".join(json.dumps(skeleton, ensure_ascii=False) + "\n" for skeleton in skeletons),
        encoding="utf-8",
    )
    first_backend = FakeBackend()
    monkeypatch.setattr("mirosim.cli.from_tier", lambda models, tier, require_key: first_backend)
    first_into, first_run = tmp_path / "first-personas", tmp_path / "first-run"
    assert main(_factory_args(frame_path, skeleton_path, first_into, first_run)) == 0
    capsys.readouterr()
    assert json.loads((first_run / "factory.json").read_text(encoding="utf-8")) == {
        "created_at": real_date.today().isoformat()
    }

    second_backend = FakeBackend()

    def no_live_call(*args, **kwargs):
        raise AssertionError("回放不应发出模型调用")

    second_backend.complete = no_live_call
    monkeypatch.setattr("mirosim.cli.from_tier", lambda models, tier, require_key: second_backend)
    second_into, second_run = tmp_path / "second-personas", tmp_path / "second-run"
    second_run.mkdir()
    shutil.copyfile(first_run / "recording.jsonl", second_run / "recording.jsonl")
    shutil.copyfile(first_run / "factory.json", second_run / "factory.json")

    class LaterDate:
        @classmethod
        def today(cls):
            return real_date(2099, 12, 31)

    monkeypatch.setattr("mirosim.cli.date", LaterDate)
    assert main(_factory_args(frame_path, skeleton_path, second_into, second_run, replay=True)) == 0
    capsys.readouterr()

    first_files = sorted(first_into.glob("*.json"))
    second_files = sorted(second_into.glob("*.json"))
    assert [path.name for path in first_files] == [path.name for path in second_files]
    assert [path.read_bytes() for path in first_files] == [path.read_bytes() for path in second_files]


def test_factory_cli_replay_without_metadata_notes_fallback_date(tmp_path, monkeypatch, capsys):
    frame = _frame()
    skeletons = _skeletons(frame)
    frame_path, skeleton_path = tmp_path / "frame.yaml", tmp_path / "skeletons.jsonl"
    frame_path.write_text(yaml.safe_dump(frame, allow_unicode=True), encoding="utf-8")
    skeleton_path.write_text("".join(json.dumps(item) + "\n" for item in skeletons), encoding="utf-8")
    backend = FakeBackend()
    monkeypatch.setattr("mirosim.cli.from_tier", lambda models, tier, require_key: backend)
    first_run = tmp_path / "first-run"
    assert main(_factory_args(frame_path, skeleton_path, tmp_path / "first-into", first_run)) == 0
    capsys.readouterr()

    class LaterDate:
        @classmethod
        def today(cls):
            return real_date(2099, 12, 31)

    monkeypatch.setattr("mirosim.cli.date", LaterDate)
    fallback_run = tmp_path / "fallback-run"
    fallback_run.mkdir()
    shutil.copyfile(first_run / "recording.jsonl", fallback_run / "recording.jsonl")
    assert main(_factory_args(
        frame_path, skeleton_path, tmp_path / "fallback-into", fallback_run, replay=True)) == 0
    captured = capsys.readouterr()
    assert "没有 factory.json" in captured.err
    card = json.loads(next((tmp_path / "fallback-into").glob("*.json")).read_text(encoding="utf-8"))
    assert card["provenance"]["created_at"] == "2099-12-31"


def test_factory_cli_skips_existing_ids_and_limits_each_segment(tmp_path, monkeypatch, capsys):
    frame = _frame(n=2)
    skeletons = _skeletons(frame)
    frame_path, skeleton_path = tmp_path / "frame.yaml", tmp_path / "skeletons.jsonl"
    frame_path.write_text(yaml.safe_dump(frame, allow_unicode=True), encoding="utf-8")
    skeleton_path.write_text(
        "".join(json.dumps(skeleton, ensure_ascii=False) + "\n" for skeleton in skeletons),
        encoding="utf-8",
    )
    into, run = tmp_path / "personas", tmp_path / "run"
    into.mkdir()
    existing = json.loads((ROOT / "examples" / "personas" / "p_0a31f7.json").read_text(encoding="utf-8"))
    existing["persona_id"] = skeletons[0]["persona_id"]
    (into / f"{skeletons[0]['persona_id']}.json").write_text(
        json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    backend = FakeBackend()
    monkeypatch.setattr("mirosim.cli.from_tier", lambda models, tier, require_key: backend)

    assert main(_factory_args(frame_path, skeleton_path, into, run, per_segment=1)) == 0
    capsys.readouterr()

    assert backend.prompts == []
    assert not (into / f"{skeletons[1]['persona_id']}.json").exists()
    assert (run / "failures.jsonl").read_text(encoding="utf-8") == ""


def test_factory_cli_reports_mock_as_unsupported(tmp_path, capsys):
    result = main([
        "factory", "--frame", str(tmp_path / "frame.yaml"), "--skeletons", str(tmp_path / "s.jsonl"),
        "--into", str(tmp_path / "personas"), "--out", str(tmp_path / "run"),
        "--backend", "mock", "--tier", "fake",
    ])

    assert result == 2
    assert "不支持 --backend mock" in capsys.readouterr().err
