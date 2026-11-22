# panel_reaction — 群体反应模式的单次调用

一个人物、一个方案版本、一次调用。系统提示词用 `persona_system.md`；下面是用户消息。

```text
今天是{{ as_of_date }}。

{% if memories %}【你记得的相关的事】
{% for m in memories %}- {{ m.occurred_on }}：{{ m.content }}
{% endfor %}{% endif %}
{% if prior_attitude %}【你原本对「{{ prior_attitude.object_label }}」的看法】
{{ prior_attitude.text }}
{% endif %}
【最近的情况】
{{ background }}

【你{{ channel_phrase }}看到了这个】
{{ variant_content }}

{% if peer_signal %}【你还听到】
{{ peer_signal }}
{% endif %}
请按下面的顺序想，并只输出一个 JSON 对象，不要输出别的文字。

1. noticed：这东西你会停下来看吗？true 或 false。跟你没关系、你平时就不看这类东西的，就是 false。
2. takeaway：用你自己的话说，它讲的是什么。只写你看懂的部分，看岔了也照你看到的写。noticed 为 false 时写你扫一眼的印象，可以为空。
3. inner_reaction：你心里第一下的念头。这是不会说出口的那句。
4. answers：逐项回答下面的问题。数字题只填数字；拿不准的填 "dk"；标了条件而你不符合的不填。
{% for m in measures %}   - {{ m.id }}：{{ m.q }} {{ m.format_hint }}
{% endfor %}5. behavior：接下来你实际最可能做的一件事，从下面选一个代号。说的是你真会做的，不是应该做的。
{% for k, v in behavior_options.items() %}   - {{ k }}：{{ v }}
{% endfor %}6. reasons：一到三条，每条不超过 20 个字，为什么这么选。
7. share：would_share（你会不会跟别人提这事）；会的话 to_whom（跟谁）、what（你会怎么说，一句话，按你平时的口气）。
8. quote：如果有人当面问你怎么看，你会怎么答。不超过 {{ max_chars }} 个字。

输出格式：
{"noticed": true, "takeaway": "", "inner_reaction": "", "answers": {}, "behavior": "", "reasons": [], "share": {"would_share": false, "to_whom": null, "what": null}, "quote": ""}
```

## 代码一侧的规则

1. **先想后答**：字段顺序固定，先 `noticed`、`takeaway`、`inner_reaction`，再打分。打分在前会诱发事后找理由。
2. **`noticed=false` 时**：`answers` 只保留 `allow_dk` 的项并记为缺失，`behavior` 强制为 `none`。这是真实的「没在意」，计入分母，不重试。
3. **自然曝光**：是否把刺激物给到人物，由代码按 `media_diet × channel_plan` 抽签决定（`docs/03` §3）。没抽中的人物不调用模型，直接记 `exposed=false`。
4. **`peer_signal`**：仅 diffusion 模式第 2 步起有值，内容是代码从上一步别人的 `share.what` 里抽出的原话。
5. **校验**：输出按 `schemas/response.schema.json` 和 `config/measures.yaml` 校验。不合格重试一次（附上校验错误），再不合格记 `valid=false` 并写明原因，不补造。
6. **`max_chars`**：来自硬控制参数；超长截断并记 `truncated=true`，超长率进入质检指标。
7. **每版本独立调用**：同一人物看不同版本是互不相通的独立对话。不允许在一次调用里让人物比较几个版本（除非实验就是要测「对比之下的选择」，那要另设 `kind: choice_set`）。
