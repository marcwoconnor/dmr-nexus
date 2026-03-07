# HBlink4 Clustering Implementation Progress

## Session: 2026-03-06

### Completed: Phase 1.1 (Cluster Bus) + Phase 1.2 (State Advertisement)

Built the TCP mesh cluster bus and state advertisement — the foundation all subsequent clustering phases build on.

#### New Files
| File | Lines | Purpose |
|------|-------|---------|
| `hblink4/cluster.py` | ~400 | TCP mesh cluster bus: peer connections, HMAC-SHA256 auth, heartbeats, JSON+binary framing |
| `tests/test_cluster.py` | ~200 | 12 tests: unit + integration with real TCP connections |
| `docs/clustering_todo.md` | ~300 | Markdown TODO checklist for all 6 phases |
| `docs/generate_todo_docx.py` | ~200 | Word document generator for printable checklist |
| `docs/HBlink4_Clustering_TODO.docx` | — | Printable checklist with checkboxes |

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | ClusterBus import/init/startup/shutdown, cluster message handler, repeater_up/down broadcasts, _cluster_state dict, full state sync on peer connect |
| `config/config_sample.json` | Added `cluster` config section |
| `tests/test_access_control.py` | Complete rewrite — self-contained test data (was: 9 tests, 5 failing; now: 31 tests, all passing) |

#### Test Results
- **94 tests total, 94 passing, 0 failures**
- 12 new cluster tests + 22 new access control tests + 60 existing tests

---

### Architecture Insights

**Connection Deduplication.** In a full mesh, two nodes can connect outbound to each other simultaneously, creating duplicate TCP connections per peer pair. The tiebreaker rule — lower `node_id` keeps its outbound — is deterministic. Both sides reach the same conclusion independently without coordination. Same pattern used in BitTorrent DHT.

**Wire Format.** A 1-byte message type prefix inside the length-prefixed frame separates control from data:
- `0x00` = JSON (control plane: heartbeats, repeater_up/down, sync)
- `0x01` = binary stream data (hot path for Phase 2)
- `0x02` = stream end

The hot path avoids JSON serialization: 1B type + 4B stream_id + 55B DMRD = 60 bytes per voice packet. At 60ms DMR frame intervals, this is negligible overhead.

**EventEmitter Pattern Reuse.** The existing `events.py` uses length-prefixed JSON framing over TCP for dashboard communication. The cluster bus reuses the same framing concept but with asyncio stream protocols instead of raw sockets — proper event loop integration without blocking `sendall()` calls. Key difference: EventEmitter is a client connecting to one server; ClusterBus is a mesh where each node is both client and server.

**Config Hash Drift Detection.** Each heartbeat includes a SHA-256 hash of the routing-relevant config sections (repeater_configurations, blacklist, connection_type_detection). If peers have different configs, a warning is logged. This catches config drift early without enforcing consistency (which would reduce availability — the wrong tradeoff for ham radio).

**Async Test Patterns.** Integration tests use `IsolatedAsyncioTestCase` with real `asyncio.start_server` on port 0 (OS picks a free port). The tricky part was teardown: async reconnect loops with 5-second connect timeouts can block test cleanup. `stop()` uses `asyncio.wait_for(gather(...), timeout=3.0)` to prevent hangs. In production this matters less (process exits), but for tests it's critical.

**Self-Contained Test Data.** The access control tests were rewritten to define config inline via a `_make_config()` helper instead of loading `config_sample.json`. Config changes can't break tests. Each test class isolates one concern (ID matching, range matching, callsign, priority, blacklist). This is the pattern to follow for all future tests as clustering grows.

---

---

### Completed: Phase 2.1-2.4 (Cross-Server Stream Routing)

