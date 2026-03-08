"""Tests for the Nexus CLI datasources and formatters."""
import json
import pytest
from unittest.mock import patch, MagicMock
from io import StringIO

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from nexus_cli.mgmt_socket import send_command
from nexus_cli.datasource import LocalDataSource, RegionalDataSource
from nexus_cli import formatters as fmt


# ═══════════════════════════════════════════════════
#  Mock data fixtures
# ═══════════════════════════════════════════════════

STATUS_RESP = {
    'ok': True, 'node_id': 'nexus-1', 'repeaters': 3,
    'outbounds': 1, 'draining': False, 'cluster_peers': 1,
}

CLUSTER_RESP = {
    'ok': True, 'enabled': True, 'local_node_id': 'nexus-1',
    'peers': [{
        'node_id': 'nexus-2', 'connected': True, 'alive': True,
        'latency_ms': 2.3, 'config_hash': 'a1b2c3d4',
        'repeater_count': 2, 'draining': False,
    }],
}

BACKBONE_RESP = {
    'ok': True, 'enabled': True, 'local_region_id': 'us-east',
    'peers': [{
        'node_id': 'gw-west', 'region_id': 'us-west', 'connected': True,
        'alive': True, 'priority': 0,
        'latency_stats': {'avg_ms': 12.3, 'current_ms': 11.8, 'jitter_ms': 3.2,
                          'consecutive_misses': 0},
    }],
    'suggestions': [],
}

REPEATERS_RESP = {
    'ok': True, 'count': 2,
    'repeaters': [
        {'repeater_id': 311000, 'callsign': 'KK4WTI', 'connection_type': 'hotspot', 'node': 'local'},
        {'repeater_id': 311001, 'callsign': 'N0MJS', 'connection_type': 'repeater', 'node': 'nexus-2'},
    ],
}

STREAMS_RESP = {
    'ok': True, 'count': 1,
    'streams': [{
        'repeater_id': 311000, 'rf_src': 3110001, 'dst_id': 3100,
        'slot': 1, 'call_type': 'group', 'duration': 4.2,
        'packet_count': 142, 'source_node': None,
    }],
}

OUTBOUNDS_RESP = {
    'ok': True, 'count': 1,
    'outbounds': [{
        'name': 'BM-Link', 'address': '10.0.0.5', 'port': 62031,
        'radio_id': 311099, 'connected': True, 'authenticated': True,
    }],
}

VERSION_RESP = {
    'ok': True, 'software': 'DMR Nexus', 'version': '2.0.0',
    'node_id': 'nexus-1', 'region': 'us-east',
}


# ═══════════════════════════════════════════════════
#  DataSource tests
# ═══════════════════════════════════════════════════

class TestLocalDataSource:
    """Tests for LocalDataSource with mocked socket."""

    @patch('nexus_cli.datasource.send_command')
    def test_mode_name(self, mock_send):
        mock_send.return_value = STATUS_RESP
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        assert ds.mode_name == 'local'

    @patch('nexus_cli.datasource.send_command')
    def test_prompt_label(self, mock_send):
        mock_send.return_value = STATUS_RESP
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        assert ds.prompt_label == 'nexus-1'

    @patch('nexus_cli.datasource.send_command')
    def test_supports_writes(self, mock_send):
        mock_send.return_value = STATUS_RESP
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        assert ds.supports_writes is True

    @patch('nexus_cli.datasource.send_command')
    def test_get_status(self, mock_send):
        mock_send.return_value = STATUS_RESP
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        data = ds.get_status()
        assert data['ok']
        assert data['node_id'] == 'nexus-1'
        mock_send.assert_called_with('status', '/tmp/test.sock')

    @patch('nexus_cli.datasource.send_command')
    def test_get_cluster(self, mock_send):
        mock_send.return_value = CLUSTER_RESP
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        data = ds.get_cluster()
        assert data['enabled']
        assert len(data['peers']) == 1

    @patch('nexus_cli.datasource.send_command')
    def test_get_streams(self, mock_send):
        mock_send.return_value = STREAMS_RESP
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        data = ds.get_streams()
        assert data['count'] == 1

    @patch('nexus_cli.datasource.send_command')
    def test_socket_error(self, mock_send):
        mock_send.side_effect = ConnectionRefusedError()
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        data = ds.get_status()
        assert 'error' in data

    @patch('nexus_cli.datasource.send_command')
    def test_do_reload(self, mock_send):
        mock_send.return_value = {'ok': True}
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        data = ds.do_reload()
        assert data['ok']

    @patch('nexus_cli.datasource.send_command')
    def test_ping_peer(self, mock_send):
        mock_send.return_value = {'ok': True, 'node_id': 'nexus-2', 'alive': True,
                                  'latency_ms': 2.3, 'connected': True}
        ds = LocalDataSource('/tmp/test.sock', 'nexus-1')
        data = ds.ping_peer('nexus-2')
        assert data['alive']
        assert data['latency_ms'] == 2.3

    @patch('nexus_cli.datasource.send_command')
    def test_auto_detect_node_name(self, mock_send):
        mock_send.return_value = STATUS_RESP
        ds = LocalDataSource('/tmp/test.sock')
        assert ds.prompt_label == 'nexus-1'


