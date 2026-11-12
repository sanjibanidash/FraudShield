-- FraudShield schema (runs automatically the first time the Postgres container starts)

CREATE TABLE IF NOT EXISTS predictions (
    tx_id             TEXT PRIMARY KEY,
    step              INTEGER NOT NULL,
    type              TEXT NOT NULL,
    amount            DOUBLE PRECISION NOT NULL,
    name_orig         TEXT,
    oldbalance_org    DOUBLE PRECISION,
    newbalance_orig   DOUBLE PRECISION,
    name_dest         TEXT,
    oldbalance_dest   DOUBLE PRECISION,
    newbalance_dest   DOUBLE PRECISION,
    fraud_probability DOUBLE PRECISION NOT NULL,
    is_fraud_pred     BOOLEAN NOT NULL,
    true_label        SMALLINT,              -- delayed ground truth (chargeback)
    analyst_label     TEXT,                  -- 'fraud' | 'legit' from the dashboard
    reviewed_at       TIMESTAMPTZ,
    model_version     TEXT,
    drift_mode        TEXT,
    produced_at       TIMESTAMPTZ,
    scored_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    latency_ms        DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_predictions_scored_at ON predictions (scored_at DESC);
CREATE INDEX IF NOT EXISTS idx_predictions_flagged ON predictions (scored_at DESC) WHERE is_fraud_pred;

CREATE TABLE IF NOT EXISTS drift_reports (
    id               SERIAL PRIMARY KEY,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_size      INTEGER,
    share_drifted    DOUBLE PRECISION,
    dataset_drift    BOOLEAN,
    drifted_features TEXT[],
    details          JSONB
);

CREATE TABLE IF NOT EXISTS daily_reports (
    id          SERIAL PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    report_date DATE,
    details     JSONB
);

CREATE TABLE IF NOT EXISTS model_events (
    id         SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    event      TEXT,
    version    TEXT,
    details    JSONB
);

CREATE TABLE IF NOT EXISTS alerts (
    id          SERIAL PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT,
    alertname   TEXT,
    severity    TEXT,
    summary     TEXT,
    payload     JSONB
);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ DEFAULT now()
);
INSERT INTO settings (key, value) VALUES ('drift_mode', 'none'), ('retrain_requested', '0')
ON CONFLICT (key) DO NOTHING;
