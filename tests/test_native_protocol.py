"""Tests for cluster-native protocol (Phase 5.1) and subscriptions (Phase 5.2)."""

import json
import time
import unittest
from unittest.mock import MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from nexus.cluster_protocol import (
    Token, TokenManager, NATIVE_MAGIC, CMD_AUTH, CMD_AUTH_ACK, CMD_AUTH_NAK,
    CMD_SUBSCRIBE, CMD_PING, CMD_PONG, CMD_DATA, CMD_DISCONNECT, CMD_SUB_ACK,
    DEFAULT_TOKEN_TTL,
)
from nexus.subscriptions import SubscriptionStore, Subscription


# ========== Token Tests (Phase 5.1) ==========

class TestToken(unittest.TestCase):
    """Token serialization and properties."""

    def test_token_roundtrip(self):
        """Token serializes and deserializes correctly."""
        t = Token(
            repeater_id=312000,
            slot1_talkgroups=[8, 9],
            slot2_talkgroups=[3120],
            issued_at=1000.0,
            expires_at=2000.0,
            cluster_id='test-cluster',
            signature=b'\x01' * 32,
        )
        data = t.to_bytes()
        t2 = Token.from_bytes(data)
        self.assertIsNotNone(t2)
        self.assertEqual(t2.repeater_id, 312000)
        self.assertEqual(t2.slot1_talkgroups, [8, 9])
        self.assertEqual(t2.slot2_talkgroups, [3120])
        self.assertEqual(t2.cluster_id, 'test-cluster')
        self.assertEqual(t2.signature, b'\x01' * 32)

    def test_token_expired(self):
        """Expired token is detected."""
        t = Token(repeater_id=1, slot1_talkgroups=None, slot2_talkgroups=None,
                  issued_at=1000.0, expires_at=1001.0, cluster_id='x')
        self.assertTrue(t.expired)

    def test_token_not_expired(self):
        """Non-expired token."""
        t = Token(repeater_id=1, slot1_talkgroups=None, slot2_talkgroups=None,
                  issued_at=time.time(), expires_at=time.time() + 3600,
                  cluster_id='x')
        self.assertFalse(t.expired)

    def test_token_hash_is_4_bytes(self):
        """token_hash is 4 bytes derived from signature."""
        t = Token(repeater_id=1, slot1_talkgroups=None, slot2_talkgroups=None,
                  issued_at=0, expires_at=0, cluster_id='x',
                  signature=b'\xab' * 32)
        self.assertEqual(len(t.token_hash), 4)

    def test_from_bytes_too_short(self):
        """Too-short data returns None."""
        self.assertIsNone(Token.from_bytes(b'\x00' * 10))

    def test_from_bytes_corrupt(self):
        """Corrupt data returns None."""
        self.assertIsNone(Token.from_bytes(b'\xff' * 100))

    def test_token_allow_all(self):
        """Token with None TG lists (allow all)."""
        t = Token(repeater_id=1, slot1_talkgroups=None, slot2_talkgroups=None,
                  issued_at=0, expires_at=0, cluster_id='x', signature=b'\x00' * 32)
        data = t.to_bytes()
        t2 = Token.from_bytes(data)
        self.assertIsNone(t2.slot1_talkgroups)
        self.assertIsNone(t2.slot2_talkgroups)


