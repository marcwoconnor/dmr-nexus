"""
Cluster topology manager for HBlink4.

Builds and pushes cluster server topology to connected repeaters (RPTTOPO)
and native clients (CLNT_TOPO). Enables automatic failover by giving
clients a pre-cached list of all servers, ordered by priority.

Topology is pushed:
  1. On registration (repeater or CLNT auth completes)
  2. On cluster state change (peer connects/disconnects/dies)
  3. On admin command (hbctl push-topology)
  4. On graceful drain (with draining flag set)
"""

import json
import logging
import time
from typing import Optional, Dict, List, Any, Callable

logger = logging.getLogger(__name__)

# Wire format constants
RPTTOPO = b'RPTTOPO'   # Server → HomeBrew repeater topology push
CMD_TOPO = b'TOPO'     # Server → CLNT native client topology (CLNT + TOPO)

# Topology protocol version
TOPO_VERSION = 1


class TopologyManager:
    """Builds and pushes cluster topology to connected clients.

    The topology payload lists all servers in the cluster with their
    DMR listen addresses, health status, and recommended failover order.
    Repeaters cache this and use it for instant failover when their
    current server dies.
    """

    def __init__(self, node_id: str, dmr_address: str, dmr_port: int,
                 cluster_bus=None, config: dict = None):
        self._node_id = node_id
        self._dmr_address = dmr_address
        self._dmr_port = dmr_port
        self._cluster_bus = cluster_bus
        self._config = config or {}
        self._seq = 0
        self._send_packet: Optional[Callable] = None  # Set by hblink.py
        self._draining = False
        self._draining_peers: set = set()  # node_ids of peers that are draining
        self._load_fn: Optional[Callable] = None  # Returns int load for this node
        self._peer_load_fn: Optional[Callable] = None  # Returns int load for a peer node_id

    def set_send_packet(self, fn: Callable):
        """Set the UDP send function (from HBProtocol)."""
        self._send_packet = fn

    def set_draining(self, draining: bool):
        """Mark this node as draining (graceful shutdown)."""
        self._draining = draining

    def set_draining_peers(self, peers: set):
        """Update the set of draining peer node_ids."""
        self._draining_peers = peers

    def set_load_fn(self, fn: Callable):
        """Set function that returns current load (connected repeater count)."""
        self._load_fn = fn

    def set_peer_load_fn(self, fn: Callable):
        """Set function that returns load (repeater count) for a given peer node_id."""
        self._peer_load_fn = fn

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def build_topology(self) -> dict:
        """Build topology payload from current cluster state.

        Returns a dict ready for JSON serialization containing all
        servers in the cluster with their health and priority info.
        """
        servers = []

        # Add ourselves
        our_load = self._load_fn() if self._load_fn else 0
        servers.append({
            'node_id': self._node_id,
            'address': self._dmr_address,
            'port': self._dmr_port,
            'alive': True,
            'draining': self._draining,
            'load': our_load,
            'latency_ms': 0,
            'priority': 0,  # Will be computed
        })

        # Add cluster peers
        if self._cluster_bus:
            for pid, ps in self._cluster_bus.get_peer_states().items():
                servers.append({
                    'node_id': pid,
                    'address': ps['address'],
                    'port': self._dmr_port,  # Same DMR port for all peers
                    'alive': ps.get('alive', False),
                    'draining': pid in self._draining_peers,
                    'load': self._peer_load_fn(pid) if self._peer_load_fn else 0,
                    'latency_ms': ps.get('latency_ms', 0),
                    'priority': 0,
                })

        # Compute priorities
        servers = self._compute_priorities(servers)

        return {
            'v': TOPO_VERSION,
            'servers': servers,
            'seq': self._next_seq(),
        }

    def _compute_priorities(self, servers: list) -> list:
        """Compute failover priority for each server.

        TODO: This is where the user defines the priority strategy.
        See the placeholder below.
        """
        for s in servers:
            s['priority'] = _compute_server_priority(s)

        # Sort by priority (lower = better)
        servers.sort(key=lambda s: s['priority'])
        return servers

    # ========== Push Methods ==========

    def push_to_repeater(self, repeater_id: bytes, addr: tuple):
        """Send RPTTOPO to a single HomeBrew repeater."""
        if not self._send_packet:
            return
        topo = self.build_topology()
        payload = json.dumps(topo, separators=(',', ':')).encode('utf-8')
        packet = RPTTOPO + repeater_id + payload
        self._send_packet(packet, addr)
        logger.info(f'Topology pushed to repeater {int.from_bytes(repeater_id, "big")} '
                    f'({len(topo["servers"])} servers, seq={topo["seq"]})')

    def push_to_native_client(self, addr: tuple):
        """Send CLNT_TOPO to a single native client."""
        if not self._send_packet:
            return
        try:
            from .cluster_protocol import NATIVE_MAGIC
        except ImportError:
            from cluster_protocol import NATIVE_MAGIC

        topo = self.build_topology()
        payload = json.dumps(topo, separators=(',', ':')).encode('utf-8')
        packet = NATIVE_MAGIC + CMD_TOPO + payload
        self._send_packet(packet, addr)
        logger.info(f'Topology pushed to native client {addr[0]}:{addr[1]} '
                    f'({len(topo["servers"])} servers, seq={topo["seq"]})')

    def push_to_all(self, repeaters: dict, native_clients: dict):
        """Push topology to all connected repeaters and native clients.

        Args:
            repeaters: dict of repeater_id (bytes) -> RepeaterState
            native_clients: dict of addr (tuple) -> client info
        """
        if not self._send_packet:
            return

        count = 0
        for rid, rpt in repeaters.items():
            if rpt.connection_state == 'connected' and rpt.sockaddr:
                self.push_to_repeater(rid, rpt.sockaddr)
                count += 1

        for addr in native_clients:
            self.push_to_native_client(addr)
            count += 1

        logger.info(f'Topology pushed to {count} clients')

    def on_cluster_change(self, repeaters: dict, native_clients: dict):
        """Called when cluster state changes. Pushes updated topology to all."""
        logger.info('Cluster state changed — pushing topology update')
        self.push_to_all(repeaters, native_clients)


def _compute_server_priority(server: dict) -> int:
    """Compute failover priority for a single server.

    Lower number = higher priority (try this server first).

    Args:
        server: dict with keys: node_id, alive, draining, load, latency_ms

    Returns:
        Integer priority score.
    """
    # Dead servers go to the bottom
    if not server.get('alive', False):
        return 9999

    # Draining servers almost as bad
    if server.get('draining', False):
        return 9000

    # Base priority from load + latency
    load = server.get('load', 0)
    latency = server.get('latency_ms', 0)
    return load + int(latency / 10)
