-- Wishlist items (current snapshot from ZY account)
CREATE TABLE IF NOT EXISTS wishlist_items (
    product_id    BIGINT PRIMARY KEY,
    name          TEXT        NOT NULL,
    url           TEXT        NOT NULL,
    image_url     TEXT,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE
);

-- SCD-style stock history; one row per (product, variant, poll)
CREATE TABLE IF NOT EXISTS stock_history (
    id            BIGSERIAL PRIMARY KEY,
    product_id    BIGINT      NOT NULL REFERENCES wishlist_items(product_id),
    checked_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    in_stock      BOOLEAN     NOT NULL,
    price         NUMERIC(10,2),
    variant_id    BIGINT,
    variant_name  TEXT
);

CREATE INDEX IF NOT EXISTS idx_stock_history_product ON stock_history (product_id, checked_at DESC);

-- Auto-order rules per product
CREATE TABLE IF NOT EXISTS auto_order_rules (
    product_id      BIGINT PRIMARY KEY REFERENCES wishlist_items(product_id),
    enabled         BOOLEAN      NOT NULL DEFAULT TRUE,
    max_price       NUMERIC(10,2),
    max_qty         INT          NOT NULL DEFAULT 1,
    require_confirm BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- Order attempts with full status lifecycle
CREATE TABLE IF NOT EXISTS order_attempts (
    id                BIGSERIAL PRIMARY KEY,
    product_id        BIGINT      NOT NULL REFERENCES wishlist_items(product_id),
    variant_id        BIGINT,
    triggered_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- pending | cart_added | awaiting_confirm | confirmed | paid | failed | cancelled
    status            TEXT        NOT NULL DEFAULT 'pending',
    telegram_msg_id   BIGINT,
    external_order_id TEXT,
    price_at_order    NUMERIC(10,2),
    error             TEXT,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Expressions in UNIQUE constraints require a separate index in PostgreSQL
CREATE UNIQUE INDEX IF NOT EXISTS idx_order_attempts_no_dup
    ON order_attempts (product_id, variant_id, date_trunc('day', triggered_at));

CREATE INDEX IF NOT EXISTS idx_order_attempts_status ON order_attempts (status, updated_at DESC);
