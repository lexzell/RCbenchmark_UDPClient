from __future__ import annotations

import csv
import json
import logging
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, Union

JSONType = Union[Dict[str, Any], list, str, int, float, bool, None]


@dataclass(frozen=True)
class UdpEndpoint:
"""Simple IP:port container."""

ip: str
port: int


class UDPChatClient:
"""
RCbenchmark UDP Chat client.

Typical RCbenchmark side (JS script):
  - listens for commands on receive_port (e.g., 56000)          [Python sends here]
  - sends telemetry to send_ip:send_port (e.g., 127.0.0.1:64126) [Python listens here]

This Python client:
  - binds ONE UDP socket to py_bind_ip:py_rx_port (default 0.0.0.0:64126)
  - uses that same socket for:
      - recvfrom() telemetry
      - sendto() commands (throttle/text)

Using a single bound socket ensures outgoing command packets have source port == py_rx_port.
Some setups require this; sending from an ephemeral source port can break command reception.
"""

def __init__(
    self,
    *,
    rcb_rx: UdpEndpoint,
    py_rx_port: int = 64126,
    py_bind_ip: str = "0.0.0.0",
    recv_buf_size: int = 65535,
    rx_timeout_s: float = 0.5,
    decode_telemetry_json: bool = True,
    on_telemetry_raw: Optional[Callable[[str, Tuple[str, int]], None]] = None,
    on_telemetry_json: Optional[Callable[[JSONType, Tuple[str, int]], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
    enable_tx_logs: bool = True,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Parameters
    ----------
    rcb_rx:
        RCbenchmark command-receive endpoint (RCbenchmark listens here).
    py_rx_port:
        UDP port to bind locally for receiving telemetry (and as TX source port).
    py_bind_ip:
        Local bind address. Use "0.0.0.0" to listen on all interfaces.
    recv_buf_size:
        Max UDP packet size to receive.
    rx_timeout_s:
        Socket timeout used by the RX thread.
    decode_telemetry_json:
        If True, attempts json.loads() on received telemetry strings.
    on_telemetry_raw:
        Callback invoked with raw telemetry string and sender address.
    on_telemetry_json:
        Callback invoked with parsed telemetry object (if JSON parse succeeds).
    on_error:
        Callback invoked when internal exceptions occur.
    enable_tx_logs:
        If True, logs each sent command.
    logger:
        Optional logger. If None, uses module logger.
    """
    self.rcb_rx = rcb_rx
    self.py_rx_port = int(py_rx_port)
    self.py_bind_ip = str(py_bind_ip)
    self.recv_buf_size = int(recv_buf_size)
    self.rx_timeout_s = float(rx_timeout_s)
    self.decode_telemetry_json = bool(decode_telemetry_json)
    self.enable_tx_logs = bool(enable_tx_logs)

    self._on_raw = on_telemetry_raw
    self._on_json = on_telemetry_json
    self._on_error = on_error

    self._log = logger or logging.getLogger(__name__)

    # SINGLE socket for both RX and TX
    self._sock: Optional[socket.socket] = None
    self._rx_thread: Optional[threading.Thread] = None
    self._stop_evt = threading.Event()

    # --- telemetry storage (thread-safe) ---
    self._telemetry_lock = threading.Lock()
    self._last_telemetry_raw: Optional[str] = None
    self._last_telemetry_obj: Optional[Dict[str, Any]] = None
    self._last_telemetry_addr: Optional[Tuple[str, int]] = None
    self._last_telemetry_ts: Optional[float] = None

    # --- CSV logging state (thread-safe) ---
    self._log_lock = threading.Lock()
    self._logging_enabled = False
    self._log_path: Optional[str] = None
    self._log_file: Optional[Any] = None
    self._log_queue: "queue.Queue[Tuple[float, Tuple[str, int], Dict[str, Any]]]" = queue.Queue(
        maxsize=5000
    )
    self._log_stop_evt = threading.Event()
    self._log_thread: Optional[threading.Thread] = None

    # logging config placeholders (set in start_logging_csv)
    self._log_value_field = "workingValue"
    self._log_unit_field: Optional[str] = None
    self._log_include_src = True
    self._log_include_arrival_ts = True
    self._log_keys: Optional[Tuple[str, ...]] = None
    self._log_fieldnames: Optional[list[str]] = None
    self._log_dict_writer: Optional[csv.DictWriter] = None
    self._log_need_header_write: bool = False

# ---------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------
def start(self) -> None:
    """Bind socket and start RX thread."""
    if self._rx_thread and self._rx_thread.is_alive():
        return

    self._stop_evt.clear()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        # not supported everywhere
        pass

    sock.settimeout(self.rx_timeout_s)
    sock.bind((self.py_bind_ip, self.py_rx_port))

    self._sock = sock

    self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True, name="rcb-udp-rx")
    self._rx_thread.start()

def stop(self) -> None:
    """Stop RX thread, stop logging, close socket."""
    self.stop_logging_csv()

    self._stop_evt.set()

    if self._sock:
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    if self._rx_thread and self._rx_thread.is_alive():
        self._rx_thread.join(timeout=1.0)

# ---------------------------------------------------------------------
# TX API
# ---------------------------------------------------------------------
def send_text(self, text: str) -> None:
    """Send a non-numeric string to RCbenchmark (it may print it)."""
    self._send_raw(text)

def send_throttle(self, throttle: Union[int, float]) -> None:
    """Send numeric throttle to RCbenchmark (it will set ESC to this value)."""
    self._send_raw(str(throttle))

def _send_raw(self, msg: str) -> None:
    """
    Send using the SAME bound socket (source port == py_rx_port).
    This avoids an ephemeral TX port.
    """
    if not self._sock:
        raise RuntimeError("Client not started. Call start() first.")

    data = msg.encode("utf-8")
    sent = self._sock.sendto(data, (self.rcb_rx.ip, self.rcb_rx.port))

    if self.enable_tx_logs:
        self._log.info(
            "TX %s:%d -> %s:%d bytes=%d msg=%r",
            self.py_bind_ip,
            self.py_rx_port,
            self.rcb_rx.ip,
            self.rcb_rx.port,
            sent,
            msg,
        )

# ---------------------------------------------------------------------
# CSV LOGGING API (FLATTENED TELEMETRY)
# ---------------------------------------------------------------------
def start_logging_csv(
    self,
    file_path: str = "udp_telemetry.csv",
    *,
    append: bool = True,
    value_field: str = "workingValue",
    unit_field: Optional[str] = None,
    keys: Optional[Tuple[str, ...]] = None,
    include_src: bool = True,
    include_arrival_ts: bool = True,
) -> str:
    """
    Start logging flattened telemetry to CSV (one column per telemetry key).

    Header is built from the first received telemetry dict unless `keys` is provided.

    Parameters
    ----------
    file_path:
        Output CSV file. Directories are created automatically.
    append:
        Append to file if it exists, otherwise overwrite.
    value_field:
        Which field inside each telemetry dict to log as the scalar value
        (commonly "workingValue" or "displayValue").
    unit_field:
        If provided, also log units (e.g. "workingUnit" or "displayUnit").
    keys:
        If provided, fixes the column order and which telemetry signals to record.
        If None, keys are inferred from the first received telemetry dict.
    include_src:
        Include src_ip and src_port columns.
    include_arrival_ts:
        Include arrival_ts_unix column.
    """
    abs_path = os.path.abspath(file_path)
    dir_name = os.path.dirname(abs_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)

    mode = "a" if append else "w"
    file_exists = os.path.exists(abs_path)
    need_header = (not file_exists) or (not append) or (os.path.getsize(abs_path) == 0)

    with self._log_lock:
        if self._logging_enabled:
            self.stop_logging_csv()

        try:
            f = open(abs_path, mode, newline="", encoding="utf-8")
        except OSError as e:
            self._emit_error(e)
            raise

        self._log_path = abs_path
        self._log_file = f
        self._logging_enabled = True
        self._log_stop_evt.clear()

        self._log_value_field = str(value_field)
        self._log_unit_field = unit_field if unit_field is None else str(unit_field)
        self._log_include_src = bool(include_src)
        self._log_include_arrival_ts = bool(include_arrival_ts)

        self._log_keys = tuple(keys) if keys is not None else None
        self._log_fieldnames = None
        self._log_dict_writer = None
        self._log_need_header_write = bool(need_header)

        self._log_thread = threading.Thread(target=self._log_loop_flat, daemon=True, name="rcb-udp-csv")
        self._log_thread.start()

    return abs_path

def stop_logging_csv(self) -> None:
    """Stop CSV logging and close file handle safely."""
    with self._log_lock:
        if not self._logging_enabled:
            return
        self._logging_enabled = False
        self._log_stop_evt.set()

    if self._log_thread and self._log_thread.is_alive():
        self._log_thread.join(timeout=2.0)

    with self._log_lock:
        if self._log_file:
            try:
                self._log_file.flush()
            except OSError:
                pass
            try:
                self._log_file.close()
            except OSError:
                pass

        self._log_file = None
        self._log_dict_writer = None
        self._log_fieldnames = None
        self._log_thread = None
        self._log_path = None
        self._log_keys = None

    try:
        while True:
            self._log_queue.get_nowait()
    except queue.Empty:
        pass

def _ensure_log_header_from_obj(self, obj: Dict[str, Any]) -> None:
    """Initialize DictWriter + header from either configured keys or first telemetry object."""
    with self._log_lock:
        if self._log_dict_writer is not None and self._log_fieldnames is not None:
            return

        f = self._log_file
        if not f:
            return

        keys = self._log_keys
        if keys is None:
            keys = tuple(obj.keys())
            self._log_keys = keys

        fieldnames: list[str] = []
        if self._log_include_arrival_ts:
            fieldnames.append("arrival_ts_unix")
        if self._log_include_src:
            fieldnames.extend(["src_ip", "src_port"])

        unit_field = self._log_unit_field
        for k in keys:
            fieldnames.append(k)
            if unit_field is not None:
                fieldnames.append(f"{k}_unit")

        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        self._log_fieldnames = fieldnames
        self._log_dict_writer = writer

        if self._log_need_header_write:
            writer.writeheader()
            try:
                f.flush()
            except OSError:
                pass
            self._log_need_header_write = False

def _enqueue_log_obj(self, ts: float, addr: Tuple[str, int], obj: Dict[str, Any]) -> None:
    """Enqueue telemetry object for logging. Drops if queue is full."""
    with self._log_lock:
        if not self._logging_enabled:
            return
    try:
        self._log_queue.put_nowait((ts, addr, obj))
    except queue.Full:
        # drop rather than blocking RX
        pass

def _log_loop_flat(self) -> None:
    """Writer thread for flattened dict rows."""
    while True:
        if self._log_stop_evt.is_set() and self._log_queue.empty():
            break
        try:
            ts, addr, obj = self._log_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        self._ensure_log_header_from_obj(obj)

        with self._log_lock:
            writer = self._log_dict_writer
            f = self._log_file
            value_field = self._log_value_field
            unit_field = self._log_unit_field
            keys = self._log_keys or tuple(obj.keys())
            include_src = self._log_include_src
            include_ts = self._log_include_arrival_ts

        if writer is None or f is None:
            continue

        row: Dict[str, Any] = {}
        if include_ts:
            row["arrival_ts_unix"] = ts
        if include_src:
            row["src_ip"] = addr[0]
            row["src_port"] = int(addr[1])

        for k in keys:
            entry = obj.get(k)
            if isinstance(entry, dict):
                row[k] = entry.get(value_field)
                if unit_field is not None:
                    row[f"{k}_unit"] = entry.get(unit_field)
            else:
                row[k] = None
                if unit_field is not None:
                    row[f"{k}_unit"] = None

        try:
            writer.writerow(row)
            f.flush()
        except Exception as e:
            self._emit_error(e)

# ---------------------------------------------------------------------
# RX
# ---------------------------------------------------------------------
def _rx_loop(self) -> None:
    """Receive telemetry, store latest, call callbacks, and enqueue for logging."""
    assert self._sock is not None

    while not self._stop_evt.is_set():
        try:
            data, addr = self._sock.recvfrom(self.recv_buf_size)
        except socket.timeout:
            continue
        except OSError as e:
            if not self._stop_evt.is_set():
                self._emit_error(e)
            break

        try:
            msg = data.decode("utf-8")
        except UnicodeDecodeError:
            # fallback if RCbenchmark emits cp1252
            msg = data.decode("cp1252", errors="strict")

        now = time.time()

        with self._telemetry_lock:
            self._last_telemetry_raw = msg
            self._last_telemetry_addr = addr
            self._last_telemetry_ts = now

        if self._on_raw:
            try:
                self._on_raw(msg, addr)
            except Exception as cb_e:
                self._emit_error(cb_e)

        obj: Optional[JSONType] = None
        if self.decode_telemetry_json:
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                obj = None

            if isinstance(obj, dict):
                with self._telemetry_lock:
                    self._last_telemetry_obj = obj

            if obj is not None and self._on_json:
                try:
                    self._on_json(obj, addr)
                except Exception as cb_e:
                    self._emit_error(cb_e)

        if isinstance(obj, dict):
            self._enqueue_log_obj(now, addr, obj)

def _emit_error(self, e: Exception) -> None:
    if self._on_error:
        try:
            self._on_error(e)
        except Exception:
            pass
    else:
        self._log.exception("Internal error: %r", e)

# ---------------------------------------------------------------------
# GETTERS
# ---------------------------------------------------------------------
def get_latest_telemetry(self) -> Optional[Dict[str, Any]]:
    """Return a shallow copy of most recent parsed telemetry dict, or None."""
    with self._telemetry_lock:
        if self._last_telemetry_obj is None:
            return None
        return dict(self._last_telemetry_obj)

def get_signal(self, key: str, *, use_display: bool = False) -> Tuple[Optional[float], str]:
    """
    Return (value, unit) for a telemetry signal.

    If `use_display=True`, uses displayValue/displayUnit, otherwise workingValue/workingUnit.

    Returns
    -------
    value:
        Parsed float value if possible, else None.
    unit:
        Unit string (may be empty).
    """
    v_key = "displayValue" if use_display else "workingValue"
    u_key = "displayUnit" if use_display else "workingUnit"

    with self._telemetry_lock:
        obj = self._last_telemetry_obj
        if obj is None:
            return None, ""

        entry = obj.get(key)
        if not isinstance(entry, dict):
            return None, ""

        val = entry.get(v_key)
        unit = entry.get(u_key) or ""

    if val == "" or val is None:
        return None, unit

    if isinstance(val, (int, float)):
        return float(val), unit

    try:
        return float(val), unit
    except (TypeError, ValueError):
        return None, unit

def get_working(self, key: str) -> Tuple[Optional[float], str]:
    """Convenience: workingValue/workingUnit."""
    return self.get_signal(key, use_display=False)

def get_display(self, key: str) -> Tuple[Optional[float], str]:
    """Convenience: displayValue/displayUnit."""
    return self.get_signal(key, use_display=True)
