# Phase 6.1-6.3 Build Notes — Backbone Bus, TG Routing, Hierarchical Forwarding

## Architecture

```
Region: us-east                    Region: us-west
┌──────────────────┐              ┌──────────────────┐
│  node-1 (node)   │              │  node-4 (node)   │
│  node-2 (node)   │              │  node-5 (node)   │
│  gw-east (gw) ◄──┼──backbone──►─┤  gw-west (gw)    │
│                  │   TCP:62033  │                  │
│  cluster bus     │              │  cluster bus     │
│  TCP:62032       │              │  TCP:62032       │
└──────────────────┘              └──────────────────┘
```

- **Intra-region**: Full mesh via `ClusterBus` (existing, port 62032)
- **Inter-region**: Gateway-to-gateway via `BackboneBus` (new, port 62033)
- **Separate secrets**: `shared_secret` for cluster, `backbone_secret` for backbone

## New File: `hblink4/backbone.py` (~500 lines)

### BackboneBus
Same pattern as ClusterBus — TCP mesh, HMAC-SHA256 auth, heartbeats, length-prefixed framing. Key differences:
- Auth includes `region_id` in addition to `node_id`
- Rejects connections from unknown regions (allowlist from config)
- Longer default timeouts (5s heartbeat, 15s dead threshold, 10s connect timeout) — cross-region links have higher latency
- Carries `region_id` on every message for routing

### TalkgroupRoutingTable
Maps `region_id` → `(slot1_tgs, slot2_tgs)`. Gateway computes its region's TG union from local repeaters + cluster state. Advertised to backbone peers only when the set changes.

Key rule: **None (wildcard) TGs never propagated to backbone.** This prevents "allow-all" configs from routing every TG to every region. Only explicit TGs are advertised.

### RegionalTGSummary
Dataclass: region_id, slot1/slot2 TG sets, updated_at, gateway_node_id.

## Integration into hblink.py

### Config
```json
{
  "cluster": {
    "region": "us-east",
    "role": "gateway"
  },
  "backbone": {
    "enabled": true,
    "port": 62033,
    "backbone_secret": "...",
    "gateways": [
      {"node_id": "gw-west", "region_id": "us-west", "address": "10.0.2.1"}
    ]
  }
}
```

### Target Calculation
Third loop in `_calculate_stream_targets()` — after local repeaters and cluster peers, check backbone TG routing table. Produces `('backbone', region_id)` tuples. Excluded when `local_only=True`.

### Stream Lifecycle
- `_handle_stream_start`: sends `backbone_stream_start` to target regions
- `_forward_stream`: sends binary data to backbone regions (same 4B stream_id + 55B DMRD format)
- `_end_stream`: sends `backbone_stream_end` to target regions

### Gateway Distribution
When a gateway receives a backbone stream, it:
1. Creates a virtual stream with local-only targets
2. Forwards to local repeaters
3. Forwards to intra-region cluster peers (so non-gateway nodes in the region get it)

### TG Summary Re-advertisement
Triggered on repeater connect/disconnect. Change detection prevents unnecessary backbone traffic.

## Tests (24 new)
### test_backbone.py (20 tests)
- TG routing table: update/get, target matching, slot2, exclude self, remove, count, get_all
- Regional TG summary: bytes TGs, int TGs, None skipping, union
- BackboneBus init: peers, allowed regions, connected regions, region lookup
- TG summary change detection: no broadcast when unchanged, broadcast on change
- TCP integration: connect + TG exchange over real TCP

### test_cluster.py (4 tests)
- Backbone target calculation: TG match, wrong TG excluded, own region excluded, local_only excludes backbone
