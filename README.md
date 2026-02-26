# rcbenchmark-udp-client

Hello together i created a UDP Chat Client to communicate between the **RCbenchmark** software and a python interface. Since you people seem to like structure i asked chatGPT to create a good repo structure. The original file is still there ´UDPChatCLient.py´. Have fun!

Python UDP client intended to communicate with the **RCbenchmark** UDP “Chat” interface (typically via a JS script inside RCbenchmark).

## What this library does

- **Receives telemetry** (usually JSON strings) from RCbenchmark via UDP.
- **Sends commands** (throttle values or text) back to RCbenchmark via UDP.
- Uses **one single bound UDP socket** for both RX and TX so that the **source port of your outgoing commands equals your listen port** (this matters in some RCbenchmark setups).

## Port / direction model (typical)

On the RCbenchmark PC:

- RCbenchmark listens for commands on `receive_port` (example: `56000`)
- RCbenchmark sends telemetry to `send_ip:send_port` (example: `127.0.0.1:64126`)

On the Python side:

- Python binds to `py_bind_ip:py_rx_port` (example: `0.0.0.0:64126`)
- Python sends commands to `rcb_rx.ip:rcb_rx.port` (example: `127.0.0.1:56000`)

## Install

### From source (editable)

```bash
pip install -e .
```

## Quickstart

```python
from rcbenchmark_udp import UDPChatClient, UdpEndpoint

client = UDPChatClient(
    rcb_rx=UdpEndpoint("127.0.0.1", 56000),
    py_rx_port=64126,
    py_bind_ip="0.0.0.0",
)

client.start()
client.send_text("Python connected.")
client.send_throttle(1100)

telemetry = client.get_latest_telemetry()
print(telemetry)

client.stop()
```

## Examples

Run the demos from the repo root:

```bash
python examples/demo_test_port.py
python examples/demo_full.py
```

## CSV logging

Telemetry can be logged to CSV with one column per telemetry key:

```python
log_path = client.start_logging_csv("logs/telemetry.csv", append=True)
# ...
client.stop_logging_csv()
```

## Notes / gotchas

- If you receive **no telemetry**, check:
  - RCbenchmark `send_ip` is correct for your Python machine
  - UDP port `py_rx_port` is allowed through the firewall
- If commands do not apply, check that RCbenchmark’s `receive_port` matches `rcb_rx.port`

## License

MIT (see `LICENSE`).
