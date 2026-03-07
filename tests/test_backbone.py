"""Tests for backbone bus (Phase 6.1) and TG routing table (Phase 6.2)."""

import asyncio
import json
import unittest
from unittest.mock import MagicMock, AsyncMock
from time import time

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from hblink4.backbone import (
    BackboneBus, BackbonePeerState, TalkgroupRoutingTable,
    RegionalTGSummary, BB_AUTH_CHALLENGE, BB_AUTH_RESPONSE,
    BB_AUTH_OK, BB_AUTH_FAIL, FRAME_HEADER, MSG_TYPE_JSON,
)


# ========== TG Routing Table Tests ==========

class TestTalkgroupRoutingTable(unittest.TestCase):
    """TG routing table for cross-region stream targeting."""

    def test_update_and_get_region(self):
        """Region TG summary stored and retrievable."""
        table = TalkgroupRoutingTable()
        table.update_region('us-west', {8, 9}, {3120})
        r = table.get_region('us-west')
        self.assertIsNotNone(r)
        self.assertEqual(r.slot1_talkgroups, {8, 9})
        self.assertEqual(r.slot2_talkgroups, {3120})

    def test_get_target_regions_match(self):
        """Regions with matching TG are returned."""
        table = TalkgroupRoutingTable()
        table.update_region('us-west', {8, 9}, {3120})
        table.update_region('eu', {8, 10}, {3121})
        table.update_region('asia', {11}, {3122})

        targets = table.get_target_regions(1, 8)
        self.assertIn('us-west', targets)
        self.assertIn('eu', targets)
        self.assertNotIn('asia', targets)

    def test_get_target_regions_no_match(self):
        """No regions returned when TG doesn't match."""
        table = TalkgroupRoutingTable()
        table.update_region('us-west', {8}, {3120})
        targets = table.get_target_regions(1, 99)
        self.assertEqual(targets, [])

    def test_get_target_regions_exclude_self(self):
        """Own region excluded from targets."""
        table = TalkgroupRoutingTable()
        table.update_region('us-east', {8}, {3120})
        table.update_region('us-west', {8}, {3120})
        targets = table.get_target_regions(1, 8, exclude_region='us-east')
        self.assertNotIn('us-east', targets)
        self.assertIn('us-west', targets)

    def test_get_target_regions_slot2(self):
        """Slot 2 TGs checked correctly."""
        table = TalkgroupRoutingTable()
        table.update_region('eu', {8}, {3120, 3121})
        targets = table.get_target_regions(2, 3120)
        self.assertIn('eu', targets)
        targets = table.get_target_regions(1, 3120)
        self.assertNotIn('eu', targets)

    def test_remove_region(self):
        """Removed region no longer appears in targets."""
        table = TalkgroupRoutingTable()
        table.update_region('us-west', {8}, {3120})
        table.remove_region('us-west')
        self.assertIsNone(table.get_region('us-west'))
        self.assertEqual(table.get_target_regions(1, 8), [])

    def test_region_count(self):
        table = TalkgroupRoutingTable()
        self.assertEqual(table.region_count, 0)
        table.update_region('r1', set(), set())
        table.update_region('r2', set(), set())
        self.assertEqual(table.region_count, 2)

    def test_get_all_regions(self):
        table = TalkgroupRoutingTable()
        table.update_region('us-west', {8, 9}, {3120}, 'gw-west')
        result = table.get_all_regions()
        self.assertIn('us-west', result)
        self.assertEqual(result['us-west']['gateway_node_id'], 'gw-west')
        self.assertEqual(result['us-west']['slot1_talkgroups'], [8, 9])


# ========== Regional TG Summary Computation ==========

