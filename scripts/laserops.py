"""
laserops.py — Core BLE library for NERF LaserOps blasters.

Protocol knowledge is derived exclusively from Android BLE HCI captures.
All semantics marked "inferred" are consistent with observed traffic but
not yet proven; treat them as tentative.

Transport:
  - Write to ATT handle 0x0026 (Write Command, ATT opcode 0x52)
  - Receive notifications from ATT handle 0x0023 (Handle Value Notification)
  - One-time setup write on ATT handle 0x0024 (Write Request) to enable notifications
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

# ---------------------------------------------------------------------------
# BLE identity constants (confirmed from HCI advertisement captures)
# ---------------------------------------------------------------------------

# All LaserOps blasters advertise this exact device name.
DEVICE_NAME = "NerfV"

# 128-bit primary service UUID (advertised in AD type 0x07).
# Use this for reliable filtering — the device name alone may not be unique.
SERVICE_UUID     = "073e1435-85d1-455c-97cd-0b8262f20eac"

# Characteristic UUIDs (confirmed from GATT service discovery)
NOTIFY_CHAR_UUID = "073e1382-85d1-455c-97cd-0b8262f20eac"  # Notify + Read
WRITE_CHAR_UUID  = "073e1383-85d1-455c-97cd-0b8262f20eac"  # Write Without Response + Read

# ---------------------------------------------------------------------------
# ATT handle constants (confirmed from captures)
# ---------------------------------------------------------------------------

NOTIFY_HANDLE = 0x0023  # gun → host notifications
WRITE_HANDLE  = 0x0026  # host → gun write commands
SETUP_HANDLE  = 0x0024  # one-time setup write (enables notifications)

# ---------------------------------------------------------------------------
# Message ID bytes (leading byte of each payload family)
# ---------------------------------------------------------------------------

# Startup exchange (confirmed)
MSG_STARTUP_QUERY    = 0x35   # host → gun: 1 byte
MSG_STARTUP_SNAPSHOT = 0x35   # gun → host: 13 bytes

# Config write (confirmed structure)
MSG_CONFIG_WRITE     = 0x36   # host → gun: 13 bytes
MSG_APPLY_COMMIT     = 0x57   # host → gun: 1 byte (inferred meaning)

# Volume control (confirmed)
MSG_VOLUME_SET       = 0x5B   # host → gun: 2 bytes, XX = 0x00–0x1f

# Status poll/reply (high confidence)
MSG_STATUS_POLL      = 0x51   # host → gun: 1 byte
MSG_STATUS_REPLY     = 0x51   # gun → host: 3 bytes

# Gameplay events (gun → host, confirmed)
MSG_TRIGGER          = 0x49   # 1 byte
MSG_RELOAD_A         = 0x52   # 1 byte, paired with reload_B ~0.5 s later
MSG_RELOAD_B_OLD     = 0x31   # 2 bytes: 31 0a (older mode)
MSG_RELOAD_B_NEW     = 0x31   # 2 bytes: 31 0d (newer/level-5 mode)
MSG_AMMO_STATE       = 0x32   # variable, per-shot counter update

# In-game control (host → gun, medium confidence)
MSG_GAME_CTRL        = 0x37   # 4 bytes

# End-of-game stats (inferred semantics)
MSG_STAT_REQUEST     = 0x5A   # host → gun: 6 bytes, prefix 5a3f01
MSG_STAT_COUNTER     = 0x30   # gun → host: 5 bytes, prefix 30013f
MSG_STAT_TERMINAL    = 0x3E   # gun → host: 3e0100

# Session close (low-medium confidence)
MSG_SESSION_CLOSE    = 0x42   # host → gun: 1 byte
MSG_ROUND_SHOTS      = 0x47   # gun → host: 3 bytes, end-of-round shot counter
MSG_ROUND_SLOT_STATS = 0x54   # bidirectional, per-slot hits/kills

# ---------------------------------------------------------------------------
# Volume constants (confirmed)
# ---------------------------------------------------------------------------

VOLUME_MIN = 0x00  # mute
VOLUME_MAX = 0x1F  # 31 decimal

# Assignment bounds observed in captures.
# - slots are seen in low single digits
# - teams are seen up to 2, with test_12 showing 2 for all-vs-all
ASSIGNMENT_SLOT_MIN = 0x00
ASSIGNMENT_SLOT_MAX = 0x0A
ASSIGNMENT_TEAM_MIN = 0x00
ASSIGNMENT_TEAM_MAX = 0x02

# AR/single-player setup sequence (observed in Tests 2-7; semantics inferred).
# Keep this AR-only to avoid mixing mode-specific control traffic into multiplayer.
DEFAULT_GAME_ASSIGNMENT_SLOT = 0x01
DEFAULT_GAME_ASSIGNMENT_TEAM = 0x00
DEFAULT_AR_GAME_SETUP_PREFIX: tuple[bytes, ...] = (
    bytes.fromhex("4401"),
)
DEFAULT_AR_GAME_SETUP_SUFFIX: tuple[bytes, ...] = (
    bytes.fromhex("4a0000000000000000001388"),
    bytes.fromhex("411388"),
)

# Multiplayer game setup (observed in Tests 8-9, 11-12; semantics inferred).
DEFAULT_MULTIPLAYER_SLOT = 0x02
DEFAULT_MULTIPLAYER_TEAM = 0x02
DEFAULT_MULTIPLAYER_DURATION_SECONDS = 300

# Safety defaults for write-without-response traffic.
# - Only allow known command ids in safe mode.
# - Space writes to reduce burst pressure on firmware.
SAFE_ALLOWED_WRITE_PREFIXES: frozenset[int] = frozenset({
    MSG_STARTUP_QUERY,   # 0x35
    MSG_CONFIG_WRITE,    # 0x36
    MSG_APPLY_COMMIT,    # 0x57
    MSG_VOLUME_SET,      # 0x5b
    MSG_STATUS_POLL,     # 0x51
    MSG_STAT_REQUEST,    # 0x5a
    MSG_SESSION_CLOSE,   # 0x42
    0x44, 0x49, 0x4A, 0x41, 0x3B, 0x39, 0x37,  # observed game controls
    0x58,  # multiplayer round start / arm
    0x54,  # multiplayer per-slot stat request
})
SAFE_DEFAULT_MIN_WRITE_INTERVAL = 0.08

# Name-id bounds observed in captures (see protocol docs).
# The app uses separate first/second name lists, so keep separate limits.
NAME_A_ID_MIN = 0x00
NAME_A_ID_MAX = 0x32
NAME_B_ID_MIN = 0x00
NAME_B_ID_MAX = 0x32

# ---------------------------------------------------------------------------
# Data classes for decoded gun→host messages
# ---------------------------------------------------------------------------

@dataclass
class StartupSnapshot:
    """
    Decoded gun startup snapshot (message id 0x35, 13 bytes).

    Fields confirmed: raw bytes 8–10 contain level + two name-part bytes.
    Exact field names are inferred.
    """
    level: int       # byte 8  — persistent gun level (high confidence)
    name_a: int      # byte 9  — name part A (inferred)
    name_b: int      # byte 10 — name part B (inferred)
    raw: bytes       # full 13-byte payload for reference


@dataclass
class StatusReply:
    """Gun status reply (message id 0x51, 3 bytes).  Semantics inferred."""
    status_word: int   # bytes 1–2 as big-endian uint16 (drifts over time)
    raw: bytes


@dataclass
class StatCounterReply:
    """End-of-game stat counter (prefix 30013f, 5 bytes).  Semantics inferred."""
    stat_type: int   # byte 3 — matches the TT byte from the request
    counter: int     # byte 4 — descends to 00


@dataclass
class RoundSlotStatsReply:
    """
    Multiplayer round per-slot stats reply.

    Payload format: ``54 00 HH KK SS`` where
      - HH = hits
      - KK = kills
      - SS = slot echo
    """
    slot: int
    hits: int
    kills: int
    raw: bytes


@dataclass
class BlasterConfiguration:
    """Current host-side model of a blaster's writable configuration."""
    level: int = 1
    name_a: int = 0
    name_b: int = 0
    volume: int = VOLUME_MAX


