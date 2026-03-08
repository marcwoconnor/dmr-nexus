"""Tests for cluster topology manager (Phase 7.1)."""

import json
import pytest
from unittest.mock import MagicMock, patch, call

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from nexus.topology import TopologyManager, _compute_server_priority, RPTTOPO, CMD_TOPO, TOPO_VERSION


def _make_cluster_bus(peers=None):
    """Create a mock cluster bus with peer states."""
    bus = MagicMock()
    peer_states = {}
    for p in (peers or []):
        peer_states[p['node_id']] = {
            'node_id': p['node_id'],
            'address': p.get('address', '10.0.0.2'),
            'port': p.get('port', 62032),
            'connected': p.get('connected', True),
            'authenticated': True,
            'alive': p.get('alive', True),
            'latency_ms': p.get('latency_ms', 1.0),
            'config_hash': '',
        }
    bus.get_peer_states.return_value = peer_states
    return bus


class TestBuildTopology:
    """Tests for topology payload construction."""

    def test_standalone_no_cluster(self):
        """Standalone server: topology has only itself."""
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        topo = tm.build_topology()
        assert topo['v'] == TOPO_VERSION
        assert len(topo['servers']) == 1
        assert topo['servers'][0]['node_id'] == 'node-1'
        assert topo['servers'][0]['address'] == '10.0.0.1'
        assert topo['servers'][0]['port'] == 62031
        assert topo['servers'][0]['alive'] is True
        assert topo['seq'] == 1

    def test_with_cluster_peers(self):
        """Topology includes cluster peers."""
        bus = _make_cluster_bus([
            {'node_id': 'node-2', 'address': '10.0.0.2', 'alive': True, 'latency_ms': 2.5},
            {'node_id': 'node-3', 'address': '10.0.0.3', 'alive': False, 'latency_ms': 0},
        ])
        tm = TopologyManager('node-1', '10.0.0.1', 62031, cluster_bus=bus)
        topo = tm.build_topology()

        assert len(topo['servers']) == 3
        node_ids = [s['node_id'] for s in topo['servers']]
        assert 'node-1' in node_ids
        assert 'node-2' in node_ids
        assert 'node-3' in node_ids

    def test_seq_increments(self):
        """Each build_topology call increments seq."""
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        t1 = tm.build_topology()
        t2 = tm.build_topology()
        t3 = tm.build_topology()
        assert t1['seq'] == 1
        assert t2['seq'] == 2
        assert t3['seq'] == 3

    def test_draining_flag(self):
        """Draining node shows draining=True in topology."""
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        topo1 = tm.build_topology()
        assert topo1['servers'][0]['draining'] is False

        tm.set_draining(True)
        topo2 = tm.build_topology()
        assert topo2['servers'][0]['draining'] is True

    def test_draining_peers(self):
        """Draining peers marked in topology."""
        bus = _make_cluster_bus([
            {'node_id': 'node-2', 'address': '10.0.0.2', 'alive': True},
        ])
        tm = TopologyManager('node-1', '10.0.0.1', 62031, cluster_bus=bus)
        tm.set_draining_peers({'node-2'})
        topo = tm.build_topology()

        node2 = next(s for s in topo['servers'] if s['node_id'] == 'node-2')
        assert node2['draining'] is True

    def test_load_from_callback(self):
        """Load comes from the load function callback."""
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        tm.set_load_fn(lambda: 42)
        topo = tm.build_topology()
        self_entry = next(s for s in topo['servers'] if s['node_id'] == 'node-1')
        assert self_entry['load'] == 42

    def test_dmr_port_used_for_peers(self):
        """Peer entries use the DMR port, not the cluster port."""
        bus = _make_cluster_bus([
            {'node_id': 'node-2', 'address': '10.0.0.2', 'port': 62032},
        ])
        tm = TopologyManager('node-1', '10.0.0.1', 62031, cluster_bus=bus)
        topo = tm.build_topology()

        node2 = next(s for s in topo['servers'] if s['node_id'] == 'node-2')
        assert node2['port'] == 62031  # DMR port, not cluster port


class TestPriority:
    """Tests for priority computation."""

    def test_alive_before_dead(self):
        """Alive servers always have lower priority than dead."""
        alive = _compute_server_priority({'alive': True, 'load': 100, 'latency_ms': 100})
        dead = _compute_server_priority({'alive': False})
        assert alive < dead

    def test_draining_before_dead(self):
        """Draining servers have lower priority than dead but higher than healthy."""
        healthy = _compute_server_priority({'alive': True, 'load': 0})
        draining = _compute_server_priority({'alive': True, 'draining': True})
        dead = _compute_server_priority({'alive': False})
        assert healthy < draining < dead

    def test_load_affects_priority(self):
        """Higher load = higher priority number (lower preference)."""
        light = _compute_server_priority({'alive': True, 'load': 2, 'latency_ms': 0})
        heavy = _compute_server_priority({'alive': True, 'load': 20, 'latency_ms': 0})
        assert light < heavy

    def test_latency_affects_priority(self):
        """Higher latency = higher priority number."""
        close = _compute_server_priority({'alive': True, 'load': 0, 'latency_ms': 10})
        far = _compute_server_priority({'alive': True, 'load': 0, 'latency_ms': 100})
        assert close < far

    def test_servers_sorted_by_priority(self):
        """build_topology returns servers sorted by priority."""
        bus = _make_cluster_bus([
            {'node_id': 'heavy', 'address': '10.0.0.2', 'alive': True, 'latency_ms': 50},
            {'node_id': 'dead', 'address': '10.0.0.3', 'alive': False},
        ])
        tm = TopologyManager('light', '10.0.0.1', 62031, cluster_bus=bus)
        tm.set_load_fn(lambda: 0)
        topo = tm.build_topology()

        priorities = [s['priority'] for s in topo['servers']]
        assert priorities == sorted(priorities)
        # light (alive, 0 load) < heavy (alive, higher latency) < dead
        assert topo['servers'][0]['node_id'] == 'light'
        assert topo['servers'][-1]['node_id'] == 'dead'