class TestRegionalTGSummary(unittest.TestCase):
    """Gateway computes TG union for its region."""

    def _make_bus(self):
        config = {
            'port': 0, 'backbone_secret': 'test',
            'gateways': [],
        }
        return BackboneBus('gw-east', 'us-east', config, AsyncMock())

    def test_local_repeaters_bytes_tgs(self):
        """Local repeaters with bytes TG sets converted to ints."""
        bus = self._make_bus()
        rpt = MagicMock()
        rpt.slot1_talkgroups = {b'\x00\x00\x08', b'\x00\x00\x09'}
        rpt.slot2_talkgroups = {b'\x00\x0c\x30'}
        s1, s2 = bus.compute_regional_tg_summary({}, {b'\x00\x00\x00\x01': rpt})
        self.assertEqual(s1, {8, 9})
        self.assertEqual(s2, {3120})

    def test_cluster_state_int_tgs(self):
        """Cluster state TGs (ints) included in summary."""
        bus = self._make_bus()
        cluster_state = {
            'node-2': {
                312000: {
                    'slot1_talkgroups': [1, 2, 8],
                    'slot2_talkgroups': [3120],
                }
            }
        }
        s1, s2 = bus.compute_regional_tg_summary(cluster_state, {})
        self.assertEqual(s1, {1, 2, 8})
        self.assertEqual(s2, {3120})

    def test_none_tgs_not_propagated(self):
        """None (allow-all) TGs never propagated to backbone."""
        bus = self._make_bus()
        rpt = MagicMock()
        rpt.slot1_talkgroups = None  # Allow all
        rpt.slot2_talkgroups = {b'\x00\x00\x08'}
        s1, s2 = bus.compute_regional_tg_summary({}, {b'\x00\x00\x00\x01': rpt})
        self.assertEqual(s1, set())  # None not propagated
        self.assertEqual(s2, {8})

    def test_cluster_state_none_tgs_skipped(self):
        """None TGs in cluster state skipped."""
        bus = self._make_bus()
        cluster_state = {
            'node-2': {
                1: {'slot1_talkgroups': None, 'slot2_talkgroups': [8]},
            }
        }
        s1, s2 = bus.compute_regional_tg_summary(cluster_state, {})
        self.assertEqual(s1, set())
        self.assertEqual(s2, {8})

    def test_union_of_local_and_cluster(self):
        """Summary is union of local + cluster TGs."""
        bus = self._make_bus()
        rpt = MagicMock()
        rpt.slot1_talkgroups = {b'\x00\x00\x08'}
        rpt.slot2_talkgroups = set()
        cluster_state = {
            'node-2': {1: {'slot1_talkgroups': [9], 'slot2_talkgroups': [3120]}},
        }
        s1, s2 = bus.compute_regional_tg_summary(
            cluster_state, {b'\x00\x00\x00\x01': rpt}
        )
        self.assertEqual(s1, {8, 9})
        self.assertEqual(s2, {3120})


# ========== BackboneBus Unit Tests ==========

class TestBackboneBusInit(unittest.TestCase):
    """BackboneBus initialization and config."""

    def test_peer_setup(self):
        """Gateways from config are set up as peers."""
        config = {
            'port': 62033, 'backbone_secret': 'test',
            'gateways': [
                {'node_id': 'gw-west', 'region_id': 'us-west',
                 'address': '10.0.2.1', 'port': 62033},
                {'node_id': 'gw-eu', 'region_id': 'eu',
                 'address': '10.0.3.1', 'port': 62033},
            ],
        }
        bus = BackboneBus('gw-east', 'us-east', config, AsyncMock())
        self.assertEqual(len(bus._peers), 2)
        self.assertEqual(bus._peers['gw-west'].region_id, 'us-west')
        self.assertEqual(bus._peers['gw-eu'].region_id, 'eu')

    def test_allowed_regions(self):
        """Only configured regions are allowed."""
        config = {
            'port': 62033, 'backbone_secret': 'test',
            'gateways': [
                {'node_id': 'gw-west', 'region_id': 'us-west',
                 'address': '10.0.2.1', 'port': 62033},
            ],
        }
        bus = BackboneBus('gw-east', 'us-east', config, AsyncMock())
        self.assertIn('us-west', bus._allowed_regions)
        self.assertNotIn('asia', bus._allowed_regions)

    def test_connected_regions(self):
        """connected_regions returns regions with active gateways."""
        config = {
            'port': 0, 'backbone_secret': 'test',
            'gateways': [
                {'node_id': 'gw-west', 'region_id': 'us-west',
                 'address': '10.0.2.1', 'port': 62033},
                {'node_id': 'gw-eu', 'region_id': 'eu',
                 'address': '10.0.3.1', 'port': 62033},
            ],
        }
        bus = BackboneBus('gw-east', 'us-east', config, AsyncMock())
        self.assertEqual(bus.connected_regions, set())

        bus._peers['gw-west'].connected = True
        bus._peers['gw-west'].authenticated = True
        self.assertEqual(bus.connected_regions, {'us-west'})

    def test_get_region_for_peer(self):
        config = {
            'port': 0, 'backbone_secret': 'test',
            'gateways': [
                {'node_id': 'gw-west', 'region_id': 'us-west',
                 'address': '10.0.2.1', 'port': 62033},
            ],
        }
        bus = BackboneBus('gw-east', 'us-east', config, AsyncMock())
        self.assertEqual(bus.get_region_for_peer('gw-west'), 'us-west')
        self.assertIsNone(bus.get_region_for_peer('unknown'))


# ========== TG Summary Change Detection ==========

