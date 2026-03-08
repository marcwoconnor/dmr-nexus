"""Output formatting for the Nexus CLI.

Cisco-style tables with color-coded status fields using rich.
"""
import json
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.syntax import Syntax
from rich.text import Text

console = Console()


def _status_style(status: str) -> str:
    """Map status strings to rich color styles."""
    s = status.lower()
    if s in ('alive', 'connected', 'active', 'true', 'yes'):
        return 'green'
    if s in ('draining', 'warning', 'suggest'):
        return 'yellow'
    if s in ('down', 'dead', 'error', 'false', 'no', 'disconnected'):
        return 'red'
    return 'white'


def _latency_style(ms: float) -> str:
    if ms < 5:
        return 'green'
    if ms < 50:
        return 'yellow'
    return 'red'


def _peer_status(peer: dict) -> str:
    if peer.get('alive'):
        return 'alive'
    if peer.get('connected'):
        return 'connected'
    return 'down'


def _ts(epoch: float) -> str:
    """Format epoch timestamp as HH:MM:SS."""
    if not epoch:
        return '-'
    try:
        return datetime.fromtimestamp(epoch).strftime('%H:%M:%S')
    except (ValueError, OSError):
        return '-'


def _ago(epoch: float) -> str:
    """Format epoch as 'Xs ago' or 'Xm ago'."""
    if not epoch:
        return '-'
    from time import time
    delta = time() - epoch
    if delta < 60:
        return f'{delta:.0f}s ago'
    if delta < 3600:
        return f'{delta/60:.0f}m ago'
    return f'{delta/3600:.1f}h ago'


# ═══════════════════════════════════════════════════════
#  Show handlers — each takes a data dict, prints output
# ═══════════════════════════════════════════════════════

def fmt_status(data: dict):
    """Format `show status` output."""
    console.print()
    console.print(f'  [bold]Node ID[/bold]       {data.get("node_id", "N/A")}')
    console.print(f'  [bold]Repeaters[/bold]     {data.get("repeaters", 0)}')
    console.print(f'  [bold]Outbounds[/bold]     {data.get("outbounds", 0)}')
    console.print(f'  [bold]Cluster Peers[/bold] {data.get("cluster_peers", 0)}')
    draining = data.get('draining', False)
    style = 'yellow' if draining else 'green'
    console.print(f'  [bold]Draining[/bold]      [{style}]{draining}[/{style}]')
    console.print()


def fmt_status_regional(data: dict):
    """Format `show status` for regional mode."""
    nodes = data.get('nodes', {})
    stats = data.get('stats', {})
    if isinstance(nodes, dict) and 'nodes' in nodes:
        nodes = nodes['nodes']
    if isinstance(nodes, list):
        t = Table(title='Region Nodes')
        t.add_column('Node', style='cyan')
        t.add_column('Status')
        t.add_column('HBlink')
        t.add_column('Repeaters', justify='right')
        for n in nodes:
            st = 'connected' if n.get('connected') else 'down'
            hb = 'up' if n.get('hblink_connected') else 'down'
            t.add_row(n.get('node_id', '?'),
                      Text(st, style=_status_style(st)),
                      Text(hb, style=_status_style(hb)),
                      str(n.get('repeater_count', 0)))
        console.print(t)
    if stats:
        console.print(f'\n  Calls today: {stats.get("stats", {}).get("total_calls_today", 0)}')
    console.print()


def fmt_cluster(data: dict):
    """Format `show cluster` output."""
    if not data.get('enabled'):
        console.print('  Clustering not enabled')
        return
    console.print(f'\n  [bold]Local Node:[/bold] {data.get("local_node_id", "?")}')
    peers = data.get('peers', [])
    if not peers:
        console.print('  No cluster peers configured\n')
        return
    t = Table()
    t.add_column('Peer', style='cyan')
    t.add_column('Status')
    t.add_column('Latency', justify='right')
    t.add_column('Repeaters', justify='right')
    t.add_column('Config Hash')
    t.add_column('Draining')
    for p in peers:
        st = _peer_status(p)
        lat = p.get('latency_ms', 0)
        drain = 'Yes' if p.get('draining') else 'No'
        t.add_row(
            p['node_id'],
            Text(st, style=_status_style(st)),
            Text(f'{lat:.1f}ms', style=_latency_style(lat)),
            str(p.get('repeater_count', 0)),
            (p.get('config_hash', '') or '-')[:8],
            Text(drain, style='yellow' if p.get('draining') else 'dim'),
        )
    console.print(t)
    console.print()


