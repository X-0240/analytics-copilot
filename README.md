# Analytics Copilot —— 面向业务分析场景的受控数据分析 Agent

一个基于 LLM 规划、只读数据查询、证据验证与能力边界控制的数据分析 Agent Demo：
用户用自然语言提业务问题，系统给出**结论 + 证据链**；证据不足、口径不匹配或超出数据能力时，
明确说明而不是硬答。

一句话定位：**不是把中文翻译成 SQL，而是"给出结论、附上证据、证不住就说不确定"**。

## 核心设计

### 1. 受控规划

LLM 只负责"提出计划与结论"，能不能执行由护栏决定：SQL 一律过白名单 / 单语句 / 行数上限检查，
失败只允许一次修复重试；无 key 时退回确定性规则路径，链路照样可测。
规划只做一次：模型给出计划后按计划执行、再基于执行结果写结论，不根据中间结果回头改写计划
（计划本身不合法时才允许一次修复重试）。

### 2. 验证层（机械可判）

逐条检查结论：数字能否追溯到声明的来源步骤（V1）、点名的维度值是否出现在结果里（V2）、
证据够不够支撑归因类结论（V3）、结论有没有超出证据允许的解释等级（V4）。
证不住的结论会降级或整题不输出强结论。规则版本化：
v1 → v1.1（解释强度）→ v1.2（实体编码 / 单位换算）→ v1.3（数值书写归一）。

### 3. 解释强度与能力边界

结论只能说到证据支持的层级——Level 1 事实、Level 2 相关性（强制附
「该结果展示相关变化模式，不能证明因果关系」），**Level 3 因果结论禁止输出**。
数据能分析什么、不能分析什么由 `GET /capabilities` 的能力卡给出（事实部分按只读视图的实际列派生），
入口展示并作为拒答依据；**能力检查先于澄清**，避免问「利润」时被反问「看哪个指标」。

### 4. 时间窗口径同源

用户说「最近三个月」时，解析结果（锚点 = 数据集最后一天、起止日期、末月是否完整）
是唯一事实来源：SQL 过滤、答案口径、caveat 都读它，避免口径漂移。
只有"明确的 N 个月/周/天"才生效，只有「最近」两个字时不猜。

## 现在能做什么（M0–M6）

- 把 UCI Online Retail 建成**只读** DuckDB 分析库（白名单视图 + 口径元数据）；
- HTTP 服务：`POST /ask`（结论 + 证据链）、`GET /capabilities`（数据能力卡）、
  `GET /healthz`（真查一次库的就绪检查）、`GET /metrics`（成本、延迟分位与失败计数）、
  `GET /`（演示页）；带会话隔离、日志、成本记账点、超时与结构化错误码；
- 评测：`eval/tasks/m1_tasks.json`（12 正常 + 3 澄清 + 3 拒答）与 `m3_boundary_tasks.json`（4 题边界），
  runner 会模拟澄清对话，输出完成率、追溯链完整率与**验收项自查**。
- 交付形态：Docker 单实例（见下）与本地直跑两条路径都能起服务。

## 评测结果（validator v1.3，2026-10-12）

| 口径 | 结果 |
|---|---|
| 主集 18 题（真实模型） | 作答 15 / 拒答 2 / 做不到 1 / **错误 0**；53 条结论支持 48 条（支持率 **0.9057**）；验收自查 11/12 + 6/6；成本 **0.089078 元** |
| 边界 4 题 | 全部作答；18 条结论支持 17 条（**0.9444**） |
| holdout clean（58 例，生成规则标注） | 漏放 **0.0** / 误拦 **0.0** |
| holdout hard（8 例，业务意图标注） | 漏放 **0.0** / 误拦 **0.2857** |

口径提醒：**支持率是机械口径**（数字能否追溯到查询结果、维度值是否在结果里），
不等于业务上正确，也不能跨版本直接比；holdout 由生成器按规范格式生成，
测不到真实书写的多样性，评测要"冻结集 + 真实输出抽样"两条腿。