# ---------------------------------------------------------------------------
# Message decoders
# ---------------------------------------------------------------------------

def decode_startup_snapshot(payload: bytes) -> Optional[StartupSnapshot]:
    """
    Decode a gun→host notification as a startup snapshot if it matches.

    Returns None if the payload does not have the expected 13-byte structure.
    """
    if len(payload) != 13 or payload[0] != MSG_STARTUP_SNAPSHOT:
        return None
    return StartupSnapshot(
        level=payload[8],
        name_a=payload[9],
        name_b=payload[10],
        raw=bytes(payload),
    )


def decode_status_reply(payload: bytes) -> Optional[StatusReply]:
    """Decode a 3-byte status reply notification."""
    if len(payload) != 3 or payload[0] != MSG_STATUS_REPLY:
        return None
    status_word = (payload[1] << 8) | payload[2]
    return StatusReply(status_word=status_word, raw=bytes(payload))


def decode_stat_counter(payload: bytes) -> Optional[StatCounterReply]:
    """Decode a 5-byte end-of-game stat counter notification (prefix 30 01 3f)."""
    if len(payload) != 5 or payload[:3] != bytes([0x30, 0x01, 0x3F]):
        return None
    return StatCounterReply(stat_type=payload[3], counter=payload[4])


def decode_round_slot_stats(payload: bytes) -> Optional[RoundSlotStatsReply]:
    """Decode a multiplayer per-slot stats reply (54 00 HH KK SS)."""
    if len(payload) != 5 or payload[0] != MSG_ROUND_SLOT_STATS or payload[1] != 0x00:
        return None
    return RoundSlotStatsReply(
        slot=payload[4],
        hits=payload[2],
        kills=payload[3],
        raw=bytes(payload),
    )


