"""NexusShell — Cisco IOS-style interactive CLI for DMR Nexus.

Tab completion, ? help, command abbreviation, colored output.
"""
import cmd2
from cmd2 import Cmd2ArgumentParser, with_argparser

from . import formatters as fmt
from .datasource import DataSource
from .rest_client import RestClient


class NexusShell(cmd2.Cmd):
    """DMR Nexus interactive management shell."""

    def __init__(self, datasource: DataSource, tg_token: str = None):
        self.ds = datasource
        self._tg_token = tg_token
        super().__init__(
            allow_cli_args=False,
            persistent_history_file='~/.nexus_cli_history',
        )
        self.prompt = f'{datasource.prompt_label}# '
        self.intro = (f'\nDMR Nexus CLI [{datasource.mode_name} mode]'
                      f'\nConnected to: {datasource.prompt_label}'
                      f'\nType ? for help, <tab> for completions.\n')
        self.self_in_py = True
        # Hide cmd2 built-ins we don't need
        for c in ['alias', 'edit', 'macro', 'run_pyscript',
                  'run_script', 'shell', 'shortcuts', 'set', 'py']:
            if c not in self.hidden_commands:
                self.hidden_commands.append(c)

    def _check_error(self, data: dict) -> bool:
        """Print error and return True if data contains an error."""
        if 'error' in data and not data.get('ok'):
            fmt.fmt_result(data)
            return True
        return False

    # ════════════════════════════════════════════════════
    #  show — main read-only inspection command
    # ════════════════════════════════════════════════════

    show_parser = Cmd2ArgumentParser(description='Display system information')
    show_sub = show_parser.add_subparsers(dest='target', help='What to show')

    # show status
    show_sub.add_parser('status', help='Node/region summary')

    # show cluster [peers|topology]
    _p = show_sub.add_parser('cluster', help='Cluster peer states')
    _p.add_argument('detail', nargs='?', choices=['peers', 'topology'],
                    help='peers=detailed, topology=failover priority list')

    # show backbone [routes|latency|reelection]
    _p = show_sub.add_parser('backbone', help='Backbone gateway states')
    _p.add_argument('detail', nargs='?', choices=['routes', 'latency', 'reelection'],
                    help='routes=TG table, latency=analytics, reelection=suggestions')

    # show repeaters [detail]
    _p = show_sub.add_parser('repeaters', help='Connected repeaters')
    _p.add_argument('detail', nargs='?', choices=['detail'],
                    help='Show full metadata for each repeater')

    # show repeater <id>
    _p = show_sub.add_parser('repeater', help='Single repeater detail')
    _p.add_argument('id', type=int, help='Repeater ID (e.g. 311000)')

    # show streams
    show_sub.add_parser('streams', help='Active transmissions')

    # show outbound
    show_sub.add_parser('outbound', help='Outbound connections')

    # show last-heard
    _p = show_sub.add_parser('last-heard', help='Recent users')
    _p.add_argument('count', nargs='?', type=int, default=20, help='Number of entries')

    # show events
    _p = show_sub.add_parser('events', help='Recent events')
    _p.add_argument('count', nargs='?', type=int, default=50, help='Number of entries')

    # show stats
    show_sub.add_parser('stats', help='Call statistics and user cache')

    # show version
    show_sub.add_parser('version', help='Software version info')

    # show running-config
    show_sub.add_parser('running-config', help='Current configuration (secrets masked)')

    @with_argparser(show_parser)
    def do_show(self, args):
        """Display system information."""
        if not args.target:
            self.do_help('show')
            return

        handler = {
            'status': self._show_status,
            'cluster': self._show_cluster,
            'backbone': self._show_backbone,
            'repeaters': self._show_repeaters,
            'repeater': self._show_repeater,
            'streams': self._show_streams,
            'outbound': self._show_outbound,
            'last-heard': self._show_last_heard,
            'events': self._show_events,
            'stats': self._show_stats,
            'version': self._show_version,
            'running-config': self._show_running_config,
        }.get(args.target)

        if handler:
            handler(args)
        else:
            self.do_help('show')

    def _show_status(self, args):
        data = self.ds.get_status()
        if self._check_error(data):
            return
        if self.ds.mode_name == 'regional':
            fmt.fmt_status_regional(data)
        else:
            fmt.fmt_status(data)

    def _show_cluster(self, args):
        detail = getattr(args, 'detail', None)
        if detail == 'topology':
            data = self.ds.get_topology()
            if self._check_error(data):
                return
            fmt.fmt_cluster_topology(data)
        else:
            data = self.ds.get_cluster()
            if self._check_error(data):
                return
            fmt.fmt_cluster(data)

    def _show_backbone(self, args):
        data = self.ds.get_backbone()
        if self._check_error(data):
            return
        detail = getattr(args, 'detail', None)
        if detail == 'routes':
            routes = self.ds.get_tg_routes()
            if not self._check_error(routes):
                fmt.fmt_backbone_routes(routes)
        elif detail == 'reelection':
            # Just show the suggestions part
            fmt.fmt_backbone(data)
        else:
            fmt.fmt_backbone(data)

    def _show_repeaters(self, args):
        data = self.ds.get_repeaters()
        if self._check_error(data):
            return
        fmt.fmt_repeaters(data)

    def _show_repeater(self, args):
        # For detailed single-repeater info, try the datasource
        data = self.ds.get_repeaters()
        if self._check_error(data):
            return
        rpt = None
        for r in data.get('repeaters', []):
            if r.get('repeater_id') == args.id:
                rpt = r
                break
        if rpt:
            fmt.fmt_repeater_detail(rpt)
        else:
            self.perror(f'Repeater {args.id} not found')

    def _show_streams(self, args):
        data = self.ds.get_streams()
        if self._check_error(data):
            return
        fmt.fmt_streams(data)

    def _show_outbound(self, args):
        data = self.ds.get_outbounds()
        if self._check_error(data):
            return
        fmt.fmt_outbounds(data)

    def _show_last_heard(self, args):
        data = self.ds.get_last_heard(args.count)
        if self._check_error(data):
            return
        fmt.fmt_last_heard(data)

    def _show_events(self, args):
        data = self.ds.get_events(args.count)
        if self._check_error(data):
            return
        fmt.fmt_events(data)

    def _show_stats(self, args):
        data = self.ds.get_stats()
        if self._check_error(data):
            return
        fmt.fmt_stats(data)

    def _show_version(self, args):
        data = self.ds.get_version()
        if self._check_error(data):
            return
        fmt.fmt_version(data)

    def _show_running_config(self, args):
        data = self.ds.get_running_config()
        if self._check_error(data):
            return
        fmt.fmt_running_config(data)

    # ════════════════════════════════════════════════════
    #  push — operational push commands
    # ════════════════════════════════════════════════════

    push_parser = Cmd2ArgumentParser(description='Push data to clients')
    push_sub = push_parser.add_subparsers(dest='target', help='What to push')
    push_sub.add_parser('topology', help='Force topology push to all repeaters/clients')

    @with_argparser(push_parser)
    def do_push(self, args):
        """Push data to connected clients."""
        if args.target == 'topology':
            data = self.ds.do_push_topology()
            fmt.fmt_result(data)
        else:
            self.do_help('push')

    # ════════════════════════════════════════════════════
    #  reload — hot-reload config
    # ════════════════════════════════════════════════════

    def do_reload(self, _):
        """Hot-reload configuration from disk."""
        data = self.ds.do_reload()
        fmt.fmt_result(data)

    # ════════════════════════════════════════════════════
    #  drain — initiate graceful shutdown
    # ════════════════════════════════════════════════════

    def do_drain(self, _):
        """Initiate graceful shutdown (drain repeaters to other servers)."""
        data = self.ds.do_drain()
        fmt.fmt_result(data)

    # ════════════════════════════════════════════════════
    #  accept-reelection — swap gateway priorities
    # ════════════════════════════════════════════════════

    reelect_parser = Cmd2ArgumentParser(description='Accept gateway re-election')
    reelect_parser.add_argument('region_id', help='Region ID to re-elect')

    @with_argparser(reelect_parser)
    def do_accept_reelection(self, args):
        """Accept a gateway re-election for a region."""
        data = self.ds.do_accept_reelection(args.region_id)
        if data.get('ok'):
            fmt.console.print(f'  [green]Re-election accepted:[/green] {data.get("new_primary")} '
                              f'is now primary for {data.get("region_id")} '
                              f'(was: {data.get("old_primary")})')
        else:
            fmt.fmt_result(data)

    # ════════════════════════════════════════════════════
    #  ping — check cluster peer connectivity
    # ════════════════════════════════════════════════════

    ping_parser = Cmd2ArgumentParser(description='Check cluster peer connectivity')
    ping_parser.add_argument('node_id', help='Cluster peer node ID')

    @with_argparser(ping_parser)
    def do_ping(self, args):
        """Check connectivity to a cluster peer."""
        data = self.ds.ping_peer(args.node_id)
        fmt.fmt_ping(data)

    # ════════════════════════════════════════════════════
    #  configure — stub for future config mode
    # ════════════════════════════════════════════════════

    configure_parser = Cmd2ArgumentParser(description='Enter configuration mode')
    configure_parser.add_argument('mode', nargs='?', choices=['terminal'],
                                  default='terminal', help='Configuration mode')

    @with_argparser(configure_parser)
    def do_configure(self, args):
        """Enter configuration mode (not yet implemented)."""
        if not self.ds.supports_writes:
            self.perror('Configuration mode only available in local mode')
            return
        self.poutput('% Configuration mode not yet implemented')
        self.poutput('% Use "reload" to apply config file changes')

    # ════════════════════════════════════════════════════
    #  tg-plan — centralized talkgroup management
    # ════════════════════════════════════════════════════

    def _tg_headers(self):
        """Authorization headers for TG plan API."""
        if not self._tg_token:
            return None
        return {'Authorization': f'Bearer {self._tg_token}'}

    def _tg_client(self) -> RestClient:
        """Get REST client pointing at the dashboard."""
        if hasattr(self.ds, '_client'):
            return self.ds._client  # RegionalDataSource
        # Local mode — assume dashboard on localhost:8080
        return RestClient('http://127.0.0.1:8080')

    def _tg_check_token(self) -> bool:
        if not self._tg_token:
            self.perror('No TG plan token — use --token or set NEXUS_TG_TOKEN')
            return False
        return True

    tg_parser = Cmd2ArgumentParser(description='TG plan management')
    tg_sub = tg_parser.add_subparsers(dest='action', help='TG plan action')

    # tg-plan health
    tg_sub.add_parser('health', help='Check PG connection health')

    # tg-plan owners
    tg_sub.add_parser('owners', help='List owners (admin)')

    # tg-plan add-owner <callsign> [name]
    _p = tg_sub.add_parser('add-owner', help='Create owner (admin)')
    _p.add_argument('callsign', help='Owner callsign')
    _p.add_argument('name', nargs='?', help='Owner name')

    # tg-plan del-owner <callsign>
    _p = tg_sub.add_parser('del-owner', help='Delete owner (admin)')
    _p.add_argument('callsign', help='Owner callsign')

    # tg-plan rotate-token <callsign>
    _p = tg_sub.add_parser('rotate-token', help='Rotate owner token (admin)')
    _p.add_argument('callsign', help='Owner callsign')

    # tg-plan repeaters
    tg_sub.add_parser('repeaters', help='List repeaters')

    # tg-plan add-repeater <radio_id> [name]
    _p = tg_sub.add_parser('add-repeater', help='Register repeater')
    _p.add_argument('radio_id', type=int, help='Repeater radio ID')
    _p.add_argument('name', nargs='?', help='Friendly name')

    # tg-plan del-repeater <radio_id>
    _p = tg_sub.add_parser('del-repeater', help='Unregister repeater')
    _p.add_argument('radio_id', type=int, help='Repeater radio ID')

    # tg-plan show <radio_id>
    _p = tg_sub.add_parser('show', help='Show TG assignments for repeater')
    _p.add_argument('radio_id', type=int, help='Repeater radio ID')

    # tg-plan set <radio_id> <slot> <tg_ids...>
    _p = tg_sub.add_parser('set', help='Set TGs for repeater slot')
    _p.add_argument('radio_id', type=int, help='Repeater radio ID')
    _p.add_argument('slot', type=int, choices=[1, 2], help='Slot number')
    _p.add_argument('tg_ids', type=int, nargs='+', help='Talkgroup IDs')

    # tg-plan bulk <slot> <tg_ids> --repeaters <ids>
    _p = tg_sub.add_parser('bulk', help='Bulk-assign TGs to multiple repeaters')
    _p.add_argument('slot', type=int, choices=[1, 2], help='Slot number')
    _p.add_argument('tg_ids', type=int, nargs='+', help='Talkgroup IDs')
    _p.add_argument('--repeaters', type=int, nargs='+', required=True,
                    help='Repeater radio IDs')

    # tg-plan who-has <tg_id>
    _p = tg_sub.add_parser('who-has', help='Which repeaters carry a TG?')
    _p.add_argument('tg_id', type=int, help='Talkgroup ID')

    @with_argparser(tg_parser)
    def do_tg_plan(self, args):
        """Manage centralized talkgroup assignments."""
        if not args.action:
            self.do_help('tg_plan')
            return

        handler = {
            'health': self._tg_health,
            'owners': self._tg_owners,
            'add-owner': self._tg_add_owner,
            'del-owner': self._tg_del_owner,
            'rotate-token': self._tg_rotate_token,
            'repeaters': self._tg_repeaters,
            'add-repeater': self._tg_add_repeater,
            'del-repeater': self._tg_del_repeater,
            'show': self._tg_show,
            'set': self._tg_set,
            'bulk': self._tg_bulk,
            'who-has': self._tg_who_has,
        }.get(args.action)

        if handler:
            handler(args)
        else:
            self.do_help('tg_plan')

    def _tg_health(self, args):
        c = self._tg_client()
        data = c.get('/api/tg-plan/health')
        if data.get('ok'):
            fmt.console.print(f'  [green]PG connected[/green]  latency: {data.get("pg_latency_ms")}ms')
        else:
            fmt.console.print(f'  [red]PG disconnected[/red]  {data.get("error", "")}')

    def _tg_owners(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.get('/api/tg-plan/owners', headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.fmt_tg_owners(data.get('owners', []))

    def _tg_add_owner(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.post('/api/tg-plan/owners',
                       json_data={'callsign': args.callsign, 'name': args.name},
                       headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.console.print(f'  [green]Created owner[/green] {data.get("callsign")}')
        fmt.console.print(f'  [yellow]API token (save this!):[/yellow] {data.get("api_token")}')

    def _tg_del_owner(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.delete(f'/api/tg-plan/owners/{args.callsign}',
                         headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.console.print(f'  [green]Deleted owner[/green] {data.get("deleted")}')

    def _tg_rotate_token(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.post(f'/api/tg-plan/owners/{args.callsign}/rotate-token',
                       headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.console.print(f'  [green]Token rotated for[/green] {data.get("callsign")}')
        fmt.console.print(f'  [yellow]New token (save this!):[/yellow] {data.get("api_token")}')

    def _tg_repeaters(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.get('/api/tg-plan/repeaters', headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.fmt_tg_repeaters(data.get('repeaters', []))

    def _tg_add_repeater(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.post('/api/tg-plan/repeaters',
                       json_data={'radio_id': args.radio_id, 'name': args.name},
                       headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.console.print(f'  [green]Registered repeater[/green] {data.get("radio_id")}')

    def _tg_del_repeater(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.delete(f'/api/tg-plan/repeaters/{args.radio_id}',
                         headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.console.print(f'  [green]Unregistered repeater[/green] {data.get("deleted")}')

    def _tg_show(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.get(f'/api/tg-plan/repeaters/{args.radio_id}/talkgroups',
                      headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.fmt_tg_assignments(data)

    def _tg_set(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        body = {}
        if args.slot == 1:
            body['slot1'] = args.tg_ids
        else:
            body['slot2'] = args.tg_ids
        data = c.put(f'/api/tg-plan/repeaters/{args.radio_id}/talkgroups',
                      json_data=body, headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.fmt_tg_assignments(data)

    def _tg_bulk(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.post('/api/tg-plan/bulk-assign',
                       json_data={
                           'repeater_ids': args.repeaters,
                           'slot': args.slot,
                           'talkgroup_ids': args.tg_ids,
                       },
                       headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.console.print(f'  [green]Bulk assigned[/green] {data.get("affected_repeaters")} repeater(s)')

    def _tg_who_has(self, args):
        if not self._tg_check_token():
            return
        c = self._tg_client()
        data = c.get(f'/api/tg-plan/talkgroups/{args.tg_id}/repeaters',
                      headers=self._tg_headers())
        if self._check_error(data):
            return
        fmt.fmt_tg_who_has(args.tg_id, data.get('repeaters', []))

    # ════════════════════════════════════════════════════
    #  Aliases for convenience
    # ════════════════════════════════════════════════════

    def do_exit(self, _):
        """Exit the CLI."""
        return True

    do_quit = do_exit
    do_end = do_exit
