# trait_probe — 人物品质检验

目的：检验写在人物卡里的性格，是否真的出现在这个人物的行为里。检验对象是**行为**，不是让人物自报性格。

## 探针题库

每条轴与每项特质配 6 道情境迫选题，共 54 道，存于 `data/probes/v1.yaml`（实施时编写，题目须经人工审定）。
题目不出现性格词汇，只给日常情境与两个都说得通的做法。例：

```yaml
- id: ei_03
  axis: ei
  situation: "同事聚餐，席间聊到一个你有不同看法的话题，大家正说得热闹。"
  options:
    a: {text: "插进去说说你的看法", sign: 1}
    b: {text: "先听着，回头单独跟熟的人说", sign: -1}
- id: jp_02
  axis: jp
  situation: "周末要出门办三件事，其中一件临时说可能办不成。"
  options:
    a: {text: "把顺序和时间重新排一遍再出门", sign: 1}
    b: {text: "先出门，到时候看情况", sign: -1}
```

## 调用（tier-economy，temperature 0.7，每题独立调用，系统提示词为 `persona_system.md`）

```text
{{ situation }}
你会怎么做？
A. {{ options.a.text }}
B. {{ options.b.text }}
只输出 JSON：{"choice": "A" 或 "B", "why": "一句话"}
```

A/B 的先后由 `u01(seed, persona_id, "probe_order", probe_id)` 决定，抵消位置偏好。

## 指标

| 指标 | 算法 | 入库门槛 |
|---|---|---|
| `trait_recovery` | 非边界轴与非中间特质中，探针得分符号与卡上分值符号一致的比例 | ≥ 0.70 |
| `retest_consistency` | 整套题隔一次会话再做一遍，两遍选择相同的比例 | ≥ 0.80 |
| `distinctiveness` | 与同分群其他人物的 54 维答案向量的平均汉明距离 / 54 | ≥ 0.25 |
| 库级：轴的可分性 | 全库中，卡上分值与探针得分的 Spearman 相关，分轴计算 | 每轴 ≥ 0.40 |

库级指标不达标的那条轴，说明 `config/mbti_behavior_map.yaml` 里对应的软渲染文字不起作用，要改写映射并升版本，而不是逐个重生成人物。

## 何时重跑

- 人物卡升版本；
- `persona_system.md` 或映射表改动；
- 人物所用模型更换（同一张卡在不同模型上的还原度不同，`quality` 按模型分别记录）；
- 人物累计提交的记忆超过 20 条（检查漂移）。