class TestPushToRepeater:
    """Tests for RPTTOPO packet construction."""

    def test_push_sends_rpttopo_packet(self):
        """push_to_repeater sends RPTTOPO + repeater_id + JSON."""
        send_fn = MagicMock()
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        tm.set_send_packet(send_fn)

        rid = (311000).to_bytes(4, 'big')
        addr = ('10.0.0.100', 62031)
        tm.push_to_repeater(rid, addr)

        send_fn.assert_called_once()
        packet, sent_addr = send_fn.call_args[0]
        assert packet[:7] == RPTTOPO
        assert packet[7:11] == rid
        # Rest is JSON
        payload = json.loads(packet[11:])
        assert payload['v'] == TOPO_VERSION
        assert len(payload['servers']) == 1
        assert sent_addr == addr

    def test_push_no_send_without_fn(self):
        """push_to_repeater is a no-op if send_packet not set."""
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        # Should not raise
        rid = (311000).to_bytes(4, 'big')
        tm.push_to_repeater(rid, ('10.0.0.100', 62031))


class TestPushToNativeClient:
    """Tests for CLNT_TOPO packet construction."""

    def test_push_sends_clnt_topo_packet(self):
        """push_to_native_client sends CLNT + TOPO + JSON."""
        send_fn = MagicMock()
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        tm.set_send_packet(send_fn)

        addr = ('10.0.0.100', 62031)
        tm.push_to_native_client(addr)

        send_fn.assert_called_once()
        packet, sent_addr = send_fn.call_args[0]
        assert packet[:4] == b'CLNT'
        assert packet[4:8] == CMD_TOPO
        payload = json.loads(packet[8:])
        assert payload['v'] == TOPO_VERSION
        assert sent_addr == addr


class TestPushToAll:
    """Tests for broadcasting topology to all clients."""

    def test_push_to_all_repeaters_and_clients(self):
        """push_to_all sends to every connected repeater and native client."""
        send_fn = MagicMock()
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        tm.set_send_packet(send_fn)

        # Mock repeaters
        rpt1 = MagicMock()
        rpt1.connection_state = 'connected'
        rpt1.sockaddr = ('10.0.0.100', 62031)
        rpt2 = MagicMock()
        rpt2.connection_state = 'disconnected'
        rpt2.sockaddr = ('10.0.0.101', 62031)
        repeaters = {
            b'\x00\x04\xBE\xE8': rpt1,  # 311000
            b'\x00\x04\xBE\xE9': rpt2,  # 311001 (disconnected, should be skipped)
        }

        native_clients = {
            ('10.0.0.200', 62031): {'token_hash': b'\x01\x02\x03\x04'},
        }

        tm.push_to_all(repeaters, native_clients)

        # Should send to rpt1 and native client, skip rpt2
        assert send_fn.call_count == 2


class TestOnClusterChange:
    """Tests for topology push on cluster state changes."""

    def test_on_cluster_change_pushes_to_all(self):
        """on_cluster_change triggers push_to_all."""
        send_fn = MagicMock()
        tm = TopologyManager('node-1', '10.0.0.1', 62031)
        tm.set_send_packet(send_fn)

        rpt = MagicMock()
        rpt.connection_state = 'connected'
        rpt.sockaddr = ('10.0.0.100', 62031)

        tm.on_cluster_change({b'\x00\x04\xBE\xE8': rpt}, {})
        assert send_fn.call_count == 1  # One repeater


class TestStaleSeqRejection:
    """Tests for sequence number handling (tested via test_repeater parsing)."""

    def test_topology_payload_is_valid_json(self):
        """The topology payload is parseable JSON with expected fields."""
        bus = _make_cluster_bus([
            {'node_id': 'node-2', 'address': '10.0.0.2', 'alive': True},
        ])
        tm = TopologyManager('node-1', '10.0.0.1', 62031, cluster_bus=bus)
        send_fn = MagicMock()
        tm.set_send_packet(send_fn)

        rid = (311000).to_bytes(4, 'big')
        tm.push_to_repeater(rid, ('10.0.0.100', 62031))

        packet = send_fn.call_args[0][0]
        payload = json.loads(packet[11:])

        # Validate structure
        assert 'v' in payload
        assert 'servers' in payload
        assert 'seq' in payload
        assert isinstance(payload['servers'], list)
        for s in payload['servers']:
            assert 'node_id' in s
            assert 'address' in s
            assert 'port' in s
            assert 'alive' in s
            assert 'priority' in s
