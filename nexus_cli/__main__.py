"""Entry point for `python3 -m nexus_cli`."""
import argparse
import os
import sys


def main():
    parser = argparse.ArgumentParser(
        prog='nexus-cli',
        description='DMR Nexus CLI — Cisco-style interactive management shell',
    )
    parser.add_argument('--socket', default='/run/dmr-nexus/mgmt.sock',
                        help='Management socket path (local mode)')
    parser.add_argument('--node', help='Node name override (for prompt)')
    parser.add_argument('--host', help='Regional dashboard host (enables regional mode)')
    parser.add_argument('--port', type=int, default=8080,
                        help='Regional dashboard port (default: 8080)')
    parser.add_argument('--region', help='Region name (for prompt, used with --host)')
    parser.add_argument('--token', help='TG plan API token (or set NEXUS_TG_TOKEN env var)')
    parser.add_argument('-c', '--command', help='Execute single command and exit')
    args = parser.parse_args()

    # TG plan token: --token flag > env var
    tg_token = args.token or os.environ.get('NEXUS_TG_TOKEN')

    # Determine mode and create datasource
    if args.host:
        from .datasource import RegionalDataSource
        base_url = f'http://{args.host}:{args.port}'
        ds = RegionalDataSource(base_url, args.region or 'region')
    else:
        from .datasource import LocalDataSource
        ds = LocalDataSource(args.socket, args.node)

    from .shell import NexusShell
    shell = NexusShell(ds, tg_token=tg_token)

    if args.command:
        # One-shot mode: run command and exit
        shell.onecmd(args.command)
    else:
        try:
            shell.cmdloop()
        except KeyboardInterrupt:
            print()


if __name__ == '__main__':
    main()
