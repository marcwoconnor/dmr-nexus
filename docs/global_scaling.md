# Global-Scale Clustering Architecture

> **Context:** The [Clustering Plan](clustering_plan.md) covers Phases 1-5 for small-to-medium clusters (2-15 nodes). This document extends the architecture for global-scale deployment — dozens of servers across continents serving thousands of repeaters, the kind of infrastructure that networks like BrandMeister and TGIF operate at.

## Why the Small-Cluster Design Breaks Down

The Phase 1-4 design has three assumptions that don't survive global scale:

**1. Full mesh between all nodes.** At 30 nodes that's 435 TCP connections. At 50 nodes it's 1,225. The connections themselves are cheap, but every state change (repeater up/down, user heard) is broadcast to every node. A busy network with 10,000 repeaters generating frequent user cache updates becomes a firehose.

**2. Every node knows about every repeater.** The `_cluster_state` dict replicates all remote repeater info to all nodes. At 50 nodes with 500 repeaters each, that's 25,000 entries replicated everywhere. More importantly, `_calculate_stream_targets()` scans this entire structure at every stream start to find which peers need the stream. At 50 peers this is still fast, but it's O(peers) work that runs on every new transmission.

**3. Every stream is evaluated against every peer.** If Server A has a stream for TG 3120, it checks all 49 other servers to see if any of them have repeaters subscribed to TG 3120. Most of those checks return false. This is wasted work that grows linearly with cluster size.

## Design: Hierarchical Routing with Regional Clusters

### The Model

Instead of one flat mesh, organize the network into **regions**, each containing a small cluster of servers. Regions are connected by **backbone links** between designated gateway nodes.

```
                        Backbone (sparse mesh)
            +---------------+----------------+
            |               |                |
     +------+------+  +----+-----+   +------+------+
     | NA Region   |  | EU Region|   | AP Region   |
     |  gw-na-1 ---|--|--gw-eu-1-|---|--gw-ap-1    |
     |  na-2       |  |  eu-2    |   |  ap-2       |
     |  na-3       |  |  eu-3    |   |  ap-3       |
     +-------------+  +----------+   +-------------+
       |   |   |        |   |          |   |
      Rpts Rpts Rpts   Rpts Rpts      Rpts Rpts
```

### Backbone forwarding rule: single-hop, no transitive relay

This is a hard rule, equivalent to the "never re-forward" rule from Phase 2:

**A gateway forwards backbone stream packets only to destination region gateways based on the TG routing table. It never relays packets received from another gateway to a third gateway.**

Every backbone stream packet carries an `origin_region` field. When a gateway receives a backbone packet, it distributes it within its own region (Tier 1) and drops it. It never forwards it to another backbone peer. The source region's gateway is responsible for sending directly to every destination region.

This means backbone forwarding is always exactly one hop: source gateway -> destination gateway. No transitive paths, no loops, no TTL needed. The sparse mesh between gateways provides connectivity; the forwarding rule constrains how it's used.

### Three tiers of communication

**Tier 1: Intra-region (full mesh, existing design)**
Servers within a region use the Phase 1-4 full mesh cluster bus exactly as designed. A region is 2-5 nodes. This is the proven design, unchanged. Latency is low (same continent, typically same data center or nearby).

**Tier 2: Inter-region backbone (sparse mesh between gateways)**
Each region designates one or two servers as **gateways**. Gateways maintain TCP connections only to gateways in other regions — not to every node globally. A network with 10 regions has 10 gateways with 45 backbone connections, regardless of how many total servers exist. Forwarding is single-hop only (see rule above).

**Tier 3: Talkgroup routing table (replicated via backbone)**
Instead of every node knowing about every remote repeater, gateways maintain a **talkgroup routing table**: "Region EU has subscribers on TG 3120, TG 1, TG 9." This is a compact summary — hundreds of TGs, not thousands of repeaters. When a stream starts, the originating server checks local peers (Tier 1) and then asks its gateway "which regions need TG 3120?" The gateway forwards the stream to those regions' gateways, which distribute internally via Tier 1.

### How it works in practice

**A transmission from a repeater in North America on TG 3120:**

1. Repeater sends DMRD to `na-2` (its connected server).
2. `na-2` runs `_calculate_stream_targets()`:
   - Checks local peers `na-1`, `na-3` via intra-region cluster state (Tier 1). Finds `na-1` has repeaters on TG 3120. Adds `na-1` to targets.
   - Checks regional gateway `gw-na-1`: "who else needs TG 3120?" Gateway knows EU and AP regions have TG 3120 subscribers. Adds `('backbone', 'eu')` and `('backbone', 'ap')` to targets.
