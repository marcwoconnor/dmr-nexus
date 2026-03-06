# HBlink4 Clustering Plan

A plan to extend HBlink4 from a single-server architecture into a clustered, fault-tolerant, resilient system.

## Current State Analysis

### What exists today

HBlink4 is a single-process server where all state lives in memory within one `HBProtocol` instance:

- **`_repeaters`**: Dict of `RepeaterState` objects keyed by 4-byte repeater ID. Each holds authentication state, talkgroup sets, and per-slot stream tracking.
- **`_outbounds`**: Dict of `OutboundState` objects for server-to-server links. These are dumb pipes — one server connects to another *as a repeater*, with no awareness that the remote is a peer.
- **`_user_cache`**: Local `UserCache` mapping radio IDs to last-heard repeater. TTL-based expiration. Used for private call routing.
- **`_active_calls`**: Simple integer counter.
- **`_denied_streams`**: Dedup tracker for denied stream logging.
- **Routing cache**: Per-stream `target_repeaters` set computed once at stream start, stored on `StreamState`.

### What makes clustering hard

1. **Real-time constraints.** DMR voice packets arrive every ~60ms (one per TDMA frame). The forwarding loop must not block. Any cluster coordination on the hot path would destroy latency.

2. **TDMA slot semantics.** Each repeater has exactly 2 timeslots. Each slot carries one stream at a time. Hang time reserves a slot for the same conversation after a stream ends. Two servers independently making slot decisions for the same repeater would corrupt audio.

3. **Stream routing is stateful.** The `target_repeaters` set is computed at stream start and cached. Mid-stream changes (repeater connects/disconnects) are handled by mutating this set. A second server would need the same view of this set.

4. **Authentication is stateful.** The login handshake uses a random salt challenge stored in `RepeaterState`. The server that generated the salt must verify the hash response.

5. **No idempotency.** Forwarding the same DMR packet twice to a repeater would cause audio glitches. Exactly-once delivery matters.

---

## Design Principles

1. **Each repeater connects to exactly one server at a time.** No split-brain for individual repeaters. A repeater's "owner" server handles all its authentication, stream tracking, and slot management.

2. **Cluster coordination happens between streams, not during them.** The hot path (packet forwarding) stays local. Cross-server communication happens at stream boundaries and for state synchronization.

3. **Eventual consistency for non-critical state.** User cache, connection metadata, and dashboard state can be slightly stale. Stream ownership and slot state must be strongly consistent for the owning server.

4. **Graceful degradation.** If the cluster bus fails, each server continues operating independently with its local repeaters. Cross-server routing stops but local routing continues.

5. **No external dependencies for the critical path.** No database, no message broker, no distributed lock service in the packet forwarding loop. Cluster state is exchanged directly between servers.

---

## Architecture

### Cluster Topology

```
                    Cluster Bus (TCP mesh)
            +-----------+-----------+
            |           |           |
        +---+---+   +---+---+   +---+---+
        | Srv A |   | Srv B |   | Srv C |
        +---+---+   +---+---+   +---+---+
          / \           |           |  \
    Rpt1  Rpt2       Rpt3       Rpt4  Rpt5
```

Every server maintains a **full mesh TCP connection** to every other server. For small clusters (2-5 nodes, which is realistic for ham radio networks), full mesh is simple and sufficient. Each connection is a persistent, length-prefixed JSON stream — the same framing pattern already used by `EventEmitter`.

### Server Identity

Each server gets a unique `node_id` (string, e.g., `"east-1"`, `"west-1"`) and a `cluster` section in config:

```json
{
    "cluster": {
        "enabled": true,
        "node_id": "east-1",
        "bind": "0.0.0.0",
        "port": 62032,
        "peers": [
            {"node_id": "west-1", "address": "10.0.1.2", "port": 62032},
            {"node_id": "central-1", "address": "10.0.1.3", "port": 62032}
        ],
        "shared_secret": "cluster-auth-key",
        "heartbeat_interval": 2.0,
        "dead_threshold": 6.0
    }
}
```

---

## Phase 1: Cluster Bus and State Awareness

