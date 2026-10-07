# 账户认证

认证使用现有 `users`、`revoked_tokens` 表，不在应用启动时建表或迁移。密码保存为 Argon2id 哈希；访问令牌使用服务端固定的 HS256 算法。

## 配置和启动

在项目根目录执行：

```powershell
uv sync --locked
uv run --no-sync python main.py
```

`.env` 必须配置 `AUTH_JWT_SECRET`，长度至少 32 字节，建议使用 `secrets.token_urlsafe(48)` 生成随机密钥。缺失或过短时应用启动失败。当前本地配置已补充随机密钥，真实 `.env` 由 Git 忽略。多个 worker 和实例应使用同一签名密钥；更换密钥后原有令牌将无法通过签名校验。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `AUTH_JWT_SECRET` | 空，必须配置 | 签名密钥 |
| `AUTH_JWT_ISSUER` | `trip-agents` | 签发方 |
| `AUTH_JWT_AUDIENCE` | `trip-agents-api` | 接收方 |
| `AUTH_TOKEN_TTL_SECONDS` | `1800` | 有效秒数，范围 1～86400 |

MySQL 使用已有的 `MYSQL_*` 配置。连接设置 `time_zone = '+00:00'`，写入的 `DATETIME` 为无时区的 UTC 时间；公开响应明确附加 UTC 时区并序列化为 ISO 8601，例如 `2026-10-07T12:00:00Z`。

## HTTP 契约

| 方法与路径 | 请求 | 成功响应 | 主要错误 |
| --- | --- | --- | --- |
| `POST /api/auth/register` | JSON：`email`、`password` | `201`：`id`、`email`、`created_at` | `409 EMAIL_ALREADY_EXISTS`、`422 VALIDATION_ERROR` |
| `POST /api/auth/login` | JSON：`email`、`password` | `200`：`access_token`、`token_type`、`expires_in` | `401 INVALID_CREDENTIALS`、`422 VALIDATION_ERROR` |
| `POST /api/auth/logout` | `Authorization: Bearer <token>` | `204`，空响应 | `401 INVALID_TOKEN` |
| `GET /api/auth/token` | `Authorization: Bearer <token>` | `200`：`user_id`、`expires_at` | `401 INVALID_TOKEN` |

邮箱去除首尾空白并统一为小写，密码保留原样，长度为 6～128 个字符。客户端额外提交身份字段会被拒绝。用户公开响应不包含密码和密码哈希。

错误响应采用以下格式，普通业务错误的 `errors` 为空列表：

```json
{
  "code": "INVALID_CREDENTIALS",
  "message": "邮箱或密码错误",
  "errors": []
}
```

所有 `401` 带 `WWW-Authenticate: Bearer`，登录响应统一设置 `Cache-Control: no-store`。`422` 由统一处理器投影为安全的 `field`、`message`，不返回原始输入、异常上下文、任意额外字段名或原始校验消息。意外数据库或内部错误返回 `500 INTERNAL_ERROR` 和固定提示。

## 身份、事务与撤销

业务层拥有最外层事务，Repository 只查询、写入和 flush。注册先检查邮箱，数据库 `uk_users_email` 保证并发唯一性；该键产生的冲突在回滚后统一映射为 `409`，其他完整性错误保留为内部失败。密码哈希和验证在线程内执行，置于数据库事务之外。

`get_current_user` 验证签名、固定算法、`iss`、`aud`、`iat`、`exp`，要求 `sub`、`jti` 等声明存在，并检查声明的严格类型和范围。每次认证都查询用户存在性与撤销记录，身份只取自验证后的 `sub`。

后续受保护路由复用 `app.api.deps.CurrentUserDep`，将其返回的 `AuthenticatedUser` 作为可信身份传给 Service；需要资源归属检查时仍由业务 Service 执行。

退出时保存当前 `jti` 和 UTC 到期时间，提交成功才返回 `204`。同一用户其他登录令牌保持有效，已撤销令牌再次认证返回 `401`。并发退出的撤销主键冲突也返回 `401`。维护时可按需执行 `scripts/cleanup_expired_tokens.sql`，每次清理至多 1000 条过期记录。

新增的 `AccessTokenClaims` 集中补充 JWT 库未严格限制的声明类型、取值及时间顺序；`AuthenticatedUser` 承载完整认证后的内部身份。现有公开请求和响应 schema 保持接口契约。

## 验证

```powershell
uv run --no-sync pytest -q
uv run --no-sync python -m compileall -q app main.py tests
```

默认测试使用临时文件 SQLite，测试表拒绝非 Argon2id 密码，数据库边界故障注入覆盖唯一冲突、提交失败和回滚；并发请求分别创建 Session。实际日志验证涵盖成功、拒绝、意外失败、脱敏、级别、关联标识和重复初始化。

真实 MySQL 集成测试单独运行，需要连接用户有创建临时表的权限。测试根据现有 DDL 创建连接内同名临时表，所有请求顺序使用该连接；测试数据随连接关闭释放。

```powershell
$env:RUN_MYSQL_AUTH_INTEGRATION = '1'
uv run --no-sync pytest tests/integration/auth/test_auth_mysql.py -o addopts='' -q
Remove-Item Env:RUN_MYSQL_AUTH_INTEGRATION
```

项目尚未配置独立 lint 或类型检查工具；`compileall` 仅验证语法。不会运行自动清理或后台重试。