class TestRegionalDataSource:
    """Tests for RegionalDataSource with mocked HTTP."""

    def test_mode_name(self):
        ds = RegionalDataSource('http://10.0.0.1:8080', 'us-east')
        assert ds.mode_name == 'regional'

    def test_prompt_label(self):
        ds = RegionalDataSource('http://10.0.0.1:8080', 'us-east')
        assert ds.prompt_label == 'us-east-region'

    def test_supports_writes_false(self):
        ds = RegionalDataSource('http://10.0.0.1:8080', 'us-east')
        assert ds.supports_writes is False

    @patch('nexus_cli.rest_client.requests.get')
    def test_get_repeaters(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {'repeaters': REPEATERS_RESP['repeaters']}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        ds = RegionalDataSource('http://10.0.0.1:8080', 'us-east')
        data = ds.get_repeaters()
        assert data['ok']
        assert data['count'] == 2

    def test_do_reload_not_supported(self):
        ds = RegionalDataSource('http://10.0.0.1:8080', 'us-east')
        data = ds.do_reload()
        assert 'error' in data


# ═══════════════════════════════════════════════════
#  Formatter tests (verify no exceptions on render)
# ═══════════════════════════════════════════════════

class TestFormatters:
    """Test that formatters don't crash on valid data."""

    def test_fmt_status(self, capsys):
        fmt.fmt_status(STATUS_RESP)

    def test_fmt_cluster(self, capsys):
        fmt.fmt_cluster(CLUSTER_RESP)

    def test_fmt_cluster_disabled(self, capsys):
        fmt.fmt_cluster({'enabled': False})

    def test_fmt_cluster_topology(self, capsys):
        fmt.fmt_cluster_topology({
            'v': 1, 'seq': 5,
            'servers': [
                {'node_id': 'nexus-1', 'address': '10.0.0.1', 'port': 62031,
                 'alive': True, 'draining': False, 'load': 3, 'latency_ms': 0, 'priority': 3},
                {'node_id': 'nexus-2', 'address': '10.0.0.2', 'port': 62031,
                 'alive': True, 'draining': False, 'load': 2, 'latency_ms': 2.3, 'priority': 2},
            ],
        })

    def test_fmt_backbone(self, capsys):
        fmt.fmt_backbone(BACKBONE_RESP)

    def test_fmt_backbone_routes(self, capsys):
        fmt.fmt_backbone_routes({
            'enabled': True, 'routes': {
                'us-west': {'gateway_node_id': 'gw-west', 'slot1_talkgroups': [3100, 3101],
                            'slot2_talkgroups': [], 'updated_at': 0},
            },
        })

    def test_fmt_repeaters(self, capsys):
        fmt.fmt_repeaters(REPEATERS_RESP)

    def test_fmt_repeaters_empty(self, capsys):
        fmt.fmt_repeaters({'repeaters': []})

    def test_fmt_streams(self, capsys):
        fmt.fmt_streams(STREAMS_RESP)

    def test_fmt_streams_empty(self, capsys):
        fmt.fmt_streams({'streams': []})

    def test_fmt_outbounds(self, capsys):
        fmt.fmt_outbounds(OUTBOUNDS_RESP)

    def test_fmt_last_heard(self, capsys):
        fmt.fmt_last_heard({
            'last_heard': [{
                'radio_id': 3110001, 'callsign': 'KK4WTI', 'talkgroup': 3100,
                'slot': 1, 'repeater_id': 311000, 'last_heard': 0, 'source_node': None,
            }],
        })

    def test_fmt_events(self, capsys):
        fmt.fmt_events({
            'events': [
                {'type': 'stream_start', 'timestamp': 0, 'repeater_id': 311000},
            ],
        })

    def test_fmt_stats(self, capsys):
        fmt.fmt_stats({
            'user_cache': {'total_entries': 10, 'valid_entries': 8,
                           'expired_entries': 2, 'timeout_seconds': 14400},
            'repeaters': 3, 'outbounds': 1,
        })

    def test_fmt_version(self, capsys):
        fmt.fmt_version(VERSION_RESP)

    def test_fmt_running_config(self, capsys):
        fmt.fmt_running_config({'config': {'global': {'port': 62031}}})

    def test_fmt_ping_alive(self, capsys):
        fmt.fmt_ping({'ok': True, 'node_id': 'nexus-2', 'alive': True,
                      'latency_ms': 2.3, 'connected': True})

    def test_fmt_ping_down(self, capsys):
        fmt.fmt_ping({'ok': True, 'node_id': 'nexus-2', 'alive': False,
                      'latency_ms': 0, 'connected': False})

    def test_fmt_result_ok(self, capsys):
        fmt.fmt_result({'ok': True, 'message': 'topology pushed'})

    def test_fmt_result_error(self, capsys):
        fmt.fmt_result({'ok': False, 'error': 'cluster not enabled'})


# ═══════════════════════════════════════════════════
#  Management socket command tests
# ═══════════════════════════════════════════════════

class TestMgmtCommands:
    """Test the new management socket commands via the handler directly."""

    def _make_proto(self):
        """Build a minimal mock proto with the attributes mgmt commands access."""
        proto = MagicMock()
        proto._cluster_bus = MagicMock()
        proto._cluster_bus._node_id = 'nexus-1'
        proto._cluster_bus.connected_peers = ['nexus-2']
        proto._cluster_bus.get_peer_states.return_value = {
            'nexus-2': {'connected': True, 'alive': True, 'latency_ms': 2.3,
                        'config_hash': 'abc', 'last_heartbeat': 0},
        }
        proto._backbone_bus = MagicMock()
        proto._backbone_bus._tg_table = MagicMock()
        proto._backbone_bus._tg_table.get_all_regions.return_value = {}
        proto._region_id = 'us-east'
        proto._draining = False
        proto._draining_peers = set()
        proto._cluster_state = {}
        proto._native_clients = {}
        proto._topology_manager = MagicMock()
        proto._topology_manager.build_topology.return_value = {
            'v': 1, 'servers': [], 'seq': 1,
        }
        proto._user_cache = MagicMock()
        proto._user_cache.get_last_heard.return_value = []
        proto._user_cache.get_stats.return_value = {
            'total_entries': 0, 'valid_entries': 0,
            'expired_entries': 0, 'timeout_seconds': 14400,
        }

        # Repeaters with mock streams
        rpt = MagicMock()
        rpt.connection_state = 'connected'
        rpt.get_slot_stream.return_value = None
        rpt.get_callsign_str.return_value = 'KK4WTI'
        rpt.connection_type = 'hotspot'
        proto._repeaters = {b'\x00\x04\xBE\xE8': rpt}

        # Outbounds
        ob_config = MagicMock()
        ob_config.address = '10.0.0.5'
        ob_config.radio_id = 311099
        ob = MagicMock()
        ob.config = ob_config
        ob.port = 62031
        ob.connected = True
        ob.authenticated = True
        proto._outbounds = {'BM-Link': ob}

        return proto

    def _get_handler(self):
        """Import and return the _handle_mgmt_command function.

        This is tricky because it's defined inside async_main().
        Instead we test via the management socket protocol indirectly,
        or we test the CLI datasource layer.
        """
        # We can't directly import the nested function, so we test
        # through the datasource mock layer instead.
        pass

    def test_local_datasource_calls_correct_commands(self):
        """Verify LocalDataSource sends the right command strings."""
        with patch('nexus_cli.datasource.send_command') as mock_send:
            mock_send.return_value = {'ok': True}
            ds = LocalDataSource('/tmp/test.sock', 'nexus-1')

            ds.get_streams()
            mock_send.assert_called_with('streams', '/tmp/test.sock')

            ds.get_outbounds()
            mock_send.assert_called_with('outbounds', '/tmp/test.sock')

            ds.get_last_heard(30)
            mock_send.assert_called_with('last-heard', '/tmp/test.sock', count=30)

            ds.get_stats()
            mock_send.assert_called_with('stats', '/tmp/test.sock')

            ds.get_version()
            mock_send.assert_called_with('version', '/tmp/test.sock')

            ds.get_running_config()
            mock_send.assert_called_with('running-config', '/tmp/test.sock')

            ds.get_topology()
            mock_send.assert_called_with('topology', '/tmp/test.sock')

            ds.get_tg_routes()
            mock_send.assert_called_with('tg-routes', '/tmp/test.sock')

            ds.ping_peer('nexus-2')
            mock_send.assert_called_with('ping', '/tmp/test.sock', node_id='nexus-2')

            ds.do_push_topology()
            mock_send.assert_called_with('push-topology', '/tmp/test.sock')

            ds.do_reload()
            mock_send.assert_called_with('reload', '/tmp/test.sock')

            ds.do_drain()
            mock_send.assert_called_with('drain', '/tmp/test.sock')


# ═══════════════════════════════════════════════════
#  TG plan CLI tests
# ═══════════════════════════════════════════════════

from nexus_cli.shell import NexusShell


class _StubDS(LocalDataSource):
    """Stub that doesn't connect to anything."""
    def __init__(self):
        self._socket = '/tmp/fake.sock'
        self._node_name = 'test'


class TestTGPlanCLI:
    def _make_shell(self, token='test-token'):
        with patch('nexus_cli.datasource.send_command') as mock_send:
            mock_send.return_value = {'ok': True, 'node_id': 'test'}
            ds = _StubDS()
        shell = NexusShell(ds, tg_token=token)
        return shell

    def test_no_token_prints_error(self, capsys):
        shell = self._make_shell(token=None)
        shell.onecmd('tg_plan owners')
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert 'token' in combined.lower() or 'NEXUS_TG_TOKEN' in combined

    def test_health(self, capsys):
        shell = self._make_shell()
        mock_client = MagicMock()
        mock_client.get.return_value = {'ok': True, 'pg_latency_ms': 1.5}
        shell._tg_client = lambda: mock_client
        shell.onecmd('tg_plan health')
        captured = capsys.readouterr()
        assert '1.5' in captured.out

    def test_show_tgs(self, capsys):
        shell = self._make_shell()
        mock_client = MagicMock()
        mock_client.get.return_value = {
            'ok': True, 'radio_id': 311000,
            'slot1': [8, 9], 'slot2': [3120]
        }
        shell._tg_client = lambda: mock_client
        shell.onecmd('tg_plan show 311000')
        captured = capsys.readouterr()
        assert '311000' in captured.out

    def test_set_tgs(self, capsys):
        shell = self._make_shell()
        mock_client = MagicMock()
        mock_client.put.return_value = {
            'ok': True, 'radio_id': 311000, 'slot1': [8, 9], 'slot2': []
        }
        shell._tg_client = lambda: mock_client
        shell.onecmd('tg_plan set 311000 1 8 9')
        call_args = mock_client.put.call_args
        assert call_args[1]['json_data'] == {'slot1': [8, 9]}

    def test_bulk_assign(self, capsys):
        shell = self._make_shell()
        mock_client = MagicMock()
        mock_client.post.return_value = {'ok': True, 'affected_repeaters': 2}
        shell._tg_client = lambda: mock_client
        shell.onecmd('tg_plan bulk 1 8 9 --repeaters 311000 311001')
        captured = capsys.readouterr()
        assert '2' in captured.out

    def test_add_owner(self, capsys):
        shell = self._make_shell()
        mock_client = MagicMock()
        mock_client.post.return_value = {
            'ok': True, 'callsign': 'KK4WTI', 'api_token': 'secret-123'
        }
        shell._tg_client = lambda: mock_client
        shell.onecmd('tg_plan add-owner KK4WTI Marc')
        captured = capsys.readouterr()
        assert 'KK4WTI' in captured.out
        assert 'secret-123' in captured.out


# ═══════════════════════════════════════════════════
#  TG plan formatter tests
# ═══════════════════════════════════════════════════

class TestTGPlanFormatters:
    def test_fmt_tg_owners(self, capsys):
        fmt.fmt_tg_owners([{'callsign': 'KK4WTI', 'name': 'Marc', 'created_at': None}])

    def test_fmt_tg_owners_empty(self, capsys):
        fmt.fmt_tg_owners([])

    def test_fmt_tg_repeaters(self, capsys):
        fmt.fmt_tg_repeaters([{'radio_id': 311000, 'owner_callsign': 'KK4WTI',
                               'name': 'Downtown', 'created_at': None}])

    def test_fmt_tg_repeaters_empty(self, capsys):
        fmt.fmt_tg_repeaters([])

    def test_fmt_tg_assignments(self, capsys):
        fmt.fmt_tg_assignments({'radio_id': 311000, 'slot1': [8, 9], 'slot2': [3120]})

    def test_fmt_tg_assignments_empty(self, capsys):
        fmt.fmt_tg_assignments({'radio_id': 311000, 'slot1': [], 'slot2': []})

    def test_fmt_tg_who_has(self, capsys):
        fmt.fmt_tg_who_has(8, [{'repeater_id': 311000, 'slot': 1,
                                'owner_callsign': 'KK4WTI', 'name': 'Downtown'}])

    def test_fmt_tg_who_has_empty(self, capsys):
        fmt.fmt_tg_who_has(8, [])
