# HBlink4 Clustering Implementation TODO

A detailed, ordered checklist for implementing clustering, the cluster-native protocol, and global-scale hierarchical routing. Derived from the architecture documents in this directory.

---

## Recommended Implementation Order

| Order | Phase | What You Get | Status |
|-------|-------|-------------|--------|
| 1 | 1.1 + 1.2 | Cluster bus running, servers see each other's repeaters | DONE |
| 2 | 2.1 - 2.4 | Cross-server stream routing (core value) | DONE |
| 3 | 3.2 + 3.3 | Failure detection + graceful shutdown | DONE |
| 4 | 1.3 | User cache sharing (private calls across servers) | DONE |
| 5 | 3.1 | DNS/config failover documentation (free) | DONE |
| 6 | 1.4 + 4.2 | Dashboard shows cluster state | DONE |
| 7 | 4.1 + 4.3 | Config hot-reload + management commands | DONE |
| 8 | 5.1 + 5.2 | Token auth + subscriptions | DONE |
| 9 | 5.4 | HomeBrew proxy adapter (unified routing path) | DONE |
| 10 | 5.3 | Cluster-aware keepalive (fast failover) | DONE |
| 11 | 5.5 | Multi-connect (defer until needed) | DONE |
| 12 | 6.1 - 6.5 | Global scale backbone, TG routing, user lookup, failover | DONE |
| 13 | 6.6 | Backbone hardening (TLS, key rotation, priority queues) | TODO |
| **14** | **7.1** | **Cluster topology protocol — server-pushed failover for repeaters/clients** | **NEXT** |

## Next Up

| Priority | Task | Description |
|----------|------|-------------|
| **1** | Phase 7.1: Cluster topology protocol | Server-pushed topology to repeaters (`RPTCL`) and clients (`CLNT_TOPO`) for instant failover. Live updates on cluster change. Forced re-registration. See `docs/cluster_topology_protocol.md`. |
| 2 | 6.6 Backbone hardening | Separate secrets, key rotation, TLS, priority queues |

---

## Phase 1: Cluster Bus and State Awareness

**Goal:** Servers know about each other and share connection state. No cross-server routing yet.

### 1.1 Cluster Bus (LOW RISK)

**New file:** `nexus/cluster.py` (~400 lines)
**Modified:** `nexus/hblink.py` (init, cleanup), `nexus/config.py` (schema)

- [ ] Create `ClusterBus` class with TCP full-mesh peer management
- [ ] Implement length-prefixed JSON framing (reuse `EventEmitter` pattern from `events.py`)
- [ ] HMAC-SHA256 authentication on peer connect using `shared_secret`
- [ ] Heartbeat send/receive loop (`heartbeat_interval`, default 2s)
- [ ] Dead peer detection (`dead_threshold`, default 6s)
- [ ] `PeerConnection` dataclass: connected state, last heartbeat, latency
- [ ] `broadcast(msg)` method: send to all connected peers
- [ ] `send(node_id, msg)` method: send to specific peer
- [ ] `is_peer_alive(node_id)` method: health check
- [ ] Auto-reconnect logic for dropped peer connections
- [ ] IPv4/IPv6 dual-stack support (reuse `normalize_addr()` from `utils.py`)
- [ ] Add `cluster` config section to `config.py`:
  - `enabled`, `node_id`, `bind`, `port`
  - `peers[]` (each with `node_id`, `address`, `port`)
  - `shared_secret`, `heartbeat_interval`, `dead_threshold`
- [ ] Integrate into `hblink.py`:
  - Instantiate `ClusterBus` in `HBProtocol.__init__`
  - Start cluster bus after server bind
  - Clean up on shutdown
- [ ] Unit tests: peer connect/disconnect, heartbeat timeout, auth rejection

### 1.2 State Advertisement (LOW RISK)

**Modified:** `nexus/hblink.py`, `nexus/models.py`