**Goal:** Servers know about each other and share connection state. No cross-server routing yet.

### 1.1 New module: `hblink4/cluster.py`

A `ClusterBus` class that manages peer connections:

```python
class ClusterBus:
    """Manages TCP mesh connections to peer servers."""

    def __init__(self, node_id, config, on_message_callback):
        self._node_id = node_id
        self._peers: Dict[str, PeerConnection] = {}
        self._on_message = on_message_callback

    async def start(self): ...        # Connect to all peers, accept inbound
    async def broadcast(self, msg): ...  # Send to all connected peers
    async def send(self, node_id, msg): ...  # Send to specific peer
    def is_peer_alive(self, node_id): ...
```

Uses the same length-prefixed framing as `EventEmitter`. Authenticates with HMAC-SHA256 on connect (shared secret). Heartbeats detect dead peers.

### 1.2 State advertisement

When a repeater completes authentication, the owning server broadcasts:

```json
{
    "type": "repeater_up",
    "node_id": "east-1",
    "repeater_id": 312100,
    "callsign": "N0MJS",
    "slot1_talkgroups": [8, 9],
    "slot2_talkgroups": [3120, 3121],
    "connection_type": "repeater"
}
```

On disconnect: `repeater_down`. Each server maintains a `_cluster_state` dict:

```python
# What other servers have connected
_cluster_state: Dict[str, Dict[int, RemoteRepeaterInfo]] = {
    "west-1": {312200: RemoteRepeaterInfo(...)},
    "central-1": {312300: RemoteRepeaterInfo(...)}
}
```

### 1.3 User cache sharing

When `UserCache.update()` is called, the owning server also broadcasts:

```json
{
    "type": "user_heard",
    "node_id": "east-1",
    "radio_id": 3121234,
    "repeater_id": 312100,
    "slot": 1,
    "talkgroup": 3120,
    "timestamp": 1709740800.0
}
```

Peers merge this into their local user cache with a `source_node` field. This gives every server a cluster-wide view of user locations for private call routing.

**Throttle:** Batch user updates every 1 second per peer to avoid overwhelming the cluster bus during high traffic.

### 1.4 Dashboard integration

The dashboard gains a "Cluster" view showing all nodes, their connected repeaters, and health status. Each server's `EventEmitter` already sends events to its local dashboard — add cluster state as a new event type.

### What this phase does NOT do

- No cross-server packet routing. Repeaters only hear traffic from other repeaters on the same server.
- No failover. If a server dies, its repeaters lose connectivity until they manually reconnect elsewhere.

---

## Phase 2: Cross-Server Stream Routing

**Goal:** A transmission on Server A's repeater reaches Server B's repeaters if they share talkgroups.

### 2.1 The cross-server forwarding decision

When `_calculate_stream_targets()` runs at stream start, it currently iterates only local `_repeaters` and `_outbounds`. Extend it to also check `_cluster_state`:

```python
# In _calculate_stream_targets():

# ... existing local repeater loop ...

# Check cluster peers for matching talkgroups
for peer_node_id, peer_repeaters in self._cluster_state.items():
    if not self._cluster_bus.is_peer_alive(peer_node_id):
        continue
    for remote_rid, remote_info in peer_repeaters.items():
        # Check if remote repeater has this TG on this slot
        remote_tgs = remote_info.slot1_talkgroups if slot == 1 else remote_info.slot2_talkgroups
        if remote_tgs is None or dst_id_int in remote_tgs:
            # At least one repeater on this peer wants the stream
            target_set.add(('cluster', peer_node_id))
            break  # One match is enough — the peer will do internal routing
```

Key insight: **we route to peer servers, not to individual remote repeaters.** The local server decides "Server B needs this stream" and sends it once. Server B handles its own internal distribution. This keeps the cross-server traffic proportional to the number of servers, not the number of repeaters.

### 2.2 Cluster stream protocol

When a stream targets a peer, the forwarding loop sends the raw DMRD packet over the cluster bus with a thin envelope:

```json
{
    "type": "stream_data",
    "stream_id": "a1b2c3d4",
    "slot": 1,
    "dst_id": 3120,
    "rf_src": 3121234,
    "call_type": "group",
    "source_node": "east-1",
    "source_repeater": 312100
}
```

