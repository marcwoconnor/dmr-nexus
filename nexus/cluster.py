"""
Cluster bus for HBlink4 — TCP mesh between peer servers.

Full mesh topology: every node connects to every other node.
Length-prefixed JSON framing for control messages, binary framing
for stream data (hot path). HMAC-SHA256 auth on connect.
Heartbeat-based failure detection.

Designed for 2-15 node clusters (ham radio scale).
"""

import asyncio
import hashlib
import hmac
import json
import logging
import struct
import time as _time
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, Any, List, Set

logger = logging.getLogger(__name__)

# Wire format constants
FRAME_HEADER = struct.Struct('!I')  # 4-byte big-endian length prefix
MSG_TYPE_JSON = 0x00
MSG_TYPE_STREAM_DATA = 0x01
MSG_TYPE_STREAM_END = 0x02

# Auth protocol
AUTH_CHALLENGE = b'CLST_CHAL'
AUTH_RESPONSE = b'CLST_AUTH'
AUTH_OK = b'CLST_OK'
AUTH_FAIL = b'CLST_FAIL'


@dataclass
class PeerState:
    """Tracks a single cluster peer's connection state."""
    node_id: str
    address: str
    port: int
    connected: bool = False
    authenticated: bool = False
    last_heartbeat_sent: float = 0.0
    last_heartbeat_recv: float = 0.0
    latency_ms: float = 0.0
    writer: Optional[asyncio.StreamWriter] = None
    reader: Optional[asyncio.StreamReader] = None
    _reconnect_task: Optional[asyncio.Task] = None
    _read_task: Optional[asyncio.Task] = None
    recv_buffer: bytes = b''
    config_hash: str = ''


