#!/usr/bin/env python3
"""HBlink4 management CLI — talks to the management Unix socket."""
import json
import socket
import sys

SOCKET_PATH = '/tmp/hblink4_mgmt.sock'

def send_command(command: str, socket_path: str = SOCKET_PATH) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
        sock.sendall(json.dumps({'command': command}).encode() + b'\n')
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
        print('Commands: status, cluster, repeaters, reload, drain')
        sys.exit(1)

    command = sys.argv[1].lower()
    try:
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