The core clustering value: DMR transmissions now cross server boundaries.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/models.py` | Added `source_node` field to StreamState |
| `hblink4/cluster.py` | Added `send_stream_start()`, `send_stream_data()`, `send_stream_end()` helpers |
| `hblink4/hblink.py` | Target calc cluster loop, `local_only` param, virtual stream handlers, cluster forwarding in `_forward_stream`, stream_end cluster notification, peer disconnect virtual stream cleanup |
| `tests/test_cluster.py` | 19 new tests: target calc (8), virtual streams (7), stream protocol over TCP (4) |

#### Sub-phase Details
- **2.1 Target Calculation** — `_calculate_stream_targets()` now loops cluster state, matches TG+slot per remote repeater, breaks on first match per peer
- **2.2 Cluster Stream Protocol** — JSON `stream_start`/`stream_end`, binary `stream_data` (4B stream_id + 55B DMRD = hot path)
- **2.3 Virtual Stream Reception** — incoming cluster streams create virtual StreamState, calculate LOCAL-ONLY targets (single-hop rule), forward DMRD to local repeaters, track assumed streams
- **2.4 Stream Lifecycle Integration** — `_forward_stream()` collects cluster targets and sends via `asyncio.ensure_future()`, `_end_stream()` notifies cluster peers, peer disconnect cleans up virtual streams

#### Test Results
- **113 tests total, 113 passing, 0 failures**
- 19 new Phase 2 tests + 12 Phase 1 cluster tests + 82 existing tests

#### Architecture Insights

**Sync/Async Bridge.** `_forward_stream()` is sync (called from `datagram_received` UDP callback) but cluster sends are async (TCP). `asyncio.ensure_future()` bridges the gap — the UDP callback doesn't block, TCP writes buffer in the kernel. For the hot path (~60ms DMR frame intervals), this avoids per-packet coroutine overhead on the UDP side.

**Single-Hop Rule.** Virtual streams from cluster peers forward to local repeaters ONLY — never re-forwarded to other cluster peers or outbound connections. This prevents routing loops in the mesh. The `local_only=True` flag on `_calculate_stream_targets()` enforces this.

**Virtual Stream Keying.** `(node_id, stream_id_bytes)` prevents stream ID collisions — different nodes can independently generate the same 4-byte stream ID. The binary hot path uses raw bytes for the key (no hex conversion on every packet).

**Redundant Stream ID.** The binary wire format duplicates stream_id: once as a 4-byte prefix (for O(1) lookup without parsing the DMRD), and again inside the 55-byte DMRD at bytes 16-20. This is intentional — the hot path reads 4 bytes from the front rather than seeking to byte 16.

---

---

### Completed: Phase 3.2 (Failure Detection) + Phase 3.3 (Graceful Shutdown)

Resilience layer: dead peer cleanup, stale virtual stream timeout, ordered shutdown with drain.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | `_draining` flag, `_draining_peers` set, RPTL rejection during drain, skip draining peers in target calc, virtual stream stale cleanup in `_check_stream_timeouts`, async `graceful_shutdown()` replacing sync `cleanup()`, `node_draining`/`node_down` cluster message handlers, `_count_active_streams()`, async signal handler |
| `tests/test_cluster.py` | 8 new tests: draining exclusion (2), virtual stream timeout (2), graceful shutdown broadcast ordering (1), node_draining/node_down handlers (3) |

#### Sub-phase Details
- **3.2 Failure Detection**: Virtual stream stale cleanup in periodic `_check_stream_timeouts()` — streams with no data for > `stream_timeout` are removed and assumed streams on targets are ended. Peer disconnect already handled (Phase 2).
- **3.3 Graceful Shutdown**: Ordered sequence: set `_draining` → broadcast `node_draining` → wait for active streams (configurable `drain_timeout`, default 30s) → send MSTCL to repeaters → broadcast `node_down` → stop cluster bus. Peers receiving `node_draining` skip that peer in target calculations. Signal handler now async.

#### Test Results
- **121 tests total, 121 passing, 0 failures**
- 8 new Phase 3 tests + 19 Phase 2 tests + 12 Phase 1 cluster tests + 82 existing tests

#### Architecture Insights

**Drain vs. Kill.** The old `cleanup()` was immediate: send MSTCL, done. The new `graceful_shutdown()` is ordered — it waits for active streams to finish before disconnecting repeaters. For ham radio, this means a QSO in progress won't get cut off during a rolling restart. The drain timeout prevents hanging forever if a stream never ends.

**Draining Peer Exclusion.** When a peer broadcasts `node_draining`, other servers stop routing NEW streams to it. Existing streams continue (the draining peer still processes them). This is Option A from the design discussion — simple, no mid-stream disruption.

**Stale Virtual Stream Cleanup.** If a peer dies without sending `stream_end` (crash, network partition), the periodic `_check_stream_timeouts` catches virtual streams that haven't received data within `stream_timeout` (default 2s). This prevents unbounded memory growth from orphaned virtual streams.

---

---

### Completed: Phase 1.3 (User Cache Sharing)

Cross-server user cache: local user_heard events are batched, throttled, and broadcast to cluster peers.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/user_cache.py` | `source_node` field on UserEntry, broadcast callback + queue, throttled `flush_broadcast()`, `merge_remote()`, `set_broadcast_callback()`, cleanup flushes queue |
| `hblink4/hblink.py` | `user_heard` cluster message handler, broadcast callback wired up on cluster bus start |
| `tests/test_cluster.py` | 12 new tests: source_node tracking, broadcast queue/throttle/batch, merge_remote, local-overwrites-remote, to_dict |