def decode_notification(payload: bytes) -> str:
    """Human-readable decoding of any known gun→host notification payload."""
    h = payload.hex()

    snapshot = decode_startup_snapshot(payload)
    if snapshot is not None:
        return (
            f"startup_snapshot level={snapshot.level} "
            f"name_parts=(0x{snapshot.name_a:02x}, 0x{snapshot.name_b:02x}) "
            f"raw={h}"
        )

    status = decode_status_reply(payload)
    if status is not None:
        return f"status_reply 0x{status.status_word:04x} raw={h}"

    if h == "49":
        return "trigger_event"

    if h == "52":
        return "reload_marker_a"

    if len(payload) == 2 and payload[0] == 0x31:
        return f"reload_marker_b variant=0x{payload[1]:02x} raw={h}"

    if payload[0:1] == b"\x32" and len(payload) >= 2:
        return f"ammo_state family=0x{payload[1]:02x} counter=0x{payload[-1]:02x} raw={h}"

    if len(payload) == 3 and payload[0] == MSG_ROUND_SHOTS:
        shots = (payload[1] << 8) | payload[2]
        return f"round_shot_counter shots={shots} raw={h}"

    if h == "3f":
        return "respawn_ready_marker raw=3f (inferred)"

    if (
        len(payload) == 5
        and payload[0] == 0x30
        and payload[:3] != bytes([0x30, 0x01, 0x3F])
    ):
        return (
            f"life_state_update mode_a=0x{payload[1]:02x} mode_b=0x{payload[2]:02x} "
            f"family=0x{payload[3]:02x} "
            f"counter=0x{payload[4]:02x} raw={h} (inferred)"
        )

    if len(payload) == 3 and payload[0] == 0x3E and h != "3e0100":
        return (
            f"life_state_marker family=0x{payload[1]:02x} "
            f"counter=0x{payload[2]:02x} raw={h} (inferred)"
        )

    round_slot = decode_round_slot_stats(payload)
    if round_slot is not None:
        return (
            f"round_slot_stats slot={round_slot.slot} "
            f"hits={round_slot.hits} kills={round_slot.kills} raw={h}"
        )

    stat = decode_stat_counter(payload)
    if stat is not None:
        return f"stat_counter type=0x{stat.stat_type:02x} value=0x{stat.counter:02x} raw={h}"

    if h == "3e0100":
        return "stat_terminal (end-of-game stats complete)"

    return f"unknown raw={h}"


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def build_startup_query() -> bytes:
    """Single-byte startup query sent to the gun at session start."""
    return bytes([MSG_STARTUP_QUERY])


def clamp_name_a_id(value: int) -> int:
    """Clamp first-name ID byte to observed supported bounds."""
    return max(NAME_A_ID_MIN, min(NAME_A_ID_MAX, value))


def clamp_name_b_id(value: int) -> int:
    """Clamp second-name ID byte to observed supported bounds."""
    return max(NAME_B_ID_MIN, min(NAME_B_ID_MAX, value))


def clamp_u8(value: int) -> int:
    """Clamp arbitrary integer to an unsigned byte."""
    return max(0x00, min(0xFF, int(value)))


def build_config_write(
    level: int,
    name_a: int,
    name_b: int,
    *,
    ammo_profile: int = 0x0A,
    damage_profile: int = 0x02,
    profile_byte4: int = 0x02,
    profile_byte5: int = 0x03,
    byte7_selector: int = 0x00,
    health_profile: int = 0x0A,
    reserved_byte11: int = 0x00,
    tail_profile: int = 0x04,
) -> bytes:
    """
    Build a 13-byte config write payload (form A, host → gun).

    Observed in Test 1 when assigning level and name parts to guns.
    ``name_a`` and ``name_b`` are the raw byte values for bytes 9 and 10
    of the payload.  Based on captures, these appear to be (app_name_index + 1)
    for each of the two name words, but this relationship is inferred.

    Profile bytes can be overridden for different blaster families.
    """
    safe_name_a = clamp_name_a_id(name_a)
    safe_name_b = clamp_name_b_id(name_b)
    return bytes([
        MSG_CONFIG_WRITE,
        0x00,
        clamp_u8(ammo_profile),
        clamp_u8(damage_profile),
        clamp_u8(profile_byte4),
        clamp_u8(profile_byte5),
        clamp_u8(byte7_selector),
        clamp_u8(health_profile),
        level & 0xFF,
        safe_name_a & 0xFF,
        safe_name_b & 0xFF,
        clamp_u8(reserved_byte11),
        clamp_u8(tail_profile),
    ])


def build_apply_commit() -> bytes:
    """Single-byte apply/commit command (inferred meaning)."""
    return bytes([MSG_APPLY_COMMIT])


def build_volume_set(volume: int) -> bytes:
    """
    Build a 2-byte volume-set command.

    ``volume`` must be 0 (mute) to 31 (max).  Values outside this range
    are clamped.  Confirmed by Test 4 sweep data.
    """
    v = max(VOLUME_MIN, min(VOLUME_MAX, volume))
    return bytes([MSG_VOLUME_SET, v])


def build_status_poll() -> bytes:
    """Single-byte periodic status poll."""
    return bytes([MSG_STATUS_POLL])


def build_stat_request(stat_type: int) -> bytes:
    """
    Build a 6-byte end-of-game stat request.

    Observed ``stat_type`` values from captures: 0x01, 0x02, 0x06.
    Semantics of each type are inferred.
    """
    return bytes([0x5A, 0x3F, 0x01, stat_type & 0xFF, 0x00, 0x00])


def build_session_close() -> bytes:
    """Single-byte session close command (low-medium confidence)."""
    return bytes([MSG_SESSION_CLOSE])


def build_round_slot_stats_request(slot: int) -> bytes:
    """Build multiplayer per-slot stats request ``54 SS``."""
    safe_slot = max(0, min(0xFF, int(slot)))
    return bytes([MSG_ROUND_SLOT_STATS, safe_slot])


