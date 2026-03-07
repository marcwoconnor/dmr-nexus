# Phase 5.4 Build Notes — HomeBrew Proxy Adapter

## Design

The proxy adapter bridges HomeBrew repeaters into the subscription-based routing system. Instead of maintaining two separate routing paths, HomeBrew repeaters get internal subscriptions created from their config-assigned TG sets. This means native clients and HomeBrew repeaters share the same `SubscriptionStore` for TG routing data.

## Key Implementation Points

### `_create_homebrew_subscription()`
Converts bytes-based TG sets (`Set[bytes]`) from `RepeaterState` into int lists for `SubscriptionStore.subscribe()`. For HomeBrew, requested == allowed (config assigns TGs directly), so intersection is identity.

Called from:
- Connection completion (after `_load_repeater_tg_config`)
- RPTO handler (after TG list update)

### Subscription Lifecycle
- **Create**: On repeater authentication completion
- **Update**: On RPTO (repeater options update — TG list change)
- **Remove**: In `_remove_repeater()` via `unsubscribe(rid_int)`

### Native Client Target Calculation
Added to `_calculate_stream_targets()` — loops `_native_clients`, checks each client's subscription for TG match on the correct slot. Produces `('native', addr)` tuples in the target set.

### Native Client Forwarding
In `_forward_stream()`, native targets get packets reformatted:
- Strip 4-byte `DMRD` prefix
- Prepend `CLNT` (4B) + `DATA` (4B) + `token_hash` (4B)
- Result: 12B header + 51B DMR payload = 63 bytes per voice packet

### Wire Format Comparison
| Protocol | Header | Payload | Total |
|----------|--------|---------|-------|
| HomeBrew | `DMRD` (4B) | 51B DMR | 55B |
| Native | `CLNT+DATA+hash` (12B) | 51B DMR | 63B |
| Cluster | `0x01` + stream_id (5B) | 55B DMRD | 60B |

## Tests (9 new)
- HomebrewProxySubscription: bytes→int conversion, None TGs, disconnect cleanup, RPTO update
- NativeClientTargetCalculation: TG match, wrong TG excluded, wrong slot excluded, multiple clients
- NativeClientForwarding: packet format verification

## Modified Files
| File | Changes |
|------|---------|
| `hblink4/hblink.py` | `_create_homebrew_subscription()`, native client loop in `_calculate_stream_targets()`, native forwarding in `_forward_stream()`, subscription cleanup in `_remove_repeater()`, subscription create calls in connection completion + RPTO handler |
| `tests/test_cluster.py` | 9 new tests, `SubscriptionStore` import, `_native_clients`+`_subscriptions` in test mock |
