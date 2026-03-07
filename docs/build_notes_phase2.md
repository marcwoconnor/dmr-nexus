# Phase 2 Build Notes — Cross-Server Stream Routing

## Design Decisions

### TG Matching (2.1)
- **Option A chosen**: Check per-repeater TG lists on remote peers, match TG+slot exactly
- Alternative was "just check if any remote repeater exists" — rejected as wasteful (forwards traffic the remote server drops anyway)
- `None` (allow-all) on remote repeaters matches ANY talkgroup, consistent with local routing behavior
- `break` after first match per peer — route to servers, not individual repeaters. One `('cluster', node_id)` entry per peer regardless of how many repeaters match

### Single-Hop Rule (2.3)
- Virtual streams from cluster peers forward to LOCAL repeaters ONLY
- Never re-forwarded to other cluster peers (prevents mesh loops)
- Also skip outbound connections for virtual streams — outbound links to non-cluster servers could create loops if that server also has a cluster path back
- Enforced via `local_only=True` parameter on `_calculate_stream_targets()`

### Sync/Async Bridge (2.4)
- `_forward_stream()` is sync — called from `datagram_received()` (asyncio UDP callback, not a coroutine)
- `_send_packet()` (UDP) is sync — fine for local repeaters and outbound connections
- Cluster sends are async (TCP with `drain()`) — bridged via `asyncio.ensure_future()`
- For the hot path (~17 packets/second per stream), this avoids blocking the UDP callback
- Cluster targets collected in loop, single `ensure_future` call after loop (not one per target)

### Virtual Stream Keying
- `(node_id, stream_id_bytes)` as dict key
- Prevents stream ID collisions — different nodes can independently generate same 4-byte stream_id
- Binary hot path uses raw bytes (no hex conversion per packet)
- JSON messages (stream_start/end) use hex strings for stream_id

### Wire Format
- `stream_start`: JSON via MSG_TYPE_JSON (0x00) — includes stream_id, slot, dst_id, rf_src, call_type, source_repeater
- `stream_data`: Binary via MSG_TYPE_STREAM_DATA (0x01) — `[4B stream_id][55B DMRD]` = 59 bytes payload
- `stream_end`: JSON via MSG_TYPE_STREAM_END (0x02) — includes stream_id, reason
- Redundant stream_id in binary format (prefix + inside DMRD at bytes 16-20) is intentional for O(1) lookup without parsing

### DMRD Packet Forwarding
- Virtual stream data forwarded as-is to local repeaters (no repeater_id rewrite)
- The repeater_id in the DMRD header (bytes 11-14) is the original source repeater from the remote server
- Local repeaters receiving forwarded packets don't validate the repeater_id — they just play the audio
- Different from outbound connections which DO rewrite repeater_id (remote server only knows our outbound radio_id)

## Code Changes Summary

### cluster.py
- `send_stream_start(node_ids, stream_info)` — JSON to specific peers
- `send_stream_data(node_ids, payload)` — binary hot path to specific peers
- `send_stream_end(node_ids, stream_id_hex, reason)` — MSG_TYPE_STREAM_END to specific peers

### models.py
- `source_node: Optional[str] = None` added to StreamState — identifies cluster origin (None = local)

### hblink.py
- `_virtual_streams` dict added to `__init__`
- `_calculate_stream_targets()`: added `local_only=False` param, wrapped outbound loop in `if not local_only:`, added cluster peer loop after outbound
- `_handle_stream_start()`: after calculating targets, extracts cluster peers and sends stream_start via `ensure_future`
- `_forward_stream()`: added `cluster_targets` list, third branch in target loop for `('cluster', node_id)`, async send after loop
- `_end_stream()`: sends stream_end to cluster peers if stream has cluster targets and is not a virtual stream (`not stream.source_node`)
- `_handle_cluster_message()`: added handlers for stream_start, stream_data_binary, stream_end
- Peer disconnect handler: cleans up virtual streams from disconnected peer, ends assumed streams on local targets
- Three new methods: `_handle_virtual_stream_start()`, `_handle_virtual_stream_data()`, `_handle_virtual_stream_end()`

## Test Strategy

### Unit Tests (TestClusterTargetCalculation)
- Mock HBProtocol with real `_calculate_stream_targets` bound via `__get__`
- Mock `_cluster_bus.is_peer_alive` to control peer liveness
- Set `_cluster_state` directly with TG lists as int lists (matching JSON format)
- 8 tests: TG match, no match, allow-all, wrong slot, dead peer, local_only, break-on-first-match, multiple peers

### Unit Tests (TestVirtualStreamHandlers)
- Same mock approach with real handler methods bound
- Local repeater in `_repeaters` with TG 9 on slot 1
- 7 tests: start creates stream, no-match targets, data forwards, data without start, end cleanup, duplicate start, terminator in data

### Integration Tests (TestClusterStreamProtocol)
- Reuses `_create_bus_pair()` pattern from Phase 1 tests
- Real TCP connections between two ClusterBus instances
- 4 tests: send stream_start, send stream_data, send stream_end, full lifecycle (start + 2 data + end)

### Mock Pattern
`_make_hbprotocol_for_routing()` creates a MagicMock(spec=HBProtocol) with real methods bound via `method.__get__(mock)`. This lets us test the actual routing logic without standing up a full server (event loop, UDP transport, config loading). CONFIG is set to minimal valid state.

## Edge Cases Noted (Not Yet Handled)

1. **Virtual stream timeout**: If stream_end never arrives and no terminator detected, virtual stream stays in `_virtual_streams` forever. Phase 3.2 (Failure Detection) should add periodic cleanup of stale virtual streams.
2. **Message ordering**: If stream_data arrives before stream_start (shouldn't happen on same TCP connection due to ordered delivery), the data is silently dropped. This is correct behavior.
3. **Repeater ID in forwarded packets**: Local repeaters see the original remote repeater_id in forwarded DMRD packets. If any repeater firmware validates this field, it would reject the packet. Not observed in practice — HomeBrew repeaters don't validate data packet repeater_id.
4. **Outbound + cluster overlap**: If a server has both an outbound HomeBrew link and a cluster connection to the same remote server, traffic could potentially be sent twice via different paths. The `local_only` flag prevents this for virtual streams, but for locally-originated streams, both outbound and cluster targets could match. This is unlikely in practice (you wouldn't configure both) and the remote server would dedup by stream_id.