#### Test Results
- **133 tests total, 133 passing, 0 failures**

---

### Completed: Phase 1.4 + 4.2 (Dashboard Cluster View)

Backend plumbing for cluster visibility in the dashboard.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | `_emit_cluster_state()` helper, 8 call sites (peer connect/disconnect, sync_state, repeater_up/down, node_draining/down, _send_initial_state) |
| `dashboard/server.py` | `DashboardState.cluster` field, `cluster_state` event handler, `cluster` in WebSocket initial_state, `/api/cluster` REST endpoint |
| `tests/test_cluster.py` | 5 new tests: emit output, draining flag, noop without bus, zero repeaters, dashboard storage |

#### Test Results
- **138 tests total, 138 passing, 0 failures**
- 5 new Phase 1.4 tests + 46 prior cluster tests + 87 existing tests

---

### Completed: Phase 4.1 + 4.3 (Config Hot-Reload + Management Interface)

Live config reload and operational management socket.

#### New Files
| File | Purpose |
|------|---------|
| `hbctl.py` | CLI client for management socket |

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | `CONFIG_FILE` global, `_reload_config()` method (re-read config, rebuild matcher, re-evaluate repeaters, update config hash), SIGHUP handler, management Unix socket server with 5 commands (status/cluster/repeaters/reload/drain) |
| `tests/test_cluster.py` | 5 new tests: reload no-file, rebuild matcher, disconnect blacklisted, emit event, mgmt socket roundtrip |

#### Test Results
- **143 tests total, 143 passing, 0 failures**
- 5 new Phase 4 tests + 51 prior cluster tests + 87 existing tests

---

### Completed: Phase 5.1 + 5.2 (Token Auth + Subscriptions)

Cluster-native client protocol with stateless token auth and client-driven TG subscriptions.

#### New Files
| File | Lines | Purpose |
|------|-------|---------|
| `hblink4/cluster_protocol.py` | ~200 | Token, TokenManager, HMAC-SHA256 signing, wire format constants |
| `hblink4/subscriptions.py` | ~160 | SubscriptionStore with config validation, cluster replication |
| `tests/test_native_protocol.py` | ~240 | 29 tests: tokens (13) + subscriptions (16) |

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | Dual-protocol dispatch (CLNT magic), TokenManager+SubscriptionStore init, 6 native handlers (auth/subscribe/ping/data/disconnect), subscription cluster messages, broadcast wiring |

#### Test Results
- **172 tests total, 172 passing, 0 failures**
- 29 new Phase 5 tests + 56 prior cluster tests + 87 existing tests

---

### Completed: Phase 5.4 (HomeBrew Proxy Adapter)

