import json
import shutil

import jinja2
import pytest
import yaml

from mirosim import ROOT
from mirosim.core.gateway import Gateway, MockBackend
from mirosim.engines.panel import applicable_spec, load_personas, measure_spec, run_panel, user_prompt, validate_payload
from mirosim.persona.render import load_template, system_prompt

EX = ROOT / "examples"
CFG = {name: yaml.safe_load((ROOT / "config" / f"{name}.yaml").read_text(encoding="utf-8"))
       for name in ("measures", "mbti_behavior_map", "persona_text", "channels")}
SCHEMA = json.loads((ROOT / "schemas" / "response.schema.json").read_text(encoding="utf-8"))["properties"]["payload"]
STIMULUS = yaml.safe_load((EX / "stimulus.policy.yaml").read_text(encoding="utf-8"))
SPEC = measure_spec(CFG["measures"]["policy"], CFG["measures"]["scales"], ["pol_fair", "pol_stance", "pol_worry"])
OFFICE = next(p for p in load_personas(EX / "personas") if p["persona_id"] == "p_e58b02")


def system(persona=OFFICE, as_of="2026-11-22"):
    return system_prompt(load_template(ROOT, "persona_system"), persona, CFG["mbti_behavior_map"], CFG["persona_text"], as_of)


def user(variant, channel=None, max_chars=150):
    return user_prompt(load_template(ROOT, "panel_reaction"), STIMULUS, variant, channel, CFG["channels"]["channels"], SPEC, max_chars)


def answer(**over):
    base = {"noticed": True, "takeaway": "t", "inner_reaction": "i", "answers": {"pol_fair": 3, "pol_stance": "support"},
            "behavior": "wait", "reasons": [], "share": {"would_share": False, "to_whom": None, "what": None}, "quote": "还行"}
    return json.dumps({**base, **over}, ensure_ascii=False)


def test_system_prompt_speaks_plainly_and_hides_codes():
    text = system()
    assert "你现在是苏晓雯" in text and "30岁" in text and "新一线城市" in text and "【几条规矩】" in text
    assert "封控期间靠骑手送菜撑了两个月" in text and "一次外卖超时一小时" not in text  # 只放 core 的生活事件
    for code in (OFFICE["psyche"]["mbti"]["type"], "new_tier1", "100k_200k", "bachelor", "self_direction", "{{", "{%"):
        assert code not in text
    assert system() == text and "31岁" in system(as_of="2027-01-01")  # 年龄跟刺激物的时间锚点走


def test_system_prompt_handles_missing_optional_fields():
    bare = json.loads(json.dumps(OFFICE))
    del bare["identity"]["household"], bare["identity"]["occupation"]["years"], bare["psyche"]["expression"]["catchphrase"]
    text = system(bare)
    assert "干了" not in text and "挂在嘴边" not in text and "产品运营" in text


def test_user_prompt_lists_questions_and_keeps_variant_labels_out():
    variant = STIMULUS["variants"][1]
    text = user(variant, max_chars=170)
    assert variant["content"].strip() in text and "不超过 170 个字" in text and "今天是2026-11-22" in text
    for mid in SPEC["measures"]:
        assert f"- {mid}：" in text
    assert "填 1 到 5 的整数" in text and "从 support、conditional、oppose、indifferent 里选一个" in text
    for code, label in SPEC["behavior_labels"].items():
        assert f"- {code}：{label}" in text
    assert "【你看到了这个】" in text and "【你记得的相关的事】" not in text and "{{" not in text
    assert variant["label"] not in text and f"\"{variant['variant_id']}\"" not in text
    assert "【你在「新闻资讯客户端」看到了这个】" in user(variant, channel="news_app")


