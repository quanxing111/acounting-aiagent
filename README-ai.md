# 轻记账 AI 化：说明与运行

在原有单文件记账网页上加了「AI 对话」，说一句「今天打车 32 块」就能自动分类落库，
也能问「这个月花了多少」「哪类花得最多」。后端 FastAPI + Agents SDK，输出是流式的（SSE）。

## 一、目录结构

```
index.html            原来的记账页（新增：右下角 AI 标签页 + 对话逻辑 + 后端同步）
app/db.py             SQLite 存储层：建表、增删改查、区间统计、分类表
app/agent.py          Agents SDK 层：模型配置、6 个工具、中文提示词
app/main.py           FastAPI：静态页面 + 记录接口 + /api/chat 流式接口
tests/test_ai_flow.py 离线自检（假模型跑通「说话→工具→落库→SSE」）
run.ps1               一键启动
.env.example         模型配置模板
data/accounting.db    账本数据库（首次启动自动创建）
```

## 二、三步跑起来

```powershell
# 1. 装依赖（condaenv1 里已经装好了）
.\condaenv1\Scripts\pip.exe install -r requirements.txt

# 2. 配置模型
Copy-Item .env.example .env
# 打开 .env，填 OPENAI_API_KEY；国内建议直接填 BASE_URL + KEY + MODEL 三行

# 3. 启动
.\run.ps1
```

浏览器打开 http://127.0.0.1:8000 ，切到 🤖 AI 标签页，输入「今天打车 32 块」。

## 三、一句话是怎么变成一笔账的

```
前端对话框  --POST /api/chat {message, history}-->  FastAPI
                                                     |
                                        Agents SDK 组装提示词（含今天日期 + 你的分类表）
                                                     |
                                       模型输出 tool_call: add_records
                                                     |
                        工具校验分类 -> 写 SQLite -> 返回 JSON
                                                     |
      前端 <--SSE-- text 增量 / tool_start / tool_done / records_changed / done
       |
       └─ 收到 records_changed 重新 GET /api/records，明细和统计页立刻是新数据
```

SSE 事件类型（前端按这个解析，改后端时别改字段名）：

| type | 含义 | 附带字段 |
| --- | --- | --- |
| `text` | 模型回复的增量文本 | `delta` |
| `tool_start` | 开始调用某个工具 | `tool`、`args` |
| `tool_done` | 工具返回 | `tool`、`summary`（人话摘要） |
| `records_changed` | 数据被改过，前端该刷新了 | — |
| `done` | 本轮结束 | `reply` 完整回复 |
| `error` | 出错 | `message` |

## 四、Agent 有哪些工具

| 工具 | 作用 |
| --- | --- |
| `add_records` | 新增一笔或多笔（一句话多笔一次写入） |
| `query_records` | 按日期区间 / 收支 / 大类 / 关键词查明细 |
| `stats_by_category` | 分类汇总：总额、笔数、占比、平均单笔（带小类下钻数据） |
| `stats_by_day` | 按天或按月汇总，用于「这周每天花了多少」 |
| `update_record` | 改已有记录（说「刚才那笔记错了」时用） |
| `delete_record` | 删记录（会先查再确认） |

模型只能调这些工具，数字全部来自数据库，不存在「编造统计结果」的路径。

## 五、分类表怎么维护

分类只认 `index.html` 里的 `CATS`。页面每次启动会把它 `PUT /api/schema` 推给后端，
Agent 的提示词按最新的分类表生成，所以你**只改前端 CATS 就够了**，不用动 Python。

归类规则写在 `app/agent.py` 的 `build_instructions()` 里，想改口吻、加映射（比如
「打车算通勤」，或者把「咖啡」固定到餐饮·奶茶），改那一段中文提示词即可。

## 六、接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/chat` | 流式对话（SSE），body：`{message, history}` |
| GET | `/api/records` | 查记录，支持 `start` `end` `type` `main` `keyword` |
| POST | `/api/records` | 新增单条（前端手动记账走的还是本地，这条给外部用） |
| PUT | `/api/records` | 前端整表同步，`replace=true` 表示以本地为准 |
| DELETE | `/api/records/{id}` | 删一条 |
| GET/PUT | `/api/schema` | 读取 / 更新分类表 |
| GET | `/api/health` | 自检：模型、Key 是否配好、记录数 |

## 七、怎么验证没写坏

```powershell
.\condaenv1\python.exe tests\test_ai_flow.py
```

用 SDK 自带的 `ScriptedModel` 假扮模型，离线跑通整条链路，不花 token、不需要 Key，
并且用的是临时数据库，不会碰你的真实账本。

## 八、常见问题

- **对话框提示 `Missing credentials`**：`.env` 没配好，或没重启服务。`GET /api/health` 看 `has_api_key`。
- **端口被占用**：`.env` 里改 `ACCOUNTING_PORT`，前端 `API_BASE` 也跟着改。
- **直接双击 index.html**：能用旧功能，AI 会连 `http://127.0.0.1:8000`，需要后端在跑（有 CORS 放行）。
- **原来的本地数据**：第一次启动会把浏览器 localStorage 里的旧记录整表灌进数据库，之后以数据库为准。
- **换模型**：只改 `.env` 三行，代码不用动。
## 界面运行演示：


https://github.com/user-attachments/assets/23af89f8-b435-497f-bda6-25b79bc92f2c