Bridges HomeBrew repeaters into the subscription-based routing system so both protocols share one routing path.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | `_create_homebrew_subscription()`, native client loop in `_calculate_stream_targets()`, native forwarding in `_forward_stream()`, subscription cleanup in `_remove_repeater()` |
| `tests/test_cluster.py` | 9 new tests: proxy subscription (4), native target calc (4), native forwarding (1). Added `_native_clients`+`_subscriptions` to routing test mock |

#### Test Results
- **194 tests total, 194 passing, 0 failures**
- 9 new Phase 5.4 tests + 29 Phase 5.1/5.2 tests + 56 prior cluster tests + 87 existing + 13 other

---

---

### Completed: Phase 5.3 (Cluster-Aware Keepalive)

Enhanced PONG response with full cluster health for fast client failover.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | Enhanced `_handle_native_ping()` PONG: node_id, active_streams, connected_repeaters, per-peer status/load, preferred_server hint |
| `tests/test_cluster.py` | 9 new tests: node_id (2), active_streams, peer status alive/draining/dead, preferred_server selection, no preferred when all draining, redirect on drain |

#### Test Results
- **203 tests total, 203 passing, 0 failures**

---

---

### Completed: Phase 5.5 (Multi-Connect Server-Side Dedup)

Server-side stream dedup for clients connected to multiple servers simultaneously.

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | `_is_stream_active_locally()`, dedup in `_handle_virtual_stream_start()`, stale virtual cleanup in `_handle_stream_start()` |
| `tests/test_cluster.py` | 6 new tests: virtual stream suppression (3), `_is_stream_active_locally` (2), stale virtual cleanup (1) |

#### Test Results
- **209 tests total, 209 passing, 0 failures**

---

---

### Completed: Phase 6.1-6.3 (Backbone Bus, TG Routing, Hierarchical Forwarding)

Inter-region gateway bus with TG-based routing table for global scale.

#### New Files
| File | Lines | Purpose |
|------|-------|---------|
| `hblink4/backbone.py` | ~500 | BackboneBus, TalkgroupRoutingTable, RegionalTGSummary |
| `tests/test_backbone.py` | ~300 | 20 tests: TG routing, TG summary, bus init, TCP integration |

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | BackboneBus init, `_handle_backbone_message()`, `_advertise_tg_summary()`, backbone stream handlers, backbone targets in `_calculate_stream_targets()` + `_forward_stream()` + `_end_stream()`, TG re-advertisement on repeater up/down, backbone shutdown |
| `config/config_sample.json` | `cluster.region`+`cluster.role` fields, `backbone` config section |
| `tests/test_cluster.py` | 4 new backbone target calc tests, `_backbone_bus`+`_region_id` on test mocks |

#### Test Results
- **233 tests total, 233 passing, 0 failures**
- 20 backbone tests + 4 backbone target tests + 209 existing

---

---

### What's Next

Per the recommended implementation order:

| Order | Phase | Status |
|-------|-------|--------|
| 1 | 1.1 + 1.2 (Cluster Bus + State Advertisement) | **DONE** |
| 2 | 2.1 - 2.4 (Cross-Server Stream Routing) | **DONE** |
| 3 | 3.2 + 3.3 (Failure Detection + Graceful Shutdown) | **DONE** |
| 4 | 1.3 (User Cache Sharing) | **DONE** |
| 5 | 3.1 (DNS/Config Failover Docs) | **DONE** |
| 6 | 1.4 + 4.2 (Dashboard Cluster View) | **DONE** |
| 7 | 4.1 + 4.3 (Config Hot-Reload + Management Commands) | **DONE** |
| 8 | 5.1 + 5.2 (Token Auth + Subscriptions) | **DONE** |
| 9 | 5.4 (HomeBrew Proxy Adapter) | **DONE** |
| 10 | 5.3 (Cluster-Aware Keepalive) | **DONE** |
| 11 | 5.5 (Multi-Connect Server-Side Dedup) | **DONE** |
| 12 | 6.1-6.3 (Backbone + TG Routing + Hierarchical Forwarding) | **DONE** |
| 13+ | 6.4-6.6 (Cross-Region User Lookup, Gateway Failover, Hardening) | Pending |