全部数字、失败案例与方法论见 [收口报告](docs/M6_收口报告.md) 与
[回归报告 v1.3](docs/回归报告_v1.3.md)。

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

# 4 起服务（本地直跑；容器方式见下一节）
PYTHONPATH=src python -m uvicorn analytics_copilot.service:app --port 8200

# 5 评测骨架冒烟
PYTHONPATH=src python -m analytics_copilot.eval.runner \
  --tasks eval/tasks/dev_tasks_v0.json --out eval/runs/smoke
```

## 部署（Docker 单实例）

```bash
docker build -t analytics-copilot:m5-1 .
docker run -d --name ac-m5 -p 8200:8200 \
  -v "<仓库路径>/data:/app/data:ro" \
  -v "<仓库路径>/logs:/app/logs" \
  -e AC_LLM_API_KEY -e AC_FORCE_STUB=0 -e AC_DAILY_COST_BUDGET_CNY=1 \
  analytics-copilot:m5-1
```

- 打开 `http://localhost:8200/` 是演示页；`/healthz` 是就绪检查，`/metrics` 是指标；
- **数据库不打进镜像**：7 MB 的 `data/analytics.duckdb` 走只读卷，换数据不用重建镜像；
- **必须单 worker**：会话在进程内存里，多 worker 会把同一会话分叉；
- key 只经宿主环境变量按名传入（`-e AC_LLM_API_KEY`），不出现在命令行、镜像与日志里；
  键名清单见 `.env.example`，参数含义与验收项见 `docs/M5_部署方案.md` 与 `docs/M5_记录.md`。
- 实测：镜像 289 MB、冷启动到 `/healthz` 健康 1683 ms；公网用 Cloudflare 隧道 + 手机 4G 实测过两次
  （隧道是临时地址）。这一步的定位是**可复现实验环境与访问链路验证**，不是生产部署。

## 架构

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
| `verifier.py` | 结论验证层：V1 数值一致性 · V2 证据覆盖 · V3 结果充分性 · V4 解释强度越界 |
| `explain.py` | 解释强度模型（Level 1/2/3）与因果措辞的机械判据 |
| `service.py` | HTTP 边界、会话隔离、公网防护、日志、成本点、超时、错误码 |
| `eval/` | 任务集格式 + runner（完成率、追溯链、支持率、等级分布、成本） |

## 不做（范围外）

不做通用 BI 与看板、不做企业数据接入、不做多数据源、不做权限体系、
不做模型训练、不做预测与因果推断、不做自动决策、**不做多 Agent 架构**。
完整清单见《项目四立项决策稿 v1》。

## 已知限制

1. **语义覆盖有限**：国家中英别名表只覆盖 12 个常用国家，表外国家的中文表述会被判不支持；
   书写变体是枚举式覆盖（千分位 / 单位 / 裸月份 / 步骤引用已归一，其它变体仍可能误拦）。
2. **任务集小且由作者构造**（18 + 4 题），未做用户访谈；结论都是方向性证据；
   上游模型有波动，同一问题不同轮次可能拒答或失败。
3. **单实例形态**：会话与指标在进程内存里，重启归零；请求超时是 `asyncio` 级，
   DuckDB 查询本身不会真正中断，靠白名单 + 行数上限控制扫描面。

完整清单、失败案例与方法论见 [收口报告](docs/M6_收口报告.md) 与
[回归报告 v1.3](docs/回归报告_v1.3.md)。

## 目录

```
scripts/    build_db（建库与核对）
src/analytics_copilot/  db · tools · agent · service · eval/
eval/       tasks（任务集）· runs（跑批产出）
data/       原始数据与 DuckDB（不入库）· metadata.json（口径，入库）
docs/       M0–M6 记录 · 验证标准 v1.3（现行）· 回归报告 · 收口报告 · 面试决策卡
tests/      db 护栏 · 工具检查 · 链路 · 服务层
```

## 许可

MIT。
