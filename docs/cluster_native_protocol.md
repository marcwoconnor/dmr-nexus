# Cluster-Native Protocol Design

> **Prerequisite:** This document builds on the [Clustering Plan](clustering_plan.md), which covers the cluster bus, cross-server stream routing, and failover using the existing HomeBrew protocol (Phases 1-4). This document describes Phase 5 — an optional new client protocol that removes HomeBrew's clustering limitations.

What changes if we stop treating the HomeBrew protocol as sacred and redesign the client-server protocol for clustering from the start.

## What the HomeBrew Protocol Forces on Us

The current protocol has five properties that make clustering painful:

**1. Stateful authentication.** The server generates a random salt, the client hashes `salt + passphrase` and sends it back. The server that generated the salt must be the one to verify it. If a load balancer sends the login to Server A and the auth response to Server B, authentication fails. This forces session affinity — a repeater is pinned to one server.

**2. Server-side routing configuration.** The server decides what talkgroups a repeater gets based on its ID and the `repeater_configurations` patterns. The repeater doesn't declare what it wants — it sends all traffic and the server filters. This means the routing brain must live on the server that owns the connection.

**3. Keepalive is connection-scoped.** `RPTPING`/`MSTPONG` are tied to a specific server. If the server dies, the repeater must wait for 3 missed pings (default 15 seconds) before it knows something is wrong and tries to reconnect.

**4. Repeater ID as primary key.** A repeater is identified by its 4-byte ID. The server stores all state (auth, config, streams, slots) keyed by this ID. Two servers can't both hold state for the same repeater ID without conflicting.

**5. No packet-level addressing.** A DMRD packet contains source, destination, and the originating repeater ID, but nothing about *which server* or *which cluster* should handle it. The server infers routing from its local state. A packet arriving at a server that doesn't own that repeater is meaningless.

## What a Cluster-Native Protocol Would Look Like

### Core idea: Stateless packet handling

If every packet carries enough information for *any* server to route it, you don't need session affinity. Any server in the cluster can handle any packet. This turns the cluster into a pool behind a standard UDP load balancer.

### Design

#### Authentication: Token-based, verify anywhere

Replace the challenge-response handshake with a token system:

1. Client sends `AUTH` with repeater ID + passphrase hash (or a pre-shared API key, or a signed JWT).
2. Any server can verify this because the credentials are in the shared config (which all cluster members have).
3. Server responds with a **session token** — a signed, time-limited blob containing:
   ```
   {repeater_id, allowed_tg_slot1, allowed_tg_slot2, issued_at, expires_at, cluster_id}
   ```
   Signed with a cluster-wide key (HMAC-SHA256 with the `shared_secret`).
4. Client attaches the token to every subsequent packet (or at least every N seconds for efficiency).
5. **Any server** can validate the token by checking the signature and expiry. No server-local state needed for auth.

Token overhead: ~64 bytes (32-byte payload + 32-byte HMAC). For voice packets at 60ms intervals, this is negligible. To reduce overhead further, the client can send the token once per second and the server caches `{repeater_id -> validated_token}` with a short TTL.

#### Routing: Client-declared subscriptions

Instead of the server looking up talkgroup lists for each repeater, the client declares what it wants:

```
SUBSCRIBE repeater_id=312100 slot1=[8,9] slot2=[3120,3121]
```

The server validates this against the config (you can't subscribe to a TG you're not authorized for) and stores the subscription. The critical difference: **subscriptions are pure data, not server-local state.** They can be stored in a shared lightweight store (even just replicated across the cluster bus from Phase 1 of the previous plan) or re-declared by the client on reconnection to a different server.

When the client reconnects to a different server after failover, it re-sends `SUBSCRIBE` and is immediately routing-ready. No multi-step handshake needed.

#### Packet format: Self-describing, routable

Extend the DMRD packet to carry routing metadata:

```
Current HomeBrew DMRD (55 bytes):
  [4B command][1B seq][3B src][3B dst][4B repeater_id][1B bits][4B stream_id][33B payload]

New packet (variable, ~70-80 bytes):
  [2B magic][1B version][1B flags][1B seq][3B src][3B dst][4B repeater_id]
  [1B bits][4B stream_id][33B payload]
  [4B token_hash]        <- first 4 bytes of session token HMAC (fast validation)
  [optional extensions]  <- future-proofing
```

The `token_hash` field lets any server do a fast lookup: "is this a valid session?" by checking the 4-byte prefix against cached validated tokens. Full token re-validation happens periodically, not per-packet.