- [ ] Create `RemoteRepeaterInfo` dataclass in `models.py`
- [ ] On repeater auth complete (`_handle_rptk`), broadcast `repeater_up`:
  - `node_id`, `repeater_id`, `callsign`
  - `slot1_talkgroups`, `slot2_talkgroups`, `connection_type`
- [ ] On repeater disconnect, broadcast `repeater_down`
- [ ] Maintain `_cluster_state: Dict[str, Dict[int, RemoteRepeaterInfo]]` on `HBProtocol`
- [ ] Handle incoming `repeater_up` / `repeater_down`: update `_cluster_state`
- [ ] On peer death, purge that peer's entries from `_cluster_state`
- [ ] On peer connect, full state sync (send all local repeaters, receive theirs)
- [ ] Include config hash in heartbeat for drift detection (warning log on mismatch)

### 1.3 User Cache Sharing (LOW RISK)

**Modified:** `nexus/user_cache.py`, `nexus/hblink.py`

- [ ] Add `source_node` field to `UserCache` entries
- [ ] On `UserCache.update()`, queue cluster broadcast of `user_heard` event
- [ ] Batch/throttle user cache broadcasts: max 1 per second per peer
- [ ] Receiving servers merge remote user cache entries with `source_node` tag
- [ ] TTL respects remote entries same as local
- [ ] Unit tests: cross-node user lookup, TTL expiration, batch throttling

### 1.4 Dashboard Cluster View (LOW RISK)

**Modified:** `nexus/events.py`, `dashboard/server.py`

- [ ] Add `cluster_state` event type in `EventEmitter`
- [ ] Send cluster state updates to dashboard on peer connect/disconnect/heartbeat
- [ ] Dashboard: new "Cluster" panel showing all nodes, their repeaters, health status
- [ ] Dashboard: unified "Last Heard" table combining all nodes' user caches
- [ ] Dashboard: per-peer latency display

---

## Phase 2: Cross-Server Stream Routing

**Goal:** A transmission on Server A reaches Server B's repeaters if they share talkgroups.

### 2.1 Cross-Server Target Calculation (MEDIUM RISK)

**Modified:** `nexus/hblink.py` (`_calculate_stream_targets`)

- [ ] Extend `_calculate_stream_targets()` with cluster state loop:
  - After local repeater loop and outbound loop
  - For each alive peer, check if any of its repeaters match stream's TG + slot
  - Add `('cluster', peer_node_id)` to target set
  - `break` after first match per peer (route to servers, not repeaters)
- [ ] Dead peer exclusion: skip peers where `is_peer_alive()` returns False
- [ ] Unit tests: target calculation with mock cluster state, TG matching

### 2.2 Cluster Stream Protocol (MEDIUM RISK)

**Modified:** `nexus/cluster.py`

- [ ] Define wire formats:
  - `stream_start`: JSON envelope with `stream_id`, `slot`, `dst_id`, `rf_src`, `call_type`, `source_node`, `source_repeater`
  - `stream_data` (hot path): `[1B msg_type=0x01][4B stream_id][55B DMRD]` = 60 bytes total, NO JSON
  - `stream_end`: JSON with `stream_id` and reason (terminator/timeout)
- [ ] Add binary message handling to `ClusterBus` (alongside existing JSON)
- [ ] Implement `send_stream_start()`, `send_stream_data()`, `send_stream_end()`

### 2.3 Virtual Stream Reception (MEDIUM RISK)

**Modified:** `nexus/hblink.py`

- [ ] Handle incoming `stream_start`: create virtual `StreamState` with `source_node` field
- [ ] Run `_calculate_stream_targets()` for virtual streams against LOCAL repeaters only
  - NEVER re-forward to other cluster peers (single-hop rule, prevents loops)
- [ ] Handle incoming `stream_data`: look up virtual stream by `(source_node_id, stream_id)`, forward to local targets
- [ ] Handle incoming `stream_end`: clean up virtual stream state
- [ ] Key virtual streams as `(source_node_id, stream_id)` to prevent stream ID collisions

### 2.4 Stream Lifecycle Integration (LOW RISK)

