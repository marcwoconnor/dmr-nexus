# Phase 3 Build Notes — Failure Detection + Graceful Shutdown

## Design Decisions

### 3.2 Failure Detection
- Peer disconnect already handled virtual stream cleanup (added in Phase 2)
- Added stale virtual stream timeout in `_check_stream_timeouts()` — catches crash/partition scenarios where `stream_end` never arrives
- Uses existing `stream_timeout` config (default 2s) — same threshold as local stream timeout
- Dead peer's user cache entries not yet handled (deferred to Phase 1.3 User Cache Sharing)

### 3.3 Graceful Shutdown — Drain Strategy
- **Option A chosen**: Stop routing NEW streams to draining node, let existing streams finish
- Alternative (Option B): Actively migrate streams away mid-stream — rejected for complexity and audio glitch risk
- Drain timeout configurable via `global.drain_timeout` (default 30s)
- Only counts real RX streams (not assumed TX streams) when waiting for drain

### Signal Handler Change
- Old: sync `handle_shutdown()` calls `cleanup()` then sets event
- New: async `handle_shutdown()` calls `await graceful_shutdown()` then sets event
- `cleanup()` kept as legacy wrapper calling `asyncio.ensure_future(graceful_shutdown())`
- Signal handler uses `asyncio.ensure_future(async_shutdown(signum))` since `loop.add_signal_handler` requires sync callback

### Shutdown Sequence
1. `self._draining = True` — reject new RPTL logins
2. `broadcast('node_draining')` — peers stop routing new streams here
3. Wait loop: check `_count_active_streams()` every 0.5s until 0 or timeout
4. Send MSTCL to all repeaters (UDP, immediate)
5. Send RPTCL to all outbound connections
6. `broadcast('node_down')` — peers clear draining state
7. Stop cluster bus (TCP teardown)
8. `await asyncio.sleep(0.5)` — let UDP packets flush

### Draining Peer Exclusion
- `_draining_peers: Set[str]` tracks which cluster peers are draining
- Checked in `_calculate_stream_targets()` cluster loop — `continue` if peer in set
- Cleared on: `node_down` message, `peer_disconnected` event
- NOT checked for existing streams — only new stream target calculation

## Code Changes

### hblink.py
- `_draining: bool` — local drain state
- `_draining_peers: Set[str]` — remote peers that are draining
- `datagram_received`: RPTL handler checks `_draining`, sends NAK if true
- `_calculate_stream_targets`: skip peers in `_draining_peers`
- `_check_stream_timeouts`: added virtual stream stale cleanup loop
- `cleanup()`: now just delegates to `graceful_shutdown()` via ensure_future
- `graceful_shutdown()`: new async method with ordered drain sequence
- `_count_active_streams()`: counts non-ended, non-assumed streams across all repeaters
- `_handle_cluster_message`: handles `node_draining` (add to set) and `node_down` (remove from set)
- `peer_disconnected`: also discards from `_draining_peers`
- Signal handler: async wrapper around `graceful_shutdown()`

## Edge Cases

1. **Double SIGTERM**: If SIGTERM arrives while already draining, `graceful_shutdown()` is called again. The second call will find `_draining=True` already set, broadcast again, and drain timeout will be short (streams already draining). Not harmful but slightly noisy. Could add a guard but not worth the complexity.

2. **Drain timeout with stuck stream**: If a stream never ends (no terminator, no timeout), the drain will eventually force disconnect after `drain_timeout` seconds. The repeater will detect the disconnect and stop transmitting.

3. **Peer crashes during drain**: If a draining peer crashes before sending `node_down`, the TCP disconnect triggers `peer_disconnected` which clears `_draining_peers` anyway.

4. **No cluster bus**: All cluster-related code is guarded by `if self._cluster_bus:` checks. Shutdown works fine without clustering enabled — just skips the broadcast steps.

## Test Notes

### TestGracefulShutdown
- Uses MagicMock with `side_effect=noop` for async methods (not `asyncio.coroutine` which was removed in Python 3.11)
- Verifies broadcast call ordering: `node_draining` before `node_down`
- Uses short `drain_timeout=0.1` to avoid slow tests

### TestVirtualStreamTimeout
- Binds multiple real methods to mock: `_check_stream_timeouts`, `_check_slot_timeout`, `_check_outbound_slot_timeout`, `_end_stream`
- Creates StreamState with `last_seen` in the past (5s ago) to trigger timeout
- Verifies fresh streams (last_seen=now) are NOT cleaned up
