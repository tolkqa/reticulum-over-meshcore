"""
MeshCoreInterface — Reticulum custom interface for MeshCore LoRa mesh.

Transmits Reticulum packets over MeshCore's flood broadcast mechanism
using CMD_SEND_RAW_DATA with path_len=0. Packets larger than a single
MeshCore frame are fragmented and reassembled transparently.

Supports directed routing via MeshCore peer paths when route_mode = directed
(default). Discovers chat peers from the node's contact list and uses their
known paths for targeted sends, falling back to flood when no path is available.

Configuration in ~/.reticulum/config:

    [[MeshCore LoRa]]
      type = MeshCoreInterface
      enabled = true
      port = /dev/ttyUSB0
      speed = 115200
      # or for BLE:
      # ble_address = AA:BB:CC:DD:EE:FF
      # ble_pin = 123456
      # route_mode = directed   # "directed" (default) or "flood"
"""

import time
import threading

import RNS
from RNS.Interfaces.Interface import Interface

from meshcore_interface.transport import MeshCoreTransport
from meshcore_interface.fragmentation import Fragmenter, Reassembler

# Delay between fragment transmissions (seconds).
# LoRa SF12 BW125: ~168 byte packet ≈ 1.5-2s airtime + processing.
INTER_FRAGMENT_DELAY = 2.5

# Reconnection parameters
RECONNECT_INITIAL_DELAY = 5    # seconds before first reconnect attempt
RECONNECT_MAX_DELAY = 60       # max backoff delay
RECONNECT_BACKOFF_FACTOR = 2


