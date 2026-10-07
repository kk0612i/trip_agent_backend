# Trip Agents 后端

Python 3.12+，使用 FastAPI、Pydantic Settings、Loguru，预留旅行 Agent 的分层目录。

## 本地运行

以下命令在后端项目根目录执行：

```powershell
uv sync --locked
# 仅在 .env 不存在时复制；初始化已生成一份本地 .env。
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# 编辑 .env，填入高德、DeepSeek 密钥和 MySQL 密码。
uv run --no-sync python main.py
```

默认监听 `http://127.0.0.1:8000`，接口文档位于 `/docs`。开发自动重载需要同时设置 `APP_ENV=development` 和 `APP_DEBUG=true`。也可以运行 `uv run --no-sync uvicorn main:app`，监听参数使用 Uvicorn CLI 选项。

## 目录职责

```text
app/
  api/          # 路由与请求依赖
  services/     # 业务规则与事务协调
  repository/   # 数据读写
  models/       # ORM 模型及未来的统一 Base
  schemas/      # 请求、响应和业务传输模型
  core/         # config.py 配置、log.py 日志
  agent/        # 图、状态、节点、边与钩子
  prompt/       # 提示词函数
  tools/        # Agent 工具适配
tests/
  core/         # 配置、日志与应用生命周期测试
  integration/  # 真实服务集成测试，默认不执行
scripts/        # 已有数据库维护脚本
docs/           # 项目文档
main.py         # FastAPI 应用与生命周期
```

配置与日志的用法见 [配置文档](docs/configuration.md)。真实 `.env`、日志和本地产物已加入 Git 忽略规则。

## 验证

```powershell
uv run --no-sync pytest -q
uv run --no-sync python -m compileall -q app main.py tests
```

测试使用临时文件和可控输入，无需真实 MySQL、地图或模型服务。项目尚未配置独立的 lint/类型检查工具；上述 `compileall` 仅验证语法。
