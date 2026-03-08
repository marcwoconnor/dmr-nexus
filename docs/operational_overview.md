# HBlink4 Operational Overview

## The Big Picture: What Problem Does This Solve?

Imagine you have several DMR (Digital Mobile Radio) repeaters spread across a region. Each repeater covers a geographic area — maybe one on a hilltop, one downtown, one at a club station. When someone keys up on one repeater and talks on Talkgroup 3120, you want that audio to come out of the *other* repeaters that are also listening to TG 3120. That's what HBlink4 does — it's the **hub** that all those repeaters connect to, and it decides where to route each transmission.

The protocol they all speak is called **HomeBrew DMR** — it's a UDP-based protocol where repeaters authenticate with the server, send keepalive pings, and stream DMR voice data back and forth.

## The Two Processes

HBlink4 runs as **two separate processes** that talk to each other:

1. **The Core Server** (`python3 run.py`) — This is the real-time DMR protocol engine. It listens on UDP port 62031, handles repeater authentication, and routes voice packets between repeaters. This is the critical path — it must never block or lag.

2. **The Dashboard** (`python3 run_dashboard.py`) — This is a web server (FastAPI + Uvicorn) that shows you what's happening in real time in a browser. It receives events from the core server and pushes them to your browser via WebSocket.

They communicate through a **Unix socket** (`/tmp/nexus.sock`) by default, or optionally TCP. The core server sends events *to* the dashboard (like "a new transmission started"), and the dashboard can send a `sync_request` back (like "I just restarted, send me the current state").

```
Repeaters <-> [Core Server :62031/UDP] <-> [Unix Socket] <-> [Dashboard :8080/HTTP+WS] <-> Browser
```

## The Core Server Module by Module

Each file in `nexus/` in the order you'd encounter them if you followed a packet through the system.

### `constants.py` — The Protocol Vocabulary

This is the simplest file. It defines the byte strings that the HomeBrew protocol uses as commands:

- `RPTL` — "Repeater Login" (a repeater asking to connect)
- `RPTK` — "Repeater Key" (sending the authentication hash)
- `RPTC` — "Repeater Config" (sending metadata like callsign, frequency, location)
- `DMRD` — "DMR Data" (the actual voice/data packets)
- `RPTPING`/`MSTPONG` — Keepalive heartbeats

Think of these like HTTP verbs (GET, POST) but for the DMR world. Every UDP packet starts with one of these prefixes so the server knows what kind of message it is.

### `models.py` — The Data Structures

This defines the "nouns" of the system using Python dataclasses:

- **`RepeaterState`** — Everything the server knows about one connected repeater: its ID (4 bytes), IP address, authentication status, callsign, frequencies, and crucially its **per-slot talkgroup lists** and **active streams**. Despite the name "Repeater," this represents *any* inbound connection — hotspots, other servers linking in, actual repeaters. The `connection_type` field (`'repeater'`, `'hotspot'`, `'network'`, `'unknown'`) tells you what it actually is.

- **`StreamState`** — One active DMR transmission. A transmission has a source radio ID, a destination (talkgroup or private call target), which timeslot it's on, and a unique stream ID. The `target_repeaters` field caches which repeaters this stream should be forwarded to, so routing only happens once per transmission.

- **`OutboundState`** / **`OutboundConnectionConfig`** — For when *this server* connects to *another server* as if it were a repeater. This is how you link networks together.

Here's something important to understand: **DMR uses TDMA (Time Division Multiple Access) with two timeslots.** Each repeater has Slot 1 and Slot 2, and each slot can carry exactly one transmission at a time. That's why `RepeaterState` has `slot1_stream` and `slot2_stream` — they're independent channels.

### `protocol.py` — Packet Parsing

This is the hot path code. `parse_dmr_packet()` takes raw bytes and extracts the fields:

```
Bytes 0-3:   Command header (DMRD)
Byte 4:      Sequence number
Bytes 5-7:   Source radio ID (who's talking)
Bytes 8-10:  Destination ID (talkgroup or private call target)
Bytes 11-14: Repeater ID (which repeater sent this)
Byte 15:     Bits field (encodes slot, call type, frame type)
Bytes 16-19: Stream ID (groups packets into one transmission)
Bytes 20-52: DMR payload (the actual voice data)
```

Notice that IDs are **3 bytes** (radio/talkgroup) or **4 bytes** (repeater). The codebase keeps these as raw `bytes` objects throughout the hot path rather than converting to integers — this avoids repeated conversion overhead during routing.

The `is_dmr_terminator()` function detects when a transmission ends by checking DMR sync patterns in the payload. This gives ~60ms termination detection, much faster than waiting for a timeout.

### `access_control.py` — Who Gets In and What Can They Do?

`RepeaterMatcher` is the gatekeeper. When a repeater tries to connect, it:

1. **Checks the blacklist first** — Does this repeater ID, ID range, or callsign match any blocked patterns? If so, raise `BlacklistError` and reject.

2. **Finds matching configuration** — Scans the `repeater_configurations.patterns` array looking for a match by specific ID, ID range, or callsign wildcard (like `"N0MJS*"`).

3. **Returns the config** — Each pattern provides: passphrase (for authentication), talkgroup lists per slot (what this repeater is allowed to hear/send), and trust level.

The pattern system is powerful. You can say "all repeaters in the 312000-312999 range get these talkgroups" and then override specific ones with more targeted patterns.

### `user_cache.py` — Smart Private Call Routing

When someone makes a **group call**, the server forwards it to every repeater that has that talkgroup in its slot config. Simple.

But for **private calls**, you need to know *which repeater* the target user is currently on. That's what `UserCache` does — it tracks "Radio ID 3121234 was last heard on Repeater 312100, Slot 2, 30 seconds ago." When a private call comes in for that radio ID, instead of flooding every repeater, the server sends it only to the right one. The cache has a TTL (default 10 minutes) to prevent stale entries from accumulating.

