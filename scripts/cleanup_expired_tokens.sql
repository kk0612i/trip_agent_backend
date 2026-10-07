-- 在已选择的业务数据库中由维护人员按需执行，每次最多删除 1000 条。
-- DATETIME 按 UTC 保存，使用 UTC_TIMESTAMP() 比较，不依赖维护连接的时区。
-- 已过期令牌会先被 JWT exp 校验拒绝，清理记录不会恢复其有效性。
DELETE FROM revoked_tokens
WHERE expires_at <= UTC_TIMESTAMP()
ORDER BY expires_at
LIMIT 1000;
