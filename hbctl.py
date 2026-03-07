#!/usr/bin/env python3
"""HBlink4 management CLI — talks to the management Unix socket."""
import json
import socket
import sys

SOCKET_PATH = '/run/dmr-nexus/mgmt.sock'

def send_command(command: str, socket_path: str = SOCKET_PATH) -> dict:
    return send_command_data({'command': command}, socket_path)

def send_command_data(cmd: dict, socket_path: str = SOCKET_PATH) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        sock.sendall(json.dumps(cmd).encode() + b'\n')
        data = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break
        return json.loads(data.decode().strip())
    finally:
        sock.close()


def main():
    if len(sys.argv) < 2:
        print('Usage: hbctl.py <command>')
        print('Commands: status, cluster, backbone, repeaters, reload, drain, accept-reelection')
        sys.exit(1)

    command = sys.argv[1].lower()
    try:
        if command == 'accept-reelection':
            if len(sys.argv) < 3:
                print('Usage: hbctl.py accept-reelection <region_id>')
                sys.exit(1)
            result = send_command_data({
                'command': 'accept-reelection',
                'region_id': sys.argv[2],
            })
        else:
            result = send_command(command)
    except (ConnectionRefusedError, FileNotFoundError):
        print(f'Error: cannot connect to {SOCKET_PATH} — is HBlink4 running?')
        sys.exit(1)

    if command == 'status':
        print(f"Node:     {result.get('node_id', 'N/A')}")
        print(f"Repeaters: {result.get('repeaters', 0)}")
        print(f"Outbounds: {result.get('outbounds', 0)}")
        print(f"Cluster:   {result.get('cluster_peers', 0)} peer(s)")
        print(f"Draining:  {result.get('draining', False)}")

    elif command == 'cluster':
        if not result.get('enabled'):
            print('Clustering not enabled')
            return
        print(f"Local: {result.get('local_node_id')}")
        for p in result.get('peers', []):
            status = 'alive' if p['alive'] else ('connected' if p['connected'] else 'down')
            drain = ' [DRAINING]' if p.get('draining') else ''
            print(f"  {p['node_id']}: {status}, {p['latency_ms']:.1f}ms, "
                  f"{p['repeater_count']} rpt(s){drain}")

    elif command == 'backbone':
        if not result.get('enabled'):
            print('Backbone not enabled')
            return
        print(f"Region: {result.get('local_region_id')}")
        for p in result.get('peers', []):
            status = 'alive' if p['alive'] else ('connected' if p['connected'] else 'down')
            stats = p.get('latency_stats', {})
            print(f"  {p['node_id']} [{p['region_id']}] pri={p['priority']}: {status}, "
                  f"avg={stats.get('avg_ms', 0):.1f}ms, "
                  f"cur={stats.get('current_ms', 0):.1f}ms, "
                  f"jitter={stats.get('jitter_ms', 0):.1f}ms, "
                  f"misses={stats.get('consecutive_misses', 0)}")
        for s in result.get('suggestions', []):
            tag = 'AUTO-SWITCH' if s['level'] == 'auto' else 'SUGGEST'
            print(f"\n  ** {tag}: {s['region_id']} — promote {s['suggested_primary']} "
                  f"(reason: {s['reason']})")
            print(f"     Accept: hbctl.py accept-reelection {s['region_id']}")

    elif command == 'accept-reelection':
        if result.get('ok'):
            print(f"Re-election accepted: {result.get('new_primary')} is now primary "
                  f"for {result.get('region_id')} (was: {result.get('old_primary')})")
        else:
            print(f"Error: {result.get('error')}")

    elif command == 'repeaters':
        for r in result.get('repeaters', []):
            print(f"  {r['repeater_id']:>7} {r['callsign']:<10} {r['connection_type']:<10} [{r['node']}]")
        print(f"Total: {result.get('count', 0)}")

    elif command in ('reload', 'drain'):
        print(json.dumps(result, indent=2))

    else:
        print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
