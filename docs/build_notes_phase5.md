# Phase 5.1 + 5.2 Build Notes — Token Auth + Subscriptions

## Phase 5.1: Token Auth (cluster_protocol.py)

### Design Decisions

**Stateless tokens.** Tokens are HMAC-SHA256 signed with the cluster shared_secret. Any server with the same secret can validate without server-local state. This is what makes seamless failover work — a token issued by server A is valid on server B immediately.

**Token structure.** JSON payload with repeater_id, allowed TGs per slot, issued_at, expires_at, cluster_id. Binary wire format: `[2B payload_len][JSON payload][32B HMAC signature]`. Version field for future format changes.

**4-byte token_hash for hot path.** SHA256 of the signature, truncated to 4 bytes. Used as a fast per-packet lookup key in the token cache. Full HMAC verification only happens at auth time; the hot path (voice packets every 60ms) just does a dict lookup by token_hash.

**Dual-protocol dispatch.** In `datagram_received()`, the first 4 bytes distinguish protocols: `CLNT` for native, `DMRD`/`RPTL`/etc for HomeBrew. Checked before the HomeBrew command parsing, so native packets never enter the HomeBrew path.

**Wire format — 8-byte header.** Every native packet starts with `CLNT` (4B magic) + sub-command (4B): AUTH, AACK, ANAK, SUBS, SACK, PING, PONG, DATA, DISC. This is more structured than HomeBrew's variable-length command prefixes.

### Auth Flow
```
Client                    Server
  |-- CLNT+AUTH+rid+hash -->|  SHA256(passphrase)
  |<-- CLNT+AACK+token  ---|  Signed token
  |-- CLNT+SUBS+th+json -->|  Subscribe to TGs
  |<-- CLNT+SACK+json   ---|  Effective subscription
  |-- CLNT+PING+th      -->|  Keepalive
  |<-- CLNT+PONG+health ---|  Cluster health
  |-- CLNT+DATA+th+dmrd -->|  Voice/data packets
```

## Phase 5.2: Subscription Store (subscriptions.py)

### Design Decisions

**Client-driven subscriptions.** Unlike HomeBrew where TGs are assigned by server config, native clients declare what TGs they want. The server validates against config (intersection of requested and allowed). This enables clients to subscribe to a subset of their allowed TGs.

**Intersection semantics.**
- Both None (allow all): empty set — clients must be explicit
- Requested=None, Allowed=list: use all allowed TGs
- Requested=list, Allowed=None: use all requested (config allows everything)
- Both lists: intersection

**Cluster replication.** Subscriptions broadcast to cluster peers on change. On peer disconnect, all remote subscriptions from that peer are purged (same pattern as cluster_state and user_cache).

### Code Changes

#### New Files
| File | Lines | Purpose |
|------|-------|---------|
| `hblink4/cluster_protocol.py` | ~200 | Token, TokenManager, wire format constants |
| `hblink4/subscriptions.py` | ~160 | SubscriptionStore with config validation |
| `tests/test_native_protocol.py` | ~240 | 29 tests for tokens and subscriptions |

#### Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | Imports, TokenManager+SubscriptionStore init, dual-protocol dispatch in datagram_received, 6 native packet handlers (auth/subscribe/ping/data/disconnect), subscription cluster message handlers, subscription broadcast wiring, subscription cleanup on peer disconnect |

## Tests (29 new)
- Token: roundtrip, expired, not expired, hash size, corrupt data, allow-all
- TokenManager: issue+validate, wrong secret, wrong cluster, expired, hash lookup, any-server validation, cache cleanup
- Subscription: to_dict, remote source_node
- SubscriptionStore: intersection logic (4 cases), get_subscribers, unsubscribe, broadcast callbacks, merge_remote, remove_by_node, get_all
