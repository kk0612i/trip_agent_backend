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

-- ---------------------------------------------------------------------------
-- 行程会话（单城市景点规划）
-- 库中旧分支遗留的 trip_sessions、trip_messages、trip_drafts、trip_versions、
-- trip_confirmations、planning_tasks、attractions 与以下表无关，不要删除或修改。
-- 时间统一按 UTC 保存为不带时区的 DATETIME；JSON 列保存 Pydantic 序列化结果。
-- ---------------------------------------------------------------------------

-- status：collecting（收集需求，可换城市）→ drafting（城市锁定，可修改）→ confirmed（只读）。
-- lock_token/lock_expires_at 为单轮对话的租约锁，过期后可被下一轮接管。
CREATE TABLE IF NOT EXISTS trips (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '行程唯一标识',
    user_id BIGINT UNSIGNED NOT NULL COMMENT '所属用户',
    status VARCHAR(16) NOT NULL COMMENT '行程状态：collecting/drafting/confirmed',
    city_adcode VARCHAR(6) NULL COMMENT '城市行政区划编码',
    city_name VARCHAR(64) NULL COMMENT '城市名称',
    requirements JSON NOT NULL COMMENT '当前需求',
    pending_question JSON NULL COMMENT '待用户回答的问题',
    last_change_summary VARCHAR(1000) NULL COMMENT '上一次草稿变化摘要',
    current_draft_version INT NULL COMMENT '当前草稿版本号',
    confirmed_version INT NULL COMMENT '已确认的版本号',
    confirmed_at DATETIME NULL COMMENT '确认时间',
    lock_token VARCHAR(64) COLLATE utf8mb4_bin NULL COMMENT '当前占用行程的轮次租约令牌',
    lock_expires_at DATETIME NULL COMMENT '租约到期时间',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (id),
    KEY idx_trips_user_id_id (user_id, id),
    CONSTRAINT fk_trips_user_id FOREIGN KEY (user_id) REFERENCES users (id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '行程会话';

-- 草稿版本只追加不更新；同一轮次最多产生一个版本。
CREATE TABLE IF NOT EXISTS trip_plan_versions (
    trip_id BIGINT UNSIGNED NOT NULL COMMENT '所属行程',
    version INT NOT NULL COMMENT '草稿版本号，从 1 开始',
    plan JSON NOT NULL COMMENT '草稿内容',
    requirements JSON NOT NULL COMMENT '生成该草稿时的需求',
    source_turn_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '产生该版本的轮次标识',
    change_summary VARCHAR(1000) NULL COMMENT '相对上一版本的变化摘要',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

    PRIMARY KEY (trip_id, version),
    UNIQUE KEY uk_trip_plan_versions_source_turn (trip_id, source_turn_id),
    CONSTRAINT fk_trip_plan_versions_trip_id FOREIGN KEY (trip_id) REFERENCES trips (id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '行程草稿版本';

-- 每轮对话写入 user 与 assistant 两条记录；唯一键保证同一轮次重试不重复写入。
CREATE TABLE IF NOT EXISTS trip_turns (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '消息唯一标识',
    trip_id BIGINT UNSIGNED NOT NULL COMMENT '所属行程',
    turn_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '客户端生成的轮次标识',
    role VARCHAR(16) NOT NULL COMMENT '发送方：user/assistant',
    content TEXT NOT NULL COMMENT '消息内容',
    draft_version INT NULL COMMENT '该回复产生的草稿版本号',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',

    PRIMARY KEY (id),
    UNIQUE KEY uk_trip_turns_turn_role (trip_id, turn_id, role),
    KEY idx_trip_turns_trip_id_id (trip_id, id),
    CONSTRAINT fk_trip_turns_trip_id FOREIGN KEY (trip_id) REFERENCES trips (id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '行程对话消息';

-- 景点自身属性的全局缓存，与会话无关；并发写入同一景点时保留先写入的记录。
CREATE TABLE IF NOT EXISTS poi_profiles (
    poi_id VARCHAR(64) COLLATE utf8mb4_bin NOT NULL COMMENT '高德 POI 标识',
    name VARCHAR(255) NOT NULL COMMENT '景点名称',
    subtype VARCHAR(32) NOT NULL COMMENT '细分体验类',
    indoor VARCHAR(16) NOT NULL COMMENT '室内外属性：indoor/outdoor/mixed',
    duration_minutes INT NOT NULL COMMENT '建议游玩分钟数',
    source VARCHAR(16) NOT NULL COMMENT '属性来源：llm/rule',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '更新时间',

    PRIMARY KEY (poi_id)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_unicode_ci
  COMMENT = '景点属性缓存';