def clamp_assignment_slot(value: int) -> int:
    """Clamp assignment slot to known-safe observed bounds."""
    return max(ASSIGNMENT_SLOT_MIN, min(ASSIGNMENT_SLOT_MAX, int(value)))


def clamp_assignment_team(value: int) -> int:
    """Clamp assignment team to known-safe observed bounds."""
    return max(ASSIGNMENT_TEAM_MIN, min(ASSIGNMENT_TEAM_MAX, int(value)))


def build_assignment(slot: int, team: int) -> bytes:
    """Build assignment payload ``49 SS TT`` with safe bounds."""
    safe_slot = clamp_assignment_slot(slot)
    safe_team = clamp_assignment_team(team)
    return bytes([0x49, safe_slot, safe_team])


def build_game_setup_commands(
    slot: int = DEFAULT_GAME_ASSIGNMENT_SLOT,
    team: int = DEFAULT_GAME_ASSIGNMENT_TEAM,
    *,
    ar_setup_prefix: tuple[bytes, ...] = DEFAULT_AR_GAME_SETUP_PREFIX,
    ar_setup_suffix: tuple[bytes, ...] = DEFAULT_AR_GAME_SETUP_SUFFIX,
) -> tuple[bytes, ...]:
    """
    Build AR/single-player setup sequence.

    ``49`` assignment packet encodes ``slot`` and ``team`` as bytes 1 and 2.
    Prefix/suffix commands are mode-specific and optional.
    """
    assignment = build_assignment(slot=slot, team=team)
    return tuple(ar_setup_prefix) + (assignment,) + tuple(ar_setup_suffix)


def build_multiplayer_assignment(
    slot: int = DEFAULT_MULTIPLAYER_SLOT,
    team: int = DEFAULT_MULTIPLAYER_TEAM,
) -> bytes:
    """
    Build multiplayer assignment payload ``49 SS TT``.

    ``SS`` is the player slot index, ``TT`` is the team/side index.
    """
    return build_assignment(slot=slot, team=team)


def build_game_mode_init() -> bytes:
    """Build AR-only mode-init command ``44 01``."""
    return bytes.fromhex("4401")


def build_multiplayer_round_config(
    duration_seconds: int = DEFAULT_MULTIPLAYER_DURATION_SECONDS,
) -> bytes:
    """
    Build multiplayer round config payload:
    ``4a 00 0a ff DD DD 00 00 00 00 27 10``.
    """
    safe_duration = max(1, min(0xFFFF, int(duration_seconds)))
    return bytes([
        0x4A,
        0x00, 0x0A, 0xFF,
        (safe_duration >> 8) & 0xFF,
        safe_duration & 0xFF,
        0x00, 0x00, 0x00, 0x00,
        0x27, 0x10,
    ])


def build_multiplayer_round_start() -> bytes:
    """Build multiplayer round start/arm command ``58``."""
    return bytes([0x58])


# ---------------------------------------------------------------------------
# Handle resolver (mirrors the approach in definition_protocol/example_ble_protocol_client.py)
# ---------------------------------------------------------------------------

def find_char_by_handle(client: BleakClient, handle: int):
    """Return the GATT characteristic whose ATT handle matches ``handle``."""
    if client.services is None:
        return None
    for service in client.services:
        for char in service.characteristics:
            if char.handle == handle:
                return char
    return None


def find_char_by_uuid(client: BleakClient, uuid: str):
    """Return the first GATT characteristic whose UUID matches ``uuid``."""
    if client.services is None:
        return None
    needle = uuid.lower()
    for service in client.services:
        for char in service.characteristics:
            if char.uuid.lower() == needle:
                return char
    return None


def find_notify_char_by_properties(client: BleakClient):
    """Fallback for firmware exposing compatible properties under other UUIDs."""
    if client.services is None:
        return None
    for service in client.services:
        for char in service.characteristics:
            props = {str(prop).lower() for prop in char.properties}
            if "notify" in props:
                return char
    return None


def find_write_char_by_properties(client: BleakClient):
    """Fallback for firmware exposing compatible write properties under other UUIDs."""
    if client.services is None:
        return None
    candidates = []
    for service in client.services:
        for char in service.characteristics:
            props = {str(prop).lower() for prop in char.properties}
            if "write-without-response" in props or "write" in props:
                candidates.append((char, props))
    for char, props in candidates:
        if "write-without-response" in props and "notify" not in props:
            return char
    for char, props in candidates:
        if "write-without-response" in props:
            return char
    for char, props in candidates:
        if "write" in props and "notify" not in props:
            return char
    return candidates[0][0] if candidates else None


def describe_characteristics(client: BleakClient) -> str:
    """Return a compact overview of discovered characteristics for diagnostics."""
    if client.services is None:
        return "none"
    details: list[str] = []
    for service in client.services:
        for char in service.characteristics:
            props = ",".join(char.properties)
            details.append(
                f"0x{char.handle:04x}:{char.uuid}({props})"
            )
    return "; ".join(details) if details else "none"


# ---------------------------------------------------------------------------
# High-level device class
# ---------------------------------------------------------------------------

NotificationCallback = Callable[[bytes], None]
DisconnectedCallback = Callable[[], None]
WriteCallback = Callable[[bytes], None]


