CREATE TABLE IF NOT EXISTS reports (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  trigger TEXT NOT NULL,
  build_id TEXT NOT NULL,
  launcher_version TEXT NOT NULL,
  os TEXT NOT NULL,
  gpu TEXT NOT NULL,
  exit_code INTEGER,
  mean_fps REAL,
  low1_fps REAL,
  worst_ms REAL,
  hitch_count INTEGER,
  crash_cause TEXT,
  r2_key TEXT NOT NULL,
  size_bytes INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reports_created_at ON reports (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_trigger ON reports (trigger, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_build_id ON reports (build_id, created_at DESC);

CREATE TABLE IF NOT EXISTS rate_limits (
  bucket_key TEXT PRIMARY KEY,
  day TEXT NOT NULL,
  count INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rate_limits_day ON rate_limits (day);