### `events.py` — Talking to the Dashboard

`EventEmitter` sends JSON events to the dashboard process. Key design decisions:

- **Non-blocking** — It uses non-blocking sockets and silently drops events if the dashboard is down. The core server must never block waiting for the dashboard.
- **Length-prefixed framing** — Each message is preceded by 4 bytes indicating its length, so the receiver can parse the stream.
- **Transport abstraction** — Unix socket (fast, ~0.5-1us) for same-machine, TCP (IPv4/IPv6 dual-stack) for remote dashboard.

### `hblink.py` — The Main Engine

This is the largest file (~900 lines) and ties everything together. `HBProtocol` is the asyncio `DatagramProtocol` subclass that handles all UDP traffic. Here is the lifecycle:

**Repeater Connection:**
1. Repeater sends `RPTL` with its 4-byte ID
2. Server looks up config via `RepeaterMatcher`, checks blacklist
3. Server sends back a random salt challenge
4. Repeater sends `RPTK` with SHA-256 hash of salt + passphrase
5. Server verifies hash, sends `RPTACK`
6. Repeater sends `RPTC` with its metadata (callsign, frequency, etc.)
7. Server acknowledges, repeater is now connected
8. Keepalives: repeater sends `RPTPING`, server responds `MSTPONG`

**Voice Packet Routing:**
1. `DMRD` packet arrives from a repeater
2. `parse_dmr_packet()` extracts source, destination, slot, stream ID
3. If it's the first packet of a new stream, create a `StreamState`, calculate which repeaters should receive it (based on talkgroup lists), and cache that in `target_repeaters`
4. Forward the packet to all target repeaters (and outbound connections)
5. When a terminator is detected or the stream times out, clean up

**Outbound Connections:**
`OutboundProtocol` handles the flip side — this server connecting *to another server* as if it were a repeater. It goes through the same login/auth/config handshake, but in reverse. This is how you link HBlink4 to other DMR networks.

## Configuration

The config file (`config/config.json`) is JSON with these major sections:

- **`global`** — Network settings (bind addresses, ports), timeouts, logging, user cache TTL
- **`repeater_configurations`** — The pattern-matching rules. Each pattern has a `match` (how to identify repeaters) and a `config` (what they're allowed to do). There's also a `default` that applies when no pattern matches.
- **`blacklist`** — Patterns that reject connections outright
- **`outbound_connections`** — Server-to-server links. Each has an address, port, radio ID to present as, passphrase, and an `options` string like `"TS1=1,2,3;TS2=3120,3121"` that tells the remote server which talkgroups to send.
- **`dashboard`** — Transport settings for the event socket
- **`connection_type_detection`** — String patterns to categorize connections (is it a hotspot running Pi-Star? A network link from another HBlink?)

The dashboard has its *own* config at `dashboard/config.json` — the transport settings in both files must match or they won't be able to talk to each other.

## How to Actually Use It

**First time setup:**
```bash
git clone <repo> && cd ham_router
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dashboard.txt
cp config/config_sample.json config/config.json
# Edit config/config.json with your actual settings
```

**Running in development:**
```bash
# Terminal 1: Core server
python3 run.py

# Terminal 2: Dashboard
python3 run_dashboard.py
# Open http://localhost:8080 in your browser

# Or both at once:
./run_all.sh
```

**Running in production:**
The repo includes systemd service files (`nexus.service`, `nexus-dash.service`) — see `SYSTEMD.md` for installation. Important: the services run as the user who owns the installation directory.

**Running tests:**
```bash
python3 -m pytest tests/ -v                    # All tests
python3 -m pytest tests/test_access_control.py -v   # One file
python3 -m pytest tests/test_access_control.py::TestRepeaterMatcher::test_specific_id_match -v  # One test
```

## Key Concepts to Remember

1. **Everything is bytes on the hot path.** Radio IDs are 3-byte `bytes`, repeater IDs are 4-byte `bytes`, talkgroup sets are `Set[bytes]`. This is intentional — avoiding int conversions in the packet forwarding loop matters when you're handling hundreds of packets per second.

2. **Two timeslots, one stream each.** TDMA means Slot 1 and Slot 2 are independent. A repeater can carry two simultaneous conversations. The routing logic is per-slot.

3. **Routing is calculated once per stream, not per packet.** When the first packet of a new transmission arrives, the server figures out which repeaters should receive it and caches that set in `StreamState.target_repeaters`. Every subsequent packet in that stream reuses the cached routing.

4. **The dashboard is optional and non-blocking.** If the dashboard crashes or isn't running, the core server continues operating without any impact. Events are silently dropped.

5. **Pattern matching is ordered.** In `repeater_configurations.patterns`, the first matching pattern wins. Put specific overrides before broad ranges.

---

## Further Reading

- **[Clustering Plan](clustering_plan.md)** — Design for extending HBlink4 into a multi-server cluster with cross-server stream routing, failover, and shared state. Covers the cluster bus, inter-server forwarding, graceful shutdown, and operational tooling.
- **[Cluster-Native Protocol](cluster_native_protocol.md)** — An optional new client protocol designed for clustering. Replaces HomeBrew's stateful auth with token-based auth, adds client-declared talkgroup subscriptions, and enables fast failover (~2-3 seconds vs ~15 seconds). Runs alongside HomeBrew for backward compatibility.
- **[Global-Scale Clustering](global_scaling.md)** — Hierarchical routing architecture for worldwide deployment (50+ servers, thousands of repeaters). Regional clusters connected by a backbone with talkgroup-level routing tables and cross-region private call lookups.