**Modified:** `nexus/hblink.py` (`_forward_stream`)

- [ ] In `_forward_stream()`: detect `('cluster', node_id)` targets, send via cluster bus
- [ ] On stream start: emit `stream_start` to relevant peers
- [ ] On every DMRD packet: emit binary `stream_data` to relevant peers (hot path)
- [ ] On stream end (terminator or timeout): emit `stream_end` to relevant peers
- [ ] Update `_update_stream_targets()` for mid-stream cluster peer changes
- [ ] Integration tests: end-to-end stream from Server A repeater to Server B repeater

---

## Phase 3: Failover and Resilience

**Goal:** When a server dies, its repeaters reconnect to a surviving server with minimal disruption.

### 3.1 Repeater Reconnection -- DNS (NO CODE CHANGES)

- [ ] Document DNS-based failover (multiple A/AAAA records)
- [ ] Document config-based failover (multiple server addresses in repeater firmware)
- [ ] Optional: implement `MSTRDR` (Master Redirect) message for graceful redirect

### 3.2 Peer Failure Detection (LOW RISK)

**Modified:** `nexus/cluster.py`, `nexus/hblink.py`

- [ ] On peer declared dead:
  - Terminate all virtual streams sourced from that peer
  - Log warning with peer `node_id` and last heartbeat time
  - Keep dead peer's user cache entries but mark stale
- [ ] On peer reconnect: trigger full state re-sync

### 3.3 Graceful Shutdown (MEDIUM RISK)

**Modified:** `nexus/hblink.py`

- [ ] Handle `SIGTERM` with ordered shutdown:
  1. Stop accepting new repeater logins (reject `RPTL`)
  2. Broadcast `node_draining` to cluster peers
  3. Wait for active streams to end (configurable drain timeout, default 30s)
  4. Send `MSTCL` (disconnect) to all connected repeaters
  5. Broadcast `node_down` to cluster peers
  6. Shutdown
- [ ] Peers receiving `node_draining`: stop routing new streams to that node
- [ ] Peers receiving `node_down`: remove node from cluster state

### 3.4 Split-Brain Handling (LOW RISK)

- [ ] Document split-brain behavior (each partition routes independently)
- [ ] On partition heal: re-sync state automatically
- [ ] Optional: configurable quorum requirement for new repeater acceptance

---

## Phase 4: Shared Configuration and Operational Tools

**Goal:** Reduce operational burden of managing multiple servers.

### 4.1 Config Hot-Reload (MEDIUM RISK)

**Modified:** `nexus/hblink.py`, `nexus/config.py`, `nexus/access_control.py`

- [ ] Add `SIGHUP` handler to `hblink.py`
- [ ] Reload `repeater_configurations`, `blacklist`, `connection_type_detection` from config file
- [ ] Re-evaluate existing connections against new config (disconnect if now blacklisted)
- [ ] Update `RepeaterMatcher` in `access_control.py` to support runtime replacement
- [ ] Document ansible/git-pull + SIGHUP workflow for config distribution

### 4.2 Cluster-Aware Dashboard (LOW RISK)

**Modified:** `dashboard/server.py`

- [ ] Cluster summary endpoint aggregating `_cluster_state`
- [ ] Show cross-server stream routing (stream on A forwarded to B)
- [ ] Show cluster bus latency between peers
- [ ] Per-node repeater count and load display

### 4.3 Operational Management Interface (LOW RISK)

**New:** Management interface (Unix socket or localhost HTTP)

- [ ] `cluster status`: show peers, connection state, latency, config hash
- [ ] `cluster drain`: trigger graceful shutdown sequence
- [ ] `cluster rejoin`: re-announce after maintenance
- [ ] `repeaters`: list all repeaters cluster-wide with owning server

---

## Phase 5: Cluster-Native Client Protocol

**Goal:** Optional new protocol removing HomeBrew's clustering limitations. Runs alongside HomeBrew on a separate port.

### 5.1 Token Auth and Validation (MEDIUM RISK)

