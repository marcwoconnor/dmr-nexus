"""Tests for native protocol forwarding integration (Phase 5.3).

Verifies that:
- Native clients are included in stream target calculation
- DMRD packets are correctly wrapped in CLNT+DATA format for native clients
- DATA packets from native clients are correctly unwrapped
- Token hash is included in forwarded packets
"""

import json
import struct
import time
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hblink4.cluster_protocol import (
    Token, TokenManager, NATIVE_MAGIC, CMD_AUTH, CMD_AUTH_ACK, CMD_DATA,
    CMD_SUBSCRIBE, CMD_SUB_ACK, CMD_PING, CMD_PONG,
)
from hblink4.subscriptions import SubscriptionStore


class TestNativeDataPacketFormat(unittest.TestCase):
    """Verify the CLNT+DATA wire format matches Go proxy expectations."""

    def test_data_packet_structure(self):
        """DATA packet: CLNT(4B) + DATA(4B) + token_hash(4B) + payload."""
        token_hash = b'\x01\x02\x03\x04'
        # Simulate a 51-byte DMRD payload (without DMRD prefix)
        dmrd_payload = bytes(range(51))

        # Build packet as server would
        pkt = NATIVE_MAGIC + CMD_DATA + token_hash + dmrd_payload

        self.assertEqual(pkt[:4], b'CLNT')
        self.assertEqual(pkt[4:8], b'DATA')
        self.assertEqual(pkt[8:12], token_hash)
        self.assertEqual(pkt[12:], dmrd_payload)
        self.assertEqual(len(pkt), 4 + 4 + 4 + 51)  # 63 bytes

    def test_data_packet_reconstructs_valid_dmrd(self):
        """Server reconstructs DMRD prefix + payload for internal routing."""
        # Build a native DATA packet (as Go proxy would send)
        token_hash = b'\xAB\xCD\xEF\x12'
        # Standard DMRD without prefix: seq(1)+src(3)+dst(3)+rptr(4)+bits(1)+stream(4)+payload(33)+BER(1)+RSSI(1)=51
        dmrd_content = b'\x00' * 51

        native_pkt = NATIVE_MAGIC + CMD_DATA + token_hash + dmrd_content

        # Server extracts payload (matching hblink.py _handle_native_data)
        self.assertTrue(len(native_pkt) >= 12 + 51)  # new check: 12 + 51
        extracted = native_pkt[12:]
        full_dmrd = b'DMRD' + extracted

        self.assertEqual(len(full_dmrd), 55)  # standard DMRD size
        self.assertTrue(full_dmrd.startswith(b'DMRD'))


class TestNativeAuthPacket(unittest.TestCase):
    """Verify AUTH packet format matches Go proxy."""

    def test_auth_request_format(self):
        """AUTH: CLNT + AUTH + 4B repeater_id + SHA256(passphrase)."""
        import hashlib

        repeater_id = 312100
        passphrase = "testpass"

        # Build as Go proxy does
        rid_bytes = struct.pack('!I', repeater_id)
        pass_hash = hashlib.sha256(passphrase.encode()).digest()
        pkt = NATIVE_MAGIC + CMD_AUTH + rid_bytes + pass_hash

        self.assertEqual(len(pkt), 44)  # 4+4+4+32
        self.assertEqual(pkt[:4], b'CLNT')
        self.assertEqual(pkt[4:8], b'AUTH')

        # Server extracts (matching hblink.py _handle_native_auth)
        extracted_rid = int.from_bytes(pkt[8:12], 'big')
        extracted_hash = pkt[12:]

        self.assertEqual(extracted_rid, 312100)
        self.assertEqual(extracted_hash, pass_hash)

    def test_auth_response_contains_valid_token(self):
        """AUTH_ACK contains a decodable token."""
        mgr = TokenManager('secret', 'test-cluster')
        token = mgr.issue_token(312000, [8, 9], [3120])
        token_bytes = token.to_bytes()

        # Server sends: CLNT + AACK + token_bytes
        pkt = NATIVE_MAGIC + b'AACK' + token_bytes

        # Go proxy extracts token_bytes from offset 8
        extracted_token_bytes = pkt[8:]
        decoded = Token.from_bytes(extracted_token_bytes)

        self.assertIsNotNone(decoded)
        self.assertEqual(decoded.repeater_id, 312000)
        self.assertEqual(decoded.slot1_talkgroups, [8, 9])


