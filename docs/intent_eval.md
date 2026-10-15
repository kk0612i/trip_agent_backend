# 意图理解评估

评估行程 Agent 根图中 `understand` 节点把一条用户消息解析为 `TurnIntent` 的准确性。运行器直接调用生产环境的 `understand_node`（含规则快速路径），使用与 `main.py` 相同的模型配置（`temperature=0`），所有判分由代码完成，不使用 LLM 裁判。

## 目录

```text
evals/
  intent/
    scenarios.py   # 冻结的会话场景：今天 2026-10-15（周四）、杭州三日草稿、待回答问题等
    dataset.py     # 用例结构、标注约定、加载与一致性校验
    grading.py     # 逐字段判分
    runner.py      # 并发调用 understand_node，记录输出、耗时、token
    report.py      # 指标汇总、Markdown 报告、两次运行对比
    __main__.py    # 命令行
    cases/*.yaml   # 评估集，按类别分文件
  results/         # 运行结果（已加入 Git 忽略）
tests/evals/       # 框架自身的单测（假模型，默认 pytest 执行）
```

## 命令

```powershell
# 校验评估集（不调用模型），修改用例或场景后先跑这个
uv run --no-sync python -m evals.intent validate

# 全量运行；--repeat 3 时额外统计一致性和 pass^k
uv run --no-sync python -m evals.intent run --repeat 3

# 只跑部分用例
uv run --no-sync python -m evals.intent run --category modify_selector --tag hard --case pending-003 --limit 20

# 换模型对比
uv run --no-sync python -m evals.intent run --model deepseek-xxx

# 发现标注错误、修正 YAML 后，用已有模型输出重新判分（不再调用模型）
uv run --no-sync python -m evals.intent regrade evals/results/intent-20261015-160000

# 改提示词前后对比
uv run --no-sync python -m evals.intent compare evals/results/<基线> evals/results/<本次>

# CI 门槛：单次通过率低于 85% 时退出码为 2
uv run --no-sync python -m evals.intent run --fail-under 0.85
```

需要在 `.env` 中配置 `DEEPSEEK_API_KEY`。每次运行在 `evals/results/intent-<时间>/` 下生成：

- `results.jsonl`：每次运行的模型输出、耗时、token 和逐字段判分；
- `summary.json`：全部指标，`compare` 使用；
- `report.md`：可读报告，包括总体指标、按类别、字段级、kind 混淆矩阵、按标签和失败用例明细。

报告元信息记录模型、温度、提示词指纹（提示词模块源码 + `TurnIntent` 结构的哈希）和 Git 提交，便于确认对比的是哪两个版本。

## 指标

| 指标 | 含义 |
| --- | --- |
| 单次运行通过率 | 所有判分字段都正确的运行占比，主指标 |
| pass^k / pass@k | 重复 k 次全部通过 / 至少一次通过的用例占比；两者差距大说明输出不稳定 |
| kind 准确率（下游等价 / 严格）、混淆矩阵 | 路由是否正确。“下游等价”把 `IntentResolver` 走同一分支的类型视为相同（plan_request、modify、带城市的 switch_city、不带选项序号的 answer_pending），用于判分；“严格”要求与标注的类型一致，只作参考 |
| 字段“抽对率” | 标注期望有值时，模型是否抽对 |
| 字段“未多填率” | 标注期望为空时，模型是否没有多填（例如沿用旧需求、编造字段） |
| ops 精确率/召回率/F1 | 修改操作级别的匹配，顺序无关 |
| 解析失败率 | 模型输出无法通过 `TurnIntent` 校验的比例，计为失败 |
| 调用错误率 | 网络、超时等异常，不计入准确率分母，单独报告 |
| 延迟、token | 只统计实际调用模型的运行 |

## 标注约定

一条用例：

```yaml
- id: modify_ref-001          # <类别>-<三位序号>
  category: modify_ref        # 见 dataset.CATEGORIES
  scenario: drafting_hz3d     # 见 scenarios.SCENARIOS
  message: 雷峰塔不去了
  recent_turns:               # 可选，覆盖场景默认的最近对话
    - {role: user, content: ...}
  expect:
    kind: modify              # 可写列表表示几种都可接受，第一个为主标注
    ops:
      - {op: remove, item_ids: [D2-1], day: "*"}
  ignore: []                  # 可选，不判分的字段
  tags: [reference]           # 可选，用于筛选和分组统计；困难用例加 hard
  note: 点名景点→item_ids；day 是否顺带填写不影响解析   # 必填
```

- **未写出的字段按“应为空/默认值”判分**，用来发现模型多填。`chat_reply`、`unhandled` 例外：未写不判分；写 `true` 表示应非空，`false` 表示应为空。
- `question`：未写表示应为空；`true` 表示应非空；关键词列表表示应包含全部关键词。
- 标签（偏好、选择器、替换目标）可写 `"a|b"`，表示几种都算对。
- 偏好可写成多组整体备选，按最接近的一组判分，不允许跨组混搭：`preferences: [{mountain: 1, lake_river: 1}, {nature: 1}]`。
- 修改操作字段写 `"*"` 表示不关心，只用于对下游解析没有影响的附带字段。
- 点名景点的 remove/move 拆成多条或合并成一条视为等价（下游结果相同），判分前会先合并。
- 选中景点的操作（remove/replace/move/swap/set_duration）用点名（item_ids）还是选择器（day/part/tags）写，只要用 `select_items` 在场景草稿上选中同一批景点就视为等价。
- 偏好权重：1 喜欢、2 非常喜欢、-1 不喜欢或少安排；同一标签出现多次时与下游一致，以最后一次为准。
- 景点名、城市名宽松匹配：全半角、空白和标点归一化后相等或互相包含即可；城市忽略“市”后缀。
- 标注以 `app/prompt/trip_understand_prompt.py` 中的规范为准；规范本身有歧义时在 `note` 中说明，并考虑同时修改提示词。

`scenarios.py` 中的日期和草稿编号是标注的基准，修改后会使已有标注失效。场景必须与线上真实会出现的状态一致，特别是待回答问题的文字、选项及其携带的需求，要按 `IntentResolver` 和规划诊断节点的生成逻辑构造；虚构的状态会让评估发现不存在的问题。`tests/evals` 会在默认 pytest 中校验整个评估集，场景或 schema 变化导致标注不一致时会立即失败。

## 维护建议

- 线上发现理解错误时，把原消息脱敏后加入评估集，先确认它在当前版本失败，再修改提示词。
- 修改提示词或换模型前后各跑一次 `--repeat 3`，用 `compare` 看变差的用例，不只看总分。
- 失败用例先判断是模型错还是标注错；标注错修正 YAML 后用 `regrade`，不必重新调用模型。
