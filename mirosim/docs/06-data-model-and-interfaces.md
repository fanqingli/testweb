# 06 数据模型与接口

## 1. 表结构

SQLite 与 PostgreSQL 通用的写法。JSON 列在 SQLite 中存为文本，在 PostgreSQL 中用 `jsonb`。所有时间为 UTC 的 ISO 8601 字符串。

```sql
-- ============ 人物层（长期）============
CREATE TABLE personas (
  persona_id      TEXT PRIMARY KEY,                -- p_xxxxxx
  current_version INTEGER NOT NULL,
  status          TEXT NOT NULL CHECK (status IN ('active','suspended','retired')),
  segment_tags    TEXT NOT NULL,                   -- JSON 数组，供抽样筛选
  created_at      TEXT NOT NULL
);

CREATE TABLE persona_versions (                    -- 只插入，不更新
  persona_id TEXT NOT NULL REFERENCES personas,
  version    INTEGER NOT NULL,
  card       TEXT NOT NULL,                        -- 完整人物卡 JSON，符合 persona.schema.json
  card_hash  TEXT NOT NULL,                        -- sha256(规范化 JSON)
  created_at TEXT NOT NULL,
  PRIMARY KEY (persona_id, version)
);

CREATE TABLE persona_quality (                     -- 探针结果，按模型分别记录
  persona_id TEXT NOT NULL, version INTEGER NOT NULL, model_id TEXT NOT NULL,
  trait_recovery REAL, retest_consistency REAL, distinctiveness REAL,
  checked_at TEXT NOT NULL,
  PRIMARY KEY (persona_id, version, model_id)
);

CREATE TABLE worlds (
  world_id   TEXT PRIMARY KEY,                     -- w_base 或 w_{experiment}_{variant}_r{n}
  parent_id  TEXT REFERENCES worlds,               -- 基准世界为 NULL
  fork_seq   INTEGER,                              -- 分叉点：父世界中序号 ≤ 此值的记忆可见
  kind       TEXT NOT NULL CHECK (kind IN ('base','sandbox')),
  sealed_at  TEXT                                  -- 封存后不可再写
);

CREATE TABLE memory_items (                        -- 只插入，不更新
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,   -- 全局递增
  memory_id   TEXT NOT NULL UNIQUE,
  persona_id  TEXT NOT NULL REFERENCES personas,
  world_id    TEXT NOT NULL REFERENCES worlds,
  kind        TEXT NOT NULL CHECK (kind IN ('world_event','interaction','experience','reflection')),
  occurred_on TEXT NOT NULL,
  content     TEXT NOT NULL,
  valence     INTEGER NOT NULL, importance INTEGER NOT NULL,
  objects     TEXT NOT NULL,                       -- JSON 数组，对象标签
  source      TEXT NOT NULL,                       -- JSON
  supersedes  TEXT
);
CREATE INDEX idx_mem_lookup ON memory_items (persona_id, world_id, seq);
CREATE VIRTUAL TABLE memory_fts USING fts5(content, content='memory_items', content_rowid='seq', tokenize='trigram');

CREATE TABLE attitudes (                           -- 每次变动插入一行，取最新
  persona_id TEXT NOT NULL, world_id TEXT NOT NULL, object TEXT NOT NULL,
  seq        INTEGER NOT NULL,                     -- 对应触发它的记忆序号
  valence    REAL NOT NULL, confidence REAL NOT NULL,
  basis      TEXT NOT NULL,                        -- JSON 数组：memory_id
  PRIMARY KEY (persona_id, world_id, object, seq)
);

CREATE TABLE relations (
  from_id TEXT NOT NULL, to_id TEXT NOT NULL, rtype TEXT NOT NULL,
  strength REAL NOT NULL, valid_from_seq INTEGER NOT NULL, valid_to_seq INTEGER,
  PRIMARY KEY (from_id, to_id, rtype, valid_from_seq)
);

-- ============ 实验层 ============
CREATE TABLE experiments (
  experiment_id TEXT PRIMARY KEY,
  config        TEXT NOT NULL,                     -- 实验配置 JSON（含内联的刺激物与人群框架）
  config_hash   TEXT NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('draft','approved','running','qc_failed','analyzed','archived')),
  manifest      TEXT,                              -- run_manifest JSON
  created_at    TEXT NOT NULL
);

CREATE TABLE experiment_personas (                 -- 人物锁定
  experiment_id TEXT NOT NULL, persona_id TEXT NOT NULL, version INTEGER NOT NULL,
  segment_id TEXT NOT NULL, weight REAL NOT NULL, reused INTEGER NOT NULL,
  PRIMARY KEY (experiment_id, persona_id)
);

CREATE TABLE runs (                                -- 运行矩阵的一格
  run_id TEXT PRIMARY KEY,
  experiment_id TEXT NOT NULL REFERENCES experiments,
  variant_id TEXT NOT NULL, replicate INTEGER NOT NULL, model_family TEXT NOT NULL,
  world_id TEXT REFERENCES worlds,
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  started_at TEXT, finished_at TEXT,
  UNIQUE (experiment_id, variant_id, replicate, model_family)
);

CREATE TABLE llm_calls (                           -- 录制
  call_key   TEXT PRIMARY KEY,                     -- 见 docs/03 §7.1
  run_id     TEXT, purpose TEXT NOT NULL,          -- persona | probe | coding | commit | factory
  model_id   TEXT NOT NULL, params TEXT NOT NULL,
  request    TEXT NOT NULL, response TEXT NOT NULL,
  input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
  latency_ms INTEGER, attempt INTEGER NOT NULL, created_at TEXT NOT NULL
);

CREATE TABLE responses (
  response_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs,
  persona_id TEXT NOT NULL, persona_version INTEGER NOT NULL,
  step INTEGER NOT NULL DEFAULT 0,                 -- 传播模式的步；研讨模式的轮
  exposed INTEGER NOT NULL, channel TEXT, draw REAL,
  call_key TEXT REFERENCES llm_calls,
  valid INTEGER NOT NULL, invalid_reason TEXT,
  payload TEXT,                                    -- 符合 response.schema.json
  UNIQUE (run_id, persona_id, step)
);

CREATE TABLE shares (                              -- 传播模式：谁在哪一步把什么转给了谁
  run_id TEXT NOT NULL, step INTEGER NOT NULL,
  from_id TEXT NOT NULL, to_id TEXT NOT NULL, text TEXT NOT NULL,
  PRIMARY KEY (run_id, step, from_id, to_id)
);

-- ============ 分析层 ============
CREATE TABLE codebooks (
  codebook_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL, field TEXT NOT NULL,
  themes TEXT NOT NULL, hash TEXT NOT NULL, approved_by TEXT
);
CREATE TABLE codings (
  response_id TEXT NOT NULL, field TEXT NOT NULL, theme_id TEXT NOT NULL,
  evidence TEXT NOT NULL, coder_model TEXT NOT NULL,
  PRIMARY KEY (response_id, field, theme_id, coder_model)
);
CREATE TABLE analysis_ledger (                     -- 沿用 v3.0 §5.18
  idempotency_key TEXT PRIMARY KEY,                -- sha256(experiment_id + responses_hash + module + module_version)
  module TEXT NOT NULL, module_version TEXT NOT NULL,
  result TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE calibration (                         -- 沿用 v3.0 §11.4
  prediction_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
  claim TEXT NOT NULL, expected TEXT NOT NULL, registered_at TEXT NOT NULL,
  observed_at TEXT, actual TEXT, verdict TEXT CHECK (verdict IN ('hit','miss','false_alarm')),
  possibly_seen_by_model INTEGER, note TEXT
);
```