def fmt_cluster_topology(data: dict):
    """Format `show cluster topology` output."""
    servers = data.get('servers', [])
    if not servers:
        console.print('  No topology data')
        return
    console.print(f'\n  [bold]Topology v{data.get("v", "?")}[/bold]  seq={data.get("seq", "?")}')
    t = Table()
    t.add_column('Priority', justify='right')
    t.add_column('Node', style='cyan')
    t.add_column('Address')
    t.add_column('Port', justify='right')
    t.add_column('Status')
    t.add_column('Load', justify='right')
    t.add_column('Latency', justify='right')
    t.add_column('Draining')
    for s in servers:
        st = 'alive' if s.get('alive') else 'down'
        drain = 'Yes' if s.get('draining') else 'No'
        lat = s.get('latency_ms', 0)
        t.add_row(
            str(s.get('priority', '-')),
            s.get('node_id', '?'),
            s.get('address', '?'),
            str(s.get('port', '?')),
            Text(st, style=_status_style(st)),
            str(s.get('load', 0)),
            Text(f'{lat:.1f}ms', style=_latency_style(lat)),
            Text(drain, style='yellow' if s.get('draining') else 'dim'),
        )
    console.print(t)
    console.print()


def fmt_backbone(data: dict):
    """Format `show backbone` output."""
    if not data.get('enabled', True) and 'peers' not in data:
        console.print('  Backbone not enabled')
        return
    region = data.get('local_region_id', data.get('region', '?'))
    console.print(f'\n  [bold]Region:[/bold] {region}')
    peers = data.get('peers', [])
    if not peers:
        console.print('  No backbone peers configured\n')
        return
    t = Table()
    t.add_column('Peer', style='cyan')
    t.add_column('Region')
    t.add_column('Pri', justify='right')
    t.add_column('Status')
    t.add_column('Avg', justify='right')
    t.add_column('Cur', justify='right')
    t.add_column('Jitter', justify='right')
    t.add_column('Misses', justify='right')
    for p in peers:
        st = _peer_status(p)
        stats = p.get('latency_stats', {})
        avg = stats.get('avg_ms', 0)
        cur = stats.get('current_ms', p.get('latency_ms', 0))
        jit = stats.get('jitter_ms', 0)
        mis = stats.get('consecutive_misses', 0)
        t.add_row(
            p['node_id'],
            p.get('region_id', '?'),
            str(p.get('priority', '-')),
            Text(st, style=_status_style(st)),
            Text(f'{avg:.1f}ms', style=_latency_style(avg)),
            Text(f'{cur:.1f}ms', style=_latency_style(cur)),
            Text(f'{jit:.1f}ms', style='yellow' if jit > 10 else 'dim'),
            Text(str(mis), style='red' if mis > 0 else 'dim'),
        )
    console.print(t)

    # Reelection suggestions
    for s in data.get('suggestions', []):
        tag = 'AUTO-SWITCH' if s['level'] == 'auto' else 'SUGGEST'
        style = 'red bold' if s['level'] == 'auto' else 'yellow'
        console.print(f'\n  [{style}]{tag}[/{style}]: {s["region_id"]} — '
                      f'promote {s["suggested_primary"]} (reason: {s["reason"]})')
        console.print(f'    Accept: [cyan]accept-reelection {s["region_id"]}[/cyan]')
    console.print()


def fmt_backbone_routes(data: dict):
    """Format `show backbone routes` output."""
    if not data.get('enabled', True) and 'routes' not in data:
        console.print('  Backbone not enabled')
        return
    routes = data.get('routes', {})
    if not routes:
        console.print('  No TG routes')
        return
    t = Table(title='Talkgroup Routing Table')
    t.add_column('Region', style='cyan')
    t.add_column('Gateway')
    t.add_column('Slot 1 TGs')
    t.add_column('Slot 2 TGs')
    t.add_column('Updated')
    for rid, info in routes.items():
        s1 = info.get('slot1_talkgroups', [])
        s2 = info.get('slot2_talkgroups', [])
        s1_str = ', '.join(str(t) for t in s1[:10]) + ('...' if len(s1) > 10 else '') if s1 else '-'
        s2_str = ', '.join(str(t) for t in s2[:10]) + ('...' if len(s2) > 10 else '') if s2 else '-'
        t.add_row(
            rid,
            info.get('gateway_node_id', '?'),
            s1_str, s2_str,
            _ago(info.get('updated_at', 0)),
        )
    console.print(t)
    console.print()


def fmt_repeaters(data: dict):
    """Format `show repeaters` output."""
    rpts = data.get('repeaters', [])
    if not rpts:
        console.print('  No repeaters connected\n')
        return
    t = Table()
    t.add_column('ID', justify='right')
    t.add_column('Callsign', style='cyan')
    t.add_column('Type')
    t.add_column('Node')
    for r in rpts:
        t.add_row(
            str(r.get('repeater_id', '?')),
            r.get('callsign', '').strip() or '-',
            r.get('connection_type', '?'),
            r.get('node', r.get('node_id', '?')),
        )
    console.print(t)
    console.print(f'  Total: {data.get("count", len(rpts))}')
    console.print()