class LaserOpsDevice:
    """
    Async context manager representing a BLE connection to a LaserOps blaster.

    The protocol uses two ATT handles:
      - 0x0023 for gun→host notifications
      - 0x0026 for host→gun write commands

    Usage::

        async with LaserOpsDevice(ble_device) as gun:
            snapshot = await gun.startup()
            print(f"Level: {snapshot.level}")
            await gun.set_volume(0)
    """

    def __init__(
        self,
        device: BLEDevice,
        notification_callback: Optional[NotificationCallback] = None,
        disconnected_callback: Optional[DisconnectedCallback] = None,
        write_callback: Optional[WriteCallback] = None,
        *,
        safe_mode: bool = True,
        min_write_interval: float = SAFE_DEFAULT_MIN_WRITE_INTERVAL,
    ) -> None:
        self._device = device
        self._client = BleakClient(
            device,
            disconnected_callback=self._on_disconnected_client,
        )
        self._notification_callback = notification_callback
        self._disconnected_callback = disconnected_callback
        self._write_callback = write_callback
        self._pending: dict[bytes, asyncio.Future] = {}
        self._notify_char = None
        self._write_char = None
        self._safe_mode = safe_mode
        self._min_write_interval = max(0.0, float(min_write_interval))
        self._last_write_at = 0.0
        self._write_lock = asyncio.Lock()
        self._intentional_disconnect = False

    # ---- context manager --------------------------------------------------

    async def __aenter__(self) -> "LaserOpsDevice":
        self._intentional_disconnect = False
        try:
            await self._client.connect()

            # Ensure service discovery is complete before characteristic lookup.
            # Bleak API differs by version: some expose get_services(), others
            # populate services during connect without this method.
            if hasattr(self._client, "get_services"):
                await self._client.get_services()

            self._notify_char = find_char_by_handle(self._client, NOTIFY_HANDLE)
            self._write_char  = find_char_by_handle(self._client, WRITE_HANDLE)

            # ATT handles can differ across firmware revisions; UUIDs are stable.
            if self._notify_char is None:
                self._notify_char = find_char_by_uuid(self._client, NOTIFY_CHAR_UUID)
            if self._write_char is None:
                self._write_char = find_char_by_uuid(self._client, WRITE_CHAR_UUID)

            # Some blasters expose compatible notify/write characteristics under
            # vendor-specific UUIDs instead of the captured LaserOps UUIDs.
            if self._notify_char is None:
                self._notify_char = find_notify_char_by_properties(self._client)
            if self._write_char is None:
                self._write_char = find_write_char_by_properties(self._client)

            # If services are unavailable on this backend/version, use UUID strings
            # directly because Bleak accepts UUID specifiers for I/O calls.
            if self._notify_char is None and self._client.services is None:
                self._notify_char = NOTIFY_CHAR_UUID
            if self._write_char is None and self._client.services is None:
                self._write_char = WRITE_CHAR_UUID

            if self._notify_char is None:
                raise RuntimeError(
                    "Notify characteristic not found "
                    f"(expected handle 0x{NOTIFY_HANDLE:04x} or UUID {NOTIFY_CHAR_UUID}). "
                    f"Discovered characteristics: {describe_characteristics(self._client)}"
                )
            if self._write_char is None:
                raise RuntimeError(
                    "Write characteristic not found "
                    f"(expected handle 0x{WRITE_HANDLE:04x} or UUID {WRITE_CHAR_UUID}). "
                    f"Discovered characteristics: {describe_characteristics(self._client)}"
                )

            await self._client.start_notify(self._notify_char, self._on_notification)
            return self
        except Exception:
            self._fail_pending(RuntimeError("ble connection setup failed"))
            try:
                if self._client.is_connected:
                    await self._client.disconnect()
            except Exception:
                pass
            raise

    async def __aexit__(self, *_) -> None:
        self._intentional_disconnect = True
        self._fail_pending(RuntimeError("ble connection is closing"))
        try:
            if self._notify_char is not None:
                await self._client.stop_notify(self._notify_char)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        finally:
            self._intentional_disconnect = False

    # ---- internals --------------------------------------------------------

    def _on_notification(self, _handle: int, data: bytearray) -> None:
        payload = bytes(data)

        # Resolve any waiting futures keyed on payload prefix
        prefix = payload[:3] if len(payload) >= 3 else payload
        for key in list(self._pending.keys()):
            if payload[:len(key)] == key:
                fut = self._pending.pop(key)
                if not fut.done():
                    fut.set_result(payload)
                break

        if self._notification_callback:
            self._notification_callback(payload)

    def _on_disconnected_client(self, _client: BleakClient) -> None:
        self._fail_pending(ConnectionError("ble link disconnected"))
        if self._intentional_disconnect:
            return
        if self._disconnected_callback is None:
            return
        try:
            self._disconnected_callback()
        except Exception:
            pass

    def _fail_pending(self, exc: Exception) -> None:
        for key in list(self._pending.keys()):
            fut = self._pending.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_exception(exc)

    async def _write(self, data: bytes) -> None:
        if not data:
            raise ValueError("empty payload is not allowed")
        if not self.is_connected:
            raise ConnectionError("ble link is not connected")
        if self._safe_mode and data[0] not in SAFE_ALLOWED_WRITE_PREFIXES:
            raise ValueError(
                f"unsafe command blocked in safe_mode: 0x{data[0]:02x}"
            )

        loop = asyncio.get_running_loop()
        async with self._write_lock:
            if self._min_write_interval > 0 and self._last_write_at > 0:
                elapsed = loop.time() - self._last_write_at
                if elapsed < self._min_write_interval:
                    await asyncio.sleep(self._min_write_interval - elapsed)
            await self._client.write_gatt_char(
                self._write_char, data, response=False
            )
            self._last_write_at = loop.time()
            if self._write_callback is not None:
                try:
                    self._write_callback(data)
                except Exception:
                    pass

    async def _write_and_wait(
        self,
        data: bytes,
        response_prefix: bytes,
        timeout: float = 5.0,
    ) -> bytes:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        if response_prefix in self._pending:
            raise RuntimeError(
                f"duplicate in-flight request for response prefix {response_prefix.hex()}"
            )
        self._pending[response_prefix] = fut
        try:
            await self._write(data)
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(response_prefix, None)

    # ---- public API -------------------------------------------------------

    @property
    def address(self) -> str:
        return self._device.address

    @property
    def name(self) -> str:
        return self._device.name or self._device.address

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected)

    async def query_startup_snapshot(self, timeout: float = 5.0) -> StartupSnapshot:
        """Query and decode the startup snapshot without changing gun volume."""
        response = await self._write_and_wait(
            build_startup_query(),
            response_prefix=bytes([MSG_STARTUP_SNAPSHOT]),
            timeout=timeout,
        )
        snapshot = decode_startup_snapshot(response)
        if snapshot is None:
            raise RuntimeError(
                f"Unexpected startup response: {response.hex()}"
            )
        return snapshot

    async def startup(self, volume: int = VOLUME_MAX) -> StartupSnapshot:
        """
        Perform the confirmed startup exchange:
          1. Send startup query (0x35)
          2. Wait for startup snapshot (0x35 ... 13 bytes)
          3. Send initial volume (0x5b XX)

        Returns the decoded StartupSnapshot.
        """
        snapshot = await self.query_startup_snapshot(timeout=5.0)
        await self._write(build_volume_set(volume))
        return snapshot

    async def write_config(
        self,
        level: int,
        name_a: int,
        name_b: int,
        *,
        ammo_profile: int = 0x0A,
        damage_profile: int = 0x02,
        profile_byte4: int = 0x02,
        profile_byte5: int = 0x03,
        byte7_selector: int = 0x00,
        health_profile: int = 0x0A,
        reserved_byte11: int = 0x00,
        tail_profile: int = 0x04,
    ) -> None:
        """
        Write configuration to the gun (0x36 config write + 0x57 apply).

        ``name_a`` and ``name_b`` are the raw byte values to embed in the
        config-write payload (bytes 9 and 10).  Based on capture evidence,
        these appear to equal (app_name_index + 1) for each selected name
        word, but this relationship is inferred.
        """
        await self._write(
            build_config_write(
                level,
                name_a,
                name_b,
                ammo_profile=ammo_profile,
                damage_profile=damage_profile,
                profile_byte4=profile_byte4,
                profile_byte5=profile_byte5,
                byte7_selector=byte7_selector,
                health_profile=health_profile,
                reserved_byte11=reserved_byte11,
                tail_profile=tail_profile,
            )
        )
        await self._write(build_apply_commit())

    async def send_game_setup(
        self,
        delay: float = 0.05,
        *,
        slot: int = DEFAULT_GAME_ASSIGNMENT_SLOT,
        team: int = DEFAULT_GAME_ASSIGNMENT_TEAM,
    ) -> None:
        """
        Send the observed game-start setup sequence for single-player AR mode.

        Command semantics are inferred from captures; byte order is preserved.
        """
        for payload in build_game_setup_commands(slot=slot, team=team):
            await self._write(payload)
            if delay > 0:
                await asyncio.sleep(delay)

    async def send_multiplayer_game_setup(
        self,
        *,
        slot: int = DEFAULT_MULTIPLAYER_SLOT,
        team: int = DEFAULT_MULTIPLAYER_TEAM,
        volume: int = VOLUME_MAX,
        duration_seconds: int = DEFAULT_MULTIPLAYER_DURATION_SECONDS,
        delay: float = 0.05,
    ) -> StartupSnapshot:
        """
        Send multiplayer setup sequence observed in Tests 8-9 and 11-12:

          1) 49 SS TT
          2) 4a 00 0a ff DD DD 00 00 00 00 27 10
          3) 5b XX
          4) 35 and wait for startup snapshot
          5) 58

        Returns the startup snapshot from step 4.
        """
        snapshot = await self.prepare_multiplayer_game_setup(
            slot=slot,
            team=team,
            volume=volume,
            duration_seconds=duration_seconds,
            delay=delay,
        )
        await self.arm_multiplayer_round()
        return snapshot

    async def prepare_multiplayer_game_setup(
        self,
        *,
        slot: int = DEFAULT_MULTIPLAYER_SLOT,
        team: int = DEFAULT_MULTIPLAYER_TEAM,
        volume: int = VOLUME_MAX,
        duration_seconds: int = DEFAULT_MULTIPLAYER_DURATION_SECONDS,
        delay: float = 0.05,
    ) -> StartupSnapshot:
        """
        Send multiplayer pre-start sequence and return startup snapshot.

        Sequence:
          1) 49 SS TT
          2) 4a 00 0a ff DD DD 00 00 00 00 27 10
          3) 5b XX
          4) 35 and wait for startup snapshot
        """
        await self._write(build_multiplayer_assignment(slot=slot, team=team))
        if delay > 0:
            await asyncio.sleep(delay)

        await self._write(
            build_multiplayer_round_config(duration_seconds=duration_seconds)
        )
        if delay > 0:
            await asyncio.sleep(delay)

        await self._write(build_volume_set(volume))
        if delay > 0:
            await asyncio.sleep(delay)

        snapshot = await self.query_startup_snapshot(timeout=5.0)
        return snapshot

    async def arm_multiplayer_round(self) -> None:
        """Send multiplayer round start/arm command ``58``."""
        await self._write(build_multiplayer_round_start())

    async def set_volume(self, volume: int) -> None:
        """Set gun volume (0 = mute, 31 = max).  Confirmed by Test 4."""
        await self._write(build_volume_set(volume))

    async def poll_status(self) -> bytes:
        """Send a status poll and return the raw 3-byte status reply."""
        return await self._write_and_wait(
            build_status_poll(),
            response_prefix=bytes([MSG_STATUS_REPLY, ]),
            timeout=5.0,
        )

    async def collect_stats(
        self,
        stat_types: tuple[int, ...] = (0x01, 0x02, 0x06),
        per_type_timeout: float = 5.0,
    ) -> list[list[StatCounterReply]]:
        """
        Request end-of-game statistics for each stat type and collect replies.

        Sends ``5a3f01 TT 0000`` for each TT in ``stat_types``, then
        collects ``30013f TT NN`` replies until the terminal marker (3e0100)
        is seen or a timeout elapses.

        Returns a list of lists (one per stat_type) of StatCounterReply objects.
        """
        results: list[list[StatCounterReply]] = []
        terminal = bytes([0x3E, 0x01, 0x00])

        for stat_type in stat_types:
            replies: list[StatCounterReply] = []
            done = asyncio.Event()

            original_cb = self._notification_callback

            def _cb(payload: bytes) -> None:
                sc = decode_stat_counter(payload)
                if sc is not None and sc.stat_type == stat_type:
                    replies.append(sc)
                if payload == terminal:
                    done.set()
                if original_cb:
                    original_cb(payload)

            self._notification_callback = _cb
            try:
                await self._write(build_stat_request(stat_type))
                try:
                    await asyncio.wait_for(done.wait(), timeout=per_type_timeout)
                except asyncio.TimeoutError:
                    pass
            finally:
                self._notification_callback = original_cb

            results.append(replies)

        return results

    async def close_session(self) -> None:
        """Send the session-close command (0x42, low-medium confidence)."""
        await self._write(build_session_close())

    async def request_round_slot_stats(
        self,
        slot: int,
        *,
        timeout: float = 3.0,
        max_attempts: int = 4,
    ) -> RoundSlotStatsReply:
        """
        Request multiplayer round stats for one slot via ``54 SS``.

        Expects response ``54 00 HH KK SS``.
        """
        safe_slot = max(0, min(0xFF, int(slot)))
        attempts = max(1, int(max_attempts))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(timeout))
        last_error: str | None = None

        for _ in range(attempts):
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            payload = await self._write_and_wait(
                build_round_slot_stats_request(safe_slot),
                response_prefix=bytes([MSG_ROUND_SLOT_STATS, 0x00]),
                timeout=remaining,
            )
            parsed = decode_round_slot_stats(payload)
            if parsed is None:
                last_error = f"invalid round slot stats reply: {payload.hex()}"
                continue
            if int(parsed.slot) != safe_slot:
                last_error = (
                    f"round slot stats mismatch: requested={safe_slot} "
                    f"received={int(parsed.slot)} raw={payload.hex()}"
                )
                continue
            return parsed

        if last_error is None:
            raise RuntimeError(
                f"round slot stats timeout for slot {safe_slot} "
                f"after {attempts} attempts"
            )
        raise RuntimeError(last_error)

    async def collect_round_slot_stats(
        self,
        slots: tuple[int, ...],
        *,
        per_slot_timeout: float = 3.0,
    ) -> list[RoundSlotStatsReply]:
        """Collect multiplayer per-slot round stats for all requested slots."""
        replies: list[RoundSlotStatsReply] = []
        for slot in slots:
            replies.append(
                await self.request_round_slot_stats(slot, timeout=per_slot_timeout)
            )
        return replies


