-- migrate:up

-- IF NOT EXISTS: this table predates its migration record on the dev DB
-- (created out-of-band); idempotent form lets dbmate reconcile cleanly.
CREATE TABLE IF NOT EXISTS waitlist_signups (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    source      TEXT NOT NULL DEFAULT 'marketing_site',
    signed_up_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_waitlist_signups_email ON waitlist_signups (email);
CREATE INDEX IF NOT EXISTS idx_waitlist_signups_signed_up_at ON waitlist_signups (signed_up_at DESC);

-- migrate:down

DROP TABLE IF EXISTS waitlist_signups;
