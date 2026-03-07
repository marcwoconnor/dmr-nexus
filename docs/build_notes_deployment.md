# Build Notes: Deployment & Dashboard Cluster View

## Deployment (2-node cluster)

### Infrastructure
- Node 1: LXC VMID 400 on pve04, IP 10.31.11.40
- Node 2: LXC VMID 401 on pve05, IP 10.31.11.41
- Ubuntu 24.04, user `dmr-nexus`, code at `/opt/dmr-nexus`

### Services (systemd)
- `dmr-nexus.service` — core HBlink server
- `dmr-nexus-dash.service` — FastAPI dashboard
- `dmr-nexus-sim.service` — persistent simulated repeater (per-node env file)

### Key deployment issues resolved
1. **PrivateTmp isolation**: systemd `PrivateTmp=true` hides `/tmp` sockets between services. Fixed by using `RuntimeDirectory=dmr-nexus` → `/run/dmr-nexus/mgmt.sock` for management socket, TCP port 8765 for dashboard events.
2. **Null bytes in callsigns**: `safe_decode_bytes()` needed `.strip('\x00')` for null-padded HomeBrew fields.
3. **Dashboard field name mismatch**: Server emits `packet_count`, old JS used `data.packets`. Fixed with fallback chain `data.packet_count || data.packets || 0`.
4. **Browser caching**: Added `Cache-Control: no-cache, must-revalidate` to dashboard HTML response to prevent stale JS after deployments.

## Test Repeater Simulator (`tools/test_repeater.py`)

Standalone HomeBrew protocol simulator. Connects as one or more repeaters, generates synthetic DMR voice traffic at real 60ms frame timing. Key features:
- Full handshake: RPTL → RPTK(auth) → RPTC(config) → RPTO(options)
- `--no-traffic` mode for persistent connections
- `--targets` JSON for splitting repeaters across cluster nodes
- Graceful SIGTERM handling (sends RPTCL disconnect)

## Cross-Cluster Traffic Test (`tools/test_traffic.py`)

Connects ephemeral repeaters to both nodes, sends traffic each direction, verifies DMRD packets arrive at the remote node's repeater via `_rx_streams` tracking. Exit 0 = pass, exit 1 = fail.

## Dashboard Cluster Health Section

Added cluster health visualization to the per-node dashboard. Shows local node + all cluster peers with:
- Connection status (green/yellow/red for connected/draining/dead)
- Latency (ms), "Local Server" for self
- Repeater count per node
- Config hash (first 8 chars, red if mismatched across peers)

Auto-hides when clustering not enabled. Updates in real-time via `cluster_state` WebSocket events.

### Architecture decision
Server dashboards show only intra-region cluster mesh (always small, 2-5 nodes). Regional/country/world views will be separate aggregation dashboards querying multiple server APIs — planned for future.

### Data pipeline (already existed, just needed UI)
```
hblink._emit_cluster_state() → EventEmitter → TCP socket →
dashboard server (state.cluster) → WebSocket → browser JS → updateCluster()
```
