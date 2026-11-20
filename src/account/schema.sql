-- 账号注册表（SQLite 版，目标库 data/trading.db）
--
-- 与 config/accounts.yaml 字段一一对应，由 tools/migrate_accounts_to_sqlite.py 建表并导入。
-- 约定：
--   1. 密码/认证码只存环境变量名和引用（secret://...），明文永不入库；
--   2. broker_profiles 存的是「连接」（期货公司接入点），不含资金号与密码明文；
--   3. 账号通过 profile_id 引用连接，一条连接可被多个账号共用；
--   4. 时间沿用注册表的 ISO 8601 本地时间字符串；
--   5. P0 的 YAML 注册表仍是权威来源，这张表是 P1 的落点，随时可以删掉重建。

CREATE TABLE IF NOT EXISTS broker_profiles (
    profile_id              TEXT PRIMARY KEY,
    label                   TEXT    NOT NULL DEFAULT '',
    broker                  TEXT    NOT NULL DEFAULT '',
    environment             TEXT    NOT NULL CHECK (environment IN ('sim', 'live')),
    broker_id               TEXT    NOT NULL DEFAULT '',
    td_fronts               TEXT    NOT NULL DEFAULT '',   -- 多台前置用 ; 分隔
    md_fronts               TEXT    NOT NULL DEFAULT '',
    app_id                  TEXT    NOT NULL DEFAULT '',
    flow_dir                TEXT    NOT NULL DEFAULT '',
    password_env            TEXT    NOT NULL DEFAULT '',
    auth_code_env           TEXT    NOT NULL DEFAULT '',
    password_ref            TEXT    NOT NULL DEFAULT '',   -- 例如 secret://ctp/sim/password
    auth_code_ref           TEXT    NOT NULL DEFAULT '',
    query_interval_sec      REAL    NOT NULL DEFAULT 30,
    login_timeout_sec       REAL    NOT NULL DEFAULT 20,
    order_wait_sec          REAL    NOT NULL DEFAULT 8,
    cancel_wait_sec         REAL    NOT NULL DEFAULT 3,
    order_count_warn        INTEGER NOT NULL DEFAULT 8,
    cancel_count_warn       INTEGER NOT NULL DEFAULT 8,
    close_split_products    TEXT    NOT NULL DEFAULT 'bu,c',
    price_tick              REAL    NOT NULL DEFAULT 1,
    max_order_volume        INTEGER NOT NULL DEFAULT 20,
    auto_cancel_on_timeout  INTEGER NOT NULL DEFAULT 1 CHECK (auto_cancel_on_timeout IN (0, 1))
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id        TEXT PRIMARY KEY,                      -- 稳定身份，形如 acc_huatai_sim_01
    display_name      TEXT    NOT NULL DEFAULT '',
    broker            TEXT    NOT NULL DEFAULT '',
    channel           TEXT    NOT NULL DEFAULT 'ctp' CHECK (channel IN ('ctp', 'gui', 'sim')),
    environment       TEXT    NOT NULL CHECK (environment IN ('sim', 'live')),
    status            TEXT    NOT NULL DEFAULT 'enabled'
                              CHECK (status IN ('enabled', 'disabled', 'archived')),
    profile_id        TEXT    REFERENCES broker_profiles(profile_id)
                              ON UPDATE CASCADE ON DELETE RESTRICT,
    investor_id       TEXT    NOT NULL DEFAULT '',
    book_id           TEXT,
    risk_policy_id    TEXT    NOT NULL DEFAULT 'default',
    password_ref      TEXT    NOT NULL DEFAULT '',
    auth_code_ref     TEXT    NOT NULL DEFAULT '',
    captcha_mode      TEXT    NOT NULL DEFAULT 'pause'
                              CHECK (captcha_mode IN ('pause', 'skip')),
    skip_auto_login   INTEGER NOT NULL DEFAULT 1 CHECK (skip_auto_login IN (0, 1)),
    max_order_volume  INTEGER NOT NULL DEFAULT 0,
    notes             TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 一条连接可被多个账号引用（资金号只存在账号上），旧的唯一约束不再使用。
DROP INDEX IF EXISTS ux_accounts_profile;

-- 一个账号一本账。
CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_book
    ON accounts(book_id) WHERE book_id IS NOT NULL AND book_id <> '';

CREATE INDEX IF NOT EXISTS ix_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS ix_accounts_environment ON accounts(environment);

-- P1 交易账本投影。JSON 仍是默认权威来源；这些表在同一事务里由整本账重建，
-- 因此新账期清空历史时，SQLite 也不会残留上一账期的成交和持仓。
CREATE TABLE IF NOT EXISTS books (
    book_id           TEXT PRIMARY KEY,
    account_id        TEXT NOT NULL,
    started_at        TEXT NOT NULL DEFAULT '',
    period_started_at TEXT NOT NULL DEFAULT '',
    initial_capital   REAL NOT NULL DEFAULT 0,
    funding           TEXT NOT NULL DEFAULT '',
    payload_json      TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    account_id   TEXT NOT NULL,
    order_id     TEXT NOT NULL,
    symbol       TEXT NOT NULL DEFAULT '',
    side         TEXT NOT NULL DEFAULT '',
    volume       INTEGER NOT NULL DEFAULT 0,
    bar_id       TEXT NOT NULL DEFAULT '',
    stage        TEXT NOT NULL DEFAULT '',
    allowed      INTEGER CHECK (allowed IN (0, 1) OR allowed IS NULL),
    price        REAL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id)
);