class ClusterBus:
    """
    Manages TCP mesh connections to peer servers.

    Each node acts as both server (accepting inbound peer connections)
    and client (connecting outbound to configured peers). Connections
    are deduplicated by node_id — if both sides connect simultaneously,
    the lower node_id keeps its outbound connection.
    """

    def __init__(self, node_id: str, config: dict, on_message: Callable):
        self._node_id = node_id
        self._config = config
        self._on_message = on_message

        self._bind = config.get('bind', '0.0.0.0')
        self._port = config.get('port', 62032)
        self._secret = config.get('shared_secret', '').encode('utf-8')
        self._heartbeat_interval = config.get('heartbeat_interval', 2.0)
        self._dead_threshold = config.get('dead_threshold', 6.0)
        self._reconnect_interval = config.get('reconnect_interval', 5.0)

        # Peer state keyed by node_id
        self._peers: Dict[str, PeerState] = {}
        for peer_cfg in config.get('peers', []):
            pid = peer_cfg['node_id']
            self._peers[pid] = PeerState(
                node_id=pid,
                address=peer_cfg['address'],
                port=peer_cfg.get('port', 62032)
            )

        self._server: Optional[asyncio.Server] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._tasks: List[asyncio.Task] = []
        self._config_hash = ''
        self._running = False

    # ========== Lifecycle ==========

    async def start(self):
        """Start the cluster bus: listen for inbound, connect to peers, begin heartbeats."""
        self._running = True

        # Start TCP server for inbound peer connections
        self._server = await asyncio.start_server(
            self._handle_inbound_connection,
            self._bind, self._port
        )
        logger.info(f'Cluster bus listening on {self._bind}:{self._port} (node_id={self._node_id})')

        # Connect to all configured peers
        for peer_id, peer in self._peers.items():
            task = asyncio.create_task(
                self._connect_loop(peer),
                name=f'cluster_connect_{peer_id}'
            )
            peer._reconnect_task = task
            self._tasks.append(task)

        # Start heartbeat loop
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name='cluster_heartbeat'
        )
        self._tasks.append(self._heartbeat_task)

    async def stop(self):
        """Gracefully shut down the cluster bus."""
        self._running = False
        logger.info('Cluster bus shutting down...')

        # Close all peer connections first (stops read loops)
        for peer in self._peers.values():
            await self._close_peer(peer)

        # Cancel all tasks (connect loops, heartbeat)
        for task in self._tasks:
            task.cancel()

        # Stop the server
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Wait for task cancellation with a hard timeout
        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                logger.warning('Cluster bus: some tasks did not stop in time')
        self._tasks.clear()

        logger.info('Cluster bus stopped')

    def set_config_hash(self, config_hash: str):
        """Set the config hash to include in heartbeats for drift detection."""
        self._config_hash = config_hash

    # ========== Public API ==========

    async def broadcast(self, msg: dict):
        """Send a JSON message to all connected, authenticated peers."""
        for peer in self._peers.values():
            if peer.connected and peer.authenticated:
                await self._send_json(peer, msg)

    async def send(self, node_id: str, msg: dict):
        """Send a JSON message to a specific peer."""
        peer = self._peers.get(node_id)
        if peer and peer.connected and peer.authenticated:
            await self._send_json(peer, msg)

    async def send_binary(self, node_id: str, msg_type: int, data: bytes):
        """Send a binary message to a specific peer (for stream hot path)."""
        peer = self._peers.get(node_id)
        if peer and peer.connected and peer.authenticated:
            await self._send_raw(peer, msg_type, data)

    async def broadcast_binary(self, msg_type: int, data: bytes):
        """Send a binary message to all connected, authenticated peers."""
        for peer in self._peers.values():
            if peer.connected and peer.authenticated:
                await self._send_raw(peer, msg_type, data)

    def is_peer_alive(self, node_id: str) -> bool:
        """Check if a peer is connected and has recent heartbeats."""
        peer = self._peers.get(node_id)
        if not peer or not peer.connected or not peer.authenticated:
            return False
        if peer.last_heartbeat_recv == 0:
            return peer.connected  # Just connected, no heartbeat yet
        return (_time.time() - peer.last_heartbeat_recv) < self._dead_threshold

    def get_peer_states(self) -> Dict[str, dict]:
        """Get summary of all peer states (for dashboard/status)."""
        result = {}
        for pid, peer in self._peers.items():
            result[pid] = {
                'node_id': pid,
                'address': peer.address,
                'port': peer.port,
                'connected': peer.connected,
                'authenticated': peer.authenticated,
                'alive': self.is_peer_alive(pid),
                'latency_ms': round(peer.latency_ms, 2),
                'last_heartbeat': peer.last_heartbeat_recv,
                'config_hash': peer.config_hash,
            }
        return result

    async def send_stream_start(self, node_ids: list, stream_info: dict):
        """Send stream_start JSON to specific cluster peers."""
        stream_info['type'] = 'stream_start'
        for node_id in node_ids:
            await self.send(node_id, stream_info)

    async def send_stream_data(self, node_ids: list, payload: bytes):
        """Send binary stream data (hot path) to specific cluster peers."""
        for node_id in node_ids:
            await self.send_binary(node_id, MSG_TYPE_STREAM_DATA, payload)

    async def send_stream_end(self, node_ids: list, stream_id_hex: str, reason: str):
        """Send stream_end to specific cluster peers."""
        data = json.dumps({'stream_id': stream_id_hex, 'reason': reason},
                          separators=(',', ':')).encode('utf-8')
        for node_id in node_ids:
            peer = self._peers.get(node_id)
            if peer and peer.connected and peer.authenticated:
                await self._send_raw(peer, MSG_TYPE_STREAM_END, data)

    @property
    def connected_peers(self) -> List[str]:
        """List of connected and authenticated peer node_ids."""
        return [pid for pid, peer in self._peers.items()
                if peer.connected and peer.authenticated]

    # ========== Inbound Connection Handling ==========

    async def _handle_inbound_connection(self, reader: asyncio.StreamReader,
                                         writer: asyncio.StreamWriter):
        """Handle a new inbound TCP connection from a peer."""
        addr = writer.get_extra_info('peername')
        logger.info(f'Cluster: inbound connection from {addr}')

        try:
            # Authenticate the inbound peer
            peer_node_id = await self._auth_inbound(reader, writer)
            if not peer_node_id:
                writer.close()
                await writer.wait_closed()
                return

            peer = self._peers.get(peer_node_id)
            if not peer:
                # Unknown peer — could be a new node not in our config.
                # For now, reject. Future: dynamic peer discovery.
                logger.warning(f'Cluster: rejecting unknown peer {peer_node_id} from {addr}')
                writer.write(AUTH_FAIL)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # Dedup: if we already have an outbound connection to this peer,
            # use tiebreaker: lower node_id keeps its outbound.
            if peer.connected and peer.authenticated:
                if self._node_id < peer_node_id:
                    # We keep our outbound, reject this inbound
                    logger.info(f'Cluster: dedup — keeping outbound to {peer_node_id}, rejecting inbound')
                    writer.write(AUTH_FAIL)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return
                else:
                    # They keep their outbound (this inbound), close our outbound
                    logger.info(f'Cluster: dedup — accepting inbound from {peer_node_id}, closing our outbound')
                    await self._close_peer(peer)

            # Accept the connection
            writer.write(AUTH_OK)
            await writer.drain()

            peer.writer = writer
            peer.reader = reader
            peer.connected = True
            peer.authenticated = True
            peer.last_heartbeat_recv = _time.time()
            logger.info(f'Cluster: peer {peer_node_id} authenticated (inbound)')

            # Notify the server
            await self._on_message({
                'type': 'peer_connected',
                'node_id': peer_node_id,
                'direction': 'inbound'
            })

            # Start reading from this peer
            peer._read_task = asyncio.create_task(
                self._read_loop(peer),
                name=f'cluster_read_{peer_node_id}'
            )

        except (ConnectionError, asyncio.IncompleteReadError) as e:
            logger.warning(f'Cluster: inbound connection error from {addr}: {e}')
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _auth_inbound(self, reader: asyncio.StreamReader,
                            writer: asyncio.StreamWriter) -> Optional[str]:
        """Authenticate an inbound peer. Returns their node_id or None."""
        try:
            # Send challenge
            challenge = hashlib.sha256(str(_time.time()).encode() + self._secret).digest()[:16]
            writer.write(AUTH_CHALLENGE + challenge)
            await writer.drain()

            # Read response (with timeout)
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if not data or not data.startswith(AUTH_RESPONSE):
                logger.warning('Cluster: invalid auth response from inbound peer')
                return None

            payload = data[len(AUTH_RESPONSE):]
            # Response format: HMAC(32) + node_id_length(1) + node_id(variable)
            if len(payload) < 33:
                return None

            their_hmac = payload[:32]
            node_id_len = payload[32]
            if len(payload) < 33 + node_id_len:
                return None
            peer_node_id = payload[33:33 + node_id_len].decode('utf-8')

            # Verify HMAC
            expected = hmac.new(self._secret, challenge + peer_node_id.encode('utf-8'),
                               hashlib.sha256).digest()
            if not hmac.compare_digest(their_hmac, expected):
                logger.warning(f'Cluster: HMAC verification failed for peer claiming to be {peer_node_id}')
                return None

            return peer_node_id

        except asyncio.TimeoutError:
            logger.warning('Cluster: auth timeout on inbound connection')
            return None
        except Exception as e:
            logger.warning(f'Cluster: auth error on inbound: {e}')
            return None

    # ========== Outbound Connection ==========

    async def _connect_loop(self, peer: PeerState):
        """Reconnection loop for an outbound peer connection."""
        while self._running:
            if not peer.connected:
                try:
                    await self._connect_to_peer(peer)
                except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                    logger.debug(f'Cluster: connect to {peer.node_id} ({peer.address}:{peer.port}) failed: {e}')
                except Exception as e:
                    logger.warning(f'Cluster: unexpected error connecting to {peer.node_id}: {e}')

            await asyncio.sleep(self._reconnect_interval)

    async def _connect_to_peer(self, peer: PeerState):
        """Establish an outbound connection to a peer and authenticate."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(peer.address, peer.port),
            timeout=5.0
        )

        try:
            # Read challenge
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if not data or not data.startswith(AUTH_CHALLENGE):
                raise ConnectionError('No challenge received')

            challenge = data[len(AUTH_CHALLENGE):]

            # Send HMAC response with our node_id
            our_hmac = hmac.new(self._secret, challenge + self._node_id.encode('utf-8'),
                                hashlib.sha256).digest()
            node_id_bytes = self._node_id.encode('utf-8')
            response = AUTH_RESPONSE + our_hmac + bytes([len(node_id_bytes)]) + node_id_bytes
            writer.write(response)
            await writer.drain()

            # Read auth result
            result = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if result == AUTH_FAIL:
                # Could be dedup — the other side prefers their outbound to us
                logger.debug(f'Cluster: auth rejected by {peer.node_id} (likely dedup)')
                writer.close()
                await writer.wait_closed()
                return

            if result != AUTH_OK:
                raise ConnectionError(f'Auth failed: {result}')

            # If we already have a connection (race with inbound), apply dedup
            if peer.connected and peer.authenticated:
                if self._node_id < peer.node_id:
                    # We keep this outbound, close old
                    old_writer = peer.writer
                    if old_writer:
                        old_writer.close()
                        try:
                            await old_writer.wait_closed()
                        except Exception:
                            pass
                else:
                    # Keep existing inbound, discard this outbound
                    writer.close()
                    await writer.wait_closed()
                    return

            peer.writer = writer
            peer.reader = reader
            peer.connected = True
            peer.authenticated = True
            peer.last_heartbeat_recv = _time.time()
            logger.info(f'Cluster: connected to peer {peer.node_id} (outbound)')

            # Notify the server
            await self._on_message({
                'type': 'peer_connected',
                'node_id': peer.node_id,
                'direction': 'outbound'
            })

            # Start reading
            if peer._read_task and not peer._read_task.done():
                peer._read_task.cancel()
            peer._read_task = asyncio.create_task(
                self._read_loop(peer),
                name=f'cluster_read_{peer.node_id}'
            )

        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise

    # ========== Read Loop ==========

    async def _read_loop(self, peer: PeerState):
        """Read framed messages from a peer connection."""
        try:
            while self._running and peer.connected:
                # Read 4-byte length prefix
                header = await peer.reader.readexactly(4)
                length = FRAME_HEADER.unpack(header)[0]

                if length > 1_000_000:  # 1MB sanity limit
                    logger.warning(f'Cluster: oversized frame ({length} bytes) from {peer.node_id}, disconnecting')
                    break

                # Read the frame
                frame = await peer.reader.readexactly(length)

                # First byte is message type
                msg_type = frame[0]
                payload = frame[1:]

                if msg_type == MSG_TYPE_JSON:
                    try:
                        msg = json.loads(payload)
                        await self._handle_peer_message(peer, msg)
                    except json.JSONDecodeError as e:
                        logger.warning(f'Cluster: invalid JSON from {peer.node_id}: {e}')
                elif msg_type == MSG_TYPE_STREAM_DATA:
                    # Binary stream data: [4B stream_id][55B DMRD]
                    await self._on_message({
                        'type': 'stream_data_binary',
                        'node_id': peer.node_id,
                        'payload': payload
                    })
                elif msg_type == MSG_TYPE_STREAM_END:
                    try:
                        msg = json.loads(payload)
                        msg['type'] = 'stream_end'
                        msg['node_id'] = peer.node_id
                        await self._on_message(msg)
                    except json.JSONDecodeError:
                        pass

        except asyncio.IncompleteReadError:
            logger.info(f'Cluster: peer {peer.node_id} disconnected (EOF)')
        except ConnectionError as e:
            logger.info(f'Cluster: peer {peer.node_id} connection lost: {e}')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f'Cluster: read error from {peer.node_id}: {e}')
        finally:
            await self._handle_peer_disconnect(peer)

    async def _handle_peer_message(self, peer: PeerState, msg: dict):
        """Process a JSON message from a peer."""
        msg_type = msg.get('type')

        if msg_type == 'heartbeat':
            now = _time.time()
            sent_at = msg.get('sent_at', now)
            peer.last_heartbeat_recv = now
            peer.latency_ms = (now - sent_at) * 1000
            peer.config_hash = msg.get('config_hash', '')

            if self._config_hash and peer.config_hash and peer.config_hash != self._config_hash:
                logger.warning(f'Cluster: config hash mismatch with {peer.node_id} '
                             f'(ours={self._config_hash[:8]}... theirs={peer.config_hash[:8]}...)')

            # Send heartbeat response
            await self._send_json(peer, {
                'type': 'heartbeat_ack',
                'sent_at': sent_at,
                'ack_at': now
            })
            return

        if msg_type == 'heartbeat_ack':
            now = _time.time()
            sent_at = msg.get('sent_at', now)
            peer.latency_ms = (now - sent_at) * 1000
            peer.last_heartbeat_recv = now
            return

        # All other messages go to the server callback
        msg['node_id'] = peer.node_id
        await self._on_message(msg)

    async def _handle_peer_disconnect(self, peer: PeerState):
        """Handle a peer disconnection."""
        was_connected = peer.connected
        peer.connected = False
        peer.authenticated = False
        peer.writer = None
        peer.reader = None

        if was_connected:
            logger.warning(f'Cluster: peer {peer.node_id} disconnected')
            try:
                await self._on_message({
                    'type': 'peer_disconnected',
                    'node_id': peer.node_id
                })
            except Exception as e:
                logger.error(f'Cluster: error in disconnect callback for {peer.node_id}: {e}')

    # ========== Send Helpers ==========

    async def _send_json(self, peer: PeerState, msg: dict):
        """Send a JSON message with length-prefixed framing."""
        if not peer.writer or not peer.connected:
            return
        try:
            payload = json.dumps(msg, separators=(',', ':')).encode('utf-8')
            frame = FRAME_HEADER.pack(1 + len(payload)) + bytes([MSG_TYPE_JSON]) + payload
            peer.writer.write(frame)
            await peer.writer.drain()
        except (ConnectionError, OSError) as e:
            logger.debug(f'Cluster: send failed to {peer.node_id}: {e}')
            await self._handle_peer_disconnect(peer)

    async def _send_raw(self, peer: PeerState, msg_type: int, data: bytes):
        """Send a binary message with length-prefixed framing."""
        if not peer.writer or not peer.connected:
            return
        try:
            frame = FRAME_HEADER.pack(1 + len(data)) + bytes([msg_type]) + data
            peer.writer.write(frame)
            await peer.writer.drain()
        except (ConnectionError, OSError) as e:
            logger.debug(f'Cluster: binary send failed to {peer.node_id}: {e}')
            await self._handle_peer_disconnect(peer)

    # ========== Heartbeat ==========

    async def _heartbeat_loop(self):
        """Periodically send heartbeats and check for dead peers."""
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                now = _time.time()

                for peer in self._peers.values():
                    if not peer.connected or not peer.authenticated:
                        continue

                    # Send heartbeat
                    await self._send_json(peer, {
                        'type': 'heartbeat',
                        'node_id': self._node_id,
                        'sent_at': now,
                        'config_hash': self._config_hash,
                    })
                    peer.last_heartbeat_sent = now

                    # Check for dead peers
                    if peer.last_heartbeat_recv > 0:
                        time_since = now - peer.last_heartbeat_recv
                        if time_since > self._dead_threshold:
                            logger.warning(f'Cluster: peer {peer.node_id} declared dead '
                                         f'(no heartbeat for {time_since:.1f}s)')
                            await self._close_peer(peer)
                            await self._handle_peer_disconnect(peer)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f'Cluster: heartbeat loop error: {e}', exc_info=True)

    # ========== Cleanup ==========

    async def _close_peer(self, peer: PeerState):
        """Close a peer connection cleanly."""
        if peer._read_task and not peer._read_task.done():
            peer._read_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(peer._read_task),
                    timeout=1.0
                )
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
            peer._read_task = None

        if peer.writer:
            try:
                peer.writer.close()
                await peer.writer.wait_closed()
            except Exception:
                pass

        peer.writer = None
        peer.reader = None
        peer.connected = False
        peer.authenticated = False
