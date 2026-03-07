"""Tests for the cluster bus (Phase 1.1 + 1.2)."""

import asyncio
import json
import struct
import unittest

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hblink4.cluster import (
    ClusterBus, PeerState, FRAME_HEADER,
    MSG_TYPE_JSON, MSG_TYPE_STREAM_DATA, AUTH_CHALLENGE, AUTH_RESPONSE, AUTH_OK
)


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


if __name__ == '__main__':
    unittest.main()
