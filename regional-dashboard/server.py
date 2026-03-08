"""
DMR Nexus Regional Dashboard
Aggregates data from multiple node dashboards via their WebSocket APIs.
"""
import asyncio
import json
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import httpx
import websockets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="DMR Nexus Regional Dashboard", version="1.0.0")


# --- Config ---

def load_config() -> dict:
    config_path = Path(__file__).parent / "config.json"
    defaults = {
        "region_name": "DMR Region",
        "dashboard_title": "DMR Nexus Regional Dashboard",
        "listen_host": "0.0.0.0",
        "listen_port": 8080,
        "nodes": [],
        "reconnect_interval": 5,
        "max_reconnect_interval": 30,
    }
    if config_path.exists():
        with open(config_path) as f:
            return {**defaults, **json.load(f)}
    logger.warning("config.json not found, using defaults (no nodes configured)")
    return defaults


config = load_config()


# --- Per-Node State ---

@dataclass
class NodeState:
    node_id: str
    host: str
    port: int
    connected: bool = False
    hblink_connected: bool = False
    node_name: str = ""
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location: str = ""
    repeaters: Dict[int, dict] = field(default_factory=dict)
    repeater_details: Dict[int, dict] = field(default_factory=dict)
    outbounds: Dict[str, dict] = field(default_factory=dict)
    streams: Dict[str, dict] = field(default_factory=dict)
    events: deque = field(default_factory=lambda: deque(maxlen=100))
    stats: dict = field(default_factory=lambda: {
        'total_calls_today': 0, 'total_duration_today': 0.0,
        'retransmitted_calls': 0
    })
    last_heard: List[dict] = field(default_factory=list)
    cluster: dict = field(default_factory=dict)
    backbone: dict = field(default_factory=dict)
    last_update: float = 0.0


# --- Aggregator ---

