"""把 prompts/ 下的模板渲染成提示词。模板正文是各文件里第一个 ```text 代码块。"""
from __future__ import annotations

import re
from pathlib import Path

import jinja2

from mirosim.persona.behavior import soft_text

_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)  # 变量缺失即报错，不静默留空


def load_template(root: Path, name: str) -> jinja2.Template:
    text = (root / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    found = re.search(r"```text\n(.*?)\n```", text, re.S)
    if not found:
        raise ValueError(f"prompts/{name}.md 里没有 ```text 模板块")
    return _ENV.from_string(found.group(1))


def system_prompt(template: jinja2.Template, persona: dict, behavior_cfg: dict, text_cfg: dict, as_of_date) -> str:
    """同一人物、同一时间锚点下逐字节不变。四字母类型不进提示词，只给由轴分值渲染的行为描述。"""
    identity, psyche, situation = persona["identity"], persona["psyche"], persona["situation"]
    household, job = identity.get("household"), identity["occupation"]
    household_text = ""
    if household:
        children = household.get("children")
        parts = [text_cfg["marital"][household["marital"]]] if "marital" in household else []
        if children is not None:
            parts.append(f"有{children}个孩子" if children else "没有孩子")
        household_text = "，".join(parts) + "。" if parts else ""
        if household.get("living_with"):
            household_text += household["living_with"] + "。"
    employment = text_cfg["employment_type"].get(job.get("employment_type"))
    bounds = situation.get("knowledge_bounds", {})
    return template.render(
        name=identity["name"],
        age=int(str(as_of_date)[:4]) - identity["birth_year"],  # 用刺激物的时间锚点，不用系统当前日期
        gender_text=text_cfg["gender"][identity["gender"]],
        education_text=text_cfg["education"][identity["education"]],
        region_text=text_cfg["region_tier"][identity["region_tier"]],
        household_text=household_text,
        occupation_text=f"{job['title']}（{employment}）" if employment else job["title"],
        years=job.get("years"),
        income_text=text_cfg["income_band"][identity["income_band"]],
        core_life_events=[e for e in persona.get("history", {}).get("life_events", []) if e.get("core")][:3],
        stakes=situation.get("stakes", []),
        constraints=situation.get("constraints", []),
        soft_lines=soft_text(persona, behavior_cfg),
        values_text="、".join(text_cfg["values"][v] for v in psyche["values_top3"]),
        language_style=psyche["expression"]["language_style"],
        catchphrase=psyche["expression"].get("catchphrase"),
        knows_text="；".join(bounds.get("knows", [])) or "自己日常接触到的事",
        does_not_know_text="；".join(bounds.get("does_not_know", [])) or "自己日常接触不到的事",
    )