class TestTokenManager(unittest.TestCase):
    """Token issuance and validation."""

    def setUp(self):
        self.mgr = TokenManager('test-secret', 'cluster-1')

    def test_issue_and_validate(self):
        """Issued token validates successfully."""
        token = self.mgr.issue_token(312000, [8, 9], [3120])
        valid, reason = self.mgr.validate_token(token)
        self.assertTrue(valid)
        self.assertEqual(reason, 'ok')

    def test_validate_wrong_secret(self):
        """Token fails validation with different secret."""
        token = self.mgr.issue_token(312000, [8], [3120])
        other = TokenManager('wrong-secret', 'cluster-1')
        valid, reason = other.validate_token(token)
        self.assertFalse(valid)
        self.assertEqual(reason, 'invalid signature')

    def test_validate_wrong_cluster(self):
        """Token fails validation for different cluster."""
        token = self.mgr.issue_token(312000, [8], [3120])
        other = TokenManager('test-secret', 'cluster-2')
        valid, reason = other.validate_token(token)
        self.assertFalse(valid)
        self.assertEqual(reason, 'wrong cluster')

    def test_validate_expired(self):
        """Expired token fails validation."""
        mgr = TokenManager('test-secret', 'cluster-1', token_ttl=0.0)
        token = mgr.issue_token(312000, [8], [3120])
        # Token expires immediately (ttl=0)
        time.sleep(0.01)
        valid, reason = mgr.validate_token(token)
        self.assertFalse(valid)
        self.assertEqual(reason, 'token expired')

    def test_token_hash_lookup(self):
        """Token can be found by its 4-byte hash."""
        token = self.mgr.issue_token(312000, [8], [3120])
        found = self.mgr.validate_token_hash(token.token_hash)
        self.assertIsNotNone(found)
        self.assertEqual(found.repeater_id, 312000)

    def test_token_hash_not_found(self):
        """Unknown hash returns None."""
        self.assertIsNone(self.mgr.validate_token_hash(b'\xff\xff\xff\xff'))

    def test_token_hash_expired_cleanup(self):
        """Expired token is removed from cache on lookup."""
        mgr = TokenManager('test-secret', 'cluster-1', token_ttl=0.0)
        token = mgr.issue_token(312000, [8], [3120])
        time.sleep(0.01)
        result = mgr.validate_token_hash(token.token_hash)
        self.assertIsNone(result)
        self.assertEqual(mgr.cache_size(), 0)

    def test_cleanup_expired(self):
        """cleanup_expired removes stale entries."""
        mgr = TokenManager('test-secret', 'cluster-1', token_ttl=0.0)
        mgr.issue_token(1, None, None)
        mgr.issue_token(2, None, None)
        time.sleep(0.01)
        mgr.cleanup_expired()
        self.assertEqual(mgr.cache_size(), 0)

    def test_any_server_validation(self):
        """Token issued by one server validates on another with same secret."""
        server_a = TokenManager('shared-secret', 'my-cluster')
        server_b = TokenManager('shared-secret', 'my-cluster')
        token = server_a.issue_token(312000, [8, 9], [3120])
        # Serialize and deserialize (simulates wire transfer)
        token_bytes = token.to_bytes()
        token_on_b = Token.from_bytes(token_bytes)
        valid, reason = server_b.validate_token(token_on_b)
        self.assertTrue(valid)
        self.assertEqual(reason, 'ok')


# ========== Subscription Tests (Phase 5.2) ==========

class TestSubscription(unittest.TestCase):
    """Subscription dataclass."""

    def test_to_dict(self):
        sub = Subscription(repeater_id=312000,
                           slot1_talkgroups={8, 9}, slot2_talkgroups={3120})
        d = sub.to_dict()
        self.assertEqual(d['repeater_id'], 312000)
        self.assertEqual(sorted(d['slot1_talkgroups']), [8, 9])
        self.assertNotIn('source_node', d)

    def test_to_dict_remote(self):
        sub = Subscription(repeater_id=1, slot1_talkgroups=set(),
                           slot2_talkgroups=set(), source_node='node-b')
        d = sub.to_dict()
        self.assertEqual(d['source_node'], 'node-b')