class RegionalAggregator:
    def __init__(self):
        self.nodes: Dict[str, NodeState] = {}
        self.ws_clients: Set[WebSocket] = set()

    def init_nodes(self, node_configs: list):
        for nc in node_configs:
            nid = nc['node_id']
            self.nodes[nid] = NodeState(
                node_id=nid, host=nc['host'], port=nc['port'],
                node_name=nid,  # default, updated from /api/config
                latitude=nc.get('latitude'),
                longitude=nc.get('longitude'),
                location=nc.get('location', ''),
            )

    # --- State from initial_state ---

    def apply_initial_state(self, node_id: str, data: dict):
        ns = self.nodes.get(node_id)
        if not ns:
            return
        ns.hblink_connected = data.get('hblink_connected', False)

        # Repeaters: list → dict keyed by repeater_id
        ns.repeaters = {}
        for r in data.get('repeaters', []):
            rid = r.get('repeater_id')
            if rid is not None:
                ns.repeaters[rid] = r

        ns.repeater_details = data.get('repeater_details', {})
        # Convert string keys to int if needed
        if ns.repeater_details and isinstance(next(iter(ns.repeater_details.keys()), None), str):
            ns.repeater_details = {int(k): v for k, v in ns.repeater_details.items()}

        # Outbounds: list → dict keyed by connection_name
        ns.outbounds = {}
        for o in data.get('outbounds', []):
            name = o.get('connection_name')
            if name:
                ns.outbounds[name] = o

        # Streams: list → dict keyed by stream key
        ns.streams = {}
        for s in data.get('streams', []):
            key = self._stream_key(s)
            ns.streams[key] = s

        ns.stats = data.get('stats', ns.stats)
        ns.last_heard = data.get('last_heard', [])
        ns.cluster = data.get('cluster', {})
        ns.backbone = data.get('backbone', {})
        ns.events = deque(data.get('events', []), maxlen=100)
        ns.last_update = time.time()

    # --- Incremental event updates ---

    def apply_event(self, node_id: str, event: dict):
        ns = self.nodes.get(node_id)
        if not ns:
            return
        ns.last_update = time.time()
        etype = event.get('type')
        data = event.get('data', {})

        if etype == 'repeater_connected':
            rid = data.get('repeater_id')
            if rid is not None:
                ns.repeaters[rid] = {**data, 'status': 'connected'}

        elif etype == 'repeater_disconnected':
            rid = data.get('repeater_id')
            if rid is not None:
                ns.repeaters.pop(rid, None)

        elif etype == 'repeater_keepalive':
            rid = data.get('repeater_id')
            if rid in ns.repeaters:
                ns.repeaters[rid]['last_ping'] = data.get('last_ping')
                ns.repeaters[rid]['missed_pings'] = data.get('missed_pings', 0)

        elif etype == 'repeater_details':
            rid = data.get('repeater_id')
            if rid is not None:
                ns.repeater_details[rid] = data

        elif etype == 'repeater_options_updated':
            rid = data.get('repeater_id')
            if rid in ns.repeaters:
                ns.repeaters[rid]['slot1_talkgroups'] = data.get('slot1_talkgroups')
                ns.repeaters[rid]['slot2_talkgroups'] = data.get('slot2_talkgroups')
                ns.repeaters[rid]['rpto_received'] = data.get('rpto_received', False)

        elif etype == 'stream_start':
            key = self._stream_key(data)
            ns.streams[key] = {**data, 'status': 'active', 'start_time': event.get('timestamp')}
            if not data.get('is_assumed', False):
                ns.events.append(event)

        elif etype == 'stream_end':
            key = self._stream_key(data)
            if key in ns.streams:
                ns.streams[key]['status'] = 'hang_time'
                ns.streams[key].update({
                    k: data[k] for k in ('end_reason', 'packet_count', 'packets', 'duration')
                    if k in data
                })
            if not data.get('is_assumed', False):
                ns.events.append(event)

        elif etype == 'stream_update':
            key = self._stream_key(data)
            if key in ns.streams:
                ns.streams[key].update({
                    k: data[k] for k in ('packets', 'duration', 'packet_count')
                    if k in data
                })

        elif etype == 'hang_time_expired':
            key = self._stream_key(data)
            ns.streams.pop(key, None)

        elif etype in ('outbound_connecting', 'outbound_connected', 'outbound_disconnected', 'outbound_error'):
            name = data.get('connection_name')
            if name:
                if name not in ns.outbounds:
                    ns.outbounds[name] = data
                status_map = {
                    'outbound_connecting': 'connecting',
                    'outbound_connected': 'connected',
                    'outbound_disconnected': 'disconnected',
                    'outbound_error': 'error',
                }
                ns.outbounds[name]['status'] = status_map.get(etype, 'unknown')
                ns.outbounds[name].update(data)

        elif etype == 'cluster_state':
            ns.cluster = data

        elif etype == 'backbone_state':
            ns.backbone = data

        elif etype == 'hblink_status':
            ns.hblink_connected = data.get('connected', False)

        elif etype == 'stats_reset':
            ns.stats = data

        # Update last_heard if included in event
        if 'last_heard' in event:
            ns.last_heard = event['last_heard']

    # --- Aggregation helpers ---

    def get_all_repeaters(self) -> list:
        result = []
        for nid, ns in self.nodes.items():
            for r in ns.repeaters.values():
                result.append({**r, 'node_id': nid, 'node_name': ns.node_name})
            # Include details
            for rid, det in ns.repeater_details.items():
                for r in result:
                    if r.get('repeater_id') == rid and r.get('node_id') == nid:
                        r['details'] = det
                        break
        return result

    def get_all_streams(self) -> list:
        result = []
        for nid, ns in self.nodes.items():
            for s in ns.streams.values():
                result.append({**s, 'node_id': nid, 'node_name': ns.node_name})
        return result

    def get_merged_last_heard(self, limit: int = 20) -> list:
        all_lh = []
        for nid, ns in self.nodes.items():
            for lh in ns.last_heard:
                all_lh.append({**lh, 'node_id': nid, 'node_name': ns.node_name})
        # Dedup by radio_id (keep most recent)
        seen = {}
        for lh in sorted(all_lh, key=lambda x: x.get('timestamp', 0), reverse=True):
            rid = lh.get('radio_id') or lh.get('src_id')
            if rid and rid not in seen:
                seen[rid] = lh
        return list(seen.values())[:limit]

    def get_aggregated_stats(self) -> dict:
        total_calls = sum(ns.stats.get('total_calls_today', 0) for ns in self.nodes.values())
        total_dur = sum(ns.stats.get('total_duration_today', 0.0) for ns in self.nodes.values())
        total_retx = sum(ns.stats.get('retransmitted_calls', 0) for ns in self.nodes.values())
        total_repeaters = sum(
            len([r for r in ns.repeaters.values() if r.get('status') == 'connected'])
            for ns in self.nodes.values()
        )
        active_streams = sum(
            len([s for s in ns.streams.values() if s.get('status') == 'active'])
            for ns in self.nodes.values()
        )
        nodes_online = sum(1 for ns in self.nodes.values() if ns.connected)
        return {
            'total_calls_today': total_calls,
            'total_duration_today': total_dur,
            'retransmitted_calls': total_retx,
            'repeaters_connected': total_repeaters,
            'active_streams': active_streams,
            'nodes_online': nodes_online,
            'nodes_total': len(self.nodes),
        }

    def get_node_summaries(self) -> list:
        result = []
        for nid, ns in self.nodes.items():
            result.append({
                'node_id': nid,
                'node_name': ns.node_name,
                'host': ns.host,
                'port': ns.port,
                'connected': ns.connected,
                'hblink_connected': ns.hblink_connected,
                'latitude': ns.latitude,
                'longitude': ns.longitude,
                'location': ns.location,
                'repeater_count': len(ns.repeaters),
                'active_streams': len([s for s in ns.streams.values() if s.get('status') == 'active']),
                'stats': ns.stats,
                'cluster': ns.cluster,
                'backbone': ns.backbone,
                'last_update': ns.last_update,
            })
        return result

    def get_merged_events(self, limit: int = 50) -> list:
        all_events = []
        for nid, ns in self.nodes.items():
            for ev in ns.events:
                all_events.append({**ev, 'node_id': nid, 'node_name': ns.node_name})
        all_events.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
        return all_events[:limit]

    def get_initial_state(self) -> dict:
        return {
            'region_name': config.get('region_name', 'DMR Region'),
            'nodes': self.get_node_summaries(),
            'repeaters': self.get_all_repeaters(),
            'streams': self.get_all_streams(),
            'stats': self.get_aggregated_stats(),
            'last_heard': self.get_merged_last_heard(),
            'events': self.get_merged_events(),
        }

    async def broadcast(self, message: dict):
        if not self.ws_clients:
            return
        text = json.dumps(message)
        dead = set()
        for client in self.ws_clients:
            try:
                await client.send_text(text)
            except Exception:
                dead.add(client)
        self.ws_clients -= dead

    @staticmethod
    def _stream_key(data: dict) -> str:
        rid = data.get('repeater_id') or data.get('connection_name', 'unknown')
        slot = data.get('slot', 0)
        return f"{rid}.{slot}"


