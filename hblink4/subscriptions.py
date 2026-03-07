"""
Subscription store for cluster-native protocol.

Clients declare which talkgroups they want per slot. The store validates
against server config (reject unauthorized TGs) and replicates across the
cluster bus.

Key difference from HomeBrew: subscriptions are client-driven, not
config-assigned. The config sets the *allowed* TGs; the client picks
which of those it actually wants.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Set, Callable

logger = logging.getLogger(__name__)


@dataclass
class Subscription:
    """A client's TG subscription for one repeater."""
    repeater_id: int
    slot1_talkgroups: Set[int] = field(default_factory=set)
    slot2_talkgroups: Set[int] = field(default_factory=set)
    updated_at: float = 0.0
    source_node: Optional[str] = None  # None = local, else remote node_id

    def to_dict(self) -> dict:
        d = {
            'repeater_id': self.repeater_id,
            'slot1_talkgroups': sorted(self.slot1_talkgroups),
            'slot2_talkgroups': sorted(self.slot2_talkgroups),
            'updated_at': self.updated_at,
        }
        if self.source_node:
            d['source_node'] = self.source_node
        return d


class SubscriptionStore:
    """Manages client TG subscriptions with config validation and cluster replication."""

    def __init__(self, broadcast_callback: Optional[Callable] = None):
        # repeater_id (int) -> Subscription
        self._subscriptions: Dict[int, Subscription] = {}
        self._broadcast_callback = broadcast_callback

    def subscribe(self, repeater_id: int,
                  slot1_requested: Optional[List[int]],
                  slot2_requested: Optional[List[int]],
                  slot1_allowed: Optional[List[int]],
                  slot2_allowed: Optional[List[int]]) -> Subscription:
        """Process a subscription request.

        Args:
            repeater_id: Client repeater ID
            slot1_requested: TGs client wants on slot 1 (None = all allowed)
            slot2_requested: TGs client wants on slot 2 (None = all allowed)
            slot1_allowed: TGs config allows on slot 1 (None = allow all)
            slot2_allowed: TGs config allows on slot 2 (None = allow all)

        Returns:
            The effective Subscription (intersection of requested and allowed).
        """
        # Compute effective TGs: intersection of requested and allowed
        s1 = self._intersect(slot1_requested, slot1_allowed)
        s2 = self._intersect(slot2_requested, slot2_allowed)

        sub = Subscription(
            repeater_id=repeater_id,
            slot1_talkgroups=s1,
            slot2_talkgroups=s2,
            updated_at=time.time(),
        )
        self._subscriptions[repeater_id] = sub

        # Broadcast to cluster
        if self._broadcast_callback:
            self._broadcast_callback({
                'type': 'subscription_update',
                'subscription': sub.to_dict(),
            })

        logger.info(f'Subscription updated: repeater {repeater_id}, '
                    f'slot1={len(s1)} TGs, slot2={len(s2)} TGs')
        return sub

    def unsubscribe(self, repeater_id: int):
        """Remove subscription for a repeater."""
        if repeater_id in self._subscriptions:
            del self._subscriptions[repeater_id]
            if self._broadcast_callback:
                self._broadcast_callback({
                    'type': 'subscription_remove',
                    'repeater_id': repeater_id,
                })
            logger.info(f'Subscription removed: repeater {repeater_id}')

    def get_subscription(self, repeater_id: int) -> Optional[Subscription]:
        """Get subscription for a repeater."""
        return self._subscriptions.get(repeater_id)

    def get_subscribers(self, slot: int, talkgroup: int) -> List[int]:
        """Find all repeater_ids subscribed to a TG on a slot.

        Returns list of repeater_ids that have this TG in their subscription.
        """
        result = []
        tg_attr = f'slot{slot}_talkgroups'
        for rid, sub in self._subscriptions.items():
            tgs = getattr(sub, tg_attr, set())
            if not tgs or talkgroup in tgs:
                # Empty set after intersection means "none" — skip
                # But we use empty set to mean "no TGs" not "all TGs"
                if talkgroup in tgs:
                    result.append(rid)
        return result

    def merge_remote(self, sub_dict: dict, source_node: str):
        """Merge a subscription update from a cluster peer."""
        rid = sub_dict['repeater_id']
        sub = Subscription(
            repeater_id=rid,
            slot1_talkgroups=set(sub_dict.get('slot1_talkgroups', [])),
            slot2_talkgroups=set(sub_dict.get('slot2_talkgroups', [])),
            updated_at=sub_dict.get('updated_at', time.time()),
            source_node=source_node,
        )
        self._subscriptions[rid] = sub

    def remove_remote(self, repeater_id: int):
        """Remove a remote subscription (peer sent subscription_remove)."""
        sub = self._subscriptions.get(repeater_id)
        if sub and sub.source_node:
            del self._subscriptions[repeater_id]

    def remove_by_node(self, node_id: str):
        """Remove all subscriptions from a disconnected peer."""
        stale = [rid for rid, sub in self._subscriptions.items()
                 if sub.source_node == node_id]
        for rid in stale:
            del self._subscriptions[rid]
        if stale:
            logger.info(f'Removed {len(stale)} subscription(s) from {node_id}')

    def get_all(self) -> List[dict]:
        """Get all subscriptions as dicts (for sync/dashboard)."""
        return [sub.to_dict() for sub in self._subscriptions.values()]

    def set_broadcast_callback(self, callback: Callable):
        self._broadcast_callback = callback

    @staticmethod
    def _intersect(requested: Optional[List[int]],
                   allowed: Optional[List[int]]) -> Set[int]:
        """Intersect requested and allowed TG lists.

        - Both None: empty set (would mean "all" but we need explicit TGs for routing)
        - requested None, allowed list: use all allowed
        - requested list, allowed None: use all requested (config allows everything)
        - Both lists: intersection
        """
        if requested is None and allowed is None:
            return set()  # Can't subscribe to "everything" — need explicit TGs
        if requested is None:
            return set(allowed) if allowed else set()
        if allowed is None:
            return set(requested)
        return set(requested) & set(allowed)
