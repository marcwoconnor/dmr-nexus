# DMR Nexus — Distributed DMR Network Platform

DMR Nexus is a distributed DMR (Digital Mobile Radio) network platform implementing the HomeBrew protocol for amateur radio networks. Built on the foundation of HBlink by Cort Buffington (N0MJS), DMR Nexus has been extensively rewritten and extended by Marc O'Connor, KK4WTI to support **multi-server clustering**, a **cluster-native client protocol**, **inter-region backbone routing**, and **global-scale hierarchical forwarding** — transforming a standalone repeater server into a distributed network platform.

## What's Different

The original HBlink was a single-server design: one process, one set of repeaters, no coordination between instances. DMR Nexus adds:

| Capability | Description |
|-----------|-------------|
| **Cluster Bus** | TCP full-mesh between servers with HMAC-SHA256 auth, heartbeats, automatic reconnect |
| **Cross-Server Routing** | A transmission on Server A reaches Server B's repeaters if they share talkgroups |
| **Graceful Failover** | Ordered drain/shutdown, dead peer detection, virtual stream cleanup |
| **Cluster-Native Protocol** | Stateless token auth, client-driven TG subscriptions, enhanced keepalive with cluster health |
| **HomeBrew Proxy Adapter** | Legacy repeaters work unchanged — HomeBrew auth translates to native subscriptions internally |
| **Multi-Connect Dedup** | Clients can connect to multiple servers simultaneously for zero-downtime failover |
| **Backbone Bus** | Gateway-only inter-region TCP mesh for global scale (separate port, separate secrets) |
| **TG Routing Table** | Gateways compute regional TG unions, advertise on change only — no broadcast storms |
| **Hierarchical Forwarding** | Single-hop cross-region stream forwarding through gateway nodes |
| **Cross-Region User Lookup** | Pull-model private call routing across regions with positive/negative caching |
| **Config Hot-Reload** | SIGHUP reloads routing rules, re-evaluates connections, no restart needed |
| **Management Interface** | Unix socket CLI (`hbctl`) for cluster status, drain, reload, repeater list |
| **Real-Time Dashboard** | FastAPI + WebSocket dashboard with cluster-wide visibility |

## Architecture

```
                         Region A                                    Region B
              ┌──────────────────────────┐                ┌──────────────────────────┐
              │                          │   Backbone Bus  │                          │
  Repeaters ──┤  Server 1 ←──────────→ Server 2 (GW) ════════ Server 3 (GW) ←──→ Server 4 │
              │       ↕  Cluster Bus  ↕  │   (port 62033)  │       ↕  Cluster Bus  ↕  │
              │  Server 5 ←──────────→ Server 6             Server 7 ←──────────→ Server 8 │
              └──────────────────────────┘                └──────────────────────────┘
```

- **Intra-region:** Flat TCP mesh (cluster bus, port 62032). All nodes see all repeaters. Streams forwarded by TG match, single-hop.
- **Inter-region:** Gateway-only TCP mesh (backbone bus, port 62033). TG routing table determines which regions receive each stream. Non-gateway nodes route cross-region traffic through their local gateway.
- **Private calls:** User cache tracks radio IDs to last-heard repeater. Cross-region lookups use a pull model through backbone gateways with 60s positive / 30s negative cache TTL.

### Core Components (~7,800 lines)

| File | Lines | Purpose |
|------|-------|---------|
| `nexus/hblink.py` | ~4,100 | Central hub — UDP protocol, stream routing, cluster/backbone integration, native protocol, management interface |
| `nexus/backbone.py` | ~900 | Inter-region backbone bus, TG routing table, cross-region user lookup service |
| `nexus/cluster.py` | ~640 | Intra-region TCP mesh cluster bus, peer management, heartbeats, HMAC auth |
| `nexus/events.py` | ~340 | Event emitter to dashboard (length-prefixed JSON over TCP/Unix socket) |
| `nexus/user_cache.py` | ~290 | Radio ID tracking for private call routing and dashboard "Last Heard" |
| `nexus/models.py` | ~280 | Dataclasses: RepeaterState, OutboundState, StreamState |
| `nexus/access_control.py` | ~220 | Pattern-based repeater matching, blacklisting, per-repeater config |
| `nexus/cluster_protocol.py` | ~200 | Cluster-native client protocol, stateless HMAC token auth |
| `nexus/protocol.py` | ~190 | DMR packet parsing, terminator detection (hot path) |
| `nexus/subscriptions.py` | ~170 | Client TG subscription store with config validation |

