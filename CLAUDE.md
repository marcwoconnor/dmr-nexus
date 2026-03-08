# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

DMR Nexus is a distributed DMR (Digital Mobile Radio) network platform. Built on the HBlink foundation by Cort Buffington (N0MJS), it has been extensively extended with multi-server clustering, a cluster-native client protocol, inter-region backbone routing, and global-scale hierarchical forwarding. Three main components: the core asyncio UDP server (`nexus/`), a FastAPI real-time web dashboard (`dashboard/`), and a management CLI (`hbctl.py`).

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
python3 hbctl.py status|cluster|repeaters|reload|drain  # Management CLI

# Tests
python3 -m pytest tests/ -v
python3 -m pytest tests/test_access_control.py::TestRepeaterMatcher::test_specific_id_match -v
```

## Architecture

### Core Server (`nexus/`)
- **`hblink.py`** (~4,100 lines): Central hub. `HBProtocol` is the asyncio UDP protocol handler managing inbound repeaters, outbound connections, cluster bus, backbone bus, native client protocol, management socket, and stream routing. All integration lives here.
- **`cluster.py`** (~640 lines): `ClusterBus` — TCP full-mesh for intra-region clustering. HMAC-SHA256 auth, heartbeats, dead peer detection, auto-reconnect, connection dedup (lower node_id wins).
- **`backbone.py`** (~900 lines): `BackboneBus` — gateway-only TCP mesh for inter-region routing. `TalkgroupRoutingTable` tracks which regions subscribe to which TGs. `UserLookupService` handles cross-region private call lookups with positive/negative caching.
- **`cluster_protocol.py`** (~200 lines): Cluster-native client protocol. `TokenManager` issues/validates HMAC-SHA256 tokens. Dual-protocol dispatch via magic bytes (CLNT vs DMRD/RPTL).
- **`subscriptions.py`** (~170 lines): `SubscriptionStore` — client-driven TG subscriptions with config validation and cluster replication.
- **`models.py`** (~280 lines): Dataclasses: `RepeaterState`, `OutboundState`, `StreamState`, `OutboundConnectionConfig`. `RepeaterState` represents any inbound connection (repeaters, hotspots, network links) — `connection_type` field distinguishes them.
- **`access_control.py`** (~220 lines): `RepeaterMatcher` matches incoming connections against JSON config patterns (specific IDs, ID ranges, callsign wildcards). Blacklist-first, then per-repeater config with talkgroup lists.
- **`events.py`** (~340 lines): `EventEmitter` sends JSON events to dashboard via TCP or Unix socket (non-blocking). Length-prefixed framing.
- **`protocol.py`** (~190 lines): DMR packet parsing and terminator detection. Hot path — `parse_dmr_packet()` runs on every voice packet.
- **`user_cache.py`** (~290 lines): `UserCache` tracks radio IDs to last-heard repeater. Used for private call routing, dashboard display, and cross-region user lookup. Supports cluster broadcast with batch throttling.
- **`config.py`**: JSON config loading and validation.
- **`constants.py`**: HomeBrew protocol command bytes and DMR sync patterns.
- **`utils.py`**: Helpers — address normalization (IPv4/IPv6), byte decoding, connection type detection.

### Dashboard (`dashboard/`)
- **`server.py`**: FastAPI app. Receives events from core server via socket, pushes to browser clients via WebSocket. Shows cluster-wide state: repeaters, streams, last heard, cluster health.

### Import Strategy
Modules use dual import paths (package-relative first, fallback to direct) for flexibility running as package or standalone.

### Key Data Flow
1. UDP packet arrives → `HBProtocol.datagram_received()`
2. Dual-protocol dispatch: CLNT magic → native path; DMRD/RPTL → HomeBrew path
3. DMR data packets parsed by `protocol.parse_dmr_packet()`
4. Access control checked via `RepeaterMatcher`
5. Stream tracking manages active transmissions per timeslot (TDMA)
6. Routing: `_calculate_stream_targets()` fans out to local repeaters, outbound connections, native clients, cluster peers, and backbone regions
7. Private calls: user-cache-based routing (not TG-based) with cross-region lookup via backbone
8. `EventEmitter` sends stream/repeater events to dashboard process
9. Dashboard pushes updates to browser via WebSocket

### Stream Tracking
Two-tier stream end detection: immediate DMR terminator detection (~60ms) + timeout fallback (`stream_timeout`). Hang time prevents conversation interruption after stream ends.

### Cluster Architecture
- **Intra-region:** Flat TCP mesh (cluster bus, port 62032). All nodes share repeater state + user cache. Streams forwarded by TG match, single-hop rule (never re-forward virtual streams).
- **Inter-region:** Gateway-only TCP mesh (backbone bus, port 62033). TG routing table determines target regions. Non-gateway nodes route through local gateway.
- **Wire format:** 1-byte type prefix: 0x00=JSON (control), 0x01=binary stream data (hot path: 4B stream_id + 55B DMRD = 60 bytes), 0x02=stream end.
- **Auth:** HMAC-SHA256 challenge-response with separate secrets for cluster vs backbone.
- **Dedup:** Lower node_id keeps outbound connection in both cluster and backbone meshes.

## Configuration

JSON configs in `config/`. Sample: `config/config_sample.json`. Key sections:
- `global`: Bind addresses, ports, timeouts, logging, user cache settings
- `repeater_configurations.patterns`: Pattern-matched per-repeater rules with talkgroup lists per slot
- `blacklist.patterns`: Block by ID, ID range, or callsign wildcard
- `outbound_connections`: Server-to-server links (acts as a repeater to remote server)
- `cluster`: node_id, peers, shared_secret, region, role (node/gateway), heartbeat/dead thresholds
- `backbone`: backbone_secret, gateways list (node_id, region_id, address, port)
- `dashboard`: Transport settings (unix socket or TCP), must match dashboard config
- `connection_type_detection`: Categorizes inbound connections by software/package ID strings

## Conventions

- Python 3.7+ with type hints; asyncio throughout (migrated from Twisted)
- Bytes-heavy protocol code: repeater IDs are 4-byte `bytes`, radio/talkgroup IDs are 3-byte `bytes`
- Talkgroup sets stored as `Set[bytes]` (3-byte TGIDs) for hot-path performance
- `user.csv` at repo root provides radio ID → callsign mapping
- Tests use self-contained inline config via `_make_config()` helpers, not config_sample.json
- Routing test mock (`_make_hbprotocol_for_routing()`) provides a minimal HBProtocol with real methods bound to a MagicMock
- 282 tests across 11 test files, all passing
