"""Data source abstraction for CLI backends.

LocalDataSource talks to the management Unix socket.
RegionalDataSource talks to the regional dashboard REST API.
Same interface, same commands — different backends.
"""
from abc import ABC, abstractmethod
from .mgmt_socket import send_command, DEFAULT_SOCKET
from .rest_client import RestClient


class DataSource(ABC):
    """Abstract data source for CLI commands."""

    @property
    @abstractmethod
    def mode_name(self) -> str:
        """'local', 'regional', or 'world'."""

    @property
    @abstractmethod
    def prompt_label(self) -> str:
        """Text for the CLI prompt (e.g. 'nexus-1')."""

    @property
    def supports_writes(self) -> bool:
        """Whether write operations (reload, drain, etc.) are supported."""
        return False

    @abstractmethod
    def get_status(self) -> dict: ...

    @abstractmethod
    def get_cluster(self) -> dict: ...

    @abstractmethod
    def get_backbone(self) -> dict: ...

    @abstractmethod
    def get_repeaters(self) -> dict: ...

    @abstractmethod
    def get_streams(self) -> dict: ...

    @abstractmethod
    def get_outbounds(self) -> dict: ...

    @abstractmethod
    def get_last_heard(self, count: int = 20) -> dict: ...

    @abstractmethod
    def get_events(self, count: int = 50) -> dict: ...

    @abstractmethod
    def get_stats(self) -> dict: ...

    @abstractmethod
    def get_version(self) -> dict: ...

    def get_topology(self) -> dict:
        return {'error': 'not available in this mode'}

    def get_tg_routes(self) -> dict:
        return {'error': 'not available in this mode'}

    def get_running_config(self) -> dict:
        return {'error': 'not available in this mode'}

    def ping_peer(self, node_id: str) -> dict:
        return {'error': 'not available in this mode'}

    def do_reload(self) -> dict:
        return {'error': 'not supported in this mode'}

    def do_drain(self) -> dict:
        return {'error': 'not supported in this mode'}

    def do_push_topology(self) -> dict:
        return {'error': 'not supported in this mode'}

    def do_accept_reelection(self, region_id: str) -> dict:
        return {'error': 'not supported in this mode'}


class LocalDataSource(DataSource):
    """Talks to a local HBlink4 node via management Unix socket."""

    def __init__(self, socket_path: str = DEFAULT_SOCKET, node_name: str = None):
        self._socket = socket_path
        self._node_name = node_name
        # Try to auto-detect node name
        if not self._node_name:
            try:
                r = send_command('status', self._socket)
                self._node_name = r.get('node_id', 'local')
            except Exception:
                self._node_name = 'local'

    @property
    def mode_name(self) -> str:
        return 'local'

    @property
    def prompt_label(self) -> str:
        return self._node_name

    @property
    def supports_writes(self) -> bool:
        return True

    def _cmd(self, command: str, **kwargs) -> dict:
        try:
            return send_command(command, self._socket, **kwargs)
        except (ConnectionRefusedError, FileNotFoundError):
            return {'error': f'Cannot connect to {self._socket} — is DMR Nexus running?'}
        except Exception as e:
            return {'error': str(e)}

    def get_status(self) -> dict:
        return self._cmd('status')

    def get_cluster(self) -> dict:
        return self._cmd('cluster')

    def get_backbone(self) -> dict:
        return self._cmd('backbone')

    def get_repeaters(self) -> dict:
        return self._cmd('repeaters')

    def get_streams(self) -> dict:
        return self._cmd('streams')

    def get_outbounds(self) -> dict:
        return self._cmd('outbounds')

    def get_last_heard(self, count: int = 20) -> dict:
        return self._cmd('last-heard', count=count)

    def get_events(self, count: int = 50) -> dict:
        return self._cmd('events', count=count)

    def get_stats(self) -> dict:
        return self._cmd('stats')

    def get_version(self) -> dict:
        return self._cmd('version')

    def get_topology(self) -> dict:
        return self._cmd('topology')

    def get_tg_routes(self) -> dict:
        return self._cmd('tg-routes')

    def get_running_config(self) -> dict:
        return self._cmd('running-config')

    def ping_peer(self, node_id: str) -> dict:
        return self._cmd('ping', node_id=node_id)

    def do_reload(self) -> dict:
        return self._cmd('reload')

    def do_drain(self) -> dict:
        return self._cmd('drain')

    def do_push_topology(self) -> dict:
        return self._cmd('push-topology')

    def do_accept_reelection(self, region_id: str) -> dict:
        return self._cmd('accept-reelection', region_id=region_id)


class RegionalDataSource(DataSource):
    """Talks to a regional dashboard via REST API."""

    def __init__(self, base_url: str, region_name: str = 'region'):
        self._client = RestClient(base_url)
        self._region_name = region_name

    @property
    def mode_name(self) -> str:
        return 'regional'

    @property
    def prompt_label(self) -> str:
        return f'{self._region_name}-region'

    def get_status(self) -> dict:
        nodes = self._client.get('/api/nodes')
        stats = self._client.get('/api/stats')
        if 'error' in nodes:
            return nodes
        return {'ok': True, 'nodes': nodes, 'stats': stats}

    def get_cluster(self) -> dict:
        return self._client.get('/api/cluster')

    def get_backbone(self) -> dict:
        return self._client.get('/api/backbone')

    def get_repeaters(self) -> dict:
        data = self._client.get('/api/repeaters')
        if 'error' in data:
            return data
        rpts = data.get('repeaters', data if isinstance(data, list) else [])
        return {'ok': True, 'repeaters': rpts, 'count': len(rpts)}

    def get_streams(self) -> dict:
        data = self._client.get('/api/streams')
        if 'error' in data:
            return data
        streams = data.get('streams', data if isinstance(data, list) else [])
        return {'ok': True, 'streams': streams, 'count': len(streams)}

    def get_outbounds(self) -> dict:
        # Regional dashboard doesn't have a direct outbounds endpoint
        return {'ok': True, 'outbounds': [], 'count': 0}

    def get_last_heard(self, count: int = 20) -> dict:
        data = self._client.get('/api/last-heard')
        if 'error' in data:
            return data
        entries = data.get('last_heard', data if isinstance(data, list) else [])
        return {'ok': True, 'last_heard': entries[:count], 'count': len(entries[:count])}

    def get_events(self, count: int = 50) -> dict:
        data = self._client.get('/api/events', limit=count)
        if 'error' in data:
            return data
        events = data.get('events', data if isinstance(data, list) else [])
        return {'ok': True, 'events': events, 'count': len(events)}

    def get_stats(self) -> dict:
        return self._client.get('/api/stats')

    def get_version(self) -> dict:
        data = self._client.get('/api/config')
        if 'error' in data:
            return data
        return {'ok': True, 'software': 'DMR Nexus Regional', 'region': self._region_name,
                'nodes': data.get('nodes', [])}
