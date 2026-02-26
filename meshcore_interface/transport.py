"""
Async-to-sync bridge between the meshcore async library and Reticulum's
synchronous interface model.

Runs an asyncio event loop in a daemon thread and provides synchronous
wrappers for MeshCore operations.

Also applies a monkey-patch to fix the RAW_DATA parsing bug in meshcore's
MessageReader.handle_rx (reads only 4 bytes instead of full payload).
"""

import io
import asyncio
import inspect
import random
import threading
import logging
from concurrent.futures import ThreadPoolExecutor

from meshcore import MeshCore
from meshcore.reader import MessageReader
from meshcore.packets import PacketType
from meshcore.events import Event, EventType

log = logging.getLogger(__name__)

_patch_applied = False


def _needs_raw_data_patch():
    """Check if MessageReader.handle_rx still has the RAW_DATA 4-byte bug.

    The bug reads only 4 bytes of payload and converts to hex string:
        res["payload"] = dbuf.read(4).hex()
    If this pattern is found, the patch is needed.
    """
    try:
        src = inspect.getsource(MessageReader.handle_rx)
        return "read(4).hex()" in src
    except (TypeError, OSError):
        log.warning("Cannot inspect MessageReader.handle_rx — applying RAW_DATA patch as a precaution")
        return True


def _apply_raw_data_patch():
    """Monkey-patch MessageReader.handle_rx to correctly parse RAW_DATA.

    The meshcore library (tested with v2.2.14) has a bug where
    PUSH_CODE_RAW_DATA (0x84) reads only 4 bytes of payload as a hex
    string instead of the full remaining data as bytes.

    Corrected packet format:
        [0x84][SNR: int8×4][RSSI: int8][reserved: 0xFF][payload_bytes...]
    """
    global _patch_applied
    if _patch_applied:
        return
    _patch_applied = True

    if not _needs_raw_data_patch():
        log.info(
            "meshcore RAW_DATA bug not detected — skipping monkey-patch. "
            "If RAW_DATA reception is broken, please report an issue."
        )
        return

    _original_handle_rx = MessageReader.handle_rx

    async def _patched_handle_rx(self, data: bytearray):
        if len(data) > 0 and data[0] == PacketType.RAW_DATA.value:
            dbuf = io.BytesIO(data)
            dbuf.read(1)  # skip packet type byte (0x84)
            res = {}
            res["SNR"] = (
                int.from_bytes(dbuf.read(1), byteorder="little", signed=True) / 4
            )
            res["RSSI"] = int.from_bytes(
                dbuf.read(1), byteorder="little", signed=True
            )
            dbuf.read(1)  # skip reserved byte (0xFF)
            res["payload"] = dbuf.read()  # read ALL remaining bytes
            log.debug(
                f"RAW_DATA: SNR={res['SNR']}, RSSI={res['RSSI']}, "
                f"payload={len(res['payload'])} bytes"
            )
            await self.dispatcher.dispatch(Event(EventType.RAW_DATA, res))
        else:
            await _original_handle_rx(self, data)

    MessageReader.handle_rx = _patched_handle_rx
    log.info("Applied RAW_DATA monkey-patch to MessageReader.handle_rx (meshcore bug workaround)")


# Apply patch on module import
_apply_raw_data_patch()


BLE_KEEPALIVE_INTERVAL = 5  # seconds between keep-alive pings


