PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('police','forensic','prosecutor','judge','defence','admin')),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
  case_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES users(user_id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_files (
  seq INTEGER PRIMARY KEY,
  file_id TEXT UNIQUE NOT NULL,
  case_id TEXT NOT NULL REFERENCES cases(case_id),
  original_filename TEXT NOT NULL,
  file_type TEXT NOT NULL,
  content_type TEXT,
  size_bytes INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  stored_path TEXT,
  status TEXT NOT NULL CHECK (status IN ('pending','verified','tampered','quarantined','failed')),
  ledger_entry_no INTEGER,
  uploaded_by TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  last_verified_at TEXT,
  tamper_reason TEXT
);

CREATE TABLE IF NOT EXISTS ledger_entries (
  entry_no INTEGER PRIMARY KEY,
  file_id TEXT NOT NULL,
  case_id TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  uploader_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  prev_entry_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger_entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;

CREATE TABLE IF NOT EXISTS audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp TEXT NOT NULL,
  user_id TEXT,
  action TEXT NOT NULL,
  file_id TEXT,
  case_id TEXT,
  result TEXT NOT NULL,
  detail TEXT
);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;

CREATE TABLE IF NOT EXISTS alerts (
  alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  severity TEXT NOT NULL,
  file_id TEXT,
  case_id TEXT,
  message TEXT NOT NULL,
  acknowledged INTEGER NOT NULL DEFAULT 0,
  acknowledged_by TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consumer_offsets (
  consumer_id TEXT PRIMARY KEY,
  last_event_id INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);