### Dashboard

FastAPI app receiving events from the core server via socket, pushing to browsers via WebSocket. Shows cluster-wide repeater state, active streams, last heard, and cluster health.

### Test Suite

282 tests across 11 test files. All tests use self-contained inline config — no dependency on config files.

## Quick Start

```bash
# Setup
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dashboard.txt  # optional, for dashboard

# Configure
cp config/config_sample.json config/config.json
# Edit config.json — see docs/configuration.md

# Run
python3 run.py                    # Core server
python3 run_dashboard.py          # Dashboard (separate terminal)
./run_all.sh                      # Both together

# Tests
python3 -m pytest tests/ -v

# Management (when running with management socket enabled)
python3 hbctl.py status
python3 hbctl.py cluster
python3 hbctl.py repeaters
```

For production deployment with systemd, see [SYSTEMD.md](SYSTEMD.md).

## Configuration

JSON configs in `config/`. Key sections:

- `global` — Bind addresses, ports, timeouts, logging, user cache settings
- `repeater_configurations.patterns` — Per-repeater rules with talkgroup lists per slot
- `blacklist.patterns` — Block by ID, ID range, or callsign wildcard
- `outbound_connections` — Server-to-server links
- `cluster` — Clustering: node_id, peers, shared_secret, region, role (node/gateway)
- `backbone` — Inter-region gateway mesh: backbone_secret, gateway list
- `dashboard` — Transport settings (TCP or Unix socket)

See `config/config_sample.json` for a complete example with all sections documented.

## Documentation

- **[Configuration Guide](docs/configuration.md)** — Complete config reference
- **[Dashboard README](dashboard/README.md)** — Dashboard features
- **[Systemd Deployment](SYSTEMD.md)** — Production setup
- **[Connecting Repeaters](docs/connecting_to_nexus.md)** — Repeater configuration
- **[Clustering Architecture](docs/clustering_plan.md)** — Multi-server design
- **[Native Protocol Spec](docs/cluster_native_protocol.md)** — Cluster-native client protocol
- **[Global Scaling](docs/global_scaling.md)** — Hierarchical routing design
- **[Implementation Progress](docs/PROGRESS.md)** — Phase-by-phase build notes

## Implementation Status

| Phase | Description | Status |
|-------|-------------|--------|
| 1.1-1.2 | Cluster Bus + State Advertisement | Done |
| 1.3 | User Cache Sharing | Done |
| 1.4 + 4.2 | Dashboard Cluster View | Done |
| 2.1-2.4 | Cross-Server Stream Routing | Done |
| 3.1-3.3 | Failover, Drain, Graceful Shutdown | Done |
| 4.1 + 4.3 | Config Hot-Reload + Management CLI | Done |
| 5.1-5.2 | Token Auth + Subscriptions | Done |
| 5.3 | Cluster-Aware Keepalive | Done |
| 5.4 | HomeBrew Proxy Adapter | Done |
| 5.5 | Multi-Connect Server-Side Dedup | Done |
| 6.1-6.3 | Backbone Bus + TG Routing + Hierarchical Forwarding | Done |
| 6.4 | Cross-Region User Lookup | Done |
| 6.5 | Gateway Failover (dual gateway per region) | Done |
| 6.6 | Backbone Hardening (TLS, key rotation, priority queues) | Planned |

## Credits

- **Cort Buffington, N0MJS** — Original HBlink/HBlink3 design, HomeBrew protocol implementation, single-server architecture. DMR Nexus was built on his work and retains the core UDP protocol handler and packet parsing.
- **SP2ONG, KC1AWV, K2IE** — HBmonitor, the original web dashboard for HBlink. DMR Nexus's dashboard was inspired by HBmonitor's approach to real-time DMR server monitoring.
- **Marc O'Connor, KK4WTI** — DMR Nexus platform: clustering architecture, backbone bus, native client protocol, cross-region routing, global-scale hierarchical forwarding, gateway failover, latency tracing, real-time dashboard, management interface, and the extensive test suite.
- The MMDVM and DMR amateur radio community.

## License

This project is licensed under the GNU GPLv3 License — see the LICENSE file for details.

Original work Copyright (C) 2016-2025 Cortney T. Buffington, N0MJS.
Clustering and global-scale extensions Copyright (C) 2025-2026 Marc O'Connor, KK4WTI.
