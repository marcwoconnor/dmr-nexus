# Cluster-Proxy Build Notes

## What This Is

Purpose-built Go proxy bridging MMDVMHost/DMRGateway (HomeBrew protocol) to HBlink4 cluster servers (cluster-native CLNT protocol). Replaces DMRGateway in the deployment chain.

```
MMDVMHost <--HomeBrew UDP--> cluster-proxy (Go) <--CLNT UDP--> HBlink4 server(s)
```

## Build & Test

```bash
# Build
cd cluster-proxy
go build -o cluster-proxy .

# Run
./cluster-proxy config.json

# Tests (48 total)
go test ./... -v

# Python-side tests (42 total)
cd .. && source venv/bin/activate && python -m pytest tests/test_native_forwarding.py tests/test_native_protocol.py -v
```

## Deployment

### Automated Install
```bash
# Default (user: cort, path: /home/cort/cluster-proxy)
./install.sh

# Custom user and path
./install.sh moconnor /opt/cluster-proxy
```

The install script:
- Builds a static binary (`CGO_ENABLED=0`, stripped symbols)
- Runs all tests before installing
- Creates install dir, copies binary
- Copies sample config only if no config.json exists (safe for upgrades)
- Generates systemd service file matching the actual install path/user
- Enables the service (but does not start it — edit config first)

### Manual Deploy
```bash
# Build for target (static, stripped)
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o cluster-proxy .

# Copy to target
scp cluster-proxy config.json cort@target:/home/cort/cluster-proxy/
scp cluster-proxy.service cort@target:/tmp/
ssh cort@target 'sudo cp /tmp/cluster-proxy.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now cluster-proxy'

# Logs
journalctl -u cluster-proxy -f
```

## File Map

| File | Purpose |
|------|---------|
| `main.go` | Entry point, signal handling (SIGINT/SIGTERM) |
| `config.go` | JSON config loader with defaults + validation |
| `protocol.go` | CLNT wire format: packet builders, token decode, HB helpers |
| `cluster.go` | Upstream client: auth, subscribe, keepalive, failover, token refresh |
| `homebrew.go` | Local HB server: RPTL/RPTK/RPTC auth, DMRC auto-register, DMRD routing |
| `proxy.go` | Glue: per-repeater TG filtering, union subscription, prune cleanup |
| `config_sample.json` | Example config |
| `cluster-proxy.service` | Systemd unit file (template; install.sh generates actual) |
| `install.sh` | Automated build + install + systemd setup |

## Protocol Details

### CLNT Wire Format
```
All packets: CLNT(4B) + CMD(4B) + ...

AUTH:      CLNT + AUTH + repeater_id(4B) + SHA256(passphrase)(32B) = 44B
AUTH_ACK:  CLNT + AACK + token_bytes(variable)
AUTH_NAK:  CLNT + ANAK + reason(variable)
SUBSCRIBE: CLNT + SUBS + token_hash(4B) + JSON{"slot1":[...],"slot2":[...]}
SUB_ACK:   CLNT + SACK
PING:      CLNT + PING + token_hash(4B) = 12B
PONG:      CLNT + PONG + JSON{peers, new_token?, redirect?}
DATA:      CLNT + DATA + token_hash(4B) + dmrd_payload(51B typical)
DISC:      CLNT + DISC + token_hash(4B) = 12B
```

### Token Wire Format
```
[2B payload_len][JSON payload][32B HMAC-SHA256 signature]

Payload: {"v":1,"rid":312000,"s1":[8,9],"s2":[3120],"iat":..., "exp":...,"cid":"cluster-id"}
Token hash (per-packet validation): SHA256(signature)[:4]
```

### DMRD Payload Layout (after "DMRD" prefix)
```
Offset  Size  Field
0       1     Sequence number
1       3     Source radio ID (big-endian)
4       3     Destination TG ID (big-endian)  <-- used for filtering
7       4     Repeater ID
11      1     Bits: bit7=timeslot(0=TS1,1=TS2), bit6=group_call  <-- used for filtering
12      4     Stream ID
16      33    DMR frame data
49      1     BER
50      1     RSSI
Total: 51 bytes
```

## Key Design Decisions

### Per-Repeater TG Filtering
Each repeater declares TGs via RPTO (`TS1=8,9;TS2=3120`). The proxy:
1. Stores per-repeater filter (nil TG set = wildcard, receive all)
2. Computes union of all repeater subscriptions
3. Sends union to cluster (so cluster only forwards needed traffic)
4. Filters inbound DMRD per-repeater before forwarding

Repeaters that never send RPTO get everything (safe default).

### Multi-Server Failover
- Connects to all configured servers simultaneously
- Primary handles TX; all servers handle RX
- Stream dedup via `sync.Map` with 200ms window on stream_id
- Dead detection: missed pong count > ping_timeout
- Failover: promotes first alive server, re-authenticates, re-subscribes
- Dead server recovery: keepalive loop periodically retries auth on dead servers

### Token Refresh
Server piggybacks new token in PONG JSON `new_token` field (base64-encoded) when current token is within 20% of expiry. No separate refresh handshake needed.

### Graceful Drain
Server sends `redirect: true` in PONG when shutting down. Proxy marks server dead and triggers failover.

## Performance Optimizations

