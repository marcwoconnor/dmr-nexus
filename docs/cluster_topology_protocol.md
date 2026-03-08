# Cluster Topology Protocol Extension

Server-pushed cluster topology for automatic repeater failover. Both HomeBrew and CLNT protocols extended so repeaters/clients learn the full server mesh and can fail over instantly without DNS or static config.

## Problem

When a server dies, repeaters must discover an alternate server. Current options (DNS round-robin, static config) are slow (~20-25s) and require external infrastructure. The server cluster already knows its own topology — it should share this with connected clients.

## Design

### New Message Types

#### HomeBrew Extension: `RPTCL` (Repeater Cluster Topology)

Server → Repeater. Sent after registration completes and on cluster state changes.

```
RPTCL <4-byte repeater_id> <JSON payload>
```

Payload:
```json
{
  "v": 1,
  "servers": [
    {
      "node_id": "nexus-1",
      "address": "10.31.11.40",
      "port": 62031,
      "priority": 1,
      "alive": true,
      "load": 4
    },
    {
      "node_id": "nexus-2",
      "address": "10.31.11.41",
      "port": 62031,
      "priority": 2,
      "alive": true,
      "load": 3
    }
  ],
  "seq": 17
}
```

Fields:
- `v`: Protocol version (for future extension)
- `servers[]`: All cluster peers, ordered by recommended failover priority
- `priority`: Lower = preferred. Based on geographic proximity, load, admin weight
- `alive`: Whether this server is currently reachable from the sending server
- `load`: Number of connected repeaters (for load-aware failover)
- `seq`: Monotonic sequence number. Repeater ignores updates with seq <= last seen

Legacy repeaters that don't understand `RPTCL` will silently ignore it (HomeBrew convention: unknown commands are dropped). Zero impact on existing firmware.

#### CLNT Extension: `CLNT_TOPO`

Server → Client. Sent after token auth completes and on cluster state changes.

```
CLNT_TOPO <JSON payload>
```

Same JSON payload as `RPTCL`. CLNT clients are our protocol — full control over both sides.

### Topology Lifecycle

#### 1. Initial Push (Registration)

```
HomeBrew:
  Repeater --RPTL--> Server
  [... auth handshake ...]
  Server --RPTACK--> Repeater (config accepted)
  Server --RPTCL--> Repeater (topology push)    <-- NEW

CLNT:
  Client --CLNT_AUTH--> Server
  Server --CLNT_AUTH_OK--> Client
  Server --CLNT_TOPO--> Client (topology push)  <-- NEW
```

#### 2. Live Updates (Cluster Change)

When the cluster bus detects a state change (peer joins, peer dies, new peer added):

1. Server builds updated topology payload with new `seq`
2. Server sends `RPTCL`/`CLNT_TOPO` to all connected repeaters/clients
3. Repeaters cache the updated server list locally

Triggers for topology push:
- Cluster peer connects (new server online)
- Cluster peer declared dead (server failure)
- Admin config reload changes cluster membership
- Node draining (graceful shutdown starting)

#### 3. Forced Re-Registration

For repeaters that may have been connected for a long time and missed updates (e.g., due to a bug or edge case), the server can force a re-registration cycle:

**Option A: Soft re-registration (preferred)**

Just send `RPTCL` again — no disruption. The repeater gets the latest topology without dropping its connection. This works because `RPTCL` is a standalone message, not tied to registration.

**Option B: Hard re-registration (fallback)**

Send `MSTCL` (master close) to force the repeater to fully disconnect and re-register. During the new registration, it gets `RPTCL` with the latest topology. Causes ~1-2s disruption.

Use case: If the server suspects the repeater's cached topology is stale (e.g., repeater connected before topology push was implemented, or seq tracking suggests missed updates).

**Option C: Admin-triggered bulk refresh**

Management command: `hbctl push-topology` — sends `RPTCL`/`CLNT_TOPO` to all connected repeaters/clients immediately. Useful after cluster maintenance or adding new nodes.

### Repeater Failover Behavior

When a repeater detects server failure (3 missed pings, ~15s):

1. Check cached topology from last `RPTTOPO`
2. Sort servers by priority, filter out current (dead) server
3. Try next server in priority order
4. On successful reconnect, receive new `RPTTOPO` with updated topology

**Failover time: ~16-17s** (15s detection + 1-2s reconnect to known-good server)

vs. DNS round-robin: ~20-25s (15s detection + DNS re-resolve + may hit dead server)

#### Three-Tier Failover Server Selection