class MeshCoreInterface(Interface):
    """Reticulum interface bridging to MeshCore LoRa mesh network."""

    DEFAULT_IFAC_SIZE = 8
    BITRATE_GUESS = 1200  # ~1.2 kbps estimate for LoRa SF12 BW125

    def __init__(self, owner, configuration):
        super().__init__()

        c = Interface.get_config_obj(configuration)
        name = c["name"]

        self._connect_args = {
            "port": c["port"] if "port" in c else None,
            "baudrate": int(c["speed"]) if "speed" in c else 115200,
            "ble_address": c["ble_address"] if "ble_address" in c else None,
            "ble_pin": c["ble_pin"] if "ble_pin" in c else None,
        }

        # TCP connection: tcp_host and tcp_port, or tcp_port as "host:port"
        tcp_host = c["tcp_host"] if "tcp_host" in c else None
        tcp_port = c["tcp_port"] if "tcp_port" in c else None
        if tcp_port and not tcp_host and ":" in str(tcp_port):
            tcp_host, tcp_port = str(tcp_port).rsplit(":", 1)
        self._connect_args["tcp_host"] = tcp_host
        self._connect_args["tcp_port"] = tcp_port

        self._route_mode = c["route_mode"] if "route_mode" in c else "directed"

        self.HW_MTU = 500
        self.owner = owner
        self.name = name
        self.bitrate = MeshCoreInterface.BITRATE_GUESS
        self.online = False
        self.IN = True
        self.OUT = False

        self._transport = MeshCoreTransport()
        self._fragmenter = Fragmenter()
        self._reassembler = Reassembler()
        self._reconnecting = False
        self._detached = False

        try:
            self._do_connect()
        except Exception as e:
            RNS.log(
                f"[{self.name}] Failed to initialize: {e}",
                RNS.LOG_ERROR,
            )
            self._schedule_reconnect()

    def _do_connect(self):
        """Establish connection to MeshCore device."""
        RNS.log(
            f"[{self.name}] Starting MeshCore transport...", RNS.LOG_DEBUG
        )
        self._transport.start()
        self._transport.subscribe_raw(self._on_raw_data)
        self._transport.set_disconnect_callback(self._on_ble_disconnect)

        RNS.log(
            f"[{self.name}] Connecting to MeshCore device...", RNS.LOG_INFO
        )
        self._transport.connect(**self._connect_args)

        RNS.log(
            f"[{self.name}] Querying device info...", RNS.LOG_DEBUG
        )
        self._transport.initialize()
        self._transport.activate_subscriptions()

        # BLE connections need a brief warmup before TX is reliable.
        # The keep-alive sends an immediate first ping; give it time to complete.
        if self._connect_args.get("ble_address"):
            time.sleep(2)

        if self._route_mode == "directed":
            try:
                self._transport.load_contacts()
                self._transport.subscribe_path_updates()
                RNS.log(
                    f"[{self.name}] Directed routing enabled, "
                    f"{self._transport.peer_count} chat peer(s) with known paths",
                    RNS.LOG_INFO,
                )
            except Exception as e:
                RNS.log(
                    f"[{self.name}] Failed to load contacts for directed routing, "
                    f"falling back to flood: {e}",
                    RNS.LOG_WARNING,
                )
        else:
            RNS.log(
                f"[{self.name}] Flood routing mode (directed routing disabled)",
                RNS.LOG_INFO,
            )

        self.online = True
        self.OUT = True
        RNS.log(
            f"[{self.name}] MeshCore interface online", RNS.LOG_INFO
        )

    def _schedule_reconnect(self):
        """Start reconnection loop in a background thread."""
        if self._reconnecting or self._detached:
            return
        self._reconnecting = True
        self.online = False
        self.OUT = False
        threading.Thread(
            target=self._reconnect_loop, name=f"MCReconnect-{self.name}", daemon=True
        ).start()

    def _reconnect_loop(self):
        delay = RECONNECT_INITIAL_DELAY
        while not self._detached:
            RNS.log(
                f"[{self.name}] Reconnecting in {delay}s...", RNS.LOG_NOTICE
            )
            time.sleep(delay)
            if self._detached:
                break

            try:
                self._transport.disconnect()
            except Exception:
                pass
            self._transport = MeshCoreTransport()

            try:
                self._do_connect()
                RNS.log(
                    f"[{self.name}] Reconnected successfully", RNS.LOG_NOTICE
                )
                self._reconnecting = False
                return
            except Exception as e:
                RNS.log(
                    f"[{self.name}] Reconnect failed: {e}", RNS.LOG_ERROR
                )
                delay = min(delay * RECONNECT_BACKOFF_FACTOR, RECONNECT_MAX_DELAY)

        self._reconnecting = False

    def process_outgoing(self, data):
        """Called by Reticulum Transport to transmit a packet."""
        if not self.online:
            return

        try:
            fragments = self._fragmenter.fragment(data)

            # Snapshot the path once for all fragments of this message
            path = None
            if self._route_mode == "directed":
                path = self._transport.get_peer_path()

            route_desc = f"directed path_len={len(path)}" if path else "flood"
            RNS.log(
                f"[{self.name}] TX {len(data)}B in {len(fragments)} fragment(s) [{route_desc}]",
                RNS.LOG_VERBOSE,
            )
            for i, fragment in enumerate(fragments):
                if i > 0:
                    time.sleep(INTER_FRAGMENT_DELAY)
                if not self._transport.send_raw(fragment, path=path):
                    RNS.log(
                        f"[{self.name}] Failed to send fragment {i+1}/{len(fragments)}",
                        RNS.LOG_ERROR,
                    )
                    # Only reconnect if BLE is actually disconnected.
                    # Transient firmware errors should not kill the connection.
                    if not self._transport.is_connected:
                        self._schedule_reconnect()
                    return
            self.txb += len(data)

        except Exception as e:
            RNS.log(f"[{self.name}] TX error: {e}", RNS.LOG_ERROR)
            if not self._transport.is_connected:
                self._schedule_reconnect()

    def _on_ble_disconnect(self):
        """Called by transport when BLE keep-alive detects disconnection."""
        RNS.log(
            f"[{self.name}] BLE disconnected (detected by keep-alive)",
            RNS.LOG_WARNING,
        )
        self._schedule_reconnect()

    def _on_raw_data(self, payload, snr, rssi):
        """Callback for incoming RAW_DATA from MeshCore device."""
        if not isinstance(payload, (bytes, bytearray)) or len(payload) == 0:
            return

        try:
            data = self._reassembler.process(payload)
            RNS.log(
                f"[{self.name}] RX fragment {len(payload)}B (SNR={snr}, RSSI={rssi})",
                RNS.LOG_VERBOSE,
            )
            if data is not None:
                RNS.log(
                    f"[{self.name}] RX complete packet {len(data)}B",
                    RNS.LOG_VERBOSE,
                )
                self.rxb += len(data)
                self.owner.inbound(data, self)

        except Exception as e:
            RNS.log(f"[{self.name}] RX error: {e}", RNS.LOG_ERROR)

    def detach(self):
        """Clean up resources when interface is detached."""
        RNS.log(f"[{self.name}] Detaching MeshCore interface", RNS.LOG_DEBUG)
        self._detached = True
        self.online = False
        self.OUT = False
        self.IN = False
        if self._transport is not None:
            self._transport.disconnect()
            self._transport = None

    def __str__(self):
        return f"MeshCoreInterface[{self.name}]"