class BlasterState:
    """
    Host-side state model for a connected blaster.

    ``sync_up`` reads the gun startup snapshot into this object.
    ``sync_down`` writes this object's config/volume back to the gun.
    """

    def __init__(
        self,
        gun: LaserOpsDevice,
        config: Optional[BlasterConfiguration] = None,
    ) -> None:
        self._gun = gun
        self.config = config or BlasterConfiguration()
        self.last_snapshot: Optional[StartupSnapshot] = None

    async def sync_up(self, timeout: float = 5.0) -> StartupSnapshot:
        """Pull config-like state from the gun into this object."""
        snapshot = await self._gun.query_startup_snapshot(timeout=timeout)
        self.config.level = snapshot.level
        self.config.name_a = snapshot.name_a
        self.config.name_b = snapshot.name_b
        self.last_snapshot = snapshot
        return snapshot

    async def sync_down(
        self,
        *,
        write_config: bool = True,
        write_volume: bool = True,
    ) -> None:
        """Push host-side state from this object to the gun."""
        if write_config:
            await self._gun.write_config(
                level=self.config.level,
                name_a=self.config.name_a,
                name_b=self.config.name_b,
            )
        if write_volume:
            await self._gun.set_volume(self.config.volume)

    # Backward-compatible alias in case callers prefer the spelling "synch".
    async def synch_up(self, timeout: float = 5.0) -> StartupSnapshot:
        return await self.sync_up(timeout=timeout)

    async def synch_down(
        self,
        *,
        write_config: bool = True,
        write_volume: bool = True,
    ) -> None:
        await self.sync_down(write_config=write_config, write_volume=write_volume)


