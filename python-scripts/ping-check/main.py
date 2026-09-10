#!/usr/bin/env python3
"""Network-level reachability check from the gateway host itself.

Runs OS `ping` against a target IP and parses packet loss / RTT. This is
deliberately NOT a device CLI command (no MOP template can do this) -- it
validates that a change is reachable over the network path from the
gateway, independent of what the device itself reports in its running-config.
"""
import argparse
import json
import re
import subprocess
import sys


def run_ping(target_ip: str, count: int, timeout: int) -> dict:
    cmd = ["ping", "-c", str(count), "-W", str(timeout), target_ip]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout * count + 10)
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "target_ip": target_ip,
            "error": "ping process timed out",
        }

    output = proc.stdout + proc.stderr

    loss_match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    packet_loss_pct = float(loss_match.group(1)) if loss_match else None

    rtt_match = re.search(
        r"= [\d.]+/([\d.]+)/[\d.]+(?:/[\d.]+)? ms", output
    )
    avg_rtt_ms = float(rtt_match.group(1)) if rtt_match else None

    sent_match = re.search(r"(\d+) packets transmitted", output)
    recv_match = re.search(r"(\d+) (?:packets )?received", output)
    packets_sent = int(sent_match.group(1)) if sent_match else count
    packets_received = int(recv_match.group(1)) if recv_match else 0

    return {
        "success": proc.returncode == 0 and packets_received > 0,
        "target_ip": target_ip,
        "packets_sent": packets_sent,
        "packets_received": packets_received,
        "packet_loss_pct": packet_loss_pct,
        "avg_rtt_ms": avg_rtt_ms,
        "raw_output": output.strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target_ip", required=True)
    parser.add_argument("--count", default="4")
    parser.add_argument("--timeout", default="2")
    args = parser.parse_args()

    # IAG passes every decorator property as a CLI flag even when unset,
    # arriving as "" rather than the schema default -- coerce here.
    count = int(args.count) if args.count else 4
    timeout = int(args.timeout) if args.timeout else 2

    try:
        result = run_ping(args.target_ip, count, timeout)
        print(json.dumps(result))
        return 0
    except Exception as e:
        print(json.dumps({"success": False, "target_ip": args.target_ip, "error": str(e)}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
