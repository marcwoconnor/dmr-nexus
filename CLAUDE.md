# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

HBlink4 is a DMR (Digital Mobile Radio) server implementing the HomeBrew protocol for amateur radio networks. It's a **repeater-centric** architecture with per-repeater routing rules using TS/TGID tuples, not server-level "system" groupings. Two main components: the core asyncio UDP server (`hblink4/`) and a FastAPI real-time web dashboard (`dashboard/`).

## Commands

```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dashboard.txt

# Run
python3 run.py [config/config.json]          # Core server (default config auto-detected)
python3 run_dashboard.py [host] [port]       # Dashboard (default 0.0.0.0:8080)
./run_all.sh                                 # Both together

# Tests
python3 -m pytest tests/ -v
python3 -m pytest tests/test_access_control.py::TestRepeaterMatcher::test_specific_id_match -v
```

## Architecture

### Core Server (`hblink4/`)
- **`hblink.py`**: Main server. `HBProtocol` is the asyncio UDP protocol handler managing all inbound repeaters (`_repeaters` dict keyed by 4-byte repeater ID) and outbound connections (`_outbounds` dict keyed by connection name). `OutboundProtocol` handles individual outbound UDP sessions. This is a large file (~900 lines) — the central hub.
- **`models.py`**: Dataclasses: `RepeaterState` (any inbound connection), `OutboundState`, `StreamState` (active DMR transmission), `OutboundConnectionConfig`. Despite the name, `RepeaterState` represents hotspots, network links, etc. — `connection_type` field distinguishes them.
- **`access_control.py`**: `RepeaterMatcher` matches incoming connections against JSON config patterns (specific IDs, ID ranges, callsign wildcards). Checks blacklist first (raises `BlacklistError`), then returns per-repeater config with passphrase and talkgroup lists.
- **`events.py`**: `EventEmitter` sends JSON events to dashboard via TCP or Unix socket (non-blocking, never blocks the server). Length-prefixed framing. Dashboard can send `sync_request` back.
- **`protocol.py`**: DMR packet parsing and terminator detection. Hot path — `parse_dmr_packet()` runs on every voice packet.
- **`user_cache.py`**: `UserCache` tracks radio IDs to last-heard repeater for private call routing optimization and dashboard display. TTL-based expiration.
- **`config.py`**: JSON config loading and validation.
- **`constants.py`**: HomeBrew protocol command bytes (`DMRD`, `RPTL`, `RPTK`, etc.) and DMR sync patterns.
- **`utils.py`**: Helpers — address normalization (IPv4/IPv6), byte decoding, connection type detection.

### Dashboard (`dashboard/`)
- **`server.py`**: FastAPI app. Receives events from core server via socket, pushes to browser clients via WebSocket. Loads `user.csv` for callsign lookups.
- Separate config at `dashboard/config.json` — transport settings must match `config/config.json`'s `dashboard` section.

### Import Strategy
Modules use dual import paths (package-relative first, fallback to direct) for flexibility running as package or standalone.

### Key Data Flow
1. UDP packet arrives → `HBProtocol.datagram_received()`
2. DMR data packets parsed by `protocol.parse_dmr_packet()`
3. Access control checked via `RepeaterMatcher`
4. Stream tracking manages active transmissions per timeslot (TDMA — each slot carries one stream)
5. Routing forwards to approved repeaters; private calls use `UserCache` for targeted routing
6. `EventEmitter` sends stream/repeater events to dashboard process
7. Dashboard pushes updates to browser via WebSocket

### Stream Tracking
Two-tier stream end detection: immediate DMR terminator detection (~60ms) + timeout fallback (`stream_timeout`). Hang time prevents conversation interruption after stream ends. Dashboard updates every 10 superframes (60 packets = 1 second).

## Configuration

JSON configs in `config/`. Sample: `config/config_sample.json`. Key sections:
- `global`: Bind addresses, ports, timeouts, logging, user cache settings
- `repeater_configurations.patterns`: Pattern-matched per-repeater rules with talkgroup lists per slot
- `blacklist.patterns`: Block by ID, ID range, or callsign wildcard
- `outbound_connections`: Server-to-server links (acts as a repeater to remote server)
- `dashboard`: Transport settings (unix socket or TCP), must match dashboard config
- `connection_type_detection`: Categorizes inbound connections by software/package ID strings

## Conventions

- Python 3.7+ with type hints; asyncio (not Twisted — migrated away)
- Bytes-heavy protocol code: repeater IDs are 4-byte `bytes`, radio/talkgroup IDs are 3-byte `bytes`
- Talkgroup sets stored as `Set[bytes]` (3-byte TGIDs) for hot-path performance
- `user.csv` at repo root provides radio ID → callsign mapping
- Systemd service files expect the installation to be owned by the running user