# ---------------------------------------------------------------------------
# Discovery helper
# ---------------------------------------------------------------------------

async def stream_discovered_devices(
    timeout: Optional[float] = None,
    name_filter: str = DEVICE_NAME,
    use_service_uuid: bool = True,
    poll_interval: float = 0.2,
    rediscover_on_reappear: bool = False,
    lost_after: float = 8.0,
) -> AsyncIterator[BLEDevice]:
    """
    Yield matching devices asynchronously as soon as they are discovered.

    Use ``timeout=None`` (or <= 0) for an open-ended stream where caller code
    decides when enough devices are connected and breaks out of the loop.

    If ``rediscover_on_reappear`` is True, a device is considered "lost" after
    ``lost_after`` seconds without advertisements and will be yielded again if
    it reappears (for example after power-cycle/restart).
    """
    needle = name_filter.lower()

    def _matches(device: BLEDevice) -> bool:
        if use_service_uuid:
            return True
        return bool(device.name and needle in device.name.lower())

    scanner_kwargs = {"service_uuids": [SERVICE_UUID]} if use_service_uuid else {}
    queue: asyncio.Queue[BLEDevice] = asyncio.Queue()
    last_seen_at: dict[str, float] = {}
    active_devices: dict[str, BLEDevice] = {}
    discovered_once: set[str] = set()

    loop = asyncio.get_event_loop()

    def _on_detect(device: BLEDevice, *_args) -> None:
        addr = device.address.lower()
        last_seen_at[addr] = loop.time()
        queue.put_nowait(device)

    scanner = BleakScanner(detection_callback=_on_detect, **scanner_kwargs)

    deadline: Optional[float] = None
    if timeout is not None and timeout > 0:
        deadline = loop.time() + timeout

    await scanner.start()
    try:
        while True:
            now = loop.time()

            if rediscover_on_reappear and lost_after > 0:
                for addr in list(active_devices.keys()):
                    last_seen = last_seen_at.get(addr, now)
                    if now - last_seen > lost_after:
                        active_devices.pop(addr, None)

            while not queue.empty():
                device = queue.get_nowait()
                address = device.address.lower()

                if not _matches(device):
                    continue

                if rediscover_on_reappear:
                    # Yield when device first appears in the currently active set.
                    is_new_active = address not in active_devices
                    active_devices[address] = device
                    if is_new_active:
                        yield device
                else:
                    if address in discovered_once:
                        continue
                    discovered_once.add(address)
                    yield device

            if deadline is not None and loop.time() >= deadline:
                break

            await asyncio.sleep(max(0.05, poll_interval))
    finally:
        await scanner.stop()

