# Analytics Copilot（项目四 · M0 骨架）

面向业务分析场景的数据分析 Agent：用户用自然语言提业务问题，Agent 给出
**结论 + 证据链**；证据不足时拒答或降级，而不是硬下结论。

一句话定位：**不是把中文翻译成 SQL，而是"给出结论、附上证据、证不住就说不确定"**。

## 现在能做什么（M1）

- 把 UCI Online Retail 建成**只读** DuckDB 分析库（白名单视图 + 口径元数据）；
- **受控 LLM 规划**：LLM 只出计划与结论，SQL 一律过 M0 护栏；失败只允许一次修复重试；
  无 key 时退回规则路径，链路照样可测；
- 分析链路：状态谓词（执行前检查）→ 澄清（最多 2 轮，超预算带显式假设）→ 计划 →
  只读查询 → 结果检查 → 结论 + 证据链 + 不确定性标注；
- 能力边界：预测 / 因果 / 无成本字段的问题**直接拒答并说明原因**；
- HTTP 服务骨架：`/ask`、`/healthz`、`/metrics`，带会话隔离、调用日志、
  成本记账点、请求超时与结构化错误码；
- 评测：`eval/tasks/m1_tasks.json`（12 正常 + 3 澄清 + 3 拒答），runner 会模拟澄清对话，
  输出完成率、追溯链完整率与**验收项自查**。

## 怎么跑

```bash
# 1 依赖（Python 3.10）
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt

# 2 建库（需要 data/raw/Online Retail.xlsx；数据文件不入库）
python scripts/build_db.py          # 首次会把 xlsx 转成 CSV（约 1 分钟），之后重建约 2 秒
python scripts/build_db.py --check  # 核对库与元数据

# 3 跑测试与静态检查
python -m pytest tests -q
python -m ruff check src tests scripts

# 4 起服务（本地；M0 不部署公网）
PYTHONPATH=src python -m uvicorn analytics_copilot.service:app --port 8200

# 5 评测骨架冒烟
PYTHONPATH=src python -m analytics_copilot.eval.runner \
  --tasks eval/tasks/dev_tasks_v0.json --out eval/runs/smoke
```

## 架构（M0）

```
用户问题
  → agent.understand   状态谓词：指标/时间窗/维度/对比基准 缺不缺
  → 澄清（≤2 轮，超预算带显式假设继续）
  → agent.plan         把理解结果变成只读查询
  → tools.sql_tool     白名单 + 单语句 + 行数上限；附机械检查
  → 结论 + 证据链（主张 → 查询 → 结果行）+ 不确定性标注
```

| 模块 | 职责 |
|---|---|
| `db.py` | 只读连接、对象白名单、单语句检查、行数上限 |
| `tools.py` | 元数据工具、SQL 工具、贡献度分解；fan-out 与占比合计两道机械检查 |
| `agent.py` | 任务理解（谓词）+ 计划 + 结论与证据链 |
| `service.py` | HTTP 边界、会话隔离、日志、成本点、超时、错误码 |
| `eval/` | 任务集格式 + runner（M0 只算能机械算的两项） |

## 边界（不做）

不做通用 BI 与看板、不做企业数据接入、不做多数据源、不做权限体系、
不做模型训练、不做预测与因果推断、不做自动决策、**不做多 Agent 架构**。
完整清单见《项目四立项决策稿 v1》。

## 已知限制（M0 如实标注）

- 会话状态在**单进程内存**里，多实例部署会失效（M5 若多实例需换存储）；
- 请求超时是 `asyncio` 级，DuckDB 查询本身**不会真正中断**，
  因此靠白名单 + 行数上限 + 视图预聚合控制扫描面；
- 结论支持率、误判率、边界降级正确率**尚未实现**（需先冻结判定规则，M3）；
- 构造任务集标注为「构造人 = 作者」，**未做用户访谈**。

## 目录

```
scripts/    build_db（建库与核对）
src/analytics_copilot/  db · tools · agent · service · eval/
eval/       tasks（任务集）· runs（跑批产出）
data/       原始数据与 DuckDB（不入库）· metadata.json（口径，入库）
docs/       M0 记录（技术选择、替代方案、踩坑、面试追问）
tests/      db 护栏 · 工具检查 · 链路 · 服务层
```

## 许可

MIT。