class TestTGSummaryAdvertise(unittest.IsolatedAsyncioTestCase):
    """TG summary only broadcast when changed."""

    async def test_no_broadcast_when_unchanged(self):
        config = {
            'port': 0, 'backbone_secret': 'test', 'gateways': [],
        }
        bus = BackboneBus('gw-east', 'us-east', config, AsyncMock())

        # First call should broadcast
        bus.broadcast = AsyncMock()
        await bus.advertise_tg_summary({8, 9}, {3120})
        bus.broadcast.assert_called_once()

        # Same summary — no broadcast
        bus.broadcast.reset_mock()
        await bus.advertise_tg_summary({8, 9}, {3120})
        bus.broadcast.assert_not_called()

    async def test_broadcast_on_change(self):
        config = {
            'port': 0, 'backbone_secret': 'test', 'gateways': [],
        }
        bus = BackboneBus('gw-east', 'us-east', config, AsyncMock())
        bus.broadcast = AsyncMock()

        await bus.advertise_tg_summary({8}, {3120})
        bus.broadcast.assert_called_once()

        bus.broadcast.reset_mock()
        await bus.advertise_tg_summary({8, 9}, {3120})  # Added TG 9
        bus.broadcast.assert_called_once()


# ========== Backbone Integration (TCP) ==========

class TestBackboneTCP(unittest.IsolatedAsyncioTestCase):
    """Integration: backbone bus over real TCP connections."""

    async def test_backbone_connect_and_tg_exchange(self):
        """Two backbone buses connect and exchange TG summaries."""
        messages_b = []

        async def on_msg_a(msg):
            pass

        async def on_msg_b(msg):
            messages_b.append(msg)

        config_a = {
            'bind': '127.0.0.1', 'port': 0, 'backbone_secret': 'test-bb',
            'heartbeat_interval': 60, 'dead_threshold': 120,
            'reconnect_interval': 60, 'gateways': [],
        }
        config_b = {
            'bind': '127.0.0.1', 'port': 0, 'backbone_secret': 'test-bb',
            'heartbeat_interval': 60, 'dead_threshold': 120,
            'reconnect_interval': 60, 'gateways': [],
        }

        bus_a = BackboneBus('gw-east', 'us-east', config_a, on_msg_a)
        bus_b = BackboneBus('gw-west', 'us-west', config_b, on_msg_b)

        # Start servers only (port 0 = OS picks)
        bus_a._running = True
        bus_b._running = True
        bus_a._server = await asyncio.start_server(
            bus_a._handle_inbound, '127.0.0.1', 0)
        bus_b._server = await asyncio.start_server(
            bus_b._handle_inbound, '127.0.0.1', 0)

        port_b = bus_b._server.sockets[0].getsockname()[1]

        # Add peer on A side only (A connects to B)
        bus_a._peers['gw-west'] = BackbonePeerState(
            node_id='gw-west', region_id='us-west',
            address='127.0.0.1', port=port_b)
        bus_a._allowed_regions.add('us-west')

        bus_b._peers['gw-east'] = BackbonePeerState(
            node_id='gw-east', region_id='us-east',
            address='127.0.0.1', port=0)
        bus_b._allowed_regions.add('us-east')

        try:
            # Connect A -> B
            await bus_a._connect_to_peer(bus_a._peers['gw-west'])
            await asyncio.sleep(0.1)

            self.assertTrue(bus_a._peers['gw-west'].connected)

            # Send TG summary from A to B
            await bus_a.broadcast({
                'type': 'tg_summary',
                'region_id': 'us-east',
                'slot1_talkgroups': [8, 9],
                'slot2_talkgroups': [3120],
            })
            await asyncio.sleep(0.1)

            # B should have received the TG summary
            tg_msgs = [m for m in messages_b if m.get('type') == 'tg_summary']
            self.assertEqual(len(tg_msgs), 1)
            self.assertEqual(tg_msgs[0]['region_id'], 'us-east')

            # B's TG table should be updated
            r = bus_b.tg_table.get_region('us-east')
            self.assertIsNotNone(r)
            self.assertEqual(r.slot1_talkgroups, {8, 9})

        finally:
            # Clean shutdown: close peers on both sides to unblock read loops
            bus_a._running = False
            bus_b._running = False

            # Close all peer connections (both sides)
            all_peers = list(bus_a._peers.values()) + list(bus_b._peers.values())
            for peer in all_peers:
                if peer._read_task and not peer._read_task.done():
                    peer._read_task.cancel()
                if peer.writer:
                    peer.writer.close()

            # Wait briefly for read tasks to notice
            await asyncio.sleep(0.05)

            bus_a._server.close()
            bus_b._server.close()
            await bus_a._server.wait_closed()
            await bus_b._server.wait_closed()


if __name__ == '__main__':
    unittest.main()
