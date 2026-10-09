CREATE TABLE IF NOT EXISTS administrators (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash BLOB NOT NULL,
  password_salt BLOB NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS admin_sessions (
  token_hash TEXT PRIMARY KEY,
  administrator_id INTEGER NOT NULL REFERENCES administrators(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS pricing_configurations (
  version INTEGER PRIMARY KEY,
  configuration_json TEXT NOT NULL,
  approved INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0,1)),
  created_by INTEGER REFERENCES administrators(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS inquiries (
  id INTEGER PRIMARY KEY,
  public_reference TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new','under_review','awaiting_client','quoted','accepted','declined','closed')),
  preferred_name TEXT NOT NULL DEFAULT '',
  contact_method TEXT NOT NULL CHECK (contact_method IN ('email','signal','whatsapp')),
  contact TEXT NOT NULL,
  project_type TEXT NOT NULL,
  academic_level TEXT NOT NULL DEFAULT '',
  discipline TEXT NOT NULL DEFAULT '',
  institution TEXT NOT NULL DEFAULT '',
  working_title TEXT NOT NULL DEFAULT '',
  style_guide TEXT NOT NULL DEFAULT '',
  word_count TEXT NOT NULL DEFAULT '',
  deadline_local TEXT NOT NULL DEFAULT '',
  timezone TEXT NOT NULL DEFAULT '',
  citation_requirements TEXT NOT NULL DEFAULT '',
  rubric TEXT NOT NULL DEFAULT '',
  support_requested TEXT NOT NULL DEFAULT '',
  integrity_confirmed INTEGER NOT NULL CHECK (integrity_confirmed IN (0,1)),
  privacy_consent INTEGER NOT NULL CHECK (privacy_consent IN (0,1)),
  internal_notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_inquiries_status_created ON inquiries(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_inquiries_contact ON inquiries(contact);

CREATE TABLE IF NOT EXISTS quotations (
  id INTEGER PRIMARY KEY,
  inquiry_id INTEGER NOT NULL REFERENCES inquiries(id),
  amount_zmw INTEGER NOT NULL CHECK (amount_zmw > 0),
  currency TEXT NOT NULL DEFAULT 'ZMW' CHECK (currency = 'ZMW'),
  scope TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','sent','accepted','declined','expired','cancelled')),
  valid_until TEXT,
  pricing_version INTEGER REFERENCES pricing_configurations(version),
  pricing_snapshot_json TEXT NOT NULL,
  acceptance_token_hash TEXT NOT NULL UNIQUE,
  created_by INTEGER NOT NULL REFERENCES administrators(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_quotations_inquiry ON quotations(inquiry_id, created_at DESC);

CREATE TABLE IF NOT EXISTS quotation_line_items (
  id INTEGER PRIMARY KEY,
  quotation_id INTEGER NOT NULL REFERENCES quotations(id) ON DELETE CASCADE,
  description TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  unit_amount_zmw INTEGER NOT NULL CHECK (unit_amount_zmw >= 0),
  line_amount_zmw INTEGER NOT NULL CHECK (line_amount_zmw >= 0)
);

CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  inquiry_id INTEGER NOT NULL REFERENCES inquiries(id),
  quotation_id INTEGER NOT NULL UNIQUE REFERENCES quotations(id),
  status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed','in_progress','waiting_on_client','completed','cancelled')),
  agreed_scope TEXT NOT NULL,
  deadline_local TEXT NOT NULL DEFAULT '',
  timezone TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_projects_status_deadline ON projects(status, deadline_local);

CREATE TABLE IF NOT EXISTS project_milestones (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  due_at TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','in_progress','completed','cancelled')),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS payment_records (
  id INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id),
  quotation_id INTEGER NOT NULL REFERENCES quotations(id),
  amount_zmw INTEGER NOT NULL CHECK (amount_zmw > 0),
  currency TEXT NOT NULL DEFAULT 'ZMW' CHECK (currency = 'ZMW'),
  status TEXT NOT NULL CHECK (status IN ('pending','successful','failed','cancelled','refunded')),
  provider TEXT NOT NULL DEFAULT 'manual',
  transaction_reference TEXT NOT NULL DEFAULT '',
  recorded_by INTEGER NOT NULL REFERENCES administrators(id),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_payments_project ON payment_records(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  preview_text TEXT NOT NULL,
  published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0,1)),
  created_by INTEGER NOT NULL REFERENCES administrators(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_samples_published_category ON samples(published, category);

CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  administrator_id INTEGER REFERENCES administrators(id),
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  details_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);

CREATE TABLE IF NOT EXISTS rate_limits (
  key_hash TEXT PRIMARY KEY,
  window_started_at INTEGER NOT NULL,
  request_count INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
