ALTER TABLE administrators ADD COLUMN role TEXT NOT NULL DEFAULT 'administrator' CHECK (role IN ('owner','administrator'));

CREATE TABLE IF NOT EXISTS business_settings (
  setting_key TEXT PRIMARY KEY,
  setting_value TEXT NOT NULL,
  updated_by INTEGER NOT NULL REFERENCES administrators(id),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_unique_reference ON payment_records(transaction_reference) WHERE transaction_reference <> '';
