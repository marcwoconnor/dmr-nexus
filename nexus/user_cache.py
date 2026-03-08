#!/usr/bin/env python3
"""
User Routing Cache for HBlink4

Tracks DMR users (radio IDs) and their last heard repeater for:
1. Dashboard "Last Heard" display
2. Private call routing optimization (avoids flooding all repeaters)

The cache is time-limited to prevent unbounded memory growth.
Default timeout: 10 minutes (configurable)

Copyright (C) 2025 Cort Buffington, N0MJS
License: GNU GPLv3
"""

import logging
from time import time
from typing import Dict, Optional, List, Tuple, Callable
from dataclasses import dataclass, field

LOGGER = logging.getLogger(__name__)

# Max queued user_heard entries before forcing a flush
_BATCH_MAX = 50


@dataclass
class UserEntry:
    """Cache entry for a heard user"""
    radio_id: int
    repeater_id: int
    callsign: str
    slot: int
    talkgroup: int
    last_heard: float = field(default_factory=time)
    talker_alias: Optional[str] = None
    source_node: Optional[str] = None  # Cluster node_id (None = local)
    region_id: Optional[str] = None    # Region where user was heard (None = local region)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization"""
        d = {
            'radio_id': self.radio_id,
            'repeater_id': self.repeater_id,
            'callsign': self.callsign,
            'slot': self.slot,
            'talkgroup': self.talkgroup,
            'last_heard': self.last_heard,
            'talker_alias': self.talker_alias
        }
        if self.source_node:
            d['source_node'] = self.source_node
        if self.region_id:
            d['region_id'] = self.region_id
        return d


