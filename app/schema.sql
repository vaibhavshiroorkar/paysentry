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

-- One funding account per currency. Top-ups are transfers out of it, so the
-- sum of all balances in a currency is always zero.
CREATE UNIQUE INDEX IF NOT EXISTS one_funding_account_per_currency
    ON accounts (currency) WHERE owner_id = 'system:funding';

INSERT INTO accounts (owner_id, currency, allow_negative)
VALUES ('system:funding', 'INR', true), ('system:funding', 'USD', true)
ON CONFLICT DO NOTHING;

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
    -- The exact response sent the first time, replayed on retries.
    response_code    int,
    response_body    jsonb,
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