When selecting a failover target, the repeater uses a three-tier fallback to handle stale topology data (common during shutdown races where the dying server's last topology push has incorrect peer states):

| Tier | Criteria | When Used |
|------|----------|-----------|
| 1 | `alive=true` + `draining=false` | Normal case — healthy server available |
| 2 | `alive=true` + `draining=true` | All healthy servers draining — connect to one still up |
| 3 | Any known server ≠ current | Last resort — `alive` flag may be stale from shutdown race |

**Shutdown race condition**: When a server shuts down, its cluster bus connection drops before the final topology push. The dying server's last topology marks its peers as `alive=false` (stale cluster bus state). Without Tier 3, repeaters would refuse to try any server, even though the peers are actually healthy.

The repeater also skips `0.0.0.0` addresses (servers report their bind address, which is `0.0.0.0` for the local node in the topology).

### Graceful Shutdown Enhancement

When a server is draining (admin ran `hbctl drain`):

1. Server updates its own status in topology: `"draining": true`
2. Pushes updated `RPTTOPO` to all repeaters with draining flag
3. Repeaters proactively switch to next-priority server before the drain completes
4. **Failover time: ~2-3s** (no missed-ping detection needed)

### Priority Calculation

Server computes failover priority for each peer. Inputs:
- **Peer health**: Alive/dead from cluster bus heartbeats
- **Load**: Number of connected repeaters per server
- **Latency**: Cluster bus latency to peer (proxy for geographic distance)

Formula (lower = better):
```
priority = load + (latency_ms / 10)
```

Special cases:
- Dead peers: priority = 9999 (still included — they may recover)
- Draining peers: priority = 9000

### Load Rebalancing (Anti-Flap Algorithm)

After a server failure and recovery, all repeaters end up on the surviving server. When the failed server comes back, the topology protocol redistributes repeaters automatically — but carefully, to prevent oscillation.

#### The Problem

Without anti-flap protection, load rebalancing causes a thundering herd:

```
1. Server-B recovers. Server-A has 6 repeaters, server-B has 0.
2. Server-A pushes topology: A(pri=6) B(pri=0)
3. All 6 repeaters see B is better → all switch to B simultaneously
4. Now A has 0, B has 6 → B pushes topology: A(pri=0) B(pri=6)
5. All 6 switch back to A → infinite oscillation
```

#### Three-Layer Anti-Flap Design

The rebalancing algorithm uses three independent mechanisms to prevent flapping:

**Layer 1: Cooldown Timer (30 seconds)**

A repeater will not consider rebalancing within 30 seconds of connecting to its current server. This ensures the topology has stabilized and load readings are accurate before acting. Without this, repeaters react to transient topology pushes during the connection storm when a server recovers.

```
Timeline:
  T+0s   Server-B recovers, cluster bus reconnects
  T+1s   Server-A pushes topology (A: load=6, B: load=0) to all repeaters
  T+1s   Repeaters receive topology, but connected_at was <30s ago → SKIP
  T+30s  Next topology push → cooldown expired, evaluate rebalance
```

**Layer 2: Threshold Gate (2x priority + minimum gap of 3)**

A repeater only rebalances when the load imbalance is dramatic, not marginal. Both conditions must be true:

```
my_priority - best_priority >= 3       (minimum gap)
my_priority > 2 * max(best_priority, 1)  (proportional check)
```

Examples with 6 repeaters across 2 servers:

| My Server | Best Server | Gap | 2x Check | Rebalance? |
|-----------|-------------|-----|----------|------------|
| load=6 (pri=6) | load=0 (pri=0) | 6 ≥ 3 ✓ | 6 > 2 ✓ | **Yes** |
| load=4 (pri=4) | load=2 (pri=2) | 2 ≥ 3 ✗ | — | **No** |
| load=3 (pri=3) | load=3 (pri=3) | 0 ≥ 3 ✗ | — | **No** (balanced) |
| load=5 (pri=5) | load=1 (pri=1) | 4 ≥ 3 ✓ | 5 > 2 ✓ | **Yes** |

The 2x proportional check prevents unnecessary moves when both servers have low load (e.g., load=3 vs load=1 — not worth disrupting streams for).

**Layer 3: Random Jitter (0-10 seconds)**

When a repeater decides to rebalance, it waits a random 0-10 second delay before switching. This spreads the rebalance across time so not all repeaters switch in the same topology push cycle.

```
Timeline for 6 repeaters rebalancing from A(6) to B(0):

  T+30s  Topology push received, cooldown expired
         RPT-1: rebalance in 2.8s
         RPT-2: rebalance in 7.1s
         RPT-3: rebalance in 4.3s
         RPT-4: evaluates...
         RPT-5: evaluates...
         RPT-6: evaluates...
  T+33s  RPT-1 switches → A(5) B(1), topology push: A(pri=5) B(pri=1)
         RPT-4,5,6 re-evaluate: 5-1=4 ≥ 3 ✓, 5 > 2 ✓ → schedule
  T+35s  RPT-3 switches → A(4) B(2), topology push: A(pri=4) B(pri=2)
         RPT-5,6 re-evaluate: 4-2=2 ≥ 3 ✗ → STOP
  T+37s  RPT-2 was already scheduled, switches → A(3) B(3)
  T+37s  Final state: 3+3 balanced. No further rebalancing.
```

The jitter creates a natural feedback loop: each switch updates the topology, which subsequent repeaters re-evaluate against the new (closer-to-balanced) state. Combined with the threshold gate, the system converges to balance without overshooting.

#### Rebalancing Properties

| Property | Value |
|----------|-------|
| Cooldown after connect | 30 seconds |
| Minimum priority gap | 3 |
| Proportional threshold | 2x |
| Jitter range | 0-10 seconds |
| Convergence time (typical) | 30-45 seconds after recovery |
| Overshoot | None — jitter + threshold gate prevent it |
| Steady-state flapping | None — equal loads produce gap=0 < 3 |

#### Rebalance vs. Failover Summary

| Trigger | Detection | Action | Time |
|---------|-----------|--------|------|
| Server crash (hard kill) | 3 missed pongs | Immediate failover to best server | ~25s |
| Graceful drain | Topology push with `draining=true` | Immediate proactive switch | ~2-3s |
| Server recovery | Topology push with updated load | Cooldown → threshold → jittered switch | ~35-45s |

#### Sticky Failover (No Rebalance Case)

If the load difference doesn't meet the threshold, repeaters stay on their current server. This is intentional — "sticky failover" avoids unnecessary stream interruptions. An admin can force redistribution via `hbctl drain` on the overloaded server, or by restarting the sim services.

#### Graceful Disconnect on Rebalance

When a repeater rebalances, it sends `RPTCL` (disconnect) to the old server before connecting to the new one. This ensures the old server cleans up the connection state immediately rather than waiting for a keepalive timeout. Without this, the old server would show stale "ghost" repeaters for ~15 seconds until the missed-pong detection fires.

## Implementation Plan

### Server Side (nexus)

#### New: `nexus/topology.py` (~150 lines)

```python
class TopologyManager:
    """Builds and pushes cluster topology to connected clients."""

    def build_topology(self, cluster_bus, config) -> dict:
        """Build topology payload from current cluster state."""

    def push_to_all(self, protocol):
        """Send RPTCL/CLNT_TOPO to all connected repeaters/clients."""

    def push_to_repeater(self, repeater_id, addr):
        """Send RPTCL to a specific repeater."""

    def on_cluster_change(self):
        """Called by cluster bus when peer state changes. Triggers push."""
```

#### Modified: `nexus/hblink.py`

- After repeater registration completes (RPTACK for config): call `topology_manager.push_to_repeater()`
- After CLNT auth completes: call `topology_manager.push_to_client()`
- Wire `cluster_bus.on_state_change` callback to `topology_manager.on_cluster_change()`

#### Modified: `nexus/cluster.py`

- Add `on_state_change` callback hook (called when peer connects/disconnects/dies)
- Expose `get_peer_stats()` for topology builder (load, latency, alive status)

#### Modified: `nexus/constants.py`

```python
RPTCL = b'RPTCL'      # Cluster topology push (server → repeater)
CLNT_TOPO = b'CLNTTOPO'  # Cluster topology push (server → native client)
```

#### New management command: `hbctl push-topology`

Sends topology refresh to all connected repeaters/clients via management socket.

### Repeater Side

#### `tools/test_repeater.py`

- Parse `RPTCL` messages, cache server list
- On disconnect: try next server from cached list instead of reconnecting to same address
- Log topology updates

#### Go cluster-proxy (`cluster-proxy/`)

- Parse `RPTCL` from upstream servers
- Merge with static config (server-pushed addresses take precedence)
- Use for failover decisions instead of static server list only

#### Future: MMDVM/Pi-Star patches

Document the `RPTCL` message format for firmware developers. Unknown commands are ignored by existing firmware, so this is backwards-compatible — old repeaters just don't get the benefit.

## Wire Compatibility

- `RPTCL` is a new command prefix. HomeBrew convention: unknown prefixes are silently dropped. Tested: MMDVM, Pi-Star, BlueDV all ignore unknown server messages.
- `CLNT_TOPO` is in our CLNT namespace — fully controlled.
- No changes to existing message formats or handshake flow.
- Repeaters that don't understand `RPTCL` continue working exactly as before.

## Sequence of Implementation

1. Add `TopologyManager` + `RPTCL`/`CLNT_TOPO` constants
2. Wire into registration flow (push on connect)
3. Wire into cluster bus state change (push on change)
4. Update `test_repeater.py` to parse + use topology for failover
5. Update Go cluster-proxy to use server-pushed topology
6. Add `hbctl push-topology` management command
7. Add graceful drain topology update (draining flag)
8. Tests: topology push, failover with cached list, stale seq rejection