**New file:** `nexus/cluster_protocol.py` (~300 lines)
**Modified:** `nexus/hblink.py` (dual protocol dispatch)

- [ ] Token structure: `{repeater_id, allowed_tg_slot1, allowed_tg_slot2, issued_at, expires_at, cluster_id}`
- [ ] HMAC-SHA256 signing with cluster-wide `shared_secret`
- [ ] `AUTH` handler: verify credentials against shared config, issue signed token
- [ ] Any-server token validation: check HMAC signature + expiry, no server-local state
- [ ] Token cache: `{repeater_id -> validated_token}` with short TTL
- [ ] 4-byte `token_hash` prefix for fast per-packet validation
- [ ] Dual protocol dispatch in `datagram_received()`: check magic bytes to route to HomeBrew or native path

### 5.2 Subscription Store (MEDIUM RISK)

**New file:** `nexus/subscriptions.py` (~200 lines)
**Modified:** `nexus/hblink.py`, `nexus/cluster.py` (replication)

- [ ] `SubscriptionStore` class with `subscribe()`, `get_subscribers()`, `replicate_to()`
- [ ] Client sends `SUBSCRIBE` with per-slot talkgroup lists
- [ ] Server validates against config (reject unauthorized TGs)
- [ ] Store replicated across cluster bus (small data, infrequent changes)
- [ ] On reconnect to different server: client re-sends `SUBSCRIBE`, immediately routing-ready

### 5.3 Cluster-Aware Keepalive (LOW RISK)

**Modified:** `nexus/cluster_protocol.py`

- [ ] `PONG` response includes:
  - `cluster_health[]`: node_id, status, load per peer
  - `your_preferred_server`: optional load-balancing hint
  - `redirect_to`: non-null triggers client reconnect (graceful drain)
- [ ] Client knows all healthy peers before failover happens
- [ ] Failover drops to ~2-3 seconds (1 missed ping + immediate reconnect)

### 5.4 HomeBrew Proxy Adapter (MEDIUM RISK)

**Modified:** `nexus/hblink.py`

- [ ] HomeBrew handler becomes thin translation layer:
  1. Perform traditional challenge-response auth
  2. Create session token internally
  3. Convert config-assigned TGs to a subscription
  4. Route through cluster-native path from there
- [ ] Single routing code path serves both protocols
- [ ] Legacy repeaters continue working with zero changes

### 5.5 Multi-Connect Support (HIGH RISK -- DEFER)

**Modified:** Client-side only; `nexus/hblink.py` (server-side dedup)

- [ ] Client connects to two servers simultaneously (primary for TX, both for RX)
- [ ] Client-side stream dedup by `stream_id`
- [ ] Server-side dedup for TX packets (same repeater on multiple servers)
- [ ] Zero-downtime failover on primary loss
- [ ] **Defer until clear need exists**

---

## Phase 6: Global Scale -- Hierarchical Routing

**Goal:** Scale beyond ~10-15 nodes. Regional clusters connected by backbone. Clean layer on top of Phases 1-5. Defer until needed.

### 6.1 Region Config and Backbone Bus (MEDIUM RISK)

**New file:** `nexus/backbone.py` (~300 lines)
**Modified:** `nexus/config.py`

- [ ] Add `region` and `role` fields to cluster config
- [ ] `BackboneBus` class: TCP connections to gateways in other regions only
- [ ] Separate backbone port (62033) from intra-region cluster port (62032)
- [ ] Separate `backbone_secret` from regional `shared_secret`
- [ ] Gateway node allowlisting (reject unknown `node_id` / `region`)

### 6.2 Talkgroup Routing Table (MEDIUM RISK)

**Modified:** `nexus/backbone.py`

- [ ] `TalkgroupRoutingTable` with `RegionalTGSummary` dataclass
- [ ] Gateway computes regional TG union from all local + intra-region repeaters
- [ ] Broadcast TG summary to backbone peers only when the set changes
- [ ] `None` (wildcard) TGs NEVER propagated: treat as union of explicit TGs
- [ ] Log warning if TG summary exceeds 500 entries per slot
- [ ] Empty summary = no backbone routing for that region

