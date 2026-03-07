"""
Backbone bus for HBlink4 — inter-region gateway connections.

Connects gateway nodes across regions for hierarchical routing.
Same wire format as ClusterBus (length-prefixed JSON + binary)
but separate port, separate secrets, separate trust boundary.

Only gateway nodes run the backbone bus. Non-gateway nodes route
cross-region traffic through their local gateway via the regular
cluster bus.

Designed for scaling beyond 10-15 nodes where full mesh becomes expensive.
"""

import asyncio
import hashlib
import hmac as _hmac
import json
import logging
import struct
import time as _time
from dataclasses import dataclass, field
from typing import Dict, Optional, Callable, List, Set, Tuple

logger = logging.getLogger(__name__)

# Reuse wire format from cluster bus
FRAME_HEADER = struct.Struct('!I')  # 4-byte big-endian length prefix
MSG_TYPE_JSON = 0x00
MSG_TYPE_STREAM_DATA = 0x01
MSG_TYPE_STREAM_END = 0x02

# Auth protocol (distinct prefix from cluster bus)
BB_AUTH_CHALLENGE = b'BB_CHAL'
BB_AUTH_RESPONSE = b'BB_AUTH'
BB_AUTH_OK = b'BB_OK'
BB_AUTH_FAIL = b'BB_FAIL'


@dataclass
class RegionalTGSummary:
    """TG summary for a remote region — what TGs they have subscribers for."""
    region_id: str
    slot1_talkgroups: Set[int] = field(default_factory=set)
    slot2_talkgroups: Set[int] = field(default_factory=set)
    updated_at: float = 0.0
    gateway_node_id: str = ''


@dataclass
class BackbonePeerState:
    """Tracks a backbone peer (gateway in another region)."""
    node_id: str
    region_id: str
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


class TalkgroupRoutingTable:
    """Tracks which regions have subscribers for which TGs.

    Gateways compute their own regional TG summary (union of all local +
    intra-region repeater TGs) and advertise it to backbone peers. The
    table lets _calculate_stream_targets decide which regions should
    receive a cross-region stream.
    """

    def __init__(self):
        # region_id -> RegionalTGSummary
        self._regions: Dict[str, RegionalTGSummary] = {}

    def update_region(self, region_id: str, slot1_tgs: Set[int],
                      slot2_tgs: Set[int], gateway_node_id: str = ''):
        """Update TG summary for a region."""
        self._regions[region_id] = RegionalTGSummary(
            region_id=region_id,
            slot1_talkgroups=slot1_tgs,
            slot2_talkgroups=slot2_tgs,
            updated_at=_time.time(),
            gateway_node_id=gateway_node_id,
        )

    def remove_region(self, region_id: str):
        """Remove a region's TG summary (gateway disconnected)."""
        self._regions.pop(region_id, None)

    def get_target_regions(self, slot: int, talkgroup: int,
                           exclude_region: str = '') -> List[str]:
        """Find regions that have subscribers for a TG on a slot.

        Args:
            slot: Timeslot (1 or 2)
            talkgroup: Talkgroup ID (int)
            exclude_region: Don't include this region (usually our own)

        Returns:
            List of region_ids that should receive this stream.
        """
        result = []
        for region_id, summary in self._regions.items():
            if region_id == exclude_region:
                continue
            tgs = summary.slot1_talkgroups if slot == 1 else summary.slot2_talkgroups
            if talkgroup in tgs:
                result.append(region_id)
        return result

    def get_region(self, region_id: str) -> Optional[RegionalTGSummary]:
        """Get TG summary for a specific region."""
        return self._regions.get(region_id)

    def get_all_regions(self) -> Dict[str, dict]:
        """Get all region summaries as dicts (for dashboard/status)."""
        return {
            rid: {
                'region_id': rid,
                'slot1_talkgroups': sorted(s.slot1_talkgroups),
                'slot2_talkgroups': sorted(s.slot2_talkgroups),
                'updated_at': s.updated_at,
                'gateway_node_id': s.gateway_node_id,
            }
            for rid, s in self._regions.items()
        }

    @property
    def region_count(self) -> int:
        return len(self._regions)


