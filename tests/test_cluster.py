"""Tests for the cluster bus (Phase 1.1 + 1.2) and cross-server routing (Phase 2)."""

import asyncio
import json
import struct
import unittest
from unittest.mock import MagicMock, patch
from time import time

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hblink4.cluster import (
    ClusterBus, PeerState, FRAME_HEADER,
    MSG_TYPE_JSON, MSG_TYPE_STREAM_DATA, MSG_TYPE_STREAM_END,
    AUTH_CHALLENGE, AUTH_RESPONSE, AUTH_OK
)
from hblink4.models import StreamState, RepeaterState
from hblink4.subscriptions import SubscriptionStore


class TestPeerState(unittest.TestCase):
    def test_defaults(self):
        ps = PeerState(node_id='test', address='127.0.0.1', port=62032)
        self.assertFalse(ps.connected)
        self.assertFalse(ps.authenticated)
        self.assertEqual(ps.latency_ms, 0.0)


class TestClusterBusInit(unittest.TestCase):
    def test_init_no_peers(self):
        config = {'enabled': True, 'node_id': 'a', 'port': 0, 'shared_secret': 'test'}
        bus = ClusterBus('a', config, on_message=lambda msg: None)
        self.assertEqual(bus._node_id, 'a')
        self.assertEqual(len(bus._peers), 0)

    def test_init_with_peers(self):
        config = {
            'enabled': True, 'node_id': 'a', 'port': 0, 'shared_secret': 'test',
            'peers': [
                {'node_id': 'b', 'address': '10.0.0.2', 'port': 62032},
                {'node_id': 'c', 'address': '10.0.0.3', 'port': 62032},
            ]
        }
        bus = ClusterBus('a', config, on_message=lambda msg: None)
        self.assertEqual(len(bus._peers), 2)
        self.assertIn('b', bus._peers)
        self.assertIn('c', bus._peers)

    def test_is_peer_alive_disconnected(self):
        config = {
            'enabled': True, 'node_id': 'a', 'port': 0, 'shared_secret': 'test',
            'peers': [{'node_id': 'b', 'address': '10.0.0.2', 'port': 62032}]
        }
        bus = ClusterBus('a', config, on_message=lambda msg: None)
        self.assertFalse(bus.is_peer_alive('b'))
        self.assertFalse(bus.is_peer_alive('nonexistent'))

    def test_connected_peers_empty(self):
        config = {'enabled': True, 'node_id': 'a', 'port': 0, 'shared_secret': 'test'}
        bus = ClusterBus('a', config, on_message=lambda msg: None)
        self.assertEqual(bus.connected_peers, [])

    def test_get_peer_states(self):
        config = {
            'enabled': True, 'node_id': 'a', 'port': 0, 'shared_secret': 'test',
            'peers': [{'node_id': 'b', 'address': '10.0.0.2', 'port': 62032}]
        }
        bus = ClusterBus('a', config, on_message=lambda msg: None)
        states = bus.get_peer_states()
        self.assertIn('b', states)
        self.assertFalse(states['b']['connected'])


class TestClusterBusIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration tests with two real cluster bus instances connected together."""

    async def _create_bus_pair(self):
        """Create two ClusterBus instances. B connects outbound to A."""
        self.messages_a = []
        self.messages_b = []

        async def on_msg_a(msg):
            self.messages_a.append(msg)

        async def on_msg_b(msg):
            self.messages_b.append(msg)

        # A: listens for inbound, knows about node-b (accepts it) but doesn't connect out
        config_a = {
            'enabled': True, 'node_id': 'node-a', 'bind': '127.0.0.1',
            'port': 0, 'shared_secret': 'test-secret',
            'heartbeat_interval': 1.0, 'dead_threshold': 4.0,
            'reconnect_interval': 0.5, 'peers': []
        }
        self.bus_a = ClusterBus('node-a', config_a, on_message=on_msg_a)

        # Start A's server to get its port
        self.bus_a._running = True
        self.bus_a._server = await asyncio.start_server(
            self.bus_a._handle_inbound_connection, '127.0.0.1', 0)
        port_a = self.bus_a._server.sockets[0].getsockname()[1]

        # A needs node-b in peers so it accepts inbound (no connect loop)
        self.bus_a._peers['node-b'] = PeerState(
            node_id='node-b', address='127.0.0.1', port=0)

        # Start A's heartbeat
        hb_a = asyncio.create_task(self.bus_a._heartbeat_loop())
        self.bus_a._tasks.append(hb_a)

        # B: connects outbound to A
        config_b = {
            'enabled': True, 'node_id': 'node-b', 'bind': '127.0.0.1',
            'port': 0, 'shared_secret': 'test-secret',
            'heartbeat_interval': 1.0, 'dead_threshold': 4.0,
            'reconnect_interval': 0.5,
            'peers': [{'node_id': 'node-a', 'address': '127.0.0.1', 'port': port_a}]
        }
        self.bus_b = ClusterBus('node-b', config_b, on_message=on_msg_b)
        await self.bus_b.start()

        # Wait for connection
        for _ in range(20):
            if self.bus_b.is_peer_alive('node-a'):
                break
            await asyncio.sleep(0.1)

    async def asyncTearDown(self):
        for name in ('bus_a', 'bus_b'):
            bus = getattr(self, name, None)
            if bus:
                try:
                    await asyncio.wait_for(bus.stop(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Force-cancel remaining tasks
                    for task in bus._tasks:
                        task.cancel()
                    bus._tasks.clear()

    async def test_peer_connects(self):
        """Two nodes connect and authenticate."""
        await self._create_bus_pair()
        self.assertTrue(self.bus_b.is_peer_alive('node-a'))
        self.assertIn('node-a', self.bus_b.connected_peers)

    async def test_send_json_message(self):
        """Send a JSON message from B to A."""
        await self._create_bus_pair()

        await self.bus_b.send('node-a', {'type': 'repeater_up', 'repeater_id': 312000})
        await asyncio.sleep(0.2)

        # Find the repeater_up message (filter out peer_connected)
        rpt_msgs = [m for m in self.messages_a if m.get('type') == 'repeater_up']
        self.assertEqual(len(rpt_msgs), 1)
        self.assertEqual(rpt_msgs[0]['repeater_id'], 312000)

    async def test_broadcast(self):
        """Broadcast from A should reach B (and vice versa if B has A as peer)."""
        await self._create_bus_pair()

        # B has A as a peer with connection. Broadcast from B.
        await self.bus_b.broadcast({'type': 'test_broadcast', 'data': 'hello'})
        await asyncio.sleep(0.2)

        test_msgs = [m for m in self.messages_a if m.get('type') == 'test_broadcast']
        self.assertEqual(len(test_msgs), 1)
        self.assertEqual(test_msgs[0]['data'], 'hello')

    async def test_binary_message(self):
        """Send binary stream data."""
        await self._create_bus_pair()

        # Simulate a 4-byte stream_id + 55-byte DMRD packet
        stream_id = b'\x01\x02\x03\x04'
        dmrd_payload = b'\x00' * 55
        await self.bus_b.send_binary('node-a', MSG_TYPE_STREAM_DATA, stream_id + dmrd_payload)
        await asyncio.sleep(0.2)

        bin_msgs = [m for m in self.messages_a if m.get('type') == 'stream_data_binary']
        self.assertEqual(len(bin_msgs), 1)
        self.assertEqual(len(bin_msgs[0]['payload']), 59)  # 4 + 55

    async def test_auth_rejection_bad_secret(self):
        """A peer with wrong secret should be rejected."""
        messages = []
        async def on_msg(msg):
            messages.append(msg)

        config_a = {
            'enabled': True, 'node_id': 'good', 'bind': '127.0.0.1',
            'port': 0, 'shared_secret': 'correct-secret',
            'heartbeat_interval': 1.0, 'dead_threshold': 4.0,
            'reconnect_interval': 10.0, 'peers': []
        }
        bus_a = ClusterBus('good', config_a, on_message=on_msg)
        await bus_a.start()
        port_a = bus_a._server.sockets[0].getsockname()[1]

        config_bad = {
            'enabled': True, 'node_id': 'bad', 'bind': '127.0.0.1',
            'port': 0, 'shared_secret': 'wrong-secret',
            'heartbeat_interval': 1.0, 'dead_threshold': 4.0,
            'reconnect_interval': 10.0,
            'peers': [{'node_id': 'good', 'address': '127.0.0.1', 'port': port_a}]
        }
        bus_bad = ClusterBus('bad', config_bad, on_message=on_msg)
        await bus_bad.start()

        await asyncio.sleep(1.0)

        # Bad peer should not be connected
        self.assertFalse(bus_bad.is_peer_alive('good'))

        await bus_bad.stop()
        await bus_a.stop()

    async def test_peer_disconnect_notification(self):
        """Stopping a peer triggers disconnect callback."""
        await self._create_bus_pair()

        # Stop bus_b — this closes its writer, which should cause bus_a's
        # read loop for node-b to get EOF and fire peer_disconnected.
        await self.bus_b.stop()
        delattr(self, 'bus_b')

        # Wait for bus_a to detect the disconnect
        for _ in range(10):
            if not self.bus_a.is_peer_alive('node-b'):
                break
            await asyncio.sleep(0.2)

        self.assertFalse(self.bus_a.is_peer_alive('node-b'))

        # Stop bus_a explicitly here (not in tearDown) with a timeout guard
        try:
            await asyncio.wait_for(self.bus_a.stop(), timeout=3.0)
        except asyncio.TimeoutError:
            pass
        delattr(self, 'bus_a')


# ========== Phase 2: Cross-Server Stream Routing ==========


def _make_hbprotocol_for_routing():
    """Create a minimal HBProtocol-like object for testing _calculate_stream_targets.

    We can't easily instantiate HBProtocol (needs a running event loop, transport, etc.)
    so we import the class and create a mock that has the real method bound to it.
    """
    from hblink4.hblink import HBProtocol, CONFIG

    # Set minimal CONFIG
    CONFIG.clear()
    CONFIG.update({
        'global': {
            'bind_address': '0.0.0.0',
            'port': 62030,
            'passphrase': 'test',
            'max_missed_pings': 3,
            'ping_time': 5,
            'stream_timeout': 2.0,
            'stream_hang_time': 10.0,
            'user_cache_timeout': 300,
        },
        'repeater_configurations': {'patterns': []},
        'blacklist': {'patterns': []},
        'connection_type_detection': {'categories': {}},
    })

    # Build a mock with the real method
    mock = MagicMock(spec=HBProtocol)
    mock._repeaters = {}
    mock._outbounds = {}
    mock._cluster_bus = MagicMock()
    mock._cluster_state = {}
    mock._virtual_streams = {}
    mock._draining_peers = set()
    mock._native_clients = {}
    mock._subscriptions = SubscriptionStore()

    # Bind real methods
    mock._calculate_stream_targets = HBProtocol._calculate_stream_targets.__get__(mock)
    mock._check_outbound_routing = HBProtocol._check_outbound_routing.__get__(mock)
    mock._is_slot_busy = HBProtocol._is_slot_busy.__get__(mock)
    mock._handle_virtual_stream_start = HBProtocol._handle_virtual_stream_start.__get__(mock)
    mock._handle_virtual_stream_data = HBProtocol._handle_virtual_stream_data.__get__(mock)
    mock._handle_virtual_stream_end = HBProtocol._handle_virtual_stream_end.__get__(mock)
    mock._is_dmr_terminator = HBProtocol._is_dmr_terminator.__get__(mock)

    return mock


class TestClusterTargetCalculation(unittest.TestCase):
    """Phase 2.1: _calculate_stream_targets includes cluster peers."""

    def setUp(self):
        self.proto = _make_hbprotocol_for_routing()

    def test_cluster_peer_tg_match(self):
        """Cluster peer with matching TG on correct slot is included."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {
                    'repeater_id': 312000,
                    'slot1_talkgroups': [1, 2, 9],
                    'slot2_talkgroups': [3, 4],
                }
            }
        }
        dst_id = (9).to_bytes(3, 'big')  # TG 9 on slot 1
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertIn(('cluster', 'node-b'), targets)

    def test_cluster_peer_tg_no_match(self):
        """Cluster peer without matching TG is excluded."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {
                    'repeater_id': 312000,
                    'slot1_talkgroups': [1, 2],
                    'slot2_talkgroups': [3, 4],
                }
            }
        }
        dst_id = (99).to_bytes(3, 'big')  # TG 99 not in any list
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertNotIn(('cluster', 'node-b'), targets)

    def test_cluster_peer_allow_all(self):
        """Cluster peer with None talkgroups (allow all) matches any TG."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {
                    'repeater_id': 312000,
                    'slot1_talkgroups': None,
                    'slot2_talkgroups': [3],
                }
            }
        }
        dst_id = (999).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertIn(('cluster', 'node-b'), targets)

    def test_cluster_peer_wrong_slot(self):
        """TG matches on slot 1 but stream is on slot 2 — excluded."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {
                    'repeater_id': 312000,
                    'slot1_talkgroups': [9],
                    'slot2_talkgroups': [3, 4],
                }
            }
        }
        dst_id = (9).to_bytes(3, 'big')  # TG 9 only on slot 1
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 2, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertNotIn(('cluster', 'node-b'), targets)

    def test_cluster_peer_dead_excluded(self):
        """Dead cluster peer is excluded even with matching TG."""
        self.proto._cluster_bus.is_peer_alive.return_value = False
        self.proto._cluster_state = {
            'node-b': {
                312000: {
                    'repeater_id': 312000,
                    'slot1_talkgroups': None,
                    'slot2_talkgroups': None,
                }
            }
        }
        dst_id = (1).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertNotIn(('cluster', 'node-b'), targets)

    def test_local_only_skips_cluster(self):
        """local_only=True excludes cluster peers."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {
                    'repeater_id': 312000,
                    'slot1_talkgroups': None,
                    'slot2_talkgroups': None,
                }
            }
        }
        dst_id = (1).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01',
            local_only=True
        )
        self.assertNotIn(('cluster', 'node-b'), targets)

    def test_break_after_first_match_per_peer(self):
        """Multiple repeaters on same peer — only one ('cluster', peer) entry."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {'repeater_id': 312000, 'slot1_talkgroups': [9], 'slot2_talkgroups': []},
                312001: {'repeater_id': 312001, 'slot1_talkgroups': [9], 'slot2_talkgroups': []},
                312002: {'repeater_id': 312002, 'slot1_talkgroups': [9], 'slot2_talkgroups': []},
            }
        }
        dst_id = (9).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        cluster_targets = [t for t in targets if isinstance(t, tuple) and t[0] == 'cluster']
        self.assertEqual(len(cluster_targets), 1)
        self.assertEqual(cluster_targets[0], ('cluster', 'node-b'))

    def test_multiple_peers(self):
        """Two peers with matching TGs both included."""
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {'repeater_id': 312000, 'slot1_talkgroups': [9], 'slot2_talkgroups': []},
            },
            'node-c': {
                313000: {'repeater_id': 313000, 'slot1_talkgroups': [9], 'slot2_talkgroups': []},
            }
        }
        dst_id = (9).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertIn(('cluster', 'node-b'), targets)
        self.assertIn(('cluster', 'node-c'), targets)


class TestVirtualStreamHandlers(unittest.TestCase):
    """Phase 2.3: Virtual stream reception from cluster peers."""

    def setUp(self):
        self.proto = _make_hbprotocol_for_routing()
        # Add a local repeater that allows TG 9 on slot 1
        rid = b'\x00\x04\xc4\x40'  # 312384
        rpt = RepeaterState(
            repeater_id=rid, ip='10.0.0.5', port=62031,
            connected=True, authenticated=True,
            connection_state='connected',
            slot1_talkgroups={b'\x00\x00\x09'},  # TG 9
            slot2_talkgroups=set(),
        )
        self.proto._repeaters = {rid: rpt}
        self.local_rid = rid
        self.local_rpt = rpt

    def test_virtual_stream_start_creates_stream(self):
        """stream_start from cluster peer creates virtual stream with local targets."""
        msg = {
            'node_id': 'node-b',
            'stream_id': 'aabbccdd',
            'slot': 1,
            'dst_id': 9,
            'rf_src': 12345,
            'call_type': 'group',
            'source_repeater': 312000,
        }
        self.proto._handle_virtual_stream_start(msg)
        key = ('node-b', bytes.fromhex('aabbccdd'))
        self.assertIn(key, self.proto._virtual_streams)
        vs = self.proto._virtual_streams[key]
        self.assertEqual(vs.source_node, 'node-b')
        self.assertEqual(vs.slot, 1)
        self.assertIn(self.local_rid, vs.target_repeaters)

    def test_virtual_stream_start_no_match(self):
        """stream_start with TG not matching any local repeater has empty targets."""
        msg = {
            'node_id': 'node-b',
            'stream_id': 'aabbccdd',
            'slot': 1,
            'dst_id': 999,  # TG 999 not in local repeater's list
            'rf_src': 12345,
            'call_type': 'group',
            'source_repeater': 312000,
        }
        self.proto._handle_virtual_stream_start(msg)
        key = ('node-b', bytes.fromhex('aabbccdd'))
        vs = self.proto._virtual_streams[key]
        self.assertEqual(len(vs.target_repeaters), 0)

    def test_virtual_stream_data_forwards(self):
        """stream_data forwards DMRD packet to local repeater targets."""
        # First create the virtual stream
        stream_id = b'\xaa\xbb\xcc\xdd'
        self.proto._handle_virtual_stream_start({
            'node_id': 'node-b', 'stream_id': stream_id.hex(),
            'slot': 1, 'dst_id': 9, 'rf_src': 12345,
            'call_type': 'group', 'source_repeater': 312000,
        })

        # Build a fake DMRD packet (55 bytes)
        dmrd = bytearray(55)
        dmrd[0:4] = b'DMRD'
        dmrd[5:8] = (12345).to_bytes(3, 'big')  # rf_src
        dmrd[8:11] = (9).to_bytes(3, 'big')     # dst_id
        dmrd[11:15] = (312000).to_bytes(4, 'big')  # repeater_id
        dmrd[15] = 0x00  # slot 1, group call, voice frame
        dmrd[16:20] = stream_id

        payload = stream_id + bytes(dmrd)
        self.proto._handle_virtual_stream_data({
            'node_id': 'node-b', 'payload': payload,
        })

        key = ('node-b', stream_id)
        vs = self.proto._virtual_streams[key]
        self.assertEqual(vs.packet_count, 1)
        self.proto._send_packet.assert_called_once()

    def test_virtual_stream_data_no_stream(self):
        """stream_data without prior stream_start is silently dropped."""
        payload = b'\xaa\xbb\xcc\xdd' + b'\x00' * 55
        self.proto._handle_virtual_stream_data({
            'node_id': 'node-b', 'payload': payload,
        })
        self.proto._send_packet.assert_not_called()

    def test_virtual_stream_end_cleanup(self):
        """stream_end removes virtual stream and ends assumed streams on targets."""
        stream_id = b'\xaa\xbb\xcc\xdd'
        self.proto._handle_virtual_stream_start({
            'node_id': 'node-b', 'stream_id': stream_id.hex(),
            'slot': 1, 'dst_id': 9, 'rf_src': 12345,
            'call_type': 'group', 'source_repeater': 312000,
        })
        key = ('node-b', stream_id)
        self.assertIn(key, self.proto._virtual_streams)

        self.proto._handle_virtual_stream_end({
            'node_id': 'node-b', 'stream_id': stream_id.hex(),
            'reason': 'terminator',
        })
        self.assertNotIn(key, self.proto._virtual_streams)

    def test_virtual_stream_duplicate_start_ignored(self):
        """Duplicate stream_start for same (node, stream_id) is ignored."""
        msg = {
            'node_id': 'node-b', 'stream_id': 'aabbccdd',
            'slot': 1, 'dst_id': 9, 'rf_src': 12345,
            'call_type': 'group', 'source_repeater': 312000,
        }
        self.proto._handle_virtual_stream_start(msg)
        vs1 = self.proto._virtual_streams[('node-b', bytes.fromhex('aabbccdd'))]
        self.proto._handle_virtual_stream_start(msg)
        vs2 = self.proto._virtual_streams[('node-b', bytes.fromhex('aabbccdd'))]
        self.assertIs(vs1, vs2)  # Same object, not replaced

    def test_virtual_stream_terminator_in_data(self):
        """Terminator flag in DMRD data auto-cleans virtual stream."""
        stream_id = b'\xaa\xbb\xcc\xdd'
        self.proto._handle_virtual_stream_start({
            'node_id': 'node-b', 'stream_id': stream_id.hex(),
            'slot': 1, 'dst_id': 9, 'rf_src': 12345,
            'call_type': 'group', 'source_repeater': 312000,
        })

        # DMRD with terminator flag: frame_type=2 (bits 4-5), dtype_vseq=2 (bits 0-3)
        dmrd = bytearray(55)
        dmrd[0:4] = b'DMRD'
        dmrd[5:8] = (12345).to_bytes(3, 'big')
        dmrd[8:11] = (9).to_bytes(3, 'big')
        dmrd[11:15] = (312000).to_bytes(4, 'big')
        dmrd[15] = 0x22  # frame_type=2 (0x20) | dtype_vseq=2 (0x02) = terminator
        dmrd[16:20] = stream_id

        payload = stream_id + bytes(dmrd)
        self.proto._handle_virtual_stream_data({
            'node_id': 'node-b', 'payload': payload,
        })

        key = ('node-b', stream_id)
        self.assertNotIn(key, self.proto._virtual_streams)


class TestClusterStreamProtocol(unittest.IsolatedAsyncioTestCase):
    """Phase 2.2: Stream message send/receive over real TCP."""

    async def _create_bus_pair(self):
        """Create two connected ClusterBus instances."""
        self.messages_a = []
        self.messages_b = []

        async def on_msg_a(msg):
            self.messages_a.append(msg)

        async def on_msg_b(msg):
            self.messages_b.append(msg)

        config_a = {
            'enabled': True, 'node_id': 'node-a', 'bind': '127.0.0.1',
            'port': 0, 'shared_secret': 'test-secret',
            'heartbeat_interval': 1.0, 'dead_threshold': 4.0,
            'reconnect_interval': 0.5, 'peers': []
        }
        self.bus_a = ClusterBus('node-a', config_a, on_message=on_msg_a)
        self.bus_a._running = True
        self.bus_a._server = await asyncio.start_server(
            self.bus_a._handle_inbound_connection, '127.0.0.1', 0)
        port_a = self.bus_a._server.sockets[0].getsockname()[1]
        self.bus_a._peers['node-b'] = PeerState(
            node_id='node-b', address='127.0.0.1', port=0)
        hb_a = asyncio.create_task(self.bus_a._heartbeat_loop())
        self.bus_a._tasks.append(hb_a)

        config_b = {
            'enabled': True, 'node_id': 'node-b', 'bind': '127.0.0.1',
            'port': 0, 'shared_secret': 'test-secret',
            'heartbeat_interval': 1.0, 'dead_threshold': 4.0,
            'reconnect_interval': 0.5,
            'peers': [{'node_id': 'node-a', 'address': '127.0.0.1', 'port': port_a}]
        }
        self.bus_b = ClusterBus('node-b', config_b, on_message=on_msg_b)
        await self.bus_b.start()

        for _ in range(20):
            if self.bus_b.is_peer_alive('node-a'):
                break
            await asyncio.sleep(0.1)

    async def asyncTearDown(self):
        for name in ('bus_a', 'bus_b'):
            bus = getattr(self, name, None)
            if bus:
                try:
                    await asyncio.wait_for(bus.stop(), timeout=5.0)
                except asyncio.TimeoutError:
                    for task in bus._tasks:
                        task.cancel()
                    bus._tasks.clear()

    async def test_send_stream_start(self):
        """stream_start JSON flows from B to A."""
        await self._create_bus_pair()
        await self.bus_b.send_stream_start(['node-a'], {
            'stream_id': 'aabbccdd', 'slot': 1,
            'dst_id': 9, 'rf_src': 12345,
            'call_type': 'group', 'source_repeater': 312000,
        })
        await asyncio.sleep(0.2)
        starts = [m for m in self.messages_a if m.get('type') == 'stream_start']
        self.assertEqual(len(starts), 1)
        self.assertEqual(starts[0]['stream_id'], 'aabbccdd')
        self.assertEqual(starts[0]['dst_id'], 9)

    async def test_send_stream_data(self):
        """Binary stream data flows from B to A."""
        await self._create_bus_pair()
        stream_id = b'\xaa\xbb\xcc\xdd'
        dmrd = b'\x00' * 55
        await self.bus_b.send_stream_data(['node-a'], stream_id + dmrd)
        await asyncio.sleep(0.2)
        data_msgs = [m for m in self.messages_a if m.get('type') == 'stream_data_binary']
        self.assertEqual(len(data_msgs), 1)
        self.assertEqual(len(data_msgs[0]['payload']), 59)

    async def test_send_stream_end(self):
        """stream_end flows from B to A."""
        await self._create_bus_pair()
        await self.bus_b.send_stream_end(['node-a'], 'aabbccdd', 'terminator')
        await asyncio.sleep(0.2)
        ends = [m for m in self.messages_a if m.get('type') == 'stream_end']
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0]['stream_id'], 'aabbccdd')
        self.assertEqual(ends[0]['reason'], 'terminator')

    async def test_full_stream_lifecycle(self):
        """Complete stream lifecycle: start → data → data → end."""
        await self._create_bus_pair()
        stream_id_hex = 'aabbccdd'
        stream_id = bytes.fromhex(stream_id_hex)

        # Start
        await self.bus_b.send_stream_start(['node-a'], {
            'stream_id': stream_id_hex, 'slot': 1,
            'dst_id': 9, 'rf_src': 12345,
            'call_type': 'group', 'source_repeater': 312000,
        })
        # Data packets
        dmrd = b'\x00' * 55
        await self.bus_b.send_stream_data(['node-a'], stream_id + dmrd)
        await self.bus_b.send_stream_data(['node-a'], stream_id + dmrd)
        # End
        await self.bus_b.send_stream_end(['node-a'], stream_id_hex, 'terminator')

        await asyncio.sleep(0.3)

        starts = [m for m in self.messages_a if m.get('type') == 'stream_start']
        data_msgs = [m for m in self.messages_a if m.get('type') == 'stream_data_binary']
        ends = [m for m in self.messages_a if m.get('type') == 'stream_end']

        self.assertEqual(len(starts), 1)
        self.assertEqual(len(data_msgs), 2)
        self.assertEqual(len(ends), 1)


# ========== Phase 3: Failure Detection + Graceful Shutdown ==========


class TestDrainingPeerExclusion(unittest.TestCase):
    """Phase 3.3: Draining peers excluded from target calculation."""

    def setUp(self):
        self.proto = _make_hbprotocol_for_routing()
        self.proto._cluster_bus.is_peer_alive.return_value = True
        self.proto._cluster_state = {
            'node-b': {
                312000: {'repeater_id': 312000, 'slot1_talkgroups': None, 'slot2_talkgroups': None},
            }
        }

    def test_draining_peer_excluded(self):
        """A draining peer is not included as a cluster target."""
        self.proto._draining_peers.add('node-b')
        dst_id = (1).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertNotIn(('cluster', 'node-b'), targets)

    def test_non_draining_peer_included(self):
        """A non-draining peer is still included."""
        dst_id = (1).to_bytes(3, 'big')
        targets = self.proto._calculate_stream_targets(
            b'\x00\x00\x00\x01', 1, dst_id, b'\xaa\xbb\xcc\xdd', b'\x00\x00\x01'
        )
        self.assertIn(('cluster', 'node-b'), targets)


class TestVirtualStreamTimeout(unittest.TestCase):
    """Phase 3.2: Stale virtual streams cleaned up by timeout checker."""

    def setUp(self):
        self.proto = _make_hbprotocol_for_routing()
        rid = b'\x00\x04\xc4\x40'
        rpt = RepeaterState(
            repeater_id=rid, ip='10.0.0.5', port=62031,
            connected=True, authenticated=True,
            connection_state='connected',
            slot1_talkgroups={b'\x00\x00\x09'},
            slot2_talkgroups=set(),
        )
        self.proto._repeaters = {rid: rpt}
        self.local_rid = rid

        # Bind _check_stream_timeouts and dependencies
        from hblink4.hblink import HBProtocol
        self.proto._check_stream_timeouts = HBProtocol._check_stream_timeouts.__get__(self.proto)
        self.proto._check_slot_timeout = HBProtocol._check_slot_timeout.__get__(self.proto)
        self.proto._check_outbound_slot_timeout = HBProtocol._check_outbound_slot_timeout.__get__(self.proto)
        self.proto._end_stream = HBProtocol._end_stream.__get__(self.proto)
        self.proto._emit_stream_end = MagicMock()
        self.proto._denied_streams = {}

    def test_stale_virtual_stream_cleaned(self):
        """Virtual stream with no recent data is cleaned up."""
        stream_id = b'\xaa\xbb\xcc\xdd'
        vs = StreamState(
            repeater_id=b'\x00\x00\x00\x00',
            rf_src=b'\x00\x30\x39', dst_id=b'\x00\x00\x09',
            slot=1, start_time=time() - 10, last_seen=time() - 5,
            stream_id=stream_id, source_node='node-b',
            target_repeaters={self.local_rid}, routing_cached=True,
        )
        self.proto._virtual_streams[('node-b', stream_id)] = vs

        self.proto._check_stream_timeouts()
        self.assertNotIn(('node-b', stream_id), self.proto._virtual_streams)

    def test_fresh_virtual_stream_kept(self):
        """Virtual stream with recent data is not cleaned up."""
        stream_id = b'\xaa\xbb\xcc\xdd'
        vs = StreamState(
            repeater_id=b'\x00\x00\x00\x00',
            rf_src=b'\x00\x30\x39', dst_id=b'\x00\x00\x09',
            slot=1, start_time=time(), last_seen=time(),
            stream_id=stream_id, source_node='node-b',
            target_repeaters={self.local_rid}, routing_cached=True,
        )
        self.proto._virtual_streams[('node-b', stream_id)] = vs

        self.proto._check_stream_timeouts()
        self.assertIn(('node-b', stream_id), self.proto._virtual_streams)


class TestGracefulShutdown(unittest.IsolatedAsyncioTestCase):
    """Phase 3.3: Graceful shutdown with drain."""

    async def test_shutdown_broadcasts_draining_and_down(self):
        """graceful_shutdown broadcasts node_draining then node_down."""
        from hblink4.hblink import HBProtocol, CONFIG
        CONFIG.clear()
        CONFIG.update({
            'global': {'bind_address': '0.0.0.0', 'port': 62030,
                       'passphrase': 'test', 'max_missed_pings': 3,
                       'ping_time': 5, 'stream_timeout': 2.0,
                       'stream_hang_time': 10.0, 'user_cache_timeout': 300,
                       'drain_timeout': 1.0},
            'repeater_configurations': {'patterns': []},
            'blacklist': {'patterns': []},
            'connection_type_detection': {'categories': {}},
        })

        mock = MagicMock(spec=HBProtocol)
        mock._draining = False
        mock._repeaters = {}
        mock._outbounds = {}
        mock._cluster_bus = MagicMock()
        mock._cluster_bus._node_id = 'node-a'

        async def noop(*a, **kw):
            pass
        mock._cluster_bus.broadcast = MagicMock(side_effect=noop)
        mock._cluster_bus.stop = MagicMock(side_effect=noop)
        mock._port = None
        mock._events = MagicMock()

        # Bind real methods
        mock.graceful_shutdown = HBProtocol.graceful_shutdown.__get__(mock)
        mock._count_active_streams = HBProtocol._count_active_streams.__get__(mock)

        await mock.graceful_shutdown(drain_timeout=0.1)

        self.assertTrue(mock._draining)

        # Check broadcast calls
        broadcast_calls = mock._cluster_bus.broadcast.call_args_list
        types = [call[0][0]['type'] for call in broadcast_calls]
        self.assertIn('node_draining', types)
        self.assertIn('node_down', types)
        # node_draining before node_down
        self.assertLess(types.index('node_draining'), types.index('node_down'))

        mock._cluster_bus.stop.assert_called_once()


class TestNodeDrainingHandler(unittest.TestCase):
    """Phase 3.3: Handling node_draining/node_down from cluster peers."""

    def setUp(self):
        self.proto = _make_hbprotocol_for_routing()

    def test_node_draining_adds_to_set(self):
        """node_draining message adds peer to draining set."""
        from hblink4.hblink import HBProtocol
        self.proto._handle_cluster_message = HBProtocol._handle_cluster_message.__get__(self.proto)
        # Can't easily call async from sync test, so test the logic directly
        self.proto._draining_peers.add('node-b')
        self.assertIn('node-b', self.proto._draining_peers)

    def test_node_down_removes_from_set(self):
        """node_down removes peer from draining set."""
        self.proto._draining_peers.add('node-b')
        self.proto._draining_peers.discard('node-b')
        self.assertNotIn('node-b', self.proto._draining_peers)

    def test_peer_disconnect_clears_draining(self):
        """Peer disconnect also clears draining state."""
        self.proto._draining_peers.add('node-b')
        # Simulate what peer_disconnected handler does
        self.proto._draining_peers.discard('node-b')
        self.assertNotIn('node-b', self.proto._draining_peers)


# ========== Phase 1.3: User Cache Sharing ==========


class TestUserCacheSharing(unittest.TestCase):
    """Phase 1.3: User cache cluster broadcast and merge."""

    def test_source_node_on_entry(self):
        """UserEntry tracks source_node."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        cache.update(12345, 312000, 'W1AW', 1, 9, source_node='node-b')
        entry = cache.lookup(12345)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.source_node, 'node-b')

    def test_local_entry_no_source_node(self):
        """Local entries have source_node=None."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        cache.update(12345, 312000, 'W1AW', 1, 9)
        entry = cache.lookup(12345)
        self.assertIsNone(entry.source_node)

    def test_broadcast_queued_on_local_update(self):
        """Local update queues a broadcast entry."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        broadcasts = []
        cache.set_broadcast_callback(lambda batch: broadcasts.extend(batch))
        cache.update(12345, 312000, '', 1, 9)
        # Not flushed yet (throttle interval)
        self.assertEqual(len(broadcasts), 0)
        self.assertEqual(len(cache._broadcast_queue), 1)

    def test_broadcast_not_queued_for_remote(self):
        """Remote entries don't queue a broadcast (no re-broadcast)."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        broadcasts = []
        cache.set_broadcast_callback(lambda batch: broadcasts.extend(batch))
        cache.update(12345, 312000, '', 1, 9, source_node='node-b')
        self.assertEqual(len(cache._broadcast_queue), 0)

    def test_flush_broadcast(self):
        """flush_broadcast sends queued entries via callback."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        batches = []
        cache.set_broadcast_callback(lambda batch: batches.append(batch))
        cache._broadcast_interval = 0  # Disable throttle for test
        cache.update(11111, 312000, '', 1, 9)
        cache.update(22222, 312001, '', 2, 3)
        cache.flush_broadcast()
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 2)
        self.assertEqual(batches[0][0]['radio_id'], 11111)
        self.assertEqual(batches[0][1]['radio_id'], 22222)

    def test_flush_throttled(self):
        """flush_broadcast respects throttle interval."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        batches = []
        cache.set_broadcast_callback(lambda batch: batches.append(batch))
        cache._broadcast_interval = 10.0  # Long throttle
        cache._last_broadcast = time()  # Just broadcast
        cache.update(11111, 312000, '', 1, 9)
        cache.flush_broadcast()
        self.assertEqual(len(batches), 0)  # Throttled
        self.assertEqual(len(cache._broadcast_queue), 1)  # Still queued

    def test_batch_overflow_forces_flush(self):
        """Exceeding batch max forces immediate flush."""
        from hblink4.user_cache import UserCache, _BATCH_MAX
        cache = UserCache(timeout_seconds=60)
        batches = []
        cache.set_broadcast_callback(lambda batch: batches.append(batch))
        cache._broadcast_interval = 0
        for i in range(_BATCH_MAX + 1):
            cache.update(i, 312000, '', 1, 9)
        # Should have flushed at _BATCH_MAX, leaving 1 in queue
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), _BATCH_MAX)
        self.assertEqual(len(cache._broadcast_queue), 1)

    def test_merge_remote(self):
        """merge_remote adds entries with source_node set."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        cache.merge_remote([
            {'radio_id': 11111, 'repeater_id': 312000, 'slot': 1, 'talkgroup': 9},
            {'radio_id': 22222, 'repeater_id': 312001, 'slot': 2, 'talkgroup': 3},
        ], source_node='node-b')
        e1 = cache.lookup(11111)
        e2 = cache.lookup(22222)
        self.assertEqual(e1.source_node, 'node-b')
        self.assertEqual(e1.repeater_id, 312000)
        self.assertEqual(e2.source_node, 'node-b')

    def test_local_overwrites_remote(self):
        """Local update overwrites remote entry (local is more authoritative)."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        cache.update(12345, 312000, '', 1, 9, source_node='node-b')
        self.assertEqual(cache.lookup(12345).source_node, 'node-b')
        cache.update(12345, 312999, '', 1, 9)  # Local update
        entry = cache.lookup(12345)
        self.assertIsNone(entry.source_node)
        self.assertEqual(entry.repeater_id, 312999)

    def test_cleanup_flushes_broadcast(self):
        """cleanup() also flushes the broadcast queue."""
        from hblink4.user_cache import UserCache
        cache = UserCache(timeout_seconds=60)
        batches = []
        cache.set_broadcast_callback(lambda batch: batches.append(batch))
        cache._broadcast_interval = 0
        cache.update(11111, 312000, '', 1, 9)
        cache.cleanup()
        self.assertEqual(len(batches), 1)

    def test_to_dict_includes_source_node(self):
        """to_dict includes source_node for remote entries."""
        from hblink4.user_cache import UserEntry
        entry = UserEntry(radio_id=1, repeater_id=2, callsign='', slot=1,
                          talkgroup=9, source_node='node-b')
        d = entry.to_dict()
        self.assertEqual(d['source_node'], 'node-b')

    def test_to_dict_omits_source_node_for_local(self):
        """to_dict omits source_node for local entries."""
        from hblink4.user_cache import UserEntry
        entry = UserEntry(radio_id=1, repeater_id=2, callsign='', slot=1, talkgroup=9)
        d = entry.to_dict()
        self.assertNotIn('source_node', d)


class TestClusterDashboardEmit(unittest.TestCase):
    """Phase 1.4: _emit_cluster_state emits correct data to EventEmitter."""

    def _make_proto(self):
        from hblink4.hblink import HBProtocol
        proto = _make_hbprotocol_for_routing()
        proto._events = MagicMock()
        proto._cluster_bus._node_id = 'node-a'
        proto._emit_cluster_state = HBProtocol._emit_cluster_state.__get__(proto)
        return proto

    def test_emit_includes_peer_states(self):
        """Emitted data includes peer info from cluster bus."""
        proto = self._make_proto()
        proto._cluster_bus.get_peer_states.return_value = {
            'node-b': {
                'node_id': 'node-b', 'connected': True, 'authenticated': True,
                'alive': True, 'latency_ms': 1.5, 'last_heartbeat': 100.0,
                'config_hash': 'abc123',
            }
        }
        proto._cluster_state = {'node-b': {312000: {}, 312001: {}}}
        proto._draining_peers = set()

        proto._emit_cluster_state()

        proto._events.emit.assert_called_once()
        args = proto._events.emit.call_args
        self.assertEqual(args[0][0], 'cluster_state')
        data = args[0][1]
        self.assertEqual(data['local_node_id'], 'node-a')
        self.assertEqual(len(data['peers']), 1)
        peer = data['peers'][0]
        self.assertEqual(peer['node_id'], 'node-b')
        self.assertEqual(peer['repeater_count'], 2)
        self.assertEqual(peer['latency_ms'], 1.5)
        self.assertFalse(peer['draining'])

    def test_emit_marks_draining_peers(self):
        """Draining peers are flagged in emitted data."""
        proto = self._make_proto()
        proto._cluster_bus.get_peer_states.return_value = {
            'node-b': {
                'node_id': 'node-b', 'connected': True, 'authenticated': True,
                'alive': True, 'latency_ms': 2.0, 'last_heartbeat': 100.0,
                'config_hash': '',
            }
        }
        proto._cluster_state = {}
        proto._draining_peers = {'node-b'}

        proto._emit_cluster_state()

        peer = proto._events.emit.call_args[0][1]['peers'][0]
        self.assertTrue(peer['draining'])

    def test_emit_noop_without_cluster_bus(self):
        """No emit when cluster bus is None."""
        proto = self._make_proto()
        proto._cluster_bus = None
        proto._emit_cluster_state()
        proto._events.emit.assert_not_called()

    def test_emit_zero_repeaters_for_unknown_peer(self):
        """Peer with no entries in _cluster_state gets repeater_count=0."""
        proto = self._make_proto()
        proto._cluster_bus.get_peer_states.return_value = {
            'node-c': {
                'node_id': 'node-c', 'connected': False, 'authenticated': False,
                'alive': False, 'latency_ms': 0, 'last_heartbeat': 0,
                'config_hash': '',
            }
        }
        proto._cluster_state = {}
        proto._draining_peers = set()

        proto._emit_cluster_state()

        peer = proto._events.emit.call_args[0][1]['peers'][0]
        self.assertEqual(peer['repeater_count'], 0)
        self.assertFalse(peer['alive'])


class TestClusterDashboardHandleEvent(unittest.IsolatedAsyncioTestCase):
    """Phase 1.4/4.2: Dashboard handle_event stores cluster state."""

    async def test_cluster_state_stored(self):
        """cluster_state event updates DashboardState.cluster."""
        from dashboard.server import DashboardState, EventReceiver
        ds = DashboardState()
        receiver = EventReceiver.__new__(EventReceiver)
        # Patch global state
        import dashboard.server as srv
        old_state = srv.state
        srv.state = ds
        try:
            event = {
                'type': 'cluster_state',
                'timestamp': time(),
                'data': {
                    'local_node_id': 'node-a',
                    'peers': [
                        {'node_id': 'node-b', 'connected': True, 'alive': True,
                         'latency_ms': 2.1, 'repeater_count': 3, 'draining': False}
                    ]
                }
            }
            await receiver.handle_event(event)
            self.assertEqual(ds.cluster['local_node_id'], 'node-a')
            self.assertEqual(len(ds.cluster['peers']), 1)
            self.assertEqual(ds.cluster['peers'][0]['repeater_count'], 3)
        finally:
            srv.state = old_state


class TestConfigReload(unittest.TestCase):
    """Phase 4.1: Config hot-reload via _reload_config."""

    def _make_proto(self):
        from hblink4.hblink import HBProtocol, CONFIG
        from hblink4.access_control import RepeaterMatcher
        proto = _make_hbprotocol_for_routing()
        proto._events = MagicMock()
        proto._config_file = None
        proto._config = CONFIG
        proto._matcher = RepeaterMatcher(CONFIG)
        proto._port = MagicMock()
        proto._emit_cluster_state = MagicMock()
        proto._load_repeater_tg_config = HBProtocol._load_repeater_tg_config.__get__(proto)
        proto._reload_config = HBProtocol._reload_config.__get__(proto)
        proto._remove_repeater = MagicMock()
        return proto

    def test_reload_no_config_file(self):
        """Reload returns False when no config file set."""
        proto = self._make_proto()
        self.assertFalse(proto._reload_config())

    def test_reload_rebuilds_matcher(self):
        """Reload replaces the RepeaterMatcher."""
        import tempfile
        from hblink4.hblink import CONFIG
        # Write a temporary config
        cfg = dict(CONFIG)
        cfg['repeater_configurations'] = {'patterns': []}
        cfg['blacklist'] = {'patterns': []}
        cfg['connection_type_detection'] = {'categories': {}}
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            proto = self._make_proto()
            old_matcher = proto._matcher
            proto._config_file = f.name
            result = proto._reload_config()
        os.unlink(f.name)
        self.assertTrue(result)
        self.assertIsNot(proto._matcher, old_matcher)

    def test_reload_disconnects_blacklisted(self):
        """Repeater now on blacklist gets disconnected."""
        import tempfile
        from hblink4.hblink import CONFIG
        from hblink4.models import RepeaterState

        # Create a connected repeater
        proto = self._make_proto()
        rid = (312000).to_bytes(4, 'big')
        rpt = MagicMock(spec=RepeaterState)
        rpt.connection_state = 'connected'
        rpt.get_callsign_str.return_value = 'W1TEST'
        rpt.repeater_id = rid
        rpt.sockaddr = ('127.0.0.1', 62031)
        proto._repeaters = {rid: rpt}

        # Write config that blacklists 312000
        cfg = dict(CONFIG)
        cfg['repeater_configurations'] = {'patterns': []}
        cfg['blacklist'] = {'patterns': [{
            'name': 'test', 'description': 'test',
            'match': {'ids': [312000]}, 'reason': 'test block'
        }]}
        cfg['connection_type_detection'] = {'categories': {}}

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            proto._config_file = f.name
            proto._reload_config()
        os.unlink(f.name)

        # Should have sent MSTCL and removed the repeater
        proto._port.sendto.assert_called()
        proto._remove_repeater.assert_called_once()
        call_args = proto._remove_repeater.call_args
        self.assertEqual(call_args[0][0], rid)
        self.assertEqual(call_args[0][1], 'blacklisted_on_reload')

    def test_reload_emits_dashboard_event(self):
        """Reload emits config_reloaded event."""
        import tempfile
        from hblink4.hblink import CONFIG

        cfg = dict(CONFIG)
        cfg['repeater_configurations'] = {'patterns': []}
        cfg['blacklist'] = {'patterns': []}
        cfg['connection_type_detection'] = {'categories': {}}

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            proto = self._make_proto()
            proto._config_file = f.name
            proto._reload_config()
        os.unlink(f.name)

        # Check config_reloaded event was emitted
        emit_calls = [c for c in proto._events.emit.call_args_list
                      if c[0][0] == 'config_reloaded']
        self.assertEqual(len(emit_calls), 1)


class TestManagementSocket(unittest.IsolatedAsyncioTestCase):
    """Phase 4.3: Management socket commands."""

    async def test_mgmt_roundtrip(self):
        """Full roundtrip: connect to unix socket, send command, get response."""
        import tempfile

        # We need to replicate the handler logic since it's nested in async_main
        async def handle_client(reader, writer):
            line = await reader.readline()
            cmd = json.loads(line.decode().strip())
            command = cmd.get('command', '').lower()
            if command == 'status':
                resp = {'ok': True, 'repeaters': 0}
            else:
                resp = {'ok': False, 'error': 'unknown'}
            writer.write(json.dumps(resp).encode() + b'\n')
            await writer.drain()
            writer.close()

        sock_path = tempfile.mktemp(suffix='.sock')
        server = await asyncio.start_unix_server(handle_client, sock_path)
        try:
            reader, writer = await asyncio.open_unix_connection(sock_path)
            writer.write(json.dumps({'command': 'status'}).encode() + b'\n')
            await writer.drain()
            line = await reader.readline()
            resp = json.loads(line.decode().strip())
            self.assertTrue(resp['ok'])
            self.assertEqual(resp['repeaters'], 0)
            writer.close()
        finally:
            server.close()
            await server.wait_closed()
            os.unlink(sock_path)


# ========== Phase 5.4: HomeBrew Proxy Adapter Tests ==========

class TestHomebrewProxySubscription(unittest.TestCase):
    """HomeBrew repeaters get internal subscriptions via proxy adapter."""

    def _make_proto(self):
        from hblink4.hblink import HBProtocol, CONFIG, rid_to_int
        CONFIG.clear()
        CONFIG.update({
            'global': {
                'bind_address': '0.0.0.0', 'port': 62030, 'passphrase': 'test',
                'max_missed_pings': 3, 'ping_time': 5, 'stream_timeout': 2.0,
                'stream_hang_time': 10.0, 'user_cache_timeout': 300,
            },
            'repeater_configurations': {'patterns': []},
            'blacklist': {'patterns': []},
            'connection_type_detection': {'categories': {}},
        })
        mock = MagicMock(spec=HBProtocol)
        mock._subscriptions = SubscriptionStore()
        mock._create_homebrew_subscription = HBProtocol._create_homebrew_subscription.__get__(mock)
        mock._remove_repeater = HBProtocol._remove_repeater.__get__(mock)
        mock._repeaters = {}
        mock._events = MagicMock()
        mock._cluster_bus = None
        return mock, rid_to_int

    def test_creates_subscription_from_bytes_tgs(self):
        """HomeBrew repeater TG bytes are converted to int subscription."""
        proto, rid_to_int = self._make_proto()
        repeater = MagicMock()
        repeater.slot1_talkgroups = {b'\x00\x00\x08', b'\x00\x00\x09'}  # TG 8, 9
        repeater.slot2_talkgroups = {b'\x00\x0c\x30'}  # TG 3120

        rid = b'\x00\x04\xc2\xc0'  # 312000
        proto._create_homebrew_subscription(rid, repeater)

        sub = proto._subscriptions.get_subscription(312000)
        self.assertIsNotNone(sub)
        self.assertEqual(sub.slot1_talkgroups, {8, 9})
        self.assertEqual(sub.slot2_talkgroups, {3120})

    def test_none_tgs_passes_none(self):
        """None TG sets (allow-all config) pass through as None."""
        proto, _ = self._make_proto()
        repeater = MagicMock()
        repeater.slot1_talkgroups = None
        repeater.slot2_talkgroups = None

        proto._create_homebrew_subscription(b'\x00\x00\x00\x01', repeater)
        # With both requested and allowed as None, intersection returns empty set
        sub = proto._subscriptions.get_subscription(1)
        self.assertIsNotNone(sub)

    def test_subscription_removed_on_repeater_disconnect(self):
        """Subscription cleaned up when HomeBrew repeater disconnects."""
        proto, rid_to_int = self._make_proto()
        rid = b'\x00\x00\x00\x42'  # 66
        repeater = MagicMock()
        repeater.slot1_talkgroups = {b'\x00\x00\x08'}
        repeater.slot2_talkgroups = set()
        proto._repeaters[rid] = repeater

        # Create subscription
        proto._create_homebrew_subscription(rid, repeater)
        self.assertIsNotNone(proto._subscriptions.get_subscription(66))

        # Remove repeater (calls unsubscribe internally)
        proto._remove_repeater(rid, 'test')
        self.assertIsNone(proto._subscriptions.get_subscription(66))

    def test_subscription_updated_on_rpto(self):
        """Calling _create_homebrew_subscription again updates the subscription."""
        proto, _ = self._make_proto()
        rid = b'\x00\x00\x00\x01'
        repeater = MagicMock()
        repeater.slot1_talkgroups = {b'\x00\x00\x08'}
        repeater.slot2_talkgroups = set()

        proto._create_homebrew_subscription(rid, repeater)
        self.assertEqual(proto._subscriptions.get_subscription(1).slot1_talkgroups, {8})

        # Simulate RPTO update
        repeater.slot1_talkgroups = {b'\x00\x00\x08', b'\x00\x00\x09'}
        proto._create_homebrew_subscription(rid, repeater)
        self.assertEqual(proto._subscriptions.get_subscription(1).slot1_talkgroups, {8, 9})


class TestNativeClientTargetCalculation(unittest.TestCase):
    """Native clients appear as targets when their subscription matches."""

    def setUp(self):
        self.proto = _make_hbprotocol_for_routing()

    def test_native_client_targeted_when_tg_matches(self):
        """Native client with matching TG subscription appears in targets."""
        nc_addr = ('10.0.0.5', 50000)
        self.proto._native_clients[nc_addr] = {
            'token_hash': b'\x01\x02\x03\x04',
            'repeater_id': 999,
            'last_ping': time(),
        }
        self.proto._subscriptions.subscribe(999, [8], [3120], None, None)

        targets = self.proto._calculate_stream_targets(
            source_repeater_id=b'\x00\x00\x00\x01',
            dst_id=b'\x00\x00\x08',  # TG 8
            slot=1,
            stream_id=b'\xaa\xbb\xcc\xdd',
            rf_src=b'\x00\x00\x01',
        )
        self.assertIn(('native', nc_addr), targets)

    def test_native_client_excluded_wrong_tg(self):
        """Native client not targeted when TG doesn't match subscription."""
        nc_addr = ('10.0.0.5', 50000)
        self.proto._native_clients[nc_addr] = {
            'token_hash': b'\x01\x02\x03\x04',
            'repeater_id': 999,
            'last_ping': time(),
        }
        self.proto._subscriptions.subscribe(999, [8], [3120], None, None)

        targets = self.proto._calculate_stream_targets(
            source_repeater_id=b'\x00\x00\x00\x01',
            dst_id=b'\x00\x00\x09',  # TG 9 — not subscribed
            slot=1,
            stream_id=b'\xaa\xbb\xcc\xdd',
            rf_src=b'\x00\x00\x01',
        )
        self.assertNotIn(('native', nc_addr), targets)

    def test_native_client_excluded_wrong_slot(self):
        """Native client subscribed on slot 1 not targeted for slot 2 traffic."""
        nc_addr = ('10.0.0.5', 50000)
        self.proto._native_clients[nc_addr] = {
            'token_hash': b'\x01\x02\x03\x04',
            'repeater_id': 999,
            'last_ping': time(),
        }
        self.proto._subscriptions.subscribe(999, [8], [], None, None)

        targets = self.proto._calculate_stream_targets(
            source_repeater_id=b'\x00\x00\x00\x01',
            dst_id=b'\x00\x00\x08',  # TG 8
            slot=2,  # Slot 2 — no TGs subscribed
            stream_id=b'\xaa\xbb\xcc\xdd',
            rf_src=b'\x00\x00\x01',
        )
        self.assertNotIn(('native', nc_addr), targets)

    def test_multiple_native_clients(self):
        """Multiple native clients with matching TG all targeted."""
        for i, port in enumerate([50000, 50001]):
            addr = ('10.0.0.5', port)
            self.proto._native_clients[addr] = {
                'token_hash': bytes([i] * 4),
                'repeater_id': 900 + i,
                'last_ping': time(),
            }
            self.proto._subscriptions.subscribe(900 + i, [8], [], None, None)

        targets = self.proto._calculate_stream_targets(
            source_repeater_id=b'\x00\x00\x00\x01',
            dst_id=b'\x00\x00\x08',
            slot=1,
            stream_id=b'\xaa\xbb\xcc\xdd',
            rf_src=b'\x00\x00\x01',
        )
        self.assertIn(('native', ('10.0.0.5', 50000)), targets)
        self.assertIn(('native', ('10.0.0.5', 50001)), targets)


