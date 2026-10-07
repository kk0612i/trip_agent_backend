-- 适用于 MySQL 5.7，执行前请选择目标数据库。
-- 邮箱在应用层统一校验格式、去除首尾空白、规范化并转为小写。
-- 原始密码长度为 6～128 个字符，仅保存 Argon2id 编码后的哈希。
-- 数据库连接时区统一设为 UTC，created_at 按 UTC 保存。

CREATE TABLE users (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '用户唯一标识',
    email VARCHAR(254) COLLATE utf8mb4_bin NOT NULL COMMENT '注册及登录邮箱',
    password_hash VARCHAR(255) COLLATE utf8mb4_bin NOT NULL COMMENT '密码哈希',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',

    PRIMARY KEY (id),
    UNIQUE KEY uk_users_email (email)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '用户账号';

-- jti 由服务端签发令牌时生成，长度不超过 64 个字符。
-- expires_at 取自已验证令牌的 exp，转换为 UTC 时间后保存。
-- 退出登录提交撤销记录后返回成功，过期记录可按 expires_at 清理。
CREATE TABLE revoked_tokens (
    jti VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '令牌唯一标识',
    expires_at DATETIME NOT NULL COMMENT '令牌到期时间',

    PRIMARY KEY (jti),
    KEY idx_revoked_tokens_expires_at (expires_at)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '已撤销令牌';
