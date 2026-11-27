# persona_factory — 人物生成

两步：代码先按人群框架抽出**骨架**（所有可量化字段），模型只负责把骨架写成一个具体的人。
模型不决定性格分值、收入、年龄——这些由代码抽样，否则分布会塌向模型心目中的「典型人」。

## 第一步：代码抽骨架

| 字段 | 抽样办法 |
|---|---|
| 分群、行业、岗位族、从业年限 | 按人群框架 `segments[].filter` |
| 出生年、性别、学历、城市线级、收入档 | 按分群内的边际分布抽；分布表缺失时均匀抽并在 `provenance` 记 `prior: uniform` |
| MBTI 四轴 | 类型按 `mbti_prior` 抽；每轴幅度按 `config/mbti_behavior_map.yaml: sampling` 抽，15% 概率落在边界区 |
| 五项补充特质 | 按 `sampling.traits` 抽（Beta(2,2)）。v1 假设各特质相互独立，`sampling.trait_correlations` 留空，有实测数据后再填 |
| 动机类型、价值观前三 | 按分群条件分布抽；同一分群内「MBTI 类型 + 动机类型」相同的不超过 `sampling.max_same_combo_per_segment` |
| 初始倾向 | 分群内至少覆盖 `min_stance_spread_per_segment` 档；倾向只影响生活事件的生成，不写进人物卡 |

## 第二步：模型写人（tier-reason，temperature 0.9）

```text
你要根据下面的骨架写出一个虚构的普通人。这个人不存在，不得使用任何真实人物的姓名、真实机构或企业的名称。

【他属于哪类人】{{ segment_label }}。{% if frame_universe %}这批人物的范围：{{ frame_universe }}。{% endif %}岗位、行业和经历都要符合这一身份。

【骨架】
{{ skeleton_json | tojson(indent=2) }}

【籍贯与现居】
籍贯：{{ region_labels[skeleton_json.identity.home_region] }}；现居：{{ region_labels[skeleton_json.identity.residence_region] }}。

【同一批里已经写好的人（避免雷同）】
{{ sibling_summaries }}

请写出下列字段，只输出 JSON，字段直接使用下列顶层键：
- name：常见的中文姓名，不与已有的重名，不用名人的名字。
- title：具体岗位，不超过 10 个字。
{% if "industry_code" not in skeleton_json.identity.occupation %}
- industry_code：格式为 I 加 4 位数字。
{% endif %}
{% if "job_family" not in skeleton_json.identity.occupation %}
- job_family：只能是 F1、F2、F3、F4、F5、F6。
{% endif %}
{% if "employment_type" not in skeleton_json.identity.occupation %}
- employment_type：只能是 employee、self_employed、gig、owner、student、retired、unemployed、homemaker。
{% endif %}
{% if "years" not in skeleton_json.identity.occupation %}
- years：非负整数。
{% endif %}
- marital：必填，只能是 single、married、divorced、widowed。
- children：必填，非负整数。
- living_with：家里的居住情况，一句话。
- life_events：数组，3 到 5 条。每条是对象 {"year": 整数, "event": "发生了什么，要具体到一件事", "impact": "这件事让他从此怎么看、怎么做", "core": true 或 false}，最多 3 条 core 为 true。
  至少一条与骨架里的「初始倾向」方向相反或无关——真人的经历不是为某个立场准备的。
  「初始倾向」指他对与这批人相关的事务（见上面的范围）最初的态度。
- stakes：2 到 4 条，他眼下最在意的具体利益，要带得出数字或对象。
- constraints：2 到 4 条，限制他选择的具体条件。
- media_diet：数组，每项是对象 {"channel": 渠道代码, "share": 0 到 1 的小数}。渠道代码只能从 {{ channel_ids | join("、") }} 里选 3 到 5 个，share 合计不超过 1。
- consumption：对象 {"decision_role": ..., "categories": [3 到 5 个品类], "habits": [2 到 3 条具体的买东西习惯]}。decision_role 只能是 decider（家里买东西他拍板）、influencer（能左右别人的决定）、user_only（只用不管买）、none。
- knowledge_bounds：对象 {"knows": [...], "does_not_know": [...]}，各 2 到 4 条。does_not_know 要包含至少一条外人以为他该懂、其实他不懂的。
- language_style：一句话描述他怎么说话。
- catchphrase：口头禅，可为 null。

字符串里引用别人的话或说法时用「」，不要用英文双引号，否则 JSON 会坏。

要求：
1. 性格只通过经历和习惯体现，不要出现性格类型的名称或「内向」「理性」这类形容词。
2. 不要写成这类人的标本。给他一处与身份不那么相称的地方。
3. 经历里不要出现英雄事迹、重大奇遇。普通人的转折多是小事。
4. 按籍贯和现居地写具体的生活细节（饮食、方言里的口头禅、消费和人情往来的习惯），但不要用地域给他贴性格标签，同一地方的人也各不相同。
```

## 第三步：入库前质检（全部由代码或独立调用完成）

| 检查 | 标准 | 不过怎么办 |
|---|---|---|
| schema 校验 | `schemas/persona.schema.json` | 重新生成 |
| 实名筛查 | 姓名、机构不在黑名单；不与公众人物重名 | 换名 |
| 雷同 | 与同分群已有人物的生活事件文本相似度 < 0.80 | 重新生成 |
| 特质还原 | 见 `trait_probe.md`，`trait_recovery ≥ 0.70` | 重新生成；连续三次不过，检查映射表 |
| 重测一致 | `retest_consistency ≥ 0.80` | 重新生成 |
| 人工抽检 | 每批抽 5%，不少于 10 张，按清单打分 | 整批不合格率 > 20% 则整批退回 |

通过后写入 `personas` 与 `persona_versions`（version=1），`provenance.generator` 记模型档位与提示词哈希。
