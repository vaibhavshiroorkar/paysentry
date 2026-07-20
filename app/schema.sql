-- Applied on every startup, so every statement must be safe to re-run.

CREATE TABLE IF NOT EXISTS accounts (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id       text NOT NULL,
    currency       char(3) NOT NULL,
    -- Money is stored in minor units (paise, cents) as an integer. No floats.
    balance_minor  bigint NOT NULL DEFAULT 0,
    allow_negative boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    -- Last line of defence. The ledger code checks funds before this fires.
    CONSTRAINT balance_not_negative CHECK (allow_negative OR balance_minor >= 0)
);

-- System and merchant owners hold exactly one account per currency.
CREATE UNIQUE INDEX IF NOT EXISTS one_account_per_owner_currency
    ON accounts (owner_id, currency)
    WHERE owner_id LIKE 'system:%' OR owner_id LIKE 'merchant:%';

-- system:funding pays for top-ups. system:gateway_clearing stands for money
-- the card gateways owe us: captures draw from it, refunds return to it.
-- Both may go negative, so the sum of all balances in a currency stays zero.
INSERT INTO accounts (owner_id, currency, allow_negative)
VALUES ('system:funding', 'INR', true), ('system:funding', 'USD', true),
       ('system:gateway_clearing', 'INR', true), ('system:gateway_clearing', 'USD', true)
ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS merchants (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    -- SHA-256 of the secret key. The key itself is shown once and never stored.
    api_key_hash    text NOT NULL UNIQUE,
    api_key_prefix  text NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- Idempotency for merchant requests. Transfers keep their own key column.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    merchant_id     uuid NOT NULL REFERENCES merchants (id),
    key             text NOT NULL,
    -- SHA-256 of method, path and body (cards reduced to brand, last 4, expiry).
    request_hash    text NOT NULL,
    state           text NOT NULL CHECK (state IN ('in_progress', 'completed')),
    response_code   int,
    response_body   json,
    created_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    PRIMARY KEY (merchant_id, key)
);

CREATE TABLE IF NOT EXISTS transfers (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    idempotency_key  text NOT NULL UNIQUE,
    -- SHA-256 of the request body, to reject a key reused with a new body.
    request_hash     text NOT NULL,
    from_account_id  uuid NOT NULL REFERENCES accounts (id),
    to_account_id    uuid NOT NULL REFERENCES accounts (id),
    amount_minor     bigint NOT NULL CHECK (amount_minor > 0),
    currency         char(3) NOT NULL,
    -- PROCESSING only exists inside the posting transaction in v0.1. No other
    -- session can see it, because the row commits with its final status.
    status           text NOT NULL CHECK (status IN ('PROCESSING', 'SUCCEEDED', 'FAILED')),
    failure_reason   text,
    -- The exact response sent the first time, replayed on retries. json, not
    -- jsonb, because jsonb reorders keys and the replay must match byte for byte.
    response_code    int,
    response_body    json,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CHECK (from_account_id <> to_account_id)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id            bigserial PRIMARY KEY,
    transfer_id   uuid NOT NULL REFERENCES transfers (id),
    account_id    uuid NOT NULL REFERENCES accounts (id),
    -- Negative for a debit, positive for a credit.
    amount_minor  bigint NOT NULL CHECK (amount_minor <> 0),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ledger_entries_account_idx ON ledger_entries (account_id, id);
CREATE INDEX IF NOT EXISTS ledger_entries_transfer_idx ON ledger_entries (transfer_id);

-- Corrections are new entries, never edits.
CREATE OR REPLACE FUNCTION reject_ledger_change() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER ledger_entries_append_only
    BEFORE UPDATE OR DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION reject_ledger_change();

CREATE TABLE IF NOT EXISTS payments (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    merchant_id            uuid NOT NULL REFERENCES merchants (id),
    amount_minor           bigint NOT NULL CHECK (amount_minor > 0),
    currency               char(3) NOT NULL,
    capture_method         text NOT NULL CHECK (capture_method IN ('automatic', 'manual')),
    -- See docs/design/payments.md for the state machine.
    status                 text NOT NULL CHECK (status IN (
                               'processing', 'authorized', 'capture_pending', 'captured',
                               'void_pending', 'voided', 'failed', 'refunded')),
    amount_captured        bigint NOT NULL DEFAULT 0,
    amount_refunded        bigint NOT NULL DEFAULT 0,
    -- Set only while a capture waits on the processor.
    pending_capture_minor  bigint,
    -- Never the full card number. See docs/design/payments.md, "Card data".
    card_brand             text NOT NULL,
    card_last4             char(4) NOT NULL,
    -- Which gateway holds the authorization, and its id for it there.
    gateway                text NOT NULL,
    gateway_reference      text,
    failure_reason         text,
    -- The key that created the payment, and the key of a capture or void in
    -- flight. Recovery stores the final response under them.
    idempotency_key        text NOT NULL,
    pending_op_key         text,
    -- Set while a recovery worker holds the row. See docs/design/gateways.md.
    lease_until            timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CHECK (amount_captured <= amount_minor),
    CHECK (amount_refunded <= amount_captured),
    CHECK (pending_capture_minor IS NULL OR pending_capture_minor BETWEEN 1 AND amount_minor)
);

CREATE INDEX IF NOT EXISTS payments_merchant_idx ON payments (merchant_id, created_at DESC);

CREATE TABLE IF NOT EXISTS refunds (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id      uuid NOT NULL REFERENCES payments (id),
    amount_minor    bigint NOT NULL CHECK (amount_minor > 0),
    -- A pending refund reserves its amount until the processor answers.
    status          text NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed')),
    failure_reason  text,
    idempotency_key text NOT NULL,
    lease_until     timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS refunds_payment_idx ON refunds (payment_id, created_at);

-- One row per authorization sent to a gateway. The id is our reference there.
CREATE TABLE IF NOT EXISTS payment_attempts (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_id         uuid NOT NULL REFERENCES payments (id),
    gateway            text NOT NULL,
    -- pending: sent, or about to be. unknown: sent, no answer. failed: the
    -- gateway certainly did nothing, or has no record of it.
    status             text NOT NULL CHECK (status IN ('pending', 'unknown', 'succeeded', 'declined', 'failed')),
    gateway_reference  text,
    decline_code       text,
    lease_until        timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS payment_attempts_payment_idx ON payment_attempts (payment_id);
CREATE INDEX IF NOT EXISTS payment_attempts_open_idx ON payment_attempts (created_at)
    WHERE status IN ('pending', 'unknown');