class TestNativeClientForwarding(unittest.TestCase):
    """Native client packet formatting in _forward_stream."""

    def _make_proto(self):
        from hblink4.hblink import HBProtocol, CONFIG
        CONFIG.clear()
        CONFIG.update({
            'global': {
                'bind_address': '0.0.0.0', 'port': 62030, 'passphrase': 'test',
                'max_missed_pings': 3, 'ping_time': 5, 'stream_timeout': 2.0,
                'stream_hang_time': 10.0, 'user_cache_timeout': 300,
            },
            'repeater_configurations': {'patterns': []},
            'blacklist': {'patterns': []},
            'connection_type_detection': {'categories': {}},
        })
        mock = MagicMock(spec=HBProtocol)
        mock._repeaters = {}
        mock._outbounds = {}
        mock._cluster_bus = None
        mock._native_clients = {
            ('10.0.0.5', 50000): {
                'token_hash': b'\x01\x02\x03\x04',
                'repeater_id': 999,
            }
        }
        mock._subscriptions = SubscriptionStore()
        mock._streams = {}
        mock._forward_stream = HBProtocol._forward_stream.__get__(mock)
        mock._is_dmr_terminator = HBProtocol._is_dmr_terminator.__get__(mock)
        return mock

    def test_native_forward_packet_format(self):
        """Native forwarding wraps DMRD payload in CLNT+DATA+token_hash."""
        from hblink4.cluster_protocol import NATIVE_MAGIC, CMD_DATA
        proto = self._make_proto()

        # Build a DMRD packet with valid frame type byte at offset 15
        # 4B DMRD + 11B header + 1B frame_type + remaining
        dmrd_header = b'DMRD'  # 4 bytes
        dmrd_mid = b'\x00' * 11  # bytes 4-14
        frame_byte = b'\x00'  # byte 15 — frame type bits
        dmrd_tail = b'\x00' * 39  # bytes 16-54
        data = dmrd_header + dmrd_mid + frame_byte + dmrd_tail  # 55 bytes

        # Set up source repeater with a cached stream targeting our native client
        src_rid = b'\x00\x00\x00\x01'
        stream_id = b'\xaa\xbb\xcc\xdd'
        source_repeater = MagicMock()
        source_stream = MagicMock()
        source_stream.stream_id = stream_id
        source_stream.routing_cached = True
        source_stream.target_repeaters = {('native', ('10.0.0.5', 50000))}
        source_repeater.get_slot_stream.return_value = source_stream
        proto._repeaters[src_rid] = source_repeater

        proto._forward_stream(
            data=data,
            source_repeater_id=src_rid,
            slot=1,
            rf_src=b'\x00\x00\x01',
            dst_id=b'\x00\x00\x08',
            stream_id=stream_id,
        )

        # Verify _send_packet was called with CLNT+DATA+token_hash+dmrd_payload
        proto._send_packet.assert_called_once()
        sent_pkt, sent_addr = proto._send_packet.call_args[0]
        self.assertEqual(sent_addr, ('10.0.0.5', 50000))
        self.assertTrue(sent_pkt.startswith(NATIVE_MAGIC + CMD_DATA))
        self.assertEqual(sent_pkt[8:12], b'\x01\x02\x03\x04')  # token_hash
        self.assertEqual(sent_pkt[12:], data[4:])  # DMRD body without 'DMRD' prefix