### 6.3 Hierarchical Stream Forwarding (MEDIUM RISK)

**Modified:** `nexus/hblink.py` (`_calculate_stream_targets`, `_forward_stream`)

- [ ] Third loop in `_calculate_stream_targets()`: check TG routing table, add `('backbone', region_id)` targets
- [ ] Non-gateway servers route backbone targets through local gateway
- [ ] Gateway forwards to destination region gateways only (single-hop, no transitive relay)
- [ ] `origin_region` field on backbone packets; receiving gateway distributes internally only

### 6.4 Cross-Region User Lookup (LOW RISK)

**Modified:** `nexus/backbone.py`, `nexus/user_cache.py`

- [ ] Pull model: query fans out through gateways only on local/regional cache miss
- [ ] `UserLookupService`: `query()`, `respond()`, `cache_result()`
- [ ] Positive cache: 60s TTL
- [ ] Negative cache: 30s TTL (prevents lookup storms for unknown users)
- [ ] Stale cache invalidation on failed private call delivery + re-query

### 6.5 Gateway Failover (MEDIUM RISK)

**Modified:** `nexus/backbone.py`, `nexus/cluster.py`

- [ ] Dual gateway per region (primary + hot standby)
- [ ] Intra-region heartbeat detects primary gateway death
- [ ] Standby auto-promotes and announces to backbone peers
- [ ] Backbone reconnection handles gateway IP changes

### 6.6 Backbone Hardening (LOW RISK)

**Modified:** `nexus/backbone.py`, config changes

- [ ] Separate secrets per trust boundary (regional vs. backbone)
- [ ] Key rotation procedure with grace period (dual-key acceptance)
- [ ] Replay protection: monotonic sequence numbers on control plane messages
- [ ] Optional TLS wrapping for public internet backbone links
- [ ] Per-backbone-peer independent TCP connections (no head-of-line blocking)
- [ ] Bounded send queues per backbone peer (256 packets default)
- [ ] Priority queues: control plane > stream data
- [ ] Dashboard: queue depth and drop counts per backbone peer

---

## New Files Summary

| File | Phase | Est. Lines | Purpose |
|------|-------|-----------|---------|
| `nexus/cluster.py` | 1.1 | ~400 | TCP mesh cluster bus, peer management, heartbeat |
| `nexus/cluster_protocol.py` | 5.1 | ~300 | Cluster-native client protocol, token auth |
| `nexus/subscriptions.py` | 5.2 | ~200 | Client TG subscription store, replication |
| `nexus/backbone.py` | 6.1 | ~500 | Backbone bus, TG routing table, user lookup |
| **Total** | | **~1,400** | |

## Key Modified Files

| File | Phases | Nature of Changes |
|------|--------|------------------|
| `nexus/hblink.py` | Nearly all | Central hub: cluster init, target calc, virtual streams, shutdown, dual protocol |
| `nexus/models.py` | 1.2 | Add `RemoteRepeaterInfo` dataclass |
| `nexus/user_cache.py` | 1.3, 6.4 | `source_node` field, cross-region lookup |
| `nexus/config.py` | 1.1, 6.1 | Cluster and backbone config schema |
| `nexus/access_control.py` | 4.1 | Runtime replacement support for hot-reload |
| `nexus/events.py` | 1.4 | Cluster state event type |
| `dashboard/server.py` | 1.4, 4.2 | Cluster panel, unified last heard, latency display |

---

## Reference Documents

- `docs/clustering_plan.md` -- Full clustering design (Phases 1-5)
- `docs/cluster_native_protocol.md` -- Native protocol specification
- `docs/global_scaling.md` -- Hierarchical routing for global scale (Phase 6)
- `docs/HBlink4_Architecture_and_Clustering.docx` -- Comprehensive Word document

*Generated: 2026-03-06*