Followed by the raw 55-byte DMRD packet as binary. The envelope is only sent on `stream_start` — subsequent packets for the same `stream_id` are sent as bare binary with a 1-byte message type prefix and 4-byte stream ID prefix (5 bytes overhead per packet, not a full JSON envelope).

```
Hot path wire format:
[1 byte: msg_type=0x01] [4 bytes: stream_id] [55 bytes: DMRD packet]
= 60 bytes per voice packet
```

### 2.3 Receiving cross-server streams

When Server B receives a `stream_data` from Server A:

1. It creates a **virtual stream** — a `StreamState` that didn't originate from a local repeater.
2. It runs its own `_calculate_stream_targets()` but only against its **local** repeaters (never re-forwarding to other cluster peers — this prevents routing loops).
3. It forwards the DMRD packet to its local repeaters that match.

The virtual stream has a `source_node` field to distinguish it from local streams. Cross-server streams are **never re-forwarded** to other peers. If Server A sends to Server B, Server B does not relay to Server C. Server A must send to Server C directly. This eliminates routing loops and keeps latency at exactly one hop.

### 2.4 Stream lifecycle over the cluster bus

- `stream_start`: Full envelope with metadata. Receiving server sets up virtual stream.
- `stream_data`: Bare binary (hot path). 60 bytes per packet.
- `stream_end`: Small message with stream_id and reason (terminator/timeout). Receiving server cleans up.

### 2.5 Latency budget

Current local forwarding: ~0 (UDP sendto, in-process).
Added cluster hop: TCP send (~50-100us LAN, ~1-5ms WAN) + receive + local forwarding.
DMR frame interval: ~60ms.
Budget: Even at 5ms WAN latency, that's well within one frame interval.

---

## Phase 3: Failover and Resilience

**Goal:** When a server dies, its repeaters automatically reconnect to a surviving server with minimal disruption.

### 3.1 Repeater-side reconnection

HomeBrew protocol repeaters already handle server unavailability — they send RPTPING and if they miss responses, they reconnect. The failover strategy leverages this existing behavior:

**DNS-based failover (simplest):** Configure repeaters with a DNS name that resolves to multiple A/AAAA records. When the primary server stops responding to pings, the repeater reconnects and DNS gives it a different server IP. This requires zero code changes.

**Config-based failover:** Some repeater firmware supports multiple server addresses with automatic fallback. Again, zero code changes on HBlink4 side.

**Active redirect (new feature):** When a server is shutting down gracefully, instead of sending `MSTNAK`, send a new `MSTRDR` (Master Redirect) message containing the address of a healthy peer. This requires repeater firmware support but enables instant failover without waiting for ping timeout (~15 seconds with default settings).

### 3.2 Server-side peer failure detection

The cluster bus heartbeat detects dead peers within `dead_threshold` seconds (default 6). When a peer is declared dead:

1. All virtual streams sourced from that peer are terminated.
2. The dead peer's repeaters are expected to reconnect to surviving servers (via DNS or config fallback).
3. The dead peer's user cache entries are kept but marked stale (they'll be refreshed when users transmit on their new server).

### 3.3 Graceful shutdown coordination

When a server receives SIGTERM:

1. **Stop accepting new repeater logins.** Existing connections continue.
2. **Broadcast `node_draining` to cluster peers.** Peers stop routing new streams to this node.
3. **Wait for active streams to end** (up to a configurable drain timeout, e.g., 30 seconds).
4. **Send disconnect (`MSTCL`) to all connected repeaters.** This triggers their reconnection logic.
5. **Broadcast `node_down` to cluster peers.** Peers remove this node from cluster state.
6. **Shut down.**

This gives repeaters a clean signal to reconnect rather than waiting for ping timeout.

### 3.4 Split-brain prevention

With a full mesh and small cluster sizes, true split-brain is unlikely but possible (e.g., network partition between data centers). The consequences in DMR are manageable:

- Each partition continues routing independently among its local repeaters.
- Cross-partition streams stop (cluster bus is down).
- When the partition heals, servers re-sync state. No user-visible corruption — the worst case is that some streams weren't cross-routed during the partition.

For stricter guarantees (optional): require a quorum (majority of configured peers reachable) before accepting new repeater connections. This prevents both sides of a partition from independently accepting the same repeater. However, this trades availability for consistency, which may not be desirable for ham radio where "keep working" matters more than "perfect consistency."

---

## Phase 4: Shared Configuration and Operational Tools

**Goal:** Reduce operational burden of managing multiple servers.

### 4.1 Configuration synchronization

The `repeater_configurations`, `blacklist`, and `connection_type_detection` sections should be identical across all cluster members. Options:

**Option A: Config file replication (simple).** Use an external tool (rsync, git pull, ansible) to push config to all servers. HBlink4 watches for file changes and hot-reloads. Add a `SIGHUP` handler that reloads config without restart.

**Option B: Config distribution over cluster bus (integrated).** One server is designated the "config source." On startup or config change, it broadcasts the config to peers. Peers validate and apply. This is more complex but self-contained.

Recommend **Option A** for initial implementation — it's simpler, uses existing tools, and config changes are infrequent.

### 4.2 Cluster-aware dashboard

Extend the dashboard to show:

- All servers in the cluster with health status
- Which repeaters are on which server
- Cross-server stream routing (show that a stream on Server A is being forwarded to Server B)
- Cluster bus latency between peers
- Unified "Last Heard" combining all servers' user caches

Implementation: Each server's dashboard already receives events via `EventEmitter`. Add a cluster summary endpoint that aggregates `_cluster_state`. For a unified view, one dashboard instance can query all servers' HTTP APIs, or the cluster bus can relay dashboard events.

### 4.3 Operational commands

Add a management interface (Unix socket or HTTP on localhost) for:

- `cluster status` — Show all peers, connection state, latency
- `cluster drain` — Initiate graceful shutdown sequence
- `cluster rejoin` — Re-announce this server to peers after maintenance
- `repeaters` — List all repeaters cluster-wide with their owning server

---

## Implementation Order and Effort Estimates

| Phase | Description | New Code | Changed Code | Risk |
|-------|-------------|----------|-------------|------|
| 1.1 | Cluster bus (TCP mesh, auth, heartbeat) | `cluster.py` (~400 lines) | `hblink.py` (init, cleanup) | Low |
| 1.2 | State advertisement | Additions to `cluster.py` | `hblink.py` (login/disconnect handlers emit to bus) | Low |
| 1.3 | User cache sharing | Additions to `cluster.py` | `user_cache.py` (source_node field), `hblink.py` | Low |
| 1.4 | Dashboard cluster view | Dashboard additions | `dashboard/server.py`, new template | Low |
| 2.1 | Cross-server target calculation | - | `hblink.py` (`_calculate_stream_targets`) | Medium |
| 2.2 | Stream forwarding over cluster bus | Additions to `cluster.py` | `hblink.py` (`_forward_stream`) | Medium |
| 2.3 | Virtual stream reception | - | `hblink.py` (new handler) | Medium |
| 2.4 | Stream lifecycle messages | Additions to `cluster.py` | `hblink.py` (stream start/end) | Low |
| 3.1 | Repeater reconnection (DNS) | None (documentation) | None | None |
| 3.2 | Peer failure detection | Additions to `cluster.py` | `hblink.py` (cleanup handler) | Low |
| 3.3 | Graceful shutdown | - | `hblink.py` (shutdown sequence) | Medium |
| 4.1 | Config hot-reload | `hblink.py` SIGHUP handler | `config.py`, `access_control.py` | Medium |
| 4.2 | Cluster dashboard | Dashboard additions | `dashboard/server.py` | Low |
| 5.1 | Token auth + validation | `cluster_protocol.py` (~300 lines) | `hblink.py` (dual protocol dispatch) | Medium |
| 5.2 | Subscription store | `subscriptions.py` (~200 lines) | `hblink.py`, `cluster.py` (replication) | Medium |
| 5.3 | Cluster-aware keepalive | Additions to `cluster_protocol.py` | `hblink.py` (PONG with peer list) | Low |
| 5.4 | HomeBrew proxy adapter | Additions to `hblink.py` | `hblink.py` (translate HB to native path) | Medium |
| 5.5 | Multi-connect support | Client-side only | `hblink.py` (dedup on server) | High |