def test_ask_if_is_decided_by_code_and_never_printed():
    m = CFG["measures"]
    spec = measure_spec(m["marketing"], m["scales"], ["mkt_appeal", "mkt_price_feel"])
    assert list(applicable_spec(spec, {}, {"attributes": {"price": 19}})["measures"]) == ["mkt_appeal", "mkt_price_feel"]
    assert list(applicable_spec(spec, {}, {"attributes": {}})["measures"]) == ["mkt_appeal"]
    assert list(applicable_spec(spec, {}, {})["measures"]) == ["mkt_appeal"]
    spec = measure_spec(m["brand"], m["scales"], ["brd_favor_post", "brd_sincere"])
    assert "brd_sincere" in applicable_spec(spec, {"kind": "crisis_response"}, {})["measures"]
    assert "brd_sincere" not in applicable_spec(spec, {"kind": "brand_message"}, {})["measures"]
    spec = measure_spec(m["policy"], m["scales"], ["pol_stance", "pol_condition"])
    text = user_prompt(load_template(ROOT, "panel_reaction"), STIMULUS, STIMULUS["variants"][1], None, {}, applicable_spec(spec, STIMULUS, {}), 150)
    assert "只在 pol_stance 选了 conditional 或 oppose 时回答" in text and " in [" not in text
    assert "\n\n\n" not in text
    spec["measures"]["pol_stance"]["ask_if"] = "something else"
    with pytest.raises(ValueError):
        applicable_spec(spec, STIMULUS, {})


def test_conditional_question_may_be_left_out_of_the_answer():
    m = CFG["measures"]
    spec = measure_spec(m["marketing"], m["scales"], ["mkt_appeal", "mkt_price_feel"])
    ok = answer(answers={"mkt_appeal": 4}, behavior="none")
    assert validate_payload(ok, spec, SCHEMA)[1] is None  # 条件题可以不答
    assert validate_payload(ok, applicable_spec(spec, {}, {}), SCHEMA)[1] is None
    assert validate_payload(answer(answers={"mkt_price_feel": "good_deal"}, behavior="none"), spec, SCHEMA)[1].startswith("缺少题目")
    # 这个版本没问的题，答了也不合格
    assert "未问" in validate_payload(answer(answers={"mkt_appeal": 4, "mkt_price_feel": "good_deal"}, behavior="none"),
                                    applicable_spec(spec, {}, {}), SCHEMA)[1]


def test_blank_lines_inside_the_stimulus_reach_the_model_unchanged():
    spaced = {**STIMULUS["variants"][1], "content": "第一段\n\n\n\n第二段"}
    tight = {**spaced, "content": "第一段\n\n第二段"}
    assert "第一段\n\n\n\n第二段" in user(spaced) and user(spaced) != user(tight)
    assert "\n\n\n" not in user(tight) and "\x00" not in user(tight)


def test_template_with_an_unknown_variable_fails_loudly(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "persona_system.md").write_text("```text\n你是{{ nmae }}\n```\n", encoding="utf-8")
    with pytest.raises(jinja2.UndefinedError):
        system_prompt(load_template(tmp_path, "persona_system"), OFFICE, CFG["mbti_behavior_map"], CFG["persona_text"], "2026-11-22")
    (tmp_path / "prompts" / "empty.md").write_text("没有模板块", encoding="utf-8")
    with pytest.raises(ValueError):
        load_template(tmp_path, "empty")


def test_answer_wrapped_in_a_code_block_is_accepted():
    payload, reason, truncated = validate_payload("好的：\n```json\n" + answer() + "\n```", SPEC, SCHEMA, 150)
    assert reason is None and payload["answers"]["pol_fair"] == 3 and not truncated
    assert validate_payload("[1, 2]", SPEC, SCHEMA)[0] is None
    assert validate_payload("我觉得还行", SPEC, SCHEMA)[1] == "不是合法的 JSON：Expecting value"


def test_first_complete_json_object_ignores_trailing_braces_and_prose():
    for text in (answer() + "}", "说明 {x} 后再回答：" + answer()):
        payload, reason, _ = validate_payload(text, SPEC, SCHEMA)
        assert reason is None and payload["answers"]["pol_fair"] == 3


def test_truncated_json_object_remains_invalid():
    for text in ('{"noticed": true', '{"reaction": ' + answer()):
        payload, reason, _ = validate_payload(text, SPEC, SCHEMA)
        assert payload is None and reason.startswith("不是合法的 JSON：")


