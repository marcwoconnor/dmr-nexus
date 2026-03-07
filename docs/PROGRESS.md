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

### What's Next

Per the recommended implementation order:

| Order | Phase | Status |
|-------|-------|--------|
| 1 | 1.1 + 1.2 (Cluster Bus + State Advertisement) | **DONE** |
| 2 | 2.1 - 2.4 (Cross-Server Stream Routing) | Next |
| 3 | 3.2 + 3.3 (Failure Detection + Graceful Shutdown) | Pending |
| 4 | 1.3 (User Cache Sharing) | Pending |
| 5 | 3.1 (DNS/Config Failover Docs) | Pending |
| 6 | 1.4 + 4.2 (Dashboard Cluster View) | Pending |
| 7+ | Phases 4-6 | Pending |

Phase 2 is the core value — transmissions crossing server boundaries. The cluster bus infrastructure from Phase 1.1/1.2 provides the transport; Phase 2 adds the routing logic.