def fmt_repeater_detail(data: dict):
    """Format `show repeater <id>` output."""
    if 'error' in data:
        console.print(f'  [red]{data["error"]}[/red]')
        return
    console.print()
    fields = [
        ('Repeater ID', 'repeater_id'), ('Callsign', 'callsign'),
        ('Connection Type', 'connection_type'), ('Status', 'status'),
        ('IP Address', 'ip'), ('RX Freq', 'rx_freq'), ('TX Freq', 'tx_freq'),
        ('Color Code', 'colorcode'), ('Location', 'location'),
        ('Latitude', 'latitude'), ('Longitude', 'longitude'),
        ('Height', 'height'), ('Software', 'software_id'),
        ('Package', 'package_id'), ('Description', 'description'),
        ('URL', 'url'), ('Pattern', 'matched_pattern'),
    ]
    for label, key in fields:
        val = data.get(key, '')
        if val or val == 0:
            console.print(f'  [bold]{label:<18}[/bold] {val}')
    console.print()


def fmt_streams(data: dict):
    """Format `show streams` output."""
    streams = data.get('streams', [])
    if not streams:
        console.print('  No active streams\n')
        return
    t = Table()
    t.add_column('Rpt ID', justify='right')
    t.add_column('Source', justify='right')
    t.add_column('Dest', justify='right')
    t.add_column('Slot')
    t.add_column('Type')
    t.add_column('Duration', justify='right')
    t.add_column('Pkts', justify='right')
    t.add_column('Origin')
    for s in streams:
        dur = s.get('duration', 0)
        t.add_row(
            str(s.get('repeater_id', '?')),
            str(s.get('rf_src', '?')),
            str(s.get('dst_id', '?')),
            str(s.get('slot', '?')),
            s.get('call_type', '?'),
            f'{dur:.1f}s',
            str(s.get('packet_count', 0)),
            s.get('source_node') or 'local',
        )
    console.print(t)
    console.print(f'  Active: {data.get("count", len(streams))}')
    console.print()


def fmt_outbounds(data: dict):
    """Format `show outbound` output."""
    obs = data.get('outbounds', [])
    if not obs:
        console.print('  No outbound connections\n')
        return
    t = Table()
    t.add_column('Name', style='cyan')
    t.add_column('Address')
    t.add_column('Port', justify='right')
    t.add_column('Radio ID', justify='right')
    t.add_column('Status')
    for o in obs:
        connected = o.get('connected', False)
        authed = o.get('authenticated', False)
        if connected and authed:
            st = 'connected'
        elif connected:
            st = 'authenticating'
        else:
            st = 'down'
        t.add_row(
            o.get('name', '?'),
            o.get('address', '?'),
            str(o.get('port', '?')),
            str(o.get('radio_id', '?')),
            Text(st, style=_status_style(st)),
        )
    console.print(t)
    console.print()


def fmt_last_heard(data: dict):
    """Format `show last-heard` output."""
    entries = data.get('last_heard', [])
    if not entries:
        console.print('  No recent activity\n')
        return
    t = Table()
    t.add_column('Radio ID', justify='right')
    t.add_column('Callsign', style='cyan')
    t.add_column('TG', justify='right')
    t.add_column('Slot')
    t.add_column('Repeater', justify='right')
    t.add_column('Last Heard')
    t.add_column('Node')
    for e in entries:
        t.add_row(
            str(e.get('radio_id', '?')),
            e.get('callsign', '-'),
            str(e.get('talkgroup', '?')),
            str(e.get('slot', '?')),
            str(e.get('repeater_id', '?')),
            _ago(e.get('last_heard', 0)),
            e.get('source_node') or e.get('node_id', 'local'),
        )
    console.print(t)
    console.print()


def fmt_events(data: dict):
    """Format `show events` output."""
    events = data.get('events', [])
    if not events:
        console.print('  No recent events\n')
        return
    t = Table()
    t.add_column('Time')
    t.add_column('Type', style='cyan')
    t.add_column('Details')
    for e in events:
        ts = _ts(e.get('timestamp', e.get('time', 0)))
        etype = e.get('type', '?')
        # Build summary from common fields
        parts = []
        for k in ('repeater_id', 'callsign', 'rf_src', 'dst_id', 'slot',
                   'connection_name', 'node_id', 'region_id', 'error'):
            if k in e:
                parts.append(f'{k}={e[k]}')
        t.add_row(ts, etype, ' '.join(parts[:5]))
    console.print(t)
    console.print()


