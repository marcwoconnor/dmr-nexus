"""Unix socket client for HBlink4 management interface."""
import json
import socket

DEFAULT_SOCKET = '/run/dmr-nexus/mgmt.sock'


def send_command(command: str, socket_path: str = DEFAULT_SOCKET, **kwargs) -> dict:
    """Send a command to the management socket and return the JSON response."""
    cmd = {'command': command}
    cmd.update(kwargs)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(5.0)
        sock.connect(socket_path)
        sock.sendall(json.dumps(cmd).encode() + b'\n')
        data = b''
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if b'\n' in data:
                break
        return json.loads(data.decode().strip())
    finally:
        sock.close()