The `flags` byte indicates:
- Bit 0: Has token (full token follows payload)
- Bit 1: Is cluster-forwarded (came from another server, don't re-forward)
- Bit 2-3: Priority level
- Bit 4: Subscription update follows

#### Keepalive: Cluster-aware, fast failover

Replace `RPTPING`/`MSTPONG` with a smarter heartbeat:

```
Client sends PING to whichever server it's connected to.
Server responds PONG with:
  {
    cluster_health: [
      {node_id: "east-1", status: "healthy", load: 12},
      {node_id: "west-1", status: "healthy", load: 8},
    ],
    your_preferred_server: "west-1",  // optional: hint for load balancing
    redirect_to: null                  // non-null = please reconnect here
  }
```

This gives the client visibility into the cluster. If its server dies, the client already knows the other servers' addresses and can reconnect immediately — no DNS lookup, no waiting for timeout. The `redirect_to` field enables graceful drain: a server about to shut down tells its clients where to go.

**Failover time drops from ~15 seconds (3 missed pings) to ~2-3 seconds (1 missed ping + immediate reconnect to known-healthy peer).**

#### Multi-connect mode (optional, advanced)

The ultimate optimization: the client connects to **two servers simultaneously**. One is primary (for TX), both send RX. If the primary dies, the client promotes the secondary instantly — zero downtime for received audio, at most one lost TX packet.

```
Client:
  Primary:   east-1  (TX + RX)
  Secondary: west-1  (RX only, hot standby)

On primary failure:
  Promote west-1 to primary (TX + RX)
  Connect to central-1 as new secondary
```

This requires deduplication on the client side (it'll receive the same stream from both servers during normal operation), but DMR stream IDs make dedup trivial — same stream_id = same audio, drop the duplicate.

## What Changes in HBlink4

### New module: `nexus/cluster_protocol.py`

Handles the new packet format, token validation, and subscription management. Lives alongside the existing `protocol.py` — the server can speak both protocols simultaneously on different ports.

### Modified `hblink.py`

The `HBProtocol` class gains a parallel code path:

```python
def datagram_received(self, data, addr):
    if data[:2] == NEW_MAGIC:
        self._handle_cluster_native_packet(data, addr)
    elif data[:4] in (DMRD, RPTL, RPTK, ...):
        self._handle_homebrew_packet(data, addr)  # existing code
```

The cluster-native path skips the multi-step state machine (no login/challenge/config/options dance). It validates the token, checks the subscription, and routes. Three function calls instead of a state machine.

### Modified `_calculate_stream_targets()`

No changes needed — it already works on the `_repeaters` dict. Cluster-native clients just populate that dict differently (via token + subscription instead of RPTL/RPTK/RPTC).

### Modified `_forward_stream()`

Minimal changes. Cluster-forwarded packets get the "cluster-forwarded" flag set so they're never re-forwarded.

### New: Subscription store

```python
class SubscriptionStore:
    """Manages client talkgroup subscriptions, replicated across cluster."""

    def subscribe(self, repeater_id, slot, talkgroups): ...
    def get_subscribers(self, slot, talkgroup) -> Set[bytes]: ...
    def replicate_to(self, peer): ...
```

This replaces the current pattern of computing talkgroups from `repeater_configurations` at auth time. The config still defines what's *allowed*, but the client declares what it *wants* within those bounds.

The subscription store is the one piece of shared state that matters for routing. It's small (number of repeaters * number of TGs, maybe a few KB), changes infrequently (only on connect/disconnect/resubscribe), and is easily replicated over the cluster bus.

## Migration Strategy

### Phase 1: Dual protocol support

Run both HomeBrew (port 62031) and cluster-native (port 62032) simultaneously. Existing repeaters continue working unchanged. New clients or upgraded firmware use the new protocol.

### Phase 2: Proxy mode for legacy clients

For HomeBrew clients that can't be upgraded, the server acts as a protocol translator:

```
Legacy repeater --[HomeBrew]--> Server --[internal cluster-native path]--> Cluster
```

The HomeBrew handler becomes a thin adapter that:
1. Performs the traditional challenge-response auth
2. Creates a session token internally
3. Converts the repeater's config-assigned TGs into a subscription
4. From that point, the packet flows through the same cluster-native routing path

This means the cluster routing logic only needs to exist once (the cluster-native path), and HomeBrew support is just a translation layer on ingress.

### Phase 3: Client library / firmware patch

Provide a reference implementation of the cluster-native protocol as:
- A Python client library (for hotspot software like MMDVM_Bridge, Pi-Star)
- Documentation for firmware developers
- A standalone proxy process that speaks HomeBrew to local repeaters and cluster-native to the server (for hardware repeaters that can't be reflashed)

## Comparison: HomeBrew Clustering vs. Native Clustering

| Property | HomeBrew + Cluster Bus (previous plan) | Cluster-Native Protocol |
|----------|---------------------------------------|------------------------|
| Failover time | ~15 seconds (ping timeout) | ~2-3 seconds (or zero with multi-connect) |
| Auth on failover | Full re-handshake (RPTL/RPTK/RPTC/RPTO) | Re-send token + SUBSCRIBE (1 round trip) |
| Load balancing | Not possible (session affinity) | Standard UDP load balancer works |
| Cross-server routing | Cluster bus forwards streams between servers | Same, but subscription store makes target calculation faster |
| Server state per client | Full RepeaterState (auth machine, salt, config, metadata) | Token + subscription (stateless, reconstructable) |
| Packet overhead | 55 bytes (unchanged) | ~70 bytes (+15 bytes for token hash + flags) |
| Backward compatibility | 100% (no client changes) | Requires client upgrade or proxy |
| Implementation complexity | Medium (cluster bus + forwarding) | High (new protocol + dual support + migration) |
| Operational complexity | Low (transparent to clients) | Medium (two protocols, token management) |

## Recommendation

**Build both, in order.**

1. **First**: Implement the HomeBrew clustering plan (previous document). This gives you cross-server routing and basic resilience with zero client changes. Every existing repeater and hotspot works immediately.

2. **Second**: Add the cluster-native protocol as an additional listener. Clients that can upgrade get faster failover, load balancing, and multi-connect. Legacy clients continue on HomeBrew through the proxy adapter.

The HomeBrew clustering plan is the 80% solution — it gets cross-server routing working without touching clients. The cluster-native protocol is the remaining 20% that unlocks fast failover and true load balancing, but requires client changes that will take time to propagate through the ecosystem.

Don't skip step 1. The cluster bus infrastructure (peer mesh, state replication, stream forwarding) is needed by both approaches. The cluster-native protocol just gives clients a better way to *connect to* the cluster — the inter-server mechanics are the same.