研讨模式的发言、命题、投票等表沿用 v3.0 第 5 章与 §15.2 的数据结构，以 `run_id` 关联，此处不重复。

### 1.1 记忆可见性查询

```sql
-- 人物 :p 在世界 :w（父世界 :parent，分叉点 :fork）中可见的记忆
SELECT * FROM memory_items
WHERE persona_id = :p
  AND ( (world_id = :parent AND seq <= :fork) OR world_id = :w )
  AND memory_id NOT IN (SELECT supersedes FROM memory_items WHERE supersedes IS NOT NULL
                        AND persona_id = :p AND ((world_id = :parent AND seq <= :fork) OR world_id = :w))
  AND occurred_on <= :as_of_date;
```

最后一个条件保证回测时人物想不起「未来」的事。

### 1.2 不变量

写成数据库约束或启动时的自检，违反即报错：

1. `persona_versions`、`memory_items`、`llm_calls` 不执行 UPDATE 与 DELETE。
2. `kind='base'` 的世界里，记忆只能由 `commit`、`world-event`、`experience` 三个命令写入。
3. 封存的世界不可再写。
4. 一个实验内，同一 `persona_id` 只对应一个 `version`。
5. `responses` 中每个（实验, 版本, 重复, 模型族, 人物, 步）恰有一行，包括未曝光与无效的。

