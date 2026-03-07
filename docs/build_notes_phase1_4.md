# Phase 1.4 + 4.2 Build Notes — Dashboard Cluster View

## Design Decisions

### Snapshot vs. Incremental Events
- Full cluster state snapshot emitted on every topology change
- Alternative (incremental peer_added/removed): rejected — cluster state is tiny, snapshot eliminates state machine complexity on dashboard side
- Dashboard just replaces the whole dict on each event — no partial update bugs

### Emit Points
- `_emit_cluster_state()` called on: peer_connected, peer_disconnected, sync_state, repeater_up, repeater_down, node_draining, node_down, _send_initial_state (dashboard reconnect)
- Not emitted on heartbeats (too frequent, latency updates are low priority) — dashboard can poll `/api/cluster` for fresh latency if needed
- Heartbeat-driven latency updates would generate ~1 event/s per peer; not worth the bandwidth for a display metric

### Data Shape
Each emit contains:
- `local_node_id`: this server's identity
- `peers[]`: one entry per configured peer with: node_id, connected, authenticated, alive, latency_ms, last_heartbeat, config_hash, repeater_count, draining

### EventEmitter is Sync
`_emit_cluster_state()` is sync — works because EventEmitter.emit() is non-blocking (raw socket send). No async/sync bridge needed unlike the stream data hot path.

## Code Changes

### hblink4/hblink.py
- `_emit_cluster_state()`: builds snapshot from `cluster_bus.get_peer_states()` + `_cluster_state` repeater counts + `_draining_peers`, calls `self._events.emit('cluster_state', ...)`
- 8 call sites added: peer_connected, peer_disconnected, repeater_up, repeater_down, sync_state, node_draining, node_down, _send_initial_state

### dashboard/server.py
- `DashboardState.cluster`: new dict field for cluster state
- `handle_event()`: handles `cluster_state` event type, stores in `state.cluster`
- `websocket_endpoint()`: includes `cluster` in initial_state payload
- `/api/cluster` REST endpoint: returns current cluster state

## Tests (5 new)
- `TestClusterDashboardEmit`: _emit_cluster_state output (peer states, draining flag, noop without bus, zero repeaters)
- `TestClusterDashboardHandleEvent`: dashboard stores cluster_state correctly