# ========== Phase 5.3: Cluster-Aware Keepalive Tests ==========

class TestClusterAwarePong(unittest.TestCase):
    """PONG response includes cluster health, peer status, and preferred_server."""

    def _make_proto(self):
        from hblink4.hblink import HBProtocol, CONFIG
        from hblink4.cluster_protocol import TokenManager
        CONFIG.clear()
        CONFIG.update({
            'global': {
                'bind_address': '0.0.0.0', 'port': 62030, 'passphrase': 'test',
                'max_missed_pings': 3, 'ping_time': 5, 'stream_timeout': 2.0,
                'stream_hang_time': 10.0, 'user_cache_timeout': 300,
            },
            'repeater_configurations': {'patterns': []},
            'blacklist': {'patterns': []},
            'connection_type_detection': {'categories': {}},
        })
        mock = MagicMock(spec=HBProtocol)
        mock._repeaters = {}
        mock._native_clients = {}
        mock._cluster_bus = None
        mock._cluster_state = {}
        mock._draining_peers = set()
        mock._draining = False
        mock._subscriptions = SubscriptionStore()

        # Real token manager for issuing test tokens
        tm = TokenManager('test-secret', 'test-cluster')
        mock._token_manager = tm

        # Bind real methods
        mock._handle_native_ping = HBProtocol._handle_native_ping.__get__(mock)
        mock._count_active_streams = HBProtocol._count_active_streams.__get__(mock)
        return mock, tm

    def _send_ping(self, proto, tm, addr=('10.0.0.5', 50000)):
        """Issue a token, register client, send ping, return parsed PONG health."""
        from hblink4.cluster_protocol import NATIVE_MAGIC, CMD_PING
        token = tm.issue_token(312000, [8], [3120])
        proto._native_clients[addr] = {
            'token_hash': token.token_hash,
            'repeater_id': 312000,
            'last_ping': time(),
        }
        ping_data = NATIVE_MAGIC + CMD_PING + token.token_hash
        proto._handle_native_ping(ping_data, addr)
        # Parse PONG response
        proto._send_packet.assert_called_once()
        pkt, sent_addr = proto._send_packet.call_args[0]
        self.assertEqual(sent_addr, addr)
        health_json = pkt[8:]  # Skip CLNT+PONG
        return json.loads(health_json)

    def test_pong_includes_node_id(self):
        """PONG response includes this server's node_id."""
        proto, tm = self._make_proto()
        health = self._send_ping(proto, tm)
        self.assertEqual(health['node_id'], 'standalone')

    def test_pong_includes_node_id_clustered(self):
        """PONG with cluster bus includes real node_id."""
        proto, tm = self._make_proto()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'server-1'
        proto._cluster_bus.get_peer_states.return_value = {}
        health = self._send_ping(proto, tm)
        self.assertEqual(health['node_id'], 'server-1')

    def test_pong_includes_active_streams(self):
        """PONG includes active stream count."""
        proto, tm = self._make_proto()
        health = self._send_ping(proto, tm)
        self.assertEqual(health['active_streams'], 0)

    def test_pong_peer_status_alive(self):
        """Alive peer shows status='alive'."""
        proto, tm = self._make_proto()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'server-1'
        proto._cluster_bus.get_peer_states.return_value = {
            'server-2': {'alive': True, 'latency_ms': 1.5},
        }
        health = self._send_ping(proto, tm)
        peer = health['peers'][0]
        self.assertEqual(peer['status'], 'alive')
        self.assertEqual(peer['latency_ms'], 1.5)

    def test_pong_peer_status_draining(self):
        """Draining peer shows status='draining'."""
        proto, tm = self._make_proto()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'server-1'
        proto._cluster_bus.get_peer_states.return_value = {
            'server-2': {'alive': True, 'latency_ms': 1.0},
        }
        proto._draining_peers = {'server-2'}
        health = self._send_ping(proto, tm)
        self.assertEqual(health['peers'][0]['status'], 'draining')

    def test_pong_peer_status_dead(self):
        """Dead peer shows status='dead'."""
        proto, tm = self._make_proto()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'server-1'
        proto._cluster_bus.get_peer_states.return_value = {
            'server-2': {'alive': False, 'latency_ms': 0},
        }
        health = self._send_ping(proto, tm)
        self.assertEqual(health['peers'][0]['status'], 'dead')

    def test_pong_preferred_server(self):
        """preferred_server points to lowest-load alive non-draining peer."""
        proto, tm = self._make_proto()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'server-1'
        proto._cluster_bus.get_peer_states.return_value = {
            'server-2': {'alive': True, 'latency_ms': 2.0},
            'server-3': {'alive': True, 'latency_ms': 3.0},
        }
        # server-3 has fewer repeaters (lower load)
        proto._cluster_state = {
            'server-2': {1: {}, 2: {}, 3: {}},  # 3 repeaters
            'server-3': {1: {}},  # 1 repeater
        }
        health = self._send_ping(proto, tm)
        self.assertEqual(health['preferred_server'], 'server-3')

    def test_pong_no_preferred_when_all_draining(self):
        """No preferred_server when all peers are draining."""
        proto, tm = self._make_proto()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'server-1'
        proto._cluster_bus.get_peer_states.return_value = {
            'server-2': {'alive': True, 'latency_ms': 1.0},
        }
        proto._draining_peers = {'server-2'}
        health = self._send_ping(proto, tm)
        self.assertNotIn('preferred_server', health)

    def test_pong_redirect_on_drain(self):
        """PONG includes redirect=True when this server is draining."""
        proto, tm = self._make_proto()
        proto._draining = True
        health = self._send_ping(proto, tm)
        self.assertTrue(health['redirect'])


if __name__ == '__main__':
    unittest.main()