| Optimization | Why |
|---|---|
| `addrKey [18]byte` map key | Eliminates `addr.String()` heap allocation on every DMRD packet |
| Byte-level packet dispatch | Avoids `string(data[:4])` allocation; compares `data[0]..data[3]` directly |
| `sync.Pool` for DMRD buffers | Reuses `[]byte` for outbound packet assembly instead of `make()` per packet |
| `BroadcastDMRD` single-lock | One lock acquisition + one buffer assembly for all repeaters instead of N |
| Hot-path TG extraction | `slot = 1 + (payload[11] >> 7)`, TG from `payload[4:7]` — two register ops, no alloc |
| 1MB write buffer | `SetWriteBuffer(1<<20)` on UDP socket for burst handling at scale |

## Security Notes

- HomeBrew auth uses `crypto/subtle.ConstantTimeCompare` (timing-safe)
- Cluster auth uses SHA256(passphrase), server validates with HMAC-SHA256 signed tokens
- Systemd service runs with `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`

## Gotchas / Lessons Learned

1. **DMRD payload size is 51 bytes** (without "DMRD" prefix), not 53. Server had a bug checking `12 + 53` — fixed to `12 + 51`. Full DMRD with prefix = 55 bytes.
2. **Python's CLNT protocol was already defined** in `cluster_protocol.py` with JSON-based tokens. Initial Go implementation used binary format and had to be rewritten to match.
3. **`parseOptions` expects `TS1=` without spaces** around `=`. Input like `TS1 = 8` won't match. This matches real MMDVMHost/DMRGateway RPTO behavior.
4. **Token `nil` vs empty slice semantics**: `nil` slot TGs = wildcard (all). Empty slice = nothing. This distinction flows through the entire subscription chain.
5. **Go `net.UDPAddr.IP.To16()` always returns 16 bytes** even for IPv4 (mapped as `::ffff:x.x.x.x`), making `addrKey` work uniformly for v4 and v6.

## Architecture Insights

### Why Go Proxy Instead of Modifying MMDVMHost/DMRGateway
Three options were evaluated:
- **(A) Modify MMDVMHost C++**: Invasive changes to upstream C++ project, hard to maintain across updates, would need to implement full token/JSON handling in C++.
- **(B) Modify DMRGateway C++**: Same C++ maintenance burden, and DMRGateway's architecture doesn't map well to cluster-native concepts.
- **(C) Purpose-built Go proxy**: Sits between existing software unchanged. Go gives static binaries, excellent UDP/concurrency support, and JSON is trivial. Chosen.

### Three-Tier Subscription Model
```
Repeater A (RPTO: TS1=8,9)      --> Proxy stores per-repeater filter
Repeater B (RPTO: TS2=3120)     --> Proxy computes union: {TS1=[8,9], TS2=[3120]}
Repeater C (no RPTO = wildcard) --> Proxy tells cluster to send union
                                     Inbound: A gets TS1/TG8,9 only
                                              B gets TS2/TG3120 only
                                              C gets everything
```
This is critical for global networks with many repeaters — each repeater only gets traffic it subscribed to, reducing unnecessary UDP traffic.

### Hot Path Cost Analysis
At 60 DMRD packets/sec during active TX (2 timeslots x 30 frames/sec):
- **Old**: N lock acquisitions + N map lookups + N buffer allocs per inbound packet = ~30K unnecessary lock ops/sec at 500 repeaters
- **New**: 1 lock + 1 buffer + N direct `WriteToUDP` = dominated by kernel syscall cost, not Go overhead
- `WriteToUDP` is non-blocking (kernel-buffered), so holding read lock during writes doesn't block other readers

### addrKey vs addr.String()
`net.UDPAddr.String()` allocates a formatted string like `"192.168.1.1:62031"` on the heap every call. The `addrKey [18]byte` is a fixed-size value type — Go compares arrays by value without allocation. `To16()` maps IPv4 into the v6 space uniformly, so the same key works for both address families.

### sync.Pool for DMRD Buffers
Pool's per-P (per-processor) design avoids contention between the homebrew receive goroutine and concurrent senders. Typical DMRD is 55 bytes, pool pre-allocates 128-byte buffers to avoid regrowth.

### Token Refresh via PONG Piggyback
Instead of a separate refresh handshake (extra round trip, extra state machine), the server checks token expiry on every PONG and includes a new one if within 20% of lifetime. The proxy decodes it inline — zero additional packets, zero additional latency.

### Systemd Hardening
`ProtectSystem=strict` + `ReadWritePaths` limits the Go binary to only its install directory. Since Go produces static binaries (no shared library deps with `CGO_ENABLED=0`), this hardening works without path exceptions. `NoNewPrivileges` prevents privilege escalation if the binary is compromised.

### CGO_ENABLED=0 Static Build
Produces a fully static binary with no libc dependency. Runs on any Linux regardless of glibc version. Combined with `-trimpath -ldflags="-s -w"` it strips debug symbols and build paths for a smaller, production-ready binary.

## Config Reference

See `config_sample.json`. Key fields:
- `local.repeater_id` — must match what MMDVMHost sends
- `local.passphrase` — shared with MMDVMHost
- `cluster.passphrase` — shared with HBlink4 server
- `cluster.servers` — primary first, then failover(s)
- `cluster.ping_interval` — seconds between keepalives (default 5)
- `cluster.ping_timeout` — missed pongs before failover (default 3)
- `subscription.slot1/slot2` — default TGs (overridden by RPTO from repeaters)