agg = RegionalAggregator()
agg.init_nodes(config.get('nodes', []))


# --- Node Connector ---

class NodeConnector:
    """WebSocket client connecting to a single node's /ws endpoint."""

    def __init__(self, node_id: str, host: str, port: int, aggregator: RegionalAggregator):
        self.node_id = node_id
        self.host = host
        self.port = port
        self.agg = aggregator
        self.ws_url = f"ws://{host}:{port}/ws"
        self.api_base = f"http://{host}:{port}"
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._task = asyncio.create_task(self._run_forever())

    async def _run_forever(self):
        backoff = config.get('reconnect_interval', 5)
        max_backoff = config.get('max_reconnect_interval', 30)
        delay = backoff

        # Fetch node name once
        await self._fetch_node_config()

        while True:
            try:
                await self._connect()
                delay = backoff  # reset on success
            except Exception as e:
                logger.warning(f"[{self.node_id}] connection failed: {e}")
            finally:
                ns = self.agg.nodes.get(self.node_id)
                if ns:
                    ns.connected = False
                await self.agg.broadcast({
                    'type': 'node_status',
                    'data': {'node_id': self.node_id, 'connected': False}
                })

            logger.info(f"[{self.node_id}] reconnecting in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_backoff)

    async def _fetch_node_config(self):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.api_base}/api/config")
                if resp.status_code == 200:
                    cfg = resp.json()
                    ns = self.agg.nodes.get(self.node_id)
                    if ns:
                        ns.node_name = cfg.get('server_name', self.node_id)
                    logger.info(f"[{self.node_id}] name: {cfg.get('server_name')}")
        except Exception as e:
            logger.warning(f"[{self.node_id}] failed to fetch config: {e}")

    async def _connect(self):
        logger.info(f"[{self.node_id}] connecting to {self.ws_url}")
        async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=10) as ws:
            ns = self.agg.nodes.get(self.node_id)
            if ns:
                ns.connected = True
            await self.agg.broadcast({
                'type': 'node_status',
                'data': {'node_id': self.node_id, 'connected': True}
            })
            logger.info(f"[{self.node_id}] connected")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if msg.get('type') == 'initial_state':
                    self.agg.apply_initial_state(self.node_id, msg.get('data', {}))
                    # Broadcast full refresh to browsers
                    await self.agg.broadcast({
                        'type': 'full_refresh',
                        'data': self.agg.get_initial_state()
                    })
                elif msg.get('type') == 'pong':
                    pass
                else:
                    self.agg.apply_event(self.node_id, msg)
                    # Forward tagged event to browser clients
                    tagged = {**msg, 'node_id': self.node_id}
                    ns = self.agg.nodes.get(self.node_id)
                    if ns:
                        tagged['node_name'] = ns.node_name
                    await self.agg.broadcast(tagged)


