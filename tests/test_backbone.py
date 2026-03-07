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
    UserLookupService, CrossRegionUserEntry,
)
from hblink4.user_cache import UserCache


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


# ========== User Lookup Service Tests (Phase 6.4) ==========

def _make_lookup_service():
    """Create UserLookupService with mocked BackboneBus."""
    bus = MagicMock()
    bus.broadcast = AsyncMock()
    bus.send_to_region = AsyncMock()
    svc = UserLookupService(bus, 'us-east')
    return svc, bus


class TestUserLookupServiceCache(unittest.TestCase):
    """Phase 6.4: Cross-region user lookup cache behavior."""

    def test_positive_cache_hit(self):
        """Positive cache hit returns entry within TTL."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 3120001, 'found': True,
            'responder_region': 'us-west', 'repeater_id': 312000,
            'source_node': 'node-w1', 'query_id': 1,
        })
        entry = svc.lookup(3120001)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.region_id, 'us-west')
        self.assertEqual(entry.repeater_id, 312000)
        self.assertFalse(entry.negative)

    def test_negative_cache_hit(self):
        """Negative cache prevents lookup storm."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 9999999, 'found': False,
            'responder_region': 'us-west', 'query_id': 1,
        })
        entry = svc.lookup(9999999)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.negative)

    def test_positive_ttl_expiry(self):
        """Positive cache expires after POSITIVE_TTL."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 3120001, 'found': True,
            'responder_region': 'us-west', 'repeater_id': 312000,
            'source_node': '', 'query_id': 1,
        })
        # Backdate past TTL
        svc._cache[3120001].cached_at -= UserLookupService.POSITIVE_TTL + 1
        self.assertIsNone(svc.lookup(3120001))

    def test_negative_ttl_expiry(self):
        """Negative cache expires after NEGATIVE_TTL."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 9999999, 'found': False,
            'responder_region': 'us-west', 'query_id': 1,
        })
        svc._cache[9999999].cached_at -= UserLookupService.NEGATIVE_TTL + 1
        self.assertIsNone(svc.lookup(9999999))

    def test_cache_miss(self):
        """Lookup for unknown radio_id returns None."""
        svc, _ = _make_lookup_service()
        self.assertIsNone(svc.lookup(1111111))

    def test_invalidate(self):
        """Invalidation removes entry for re-query."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 3120001, 'found': True,
            'responder_region': 'us-west', 'repeater_id': 312000,
            'source_node': '', 'query_id': 1,
        })
        svc.invalidate(3120001)
        self.assertIsNone(svc.lookup(3120001))

    def test_positive_overrides_negative(self):
        """Positive response overwrites negative cache."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 3120001, 'found': False,
            'responder_region': 'eu', 'query_id': 1,
        })
        self.assertTrue(svc.lookup(3120001).negative)
        svc.cache_result({
            'radio_id': 3120001, 'found': True,
            'responder_region': 'us-west', 'repeater_id': 312000,
            'source_node': '', 'query_id': 2,
        })
        entry = svc.lookup(3120001)
        self.assertFalse(entry.negative)
        self.assertEqual(entry.region_id, 'us-west')

    def test_negative_does_not_override_positive(self):
        """Negative response does not overwrite existing positive cache."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 3120001, 'found': True,
            'responder_region': 'us-west', 'repeater_id': 312000,
            'source_node': '', 'query_id': 1,
        })
        svc.cache_result({
            'radio_id': 3120001, 'found': False,
            'responder_region': 'eu', 'query_id': 2,
        })
        entry = svc.lookup(3120001)
        self.assertFalse(entry.negative)
        self.assertEqual(entry.region_id, 'us-west')

    def test_cleanup_removes_expired(self):
        """Cleanup removes expired entries and stale pending."""
        svc, _ = _make_lookup_service()
        svc.cache_result({
            'radio_id': 111, 'found': True,
            'responder_region': 'eu', 'repeater_id': 1,
            'source_node': '', 'query_id': 1,
        })
        svc._cache[111].cached_at -= UserLookupService.POSITIVE_TTL + 1
        svc._pending[99] = time() - 11  # Stale pending
        removed = svc.cleanup()
        self.assertEqual(removed, 1)
        self.assertNotIn(111, svc._cache)
        self.assertNotIn(99, svc._pending)


class TestUserLookupQueryRespond(unittest.IsolatedAsyncioTestCase):
    """Phase 6.4: Async query/respond for cross-region lookup."""

    async def test_query_broadcasts(self):
        """Query sends user_lookup_query to all backbone peers."""
        svc, bus = _make_lookup_service()
        qid = await svc.query(3120001)
        bus.broadcast.assert_called_once()
        msg = bus.broadcast.call_args[0][0]
        self.assertEqual(msg['type'], 'user_lookup_query')
        self.assertEqual(msg['radio_id'], 3120001)
        self.assertEqual(msg['origin_region'], 'us-east')
        self.assertEqual(msg['query_id'], qid)
        self.assertIn(qid, svc._pending)

    async def test_respond_found(self):
        """Respond sends positive result when user in local cache."""
        svc, bus = _make_lookup_service()
        cache = UserCache(timeout_seconds=600)
        cache.update(3120001, 312000, 'N0CALL', 1, 1)
        await svc.respond({
            'radio_id': 3120001, 'query_id': 1, 'origin_region': 'eu',
        }, cache)
        bus.send_to_region.assert_called_once()
        region, resp = bus.send_to_region.call_args[0]
        self.assertEqual(region, 'eu')
        self.assertTrue(resp['found'])
        self.assertEqual(resp['repeater_id'], 312000)

    async def test_respond_not_found(self):
        """Respond sends negative result when user not in cache."""
        svc, bus = _make_lookup_service()
        cache = UserCache(timeout_seconds=600)
        await svc.respond({
            'radio_id': 9999999, 'query_id': 1, 'origin_region': 'eu',
        }, cache)
        bus.send_to_region.assert_called_once()
        _, resp = bus.send_to_region.call_args[0]
        self.assertFalse(resp['found'])

    async def test_query_counter_increments(self):
        """Each query gets a unique query_id."""
        svc, _ = _make_lookup_service()
        qid1 = await svc.query(111)
        qid2 = await svc.query(222)
        self.assertNotEqual(qid1, qid2)
        self.assertEqual(qid2, qid1 + 1)


# ========== Gateway Failover Tests (Phase 6.5) ==========

def _make_dual_gw_bus():
    """Create a BackboneBus with dual gateways for us-west region."""
    config = {
        'port': 0, 'backbone_secret': 'test',
        'gateways': [
            {'node_id': 'gw-west-1', 'region_id': 'us-west',
             'address': '10.0.2.1', 'port': 62033, 'priority': 0},
            {'node_id': 'gw-west-2', 'region_id': 'us-west',
             'address': '10.0.2.2', 'port': 62033, 'priority': 1},
            {'node_id': 'gw-eu', 'region_id': 'eu',
             'address': '10.0.3.1', 'port': 62033},
        ],
    }
    return BackboneBus('gw-east', 'us-east', config, AsyncMock())


class TestGatewayFailover(unittest.IsolatedAsyncioTestCase):
    """Dual gateway failover (Phase 6.5)."""

    def test_priority_loaded_from_config(self):
        """Priority field loaded from gateway config."""
        bus = _make_dual_gw_bus()
        self.assertEqual(bus._peers['gw-west-1'].priority, 0)
        self.assertEqual(bus._peers['gw-west-2'].priority, 1)
        self.assertEqual(bus._peers['gw-eu'].priority, 0)  # default

    def test_get_gateways_sorted_by_priority(self):
        """Gateways returned sorted by priority (lowest first)."""
        bus = _make_dual_gw_bus()
        gws = bus._get_gateways_for_region('us-west')
        self.assertEqual(len(gws), 2)
        self.assertEqual(gws[0].node_id, 'gw-west-1')
        self.assertEqual(gws[1].node_id, 'gw-west-2')

    def test_get_gateways_empty_for_unknown_region(self):
        """No gateways returned for unknown region."""
        bus = _make_dual_gw_bus()
        self.assertEqual(bus._get_gateways_for_region('asia'), [])

    async def test_send_to_region_prefers_primary(self):
        """Primary gateway gets traffic when both are alive."""
        bus = _make_dual_gw_bus()
        bus._send_json = AsyncMock()
        # Both alive
        for nid in ('gw-west-1', 'gw-west-2'):
            bus._peers[nid].connected = True
            bus._peers[nid].authenticated = True

        await bus.send_to_region('us-west', {'type': 'test'})
        bus._send_json.assert_called_once()
        self.assertEqual(bus._send_json.call_args[0][0].node_id, 'gw-west-1')

    async def test_send_to_region_failover_to_secondary(self):
        """Secondary gets traffic when primary is disconnected."""
        bus = _make_dual_gw_bus()
        bus._send_json = AsyncMock()
        # Only secondary alive
        bus._peers['gw-west-2'].connected = True
        bus._peers['gw-west-2'].authenticated = True

        await bus.send_to_region('us-west', {'type': 'test'})
        bus._send_json.assert_called_once()
        self.assertEqual(bus._send_json.call_args[0][0].node_id, 'gw-west-2')

    async def test_binary_send_failover(self):
        """Binary stream data fails over to secondary."""
        bus = _make_dual_gw_bus()
        bus._send_raw = AsyncMock()
        # Only secondary alive
        bus._peers['gw-west-2'].connected = True
        bus._peers['gw-west-2'].authenticated = True

        await bus.send_binary_to_region('us-west', 0x01, b'\x00\x01')
        bus._send_raw.assert_called_once()
        self.assertEqual(bus._send_raw.call_args[0][0].node_id, 'gw-west-2')

    async def test_disconnect_keeps_region_if_secondary_alive(self):
        """TG table not removed when secondary still connected."""
        bus = _make_dual_gw_bus()
        bus.tg_table.update_region('us-west', {8}, {3120})
        # Both connected
        for nid in ('gw-west-1', 'gw-west-2'):
            bus._peers[nid].connected = True
            bus._peers[nid].authenticated = True

        # Disconnect primary
        await bus._handle_peer_disconnect(bus._peers['gw-west-1'])
        # Region should still be in TG table
        self.assertIsNotNone(bus.tg_table.get_region('us-west'))

    async def test_disconnect_removes_region_when_all_dead(self):
        """TG table removed when both gateways disconnect."""
        bus = _make_dual_gw_bus()
        bus.tg_table.update_region('us-west', {8}, {3120})
        # Only primary connected
        bus._peers['gw-west-1'].connected = True
        bus._peers['gw-west-1'].authenticated = True

        await bus._handle_peer_disconnect(bus._peers['gw-west-1'])
        # Region should be removed — no other gateway alive
        self.assertIsNone(bus.tg_table.get_region('us-west'))

    def test_priority_in_get_peer_states(self):
        """Priority included in peer state output."""
        bus = _make_dual_gw_bus()
        states = bus.get_peer_states()
        self.assertEqual(states['gw-west-1']['priority'], 0)
        self.assertEqual(states['gw-west-2']['priority'], 1)


# ========== Latency Tracking Tests (Phase 6.5b) ==========

class TestLatencyTracking(unittest.TestCase):
    """Latency history and stats tracking."""

    def test_latency_history_recorded(self):
        """Heartbeat latency appended to history deque."""
        bus = _make_dual_gw_bus()
        peer = bus._peers['gw-west-1']
        peer.latency_ms = 42.5
        peer.latency_history.append(42.5)
        peer.latency_ms = 38.0
        peer.latency_history.append(38.0)
        self.assertEqual(len(peer.latency_history), 2)
        self.assertEqual(list(peer.latency_history), [42.5, 38.0])

    def test_latency_history_maxlen(self):
        """Deque bounded at 60 samples."""
        bus = _make_dual_gw_bus()
        peer = bus._peers['gw-west-1']
        for i in range(100):
            peer.latency_history.append(float(i))
        self.assertEqual(len(peer.latency_history), 60)
        self.assertEqual(peer.latency_history[0], 40.0)  # oldest kept

    def test_consecutive_misses_tracked(self):
        """Consecutive misses increment and reset."""
        bus = _make_dual_gw_bus()
        peer = bus._peers['gw-west-1']
        peer.consecutive_misses = 3
        # Simulate heartbeat received
        peer.consecutive_misses = 0
        self.assertEqual(peer.consecutive_misses, 0)

    def test_get_latency_stats(self):
        """Latency stats computed from history."""
        bus = _make_dual_gw_bus()
        peer = bus._peers['gw-west-1']
        for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
            peer.latency_history.append(v)
        peer.latency_ms = 50.0
        peer.consecutive_misses = 1
        stats = bus.get_latency_stats('gw-west-1')
        self.assertEqual(stats['current_ms'], 50.0)
        self.assertEqual(stats['avg_ms'], 30.0)
        self.assertEqual(stats['min_ms'], 10.0)
        self.assertEqual(stats['max_ms'], 50.0)
        self.assertEqual(stats['jitter_ms'], 40.0)
        self.assertEqual(stats['samples'], 5)
        self.assertEqual(stats['consecutive_misses'], 1)

    def test_latency_stats_in_peer_states(self):
        """get_peer_states() includes latency_stats."""
        bus = _make_dual_gw_bus()
        bus._peers['gw-west-1'].latency_history.append(25.0)
        states = bus.get_peer_states()
        self.assertIn('latency_stats', states['gw-west-1'])
        self.assertEqual(states['gw-west-1']['latency_stats']['samples'], 1)


# ========== Re-Election Suggestion Tests (Phase 6.5b) ==========

class TestReelectionSuggestions(unittest.TestCase):
    """Two-tier re-election: suggest + auto-switch."""

    def _populate_history(self, peer, values):
        """Fill peer's latency history with given values."""
        for v in values:
            peer.latency_history.append(v)
        peer.latency_ms = values[-1] if values else 0

    def test_no_suggestion_single_gateway(self):
        """No suggestion for region with only one gateway."""
        bus = _make_dual_gw_bus()
        # eu has only one gateway
        self._populate_history(bus._peers['gw-eu'], [10.0] * 20)
        suggestions = bus.get_reelection_suggestions()
        eu_suggestions = [s for s in suggestions if s['region_id'] == 'eu']
        self.assertEqual(eu_suggestions, [])

    def test_no_suggestion_insufficient_samples(self):
        """No suggestion until MIN_SUGGEST samples (12)."""
        bus = _make_dual_gw_bus()
        self._populate_history(bus._peers['gw-west-1'], [100.0] * 5)
        self._populate_history(bus._peers['gw-west-2'], [10.0] * 5)
        suggestions = bus.get_reelection_suggestions()
        self.assertEqual(suggestions, [])

    def test_suggest_when_secondary_30pct_faster(self):
        """Suggest tier fires when secondary 30%+ better."""
        bus = _make_dual_gw_bus()
        self._populate_history(bus._peers['gw-west-1'], [100.0] * 15)
        self._populate_history(bus._peers['gw-west-2'], [60.0] * 15)  # 40% better
        suggestions = bus.get_reelection_suggestions()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['level'], 'suggest')
        self.assertEqual(suggestions[0]['suggested_primary'], 'gw-west-2')

    def test_suggest_primary_misses(self):
        """Suggest tier on 2+ missed heartbeats."""
        bus = _make_dual_gw_bus()
        bus._peers['gw-west-1'].consecutive_misses = 3
        bus._peers['gw-west-2'].consecutive_misses = 0
        suggestions = bus.get_reelection_suggestions()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['level'], 'suggest')

    def test_auto_switch_when_50pct_faster_full_window(self):
        """Auto tier fires when secondary 50%+ better over full 60-sample window."""
        bus = _make_dual_gw_bus()
        self._populate_history(bus._peers['gw-west-1'], [200.0] * 60)
        self._populate_history(bus._peers['gw-west-2'], [50.0] * 60)  # 75% better
        suggestions = bus.get_reelection_suggestions()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['level'], 'auto')

    def test_auto_switch_on_5_misses(self):
        """Auto tier fires when primary has 5+ consecutive misses."""
        bus = _make_dual_gw_bus()
        bus._peers['gw-west-1'].consecutive_misses = 5
        bus._peers['gw-west-2'].consecutive_misses = 0
        suggestions = bus.get_reelection_suggestions()
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]['level'], 'auto')

    def test_anti_flap_prevents_rapid_auto_switch(self):
        """Anti-flap guard blocks auto-switch within 5 minutes."""
        bus = _make_dual_gw_bus()
        bus._peers['gw-west-1'].consecutive_misses = 5
        bus._peers['gw-west-2'].consecutive_misses = 0
        # First auto-switch
        bus._maybe_auto_switch()
        # Now gw-west-2 is primary (priority 0) and gw-west-1 is secondary (priority 1)
        self.assertEqual(bus._peers['gw-west-2'].priority, 0)
        self.assertEqual(bus._peers['gw-west-1'].priority, 1)
        # Simulate reverse condition (new primary now has misses)
        bus._peers['gw-west-2'].consecutive_misses = 5
        bus._peers['gw-west-1'].consecutive_misses = 0
        # Try auto-switch again — should be blocked by anti-flap
        bus._maybe_auto_switch()
        # Priorities should NOT have changed
        self.assertEqual(bus._peers['gw-west-2'].priority, 0)
        self.assertEqual(bus._peers['gw-west-1'].priority, 1)

    def test_accept_reelection_swaps_priority(self):
        """accept_reelection swaps primary/secondary priorities."""
        bus = _make_dual_gw_bus()
        result = bus.accept_reelection('us-west')
        self.assertTrue(result['ok'])
        self.assertEqual(result['new_primary'], 'gw-west-2')
        self.assertEqual(bus._peers['gw-west-1'].priority, 1)
        self.assertEqual(bus._peers['gw-west-2'].priority, 0)

    def test_accept_reelection_affects_routing(self):
        """After re-election, send_to_region uses new primary."""
        bus = _make_dual_gw_bus()
        for nid in ('gw-west-1', 'gw-west-2'):
            bus._peers[nid].connected = True
            bus._peers[nid].authenticated = True
        bus.accept_reelection('us-west')
        # Now gw-west-2 should be preferred
        gws = bus._get_gateways_for_region('us-west')
        self.assertEqual(gws[0].node_id, 'gw-west-2')

    def test_accept_reelection_fails_single_gateway(self):
        """accept_reelection fails for region with <2 gateways."""
        bus = _make_dual_gw_bus()
        result = bus.accept_reelection('eu')
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