### Suggested implementation sequence

1. **Phase 1.1 + 1.2** first. Get the cluster bus working and state flowing. This is the foundation everything else builds on and can be tested independently.
2. **Phase 2 (all)** next. This is the core value — cross-server routing. Without it, the cluster bus is just monitoring.
3. **Phase 3.2 + 3.3** for resilience. Failure detection and graceful shutdown.
4. **Phase 1.3** (user cache sharing) whenever private call routing across servers becomes a requirement.
5. **Phase 3.1** is free — just documentation on DNS/config-based failover.
6. **Phase 4** as operational needs arise.
7. **Phase 5** after Phases 1-3 are stable. The cluster bus and cross-server forwarding are prerequisites. Start with 5.1 + 5.2 (token auth + subscriptions), then 5.4 (HomeBrew proxy adapter so all traffic flows through the native path), then 5.3 (cluster-aware keepalive for fast failover). Phase 5.5 (multi-connect) is optional and high-risk — defer until there's a clear need.

---

## Key Design Decisions and Tradeoffs

### Why TCP for the cluster bus, not UDP?

DMR voice packets between repeaters and HBlink4 use UDP because that's the HomeBrew protocol. But the cluster bus carries *control messages and forwarded streams between trusted servers on reliable networks*. TCP gives us:
- Ordered delivery (no packet reordering causing stream corruption)
- Reliable delivery (no silent packet loss)
- Flow control (backpressure if a peer is slow)
- Connection state (know immediately when a peer disconnects)

The latency cost of TCP vs UDP is negligible on LAN (<100us) and acceptable on WAN (<5ms), well within the 60ms DMR frame budget.

### Why full mesh, not a message broker?

For 2-5 servers (realistic for ham networks), full mesh is `n*(n-1)/2` connections — that's 1-10 connections. A message broker (Redis, RabbitMQ, NATS) would add an external dependency, a failure point, and operational complexity for no benefit at this scale. If the cluster needed to grow beyond ~10 nodes, a gossip protocol or broker would make sense, but that's not a realistic requirement.

### Why route to servers, not individual remote repeaters?

If Server A has a stream for TG 3120 and Server B has 50 repeaters on TG 3120, sending the packet once to Server B (which distributes locally) means 1 cross-server packet. Sending to each remote repeater individually would mean 50 cross-server packets. The server-level routing is O(servers) not O(repeaters).

### Why no distributed consensus (Raft/Paxos)?

DMR stream routing is not a consensus problem. There's no shared write that all servers must agree on. Each server owns its local repeaters and makes independent routing decisions. The cluster bus is for *sharing information*, not for *agreeing on decisions*. The complexity and latency of consensus algorithms would be harmful here.

### Why no re-forwarding (single-hop only)?

If Server A sends a stream to Server B, and Server B re-forwards to Server C, you get:
- Doubled latency for Server C's repeaters
- Risk of routing loops (A->B->C->A)
- Complex TTL/loop-detection logic

Single-hop means Server A sends directly to both B and C. Latency is uniform. No loops possible. The cost is slightly more cluster bus traffic (one copy per peer instead of one copy total), but with 2-5 servers this is negligible.

### What about the IPv4/IPv6 dual-stack pattern?

The existing codebase already handles dual-stack for the HomeBrew protocol listener. The cluster bus should follow the same pattern — try IPv6 first, fall back to IPv4, configurable per-peer. Reuse the `normalize_addr()` utility from `utils.py`.

---

## Known Risks and Mitigations

### Risk 1: Python + asyncio under cluster fan-out load

The cluster bus adds concurrent TCP streams on top of the existing UDP forwarding and JSON event emission. Python's GIL and asyncio's single-threaded event loop could become a bottleneck under worst-case fan-out (many simultaneous streams, each forwarded to multiple peers).

