# Trip Agents 后端

Python 3.12+，使用 FastAPI、SQLAlchemy、Pydantic Settings、Loguru，已实现注册、登录、令牌校验及退出撤销，以及基于 LangGraph + OR-Tools 的单城市多轮景点行程规划 Agent（高德地图、DeepSeek，可选博查搜索）。

## 本地运行

以下命令在后端项目根目录执行：

```powershell
uv sync --locked
# 仅在 .env 不存在时复制；初始化已生成一份本地 .env。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 编辑 .env，配置 MySQL 和 AUTH_JWT_SECRET；行程对话需要 AMAP_API_KEY 和 DEEPSEEK_API_KEY，BOCHA_API_KEY 可选。
# 首次部署执行 scripts/trip_agents.sql 建表。
uv run --no-sync python main.py
```

默认监听 `http://127.0.0.1:8000`，接口文档位于 `/docs`。开发自动重载需要同时设置 `APP_ENV=development` 和 `APP_DEBUG=true`。也可以运行 `uv run --no-sync uvicorn main:app`，监听参数使用 Uvicorn CLI 选项。

## 目录职责

```text
app/
  api/          # 路由与请求依赖
  services/     # 业务规则与事务协调
  repository/   # 数据读写
  models/       # 统一 Base、用户与令牌撤销表映射
  schemas/      # 请求、响应和业务传输模型
  core/         # 配置、日志、数据库引擎、认证基础能力及业务异常
  agent/        # 图、状态、节点、边与钩子；trip/ 为行程根图，trip/planning/ 为规划子图与求解器
  prompt/       # 提示词函数
  tools/        # 高德、博查客户端与问答 Agent 工具
tests/
  core/         # 配置、日志与应用生命周期测试
  auth/         # 认证 schema、HTTP 契约、故障及并发测试
  trip/         # 行程规划：求解器、候选集、校验、意图解析、图与 HTTP 契约测试
  integration/  # 真实服务集成测试，默认不执行
scripts/        # 已有数据库维护脚本
docs/           # 项目文档
main.py         # FastAPI 应用与生命周期
```

配置与日志的用法见 [配置文档](docs/configuration.md)，接口、安全边界及 MySQL 集成验证见 [认证文档](docs/authentication.md)，行程 Agent 的接口、图结构、候选与规划规则见 [行程 Agent 文档](docs/trip_agent.md)。真实 `.env`、日志和本地产物已加入 Git 忽略规则。

## 验证

```powershell
uv run --no-sync pytest -q
uv run --no-sync python -m compileall -q app main.py tests
```

默认测试使用临时文件 SQLite 和可控输入，无需真实 MySQL、地图或模型服务。真实 MySQL 及真实 DeepSeek + 高德端到端验证需设置 `RUN_TRIP_INTEGRATION=1` 后单独运行。项目尚未配置独立的 lint/类型检查工具；上述 `compileall` 仅验证语法。