def fmt_stats(data: dict):
    """Format `show stats` output."""
    console.print()
    # User cache stats
    uc = data.get('user_cache', {})
    if uc:
        console.print('  [bold]User Cache[/bold]')
        console.print(f'    Total entries:   {uc.get("total_entries", 0)}')
        console.print(f'    Valid entries:   {uc.get("valid_entries", 0)}')
        console.print(f'    Expired:        {uc.get("expired_entries", 0)}')
        console.print(f'    Timeout:        {uc.get("timeout_seconds", 0)}s')
    # General stats
    console.print(f'  [bold]Repeaters[/bold]      {data.get("repeaters", data.get("repeaters_connected", 0))}')
    console.print(f'  [bold]Outbounds[/bold]      {data.get("outbounds", 0)}')
    # Dashboard stats if present
    st = data.get('stats', {})
    if st:
        console.print(f'  [bold]Calls Today[/bold]    {st.get("total_calls_today", 0)}')
        console.print(f'  [bold]Duration Today[/bold] {st.get("total_duration_today", 0):.0f}s')
    console.print()


def fmt_version(data: dict):
    """Format `show version` output."""
    console.print()
    console.print(f'  [bold]{data.get("software", "DMR Nexus")}[/bold] v{data.get("version", "?")}')
    if data.get('node_id'):
        console.print(f'  Node ID:  {data["node_id"]}')
    if data.get('region'):
        console.print(f'  Region:   {data["region"]}')
    console.print()


def fmt_running_config(data: dict):
    """Format `show running-config` output."""
    cfg = data.get('config', {})
    if not cfg:
        console.print('  No config data')
        return
    console.print(Syntax(json.dumps(cfg, indent=2), 'json', theme='monokai'))
    console.print()


def fmt_ping(data: dict):
    """Format `ping` output."""
    if 'error' in data and not data.get('ok'):
        console.print(f'  [red]{data["error"]}[/red]')
        return
    nid = data.get('node_id', '?')
    alive = data.get('alive', False)
    lat = data.get('latency_ms', 0)
    st = 'alive' if alive else 'down'
    console.print(f'  {nid}: [{_status_style(st)}]{st}[/{_status_style(st)}]'
                  f'  latency=[{_latency_style(lat)}]{lat:.1f}ms[/{_latency_style(lat)}]'
                  f'  connected={data.get("connected", False)}')
    console.print()


def fmt_result(data: dict):
    """Format a generic command result (reload, drain, push-topology, etc.)."""
    if data.get('ok'):
        msg = data.get('message', 'OK')
        console.print(f'  [green]{msg}[/green]')
    else:
        console.print(f'  [red]Error: {data.get("error", "unknown")}[/red]')
    console.print()


# ── TG plan formatters ────────────────────────────


def fmt_tg_owners(owners: list):
    """Format owner list."""
    if not owners:
        console.print('  No owners registered')
        console.print()
        return
    t = Table(title='TG Plan Owners', show_lines=False)
    t.add_column('Callsign', style='cyan')
    t.add_column('Name')
    t.add_column('Created')
    for o in owners:
        created = str(o.get('created_at', ''))[:10] if o.get('created_at') else '-'
        t.add_row(o.get('callsign', ''), o.get('name', '') or '-', created)
    console.print(t)
    console.print()


def fmt_tg_repeaters(repeaters: list):
    """Format repeater list from TG plan."""
    if not repeaters:
        console.print('  No repeaters registered')
        console.print()
        return
    t = Table(title='TG Plan Repeaters', show_lines=False)
    t.add_column('Radio ID', style='cyan')
    t.add_column('Owner')
    t.add_column('Name')
    t.add_column('Created')
    for r in repeaters:
        created = str(r.get('created_at', ''))[:10] if r.get('created_at') else '-'
        t.add_row(str(r.get('radio_id', '')), r.get('owner_callsign', ''),
                  r.get('name', '') or '-', created)
    console.print(t)
    console.print()


def fmt_tg_assignments(data: dict):
    """Format TG assignment for a single repeater."""
    rid = data.get('radio_id', '?')
    s1 = data.get('slot1', [])
    s2 = data.get('slot2', [])
    console.print(f'  Repeater [cyan]{rid}[/cyan] TG assignments:')
    console.print(f'    Slot 1: {", ".join(str(t) for t in s1) if s1 else "(none)"}')
    console.print(f'    Slot 2: {", ".join(str(t) for t in s2) if s2 else "(none)"}')
    console.print()


def fmt_tg_who_has(tg_id: int, repeaters: list):
    """Format 'who has TG?' result."""
    if not repeaters:
        console.print(f'  No repeaters carry TG {tg_id}')
        console.print()
        return
    t = Table(title=f'Repeaters carrying TG {tg_id}', show_lines=False)
    t.add_column('Radio ID', style='cyan')
    t.add_column('Slot')
    t.add_column('Owner')
    t.add_column('Name')
    for r in repeaters:
        t.add_row(str(r.get('repeater_id', '')), str(r.get('slot', '')),
                  r.get('owner_callsign', ''), r.get('name', '') or '-')
    console.print(t)
    console.print()