def test_long_quote_is_cut_to_the_persona_limit_not_rejected():
    payload, reason, truncated = validate_payload(answer(quote="啊" * 200), SPEC, SCHEMA, 90)
    assert reason is None and truncated and len(payload["quote"]) == 90
    assert validate_payload(answer(quote="啊" * 90), SPEC, SCHEMA, 90)[2] is False


def test_not_noticing_is_a_valid_reaction_with_no_answers():
    payload, reason, _ = validate_payload(answer(noticed=False, behavior="voice"), SPEC, SCHEMA, 150)
    assert reason is None and payload["answers"] == {} and payload["behavior"] == "none"


class Recorder(MockBackend):
    """第一次给每个人物的引语超长且格式不对，记下收到的提示词。"""
    model_id = "recorder"

    def __init__(self):
        self.users = []

    def complete(self, system, user, *, seed, spec=None):
        self.users.append(user)
        if "上一次的输出不合格" not in user:
            return "我觉得还行"
        payload = json.loads(super().complete(system, user, seed=seed, spec=spec))
        payload["quote"] = "啊" * 220
        return json.dumps(payload, ensure_ascii=False)


def test_retry_tells_the_model_what_was_wrong_and_run_marks_truncation(tmp_path):
    backend = Recorder()
    summary = run_panel(EX / "experiment.policy.yaml", EX / "personas", tmp_path, Gateway(backend, tmp_path / "recording.jsonl"))
    assert "不是合法的 JSON" in backend.users[1] and backend.users[1].startswith(backend.users[0])
    rows = [json.loads(line) for line in (tmp_path / "responses.jsonl").read_text(encoding="utf-8").splitlines()]
    limits = {p["persona_id"]: None for p in load_personas(EX / "personas")}
    for r in rows:
        assert r["valid"] and r["truncated"] is True and len(r["payload"]["quote"]) < 220
        limits[r["persona_id"]] = len(r["payload"]["quote"])
    assert len(set(limits.values())) > 1  # 上限因人而异（由 E/I 轴决定）
    assert summary["truncated"] == len(rows) and set(summary["inputs"]["prompts"]) == {"panel_reaction", "persona_system"}


def test_placebo_is_asked_the_same_questions_as_its_target(tmp_path):
    """样例里的安慰剂没有写 attributes；带属性条件的题必须跟着它对应的版本走。"""
    measures = yaml.safe_load((ROOT / "config" / "measures.yaml").read_text(encoding="utf-8"))
    measures["policy"]["measures"]["pol_fair"]["ask_if"] = "variant.attributes.appeal exists"
    root = tmp_path / "root"
    for sub in ("config", "schemas", "prompts", "examples"):
        shutil.copytree(ROOT / sub, root / sub)
    (root / "config" / "measures.yaml").write_text(yaml.safe_dump(measures, allow_unicode=True), encoding="utf-8")
    backend = Recorder()
    run_panel(root / "examples" / "experiment.policy.yaml", EX / "personas", tmp_path / "run", Gateway(backend, tmp_path / "recording.jsonl"), root=root)
    calls = [json.loads(line) for line in (tmp_path / "recording.jsonl").read_text(encoding="utf-8").splitlines()]
    first = {c["request"]["user"] for c in calls if c["ordinal"] == 0}
    by_call = {"c_" + c["call_key"][:16]: c["request"]["user"] for c in calls}
    users = {}
    for line in (tmp_path / "run" / "responses.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r["call_id"]:  # 记的是重试那一次；去掉附在后面的不合格原因就是首次的提示词
            users.setdefault(r["variant_id"], set()).add(next(u for u in first if by_call[r["call_id"]].startswith(u)))
    assert users["B"] == users["B_PLACEBO"] and "pol_fair" in next(iter(users["B"]))
    assert all("- pol_fair：" not in u for u in users["A"])


def test_only_a_leading_think_block_is_dropped():
    payload, reason, _ = validate_payload("<think>选 {wait} 吧</think>\n" + answer(quote="他说<think>再想想</think>吧"), SPEC, SCHEMA, 150)
    assert reason is None and payload["quote"] == "他说<think>再想想</think>吧"
