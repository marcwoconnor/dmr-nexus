#!/usr/bin/env python3
"""
DMR Nexus Test Repeater Simulator

Simulates one or more HomeBrew protocol repeaters connecting to DMR Nexus
servers and generating synthetic DMR voice traffic. Useful for testing
clustering, stream routing, dashboard visualization, and failover.

Usage:
    python3 tools/test_repeater.py -s 10.31.11.40 -n 2
    python3 tools/test_repeater.py -s 10.31.11.40 -n 3 --targets '{"1":"10.31.11.41:62031"}'
    python3 tools/test_repeater.py -s 10.31.11.40 --no-traffic  # connect only
"""

import argparse
import asyncio
import json
import logging
import os
import random
import signal
import sys
from hashlib import sha256
from time import time

# -- Protocol constants (from hblink4/constants.py) --
RPTL    = b'RPTL'
RPTK    = b'RPTK'
RPTC    = b'RPTC'
RPTO    = b'RPTO'
RPTCL   = b'RPTCL'
RPTPING = b'RPTPING'
RPTACK  = b'RPTACK'
MSTPONG = b'MSTPONG'
MSTNAK  = b'MSTNAK'
MSTCL   = b'MSTCL'
DMRD    = b'DMRD'

log = logging.getLogger('test_repeater')


def build_dmrd_packet(seq, src_id, dst_id, repeater_id, slot, stream_id,
                      call_type=0, is_terminator=False):
    """Build a 55-byte DMRD voice packet.

    Args:
        seq: Sequence number (0-255)
        src_id: Source radio ID (int)
        dst_id: Destination talkgroup or radio ID (int)
        repeater_id: Repeater ID (int)
        slot: Timeslot (1 or 2)
        stream_id: 4-byte stream identifier
        call_type: 0=group, 1=private
        is_terminator: True for stream terminator packet
    """
    pkt = bytearray(55)
    pkt[0:4] = DMRD
    pkt[4] = seq & 0xFF
    pkt[5:8] = src_id.to_bytes(3, 'big')
    pkt[8:11] = dst_id.to_bytes(3, 'big')
    pkt[11:15] = repeater_id.to_bytes(4, 'big')

    # Bits field: bit7=slot, bit6=call_type, bits5-4=frame_type, bits3-0=dtype
    bits = 0
    if slot == 2:
        bits |= 0x80
    if call_type:
        bits |= 0x40
    if is_terminator:
        bits |= 0x22  # frame_type=2 (data sync) + dtype=2 (voice term)
    else:
        bits |= 0x10  # frame_type=1 (voice sync)
    pkt[15] = bits

    pkt[16:20] = stream_id
    # bytes 20-52: DMR payload (zeros -- server doesn't decode codec)
    return bytes(pkt)


def build_config_packet(repeater_id, callsign, software_id='DMRNexus-Sim',
                        package_id='test_repeater v1'):
    """Build a 302-byte RPTC configuration packet."""
    rid = repeater_id.to_bytes(4, 'big')
    pkt = RPTC + rid
    pkt += callsign.encode().ljust(8, b'\x00')[:8]
    pkt += b'449000000'.ljust(9, b'\x00')[:9]     # rx_freq
    pkt += b'444000000'.ljust(9, b'\x00')[:9]     # tx_freq
    pkt += b'50'.ljust(2, b'\x00')[:2]            # power
    pkt += b'1\x00'                                # colorcode
    pkt += b'38.00000'.ljust(8, b'\x00')[:8]      # latitude
    pkt += b'-97.00000'.ljust(9, b'\x00')[:9]     # longitude
    pkt += b'100'.ljust(3, b'\x00')[:3]           # height
    pkt += b'Test Simulator'.ljust(20, b'\x00')[:20]  # location
    pkt += callsign.encode().ljust(19, b'\x00')[:19]  # description
    pkt += b'3'                                    # slots (both)
    pkt += b''.ljust(124, b'\x00')[:124]          # url
    pkt += software_id.encode().ljust(40, b'\x00')[:40]
    pkt += package_id.encode().ljust(40, b'\x00')[:40]
    return pkt