## 2. 目录结构

```text
mirosim/
├── README.md
├── docs/                    设计文档（本目录）
├── schemas/                 数据契约
├── config/                  映射表、量表、渠道、模型档位
├── prompts/                 提示词模板
├── examples/                样例人物、刺激物、人群框架、实验
├── tools/                   校验脚本与行为参数参考实现
│
│   —— 以下为实施时新增 ——
├── pyproject.toml
├── src/mirosim/
│   ├── core/
│   │   ├── rng.py           u01 及派生
│   │   ├── store.py         数据库访问；不变量自检
│   │   ├── gateway.py       模型网关：档位、并发、重试、录制、回放
│   │   ├── manifest.py      运行清单
│   │   └── ledger.py        分析台账
│   ├── persona/
│   │   ├── models.py        Pydantic 模型
│   │   ├── behavior.py      行为参数（由 tools/derive_behavior.py 移入）
│   │   ├── render.py        人物卡 → 系统提示词
│   │   ├── factory.py       骨架抽样 + 模型写人
│   │   ├── probe.py         探针与品质指标
│   │   ├── memory.py        检索、提交、反思
│   │   └── worlds.py        分支、封存
│   ├── experiment/
│   │   ├── config.py        载入与交叉校验（由 tools/validate_examples.py 移入）
│   │   ├── sampling.py      人群抽样与权重
│   │   ├── exposure.py      曝光
│   │   └── matrix.py        运行矩阵、预算
│   ├── engines/
│   │   ├── panel.py
│   │   ├── deliberation.py  接 v3.0 的轮次与剧本
│   │   └── diffusion.py
│   ├── analysis/
│   │   ├── aggregate.py     汇总与加权
│   │   ├── contrast.py      配对差、自助法、噪声底
│   │   ├── coding.py        主题编码
│   │   ├── stability.py     重跑稳定性
│   │   ├── qc.py            十二项失真指标与闸门
│   │   └── calibration.py
│   ├── report/              模板与生成器；按效度等级裁剪
│   └── cli.py
├── tests/
│   ├── fixtures/recording/  一份固定的调用录制，用于回放测试
│   └── ...
└── data/                    数据库与运行产物（不入版本库）
```

## 3. 命令行

```text
# 人物库
mirosim persona generate --frame <frame.yaml> [--n-per-segment N]    按人群框架生成并质检入库
mirosim persona probe    [--persona p_xxx | --all] [--model-family F]  跑探针，更新品质指标
mirosim persona show     p_xxx [--version N] [--world W]              查看人物卡、记忆、态度
mirosim persona revise   p_xxx --patch <patch.json> --note "..."      出新版本
mirosim persona list     [--segment S] [--mbti T] [--status active]

# 世界与记忆
mirosim world event      --file <event.yaml>                          向一类人物写入世界事件
mirosim world experience --persona p_xxx --file <exp.yaml>            写入个人经历
mirosim commit           --experiment X --variant V --occurred-on D   把现实中发生的版本提交进基准世界
                         [--outcome <text|file>] [--dry-run]

# 实验
mirosim experiment validate <experiment.yaml>                         校验配置，估算调用量与成本
mirosim experiment run      <experiment.yaml> [--yes] [--resume]      抽样、锁定、执行；可断点续跑
mirosim experiment qc       X                                         质检报告
mirosim experiment analyze  X                                         汇总、比较、编码、稳定性
mirosim experiment report   X [--format md|html]                      生成报告
mirosim experiment replay   X [--verify]                              只用录制重算；--verify 时与原结果逐字节比对
mirosim experiment funnel   X --to deliberation --variants A,B        按规则从群体反应结果中挑人，生成研讨实验配置

# 校准
mirosim calibrate register  X --file <predictions.yaml>               预登记预测
mirosim calibrate observe   <prediction_id> --actual ... --verdict ...
mirosim calibrate compare   X --survey <real.csv>                     与真实问卷对照，生成修正表
mirosim calibrate status    [--pack P]                                各场景包 × 人群的效度等级与命中账本
```

