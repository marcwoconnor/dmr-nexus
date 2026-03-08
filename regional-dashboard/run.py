#!/usr/bin/env python3
"""Regional Dashboard launcher for DMR Nexus"""

import sys
import json
import uvicorn
from pathlib import Path

if __name__ == '__main__':
    # Load config for listen settings
    config_path = Path(__file__).parent / "config.json"
    host = '0.0.0.0'
    port = 8080

    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
            host = cfg.get('listen_host', host)
            port = cfg.get('listen_port', port)

    # CLI overrides
    if len(sys.argv) > 1:
        host = sys.argv[1]
    if len(sys.argv) > 2:
        port = int(sys.argv[2])

    print(f"Starting DMR Nexus Regional Dashboard on http://{host}:{port}")
    print("Press CTRL+C to stop")
    print()

    uvicorn.run(
        "server:app",
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        loop="asyncio"
    )