class RepeaterProtocol(asyncio.DatagramProtocol):
    """Simulates a single HomeBrew repeater client."""

    def __init__(self, repeater_id, callsign, passphrase, options_str,
                 server_addr, on_connected):
        self.repeater_id = repeater_id
        self.rid_bytes = repeater_id.to_bytes(4, 'big')
        self.callsign = callsign
        self.passphrase = passphrase
        self.options_str = options_str
        self.server_addr = server_addr
        self.on_connected = on_connected

        self.transport = None
        self.state = 'disconnected'
        self.auth_sent = False
        self.config_sent = False
        self.options_sent = False
        self.last_pong = 0
        self.missed_pongs = 0

        self._prefix = f'[{callsign}/{repeater_id}]'
        self._rx_streams = {}  # track received stream IDs for logging

    def connection_made(self, transport):
        self.transport = transport
        self.state = 'login'
        self.auth_sent = False
        self.config_sent = False
        self.options_sent = False
        # Send RPTL
        self.transport.sendto(RPTL + self.rid_bytes)
        log.info(f'{self._prefix} RPTL sent -> {self.server_addr[0]}:{self.server_addr[1]}')

    def datagram_received(self, data, addr):
        if len(data) < 6:
            return

        # Identify command
        if data[:7] == MSTPONG:
            self.last_pong = time()
            self.missed_pongs = 0
            log.debug(f'{self._prefix} PONG')
            return

        if data[:6] == MSTNAK:
            log.error(f'{self._prefix} MSTNAK — rejected by server')
            self.state = 'disconnected'
            return

        if data[:5] == MSTCL:
            log.warning(f'{self._prefix} MSTCL — server disconnect')
            self.state = 'disconnected'
            return

        if data[:4] == DMRD and len(data) >= 55:
            self._handle_rx_dmrd(data)
            return

        if data[:6] == RPTACK:
            self._handle_ack(data)
            return

    def _handle_ack(self, data):
        """Process RPTACK at each handshake stage."""
        if not self.auth_sent:
            # First ACK contains salt
            if len(data) < 10:
                log.error(f'{self._prefix} Invalid RPTACK (too short for salt)')
                return
            salt = int.from_bytes(data[6:10], 'big')
            salt_bytes = salt.to_bytes(4, 'big')
            auth_hash = bytes.fromhex(
                sha256(salt_bytes + self.passphrase.encode()).hexdigest()
            )
            self.transport.sendto(RPTK + self.rid_bytes + auth_hash)
            self.auth_sent = True
            log.info(f'{self._prefix} RPTK sent (auth)')

        elif not self.config_sent:
            # Auth accepted, send config
            self.config_sent = True
            pkt = build_config_packet(self.repeater_id, self.callsign)
            self.transport.sendto(pkt)
            log.info(f'{self._prefix} RPTC sent (config)')

        elif not self.options_sent:
            # Config accepted, send options
            self.options_sent = True
            if self.options_str:
                opts_bytes = self.options_str.encode().ljust(300, b'\x00')[:300]
                self.transport.sendto(RPTO + self.rid_bytes + opts_bytes)
                log.info(f'{self._prefix} RPTO sent: {self.options_str}')
            else:
                self.state = 'connected'
                self.on_connected.set()
                log.info(f'{self._prefix} CONNECTED (no options)')

        else:
            # Options accepted
            self.state = 'connected'
            self.on_connected.set()
            log.info(f'{self._prefix} CONNECTED')

    def _handle_rx_dmrd(self, data):
        """Log DMRD packets received from the server (forwarded streams)."""
        stream_id = data[16:20]
        if stream_id not in self._rx_streams:
            src = int.from_bytes(data[5:8], 'big')
            dst = int.from_bytes(data[8:11], 'big')
            slot = 2 if (data[15] & 0x80) else 1
            self._rx_streams[stream_id] = time()
            log.info(f'{self._prefix} RX: {src} -> TG {dst} TS{slot}')
        # Clean old entries
        now = time()
        self._rx_streams = {k: v for k, v in self._rx_streams.items()
                            if now - v < 5}

    def error_received(self, exc):
        log.error(f'{self._prefix} UDP error: {exc}')

    def connection_lost(self, exc):
        log.warning(f'{self._prefix} Connection lost: {exc}')
        self.state = 'disconnected'

    def disconnect(self):
        """Send RPTCL for graceful disconnect."""
        if self.transport and self.state == 'connected':
            self.transport.sendto(RPTCL + self.rid_bytes)
            log.info(f'{self._prefix} RPTCL sent (disconnect)')


async def keepalive_loop(protocol, interval=5.0):
    """Send RPTPING every interval once connected."""
    await protocol.on_connected.wait()
    while protocol.state == 'connected':
        protocol.transport.sendto(RPTPING + protocol.rid_bytes)
        log.debug(f'{protocol._prefix} PING')
        await asyncio.sleep(interval)


async def generate_traffic(protocol, radio_id, talkgroups,
                           min_packets=20, max_packets=100,
                           min_interval=2.0, max_interval=30.0):
    """Generate random voice streams on a connected repeater."""
    await protocol.on_connected.wait()
    prefix = protocol._prefix

    while protocol.state == 'connected':
        tg = random.choice(talkgroups)
        slot = random.choice([1, 2])
        num_packets = random.randint(min_packets, max_packets)
        stream_id = os.urandom(4)
        duration_s = num_packets * 0.06

        log.info(f'{prefix} TX START: {radio_id} -> TG {tg} TS{slot} '
                 f'({num_packets} frames, ~{duration_s:.1f}s)')

        for seq in range(num_packets):
            pkt = build_dmrd_packet(
                seq=seq,
                src_id=radio_id,
                dst_id=tg,
                repeater_id=protocol.repeater_id,
                slot=slot,
                stream_id=stream_id,
                is_terminator=(seq == num_packets - 1),
            )
            protocol.transport.sendto(pkt)
            await asyncio.sleep(0.060)

        log.info(f'{prefix} TX END: stream={stream_id.hex()}')

        interval = random.uniform(min_interval, max_interval)
        await asyncio.sleep(interval)