# --- REST API ---

@app.get("/api/config")
async def get_config():
    return {
        'region_name': config.get('region_name'),
        'dashboard_title': config.get('dashboard_title'),
        'nodes': [{'node_id': n['node_id']} for n in config.get('nodes', [])],
    }


@app.get("/api/nodes")
async def get_nodes():
    return {"nodes": agg.get_node_summaries()}


@app.get("/api/repeaters")
async def get_repeaters():
    return {"repeaters": agg.get_all_repeaters()}


@app.get("/api/streams")
async def get_streams():
    return {"streams": agg.get_all_streams()}


@app.get("/api/stats")
async def get_stats():
    return {"stats": agg.get_aggregated_stats()}


@app.get("/api/last-heard")
async def get_last_heard():
    return {"last_heard": agg.get_merged_last_heard()}


@app.get("/api/events")
async def get_events(limit: int = 50):
    return {"events": agg.get_merged_events(limit)}


@app.get("/api/cluster")
async def get_cluster():
    # Return cluster data from all nodes
    clusters = {}
    for nid, ns in agg.nodes.items():
        if ns.cluster:
            clusters[nid] = ns.cluster
    return {"clusters": clusters}


@app.get("/api/backbone")
async def get_backbone():
    backbones = {}
    for nid, ns in agg.nodes.items():
        if ns.backbone:
            backbones[nid] = ns.backbone
    return {"backbones": backbones}


@app.get("/api/node/{node_id}/repeater/{repeater_id}")
async def get_repeater_detail(node_id: str, repeater_id: int):
    """Proxy repeater detail request to the correct node."""
    ns = agg.nodes.get(node_id)
    if not ns:
        return {"error": "Unknown node"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"http://{ns.host}:{ns.port}/api/repeater/{repeater_id}")
            return resp.json()
    except Exception as e:
        return {"error": str(e)}


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    agg.ws_clients.add(websocket)
    logger.info(f"Browser connected (total: {len(agg.ws_clients)})")

    # Send initial state
    await websocket.send_json({
        'type': 'initial_state',
        'data': agg.get_initial_state()
    })

    try:
        while True:
            data = await websocket.receive_text()
            if data == 'ping':
                await websocket.send_text('pong')
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        agg.ws_clients.discard(websocket)


# --- Frontend ---

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html_path = Path(__file__).parent / 'static' / 'regional.html'
    if not html_path.exists():
        return HTMLResponse("<h1>Regional dashboard HTML not found</h1>", status_code=404)
    with open(html_path) as f:
        return HTMLResponse(f.read(), headers={'Cache-Control': 'no-cache, must-revalidate'})


static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=static_path), name="static")


# --- Startup ---

@app.on_event("startup")
async def startup():
    logger.info(f"Regional dashboard starting - {len(agg.nodes)} nodes configured")
    for nid, ns in agg.nodes.items():
        connector = NodeConnector(nid, ns.host, ns.port, agg)
        await connector.start()
        logger.info(f"Started connector for {nid} ({ns.host}:{ns.port})")
