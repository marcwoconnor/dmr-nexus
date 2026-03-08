# Phase 4.1 + 4.3 Build Notes — Config Hot-Reload + Management Interface

## Phase 4.1: Config Hot-Reload (SIGHUP)

### Design Decisions

**What gets reloaded.** Only routing-relevant sections: `repeater_configurations`, `blacklist`, `connection_type_detection`. Global settings (bind addresses, ports, timeouts) require a restart — they affect the transport layer which can't be reconfigured at runtime.

**Re-evaluation strategy.** After rebuilding the matcher, every connected repeater is re-checked:
1. Blacklist check — newly blacklisted repeaters get MSTCL + disconnect
2. Config match check — repeaters that no longer match any pattern get disconnected
3. TG list update — surviving repeaters get their TG sets refreshed from new config

This means you can add a repeater to the blacklist, SIGHUP, and it's immediately disconnected. Or you can change TG assignments and they take effect without repeaters reconnecting.

**Config hash update.** The cluster bus config hash is recomputed after reload so peers detect drift on the next heartbeat.

### Code Changes
- `CONFIG_FILE` module-level variable stores config path for reload
- `HBProtocol._reload_config()`: reads config, rebuilds matcher, re-evaluates repeaters, updates config hash, emits dashboard event
- `async_main()`: SIGHUP handler calls `_reload_config()` on all protocol instances

### Workflow
```bash
# Edit config
vim config/config.json

# Reload without restart
kill -HUP $(pidof python3)  # or: kill -HUP $(cat /var/run/nexus.pid)

# Verify via management socket
python3 hbctl.py status
```

For multi-server clusters: edit config on all servers, then SIGHUP each one. Config hash drift detection in heartbeats will warn if you forget one.

---

## Phase 4.3: Management Interface

### Design Decisions

**Unix socket over HTTP.** Keeps it simple — no auth needed (filesystem permissions handle access), no HTTP overhead, works from shell scripts. Same pattern as Docker's `/var/run/docker.sock`.

**Line-delimited JSON.** One JSON object per line in each direction. Client sends `{"command": "status"}\n`, server responds with `{"ok": true, ...}\n`. Simple, no framing needed.

**Nested function.** `_handle_mgmt_command()` is defined inside `async_main()` because it needs access to the `protocols` list. This means it can't be unit-tested via import, but the Unix socket roundtrip test covers it.

### Commands
| Command | Description |
|---------|-------------|
| `status` | Server overview: node_id, repeater/outbound/peer counts, draining |
| `cluster` | Cluster detail: per-peer connected/alive/latency/repeater_count/draining |
| `repeaters` | All repeaters cluster-wide with owning node |
| `reload` | Trigger config hot-reload (same as SIGHUP) |
| `drain` | Initiate graceful shutdown sequence |

### CLI Tool
`hbctl.py` — simple Python CLI that connects to the management socket and pretty-prints responses. No dependencies beyond stdlib.

### Code Changes
- `async_main()`: Unix socket server at `/tmp/nexus_mgmt.sock` (configurable via `global.management_socket`)
- `handle_mgmt_client()`: async connection handler, line-delimited JSON
- `_handle_mgmt_command()`: command dispatcher
- `hbctl.py`: CLI client

## Tests (5 new)
- `TestConfigReload`: no config file returns False, rebuilds matcher, disconnects blacklisted, emits dashboard event
- `TestManagementSocket`: Unix socket roundtrip test