class BackboneBus:
    """
    Manages TCP connections between gateways in different regions.

    Similar to ClusterBus but:
    - Connects only to gateways in OTHER regions (not intra-region)
    - Uses a separate port (default 62033) and separate secret
    - Carries region_id in auth and messages
    - Exchanges TG summaries instead of per-repeater state
    """

    def __init__(self, node_id: str, region_id: str, config: dict,
                 on_message: Callable):
        self._node_id = node_id
        self._region_id = region_id
        self._config = config
        self._on_message = on_message

        self._bind = config.get('bind', '0.0.0.0')
        self._port = config.get('port', 62033)
        self._secret = config.get('backbone_secret', '').encode('utf-8')
        self._heartbeat_interval = config.get('heartbeat_interval', 5.0)
        self._dead_threshold = config.get('dead_threshold', 15.0)
        self._reconnect_interval = config.get('reconnect_interval', 10.0)

        # Backbone peers keyed by node_id
        self._peers: Dict[str, BackbonePeerState] = {}
        for peer_cfg in config.get('gateways', []):
            pid = peer_cfg['node_id']
            self._peers[pid] = BackbonePeerState(
                node_id=pid,
                region_id=peer_cfg['region_id'],
                address=peer_cfg['address'],
                port=peer_cfg.get('port', 62033),
            )

        # TG routing table
        self.tg_table = TalkgroupRoutingTable()

        # Allowed regions (reject connections from unknown regions)
        self._allowed_regions: Set[str] = set()
        for peer_cfg in config.get('gateways', []):
            self._allowed_regions.add(peer_cfg['region_id'])

        self._server: Optional[asyncio.Server] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._tasks: List[asyncio.Task] = []
        self._running = False

        # Track our own TG summary for change detection
        self._last_tg_summary: Optional[Tuple[frozenset, frozenset]] = None

    # ========== Lifecycle ==========

    async def start(self):
        """Start the backbone bus."""
        self._running = True

        self._server = await asyncio.start_server(
            self._handle_inbound, self._bind, self._port
        )
        logger.info(f'Backbone bus listening on {self._bind}:{self._port} '
                    f'(node={self._node_id}, region={self._region_id})')

        for peer_id, peer in self._peers.items():
            task = asyncio.create_task(
                self._connect_loop(peer),
                name=f'backbone_connect_{peer_id}'
            )
            peer._reconnect_task = task
            self._tasks.append(task)

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name='backbone_heartbeat'
        )
        self._tasks.append(self._heartbeat_task)

    async def stop(self):
        """Gracefully shut down the backbone bus."""
        self._running = False
        logger.info('Backbone bus shutting down...')

        for peer in self._peers.values():
            await self._close_peer(peer)

        for task in self._tasks:
            task.cancel()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=3.0
                )
            except asyncio.TimeoutError:
                logger.warning('Backbone bus: some tasks did not stop in time')
        self._tasks.clear()
        logger.info('Backbone bus stopped')

    # ========== TG Summary ==========

    def compute_regional_tg_summary(self, cluster_state: dict,
                                     local_repeaters: dict) -> Tuple[Set[int], Set[int]]:
        """Compute the TG union for our region from local + intra-region repeaters.

        Args:
            cluster_state: dict of node_id -> {repeater_id -> repeater_info}
            local_repeaters: dict of repeater_id (bytes) -> RepeaterState

        Returns:
            (slot1_tgs, slot2_tgs) as sets of ints
        """
        s1: Set[int] = set()
        s2: Set[int] = set()

        # Local repeaters (bytes TG sets → int)
        for rpt in local_repeaters.values():
            if hasattr(rpt, 'slot1_talkgroups') and rpt.slot1_talkgroups:
                # None means allow-all — never propagate wildcards to backbone
                for tg in rpt.slot1_talkgroups:
                    if isinstance(tg, bytes):
                        s1.add(int.from_bytes(tg, 'big'))
                    else:
                        s1.add(tg)
            if hasattr(rpt, 'slot2_talkgroups') and rpt.slot2_talkgroups:
                for tg in rpt.slot2_talkgroups:
                    if isinstance(tg, bytes):
                        s2.add(int.from_bytes(tg, 'big'))
                    else:
                        s2.add(tg)

        # Intra-region repeaters from cluster state
        for node_id, repeaters in cluster_state.items():
            for rid, rpt_info in repeaters.items():
                tgs1 = rpt_info.get('slot1_talkgroups')
                tgs2 = rpt_info.get('slot2_talkgroups')
                # None = allow-all — skip (never propagate wildcards)
                if tgs1 is not None:
                    s1.update(tgs1)
                if tgs2 is not None:
                    s2.update(tgs2)

        if len(s1) + len(s2) > 500:
            logger.warning(f'Regional TG summary is large: {len(s1)} slot1 + {len(s2)} slot2 TGs')

        return s1, s2

    async def advertise_tg_summary(self, s1: Set[int], s2: Set[int]):
        """Broadcast our region's TG summary to backbone peers if changed."""
        current = (frozenset(s1), frozenset(s2))
        if current == self._last_tg_summary:
            return  # No change
        self._last_tg_summary = current

        msg = {
            'type': 'tg_summary',
            'region_id': self._region_id,
            'slot1_talkgroups': sorted(s1),
            'slot2_talkgroups': sorted(s2),
        }
        await self.broadcast(msg)
        logger.info(f'Backbone: advertised TG summary — {len(s1)} slot1 + {len(s2)} slot2 TGs')

    # ========== Public API ==========

    async def broadcast(self, msg: dict):
        """Send a JSON message to all connected backbone peers."""
        for peer in self._peers.values():
            if peer.connected and peer.authenticated:
                await self._send_json(peer, msg)

    async def send_to_region(self, region_id: str, msg: dict):
        """Send a JSON message to the gateway of a specific region."""
        for peer in self._peers.values():
            if peer.region_id == region_id and peer.connected and peer.authenticated:
                await self._send_json(peer, msg)
                return  # One gateway per region is enough

    async def send_binary_to_region(self, region_id: str, msg_type: int, data: bytes):
        """Send binary data to the gateway of a specific region."""
        for peer in self._peers.values():
            if peer.region_id == region_id and peer.connected and peer.authenticated:
                await self._send_raw(peer, msg_type, data)
                return

    def is_peer_alive(self, node_id: str) -> bool:
        peer = self._peers.get(node_id)
        if not peer or not peer.connected or not peer.authenticated:
            return False
        if peer.last_heartbeat_recv == 0:
            return peer.connected
        return (_time.time() - peer.last_heartbeat_recv) < self._dead_threshold

    def get_peer_states(self) -> Dict[str, dict]:
        result = {}
        for pid, peer in self._peers.items():
            result[pid] = {
                'node_id': pid,
                'region_id': peer.region_id,
                'address': peer.address,
                'port': peer.port,
                'connected': peer.connected,
                'authenticated': peer.authenticated,
                'alive': self.is_peer_alive(pid),
                'latency_ms': round(peer.latency_ms, 2),
                'last_heartbeat': peer.last_heartbeat_recv,
            }
        return result

    def get_region_for_peer(self, node_id: str) -> Optional[str]:
        """Get the region_id for a backbone peer."""
        peer = self._peers.get(node_id)
        return peer.region_id if peer else None

    @property
    def connected_peers(self) -> List[str]:
        return [pid for pid, peer in self._peers.items()
                if peer.connected and peer.authenticated]

    @property
    def connected_regions(self) -> Set[str]:
        """Set of region_ids with at least one connected gateway."""
        return {peer.region_id for peer in self._peers.values()
                if peer.connected and peer.authenticated}

    # ========== Stream Forwarding ==========

    async def send_stream_start(self, region_ids: List[str], stream_info: dict):
        """Send stream_start to gateways in specific regions."""
        stream_info['type'] = 'backbone_stream_start'
        stream_info['origin_region'] = self._region_id
        for region_id in region_ids:
            await self.send_to_region(region_id, stream_info)

    async def send_stream_data(self, region_ids: List[str], payload: bytes):
        """Send binary stream data to gateways in specific regions."""
        for region_id in region_ids:
            await self.send_binary_to_region(region_id, MSG_TYPE_STREAM_DATA, payload)

    async def send_stream_end(self, region_ids: List[str], stream_id_hex: str, reason: str):
        """Send stream_end to gateways in specific regions."""
        data = json.dumps({
            'stream_id': stream_id_hex,
            'reason': reason,
            'origin_region': self._region_id,
        }, separators=(',', ':')).encode('utf-8')
        for region_id in region_ids:
            await self.send_binary_to_region(region_id, MSG_TYPE_STREAM_END, data)

    # ========== Inbound Connection Handling ==========

    async def _handle_inbound(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter):
        addr = writer.get_extra_info('peername')
        logger.info(f'Backbone: inbound connection from {addr}')

        try:
            peer_node_id, peer_region_id = await self._auth_inbound(reader, writer)
            if not peer_node_id:
                writer.close()
                await writer.wait_closed()
                return

            # Reject unknown peers
            peer = self._peers.get(peer_node_id)
            if not peer:
                logger.warning(f'Backbone: rejecting unknown gateway {peer_node_id} '
                             f'(region={peer_region_id}) from {addr}')
                writer.write(BB_AUTH_FAIL)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # Reject unknown regions
            if peer_region_id not in self._allowed_regions:
                logger.warning(f'Backbone: rejecting gateway from unknown region {peer_region_id}')
                writer.write(BB_AUTH_FAIL)
                await writer.drain()
                writer.close()
                await writer.wait_closed()
                return

            # Dedup (same as cluster bus)
            if peer.connected and peer.authenticated:
                if self._node_id < peer_node_id:
                    logger.info(f'Backbone: dedup — keeping outbound to {peer_node_id}')
                    writer.write(BB_AUTH_FAIL)
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                    return
                else:
                    logger.info(f'Backbone: dedup — accepting inbound from {peer_node_id}')
                    await self._close_peer(peer)

            writer.write(BB_AUTH_OK)
            await writer.drain()

            peer.writer = writer
            peer.reader = reader
            peer.connected = True
            peer.authenticated = True
            peer.last_heartbeat_recv = _time.time()
            logger.info(f'Backbone: gateway {peer_node_id} (region={peer_region_id}) authenticated')

            await self._on_message({
                'type': 'backbone_peer_connected',
                'node_id': peer_node_id,
                'region_id': peer_region_id,
            })

            peer._read_task = asyncio.create_task(
                self._read_loop(peer),
                name=f'backbone_read_{peer_node_id}'
            )

        except (ConnectionError, asyncio.IncompleteReadError) as e:
            logger.warning(f'Backbone: inbound connection error from {addr}: {e}')
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _auth_inbound(self, reader, writer) -> Tuple[Optional[str], str]:
        """Authenticate inbound backbone peer. Returns (node_id, region_id) or (None, '')."""
        try:
            challenge = hashlib.sha256(
                str(_time.time()).encode() + self._secret
            ).digest()[:16]
            writer.write(BB_AUTH_CHALLENGE + challenge)
            await writer.drain()

            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if not data or not data.startswith(BB_AUTH_RESPONSE):
                return None, ''

            payload = data[len(BB_AUTH_RESPONSE):]
            # Format: HMAC(32) + node_id_len(1) + node_id + region_id_len(1) + region_id
            if len(payload) < 34:
                return None, ''

            their_hmac = payload[:32]
            node_id_len = payload[32]
            if len(payload) < 34 + node_id_len:
                return None, ''
            peer_node_id = payload[33:33 + node_id_len].decode('utf-8')

            region_id_len = payload[33 + node_id_len]
            region_start = 34 + node_id_len
            if len(payload) < region_start + region_id_len:
                return None, ''
            peer_region_id = payload[region_start:region_start + region_id_len].decode('utf-8')

            expected = _hmac.new(
                self._secret,
                challenge + peer_node_id.encode() + peer_region_id.encode(),
                hashlib.sha256
            ).digest()
            if not _hmac.compare_digest(their_hmac, expected):
                logger.warning(f'Backbone: HMAC failed for {peer_node_id}')
                return None, ''

            return peer_node_id, peer_region_id

        except asyncio.TimeoutError:
            logger.warning('Backbone: auth timeout on inbound')
            return None, ''
        except Exception as e:
            logger.warning(f'Backbone: auth error: {e}')
            return None, ''

    # ========== Outbound Connection ==========

    async def _connect_loop(self, peer: BackbonePeerState):
        while self._running:
            if not peer.connected:
                try:
                    await self._connect_to_peer(peer)
                except (ConnectionError, OSError, asyncio.TimeoutError) as e:
                    logger.debug(f'Backbone: connect to {peer.node_id} failed: {e}')
                except Exception as e:
                    logger.warning(f'Backbone: unexpected error connecting to {peer.node_id}: {e}')
            await asyncio.sleep(self._reconnect_interval)

    async def _connect_to_peer(self, peer: BackbonePeerState):
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(peer.address, peer.port),
            timeout=10.0  # Longer timeout for cross-region links
        )
        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if not data or not data.startswith(BB_AUTH_CHALLENGE):
                raise ConnectionError('No backbone challenge received')

            challenge = data[len(BB_AUTH_CHALLENGE):]

            our_hmac = _hmac.new(
                self._secret,
                challenge + self._node_id.encode() + self._region_id.encode(),
                hashlib.sha256
            ).digest()
            node_id_bytes = self._node_id.encode()
            region_id_bytes = self._region_id.encode()
            response = (BB_AUTH_RESPONSE + our_hmac +
                       bytes([len(node_id_bytes)]) + node_id_bytes +
                       bytes([len(region_id_bytes)]) + region_id_bytes)
            writer.write(response)
            await writer.drain()

            result = await asyncio.wait_for(reader.read(4096), timeout=5.0)
            if result == BB_AUTH_FAIL:
                logger.debug(f'Backbone: auth rejected by {peer.node_id} (likely dedup)')
                writer.close()
                await writer.wait_closed()
                return
            if result != BB_AUTH_OK:
                raise ConnectionError(f'Backbone auth failed: {result}')

            if peer.connected and peer.authenticated:
                if self._node_id < peer.node_id:
                    old_writer = peer.writer
                    if old_writer:
                        old_writer.close()
                        try:
                            await old_writer.wait_closed()
                        except Exception:
                            pass
                else:
                    writer.close()
                    await writer.wait_closed()
                    return

            peer.writer = writer
            peer.reader = reader
            peer.connected = True
            peer.authenticated = True
            peer.last_heartbeat_recv = _time.time()
            logger.info(f'Backbone: connected to gateway {peer.node_id} '
                       f'(region={peer.region_id})')

            await self._on_message({
                'type': 'backbone_peer_connected',
                'node_id': peer.node_id,
                'region_id': peer.region_id,
            })

            if peer._read_task and not peer._read_task.done():
                peer._read_task.cancel()
            peer._read_task = asyncio.create_task(
                self._read_loop(peer),
                name=f'backbone_read_{peer.node_id}'
            )

        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise

    # ========== Read Loop ==========

    async def _read_loop(self, peer: BackbonePeerState):
        try:
            while self._running and peer.connected:
                header = await peer.reader.readexactly(4)
                length = FRAME_HEADER.unpack(header)[0]

                if length > 1_000_000:
                    logger.warning(f'Backbone: oversized frame from {peer.node_id}')
                    break

                frame = await peer.reader.readexactly(length)
                msg_type = frame[0]
                payload = frame[1:]

                if msg_type == MSG_TYPE_JSON:
                    try:
                        msg = json.loads(payload)
                        await self._handle_peer_message(peer, msg)
                    except json.JSONDecodeError as e:
                        logger.warning(f'Backbone: invalid JSON from {peer.node_id}: {e}')
                elif msg_type == MSG_TYPE_STREAM_DATA:
                    await self._on_message({
                        'type': 'backbone_stream_data',
                        'node_id': peer.node_id,
                        'region_id': peer.region_id,
                        'payload': payload,
                    })
                elif msg_type == MSG_TYPE_STREAM_END:
                    try:
                        msg = json.loads(payload)
                        msg['type'] = 'backbone_stream_end'
                        msg['node_id'] = peer.node_id
                        msg['region_id'] = peer.region_id
                        await self._on_message(msg)
                    except json.JSONDecodeError:
                        pass

        except asyncio.IncompleteReadError:
            logger.info(f'Backbone: gateway {peer.node_id} disconnected (EOF)')
        except ConnectionError as e:
            logger.info(f'Backbone: gateway {peer.node_id} connection lost: {e}')
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f'Backbone: read error from {peer.node_id}: {e}')
        finally:
            await self._handle_peer_disconnect(peer)

    async def _handle_peer_message(self, peer: BackbonePeerState, msg: dict):
        msg_type = msg.get('type')

        if msg_type == 'heartbeat':
            now = _time.time()
            sent_at = msg.get('sent_at', now)
            peer.last_heartbeat_recv = now
            peer.latency_ms = (now - sent_at) * 1000
            await self._send_json(peer, {
                'type': 'heartbeat_ack',
                'sent_at': sent_at,
                'ack_at': now,
            })
            return

        if msg_type == 'heartbeat_ack':
            now = _time.time()
            peer.last_heartbeat_recv = now
            peer.latency_ms = (now - msg.get('sent_at', now)) * 1000
            return

        if msg_type == 'tg_summary':
            # Remote region advertising their TG summary
            region_id = msg.get('region_id', peer.region_id)
            s1 = set(msg.get('slot1_talkgroups', []))
            s2 = set(msg.get('slot2_talkgroups', []))
            self.tg_table.update_region(region_id, s1, s2, peer.node_id)
            logger.info(f'Backbone: received TG summary from {region_id} — '
                       f'{len(s1)} slot1 + {len(s2)} slot2 TGs')
            # Forward to server callback for dashboard updates
            msg['node_id'] = peer.node_id
            await self._on_message(msg)
            return

        # All other messages go to server callback
        msg['node_id'] = peer.node_id
        msg['region_id'] = peer.region_id
        await self._on_message(msg)

    async def _handle_peer_disconnect(self, peer: BackbonePeerState):
        was_connected = peer.connected
        peer.connected = False
        peer.authenticated = False
        peer.writer = None
        peer.reader = None

        if was_connected:
            logger.warning(f'Backbone: gateway {peer.node_id} (region={peer.region_id}) disconnected')
            # Remove TG summary for this region
            self.tg_table.remove_region(peer.region_id)
            try:
                await self._on_message({
                    'type': 'backbone_peer_disconnected',
                    'node_id': peer.node_id,
                    'region_id': peer.region_id,
                })
            except Exception as e:
                logger.error(f'Backbone: error in disconnect callback: {e}')

    # ========== Send Helpers ==========

    async def _send_json(self, peer: BackbonePeerState, msg: dict):
        if not peer.writer or not peer.connected:
            return
        try:
            payload = json.dumps(msg, separators=(',', ':')).encode('utf-8')
            frame = FRAME_HEADER.pack(1 + len(payload)) + bytes([MSG_TYPE_JSON]) + payload
            peer.writer.write(frame)
            await peer.writer.drain()
        except (ConnectionError, OSError) as e:
            logger.debug(f'Backbone: send failed to {peer.node_id}: {e}')
            await self._handle_peer_disconnect(peer)

    async def _send_raw(self, peer: BackbonePeerState, msg_type: int, data: bytes):
        if not peer.writer or not peer.connected:
            return
        try:
            frame = FRAME_HEADER.pack(1 + len(data)) + bytes([msg_type]) + data
            peer.writer.write(frame)
            await peer.writer.drain()
        except (ConnectionError, OSError) as e:
            logger.debug(f'Backbone: binary send failed to {peer.node_id}: {e}')
            await self._handle_peer_disconnect(peer)

    # ========== Heartbeat ==========

    async def _heartbeat_loop(self):
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                now = _time.time()

                for peer in self._peers.values():
                    if not peer.connected or not peer.authenticated:
                        continue

                    await self._send_json(peer, {
                        'type': 'heartbeat',
                        'node_id': self._node_id,
                        'region_id': self._region_id,
                        'sent_at': now,
                    })
                    peer.last_heartbeat_sent = now

                    if peer.last_heartbeat_recv > 0:
                        time_since = now - peer.last_heartbeat_recv
                        if time_since > self._dead_threshold:
                            logger.warning(f'Backbone: gateway {peer.node_id} declared dead')
                            await self._close_peer(peer)
                            await self._handle_peer_disconnect(peer)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f'Backbone: heartbeat error: {e}', exc_info=True)

    # ========== Cleanup ==========

    async def _close_peer(self, peer: BackbonePeerState):
        if peer._read_task and not peer._read_task.done():
            peer._read_task.cancel()
            try:
                await asyncio.wait_for(
                    asyncio.shield(peer._read_task), timeout=1.0
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
