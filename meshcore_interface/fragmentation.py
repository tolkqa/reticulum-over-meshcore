"""
Fragmentation and reassembly for Reticulum packets over MeshCore.

MeshCore frames are limited in raw payload size, while Reticulum MTU is
500 bytes. This module provides transparent fragmentation/reassembly.

Fragment header format (5 bytes):
    [msg_id: 4 bytes (random)][frag_idx: 4 bits | total_frags_minus_1: 4 bits]

    - msg_id: random ID to correlate fragments of the same message
    - frag_idx: fragment index (0-15)
    - total_frags_minus_1: total fragment count minus 1 (0-15, so 1-16 fragments)
"""

import os
import time
import threading

# Fragment header size in bytes: 4-byte msg_id + 1-byte frag_info
FRAGMENT_HEADER_SIZE = 5

# Max Reticulum data per fragment (excluding fragment header).
# Empirically measured: MeshCore truncates raw data payloads above ~165 bytes
# on some hardware (Heltec T114 nRF52). Using 160 bytes per fragment gives
# 165-byte fragments on the wire (160 + 5 header), at the observed limit.
# Conservative value of 155 provides a safety margin across firmware/hardware.
FRAGMENT_MAX_PAYLOAD = 155

# Max fragments per message (4-bit field, 0-15 encodes 1-16)
MAX_FRAGMENTS = 16

# Timeout for incomplete reassembly (seconds).
# Must exceed worst-case delivery time: 16 frags × 2.5s delay + airtime ≈ 45s.
REASSEMBLY_TIMEOUT = 60

# Number of recently completed msg_ids to remember for deduplication
DEDUP_BUFFER_SIZE = 64

# Max concurrent pending reassemblies to prevent memory exhaustion under noise/flood
MAX_PENDING = 32


class Fragmenter:
    """Splits Reticulum packets into MeshCore-sized fragments."""

    @staticmethod
    def fragment(data: bytes) -> list:
        """Fragment data into chunks suitable for MeshCore transmission.

        Each returned fragment is prefixed with a 5-byte header.
        Single-packet messages also get a header (total_frags=1).

        Returns:
            List of fragment bytes ready for send_raw().

        Raises:
            ValueError: If data is too large (> MAX_FRAGMENTS * FRAGMENT_MAX_PAYLOAD).
        """
        if len(data) == 0:
            return []

        chunks = []
        for i in range(0, len(data), FRAGMENT_MAX_PAYLOAD):
            chunks.append(data[i : i + FRAGMENT_MAX_PAYLOAD])

        total = len(chunks)
        if total > MAX_FRAGMENTS:
            raise ValueError(
                f"Data too large to fragment: {len(data)} bytes needs "
                f"{total} fragments, max is {MAX_FRAGMENTS}"
            )

        msg_id = os.urandom(4)

        fragments = []
        for idx, chunk in enumerate(chunks):
            # High nibble: fragment index, low nibble: total_frags - 1
            frag_byte = ((idx & 0x0F) << 4) | ((total - 1) & 0x0F)
            header = msg_id + bytes([frag_byte])
            fragments.append(header + chunk)

        return fragments


class Reassembler:
    """Collects fragments and reassembles complete Reticulum packets."""

    def __init__(self):
        self._lock = threading.Lock()
        # msg_id bytes -> {fragments: dict[int, bytes], total: int, timestamp: float}
        self._pending = {}
        # Circular buffer of recently completed msg_ids for dedup
        self._completed = []

    def process(self, fragment: bytes):
        """Process an incoming fragment.

        Returns:
            Complete reassembled data when all fragments are received,
            or None if still waiting for more fragments.
        """
        if len(fragment) < FRAGMENT_HEADER_SIZE:
            return None

        msg_id = fragment[0:4]
        frag_byte = fragment[4]
        frag_idx = (frag_byte >> 4) & 0x0F
        total_frags = (frag_byte & 0x0F) + 1
        payload = fragment[FRAGMENT_HEADER_SIZE:]

        # Reject fragments with out-of-range index
        if frag_idx >= total_frags:
            return None

        with self._lock:
            # Dedup: skip if we already completed this message
            if msg_id in self._completed:
                return None

            # Clean expired incomplete entries
            self._cleanup()

            key = bytes(msg_id)

            if key not in self._pending:
                # Evict oldest entry if at capacity
                if len(self._pending) >= MAX_PENDING:
                    oldest_key = min(self._pending, key=lambda k: self._pending[k]["timestamp"])
                    del self._pending[oldest_key]
                self._pending[key] = {
                    "fragments": {},
                    "total": total_frags,
                    "timestamp": time.monotonic(),
                }

            entry = self._pending[key]

            # If total count is inconsistent, discard the poisoned entry
            # so a legitimate retransmission with the same msg_id can start fresh.
            if entry["total"] != total_frags:
                del self._pending[key]
                return None

            # Store fragment (ignore duplicate fragment indices)
            if frag_idx not in entry["fragments"]:
                entry["fragments"][frag_idx] = payload
                # Refresh timestamp so slow multi-fragment streams don't expire
                entry["timestamp"] = time.monotonic()

            # Check if all fragments received
            if len(entry["fragments"]) == total_frags:
                # Reassemble in order
                data = b""
                for i in range(total_frags):
                    data += entry["fragments"][i]

                # Cleanup and record completion
                del self._pending[key]
                self._completed.append(key)
                if len(self._completed) > DEDUP_BUFFER_SIZE:
                    self._completed.pop(0)

                return data

            return None

    def _cleanup(self):
        """Remove expired pending reassemblies. Must be called with lock held."""
        now = time.monotonic()
        expired = [
            k
            for k, v in self._pending.items()
            if now - v["timestamp"] > REASSEMBLY_TIMEOUT
        ]
        for k in expired:
            del self._pending[k]
