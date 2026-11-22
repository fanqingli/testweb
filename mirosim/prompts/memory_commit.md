# memory_commit — 把一次反应写成人物的记忆

只在实验负责人执行 `commit` 时调用：把**现实中真的发生了的那个版本**对应的反应，写进人物的基准世界。
其余版本的反应留在 `responses` 表里，永不进入人物记忆。

## 调用（tier-economy，temperature 0.3，系统提示词为 `persona_system.md`）

```text
{{ occurred_on }}，发生了这件事：
{{ variant_content }}

你当时的反应是：
- 你的理解：{{ takeaway }}
- 你心里想的：{{ inner_reaction }}
- 你打算：{{ behavior_text }}
- 你说过：{{ quote }}
{% if outcome %}
后来实际发生的是：
{{ outcome }}
{% endif %}
过一阵子再想起这件事，你会记得什么？用第一人称写一到两句，不超过 80 个字。
只写你记得住的：大概是什么事、你当时什么感觉、后来怎样。不要复述条文，不要总结道理。

只输出 JSON：{"content": "", "valence": -2到2的整数, "importance": 1到10的整数}
```

## 代码一侧的规则

1. `occurred_on` 取刺激物的 `as_of_date`；`world_id` 为基准世界；`seq` 取全局自增。
2. `objects` 由代码填：`{pack}:{stimulus_id}` 加上刺激物配置里声明的主题与品牌标签。检索靠它，不靠模型自由打标签。
3. `source` 记 `committed_run` 与对应的 `run_id`、`response_id`，可回溯。
4. `noticed=false` 的反应不生成记忆——没在意的事不会被记住。
5. `outcome`（事后实际结果）可在首次提交后补录：追加一条新记忆并在 `supersedes` 指向旧条目，旧条目保留。
6. 同时更新态度表：对象为刺激物声明的品牌或政策主题，取值由 `brd_favor_post` 或 `pol_stance` 换算，单次变动不超过 `attitude_shift_cap`。
7. 每累计 10 条提交的记忆，触发一次反思：让人物用一到两句概括这段时间对某个对象的看法，存为 `kind: reflection`，`importance` 取被概括条目的最大值。原条目不删。