`run` 的断点续跑靠 `responses` 表的唯一约束：已有的行不重做。

## 4. 模型网关

```python
class Gateway:
    async def call(self, *, tier: str, system: str, user: str,
                   purpose: str, run_id: str | None, ordinal: int = 0,
                   json_schema: dict | None = None) -> CallResult: ...
```

| 职责 | 做法 |
|---|---|
| 档位解析 | 按 `config/model_tiers.yaml` 把档位解析为具体模型；一个实验内同档位固定同一模型 |
| 录制 | 每次调用按 `call_key` 写入 `llm_calls`；写入成功后才把结果交给调用方 |
| 回放 | 回放模式下只查表；查不到即报错，不发请求 |
| 并发与限流 | 每个模型一个信号量；遇到限流按 2、4、8 秒退避，最多 3 次（沿用 v3.0 §9.4） |
| 降级 | 主模型连续失败 3 次则切换到同档位的备用模型，至多降一级；降级写入清单，该实验标记为「含降级」 |
| 结构化输出 | 模型支持 JSON 模式则启用；不支持则从输出中提取第一个完整的 JSON 对象 |
| 缓存 | 系统提示词在一个实验内对同一人物不变，置于请求最前，便于供应方的前缀缓存 |
| 计量 | 记录输入、输出、缓存命中的字数，汇总进清单的 `usage` |

网关只依赖「OpenAI 兼容的对话接口」这一约定，不依赖某一家供应方。

## 5. 接口服务（第 4 期）

命令行稳定后，按同样的操作包一层 HTTP 接口，供前端与外部系统使用：

| 方法与路径 | 作用 |
|---|---|
| `POST /experiments` | 提交实验配置，返回校验结果与成本估算 |
| `POST /experiments/{id}/approve` | 人工确认后开始执行 |
| `GET /experiments/{id}` | 状态与进度 |
| `GET /experiments/{id}/report` | 报告 |
| `GET /experiments/{id}/responses?variant=&segment=&theme=` | 反应明细，支持从报告里的每个数字下钻到原话 |
| `GET /personas?segment=&mbti=` | 人物库检索 |
| `GET /personas/{id}?world=` | 人物卡、记忆、态度、参与过的实验 |
| `POST /commits` | 提交现实版本 |

所有返回体带 `notice` 字段；导出的文件带合成标记水印（沿用 v3.0 §12）。

## 6. 成本估算

实验启动前给出估算，公式（每次人物调用）：

```text
输入 ≈ 系统提示词 + 记忆与背景 + 刺激物 + 输出格式说明
输出 ≈ 结构化反应
总量 = 人数 × 版本数 × 重复数 × 模型族数 × (1 + 重试率)
```

各项的实际字数取决于人物卡长度、所选模型的分词方式与供应方计价，本文档不给出数字。
第 0 期要做的一件事就是用 20 个人物跑一次小实验，实测每次调用的输入输出量与单价，填入 `config/model_tiers.yaml` 的 `price_table`，此后的估算以实测为准。

降低成本的手段，按优先顺序：系统提示词前缀缓存；群体反应模式用最便宜的档位；先用 100 人做探索、确认有差别再扩到全量；版本数控制在 5 个以内。