class TestSubscriptionStore(unittest.TestCase):
    """Subscription store with config validation and replication."""

    def test_subscribe_intersection(self):
        """Subscription is intersection of requested and allowed."""
        store = SubscriptionStore()
        sub = store.subscribe(312000,
                              slot1_requested=[1, 2, 8, 99],
                              slot2_requested=[3120, 9999],
                              slot1_allowed=[1, 2, 3, 8],
                              slot2_allowed=[3120, 3121])
        self.assertEqual(sub.slot1_talkgroups, {1, 2, 8})
        self.assertEqual(sub.slot2_talkgroups, {3120})

    def test_subscribe_all_requested_none_allowed(self):
        """Requested=None + allowed=list => use all allowed."""
        store = SubscriptionStore()
        sub = store.subscribe(1, slot1_requested=None, slot2_requested=None,
                              slot1_allowed=[8, 9], slot2_allowed=[3120])
        self.assertEqual(sub.slot1_talkgroups, {8, 9})
        self.assertEqual(sub.slot2_talkgroups, {3120})

    def test_subscribe_requested_allowed_none(self):
        """Requested=list + allowed=None => use all requested."""
        store = SubscriptionStore()
        sub = store.subscribe(1, slot1_requested=[1, 2], slot2_requested=[3],
                              slot1_allowed=None, slot2_allowed=None)
        self.assertEqual(sub.slot1_talkgroups, {1, 2})
        self.assertEqual(sub.slot2_talkgroups, {3})

    def test_subscribe_both_none(self):
        """Both None => empty set (can't subscribe to everything)."""
        store = SubscriptionStore()
        sub = store.subscribe(1, None, None, None, None)
        self.assertEqual(sub.slot1_talkgroups, set())
        self.assertEqual(sub.slot2_talkgroups, set())

    def test_get_subscribers(self):
        """get_subscribers finds repeaters with matching TG."""
        store = SubscriptionStore()
        store.subscribe(1, [8], [3120], None, None)
        store.subscribe(2, [8, 9], [3121], None, None)
        store.subscribe(3, [9], [3120], None, None)

        subs = store.get_subscribers(1, 8)
        self.assertIn(1, subs)
        self.assertIn(2, subs)
        self.assertNotIn(3, subs)

        subs = store.get_subscribers(2, 3120)
        self.assertIn(1, subs)
        self.assertNotIn(2, subs)
        self.assertIn(3, subs)

    def test_unsubscribe(self):
        """Unsubscribe removes the subscription."""
        store = SubscriptionStore()
        store.subscribe(1, [8], [9], None, None)
        store.unsubscribe(1)
        self.assertIsNone(store.get_subscription(1))

    def test_broadcast_on_subscribe(self):
        """Subscribe triggers broadcast callback."""
        broadcasts = []
        store = SubscriptionStore(broadcast_callback=broadcasts.append)
        store.subscribe(1, [8], [9], None, None)
        self.assertEqual(len(broadcasts), 1)
        self.assertEqual(broadcasts[0]['type'], 'subscription_update')

    def test_broadcast_on_unsubscribe(self):
        """Unsubscribe triggers broadcast callback."""
        broadcasts = []
        store = SubscriptionStore(broadcast_callback=broadcasts.append)
        store.subscribe(1, [8], [9], None, None)
        store.unsubscribe(1)
        self.assertEqual(broadcasts[-1]['type'], 'subscription_remove')

    def test_merge_remote(self):
        """Remote subscription is merged with source_node."""
        store = SubscriptionStore()
        store.merge_remote({
            'repeater_id': 999,
            'slot1_talkgroups': [8],
            'slot2_talkgroups': [3120],
        }, 'node-b')
        sub = store.get_subscription(999)
        self.assertIsNotNone(sub)
        self.assertEqual(sub.source_node, 'node-b')
        self.assertEqual(sub.slot1_talkgroups, {8})

    def test_remove_by_node(self):
        """remove_by_node purges all subscriptions from a peer."""
        store = SubscriptionStore()
        store.merge_remote({'repeater_id': 1, 'slot1_talkgroups': [], 'slot2_talkgroups': []}, 'node-b')
        store.merge_remote({'repeater_id': 2, 'slot1_talkgroups': [], 'slot2_talkgroups': []}, 'node-b')
        store.subscribe(3, [8], [9], None, None)  # Local
        store.remove_by_node('node-b')
        self.assertIsNone(store.get_subscription(1))
        self.assertIsNone(store.get_subscription(2))
        self.assertIsNotNone(store.get_subscription(3))  # Local survives

    def test_get_all(self):
        """get_all returns all subscriptions as dicts."""
        store = SubscriptionStore()
        store.subscribe(1, [8], [9], None, None)
        store.subscribe(2, [1, 2], [3], None, None)
        result = store.get_all()
        self.assertEqual(len(result), 2)
        ids = {r['repeater_id'] for r in result}
        self.assertEqual(ids, {1, 2})


if __name__ == '__main__':
    unittest.main()