async def scan_for_devices(
    timeout: float = 10.0,
    name_filter: str = DEVICE_NAME,
    use_service_uuid: bool = True,
    expected_count: Optional[int] = None,
) -> list[BLEDevice]:
    """
    Return nearby LaserOps blasters.

    By default scans for the advertised service UUID (most reliable). Falls
    back to a case-insensitive device-name substring match when
    ``use_service_uuid`` is False.

    If ``expected_count`` is set to a positive value, scanning stops early once
    at least that many matching devices are discovered.

    All LaserOps blasters advertise:
      - Device Name (AD type 0x09): "NerfV"
      - 128-bit Service UUID (AD type 0x07): 073e1435-85d1-455c-97cd-0b8262f20eac

        For incremental/continuous discovery, use ``stream_discovered_devices``.
    """
    needle = name_filter.lower()

    def _matches(device: BLEDevice) -> bool:
        if use_service_uuid:
            return True
        return bool(device.name and needle in device.name.lower())

    if not expected_count or expected_count <= 0:
        if use_service_uuid:
            devices = await BleakScanner.discover(
                timeout=timeout,
                service_uuids=[SERVICE_UUID],
            )
            return list(devices)

        devices = await BleakScanner.discover(timeout=timeout)
        return [d for d in devices if _matches(d)]

    scanner_kwargs = {"service_uuids": [SERVICE_UUID]} if use_service_uuid else {}
    scanner = BleakScanner(**scanner_kwargs)
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout

    await scanner.start()
    try:
        while True:
            matched = [d for d in scanner.discovered_devices if _matches(d)]
            if len(matched) >= expected_count:
                return matched

            remaining = deadline - loop.time()
            if remaining <= 0:
                return matched

            await asyncio.sleep(min(0.2, remaining))
    finally:
        await scanner.stop()
