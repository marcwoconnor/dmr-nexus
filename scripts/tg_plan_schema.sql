-- TG Plan schema for DMR Nexus
-- Run: psql -U nexus -d nexus -f scripts/tg_plan_schema.sql
-- Or: auto-created on startup when tg_plan.enabled = true

CREATE TABLE IF NOT EXISTS owners (
    callsign    TEXT PRIMARY KEY,
    api_token   TEXT UNIQUE NOT NULL,   -- bcrypt hash
    name        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS repeaters (
    radio_id        INTEGER PRIMARY KEY,
    owner_callsign  TEXT NOT NULL REFERENCES owners(callsign) ON DELETE CASCADE,
    name            TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tg_assignments (
    repeater_id   INTEGER NOT NULL REFERENCES repeaters(radio_id) ON DELETE CASCADE,
    slot          INTEGER NOT NULL CHECK (slot IN (1, 2)),
    talkgroup_id  INTEGER NOT NULL,
    PRIMARY KEY (repeater_id, slot, talkgroup_id)
);

-- Index for "which repeaters carry TG X?" lookups
CREATE INDEX IF NOT EXISTS idx_tg_assignments_tg ON tg_assignments(talkgroup_id);

-- Index for owner's repeater list
CREATE INDEX IF NOT EXISTS idx_repeaters_owner ON repeaters(owner_callsign);
