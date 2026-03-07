# Phase 5.3 Build Notes — Cluster-Aware Keepalive

## Design

Enhanced PONG response gives clients everything they need for fast failover without discovery overhead. Instead of "I'm alive," the server says "I'm alive, here's who else is alive, here's their load, and here's where you should reconnect if I go down."

## PONG Response Fields

```json
{
  "node_id": "server-1",
  "active_streams": 2,
  "connected_repeaters": 5,
  "peers": [
    {"node_id": "server-2", "status": "alive", "latency_ms": 1.5, "repeater_count": 3},
    {"node_id": "server-3", "status": "draining", "latency_ms": 2.0, "repeater_count": 1}
  ],
  "preferred_server": "server-2",
  "redirect": false,
  "new_token": "base64..."
}
```

### New Fields (Phase 5.3)
- `node_id`: This server's identity
- `active_streams`: Current active DMR streams on this server
- `connected_repeaters`: Number of connected HomeBrew repeaters
- `peers[].status`: `alive` / `dead` / `draining` (was just `alive` bool)
- `peers[].repeater_count`: Load indicator per peer
- `preferred_server`: Lowest-load alive non-draining peer (load-balancing hint)

### Existing Fields (Phase 5.1)
- `redirect`: True when this server is draining — client should reconnect
- `new_token`: Auto-refreshed token when within 20% of expiry
- `peers[].latency_ms`: Inter-cluster latency

## Failover Timeline
1. Client pings every ~5s (configurable)
2. 1 missed PONG → client knows server is down
3. Client has full peer list with health from last PONG
4. Reconnect to `preferred_server` or any alive peer
5. Token is valid on any cluster server (stateless HMAC)
6. Total failover: ~5-10s (1 ping interval + reconnect)

## Tests (9 new)
- node_id standalone vs clustered
- active_streams count
- peer status: alive, draining, dead
- preferred_server selection (lowest load)
- no preferred_server when all draining
- redirect flag on drain