3. `na-2` forwards the packet to `na-1` (direct, Tier 1) and to `gw-na-1` (for backbone distribution).
4. `gw-na-1` forwards to `gw-eu-1` and `gw-ap-1` over backbone links (Tier 2).
5. `gw-eu-1` receives the stream and distributes to `eu-2`, `eu-3` via its regional mesh (Tier 1). Each of those forwards to their local repeaters subscribed to TG 3120.
6. Same for `gw-ap-1` in the AP region.

**Total cross-region packets per voice frame:** One copy per region that has subscribers (2 in this example), regardless of how many servers or repeaters are in those regions.

### The talkgroup routing table

This is the key data structure that makes global scale work. Instead of replicating per-repeater state globally, each gateway maintains:

```python
@dataclass
class RegionalTGSummary:
    """What talkgroups a remote region is interested in."""
    region_id: str
    gateway_node_id: str
    slot1_talkgroups: Set[int]  # Union of all TGs from all repeaters in that region
    slot2_talkgroups: Set[int]
    last_updated: float
```

This is computed locally by each region's gateway by taking the union of all talkgroup sets from all repeaters in the region (local + intra-region peers). When a repeater connects or disconnects, the gateway recomputes the union and, only if it changed, broadcasts the updated summary to other gateways.

**Size:** A region with 500 repeaters across 100 different talkgroups produces a summary of ~100 integers per slot. The entire global routing table for 10 regions is maybe 2,000 integers. Trivial to store, replicate, and query.

**Update frequency:** Only changes when the set of active talkgroups in a region changes — which is when a repeater with a *new* TG connects or the *last* repeater with a TG disconnects. This is rare compared to per-packet events. A busy network might see a few updates per minute, not per second.

**Wildcard semantics and size bounds:**

A repeater with `slot_talkgroups = None` (allow all) is common in the existing codebase — it means the repeater's config doesn't restrict which TGs it receives. At the intra-region level, this works fine: the server routes to that repeater for any TG. But if `None` propagated into the backbone TG summary, it would mean "this region subscribes to everything," and every stream worldwide would be forwarded to that region — defeating the routing table entirely.

The rule: **`None` (wildcard) is never propagated in TG summaries.** When computing the regional union, a repeater with `None` talkgroups is treated as subscribing to the union of all explicitly enumerated TGs from other repeaters in the region. It widens local routing (the repeater hears everything its neighbors hear) but doesn't inflate the backbone summary. This means a region's TG summary only ever contains TGs that at least one repeater in the region explicitly lists.

If no repeaters in a region have explicit TG lists (all are wildcard), the gateway advertises an empty summary. Backbone streams won't be forwarded there. This is correct: if every repeater is "allow all," the network operator hasn't configured TG filtering, and backbone routing is undefined. The operator must assign explicit TG lists to at least some repeaters to participate in cross-region routing.

**Size soft cap:** Gateways log a warning if a regional TG summary exceeds 500 entries per slot. This indicates either misconfiguration or an unusually fragmented TG assignment. The summary is not hard-capped — it still functions — but the warning signals that backbone routing selectivity is degrading.

### Gateway selection and redundancy

Each region should have **two gateways** for redundancy. Both maintain backbone connections. One is primary (actively forwarding), the other is hot standby that takes over if the primary fails. The intra-region cluster bus heartbeat handles gateway failover — if `gw-na-1` dies, `gw-na-2` (another server in the NA region) promotes itself and announces to the backbone.

### Gateway overload and degradation policy

Gateways are fan-out points: they receive streams from the intra-region mesh and forward them to multiple backbone peers, while also handling TG summary replication and user lookups. Under heavy load (many concurrent streams, slow backbone links, or a burst of repeater connect/disconnect events), a gateway can fall behind.

**The core principle: voice stream data is loss-tolerant; control plane messages are not.**

A few dropped DMR voice frames cause a brief audio glitch (a "blip"). A dropped TG summary update causes incorrect routing until the next update. A dropped heartbeat can trigger false peer-death detection. So the degradation priority is:

1. **Highest priority: Control plane.** Heartbeats, TG summary updates, user lookup queries/responses, and gateway failover messages. These use a separate send queue per backbone peer with no depth limit (they're small and infrequent). If the event loop is overloaded, control plane messages are still drained first.

2. **Lower priority: Stream data.** Voice packets use a bounded send queue per backbone peer (default: 256 packets, ~15 seconds of audio for one stream). If the queue fills because a backbone peer is slow, new stream packets for that peer are dropped silently. The stream continues to other peers unaffected. The gateway logs a warning with the affected peer and drop count.

3. **Head-of-line blocking prevention.** Each backbone peer gets its own independent TCP connection and send buffer. A slow connection to EU does not stall packets to AP. The non-blocking send pattern from `EventEmitter` (already proven in the codebase) is reused: attempt `sendall`, catch `BlockingIOError` / `BrokenPipeError`, drop the packet and log.

4. **Monitoring.** The gateway exposes queue depth and drop counts per backbone peer via the dashboard event stream, so operators can see degradation in real time.

Gateway role is a configuration flag, not a separate process:

```json
{
    "cluster": {
        "node_id": "gw-na-1",
        "region": "north-america",
        "role": "gateway",
        "backbone_peers": [
            {"region": "europe", "address": "gw-eu-1.example.com", "port": 62033},
            {"region": "asia-pacific", "address": "gw-ap-1.example.com", "port": 62033}
        ],
        "peers": [
            {"node_id": "na-2", "address": "10.0.1.2", "port": 62032},
            {"node_id": "na-3", "address": "10.0.1.3", "port": 62032}
        ]
    }
}
```

### User cache at global scale

The user cache needs the same tiered treatment:

**Intra-region:** Replicate user cache entries across all nodes in the region (existing Phase 1.3 design). Private calls within a region route efficiently.

**Inter-region:** Don't replicate every user heard event globally. Instead, when a private call's target isn't found in the local region's cache, the server sends a **user lookup query** to its gateway, which fans it out to other regions' gateways. The region that has the user responds with the user's location. This is a pull model instead of push — it adds one round trip for cross-region private calls but eliminates the firehose of global user cache replication.

For frequently called users across regions, the lookup result can be cached locally with a short TTL (e.g., 60 seconds) so repeated calls don't trigger repeated lookups.

```
Private call A -> B (B is in Europe, caller is in North America):

1. na-2 checks local user cache: not found
2. na-2 checks regional user cache (from na-1, na-3): not found
3. na-2 asks gw-na-1: "where is user B?"
4. gw-na-1 asks gw-eu-1, gw-ap-1: "where is user B?"
5. gw-eu-1 responds: "user B is on eu-2, repeater 232100, slot 1"
6. gw-na-1 forwards the response to na-2
7. na-2 sends the private call stream to gw-na-1 -> gw-eu-1 -> eu-2 -> repeater 232100
8. na-2 caches: "user B is in EU region" (60s TTL)
```

Total latency for the lookup: one backbone round trip (~50-200ms depending on continents). This happens once per private call to a new cross-region target, not per packet.

**Negative caching:** If no region responds to a user lookup within 2 seconds (or all respond "not found"), the originating server caches a negative result: "user B is not known anywhere" with a 30-second TTL. This prevents lookup storms if a caller repeatedly tries to reach an inactive user. Without negative caching, every private call attempt to an unknown user would fan out a query to every gateway.

**Stale cache and user mobility:** A user can move between regions (e.g., a ham operator travels from NA to EU and connects their hotspot there). If a cached lookup points to the old region and the private call stream fails (the target region reports "user not found on this repeater"), the originating server invalidates the cache entry and re-queries all regions on the next attempt. This means at most one failed private call after a cross-region move, followed by correct routing on the second attempt.

**Cache entry structure:**

```python
@dataclass
class CrossRegionLookup:
    """Cached result of a cross-region user lookup."""
    radio_id: int
    region_id: Optional[str]   # None = negative result (not found)
    repeater_id: Optional[int]
    timestamp: float
    ttl: float                 # 60s for positive, 30s for negative
```

## Scaling Math

### Connections

| Regions | Nodes/region | Total nodes | Intra-region connections | Backbone connections | Total connections |
|---------|-------------|-------------|------------------------|---------------------|-------------------|
| 3       | 3           | 9           | 9                      | 3                   | 12                |
| 5       | 4           | 20          | 30                     | 10                  | 40                |
| 10      | 5           | 50          | 100                    | 45                  | 145               |
| 15      | 5           | 75          | 150                    | 105                 | 255               |
| 20      | 5           | 100         | 200                    | 190                 | 390               |

Compare to flat mesh: 100 nodes would be 4,950 connections. Hierarchical gives 390.

### Per-packet fan-out

With hierarchical routing, a voice packet fans out to:
- Intra-region peers: 2-4 TCP writes (same as small cluster)
- Backbone: 1 TCP write to the local gateway (the gateway handles further fan-out)

The gateway's fan-out to other regions is bounded by the number of regions, not the number of nodes. 10 regions means at most 9 backbone writes per packet — well within Python's capacity.

### State replication

| What | Flat mesh (50 nodes) | Hierarchical (10 regions x 5 nodes) |
|------|---------------------|-------------------------------------|
| Repeater state entries per node | 25,000 (all remote repeaters) | ~2,000 (own region) + TG summary table |
| User cache events/sec | Broadcast to 49 peers | Broadcast to 4 regional peers |
| Routing table size | 25,000 repeater entries | ~200 TG entries (10 regions x ~20 TGs) |
| Stream target calculation | Iterate 49 peers | Iterate 4 regional peers + 1 TG table lookup |

## Implementation

### What changes from the existing plan

**Phase 1 (Cluster Bus):** Add `region` and `role` fields to cluster config. The `ClusterBus` class gains a `BackboneBus` subclass that manages inter-region connections separately. Intra-region communication is unchanged.

**Phase 2 (Cross-Server Routing):** `_calculate_stream_targets()` gains a third loop after local repeaters and intra-region peers: check the talkgroup routing table for matching regions and add `('backbone', region_id)` targets. The `_forward_stream()` method routes backbone targets through the local gateway.

**New: `nexus/backbone.py`** (~500 lines):
- `BackboneBus`: Manages TCP connections to gateways in other regions
- `TalkgroupRoutingTable`: Maintains and queries the per-region TG summaries
- `UserLookupService`: Handles cross-region private call user lookups
- Gateway promotion/demotion logic

**Phase 3 (Failover):** Gateway failover within a region uses the existing cluster bus heartbeat. Backbone reconnection logic handles gateway IP changes.

**Phase 5 (Native Protocol):** Unchanged. The cluster-native protocol operates between client and its local server. The hierarchical routing is transparent to clients.

### New implementation phases

| Phase | Description | New Code | Risk |
|-------|-------------|----------|------|
| 6.1 | Region config + backbone bus | `backbone.py` (~300 lines), config changes | Medium |
| 6.2 | TG routing table + distribution | Additions to `backbone.py` | Medium |
| 6.3 | Hierarchical stream forwarding | Changes to `_calculate_stream_targets`, `_forward_stream` | Medium |
| 6.4 | Cross-region user lookup | Additions to `backbone.py`, `user_cache.py` | Low |
| 6.5 | Gateway failover | Additions to `backbone.py`, `cluster.py` | Medium |
| 6.6 | Backbone hardening (secrets, allowlists, replay protection, optional TLS) | Additions to `backbone.py`, config changes | Low |

### When to build this

**Not yet.** Phases 1-5 are the right starting point. The hierarchical design is a clean extension — it doesn't invalidate the earlier phases, it layers on top. A region of 2-5 nodes is exactly the small cluster from the original plan. The backbone is a new communication layer between regions.

Build this when:
- The network grows beyond ~10-12 nodes and fan-out becomes measurable
- Cross-continent latency makes flat mesh stream forwarding noticeable
- Operational teams are distributed by region and want independent management

The migration path is smooth: take an existing flat cluster, assign nodes to regions, designate gateways, and deploy the backbone config. No data migration, no protocol changes, no client impact.

## Backbone Security Considerations

The intra-region cluster bus authenticates peers with HMAC-SHA256 using a `shared_secret` from config. This works for a small cluster under one operator's control. The backbone spans regions that may be operated by different teams, cross untrusted networks, and have different security postures.

The following items should be addressed before worldwide production deployment. They don't change the architecture — they're guardrails on the existing trust model.

**Separate secrets per trust boundary.** The intra-region `shared_secret` and the backbone secret should be different keys. A compromised regional node shouldn't grant backbone access. Config structure:

```json
{
    "cluster": {
        "shared_secret": "regional-key-na",
        "backbone_secret": "backbone-global-key"
    }
}
```

**Gateway node allowlisting.** Each gateway should have an explicit list of allowed peer gateway node IDs and regions. A backbone connection from an unknown `node_id` or unexpected `region` is rejected even if it presents the correct secret. This limits the blast radius of a leaked backbone key.

**Key rotation.** Define a manual rotation procedure: generate new key, deploy to all gateways with a grace period where both old and new keys are accepted, then remove the old key. The backbone handshake should support a key ID field so both sides know which key to use during rotation.

**Replay protection on control plane messages.** TG summary updates and user lookup responses should include a monotonically increasing sequence number per peer. The receiver tracks the last seen sequence and rejects messages with a lower or equal number. This prevents an attacker who captured a backbone packet from replaying it to inject stale routing information. Stream data packets don't need replay protection — they're idempotent (a replayed voice frame at worst causes a brief audio artifact, and stream IDs prevent routing confusion).

**TLS for backbone links (optional).** For backbone connections crossing the public internet, wrapping the TCP connection in TLS provides encryption and stronger authentication (mutual certificate auth). This is a transport-level change that doesn't affect the framing or message format. The implementation cost is low (Python's `ssl` module wraps existing sockets), but operational cost increases (certificate management). Whether to require TLS should be a per-deployment decision based on network topology.

These items are scoped as **Phase 6.6: Backbone Hardening** in the implementation sequence — after the backbone is functionally complete and tested, before worldwide production rollout.