class UserCache:
    """
    Routing cache for tracking users and their last heard repeater.
    
    This cache serves two purposes:
    1. Provides data for dashboard "Last Heard" display
    2. Enables efficient private call routing without flooding all repeaters
    
    Memory management: Entries expire after configurable timeout (default 10 minutes)
    to prevent unbounded growth.
    """
    
    def __init__(self, timeout_seconds: int = 600):
        """
        Initialize user cache.

        Args:
            timeout_seconds: How long to keep entries (default 600 = 10 minutes)
        """
        self._cache: Dict[int, UserEntry] = {}
        self._timeout = timeout_seconds

        # Cluster broadcast support (Phase 1.3)
        self._broadcast_callback: Optional[Callable] = None
        self._broadcast_queue: List[dict] = []
        self._last_broadcast: float = 0.0
        self._broadcast_interval: float = 1.0  # Max 1 broadcast per second

        LOGGER.info(f'User cache initialized with {timeout_seconds}s timeout')
    
    def set_broadcast_callback(self, callback: Callable) -> None:
        """Set callback for broadcasting user_heard to cluster peers.

        The callback receives a list of user_heard dicts to broadcast.
        """
        self._broadcast_callback = callback

    def update(self, radio_id: int, repeater_id: int, callsign: str,
               slot: int, talkgroup: int, talker_alias: Optional[str] = None,
               source_node: Optional[str] = None) -> None:
        """
        Update cache with user activity.

        Args:
            radio_id: Source radio ID (user)
            repeater_id: Repeater the user was heard on
            callsign: User's callsign
            slot: Timeslot (1 or 2)
            talkgroup: Talkgroup/destination ID
            talker_alias: Optional decoded talker alias
            source_node: Cluster node_id (None = local)
        """
        now = time()

        # Update or create entry
        if radio_id in self._cache:
            entry = self._cache[radio_id]
            entry.repeater_id = repeater_id
            entry.callsign = callsign
            entry.slot = slot
            entry.talkgroup = talkgroup
            entry.last_heard = now
            entry.source_node = source_node
            if talker_alias:
                entry.talker_alias = talker_alias
            LOGGER.debug(f'Updated cache: user {radio_id} ({callsign}) on repeater {repeater_id} slot {slot} TG {talkgroup}')
        else:
            self._cache[radio_id] = UserEntry(
                radio_id=radio_id,
                repeater_id=repeater_id,
                callsign=callsign,
                slot=slot,
                talkgroup=talkgroup,
                last_heard=now,
                talker_alias=talker_alias,
                source_node=source_node,
            )
            LOGGER.debug(f'Added to cache: user {radio_id} ({callsign}) on repeater {repeater_id} slot {slot} TG {talkgroup}')

        # Queue cluster broadcast for local entries only (don't re-broadcast remote)
        if source_node is None and self._broadcast_callback:
            self._broadcast_queue.append({
                'radio_id': radio_id,
                'repeater_id': repeater_id,
                'slot': slot,
                'talkgroup': talkgroup,
            })
            if len(self._broadcast_queue) >= _BATCH_MAX:
                self.flush_broadcast()
    
    def flush_broadcast(self) -> None:
        """Flush queued user_heard entries to cluster peers (throttled)."""
        now = time()
        if not self._broadcast_queue:
            return
        if now - self._last_broadcast < self._broadcast_interval:
            return  # Throttled — will flush on next cleanup cycle or batch overflow

        batch = self._broadcast_queue
        self._broadcast_queue = []
        self._last_broadcast = now

        if self._broadcast_callback:
            try:
                self._broadcast_callback(batch)
            except Exception as e:
                LOGGER.warning(f'User cache broadcast failed: {e}')

    def merge_remote(self, entries: list, source_node: str) -> None:
        """Merge user_heard entries from a cluster peer.

        Args:
            entries: List of dicts with radio_id, repeater_id, slot, talkgroup
            source_node: Cluster node_id of the source
        """
        for entry in entries:
            self.update(
                radio_id=entry['radio_id'],
                repeater_id=entry['repeater_id'],
                callsign='',
                slot=entry.get('slot', 0),
                talkgroup=entry.get('talkgroup', 0),
                source_node=source_node,
            )

    def lookup(self, radio_id: int) -> Optional[UserEntry]:
        """
        Look up a user in the cache.
        
        Args:
            radio_id: Radio ID to look up
            
        Returns:
            UserEntry if found and not expired, None otherwise
        """
        if radio_id not in self._cache:
            return None
        
        entry = self._cache[radio_id]
        
        # Check if expired
        if time() - entry.last_heard > self._timeout:
            del self._cache[radio_id]
            LOGGER.debug(f'Removed expired entry for user {radio_id}')
            return None
        
        return entry
    
    def get_repeater_for_user(self, radio_id: int) -> Optional[int]:
        """
        Get the repeater ID where a user was last heard.
        
        This is the primary function for private call routing optimization.
        
        Args:
            radio_id: Target radio ID for private call
            
        Returns:
            Repeater ID if user found and not expired, None otherwise
        """
        entry = self.lookup(radio_id)
        return entry.repeater_id if entry else None
    
    def cleanup(self) -> int:
        """
        Remove expired entries from cache and flush broadcast queue.

        This should be called periodically (e.g., once per minute) to prevent
        unbounded memory growth.

        Returns:
            Number of entries removed
        """
        # Flush any pending broadcasts
        self.flush_broadcast()

        now = time()
        expired = []

        for radio_id, entry in self._cache.items():
            if now - entry.last_heard > self._timeout:
                expired.append(radio_id)

        for radio_id in expired:
            del self._cache[radio_id]

        if expired:
            LOGGER.info(f'Cleaned up {len(expired)} expired user cache entries')

        return len(expired)
    
    def get_last_heard(self, limit: int = 50) -> List[dict]:
        """
        Get list of recently heard users, sorted by most recent first.
        
        Args:
            limit: Maximum number of entries to return
            
        Returns:
            List of user entries as dictionaries, sorted by last_heard descending
        """
        # Filter out expired entries
        now = time()
        valid_entries = [
            entry for entry in self._cache.values()
            if now - entry.last_heard <= self._timeout
        ]
        
        # Sort by last heard (most recent first)
        sorted_entries = sorted(valid_entries, key=lambda e: e.last_heard, reverse=True)
        
        # Limit results
        return [entry.to_dict() for entry in sorted_entries[:limit]]
    
    def get_stats(self) -> dict:
        """
        Get cache statistics.
        
        Returns:
            Dictionary with cache statistics
        """
        now = time()
        valid = sum(1 for entry in self._cache.values() 
                   if now - entry.last_heard <= self._timeout)
        
        return {
            'total_entries': len(self._cache),
            'valid_entries': valid,
            'expired_entries': len(self._cache) - valid,
            'timeout_seconds': self._timeout
        }
    
    def clear(self) -> None:
        """Clear all entries from cache."""
        count = len(self._cache)
        self._cache.clear()
        LOGGER.info(f'Cleared {count} entries from user cache')
