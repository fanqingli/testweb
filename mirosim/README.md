# MiroSim 平行推演系统 · 可执行设计

> 本目录全部内容为系统设计与样例。样例中的人物、品牌、政策均为虚构的合成数据，不代表任何真实个人、机构或真实意见（`SIMULATION_NOTICE_v1`）。

## 这是什么

在《MiroSim 平行推演系统 · 详细设计 v3.0》的基础上，把「用可复用的虚拟人物预评估政策、营销与品牌方案」这件事细化到可以开工的程度：
设计文档、数据契约、配置、提示词模板、样例数据，以及两个可以直接运行的工具。

一句话：**给定一件还没发生的事的几个候选版本，让同一批有长期档案的虚拟人在互不相通的平行世界里各经历一个版本，比较他们的理解、态度、打算和说法有什么不同，以及这些不同出现在哪类人身上。**

## 相对 v3.0 做了什么

| | v3.0 | 本设计 |
|---|---|---|
| 推演形态 | 会议研讨 | 群体反应、研讨、传播三种模式，共用人物库与分析底座 |
| MBTI | 人物卡上的一个字符串 | 四字母 + 四条连续轴 + 五项补充特质；发言概率、字数、转述、立场变动上限由代码执行 |
| 人物成长 | 每场后自动更新 | 人物版本不可变、记忆只追加、平行世界分支；只有现实中发生了的版本才写入人物的人生 |
| 人群 | 相关方代表 | 另加人群框架：分群、占比、抽样、代表性权重 |
| 对照 | 单变量敏感性 | 现状、安慰剂、负对照；安慰剂差异作为噪声底 |
| 可复现 | 重放一致率 ≥ 90% | 拆成回放（逐字节一致）与重跑稳定性（报告区间） |
| 场景 | 政策 | 政策、营销、品牌三个场景包 |
| 结论强度 | 不随校准状态变 | 四级效度阶梯，等级决定报告能下什么结论 |

v3.0 的运行清单、分析台账、命题级共识、影响权重、证据回指、模型档位、行业树与岗位族、合规要求均保留沿用。详见 `docs/00-thinking-and-decisions.md` §3。

## 目录

```text
mirosim/
├── docs/        设计文档，从 00 读起
│   ├── 00-thinking-and-decisions.md             思路梳理、对 v3.0 的判断、八个关键决策、依据
│   ├── 01-architecture-and-modes.md             分层、核心对象、运行矩阵、三种模式、漏斗、技术选型
│   ├── 02-persona-and-archive.md                人物构成、MBTI 的用法、建库与质检、长期档案与平行世界
│   ├── 03-engines-and-realism.md                调用流程、确定性随机、曝光、传播、十二种失真、对照、可复现
│   ├── 04-scenario-packs.md                     政策、营销、品牌：刺激物、量表、行为、报告结构
│   ├── 05-analysis-evaluation-calibration.md    汇总、配对比较、编码、效度阶梯、校准
│   ├── 06-data-model-and-interfaces.md          表结构、目录、命令行、模型网关、接口、成本
│   └── 07-implementation-plan.md                分期、验收、风险、合规、第一步
├── schemas/     七份 JSON Schema：人物、记忆、人群框架、刺激物、实验、反应、运行清单
├── config/      MBTI 行为映射表、量表、渠道、模型档位
├── prompts/     七份提示词模板：人物系统提示词、群体反应、研讨发言、人物生成、品质探针、主题编码、记忆提交
├── examples/    三张人物卡、两个人群框架、三个刺激物、三个实验配置、样例反应与记忆
├── tools/       validate_examples.py（校验）、derive_behavior.py（行为参数的命令行入口）
├── src/mirosim/ 群体反应模式的骨架代码（见下）
└── tests/
```

## 马上可以运行的部分

```bash
cd mirosim
python -m venv .venv && . .venv/bin/activate
pip install "jsonschema>=4" pyyaml

python tools/validate_examples.py
# 通过：3 张人物卡、3 个刺激物、2 个人群框架、3 个实验配置、样例反应与记忆

python tools/derive_behavior.py examples/personas/p_0a31f7.json --power-gap 0.8
# 输出该人物的硬控制参数与写入提示词的行为描述
```

`validate_examples.py` 除 schema 校验外，还检查 schema 表达不了的约束：MBTI 类型与轴分值是否一致、渠道是否已定义、安慰剂内容是否与原版本逐字相同、量表项是否属于对应场景包、分群占比是否合计为 1、调用量是否超出预算。实施时把它接进 CI。

## 可运行的骨架（群体反应模式）

`src/mirosim/` 下是第一步代码，只为打通流程：确定性随机、按人群框架取人与加权、公共随机数曝光、调用的录制与回放、反应校验与一次重试、调用预算、配对比较。**后端只有 `mock`，它按种子给出格式合法的随机答案，输出不是模拟结果。**

```bash
pip install -e ".[dev]"
pytest

python -m mirosim.cli run-panel --experiment examples/experiment.policy.yaml \
    --personas examples/personas --out runs/demo
python -m mirosim.cli run-panel --experiment examples/experiment.policy.yaml \
    --personas examples/personas --out runs/demo --replay     # 不发调用，产物逐字节相同
python -m mirosim.cli contrast --run runs/demo \
    --experiment examples/experiment.policy.yaml --measure pol_fair
```

**取人与加权。** 运行时按实验配置里的人群框架，从 `--personas` 目录里给每个分群取 `n` 人（同一批人用于所有版本与重复），写出 `sample.jsonl`；`contrast` 同时给出加权与不加权两个数。目录里人数不够时不补人，只在 `summary.json` 的 `sample` 里报告缺口（`shortfall`）和抽到了人的分群占比之和（`covered_share`）。`covered_share` 小于 1 时，加权数字只代表这一部分人群。样例目录只有 3 个人物，所以样例实验的 `covered_share` 远小于 1。

骨架里还没有的：真实模型后端、人物工厂（库内人数不足时补人）、分群内的多样性控制、记忆读取、多模型族、数据库（产物是 `runs/` 下的 JSONL）、`prompts/` 模板的接入（提示词是临时的最小版本）、字数等硬控制、报告。

## 现状与未做

| 已有 | 未做 |
|---|---|
| 设计文档八份 | 推演引擎、模型网关、分析与报告的代码，除上节的骨架外均未写（`docs/06` §2 列出了模块划分，`docs/07` 给出分期） |
| 数据契约、配置、提示词模板 | 探针题库（`prompts/trait_probe.md` 给出了格式与样题，54 道题待编写） |
| 样例数据与校验工具 | 具体模型的选定与单价（`config/model_tiers.yaml` 中留空，第 0 期实测后填写） |
| 行为参数的参考实现 | 与真实数据的任何对照 |

需要特别说明的三点：

1. **没有调用过任何模型。** 样例里的人物反应（`examples/response.sample.json`）是手写的格式示例，不是模型的输出。
2. **MBTI 行为映射表是待检验的设计假设。** `config/mbti_behavior_map.yaml` 中的系数与描述都是初值，第 0 期的首要任务就是检验它们是否起作用（`docs/07` §1）。
3. **系统的预测效度未知。** 所引用的研究以美国受试者为主；在中文语境下模拟结果与现实相差多少，只能靠系统自己的校准环节回答（`docs/05` §6）。在那之前，按设计只允许下定性和排序的结论。
