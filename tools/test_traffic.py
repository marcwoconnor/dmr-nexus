#!/usr/bin/env python3
"""
DMR Nexus Cross-Cluster Traffic Test

Connects ephemeral repeaters to both cluster nodes, sends traffic in each
direction, and verifies the remote node's repeater receives the forwarded
DMRD packets.  Exit 0 = all tests pass, exit 1 = failure.

Usage:
    python3 tools/test_traffic.py
    python3 tools/test_traffic.py --node1 10.31.11.40 --node2 10.31.11.41
"""

import argparse
import asyncio
import logging
import os
import sys

# Import protocol helpers from test_repeater (same directory)
sys.path.insert(0, os.path.dirname(__file__))
from test_repeater import RepeaterProtocol, build_dmrd_packet  # noqa: E402

log = logging.getLogger('test_traffic')


async def send_stream(protocol, radio_id, talkgroup, slot, num_packets=15):
    """Send a voice stream and return the stream_id used."""
    stream_id = os.urandom(4)
    for seq in range(num_packets):
        pkt = build_dmrd_packet(
            seq=seq,
            src_id=radio_id,
            dst_id=talkgroup,
            repeater_id=protocol.repeater_id,
            slot=slot,
            stream_id=stream_id,
            is_terminator=(seq == num_packets - 1),
        )
        protocol.transport.sendto(pkt)
        await asyncio.sleep(0.060)
    return stream_id


async def run_tests(args):
    loop = asyncio.get_event_loop()
    results = []
    options = 'TS1=1,2,8,9,3100,3120;TS2=1,2,8,9,3100,3120'

    # Connect repeater A to node 1
    evt_a = asyncio.Event()
    transport_a, proto_a = await loop.create_datagram_endpoint(
        lambda: RepeaterProtocol(
            repeater_id=312000, callsign='TEST-A',
            passphrase=args.passphrase, options_str=options,
            server_addr=(args.node1, args.port), on_connected=evt_a,
        ),
        remote_addr=(args.node1, args.port),
    )

    # Connect repeater B to node 2
    evt_b = asyncio.Event()
    transport_b, proto_b = await loop.create_datagram_endpoint(
        lambda: RepeaterProtocol(
            repeater_id=312001, callsign='TEST-B',
            passphrase=args.passphrase, options_str=options,
            server_addr=(args.node2, args.port), on_connected=evt_b,
        ),
        remote_addr=(args.node2, args.port),
    )

    # Wait for both connections
    try:
        await asyncio.wait_for(
            asyncio.gather(evt_a.wait(), evt_b.wait()), timeout=10)
    except asyncio.TimeoutError:
        a_state = proto_a.state
        b_state = proto_b.state
        print(f'FAIL: connection timeout (A={a_state}, B={b_state})')
        transport_a.close()
        transport_b.close()
        return False

    print(f'Connected: TEST-A -> {args.node1}, TEST-B -> {args.node2}')

    # Test 1: Node1 → Node2
    print('\nTest 1: Node1 → Node2 (TG 1 TS1)...')
    sid1 = await send_stream(proto_a, radio_id=3120001, talkgroup=1, slot=1)
    await asyncio.sleep(1.5)
    if sid1 in proto_b._rx_streams:
        print(f'  PASS  stream {sid1.hex()} received on Node2')
        results.append(True)
    else:
        print(f'  FAIL  stream {sid1.hex()} NOT received on Node2')
        results.append(False)

    # Test 2: Node2 → Node1
    print('\nTest 2: Node2 → Node1 (TG 3100 TS2)...')
    sid2 = await send_stream(proto_b, radio_id=3120002, talkgroup=3100, slot=2)
    await asyncio.sleep(1.5)
    if sid2 in proto_a._rx_streams:
        print(f'  PASS  stream {sid2.hex()} received on Node1')
        results.append(True)
    else:
        print(f'  FAIL  stream {sid2.hex()} NOT received on Node1')
        results.append(False)

    # Cleanup
    proto_a.disconnect()
    proto_b.disconnect()
    await asyncio.sleep(0.2)
    transport_a.close()
    transport_b.close()

    passed = all(results)
    print(f'\n{"ALL TESTS PASSED" if passed else "SOME TESTS FAILED"}')
    return passed


def main():
    p = argparse.ArgumentParser(description='DMR Nexus cross-cluster traffic test')
    p.add_argument('--node1', default='10.31.11.40', help='Node 1 address')
    p.add_argument('--node2', default='10.31.11.41', help='Node 2 address')
    p.add_argument('--port', type=int, default=62031, help='Server port')
    p.add_argument('--passphrase', default='passw0rd', help='Auth passphrase')
    p.add_argument('--log-level', default='WARNING', help='Logging level')
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format='%(asctime)s %(levelname)-5s %(message)s',
        datefmt='%H:%M:%S',
    )

    passed = asyncio.run(run_tests(args))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
