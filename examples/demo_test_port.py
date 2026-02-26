#!/usr/bin/env python3
import logging
import time

from rcbenchmark_udp import UDPChatClient, UdpEndpoint


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    rcb_ip = "127.0.0.1"
    rcb_rx_port = 56000
    py_listen_port = 64126

    client = UDPChatClient(
        rcb_rx=UdpEndpoint(rcb_ip, rcb_rx_port),
        py_rx_port=py_listen_port,
        py_bind_ip="0.0.0.0",
    )

    client.start()
    print(f"[INFO] Listening+Sending from UDP {py_listen_port} (bind 0.0.0.0)")
    print(f"[INFO] RCbenchmark at {rcb_ip}:{rcb_rx_port}")

    try:
        client.send_text("Python connected.")
        for thr in [1000, 1100, 1200, 1300, 1200, 1100, 1000]:
            client.send_throttle(thr)
            time.sleep(1.0)
    finally:
        client.stop()
        print("[INFO] stopped")


if __name__ == "__main__":
    main()