CREATE INDEX IF NOT EXISTS ix_orders_updated ON orders(account_id, updated_at);
CREATE INDEX IF NOT EXISTS ix_orders_symbol ON orders(account_id, symbol, updated_at);

CREATE TABLE IF NOT EXISTS fills (
    book_id     TEXT NOT NULL,
    fill_id     TEXT NOT NULL,
    account_id  TEXT NOT NULL,
    time        TEXT NOT NULL DEFAULT '',
    symbol      TEXT NOT NULL DEFAULT '',
    offset      TEXT NOT NULL DEFAULT '',
    direction   TEXT NOT NULL DEFAULT '',
    price       REAL NOT NULL DEFAULT 0,
    volume      INTEGER NOT NULL DEFAULT 0,
    multiplier  REAL NOT NULL DEFAULT 0,
    commission  REAL NOT NULL DEFAULT 0,
    bar_id      TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    PRIMARY KEY (book_id, fill_id)
);

CREATE INDEX IF NOT EXISTS ix_fills_time ON fills(book_id, time);
CREATE INDEX IF NOT EXISTS ix_fills_symbol ON fills(book_id, symbol, time);

CREATE TABLE IF NOT EXISTS cashflows (
    book_id TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    time    TEXT NOT NULL DEFAULT '',
    kind    TEXT NOT NULL DEFAULT '',
    amount  REAL NOT NULL DEFAULT 0,
    note    TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (book_id, seq)
);

CREATE INDEX IF NOT EXISTS ix_cashflows_day ON cashflows(book_id, time, kind);

CREATE TABLE IF NOT EXISTS positions (
    book_id     TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL CHECK (side IN ('long', 'short')),
    volume      INTEGER NOT NULL DEFAULT 0,
    entry_price REAL NOT NULL DEFAULT 0,
    opened_at   TEXT NOT NULL DEFAULT '',
    stop_price  REAL NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (book_id, symbol)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    book_id TEXT NOT NULL,
    source  TEXT NOT NULL,
    seq     INTEGER NOT NULL,
    time    TEXT NOT NULL DEFAULT '',
    equity  REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (book_id, source, seq)
);

CREATE INDEX IF NOT EXISTS ix_equity_time ON equity_snapshots(book_id, time);

CREATE TABLE IF NOT EXISTS risk_state (
    account_id  TEXT PRIMARY KEY,
    trading_day TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risk_decisions (
    decision_id TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    trading_day TEXT NOT NULL DEFAULT '',
    ts          TEXT NOT NULL,
    gate        TEXT NOT NULL DEFAULT '',
    symbol      TEXT NOT NULL DEFAULT '',
    side        TEXT NOT NULL DEFAULT '',
    volume      INTEGER NOT NULL DEFAULT 0,
    allowed     INTEGER NOT NULL CHECK (allowed IN (0, 1)),
    code        TEXT NOT NULL DEFAULT '',
    reason      TEXT NOT NULL DEFAULT '',
    intent_json TEXT NOT NULL DEFAULT '{}',
    state_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS ix_risk_decisions_query
    ON risk_decisions(account_id, trading_day, ts);

-- P2 发件箱：订单/成交事实先落这里，再由后台线程补账本投影。
-- 状态：pending=已落库待入账；ready=可投递；done=消费完成；failed=重试耗尽。
CREATE TABLE IF NOT EXISTS event_outbox (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    type         TEXT NOT NULL,
    account_id   TEXT NOT NULL DEFAULT '',
    book_id      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'ready', 'done', 'failed')),
    attempts     INTEGER NOT NULL DEFAULT 0,
    last_error   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_outbox_status ON event_outbox(status, seq);