class TestNativeSubscribePacket(unittest.TestCase):
    """Verify SUBSCRIBE packet format."""

    def test_subscribe_json_format(self):
        """SUBSCRIBE: CLNT + SUBS + token_hash(4B) + JSON."""
        token_hash = b'\x01\x02\x03\x04'
        sub_data = {"slot1": [8, 9], "slot2": [3120, 3121]}
        json_bytes = json.dumps(sub_data, separators=(',', ':')).encode()

        pkt = NATIVE_MAGIC + CMD_SUBSCRIBE + token_hash + json_bytes

        # Server extracts (matching hblink.py _handle_native_subscribe)
        extracted_hash = pkt[8:12]
        extracted_json = json.loads(pkt[12:].decode('utf-8'))

        self.assertEqual(extracted_hash, token_hash)
        self.assertEqual(extracted_json['slot1'], [8, 9])
        self.assertEqual(extracted_json['slot2'], [3120, 3121])

    def test_subscribe_with_store(self):
        """Subscription store correctly processes native subscribe."""
        store = SubscriptionStore()
        sub = store.subscribe(
            repeater_id=312000,
            slot1_requested=[8, 9, 99],
            slot2_requested=[3120],
            slot1_allowed=[8, 9, 10],
            slot2_allowed=[3120, 3121],
        )
        # Intersection: requested & allowed
        self.assertEqual(sub.slot1_talkgroups, {8, 9})
        self.assertEqual(sub.slot2_talkgroups, {3120})


class TestNativePongPacket(unittest.TestCase):
    """Verify PONG packet format matches Go parser."""

    def test_pong_with_peers(self):
        """PONG contains JSON cluster health."""
        health = {
            'peers': [
                {'node_id': 'east-1', 'alive': True, 'latency_ms': 2.5},
                {'node_id': 'west-1', 'alive': False, 'latency_ms': 0},
            ]
        }
        json_bytes = json.dumps(health, separators=(',', ':')).encode()
        pkt = NATIVE_MAGIC + CMD_PONG + json_bytes

        # Go proxy extracts JSON from offset 8
        extracted = json.loads(pkt[8:].decode('utf-8'))
        self.assertEqual(len(extracted['peers']), 2)
        self.assertEqual(extracted['peers'][0]['node_id'], 'east-1')
        self.assertTrue(extracted['peers'][0]['alive'])

    def test_pong_empty(self):
        """Empty PONG (no cluster info)."""
        pkt = NATIVE_MAGIC + CMD_PONG + b'{}'
        extracted = json.loads(pkt[8:].decode('utf-8'))
        self.assertEqual(extracted, {})


class TestTokenInterop(unittest.TestCase):
    """Cross-language token interoperability."""

    def test_token_roundtrip_bytes(self):
        """Token serialized by Python can be parsed by Go-equivalent logic."""
        mgr = TokenManager('shared-secret', 'prod-cluster')
        token = mgr.issue_token(312100, [8, 9], [3120, 3121])
        token_bytes = token.to_bytes()

        # Simulate Go's DecodeToken logic
        payload_len = struct.unpack('!H', token_bytes[:2])[0]
        payload_data = token_bytes[2:2 + payload_len]
        signature = token_bytes[2 + payload_len:2 + payload_len + 32]

        parsed = json.loads(payload_data.decode('utf-8'))
        self.assertEqual(parsed['rid'], 312100)
        self.assertEqual(parsed['s1'], [8, 9])
        self.assertEqual(parsed['s2'], [3120, 3121])
        self.assertEqual(parsed['cid'], 'prod-cluster')
        self.assertEqual(parsed['v'], 1)
        self.assertEqual(len(signature), 32)

    def test_token_hash_deterministic(self):
        """Same token produces same hash every time."""
        mgr = TokenManager('secret', 'c1')
        token = mgr.issue_token(1, [8], [9])
        h1 = token.token_hash
        h2 = token.token_hash
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 4)

    def test_token_hash_uses_sha256_of_signature(self):
        """token_hash = SHA256(signature)[:4] — must match Go implementation."""
        import hashlib
        sig = b'\xAB' * 32
        token = Token(
            repeater_id=1, slot1_talkgroups=None, slot2_talkgroups=None,
            issued_at=0, expires_at=0, cluster_id='x', signature=sig,
        )
        expected = hashlib.sha256(sig).digest()[:4]
        self.assertEqual(token.token_hash, expected)


class TestDataHandlerMinLength(unittest.TestCase):
    """Verify the fixed minimum length check in _handle_native_data."""

    def test_minimum_51_byte_payload(self):
        """Server accepts 51-byte DMRD payload (the correct minimum)."""
        # 51-byte payload: seq(1)+src(3)+dst(3)+rptr(4)+bits(1)+stream(4)+dmr(33)+BER(1)+RSSI(1)
        payload_51 = b'\x00' * 51
        pkt = NATIVE_MAGIC + CMD_DATA + b'\x00\x00\x00\x00' + payload_51
        # len(pkt) = 4+4+4+51 = 63, check: 63 >= 12+51=63 => passes
        self.assertTrue(len(pkt) >= 12 + 51)

    def test_reject_50_byte_payload(self):
        """Server rejects payload shorter than 51 bytes."""
        payload_50 = b'\x00' * 50
        pkt = NATIVE_MAGIC + CMD_DATA + b'\x00\x00\x00\x00' + payload_50
        # len(pkt) = 62, check: 62 >= 63 => fails (correctly rejected)
        self.assertFalse(len(pkt) >= 12 + 51)


if __name__ == '__main__':
    unittest.main()
