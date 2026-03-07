# Phase 1.3 Build Notes — User Cache Sharing

## Design Decisions

### Broadcast vs. Push
- Broadcast model: every local user_heard is broadcast to all cluster peers
- Alternative (pull model): peers query on cache miss — rejected because it adds latency to private call routing
- Broadcast is cheap: small JSON messages, infrequent relative to voice packets

### Throttling Strategy
- Max 1 broadcast per second per node (configurable via `_broadcast_interval`)
- Batch overflow at 50 entries forces immediate flush (prevents unbounded queue growth)
- Periodic `cleanup()` (runs every 60s) also flushes the queue as a safety net
- Three flush triggers: throttle timer expired, batch overflow, periodic cleanup

### No Re-Broadcast
- Remote entries (source_node != None) are NOT re-broadcast to other peers
- Prevents amplification: node A hears user, broadcasts to B and C. B and C don't re-broadcast to each other.
- Each node broadcasts only its own local observations

### Local Overwrites Remote
- A local update for a radio_id overwrites a remote entry (sets source_node=None)
- This is correct: if we hear the user locally, that's more authoritative than a remote report
- The remote entry may be stale (user moved to our server)

### source_node in to_dict
- Only included when non-None (remote entries)
- Keeps JSON compact for the common case (local entries)
- Dashboard can use this to show "heard on node-b" for remote entries

## Code Changes

### user_cache.py
- `UserEntry.source_node: Optional[str]` — tracks which cluster node reported this user
- `UserCache._broadcast_callback` — callable that receives batched entries
- `UserCache._broadcast_queue` — list of pending user_heard dicts
- `UserCache._last_broadcast` — timestamp of last broadcast (for throttling)
- `set_broadcast_callback(callback)` — sets the broadcast function
- `update()`: added `source_node` param, queues broadcast for local-only entries
- `flush_broadcast()`: sends queued entries if throttle allows
- `merge_remote(entries, source_node)`: bulk merge of remote entries
- `cleanup()`: also calls `flush_broadcast()`

### hblink.py
- `_handle_cluster_message`: handles `user_heard` type, calls `_user_cache.merge_remote()`
- `_start_cluster_bus`: after cluster bus starts, wires up broadcast callback using `asyncio.ensure_future` to broadcast via cluster bus

## Broadcast Message Format
```json
{
  "type": "user_heard",
  "node_id": "node-a",
  "entries": [
    {"radio_id": 12345, "repeater_id": 312000, "slot": 1, "talkgroup": 9},
    {"radio_id": 67890, "repeater_id": 312001, "slot": 2, "talkgroup": 3}
  ]
}
```

## What This Enables
- Private call routing across servers: if radio 12345 was heard on server A, and someone on server B makes a private call to 12345, server B knows the user is on server A
- Unified "Last Heard" table across the cluster (when dashboard Phase 1.4 is built)
- Note: actual private call routing logic (using the cache to route via cluster) is not yet implemented — that requires extending the private call handler which currently rejects all unit calls
