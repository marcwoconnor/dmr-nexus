# Repeater Failover Guide

How repeaters reconnect when an HBlink4 server goes down, and how to configure your network for automatic failover.

## Background

HBlink4 servers can run in a cluster (see `clustering_plan.md`). When one server goes down — planned maintenance or crash — its repeaters need to reconnect to a surviving server. The HomeBrew protocol's built-in keepalive mechanism handles detection: repeaters send `RPTPING` and expect `MSTPONG`. After 3 missed pongs (~15 seconds with default 5s ping interval), the repeater considers the server dead and reconnects.

The question is: **reconnect to what?**

---

## Option 1: DNS-Based Failover (Recommended)

**Zero code changes. Works with all repeater firmware.**

Configure repeaters to connect to a DNS hostname instead of an IP address. The hostname resolves to multiple A (IPv4) or AAAA (IPv6) records, one per server.

### Setup

1. Create DNS records pointing to each server:
   ```
   dmr.example.com.  A  10.31.11.10   ; server-a
   dmr.example.com.  A  10.31.11.11   ; server-b
   ```

2. Set a low TTL (60-120 seconds) so repeaters pick up changes quickly.

3. Configure repeaters with `dmr.example.com` as the server address.

### How It Works

1. Repeater connects to `dmr.example.com`, DNS resolves to `10.31.11.10` (server-a).
2. Server-a goes down. Repeater detects missed pongs after ~15 seconds.
3. Repeater re-resolves `dmr.example.com`. DNS round-robin returns `10.31.11.11` (server-b).
4. Repeater authenticates with server-b. Since both servers share the same `repeater_configurations`, the repeater's ID and passphrase are accepted.
5. Server-b broadcasts `repeater_up` to surviving cluster peers. Routing resumes.

### DNS Considerations

- **Round-robin**: Most DNS resolvers rotate through A records. The repeater gets a different server on each resolution. Some resolvers are sticky — this is fine, the repeater will eventually get a live server.
- **Health-checked DNS**: Services like Route 53 or Cloudflare can remove dead servers from DNS automatically. This gives faster failover than waiting for the repeater to try each address.
- **TTL tradeoff**: Lower TTL = faster failover but more DNS queries. 60s is a good balance for ham radio.

### Limitations

- Failover takes ~15-20 seconds (3 missed pings + DNS re-resolution + auth handshake).
- If DNS caches a dead server's IP, the repeater may try it again before getting a live one.
- No awareness of server load — DNS doesn't know which server has fewer connections.

---

## Option 2: Config-Based Failover

**Zero code changes. Requires firmware support.**

Some repeater firmware (Pi-Star, MMDVM, BlueDV) supports configuring multiple server addresses with automatic fallback.

### Setup

Configure the repeater with a primary and secondary server address:
```
Server 1: 10.31.11.10:62031  (server-a, primary)
Server 2: 10.31.11.11:62031  (server-b, fallback)
```

The exact configuration depends on the firmware. Check your repeater's documentation.

### How It Works

1. Repeater connects to server-a (primary).
2. Server-a goes down. Repeater detects missed pongs.
3. Repeater tries server-b (fallback).
4. If server-b is also down, repeater cycles back to server-a.

### Advantages Over DNS

- Deterministic failover order (primary → secondary).
- No DNS dependency — works with raw IP addresses.
- Some firmware retries faster than DNS resolution allows.

### Limitations

- Not all firmware supports multiple server addresses.
- Operator must manually configure each repeater with both addresses.
- Adding a third server requires touching every repeater's config.

---

## Option 3: Server-Pushed Topology (Implemented — Phase 7.1)

**Implemented. Works with topology-aware clients (test_repeater.py, CLNT native protocol). Legacy repeaters fall back to Options 1/2.**

The server pushes its full cluster topology (`RPTTOPO` message) to connected repeaters on registration and on every cluster state change. Repeaters cache this and use it for instant failover, proactive drain switching, and automatic load rebalancing.

### How It Works

1. Repeater connects and completes auth handshake.
2. Server sends `RPTTOPO` with all cluster servers, their health, load, and priority.
3. Repeater caches the server list locally.
4. On server failure (3 missed pongs, ~25s): repeater picks next-best server from cache, reconnects in ~1-2s.
5. On graceful drain: server pushes topology with `draining=true`, repeater switches immediately (~2-3s).
6. On server recovery: topology push triggers load-based rebalancing with anti-flap protection (~35-45s).

### Advantages Over DNS/Config

- **Instant failover target**: No DNS re-resolve or static fallback list — the server told the repeater exactly where to go.
- **Load-aware**: Repeaters switch to the least-loaded server, not a random DNS record.
- **Proactive drain**: Repeaters switch before the server shuts down, not after.
- **Automatic rebalancing**: After a failure+recovery, repeaters redistribute across the cluster.
- **Zero external infrastructure**: No health-checked DNS, no load balancer, no static configs.

### Limitations

- Only works with topology-aware clients (our sim tool, CLNT native protocol).
- Legacy HomeBrew repeaters (MMDVM, Pi-Star) silently ignore `RPTTOPO` and need Options 1/2.
- Rebalance convergence takes ~35-45s (by design — anti-flap protection).

See `cluster_topology_protocol.md` for full protocol details, wire format, anti-flap algorithm, and priority calculation.

---

## Cluster Server Requirements

For any failover method to work, the cluster servers must share configuration:

1. **Same `repeater_configurations`**: All servers must accept the same repeater IDs with the same passphrases and talkgroup assignments.
2. **Same `blacklist`**: A repeater blocked on one server should be blocked on all.
3. **Cluster bus connected**: Servers must be in the same cluster so state (repeater_up/down, streams, user cache) is shared.

The config hash drift detection in heartbeats will warn if servers have different configurations.

### Recommended Deployment

```
         +-----------+     +-----------+
         | server-a  |<--->| server-b  |   Cluster bus (TCP mesh)
         | 10.31.11.10     | 10.31.11.11
         +-----------+     +-----------+
              ^                  ^
              |                  |
     DNS: dmr.example.com → [10.31.11.10, 10.31.11.11]
              |                  |
         +--------+         +--------+
         | rpt-1  |         | rpt-2  |     Repeaters connect via DNS
         +--------+         +--------+
```

### Rolling Upgrade Procedure

1. Drain server-a: `systemctl stop dmr-nexus` (triggers graceful shutdown)
2. Server-a sets `draining=true`, pushes topology → topology-aware repeaters switch proactively (~2-3s)
3. Server-a broadcasts `node_draining`, waits for streams, sends `MSTCL` → legacy repeaters reconnect via DNS (~15-20s)
4. Upgrade server-a software
5. Start server-a, cluster bus reconnects, state syncs
6. Server-b pushes updated topology showing server-a alive with load=0
7. Topology-aware repeaters rebalance back to server-a (~35-45s, anti-flap protected)
8. Repeat for server-b if needed