**Why it's manageable:** The hot path is already proven — HBlink4 handles packet forwarding today at production load. The cluster bus adds one TCP write per peer per packet (2-4 extra writes for a typical cluster). TCP sendall on a local/LAN connection is sub-millisecond. The JSON envelope is only on stream start/end; the per-packet wire format is 60 bytes of raw binary with no serialization.

**Mitigation:** Profile Phase 2 under worst-case load before production rollout. Key metrics: event loop latency (should stay under 5ms), cluster bus write backlog, and CPU utilization. If the GIL becomes a bottleneck, the cluster bus TCP I/O could be moved to a separate thread with a queue, keeping the asyncio loop clean. This is a straightforward optimization that doesn't change the architecture.

### Risk 2: Stream ID collisions across servers

Stream IDs are 4-byte values generated by repeaters. Within a single server, collisions are effectively impossible (the repeater includes its own ID in the packet, so the tuple `(repeater_id, slot, stream_id)` is unique). But when streams are forwarded across servers, two different repeaters on different servers could theoretically generate the same stream ID simultaneously.

**Why it's unlikely:** Stream IDs are random 32-bit values. With a typical cluster handling ~10 concurrent streams, the collision probability is negligible (birthday paradox threshold is ~65,000 concurrent streams for 50% collision probability).

**Mitigation if needed:** Virtual streams (received from peers) should be keyed internally as `(source_node_id, stream_id)` rather than bare `stream_id`. This is a trivial change to the virtual stream tracking in Phase 2.3 and eliminates the theoretical risk entirely.

### Risk 3: Operational discipline for config consistency

The design requires identical `repeater_configurations`, `blacklist`, and `connection_type_detection` configs across all cluster members. Config drift between nodes could cause inconsistent routing (Server A thinks a repeater is allowed TG 3120, Server B doesn't).

**Mitigation:** Phase 4.1 addresses this directly with config hot-reload via SIGHUP, enabling standard config management tools (ansible, git pull + SIGHUP, etc.). For early phases before 4.1 is implemented, document the requirement clearly and provide a simple verification script that compares config hashes across nodes via the cluster bus. The cluster bus heartbeat could include a config hash field so drift is detected automatically.

---

## Phase 5: Cluster-Native Client Protocol

Phases 1-4 build clustering on top of the existing HomeBrew protocol without any client changes. This works but inherits HomeBrew's limitations: stateful authentication forces session affinity, server-side routing configuration can't be reconstructed without a full re-handshake, and failover takes ~15 seconds (3 missed pings).

Phase 5 introduces an optional new client protocol designed for clustering from the ground up. Key changes:

- **Token-based authentication** — Client authenticates once, receives a signed session token. Any server can validate it by checking the HMAC signature. No server-local salt state.
- **Client-declared subscriptions** — Client tells the server which talkgroups it wants per slot. Server validates against config and stores the subscription. On failover, client re-subscribes in one message instead of a multi-step handshake.
- **Cluster-aware keepalive** — PONG responses include a list of healthy cluster peers. Client already knows where to reconnect before its server dies. Failover drops to ~2-3 seconds.
- **Optional multi-connect** — Client connects to two servers simultaneously (one primary for TX, both for RX). Zero-downtime failover on primary loss.

The new protocol runs on a separate port alongside HomeBrew. Legacy clients continue on HomeBrew through a thin translation layer. The cluster bus and inter-server forwarding (Phases 1-3) are shared infrastructure used by both protocols.

See [Cluster-Native Protocol Design](cluster_native_protocol.md) for the full specification, packet formats, migration strategy, and comparison with HomeBrew-only clustering.

---

## Scaling Beyond 15 Nodes

Phases 1-5 use a full mesh topology that works well for 2-15 nodes. For global-scale deployments (dozens of servers across continents, thousands of repeaters), the architecture extends to hierarchical routing with regional clusters connected by a backbone. This is a clean layer on top of the existing design — each region is a small cluster running Phases 1-5 unchanged.

See [Global-Scale Clustering Architecture](global_scaling.md) for the hierarchical design, talkgroup routing tables, cross-region private call lookups, and scaling math.
