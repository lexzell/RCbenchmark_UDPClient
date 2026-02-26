#!/usr/bin/env python3
import logging
import time
from typing import Tuple

from rcbenchmark_udp import JSONType, UDPChatClient, UdpEndpoint


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # You must set:
    #  - rcb_ip       = IP of the PC running RCbenchmark
    #  - rcb_rx_port  = receive_port in the RCbenchmark JS script (e.g. 56000)
    #  - py_listen_port = send_port in RCbenchmark JS script (e.g. 64126)
    rcb_ip = "127.0.0.1"
    rcb_rx_port = 56000
    py_listen_port = 64126

    telemetry_seen = {"count": 0, "last_from": None}

    def on_raw(msg: str, addr: Tuple[str, int]) -> None:
        telemetry_seen["count"] += 1
        telemetry_seen["last_from"] = addr

    def on_json(obj: JSONType, addr: Tuple[str, int]) -> None:
        # Example: inspect keys
        if isinstance(obj, dict):
            _ = list(obj.keys())

    client = UDPChatClient(
        rcb_rx=UdpEndpoint(rcb_ip, rcb_rx_port),
        py_rx_port=py_listen_port,
        py_bind_ip="0.0.0.0",
        on_telemetry_raw=on_raw,
        on_telemetry_json=on_json,
    )

    client.start()
    print(f"[INFO] Listening for telemetry on UDP {py_listen_port} (bind 0.0.0.0)")
    print(f"[INFO] Sending commands to RCbenchmark at {rcb_ip}:{rcb_rx_port}")

    log_path = client.start_logging_csv("logs/rcbenchmark_telemetry.csv", append=True)
    print(f"[INFO] CSV logging to: {log_path}")

    t0 = time.time()
    while time.time() - t0 < 2.0 and telemetry_seen["count"] == 0:
        time.sleep(0.05)

    if telemetry_seen["count"] == 0:
        print("[DIAG] No telemetry received in 2s.")
        print("       => RCbenchmark 'send_ip' is wrong OR firewall blocks UDP port on this PC.")
    else:
        print(f"[DIAG] Telemetry received from {telemetry_seen['last_from']} count={telemetry_seen['count']}")

    client.send_text("Python connection working.")

    try:
        for thr in [1000, 1100, 1200, 1300, 1400, 1300, 1200, 1100, 1000]:
            client.send_throttle(thr)
            print(f"[DEMO] Sent throttle: {thr}")
            time.sleep(10)
    finally:
        client.stop_logging_csv()
        client.stop()
        print("[INFO] stopped")


if __name__ == "__main__":
    main()
