# theme_coding — 开放题编码

把 `pol_worry`、`pol_condition`、`mkt_objection`、`reasons`、`quote` 等自由文本归并成可计数的主题。
用 `tier-reason`，temperature 0.2；所用模型应与人物模型不同族（`config/model_tiers.yaml: rules`）。

## 第一遍：归纳主题（每个实验一次）

从全部有效回答中按分群分层抽 200 条（不足则全取）：

```text
下面是一批人对同一件事的回答，每行一条，前面是编号。
请归纳出 8 到 15 个互不重叠的主题。每个主题给出：
- theme_id：英文小写加下划线
- label：不超过 12 个字
- definition：一句话，说明什么算、什么不算
- examples：两个编号
另设 other（归不进去的）与 none（没有实质内容的）。只输出 JSON 数组。

{{ numbered_texts }}
```

人工过一遍主题表（合并、改名、删除），定稿后存为 `codebook`，哈希写入 `run_manifest`。

## 第二遍：逐条编码（每批 40 条）

```text
按下面的主题表给每条回答编码。一条回答可以有一到两个主题。
每个主题必须给出 evidence：从原文里原样摘出的一段，不得改写。

【主题表】
{{ codebook }}

【回答】
{{ numbered_texts }}

只输出 JSON 数组：[{"n": 1, "themes": [{"theme_id": "", "evidence": ""}]}]
```

## 校验

| 检查 | 做法 |
|---|---|
| 证据回查 | `evidence` 必须是原文的子串，否则该条编码作废重编（沿用 v3.0 的 `evidence_span` 做法） |
| 编码一致性 | 抽 10% 用另一模型族再编一遍，按主题算 Cohen's κ；κ < 0.6 的主题在报告里标「编码不稳」，不用于下结论 |
| `other` 占比 | 超过 15% 时回到第一遍补主题 |
| 人工抽检 | 每个实验抽 50 条，人工判对错，准确率写入报告 |
