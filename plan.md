# Central TG Plan — Implementation Plan

## Overview

Add a centralized talkgroup management system backed by PostgreSQL. Repeater owners authenticate with API tokens and manage per-repeater TG assignments via REST API. Core servers receive real-time updates via PG LISTEN/NOTIFY.

**Key principle:** Config patterns remain the security boundary (auth + max-allowed TGs). The TG plan operates *within* that boundary. No TG plan entry = backward compatible (uses config TGs as today).

## Data Model

```sql
CREATE TABLE owners (
    callsign    TEXT PRIMARY KEY,
    api_token   TEXT UNIQUE NOT NULL,   -- stored as bcrypt hash
    name        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE repeaters (
    radio_id        INTEGER PRIMARY KEY,
    owner_callsign  TEXT NOT NULL REFERENCES owners(callsign) ON DELETE CASCADE,
    name            TEXT,                -- friendly name ("Downtown Hilltop")
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE tg_assignments (
    repeater_id   INTEGER NOT NULL REFERENCES repeaters(radio_id) ON DELETE CASCADE,
    slot          INTEGER NOT NULL CHECK (slot IN (1, 2)),
    talkgroup_id  INTEGER NOT NULL,
    PRIMARY KEY (repeater_id, slot, talkgroup_id)
);
```

Bulk assignment = INSERT same TG across multiple repeaters in one transaction.

## Architecture

```
   Owner (API token)
        │
        ▼
   Dashboard REST API ──── POST /api/tg-plan/...
        │
        ▼
   PostgreSQL ──── NOTIFY tg_plan_changed
        │
        ├──► Core Server Node 1 (LISTEN) → update RepeaterState TGs
        └──► Core Server Node 2 (LISTEN) → update RepeaterState TGs
```

Both core servers and dashboard connect directly to PostgreSQL.

## File Changes

### New Files

| File | Purpose |
|------|---------|
| `nexus/tg_plan.py` (~200 lines) | `TGPlanStore` — async PG CRUD, LISTEN/NOTIFY handler, token validation |
| `dashboard/tg_plan_api.py` (~150 lines) | FastAPI router with TG plan REST endpoints + API token auth middleware |
| `scripts/tg_plan_schema.sql` | Database schema DDL |
| `tests/test_tg_plan.py` | Unit tests for TGPlanStore |
| `tests/test_tg_plan_api.py` | API endpoint tests |

### Modified Files

| File | Change |
|------|--------|
| `nexus/hblink.py` | Load TGPlanStore at startup, query during `_load_repeater_tg_config()`, subscribe to PG notifications, handle live TG updates |
| `nexus/config.py` | Add `tg_plan` config section (PG connection string, enabled flag) |
| `config/config_sample.json` | Add `tg_plan` section |
| `dashboard/server.py` | Mount TG plan API router, add PG connection pool |
| `dashboard/config_sample.json` | Add `tg_plan` section (PG connection string) |
| `nexus_cli/shell.py` | Add `tg-plan` command family |
| `nexus_cli/datasource.py` | Add TG plan query methods |
| `requirements.txt` | Add `asyncpg`, `bcrypt` |
| `requirements-dashboard.txt` | Add `asyncpg`, `bcrypt` |

## Integration: How TG Plan Merges with Config

In `_load_repeater_tg_config()`:

```
config_tgs = pattern match from JSON config (existing)
plan_tgs = query from PostgreSQL TG plan

if plan exists for this repeater:
    if config allows all (None):
        final = plan_tgs
    else:
        final = config_tgs ∩ plan_tgs   # intersection
else:
    final = config_tgs                   # backward compatible
```

This preserves the security boundary — config patterns define what's *allowed*, TG plan defines what's *active*.

## Live Update Flow

1. API writes TG assignment to PostgreSQL
2. API issues `NOTIFY tg_plan_changed, '{"repeater_id": 311000}'`
3. Core server's asyncpg listener fires callback
4. Callback re-queries TG plan for affected repeater
5. Updates `RepeaterState.slot1/2_talkgroups` in-place
6. Updates HomeBrew subscription via `_create_homebrew_subscription()`
7. Emits `repeater_options_updated` event → dashboard gets live update
8. If backbone gateway, re-computes regional TG union → advertise if changed

## REST API Endpoints

All endpoints require `Authorization: Bearer <api_token>` header.

### Owner Management (admin-only, separate admin token in config)
```
POST   /api/tg-plan/owners              — Create owner {callsign, name}
GET    /api/tg-plan/owners              — List owners
DELETE /api/tg-plan/owners/:callsign    — Remove owner + cascade
POST   /api/tg-plan/owners/:callsign/rotate-token — Generate new API token
```

### Repeater Registration (owner-scoped)
```
POST   /api/tg-plan/repeaters           — Register repeater {radio_id, name}
GET    /api/tg-plan/repeaters           — List my repeaters
DELETE /api/tg-plan/repeaters/:id       — Unregister repeater + cascade
```

### TG Assignments (owner-scoped)
```
GET    /api/tg-plan/repeaters/:id/talkgroups         — Get TGs for repeater
PUT    /api/tg-plan/repeaters/:id/talkgroups         — Set TGs {slot1: [...], slot2: [...]}
POST   /api/tg-plan/bulk-assign                       — Assign TG to multiple repeaters
         {repeater_ids: [...], slot: 1, talkgroup_ids: [...]}
```

### Read-Only (any authenticated owner)
```
GET    /api/tg-plan/talkgroups/:tg_id/repeaters      — Which repeaters carry this TG?
```

## Implementation Phases

### Phase 1: Schema + TGPlanStore (~200 lines)
- PostgreSQL schema DDL
- `nexus/tg_plan.py`: async CRUD with asyncpg
- Token generation (secrets.token_urlsafe) + bcrypt hashing
- LISTEN/NOTIFY subscription
- Unit tests

### Phase 2: Core Server Integration
- Config section for PG connection
- Load TGPlanStore at startup
- Modify `_load_repeater_tg_config()` to consult TG plan
- LISTEN callback → live TG updates on connected repeaters
- Tests for intersection logic

### Phase 3: REST API
- `dashboard/tg_plan_api.py`: FastAPI router
- API token auth dependency
- Owner CRUD, repeater CRUD, TG assignment CRUD
- Bulk assignment endpoint
- Mount on dashboard

### Phase 4: CLI Integration
- `hbctl tg-plan` or `nexus-cli tg-plan` commands
- List owners, repeaters, assignments
- Assign/remove TGs

## Config Addition

```json
{
    "tg_plan": {
        "enabled": false,
        "database_url": "postgresql://nexus:password@10.31.11.x:5432/nexus",
        "admin_token": "change-me-admin-token"
    }
}
```

## Dependencies

- `asyncpg` — async PostgreSQL driver (production-grade, used by FastAPI ecosystem)
- `bcrypt` — API token hashing

## What Does NOT Change

- Pattern-based config still controls auth/passphrase
- Blacklist still checked first
- RPTO (OPTIONS) protocol still works (within TG plan bounds)
- Cluster bus, backbone bus, stream routing — all unchanged
- Config hot-reload still works (re-evaluates config patterns)
- Trust flag still works (config layer, orthogonal to TG plan)