class MeshCoreTransport:
    """Bridge between async meshcore library and sync Reticulum interface."""

    def __init__(self):
        self._mc = None
        self._loop = None
        self._thread = None
        self._raw_callbacks = []
        self._subscription = None
        self._is_ble = False
        self._keepalive_handle = None
        self._disconnect_callback = None
        self._peer_paths = {}           # {public_key_hex: bytes} — directed path per peer
        self._path_subscription = None
        # Single-thread executor to serialize inbound callbacks, matching
        # the sequential pattern used by standard RNS interfaces (e.g. SerialInterface).
        self._rx_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="MCRx")

    def set_disconnect_callback(self, callback):
        """Register a callback invoked when BLE keep-alive detects disconnection.

        The callback signature: callback() — called from a thread pool.
        """
        self._disconnect_callback = callback

    def start(self):
        """Start the asyncio event loop in a background thread.

        Idempotent: if a loop is already running, it is stopped first to
        prevent thread leaks.
        """
        if self._loop is not None and self._loop.is_running():
            self._stop_loop()
        self._rx_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="MCRx")
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="MeshCoreAsync", daemon=True
        )
        self._thread.start()

    def _stop_loop(self):
        """Stop the asyncio event loop and join its thread."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._loop.close()
        self._loop = None
        self._thread = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_coro(self, coro, timeout=10):
        """Run an async coroutine from sync context and return its result."""
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("Asyncio event loop is not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def connect(self, port=None, baudrate=115200, ble_address=None, ble_pin=None,
                tcp_host=None, tcp_port=None):
        """Connect to MeshCore device via USB serial, BLE, or TCP.

        Args:
            port: Serial port path (e.g. /dev/ttyUSB0)
            baudrate: Serial baud rate (default 115200)
            ble_address: Bluetooth address (e.g. AA:BB:CC:DD:EE:FF)
            ble_pin: BLE pairing PIN
            tcp_host: TCP host address (e.g. 192.168.3.36)
            tcp_port: TCP port number (e.g. 5000)
        """

        self._is_ble = False

        async def _connect():
            if tcp_host and tcp_port:
                self._mc = await MeshCore.create_tcp(
                    host=tcp_host,
                    port=int(tcp_port),
                )
                if self._mc is None:
                    raise ConnectionError(
                        f"MeshCore device at {tcp_host}:{tcp_port} did not respond. "
                        "Check that no other app is connected and the device is in companion mode."
                    )
                log.info(f"Connected to MeshCore device via TCP ({tcp_host}:{tcp_port})")
            elif ble_address:
                self._mc = await MeshCore.create_ble(
                    address=ble_address,
                    pin=ble_pin,
                )
                if self._mc is None:
                    raise ConnectionError(
                        f"MeshCore device at {ble_address} did not respond."
                    )
                self._is_ble = True
                log.info(f"Connected to MeshCore device via BLE ({ble_address})")
            elif port:
                self._mc = await MeshCore.create_serial(
                    port=port,
                    baudrate=baudrate,
                )
                if self._mc is None:
                    raise ConnectionError(
                        f"MeshCore device at {port} did not respond."
                    )
                log.info(f"Connected to MeshCore device via serial ({port})")
            else:
                raise ValueError("Either port, ble_address, or tcp_host must be specified")

        self._run_coro(_connect(), timeout=30)

    def initialize(self):
        """Query device info after connection.

        Sends CMD_DEVICE_QUERY to negotiate protocol version.
        Returns device info payload.
        """

        async def _init():
            result = await self._mc.commands.send(
                b"\x16\x03",
                [EventType.DEVICE_INFO, EventType.ERROR],
            )
            if result.type == EventType.ERROR:
                raise RuntimeError(f"Device query failed: {result.payload}")
            log.info(f"MeshCore device info: {result.payload}")
            return result.payload

        return self._run_coro(_init(), timeout=15)

    def get_radio_params(self):
        """Return radio parameters from the cached SELF_INFO fetched during connect().

        The meshcore library calls send_appstart() internally on connection and
        stores the result in self._mc._self_info — no extra round-trip needed.

        Returns a dict with keys: radio_sf, radio_bw (kHz), radio_cr.
        Returns None if the fields are not available.
        """
        try:
            info = self._mc._self_info
        except AttributeError:
            log.warning("_self_info not available on MeshCore instance")
            return None

        if info is None:
            log.warning("SELF_INFO not cached yet")
            return None

        if "radio_sf" not in info or "radio_bw" not in info or "radio_cr" not in info:
            log.warning(f"SELF_INFO missing radio fields: {info}")
            return None

        log.info(
            f"Radio params: SF={info['radio_sf']} BW={info['radio_bw']}kHz CR={info['radio_cr']}"
        )
        return {
            "radio_sf": info["radio_sf"],
            "radio_bw": info["radio_bw"],
            "radio_cr": info["radio_cr"],
        }

    SEND_RAW_RETRIES = 3
    SEND_RAW_RETRY_DELAY = 1.0   # base delay between retries (seconds)
    SEND_RAW_RETRY_JITTER = 1.0  # max random jitter added to each retry delay

    def send_raw(self, payload: bytes, path: bytes = None):
        """Send raw data via CMD_SEND_RAW_DATA.

        Args:
            payload: Raw bytes to transmit (should be a single fragment).
            path: Optional directed path bytes. If None, uses flood broadcast (path_len=0).

        Returns:
            True if sent successfully, False on error.
        """

        async def _send():
            if path is not None and len(path) > 0:
                cmd_data = b"\x19" + bytes([len(path)]) + path + payload
            else:
                cmd_data = b"\x19\x00" + payload
            for attempt in range(self.SEND_RAW_RETRIES):
                result = await self._mc.commands.send(
                    cmd_data,
                    [EventType.OK, EventType.ERROR],
                )
                if result.type != EventType.ERROR:
                    log.debug(f"Raw data sent, response: {result.type} {result.payload}")
                    return True
                log.warning(
                    f"Raw data send attempt {attempt+1}/{self.SEND_RAW_RETRIES} "
                    f"failed: {result.payload}"
                )
                if attempt < self.SEND_RAW_RETRIES - 1:
                    delay = self.SEND_RAW_RETRY_DELAY + random.uniform(0, self.SEND_RAW_RETRY_JITTER)
                    await asyncio.sleep(delay)
            log.error(f"Raw data send failed after {self.SEND_RAW_RETRIES} attempts")
            return False

        return self._run_coro(_send(), timeout=15)

    def subscribe_raw(self, callback):
        """Register a callback for incoming RAW_DATA events.

        The callback signature: callback(payload: bytes, snr: float, rssi: int)
        Callbacks are dispatched to a thread pool, safe for blocking operations.
        """
        self._raw_callbacks.append(callback)

    def activate_subscriptions(self):
        """Activate event subscriptions. Call after connect()."""
        if self._mc is None or not self._raw_callbacks:
            return

        async def _subscribe():
            async def _on_raw(event):
                payload = event.payload.get("payload", b"")
                snr = event.payload.get("SNR", 0)
                rssi = event.payload.get("RSSI", 0)
                # Dispatch to a single-thread executor so:
                # 1. Sync callbacks (Reticulum owner.inbound) don't block the asyncio loop.
                # 2. Callbacks are serialized (no concurrent inbound), matching the
                #    sequential pattern of standard RNS interfaces like SerialInterface.
                for cb in self._raw_callbacks:
                    self._loop.run_in_executor(
                        self._rx_executor, self._invoke_callback, cb, payload, snr, rssi
                    )

            self._subscription = self._mc.subscribe(EventType.RAW_DATA, _on_raw)

        self._run_coro(_subscribe(), timeout=5)

        # Start BLE keep-alive to prevent idle disconnect
        if self._is_ble:
            self._start_keepalive()

    def load_contacts(self):
        """Fetch contacts from device and cache paths for chat peers."""

        async def _load():
            result = await self._mc.commands.get_contacts()
            if result is None or result.type == EventType.ERROR:
                log.warning(f"Failed to load contacts: {result}")
                self._peer_paths = {}  # Clear stale paths on failure
                return
            self._peer_paths = self._parse_chat_peers(result.payload)
            log.info(f"Discovered {len(self._peer_paths)} chat peer(s) with known paths")

        self._run_coro(_load(), timeout=15)

    def subscribe_path_updates(self):
        """Subscribe to PATH_UPDATE events to refresh peer paths on routing changes."""

        async def _subscribe():
            async def _on_path_update(event):
                pk_hex = event.payload.get("public_key", "")
                log.info(f"Path update for {pk_hex[:16]}..., re-fetching contacts")
                result = await self._mc.commands.get_contacts()
                if result is None or result.type == EventType.ERROR:
                    log.warning(f"Failed to re-fetch contacts after path update: {result}")
                    self._peer_paths = {}  # Clear stale paths on failure
                    return
                self._peer_paths = self._parse_chat_peers(result.payload)
                log.info(f"Updated peer paths: {len(self._peer_paths)} peer(s) with known paths")

            self._path_subscription = self._mc.subscribe(EventType.PATH_UPDATE, _on_path_update)

        self._run_coro(_subscribe(), timeout=5)

    def _parse_chat_peers(self, contacts):
        """Extract chat peers with routed paths from a contacts dict.

        Only stores peers with out_path_len > 0 (at least one repeater hop).
        Direct neighbors (out_path_len == 0) are skipped because they are
        already reachable via flood and storing them as b"" would cause
        get_peer_path() to silently select flood over a real directed path.

        Args:
            contacts: Dict keyed by public_key hex, values are contact dicts.

        Returns:
            Dict mapping public_key hex to path bytes for routed chat peers.
        """
        peers = {}
        for pk_hex, contact in contacts.items():
            if contact.get("type") != 1:  # Only Chat contacts
                continue
            path_len = contact.get("out_path_len", -1)
            if path_len < 0:
                log.debug(
                    f"Chat peer {contact.get('adv_name', pk_hex[:8])} has no known path"
                )
                continue
            if path_len == 0:
                log.debug(
                    f"Chat peer {contact.get('adv_name', pk_hex[:8])} is direct neighbor (flood)"
                )
                continue
            try:
                raw_path = bytes.fromhex(contact.get("out_path", ""))
            except (ValueError, TypeError):
                log.warning(
                    f"Chat peer {contact.get('adv_name', pk_hex[:8])}: "
                    f"invalid out_path {contact.get('out_path')!r}, skipping"
                )
                continue
            # Reverse path for wire format: the meshcore library reverses
            # out_path when sending routed commands (see base.py send_anon_req).
            # CMD_SEND_RAW_DATA is also a routed command, so we reverse here
            # to match the firmware's expected hop order.
            peers[pk_hex] = raw_path[::-1]
            log.info(
                f"Chat peer {contact.get('adv_name', pk_hex[:8])}: "
                f"path_len={path_len}"
            )
        return peers

    def get_peer_path(self):
        """Get the best peer path for directed routing.

        Returns the shortest path (fewest hops) if peers are available,
        or None if no peers have known paths (falls back to flood).

        Note: this picks the shortest path to ANY chat peer, not to a specific
        Reticulum destination. This is correct for point-to-point setups
        (2 nodes + repeaters) but may mis-route in multi-destination meshes.
        For N>2 nodes, use route_mode=flood in the interface config.
        """
        if not self._peer_paths:
            return None
        return min(self._peer_paths.values(), key=len)

    def invalidate_path(self, path: bytes):
        """Remove all peers whose cached path matches the given bytes.

        Called after a directed TX failure so subsequent packets fall back
        to flood until a PATH_UPDATE event restores a fresh route.
        """
        stale = [pk for pk, p in self._peer_paths.items() if p == path]
        for pk in stale:
            del self._peer_paths[pk]
            log.info(f"Invalidated stale path {path.hex()} for peer {pk[:8]}")

    @property
    def peer_count(self):
        """Number of discovered chat peers with known paths."""
        return len(self._peer_paths)

    def _start_keepalive(self):
        """Schedule periodic keep-alive pings on the asyncio loop for BLE connections."""
        self._stop_keepalive()

        async def _ping():
            # First ping immediately to warm up the connection
            while True:
                if self._mc is None or not self._mc.is_connected:
                    break
                try:
                    result = await self._mc.commands.send(
                        b"\x16\x03",
                        [EventType.DEVICE_INFO, EventType.ERROR],
                    )
                    if result.type == EventType.ERROR:
                        log.warning(f"BLE keep-alive got error: {result.payload}")
                        break
                    log.debug("BLE keep-alive ping OK")
                except Exception as e:
                    log.warning(f"BLE keep-alive ping failed: {e}")
                    break
                await asyncio.sleep(BLE_KEEPALIVE_INTERVAL)

            # Notify the interface that BLE disconnected
            if self._disconnect_callback:
                log.warning("BLE keep-alive detected disconnect, notifying interface")
                self._loop.run_in_executor(None, self._disconnect_callback)

        self._keepalive_handle = asyncio.run_coroutine_threadsafe(_ping(), self._loop)

    def _stop_keepalive(self):
        """Cancel the BLE keep-alive task if running."""
        if self._keepalive_handle is not None:
            self._keepalive_handle.cancel()
            self._keepalive_handle = None

    @staticmethod
    def _invoke_callback(cb, payload, snr, rssi):
        """Run a single RX callback with error handling (called in thread pool)."""
        try:
            cb(payload, snr, rssi)
        except Exception as e:
            log.error(f"Raw data callback error: {e}")

    def disconnect(self):
        """Disconnect from MeshCore device and stop the event loop."""
        self._stop_keepalive()
        if self._mc is not None:
            try:
                self._run_coro(self._mc.disconnect(), timeout=10)
            except Exception as e:
                log.error(f"Error during disconnect: {e}")
            self._mc = None

        self._stop_loop()
        self._rx_executor.shutdown(wait=False)

    @property
    def is_connected(self):
        return self._mc is not None and self._mc.is_connected