def build_options_from_tgs(talkgroups):
    """Build RPTO options string from a list of talkgroup IDs."""
    tg_str = ','.join(str(t) for t in talkgroups)
    return f'TS1={tg_str};TS2={tg_str}'


def parse_targets(targets_str):
    """Parse --targets JSON: {"index": "host:port"} -> {int: (str, int)}"""
    raw = json.loads(targets_str)
    result = {}
    for idx, addr in raw.items():
        host, port = addr.rsplit(':', 1)
        result[int(idx)] = (host, int(port))
    return result


async def run(args):
    loop = asyncio.get_event_loop()
    talkgroups = [int(t) for t in args.talkgroups.split(',')]
    targets = parse_targets(args.targets) if args.targets else {}
    options_str = args.options or build_options_from_tgs(talkgroups)

    protocols = []
    tasks = []

    for i in range(args.count):
        repeater_id = args.base_id + i
        radio_id = args.base_radio + i
        callsign = f'{args.callsign_prefix}{i + 1:02d}'
        server, port = targets.get(i, (args.server, args.port))

        connected = asyncio.Event()

        transport, protocol = await loop.create_datagram_endpoint(
            lambda rid=repeater_id, cs=callsign, evt=connected: RepeaterProtocol(
                repeater_id=rid,
                callsign=cs,
                passphrase=args.passphrase,
                options_str=options_str,
                server_addr=(server, port),
                on_connected=evt,
            ),
            remote_addr=(server, port),
        )

        protocols.append(protocol)
        tasks.append(asyncio.ensure_future(keepalive_loop(protocol)))

        if not args.no_traffic:
            tasks.append(asyncio.ensure_future(generate_traffic(
                protocol, radio_id, talkgroups,
                min_packets=args.min_packets,
                max_packets=args.max_packets,
                min_interval=args.min_interval,
                max_interval=args.max_interval,
            )))

        log.info(f'Repeater {callsign} (ID {repeater_id}, radio {radio_id}) '
                 f'-> {server}:{port}')

    # Handle shutdown
    stop = asyncio.Event()

    def _signal_handler():
        log.info('Shutting down...')
        for p in protocols:
            p.disconnect()
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop.wait()
    # Brief delay for disconnect packets to send
    await asyncio.sleep(0.2)
    for t in tasks:
        t.cancel()


def parse_args():
    p = argparse.ArgumentParser(
        description='DMR Nexus Test Repeater Simulator',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  %(prog)s -s 10.31.11.40 -n 2\n'
               '  %(prog)s -s 10.31.11.40 -n 3 --targets \'{"1":"10.31.11.41:62031"}\'\n'
               '  %(prog)s -s 10.31.11.40 --no-traffic\n',
    )
    p.add_argument('-s', '--server', default='127.0.0.1', help='Server address (default: 127.0.0.1)')
    p.add_argument('-p', '--port', type=int, default=62031, help='Server port (default: 62031)')
    p.add_argument('-n', '--count', type=int, default=1, help='Number of simulated repeaters (default: 1)')
    p.add_argument('--base-id', type=int, default=311000, help='Starting repeater ID (default: 311000)')
    p.add_argument('--base-radio', type=int, default=3110001, help='Starting radio ID (default: 3110001)')
    p.add_argument('--passphrase', default='passw0rd', help='Auth passphrase (default: passw0rd)')
    p.add_argument('--callsign-prefix', default='SIM', help='Callsign prefix (default: SIM)')
    p.add_argument('--talkgroups', default='1,2,8,9,3100,3120', help='Comma-separated TG list (default: 1,2,8,9,3100,3120)')
    p.add_argument('--options', default=None, help='RPTO options string (default: auto from --talkgroups)')
    p.add_argument('--min-packets', type=int, default=20, help='Min voice frames per stream (default: 20, ~1.2s)')
    p.add_argument('--max-packets', type=int, default=100, help='Max voice frames per stream (default: 100, ~6s)')
    p.add_argument('--min-interval', type=float, default=2.0, help='Min seconds between TX (default: 2.0)')
    p.add_argument('--max-interval', type=float, default=30.0, help='Max seconds between TX (default: 30.0)')
    p.add_argument('--no-traffic', action='store_true', help='Connect only, no synthetic traffic')
    p.add_argument('--targets', default=None, help='JSON mapping repeater index to server:port')
    p.add_argument('--log-level', default='INFO', help='Logging level (default: INFO)')
    return p.parse_args()


def main():
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s %(levelname)-5s %(message)s',
        datefmt='%H:%M:%S',
    )
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
