# deliberation_turn — 研讨模式的单次发言

沿用 v3.0 的主持人—代表结构与轮次。这里只规定人物一侧的调用，与 v3.0 的差别有三处：
谁发言由代码抽签（`speak_propensity`）；发言前先私下记一笔立场（用于量从众）；输出是结构化的。

```text
今天是{{ as_of_date }}。你在参加一个关于「{{ issue_title }}」的讨论会，在场的还有：{{ attendee_list }}。
{% if memories %}
【你记得的相关的事】
{% for m in memories %}- {{ m.occurred_on }}：{{ m.content }}
{% endfor %}{% endif %}
【讨论的方案】
{{ variant_content }}

【到现在为止大家说了什么】
{{ transcript_window }}

{% if majority_hint %}【场上的气氛】
{{ majority_hint }}
{% endif %}
【主持人现在问】
{{ moderator_prompt }}

只输出一个 JSON 对象：
{
  "private_stance": "support | conditional | oppose | indifferent",   // 你心里真实的立场，不会被别人看到
  "private_note": "",          // 你心里的一句话，不会被别人看到
  "want_to_speak": true,       // 这一轮你想不想开口
  "speech": "",                // 你要说的话，不超过 {{ max_chars }} 字；不想说就留空
  "addressed_to": null,        // 这话主要是对谁说的，填名字或 null
  "public_stance": "support | conditional | oppose | indifferent",    // 你在会上表现出来的立场
  "condition": null            // 如果是 conditional，条件是什么
}
```

## 代码一侧的规则

| 规则 | 做法 |
|---|---|
| 谁发言 | 每轮对每个人物计算 `speak_propensity`（含与在场最高权力者的 `power_gap`），用 `u01(seed, persona_id, "speak", replicate, round)` 抽签。抽中且 `want_to_speak=true` 的发言；全场无人发言时主持人点名倾向值最高的一位 |
| 可见范围 | `transcript_window` 只含公开的 `speech`，按 v3.0 §4.5 的上下文裁剪规则压缩；任何人的 `private_*` 字段不进入别人的上下文 |
| 多数信号 | `majority_hint` 是否给出，由 `majority_visibility` 抽签决定；给出时只写「多数人倾向于……」，不点名 |
| 立场变动上限 | 相邻两轮 `private_stance` 的变动超过 `attitude_shift_cap` 时，标记 `suspicious_shift`，交由质检，不自动改写 |
| 从众度量 | `private_stance ≠ public_stance` 记为一次表面服从；每场统计比例，写入报告 |
| 发言长度 | 超过 `max_chars` 截断并记录 |

`private_stance` 进入 v3.0 的投票与共识模块（替代原先从发言文本里推断立场），`speech` 进入命题抽取。两者分开，是为了让「说了什么」和「心里怎么想」各有证据。
