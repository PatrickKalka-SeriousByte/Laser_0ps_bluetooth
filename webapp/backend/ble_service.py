from __future__ import annotations

import asyncio
import copy
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from bleak import BleakScanner

from scripts.laserops import (
    LaserOpsDevice,
    RoundSlotStatsReply,
    decode_notification,
    scan_for_devices,
)

MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
SAFE_LEVEL_MIN = 1
SAFE_LEVEL_MAX = 5
SAFE_NAME_INDEX_MIN = 0
SAFE_NAME_INDEX_MAX = 49
SAFE_PROFILE_BYTE_MIN = 0x00
SAFE_PROFILE_BYTE_MAX = 0xFF
SAFE_SLOT_MIN = 1
SAFE_SLOT_MAX = 10
SAFE_TEAM_MIN = 0
SAFE_TEAM_MAX = 2
# Test 12 (all-vs-all) shows team id 0x02 for all participants.
SAFE_MULTIPLAYER_FFA_TEAM = 2
SAFE_GAME_DELAY_MIN = 0.00
SAFE_GAME_DELAY_MAX = 0.30
SAFE_MULTI_START_MAX_DEVICES = SAFE_SLOT_MAX - SAFE_SLOT_MIN + 1
SAFE_MULTI_RECONNECT_TIMEOUT = 8.0
FOLLOWUP_ROUND_RECYCLE_WAIT_SECONDS = 4.0
FOLLOWUP_ROUND_RECYCLE_CONNECT_TIMEOUT = 15.0
FOLLOWUP_ROUND_RECYCLE_CONNECT_ATTEMPTS = 2
FOLLOWUP_ROUND_RECYCLE_RETRY_DELAY_SECONDS = 2.0
FOLLOWUP_ROUND_RECYCLE_CANCEL_TIMEOUT_SECONDS = 2.0
SAFE_STARTUP_VOLUME_DEFAULT = 0
SAFE_GAME_DURATION_SECONDS_DEFAULT = 300
SAFE_GAME_DURATION_SECONDS_MIN = 30
SAFE_GAME_DURATION_SECONDS_MAX = 3600
LIVE_SUBSCRIBER_QUEUE_SIZE = 500
RECONNECT_SCAN_TIMEOUT = 15.0
RECONNECT_MAX_ATTEMPTS = 12
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 15.0
CONNECT_STARTUP_PROBE_TIMEOUT = 5.0
LOCAL_NAME_MAX_LENGTH = 32
LOCAL_NAME_STORE_FILENAME = "local_names.json"
DEBUG_EVENT_LOG_FILENAME = "live_event_log.ndjson"
ROUND_SLOT_STATS_ERROR_DETAIL_LIMIT = 8
AUTO_END_ROUND_SHOTS_GRACE_SECONDS = 3.0
TEAM_CONFIRMATION_LINK_LOSS_WINDOW_SECONDS = 3.0
# Some firmware revisions emit back-to-back duplicate trigger/reload notifications
# for one physical action. Keep a tiny dedupe window so live shot/reload counters
# match real actions while preserving raw event visibility.
TRIGGER_RELOAD_DEDUP_WINDOW_SECONDS = 0.05


DEFAULT_ALPHA_CONFIG_TEMPLATE: dict[str, int] = {
    "ammo_profile": 0x0A,
    "damage_profile": 0x02,
    "profile_byte4": 0x02,
    "profile_byte5": 0x03,
    "byte7_selector": 0x00,
    "health_profile": 0x0A,
    "reserved_byte11": 0x00,
    "tail_profile": 0x04,
}

DEFAULT_DELTA_CONFIG_TEMPLATE: dict[str, int] = {
    "ammo_profile": 0x12,
    "damage_profile": 0x01,
    "profile_byte4": 0x02,
    "profile_byte5": 0x00,
    "byte7_selector": 0x01,
    "health_profile": 0x0A,
    "reserved_byte11": 0x00,
    "tail_profile": 0x04,
}


def _snapshot_to_dict(snapshot: Any) -> dict[str, Any]:
    raw_hex = snapshot.raw.hex()
    return {
        "level": snapshot.level,
        "name_a": snapshot.name_a,
        "name_b": snapshot.name_b,
        "raw": raw_hex,
        "blaster_type": _detect_blaster_type_from_snapshot_raw(raw_hex),
    }


def _detect_blaster_type_from_snapshot_raw(raw_hex: str | None) -> str | None:
    if not raw_hex:
        return None
    try:
        payload = bytes.fromhex(str(raw_hex))
    except ValueError:
        return None
    if len(payload) != 13 or payload[0] != 0x35:
        return None
    # Prefer selector byte (1-based byte 7), which stays stable when
    # ammo/damage/health profile bytes are customized via 0x36.
    if payload[6] == 0x00:
        return "AlphaPoint"
    if payload[6] == 0x01:
        return "DeltaBurst"
    # Fallback to historical signatures.
    if payload[2] == 0x0A and payload[3] == 0x02 and payload[4] == 0x02:
        return "AlphaPoint"
    if payload[2] == 0x12 and payload[3] == 0x01 and payload[4] == 0x02:
        return "DeltaBurst"
    return "Unknown"


def _extract_config_write_profile(payload: bytes) -> dict[str, Any] | None:
    if len(payload) != 13 or payload[0] != 0x36:
        return None
    byte7_selector = int(payload[6])  # 1-based byte 7
    if byte7_selector == 0x00:
        family_hint = "AlphaPoint_candidate"
    elif byte7_selector == 0x01:
        family_hint = "DeltaBurst_candidate"
    else:
        family_hint = "Unknown"
    return {
        "template_prefix": payload[1:8].hex(),
        "byte7_selector": byte7_selector,
        "family_hint": family_hint,
        "ammo_profile": int(payload[2]),
        "damage_profile": int(payload[3]),
        "health_profile": int(payload[7]),
        "level": int(payload[8]),
        "name_a": int(payload[9]),
        "name_b": int(payload[10]),
        "tail_profile": int(payload[12]),
    }


def _extract_config_template_from_startup_payload(payload: bytes) -> dict[str, int] | None:
    if len(payload) != 13 or payload[0] != 0x35:
        return None
    return {
        "ammo_profile": int(payload[2]),
        "damage_profile": int(payload[3]),
        "profile_byte4": int(payload[4]),
        "profile_byte5": int(payload[5]),
        "byte7_selector": int(payload[6]),
        "health_profile": int(payload[7]),
        "reserved_byte11": int(payload[11]),
        "tail_profile": int(payload[12]),
    }


def _default_template_for_blaster_type(blaster_type: str | None) -> dict[str, int]:
    if blaster_type == "DeltaBurst":
        return dict(DEFAULT_DELTA_CONFIG_TEMPLATE)
    return dict(DEFAULT_ALPHA_CONFIG_TEMPLATE)


def _decode_tx_packet(payload: bytes) -> str:
    if not payload:
        return "tx_empty"
    if payload == b"\x35":
        return "startup_query"
    if len(payload) == 13 and payload[0] == 0x36:
        cfg = _extract_config_write_profile(payload)
        if cfg is None:
            return f"config_write raw={payload.hex()}"
        return (
            f"config_write level={cfg['level']} "
            f"name_parts=(0x{cfg['name_a']:02x}, 0x{cfg['name_b']:02x}) "
            f"family_hint={cfg['family_hint']} "
            f"byte7=0x{cfg['byte7_selector']:02x} "
            f"profile=(ammo=0x{cfg['ammo_profile']:02x}, "
            f"damage=0x{cfg['damage_profile']:02x}, "
            f"health=0x{cfg['health_profile']:02x}, "
            f"tail=0x{cfg['tail_profile']:02x}) raw={payload.hex()}"
        )
    if payload == b"\x57":
        return "config_apply_commit"
    if len(payload) == 2 and payload[0] == 0x5B:
        return f"volume_set value={payload[1]} raw={payload.hex()}"
    if payload == b"\x51":
        return "status_poll"
    if len(payload) == 3 and payload[0] == 0x49:
        return (
            f"multiplayer_assignment slot={payload[1]} team={payload[2]} "
            f"raw={payload.hex()}"
        )
    if len(payload) == 12 and payload[0] == 0x4A:
        duration = (payload[4] << 8) | payload[5]
        return f"multiplayer_game_config duration_seconds={duration} raw={payload.hex()}"
    if payload == b"\x58":
        return "multiplayer_round_arm"
    if len(payload) == 2 and payload[0] == 0x54:
        return f"round_slot_stats_request slot={payload[1]} raw={payload.hex()}"
    if len(payload) == 6 and payload[:3] == bytes([0x5A, 0x3F, 0x01]):
        return f"stat_request type=0x{payload[3]:02x} raw={payload.hex()}"
    if payload == b"\x42":
        return "session_close"
    if len(payload) == 4 and payload[0] == 0x37:
        return f"game_ctrl raw={payload.hex()}"
    if len(payload) == 2 and payload[0] == 0x44:
        return f"game_mode_flag mode={payload[1]} raw={payload.hex()}"
    if payload == b"\x47":
        return "round_shot_counter_request"
    if payload[:1] in (b"\x41", b"\x3B", b"\x39"):
        return f"game_setup_param raw={payload.hex()}"
    return f"tx_unknown raw={payload.hex()}"


def _derive_packet_fields(payload: bytes, *, direction: str) -> dict[str, Any]:
    if not payload:
        return {"direction": direction, "length": 0, "msg_id": None}
    msg_id = int(payload[0])
    out: dict[str, Any] = {
        "direction": direction,
        "length": len(payload),
        "msg_id": f"0x{msg_id:02x}",
    }
    if direction == "tx":
        if msg_id == 0x36 and len(payload) == 13:
            cfg = _extract_config_write_profile(payload)
            if cfg is not None:
                out.update(cfg)
                out["tail_marker"] = f"0x{int(cfg['tail_profile']):02x}"
        elif msg_id == 0x49 and len(payload) == 3:
            out.update({"slot": int(payload[1]), "team": int(payload[2])})
        elif msg_id == 0x4A and len(payload) == 12:
            out.update(
                {
                    "duration_seconds": int((payload[4] << 8) | payload[5]),
                    "timer_tail": payload[10:12].hex(),
                }
            )
        elif msg_id == 0x5B and len(payload) == 2:
            out.update({"volume": int(payload[1])})
        elif msg_id == 0x54 and len(payload) == 2:
            out.update({"slot": int(payload[1])})
        elif msg_id == 0x5A and len(payload) == 6 and payload[:3] == bytes([0x5A, 0x3F, 0x01]):
            out.update({"stat_type": int(payload[3])})
        elif msg_id == 0x44 and len(payload) == 2:
            out.update({"mode_flag": int(payload[1])})
        elif msg_id == 0x37 and len(payload) == 4:
            control = int(payload[1])
            out.update(
                {
                    "game_ctrl_control": control,
                    "game_ctrl_control_hex": f"0x{control:02x}",
                    "game_ctrl_tail": payload[2:4].hex(),
                }
            )
        elif msg_id == 0x47 and len(payload) == 1:
            out.update({"round_shot_counter_request": True})
        return out

    # RX (notification) derived values
    if msg_id == 0x35 and len(payload) == 13:
        raw_hex = payload.hex()
        out.update(
            {
                "level": int(payload[8]),
                "name_a": int(payload[9]),
                "name_b": int(payload[10]),
                "profile_guess": _detect_blaster_type_from_snapshot_raw(raw_hex),
            }
        )
    elif msg_id == 0x51 and len(payload) == 3:
        out.update({"status_word": int((payload[1] << 8) | payload[2])})
    elif msg_id == 0x32 and len(payload) >= 2:
        out.update({"ammo_family": int(payload[1]), "ammo_counter": int(payload[-1])})
    elif (
        msg_id == 0x30
        and len(payload) == 5
        and payload[:3] != bytes([0x30, 0x01, 0x3F])
    ):
        out.update(
            {
                "life_mode_a": int(payload[1]),
                "life_mode_b": int(payload[2]),
                "life_family": int(payload[3]),
                "life_counter": int(payload[4]),
            }
        )
    elif msg_id == 0x3E and len(payload) == 3:
        out.update(
            {
                "marker_family": int(payload[1]),
                "marker_counter": int(payload[2]),
                "stats_terminal": bool(payload == bytes([0x3E, 0x01, 0x00])),
            }
        )
    elif msg_id == 0x3F and len(payload) == 1:
        out.update({"respawn_ready": True})
    elif msg_id == 0x47 and len(payload) == 3:
        out.update({"round_shots": int((payload[1] << 8) | payload[2])})
    elif msg_id == 0x54 and len(payload) == 5:
        out.update(
            {
                "hits": int(payload[2]),
                "kills": int(payload[3]),
                "slot": int(payload[4]),
            }
        )
    elif msg_id == 0x31 and len(payload) == 2:
        out.update({"reload_variant": int(payload[1])})
    elif msg_id == 0x49 and len(payload) == 1:
        out.update({"trigger_event": True})
    elif msg_id == 0x52 and len(payload) == 1:
        out.update({"reload_marker": True})
    return out


@dataclass
class ConnectionState:
    address: str
    name: str
    gun: LaserOpsDevice
    ble_device: Any
    connected_at: float = field(default_factory=time.time)
    notifications: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=250)
    )
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_snapshot: dict[str, Any] | None = None
    last_game_start_at: float | None = None
    local_name: str | None = None
    assigned_slot: int | None = None
    assigned_team: int | None = None
    connection_state: str = "connected"
    desired_connected: bool = True
    auto_reconnect: bool = True
    reconnect_attempts: int = 0
    reconnect_count: int = 0
    disconnect_count: int = 0
    last_disconnect_at: float | None = None
    last_reconnect_at: float | None = None
    last_disconnect_reason: str | None = None
    last_error: str | None = None
    reconnect_task: asyncio.Task | None = None
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_counted_trigger_ts: float | None = None
    last_counted_reload_ts: float | None = None
    live_state: dict[str, Any] = field(
        default_factory=lambda: {
            "last_event": None,
            "last_event_ts": None,
            "last_raw": None,
            "shot_count": 0,
            "shot_count_source": None,
            "received_hit_count": 0,
            "received_kill_count": 0,
            "received_assist_count": 0,
            "received_hits_by_attacker_slot": {},
            "received_kills_by_attacker_slot": {},
            "received_assists_by_attacker_slot": {},
            "current_life_damage_by_attacker_slot": {},
            "trigger_count": 0,
            "reload_count": 0,
            "trigger_raw_count": 0,
            "reload_raw_count": 0,
            "stats_terminal_count": 0,
            "last_status_word": None,
            "last_ammo_family": None,
            "last_ammo_counter": None,
            "last_ammo_delta": None,
            "startup_ammo_profile": None,
            "configured_ammo_profile": None,
            "last_life_mode_a": None,
            "last_life_mode_b": None,
            "last_life_family": None,
            "last_life_counter": None,
            "last_life_attacker_slot": None,
            "last_life_delta": None,
            "last_life_assist_slots": [],
            "startup_health_profile": None,
            "configured_health_profile": None,
            "last_life_marker_family": None,
            "last_life_marker_counter": None,
            "respawn_ready_count": 0,
            "last_respawn_ready_ts": None,
            "last_reload_variant": None,
            "last_stat_type": None,
            "last_stat_counter": None,
            "last_round_shots": None,
            "last_round_shots_ts": None,
            "last_round_shots_raw": None,
            "round_shots_report_count": 0,
            "startup_level": None,
            "startup_name_a": None,
            "startup_name_b": None,
            "startup_raw": None,
            "startup_blaster_type": None,
        }
    )

    def on_notification(self, payload: bytes) -> dict[str, Any]:
        ts = time.time()
        decoded = decode_notification(payload)
        item = {"ts": ts, "raw": payload.hex(), "decoded": decoded}
        self.notifications.append(item)
        self._update_live_state(payload=payload, decoded=decoded, ts=ts)
        return item

    def live_state_snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self.live_state)

    def _configured_start_life(self) -> int | None:
        state = self.live_state
        raw_values = (
            state.get("configured_health_profile"),
            state.get("startup_health_profile"),
        )
        for raw in raw_values:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            return max(0, min(0xFF, value))
        return None

    def _configured_start_ammo(self) -> int | None:
        state = self.live_state
        raw_values = (
            state.get("configured_ammo_profile"),
            state.get("startup_ammo_profile"),
        )
        for raw in raw_values:
            try:
                value = int(raw)
            except (TypeError, ValueError):
                continue
            return max(0, min(0xFF, value))
        return None

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _update_live_state(self, *, payload: bytes, decoded: str, ts: float) -> None:
        state = self.live_state
        state["last_event"] = decoded
        state["last_event_ts"] = ts
        state["last_raw"] = payload.hex()

        if payload == b"\x49":
            state["trigger_raw_count"] = int(state.get("trigger_raw_count", 0) or 0) + 1
            if self.last_counted_trigger_ts is not None:
                if (ts - self.last_counted_trigger_ts) <= TRIGGER_RELOAD_DEDUP_WINDOW_SECONDS:
                    return
            self.last_counted_trigger_ts = ts
            state["trigger_count"] += 1
            return
        if payload == b"\x52":
            state["reload_raw_count"] = int(state.get("reload_raw_count", 0) or 0) + 1
            if self.last_counted_reload_ts is not None:
                if (ts - self.last_counted_reload_ts) <= TRIGGER_RELOAD_DEDUP_WINDOW_SECONDS:
                    return
            self.last_counted_reload_ts = ts
            state["reload_count"] += 1
            return
        if payload in (bytes.fromhex("310a"), bytes.fromhex("310d")):
            state["last_reload_variant"] = payload[1]
            start_ammo = self._configured_start_ammo()
            if start_ammo is not None:
                state["last_ammo_counter"] = start_ammo
                state["last_ammo_family"] = state.get("last_ammo_family") or start_ammo
                state["last_ammo_delta"] = 0
                state["ammo_reset_source"] = "reload_marker_b"
            return
        if len(payload) == 13 and payload[:1] == b"\x35":
            state["startup_level"] = payload[8]
            state["startup_name_a"] = payload[9]
            state["startup_name_b"] = payload[10]
            state["startup_ammo_profile"] = payload[2]
            state["startup_health_profile"] = payload[7]
            state["startup_raw"] = payload.hex()
            state["startup_blaster_type"] = _detect_blaster_type_from_snapshot_raw(
                payload.hex()
            )
            return
        if len(payload) == 3 and payload[:1] == b"\x51":
            state["last_status_word"] = (payload[1] << 8) | payload[2]
            return
        if len(payload) >= 2 and payload[:1] == b"\x32":
            new_counter = int(payload[-1])
            previous_counter = self._safe_int(state.get("last_ammo_counter"))
            if previous_counter is None:
                previous_counter = self._configured_start_ammo()
            if previous_counter is not None:
                delta = previous_counter - new_counter
                if delta > 0:
                    state["shot_count"] = int(state.get("shot_count", 0) or 0) + delta
                    state["shot_count_source"] = "ammo_delta"
                    state["last_ammo_delta"] = delta
                elif delta < 0:
                    state["last_ammo_delta"] = 0
                    state["ammo_reset_source"] = "ammo_state_increase"
                else:
                    state["last_ammo_delta"] = 0
            state["last_ammo_family"] = payload[1]
            state["last_ammo_counter"] = new_counter
            return
        if (
            len(payload) == 5
            and payload[0] == 0x30
            and payload[:3] != bytes([0x30, 0x01, 0x3F])
        ):
            previous_life = self._safe_int(state.get("last_life_counter"))
            if previous_life is None:
                previous_life = self._configured_start_life()
            attacker_slot = int(payload[2])
            new_life = int(payload[4])
            state["last_life_attacker_slot"] = attacker_slot
            if previous_life is not None and new_life < previous_life:
                delta = previous_life - new_life
                slot_key = str(attacker_slot)
                hits_by_slot = state.setdefault(
                    "received_hits_by_attacker_slot", {}
                )
                kills_by_slot = state.setdefault(
                    "received_kills_by_attacker_slot", {}
                )
                assists_by_slot = state.setdefault(
                    "received_assists_by_attacker_slot", {}
                )
                current_damage_by_slot = state.setdefault(
                    "current_life_damage_by_attacker_slot", {}
                )
                hits_by_slot[slot_key] = int(hits_by_slot.get(slot_key, 0) or 0) + delta
                current_damage_by_slot[slot_key] = (
                    int(current_damage_by_slot.get(slot_key, 0) or 0) + delta
                )
                state["received_hit_count"] = (
                    int(state.get("received_hit_count", 0) or 0) + delta
                )
                state["last_life_delta"] = delta
                state["last_life_assist_slots"] = []
                if new_life == 0:
                    kills_by_slot[slot_key] = (
                        int(kills_by_slot.get(slot_key, 0) or 0) + 1
                    )
                    state["received_kill_count"] = (
                        int(state.get("received_kill_count", 0) or 0) + 1
                    )
                    assist_slots: list[int] = []
                    for assist_slot_key, assist_damage in current_damage_by_slot.items():
                        if assist_slot_key == slot_key:
                            continue
                        try:
                            assist_slot = int(assist_slot_key)
                            assist_damage_count = int(assist_damage)
                        except (TypeError, ValueError):
                            continue
                        if assist_slot <= 0 or assist_damage_count <= 0:
                            continue
                        assists_by_slot[assist_slot_key] = (
                            int(assists_by_slot.get(assist_slot_key, 0) or 0) + 1
                        )
                        state["received_assist_count"] = (
                            int(state.get("received_assist_count", 0) or 0) + 1
                        )
                        assist_slots.append(assist_slot)
                    state["last_life_assist_slots"] = sorted(assist_slots)
                    state["current_life_damage_by_attacker_slot"] = {}
            else:
                state["last_life_delta"] = 0
                if previous_life is not None and new_life > previous_life:
                    state["life_reset_source"] = "life_counter_increase"
                    state["current_life_damage_by_attacker_slot"] = {}
                    state["last_life_assist_slots"] = []
            state["last_life_mode_a"] = payload[1]
            state["last_life_mode_b"] = payload[2]
            state["last_life_family"] = payload[3]
            state["last_life_counter"] = new_life
            return
        if len(payload) == 5 and payload[:3] == bytes([0x30, 0x01, 0x3F]):
            state["last_stat_type"] = payload[3]
            state["last_stat_counter"] = payload[4]
            return
        if payload == bytes([0x3F]):
            state["respawn_ready_count"] += 1
            state["last_respawn_ready_ts"] = ts
            start_life = self._configured_start_life()
            if start_life is not None:
                state["last_life_counter"] = start_life
                state["last_life_family"] = state.get("last_life_family") or 0x0A
                state["life_reset_source"] = "respawn_ready"
            state["current_life_damage_by_attacker_slot"] = {}
            state["last_life_assist_slots"] = []
            return
        if payload == bytes([0x3E, 0x01, 0x00]):
            state["stats_terminal_count"] += 1
            return
        if len(payload) == 3 and payload[0] == 0x3E:
            state["last_life_marker_family"] = payload[1]
            state["last_life_marker_counter"] = payload[2]
            return
        if len(payload) == 3 and payload[0] == 0x47:
            state["last_round_shots"] = (payload[1] << 8) | payload[2]
            state["last_round_shots_ts"] = ts
            state["last_round_shots_raw"] = payload.hex()
            state["round_shots_report_count"] = (
                int(state.get("round_shots_report_count", 0) or 0) + 1
            )
            return


@dataclass
class GameSessionState:
    session_id: int
    started_at: float
    duration_seconds: int
    planned_end_at: float
    participant_keys: list[str]
    baseline_shots: dict[str, int]
    baseline_triggers: dict[str, int]
    baseline_reloads: dict[str, int]
    baseline_life_slot_stats: dict[int, dict[str, int]]
    baseline_round_shot_reports: dict[str, int]
    status: str = "running"
    ended_at: float | None = None
    end_reason: str | None = None
    final_snapshot: dict[str, Any] | None = None
    final_slot_stats: dict[int, dict[str, int]] = field(default_factory=dict)
    final_round_shots: dict[str, dict[str, Any]] = field(default_factory=dict)
    round_shot_wait: dict[str, Any] | None = None


class BleHub:
    def __init__(self) -> None:
        self._connections: dict[str, ConnectionState] = {}
        self._connections_lock = asyncio.Lock()
        self._discovery_lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._bulk_game_start_lock = asyncio.Lock()
        self._state_dir = Path(__file__).resolve().parent / "state"
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._local_name_store_path = self._state_dir / LOCAL_NAME_STORE_FILENAME
        self._debug_event_log_path = self._state_dir / DEBUG_EVENT_LOG_FILENAME
        self._debug_event_log_lock = asyncio.Lock()
        self._local_names, self._local_team_profiles = self._load_local_name_store()
        self._local_names_lock = asyncio.Lock()
        self._game_session_lock = asyncio.Lock()
        self._game_session: GameSessionState | None = None
        self._game_session_counter = 0
        self._game_end_task: asyncio.Task | None = None
        self._game_end_session_id: int | None = None
        self._event_seq = 0
        self._next_subscriber_id = 1
        self._subscribers: dict[int, asyncio.Queue[dict[str, Any]]] = {}
        self._subscribers_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()

    async def scan(
        self,
        *,
        timeout: float,
        by_name: bool,
        name: str,
        expected_count: int,
    ) -> list[dict[str, Any]]:
        if self._discovery_lock.locked():
            raise ValueError("bluetooth discovery already running; please wait")
        async with self._discovery_lock:
            try:
                devices = await scan_for_devices(
                    timeout=timeout,
                    name_filter=name,
                    use_service_uuid=not by_name,
                    expected_count=expected_count if expected_count > 0 else None,
                )
            except Exception as exc:
                text = str(exc)
                if (
                    "org.bluez.Error.InProgress" in text
                    or "Operation already in progress" in text
                ):
                    raise ValueError(
                        "bluetooth scan already in progress on host; wait and retry"
                    ) from exc
                raise
        result: list[dict[str, Any]] = []
        for dev in devices:
            result.append(
                {
                    "address": dev.address,
                    "name": dev.name or "unknown",
                    "rssi": getattr(dev, "rssi", None),
                }
            )
        return result

    async def connect(self, address: str, timeout: float) -> dict[str, Any]:
        async with self._connect_lock:
            return await self._connect_serialized(address, timeout)

    async def _connect_serialized(self, address: str, timeout: float) -> dict[str, Any]:
        normalized = self._normalize_address(address)
        key = normalized.lower()
        async with self._connections_lock:
            if key in self._connections:
                raise ValueError(f"device already connected: {address}")

        async with self._discovery_lock:
            ble_dev = await BleakScanner.find_device_by_address(
                normalized, timeout=timeout
            )
        if ble_dev is None:
            raise LookupError(f"device not found: {address}")

        placeholder = ConnectionState(
            address=ble_dev.address,
            name=ble_dev.name or "unknown",
            gun=None,  # type: ignore[arg-type]
            ble_device=ble_dev,
        )
        async with self._local_names_lock:
            placeholder.local_name = self._local_names.get(key)
            stored_team_profile = self._local_team_profiles.get(key)
        notification_cb, disconnected_cb, write_cb = self._build_device_callbacks(
            placeholder
        )
        gun = LaserOpsDevice(
            ble_dev,
            notification_callback=notification_cb,
            disconnected_callback=disconnected_cb,
            write_callback=write_cb,
        )
        await gun.__aenter__()
        placeholder.gun = gun
        placeholder.connection_state = "connected"
        placeholder.desired_connected = True
        placeholder.last_reconnect_at = time.time()
        placeholder.last_error = None

        async with self._connections_lock:
            used_slots = {
                conn.assigned_slot
                for conn in self._connections.values()
                if conn.assigned_slot is not None
            }
            slot = None
            team = None
            if stored_team_profile is not None:
                stored_slot, stored_team = stored_team_profile
                if stored_slot not in used_slots:
                    slot = stored_slot
                    team = stored_team
            if slot is None:
                slot = SAFE_SLOT_MIN
                while slot in used_slots and slot <= SAFE_SLOT_MAX:
                    slot += 1
            if slot > SAFE_SLOT_MAX:
                slot = SAFE_SLOT_MIN
            placeholder.assigned_slot = slot
            placeholder.assigned_team = (
                team if team is not None else self._default_team_for_slot(slot)
            )
            self._connections[key] = placeholder

        await self._refresh_startup_snapshot(
            placeholder,
            timeout=CONNECT_STARTUP_PROBE_TIMEOUT,
        )

        self._schedule_event(
            "connection",
            {
                "action": "connected",
                "connection": self.connection_summary(placeholder),
            },
        )
        return self.connection_summary(placeholder)

    async def disconnect(self, address: str) -> None:
        key = self._normalize_address(address).lower()
        async with self._connections_lock:
            conn = self._connections.pop(key, None)
        if conn is None:
            raise LookupError(f"device is not connected: {address}")
        conn.desired_connected = False
        conn.connection_state = "disconnected"
        conn.last_disconnect_reason = "manual_disconnect"
        conn.last_disconnect_at = time.time()
        await self._persist_connection_local_profile(conn)
        self._cancel_reconnect_task(conn)
        await conn.gun.__aexit__(None, None, None)
        self._schedule_event(
            "connection",
            {
                "action": "disconnected",
                "address": conn.address,
                "name": conn.name,
                "local_name": conn.local_name,
                "display_name": self._display_name(conn),
            },
        )

    async def disconnect_all(self) -> None:
        async with self._connections_lock:
            items = list(self._connections.items())
            self._connections.clear()
        async with self._game_session_lock:
            self._game_session = None
        self._cancel_game_end_task()
        self._schedule_event(
            "ranking",
            self._empty_ranking_snapshot(),
        )
        for _key, conn in items:
            conn.desired_connected = False
            conn.connection_state = "disconnected"
            conn.last_disconnect_reason = "manual_disconnect_all"
            conn.last_disconnect_at = time.time()
            await self._persist_connection_local_profile(conn)
            self._cancel_reconnect_task(conn)
            try:
                await conn.gun.__aexit__(None, None, None)
            except Exception:
                pass
            self._schedule_event(
                "connection",
                {
                    "action": "disconnected",
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                },
            )

    async def startup(self, address: str, volume: int) -> dict[str, Any]:
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            await self._ensure_connected(conn)
            async with conn.op_lock:
                snapshot = await conn.gun.startup(volume=volume)
                conn.last_snapshot = _snapshot_to_dict(snapshot)
                self._schedule_event(
                    "startup",
                    {
                        "address": conn.address,
                        "name": conn.name,
                        "volume": volume,
                        "snapshot": conn.last_snapshot,
                        "live_state": conn.live_state_snapshot(),
                    },
                )
                return conn.last_snapshot

    async def set_volume(self, address: str, volume: int) -> None:
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            await self._ensure_connected(conn)
            async with conn.op_lock:
                await conn.gun.set_volume(volume)
                self._schedule_event(
                    "volume",
                    {"address": conn.address, "name": conn.name, "volume": volume},
                )

    async def write_config(
        self,
        address: str,
        level: int,
        name_a: int,
        name_b: int,
        *,
        ammo_profile: int | None = None,
        damage_profile: int | None = None,
        health_profile: int | None = None,
    ) -> None:
        self._validate_safe_config(
            level=level,
            name_a=name_a,
            name_b=name_b,
            ammo_profile=ammo_profile,
            damage_profile=damage_profile,
            health_profile=health_profile,
        )
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            await self._ensure_connected(conn)
            template = self._resolve_config_template_for_connection(conn)
            if ammo_profile is not None:
                template["ammo_profile"] = int(ammo_profile)
            if damage_profile is not None:
                template["damage_profile"] = int(damage_profile)
            if health_profile is not None:
                template["health_profile"] = int(health_profile)
            # Mirror assign_device.py behavior: UI/API provide 0-based indices,
            # payload uses (index + 1) bytes.
            name_a_byte = name_a + 1
            name_b_byte = name_b + 1
            async with conn.op_lock:
                await conn.gun.write_config(
                    level=level,
                    name_a=name_a_byte,
                    name_b=name_b_byte,
                    ammo_profile=template["ammo_profile"],
                    damage_profile=template["damage_profile"],
                    profile_byte4=template["profile_byte4"],
                    profile_byte5=template["profile_byte5"],
                    byte7_selector=template["byte7_selector"],
                    health_profile=template["health_profile"],
                    reserved_byte11=template["reserved_byte11"],
                    tail_profile=template["tail_profile"],
                )
                self._schedule_event(
                    "config",
                    {
                        "address": conn.address,
                        "name": conn.name,
                        "level": level,
                        "name_a": name_a,
                        "name_b": name_b,
                        "ammo_profile": template["ammo_profile"],
                        "damage_profile": template["damage_profile"],
                        "health_profile": template["health_profile"],
                        "template_byte7_selector": template["byte7_selector"],
                    },
                )

    def _resolve_config_template_for_connection(
        self, conn: ConnectionState
    ) -> dict[str, int]:
        snapshot_raw = None
        if isinstance(conn.last_snapshot, dict):
            snapshot_raw = conn.last_snapshot.get("raw")
        if isinstance(snapshot_raw, str):
            try:
                payload = bytes.fromhex(snapshot_raw)
                template = _extract_config_template_from_startup_payload(payload)
                if template is not None:
                    return template
            except ValueError:
                pass

        live_raw = conn.live_state.get("startup_raw")
        if isinstance(live_raw, str):
            try:
                payload = bytes.fromhex(live_raw)
                template = _extract_config_template_from_startup_payload(payload)
                if template is not None:
                    return template
            except ValueError:
                pass

        blaster_type = None
        if isinstance(conn.last_snapshot, dict):
            snapshot_type = conn.last_snapshot.get("blaster_type")
            if isinstance(snapshot_type, str) and snapshot_type:
                blaster_type = snapshot_type
        if blaster_type is None:
            startup_type = conn.live_state.get("startup_blaster_type")
            if isinstance(startup_type, str) and startup_type:
                blaster_type = startup_type
        return _default_template_for_blaster_type(blaster_type)

    async def set_team_profile(self, address: str, slot: int, team: int) -> dict[str, Any]:
        safe_slot, safe_team = self._validate_safe_team(slot=slot, team=team)
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            async with self._connections_lock:
                for key, other in self._connections.items():
                    if key == conn.address.lower():
                        continue
                    if other.assigned_slot == safe_slot:
                        raise ValueError(
                            f"slot {safe_slot} already assigned to {other.address}"
                        )
            conn.assigned_slot = safe_slot
            conn.assigned_team = safe_team
            key = self._normalize_address(conn.address).lower()
            async with self._local_names_lock:
                previous_profile = self._local_team_profiles.get(key)
                self._local_team_profiles[key] = (safe_slot, safe_team)
                try:
                    self._save_local_name_store()
                except Exception:
                    if previous_profile is None:
                        self._local_team_profiles.pop(key, None)
                    else:
                        self._local_team_profiles[key] = previous_profile
                    raise
            summary = self.connection_summary(conn)
            self._schedule_event(
                "team_profile",
                {
                    "address": conn.address,
                    "name": conn.name,
                    "slot": safe_slot,
                    "team": safe_team,
                },
            )
            return summary

    async def set_local_name(self, address: str, local_name: str | None) -> dict[str, Any]:
        safe_local_name = self._normalize_local_name(local_name)
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            previous_local_name = conn.local_name
            conn.local_name = safe_local_name
            key = self._normalize_address(conn.address).lower()
            async with self._local_names_lock:
                previous_stored_name = self._local_names.get(key)
                previous_stored_profile = self._local_team_profiles.get(key)
                if safe_local_name is None:
                    self._local_names.pop(key, None)
                else:
                    self._local_names[key] = safe_local_name
                if conn.assigned_slot is not None and conn.assigned_team is not None:
                    self._local_team_profiles[key] = (
                        int(conn.assigned_slot),
                        int(conn.assigned_team),
                    )
                try:
                    self._save_local_name_store()
                except Exception:
                    if previous_stored_name is None:
                        self._local_names.pop(key, None)
                    else:
                        self._local_names[key] = previous_stored_name
                    if previous_stored_profile is None:
                        self._local_team_profiles.pop(key, None)
                    else:
                        self._local_team_profiles[key] = previous_stored_profile
                    conn.local_name = previous_local_name
                    raise
            summary = self.connection_summary(conn)
            self._schedule_event(
                "local_name",
                {
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "connection": summary,
                },
            )
            return summary

    async def start_game(
        self,
        address: str,
        delay: float,
        *,
        startup_volume: int = SAFE_STARTUP_VOLUME_DEFAULT,
        force_startup: bool = True,
        duration_seconds: int = SAFE_GAME_DURATION_SECONDS_DEFAULT,
        slot: int | None = None,
        team: int | None = None,
    ) -> dict[str, Any]:
        safe_delay = self._validate_safe_game_delay(delay)
        safe_duration_seconds = self._validate_game_duration(duration_seconds)
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            if not conn.gun.is_connected:
                raise ValueError(
                    f"device not connected at game start: {conn.address}"
                )
            await self._finalize_game_session(
                reason="superseded_by_new_start",
                send_close=True,
                collect_slot_stats=False,
            )
            selected_slot, selected_team = self._resolve_team_for_start(
                conn=conn,
                slot=slot,
                team=team,
            )
            snapshot, recovery_used = await self._run_multiplayer_start_sequence(
                conn=conn,
                slot=selected_slot,
                team=selected_team,
                startup_volume=startup_volume,
                delay=safe_delay,
                duration_seconds=safe_duration_seconds,
                allow_reconnect=False,
            )
            conn.last_snapshot = _snapshot_to_dict(snapshot)
            startup_performed = True
            conn.last_game_start_at = time.time()
            self._schedule_event(
                "game_start",
                {
                    "address": conn.address,
                    "name": conn.name,
                    "startup_performed": startup_performed,
                    "force_startup": force_startup,
                    "delay": safe_delay,
                    "duration_seconds": safe_duration_seconds,
                    "slot": selected_slot,
                    "team": selected_team,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "game_start_at": conn.last_game_start_at,
                    "recovery_used": recovery_used,
                },
            )
            ranking_snapshot = await self._start_game_session(
                [conn],
                duration_seconds=safe_duration_seconds,
            )
            return {
                "address": conn.address,
                "name": conn.name,
                "local_name": conn.local_name,
                "display_name": self._display_name(conn),
                "startup_performed": startup_performed,
                "last_snapshot": conn.last_snapshot,
                "slot": selected_slot,
                "team": selected_team,
                "duration_seconds": safe_duration_seconds,
                "game_start_at": conn.last_game_start_at,
                "recovery_used": recovery_used,
                "ranking": ranking_snapshot,
            }

    async def start_game_multi(
        self,
        *,
        addresses: list[str] | None,
        startup_volume: int,
        delay: float,
        force_startup: bool,
        duration_seconds: int = SAFE_GAME_DURATION_SECONDS_DEFAULT,
        auto_recycle_followup: bool = True,
    ) -> dict[str, Any]:
        safe_delay = self._validate_safe_game_delay(delay)
        safe_duration_seconds = self._validate_game_duration(duration_seconds)
        async with self._bulk_game_start_lock:
            requested_conns = await self._resolve_connections(addresses)
            if len(requested_conns) < 2:
                raise ValueError("multiplayer start requires at least 2 connected blasters")
            recycle_followup = (
                bool(auto_recycle_followup)
                and await self._should_recycle_for_followup_round_start()
            )
            await self._finalize_game_session(
                reason="superseded_by_new_start",
                send_close=True,
                collect_slot_stats=False,
            )

            if len(requested_conns) > SAFE_MULTI_START_MAX_DEVICES:
                raise ValueError(
                    f"too many devices for one synchronized start "
                    f"({len(requested_conns)} > {SAFE_MULTI_START_MAX_DEVICES})"
                )

            team_profiles = self._resolve_multi_team_profiles(requested_conns)
            self._validate_multi_team_mode(team_profiles)
            busy = [conn.address for conn in requested_conns if conn.op_lock.locked()]
            if busy:
                raise ValueError(
                    "devices are busy; retry after current operation: "
                    + ", ".join(busy)
                )
            recycle_result = None
            if recycle_followup:
                requested_conns, recycle_result = (
                    await self._recycle_connections_for_followup_start(
                        conns=requested_conns,
                        team_profiles=team_profiles,
                    )
                )
                team_profiles = self._resolve_multi_team_profiles(requested_conns)
                self._validate_multi_team_mode(team_profiles)
            connected_for_start = [
                conn for conn in requested_conns if conn.gun.is_connected
            ]
            disconnected_conns = [
                conn for conn in requested_conns if not conn.gun.is_connected
            ]
            prepared = [
                {
                    "address": conn.address,
                    "startup_performed": True,
                    "force_startup": bool(force_startup),
                    "slot": team_profiles[conn.address.lower()][0],
                    "team": team_profiles[conn.address.lower()][1],
                }
                for conn in requested_conns
            ]

            failures: list[dict[str, str]] = [
                {
                    "address": conn.address,
                    "error": (
                        "ConnectionError: device not connected at multi-start "
                        f"(state={conn.connection_state})"
                    ),
                }
                for conn in disconnected_conns
            ]
            started: list[dict[str, Any]] = []
            # Match observed multiplayer sequence per blaster:
            # 49 -> 4a -> 5b -> 35(snapshot) -> 58
            # Run each blaster sequence concurrently so all devices are ready quickly.
            tasks = [
                asyncio.create_task(
                    self._start_one_multi_connection(
                        conn=conn,
                        slot=team_profiles[conn.address.lower()][0],
                        team=team_profiles[conn.address.lower()][1],
                        startup_volume=startup_volume,
                        delay=safe_delay,
                        duration_seconds=safe_duration_seconds,
                        force_startup=force_startup,
                    )
                )
                for conn in connected_for_start
            ]
            if tasks:
                results = await asyncio.gather(*tasks)
                for started_item, failure_item in results:
                    if failure_item is not None:
                        failures.append(failure_item)
                        continue
                    if started_item is not None:
                        started.append(started_item)

            ranking_snapshot: dict[str, Any] | None = None
            if len(started) >= 2:
                started_keys = {item["address"].lower() for item in started}
                started_conns = [
                    conn
                    for conn in requested_conns
                    if conn.address.lower() in started_keys
                ]
                ranking_snapshot = await self._start_game_session(
                    started_conns,
                    duration_seconds=safe_duration_seconds,
                )
            else:
                ranking_snapshot = await self.game_ranking()

            return {
                "requested_count": len(requested_conns),
                "prepared_count": len(prepared),
                "started_count": len(started),
                "failure_count": len(failures),
                "aborted": bool((len(started) < 2) and failures),
                "duration_seconds": safe_duration_seconds,
                "prepared": prepared,
                "started": started,
                "failures": failures,
                "ranking": ranking_snapshot,
                "recycle": recycle_result,
            }

    async def poll_status(self, address: str) -> str:
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            await self._ensure_connected(conn)
            async with conn.op_lock:
                payload = await conn.gun.poll_status()
                return payload.hex()

    async def collect_stats(self, address: str) -> list[dict[str, Any]]:
        raise ValueError(
            "legacy stats command is disabled in safe mode "
            "(uses damage-related protocol traffic)"
        )

    async def close_session(self, address: str) -> None:
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            await self._ensure_connected(conn)
            async with conn.op_lock:
                await conn.gun.close_session()

    async def end_game(self, reason: str = "manual_stop") -> dict[str, Any]:
        async with self._bulk_game_start_lock:
            return await self._finalize_game_session(
                reason=self._sanitize_end_reason(reason),
                send_close=True,
            )

    async def reconnect(
        self, address: str, timeout: float = RECONNECT_SCAN_TIMEOUT
    ) -> dict[str, Any]:
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            conn.desired_connected = True
            await self._reconnect_once(conn, timeout=timeout)
            return self.connection_summary(conn)

    async def set_auto_reconnect(self, address: str, enabled: bool) -> dict[str, Any]:
        async with self._bulk_game_start_lock:
            conn = await self._get(address)
            conn.auto_reconnect = bool(enabled)
            if not conn.auto_reconnect:
                self._cancel_reconnect_task(conn)
            elif conn.desired_connected and not conn.gun.is_connected:
                self._start_reconnect_task(conn)
            self._schedule_event(
                "connection",
                {
                    "action": "auto_reconnect_changed",
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "enabled": conn.auto_reconnect,
                },
            )
            return self.connection_summary(conn)

    async def game_ranking(self) -> dict[str, Any]:
        async with self._game_session_lock:
            session = self._game_session
            if session is None:
                return self._empty_ranking_snapshot()
            if session.final_snapshot is not None:
                return copy.deepcopy(session.final_snapshot)
            session_copy = copy.deepcopy(session)

        async with self._connections_lock:
            active_connections = dict(self._connections)
        return self._build_session_snapshot(
            session=session_copy,
            active_connections=active_connections,
            running=session_copy.status == "running",
        )

    async def notifications(self, address: str) -> list[dict[str, Any]]:
        conn = await self._get(address)
        return list(conn.notifications)

    async def list_connections(self) -> list[dict[str, Any]]:
        async with self._connections_lock:
            conns = list(self._connections.values())
        return [self.connection_summary(conn) for conn in conns]

    def connection_summary(self, conn: ConnectionState) -> dict[str, Any]:
        blaster_type = None
        if isinstance(conn.last_snapshot, dict):
            blaster_type = conn.last_snapshot.get("blaster_type")
        if not blaster_type:
            startup_type = conn.live_state.get("startup_blaster_type")
            if startup_type:
                blaster_type = startup_type
        return {
            "address": conn.address,
            "name": conn.name,
            "local_name": conn.local_name,
            "display_name": self._display_name(conn),
            "blaster_type": blaster_type or "Unknown",
            "connected_at": conn.connected_at,
            "last_snapshot": conn.last_snapshot,
            "last_game_start_at": conn.last_game_start_at,
            "assigned_slot": conn.assigned_slot,
            "assigned_team": conn.assigned_team,
            "connection_state": conn.connection_state,
            "auto_reconnect": conn.auto_reconnect,
            "reconnect_attempts": conn.reconnect_attempts,
            "reconnect_count": conn.reconnect_count,
            "disconnect_count": conn.disconnect_count,
            "last_disconnect_at": conn.last_disconnect_at,
            "last_reconnect_at": conn.last_reconnect_at,
            "last_disconnect_reason": conn.last_disconnect_reason,
            "last_error": conn.last_error,
            "notification_count": len(conn.notifications),
            "live_state": conn.live_state_snapshot(),
        }

    async def shutdown(self) -> None:
        await self.disconnect_all()
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

    async def _get(self, address: str) -> ConnectionState:
        key = self._normalize_address(address).lower()
        async with self._connections_lock:
            conn = self._connections.get(key)
        if conn is None:
            raise LookupError(f"device is not connected: {address}")
        return conn

    async def _persist_connection_local_profile(self, conn: ConnectionState) -> None:
        key = self._normalize_address(conn.address).lower()
        async with self._local_names_lock:
            if conn.local_name:
                self._local_names[key] = conn.local_name
            if conn.assigned_slot is not None and conn.assigned_team is not None:
                self._local_team_profiles[key] = (
                    int(conn.assigned_slot),
                    int(conn.assigned_team),
                )
            try:
                self._save_local_name_store()
            except Exception:
                # Disconnects should not be blocked by a local preference write.
                pass

    async def _resolve_connections(
        self, addresses: list[str] | None
    ) -> list[ConnectionState]:
        if addresses:
            normalized = [self._normalize_address(addr).lower() for addr in addresses]
            if len(normalized) != len(set(normalized)):
                raise ValueError("duplicate addresses in multi-start request")
            selected = []
            async with self._connections_lock:
                for key in normalized:
                    conn = self._connections.get(key)
                    if conn is None:
                        raise LookupError(f"device is not connected: {key}")
                    selected.append(conn)
            return sorted(selected, key=lambda x: x.address.lower())

        async with self._connections_lock:
            selected = list(self._connections.values())
        if not selected:
            raise LookupError("no connected devices")
        return sorted(selected, key=lambda x: x.address.lower())

    async def _should_recycle_for_followup_round_start(self) -> bool:
        async with self._game_session_lock:
            if self._game_session_counter <= 0:
                return False
            session = self._game_session
            if session is None:
                return True
            return bool(session.final_snapshot is not None or session.status == "ended")

    async def _recycle_connections_for_followup_start(
        self,
        *,
        conns: list[ConnectionState],
        team_profiles: dict[str, tuple[int, int]],
    ) -> tuple[list[ConnectionState], dict[str, Any]]:
        targets = sorted(conns, key=lambda conn: conn.address.lower())
        reconnect_targets: list[dict[str, Any]] = []
        for conn in targets:
            key = conn.address.lower()
            slot, team = team_profiles[key]
            conn.assigned_slot = int(slot)
            conn.assigned_team = int(team)
            reconnect_targets.append(
                {
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "slot": int(slot),
                    "team": int(team),
                    "auto_reconnect": bool(conn.auto_reconnect),
                }
            )

        async with self._connections_lock:
            for item in reconnect_targets:
                self._connections.pop(str(item["address"]).lower(), None)

        disconnect_failures: list[dict[str, str]] = []
        for conn in targets:
            conn.desired_connected = False
            conn.connection_state = "disconnected"
            conn.last_disconnect_reason = "round_recycle"
            conn.last_disconnect_at = time.time()
            reconnect_task = conn.reconnect_task
            self._cancel_reconnect_task(conn)
            if reconnect_task is not None and not reconnect_task.done():
                try:
                    await asyncio.wait_for(
                        reconnect_task,
                        timeout=FOLLOWUP_ROUND_RECYCLE_CANCEL_TIMEOUT_SECONDS,
                    )
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass
            await self._persist_connection_local_profile(conn)
            try:
                await conn.gun.__aexit__(None, None, None)
            except Exception as exc:
                disconnect_failures.append(
                    {"address": conn.address, "error": self._format_error(exc)}
                )
            self._schedule_event(
                "connection",
                {
                    "action": "disconnected",
                    "reason": "round_recycle",
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "connection_state": conn.connection_state,
                },
            )

        wait_seconds = FOLLOWUP_ROUND_RECYCLE_WAIT_SECONDS
        self._schedule_event(
            "connection",
            {
                "action": "round_recycle_wait",
                "address": "",
                "display_name": "Folgerunde",
                "connection_state": "recycling",
                "count": len(reconnect_targets),
                "wait_seconds": wait_seconds,
            },
        )
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        connected: list[ConnectionState] = []
        reconnect_failures: list[dict[str, str]] = []
        for item in reconnect_targets:
            address = str(item["address"])
            last_exc: Exception | None = None
            for attempt in range(1, FOLLOWUP_ROUND_RECYCLE_CONNECT_ATTEMPTS + 1):
                try:
                    await self._connect_serialized(
                        address,
                        timeout=FOLLOWUP_ROUND_RECYCLE_CONNECT_TIMEOUT,
                    )
                    conn = await self._get(address)
                    conn.assigned_slot = int(item["slot"])
                    conn.assigned_team = int(item["team"])
                    conn.auto_reconnect = bool(item["auto_reconnect"])
                    await self._persist_connection_local_profile(conn)
                    connected.append(conn)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    self._schedule_event(
                        "connection",
                        {
                            "action": "round_recycle_reconnect_failed",
                            "address": address,
                            "name": item.get("name"),
                            "local_name": item.get("local_name"),
                            "display_name": item.get("display_name"),
                            "attempt": attempt,
                            "error": self._format_error(exc),
                        },
                    )
                    if attempt < FOLLOWUP_ROUND_RECYCLE_CONNECT_ATTEMPTS:
                        await asyncio.sleep(
                            FOLLOWUP_ROUND_RECYCLE_RETRY_DELAY_SECONDS
                        )
            if last_exc is not None:
                reconnect_failures.append(
                    {"address": address, "error": self._format_error(last_exc)}
                )

        result = {
            "enabled": True,
            "wait_seconds": wait_seconds,
            "requested_count": len(reconnect_targets),
            "connected_count": len(connected),
            "disconnect_failures": disconnect_failures,
            "reconnect_failures": reconnect_failures,
        }
        if reconnect_failures:
            detail = "; ".join(
                f"{item['address']}: {item['error']}" for item in reconnect_failures
            )
            raise ValueError(f"follow-up round reconnect failed: {detail}")
        return sorted(connected, key=lambda conn: conn.address.lower()), result

    def _normalize_address(self, address: str) -> str:
        normalized = address.strip()
        if not MAC_RE.fullmatch(normalized):
            raise ValueError(f"invalid BLE address format: {address}")
        return normalized.replace("-", ":").lower()

    def _build_device_callbacks(
        self, conn: ConnectionState
    ) -> tuple[Callable[[bytes], None], Callable[[], None], Callable[[bytes], None]]:
        def _on_notification(payload: bytes) -> None:
            item = conn.on_notification(payload)
            packet = {
                "direction": "rx",
                "raw": payload.hex(),
                "decoded": item.get("decoded"),
                "derived": _derive_packet_fields(payload, direction="rx"),
            }
            item["packet"] = packet
            self._schedule_event(
                "notification",
                {
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "notification": item,
                    "packet": packet,
                    "live_state": conn.live_state_snapshot(),
                },
            )
            if payload in (b"\x49", b"\x52"):
                self._spawn_background(
                    self._publish_ranking_if_running(conn.address.lower())
                )

        def _on_write(payload: bytes) -> None:
            cfg = _extract_config_write_profile(payload)
            if cfg is not None:
                conn.live_state["configured_ammo_profile"] = int(
                    cfg["ammo_profile"]
                )
                conn.live_state["configured_health_profile"] = int(
                    cfg["health_profile"]
                )
            self._schedule_event(
                "tx_packet",
                {
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "packet": {
                        "ts": time.time(),
                        "direction": "tx",
                        "raw": payload.hex(),
                        "decoded": _decode_tx_packet(payload),
                        "derived": _derive_packet_fields(payload, direction="tx"),
                    },
                    "live_state": conn.live_state_snapshot(),
                },
            )

        def _on_disconnected() -> None:
            self._spawn_background(
                self._handle_unexpected_disconnect(
                    conn=conn,
                    reason="link_lost",
                )
            )

        return _on_notification, _on_disconnected, _on_write

    async def _handle_unexpected_disconnect(
        self, *, conn: ConnectionState, reason: str
    ) -> None:
        if not conn.desired_connected:
            return
        now = time.time()
        is_team_confirmation = (
            reason == "link_lost"
            and conn.last_game_start_at is not None
            and (now - conn.last_game_start_at)
            <= TEAM_CONFIRMATION_LINK_LOSS_WINDOW_SECONDS
        )
        event_action = "team_confirmation_wait" if is_team_confirmation else "lost"
        event_reason = "team_confirmation" if is_team_confirmation else reason
        conn.connection_state = (
            "awaiting_team_confirmation" if is_team_confirmation else "disconnected"
        )
        conn.disconnect_count += 1
        conn.last_disconnect_at = now
        conn.last_disconnect_reason = event_reason
        self._schedule_event(
            "connection",
            {
                "action": event_action,
                "address": conn.address,
                "name": conn.name,
                "local_name": conn.local_name,
                "display_name": self._display_name(conn),
                "connection_state": conn.connection_state,
                "reason": event_reason,
                "disconnect_count": conn.disconnect_count,
            },
        )
        if not conn.auto_reconnect:
            return
        self._start_reconnect_task(conn)

    def _start_reconnect_task(self, conn: ConnectionState) -> None:
        task = conn.reconnect_task
        if task is not None and not task.done():
            return
        conn.reconnect_task = self._spawn_background(self._reconnect_loop(conn))

    async def _refresh_startup_snapshot(
        self,
        conn: ConnectionState,
        *,
        timeout: float,
    ) -> None:
        if not conn.gun.is_connected:
            return
        try:
            async with conn.op_lock:
                snapshot = await conn.gun.query_startup_snapshot(timeout=timeout)
        except Exception:
            return
        conn.last_snapshot = _snapshot_to_dict(snapshot)

    def _cancel_reconnect_task(self, conn: ConnectionState) -> None:
        task = conn.reconnect_task
        conn.reconnect_task = None
        if task is not None and not task.done():
            task.cancel()

    async def _reconnect_loop(self, conn: ConnectionState) -> None:
        delay = RECONNECT_BASE_DELAY
        attempts = 0
        while conn.desired_connected and conn.auto_reconnect:
            if conn.gun.is_connected:
                conn.connection_state = "connected"
                return
            attempts += 1
            if attempts > RECONNECT_MAX_ATTEMPTS:
                conn.last_error = "reconnect attempts exceeded"
                self._schedule_event(
                    "connection",
                    {
                        "action": "reconnect_giveup",
                        "address": conn.address,
                        "name": conn.name,
                        "local_name": conn.local_name,
                        "display_name": self._display_name(conn),
                        "attempts": attempts - 1,
                    },
                )
                return
            try:
                await self._reconnect_once(conn, timeout=RECONNECT_SCAN_TIMEOUT)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                conn.last_error = str(exc)
                self._schedule_event(
                    "connection",
                    {
                        "action": "reconnect_failed",
                        "address": conn.address,
                        "name": conn.name,
                        "local_name": conn.local_name,
                        "display_name": self._display_name(conn),
                        "attempt": attempts,
                        "error": str(exc),
                    },
                )
            await asyncio.sleep(delay)
            delay = min(RECONNECT_MAX_DELAY, max(RECONNECT_BASE_DELAY, delay * 1.8))

    async def _ensure_connected(
        self,
        conn: ConnectionState,
        *,
        timeout: float = RECONNECT_SCAN_TIMEOUT,
    ) -> None:
        if conn.gun.is_connected:
            conn.connection_state = "connected"
            return
        if not conn.desired_connected:
            raise ConnectionError("device is disconnected by user request")
        await self._reconnect_once(conn, timeout=timeout)

    async def _reconnect_once(self, conn: ConnectionState, timeout: float) -> None:
        async with conn.reconnect_lock:
            if not conn.desired_connected:
                raise ConnectionError("reconnect blocked: desired_connected is false")
            if conn.gun.is_connected:
                conn.connection_state = "connected"
                return
            conn.connection_state = "reconnecting"
            conn.reconnect_attempts += 1
            self._schedule_event(
                "connection",
                {
                    "action": "reconnect_attempt",
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "attempt": conn.reconnect_attempts,
                },
            )

            reconnect_candidates: list[Any] = []
            if conn.ble_device is not None:
                reconnect_candidates.append(conn.ble_device)

            scanned_device = None
            async with self._discovery_lock:
                scanned_device = await BleakScanner.find_device_by_address(
                    conn.address,
                    timeout=timeout,
                )
            if scanned_device is not None:
                reconnect_candidates.append(scanned_device)

            if not reconnect_candidates:
                conn.connection_state = "disconnected"
                conn.last_error = f"device not found during reconnect: {conn.address}"
                raise LookupError(conn.last_error)

            old_gun = conn.gun
            notification_cb, disconnected_cb, write_cb = self._build_device_callbacks(
                conn
            )
            new_gun = None
            last_exc: Exception | None = None
            used_device = None
            for candidate in reconnect_candidates:
                trial_gun = LaserOpsDevice(
                    candidate,
                    notification_callback=notification_cb,
                    disconnected_callback=disconnected_cb,
                    write_callback=write_cb,
                )
                try:
                    async with self._connect_lock:
                        await trial_gun.__aenter__()
                except Exception as exc:
                    last_exc = exc
                    try:
                        await trial_gun.__aexit__(None, None, None)
                    except Exception:
                        pass
                    continue
                new_gun = trial_gun
                used_device = candidate
                break

            if new_gun is None:
                conn.connection_state = "disconnected"
                conn.last_error = str(last_exc) if last_exc is not None else (
                    f"reconnect failed: {conn.address}"
                )
                if last_exc is not None:
                    raise last_exc
                raise ConnectionError(conn.last_error)

            conn.gun = new_gun
            conn.ble_device = used_device
            conn.connection_state = "connected"
            conn.last_reconnect_at = time.time()
            conn.reconnect_count += 1
            conn.last_error = None
            self._cancel_reconnect_task(conn)
            await self._refresh_startup_snapshot(
                conn,
                timeout=CONNECT_STARTUP_PROBE_TIMEOUT,
            )
            try:
                await old_gun.__aexit__(None, None, None)
            except Exception:
                pass
            self._schedule_event(
                "connection",
                {
                    "action": "reconnected",
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "connection_state": conn.connection_state,
                    "reconnect_count": conn.reconnect_count,
                },
            )

    async def _start_game_session(
        self,
        participants: list[ConnectionState],
        *,
        duration_seconds: int,
    ) -> dict[str, Any]:
        safe_duration = self._validate_game_duration(duration_seconds)
        participant_keys = sorted({conn.address.lower() for conn in participants})
        for conn in participants:
            start_life = conn._configured_start_life()
            if start_life is not None:
                conn.live_state["last_life_counter"] = start_life
                conn.live_state["last_life_delta"] = 0
                conn.live_state["life_reset_source"] = "game_start"
            conn.live_state["current_life_damage_by_attacker_slot"] = {}
            conn.live_state["last_life_assist_slots"] = []
        baseline_shots: dict[str, int] = {}
        baseline_triggers: dict[str, int] = {}
        baseline_reloads: dict[str, int] = {}
        baseline_life_slot_stats = self._aggregate_life_slot_stats(participants)
        baseline_round_shot_reports: dict[str, int] = {}
        for conn in participants:
            key = conn.address.lower()
            baseline_shots[key] = self._live_shot_count(conn)
            baseline_triggers[key] = int(conn.live_state.get("trigger_count", 0) or 0)
            baseline_reloads[key] = int(conn.live_state.get("reload_count", 0) or 0)
            baseline_round_shot_reports[key] = int(
                conn.live_state.get("round_shots_report_count", 0) or 0
            )

        started_at = time.time()
        planned_end_at = started_at + float(safe_duration)

        self._cancel_game_end_task()
        async with self._game_session_lock:
            self._game_session_counter += 1
            session_id = self._game_session_counter
            self._game_session = GameSessionState(
                session_id=session_id,
                started_at=started_at,
                duration_seconds=safe_duration,
                planned_end_at=planned_end_at,
                participant_keys=participant_keys,
                baseline_shots=baseline_shots,
                baseline_triggers=baseline_triggers,
                baseline_reloads=baseline_reloads,
                baseline_life_slot_stats=baseline_life_slot_stats,
                baseline_round_shot_reports=baseline_round_shot_reports,
            )

        self._arm_game_end_task(session_id=session_id, planned_end_at=planned_end_at)
        ranking_snapshot = await self.game_ranking()
        self._schedule_event("ranking", ranking_snapshot)
        self._schedule_event(
            "game_session",
            {
                "action": "started",
                "session_id": session_id,
                "started_at": started_at,
                "duration_seconds": safe_duration,
                "planned_end_at": planned_end_at,
                "participants": ranking_snapshot.get("participants", []),
            },
        )
        return ranking_snapshot

    async def _publish_ranking_if_running(self, address_key: str) -> None:
        snapshot = await self.game_ranking()
        if not snapshot.get("running"):
            return
        participants = {str(item).lower() for item in snapshot.get("participants", [])}
        if address_key not in participants:
            return
        await self._publish_event(event_type="ranking", payload=snapshot)

    def _arm_game_end_task(self, *, session_id: int, planned_end_at: float) -> None:
        self._cancel_game_end_task()
        self._game_end_session_id = session_id
        self._game_end_task = self._spawn_background(
            self._auto_end_game_session(
                session_id=session_id,
                planned_end_at=planned_end_at,
            )
        )

    def _cancel_game_end_task(self) -> None:
        task = self._game_end_task
        self._game_end_task = None
        self._game_end_session_id = None
        if task is not None and not task.done():
            task.cancel()

    async def _auto_end_game_session(
        self,
        *,
        session_id: int,
        planned_end_at: float,
    ) -> None:
        delay = max(0.0, float(planned_end_at - time.time()))
        if delay > 0:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
        try:
            async with self._bulk_game_start_lock:
                await self._finalize_game_session(
                    reason="duration_elapsed",
                    send_close=True,
                    expected_session_id=session_id,
                )
        except asyncio.CancelledError:
            return
        except Exception:
            # Keep timer failures isolated; live gameplay must continue.
            return

    async def _finalize_game_session(
        self,
        *,
        reason: str,
        send_close: bool,
        collect_slot_stats: bool = True,
        expected_session_id: int | None = None,
    ) -> dict[str, Any]:
        async with self._game_session_lock:
            session = self._game_session
            if session is None:
                return self._empty_ranking_snapshot()
            if (
                expected_session_id is not None
                and session.session_id != expected_session_id
            ):
                if session.final_snapshot is not None:
                    return copy.deepcopy(session.final_snapshot)
                return copy.deepcopy(self._empty_ranking_snapshot())
            if session.final_snapshot is not None:
                return copy.deepcopy(session.final_snapshot)
            session.status = "ending"
            session_copy = copy.deepcopy(session)

        async with self._connections_lock:
            active_connections = dict(self._connections)
        participant_connections = [
            active_connections[key]
            for key in session_copy.participant_keys
            if key in active_connections
        ]

        if reason == "duration_elapsed":
            session_copy.round_shot_wait = await self._wait_for_round_shot_reports(
                session=session_copy,
                participant_connections=participant_connections,
                timeout=AUTO_END_ROUND_SHOTS_GRACE_SECONDS,
            )

        close_failures: list[dict[str, str]] = []
        if send_close:
            for conn in participant_connections:
                if not conn.gun.is_connected:
                    continue
                try:
                    async with conn.op_lock:
                        await conn.gun.close_session()
                except Exception as exc:
                    close_failures.append(
                        {
                            "address": conn.address,
                            "error": str(exc),
                        }
                    )

        slot_stats_map: dict[int, dict[str, int]] = {}
        stats_source_address: str | None = None
        stats_error: str | None = None
        slots = sorted(
            {
                int(conn.assigned_slot)
                for conn in participant_connections
                if conn.assigned_slot is not None
            }
        )
        if collect_slot_stats and slots:
            slot_stats_map, stats_source_address, stats_error = (
                await self._collect_round_slot_stats(
                    participant_connections=participant_connections,
                    slots=tuple(slots),
                )
            )

        ended_at = time.time()
        session_copy.status = "ended"
        session_copy.ended_at = ended_at
        session_copy.end_reason = reason
        session_copy.final_slot_stats = slot_stats_map
        session_copy.final_round_shots = self._collect_round_shot_reports(
            session=session_copy,
            participant_connections=participant_connections,
        )
        final_snapshot = self._build_session_snapshot(
            session=session_copy,
            active_connections=active_connections,
            running=False,
            close_failures=close_failures,
            stats_source_address=stats_source_address,
            stats_error=stats_error,
        )

        async with self._game_session_lock:
            live_session = self._game_session
            if (
                live_session is not None
                and live_session.session_id == session_copy.session_id
                and live_session.final_snapshot is None
            ):
                live_session.status = "ended"
                live_session.ended_at = ended_at
                live_session.end_reason = reason
                live_session.final_slot_stats = dict(slot_stats_map)
                live_session.final_round_shots = copy.deepcopy(
                    session_copy.final_round_shots
                )
                live_session.round_shot_wait = copy.deepcopy(
                    session_copy.round_shot_wait
                )
                live_session.final_snapshot = copy.deepcopy(final_snapshot)
                final_snapshot = copy.deepcopy(live_session.final_snapshot)
            elif live_session is not None and live_session.final_snapshot is not None:
                final_snapshot = copy.deepcopy(live_session.final_snapshot)

        if expected_session_id is None or expected_session_id == session_copy.session_id:
            self._cancel_game_end_task()
        self._schedule_event("ranking", final_snapshot)
        self._schedule_event(
            "game_session",
            {
                "action": "ended",
                "session_id": final_snapshot.get("session_id"),
                "started_at": final_snapshot.get("started_at"),
                "ended_at": final_snapshot.get("ended_at"),
                "end_reason": final_snapshot.get("end_reason"),
                "duration_seconds": final_snapshot.get("duration_seconds"),
                "participants": final_snapshot.get("participants", []),
            },
        )
        return final_snapshot

    async def _wait_for_round_shot_reports(
        self,
        *,
        session: GameSessionState,
        participant_connections: list[ConnectionState],
        timeout: float,
    ) -> dict[str, Any]:
        started = time.time()
        timeout_seconds = max(0.0, float(timeout))
        deadline = time.monotonic() + timeout_seconds

        def _missing() -> list[str]:
            missing: list[str] = []
            for conn in participant_connections:
                key = conn.address.lower()
                baseline = int(
                    session.baseline_round_shot_reports.get(key, 0) or 0
                )
                current = int(
                    conn.live_state.get("round_shots_report_count", 0) or 0
                )
                if current <= baseline:
                    missing.append(conn.address)
            return missing

        missing = _missing()
        while missing and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
            missing = _missing()

        return {
            "waited_seconds": round(time.time() - started, 3),
            "timeout_seconds": timeout_seconds,
            "complete": not missing,
            "missing": missing,
        }

    def _collect_round_shot_reports(
        self,
        *,
        session: GameSessionState,
        participant_connections: list[ConnectionState],
    ) -> dict[str, dict[str, Any]]:
        reports: dict[str, dict[str, Any]] = {}
        for conn in participant_connections:
            key = conn.address.lower()
            baseline = int(session.baseline_round_shot_reports.get(key, 0) or 0)
            current = int(conn.live_state.get("round_shots_report_count", 0) or 0)
            if current <= baseline:
                continue
            reports[key] = {
                "address": conn.address,
                "display_name": self._display_name(conn),
                "shots": conn.live_state.get("last_round_shots"),
                "raw": conn.live_state.get("last_round_shots_raw"),
                "ts": conn.live_state.get("last_round_shots_ts"),
                "report_count": current - baseline,
            }
        return reports

    async def _collect_round_slot_stats(
        self,
        *,
        participant_connections: list[ConnectionState],
        slots: tuple[int, ...],
    ) -> tuple[dict[int, dict[str, int]], str | None, str | None]:
        connected = [conn for conn in participant_connections if conn.gun.is_connected]
        if not connected:
            return {}, None, "no connected device available for round slot stats"

        slot_values: dict[int, list[tuple[str, int, int]]] = {
            int(slot): [] for slot in slots
        }
        source_errors: list[str] = []
        source_error_overflow = 0
        request_error_count = 0
        invalid_reply_count = 0
        slot_mismatch_count = 0
        unexpected_slot_count = 0
        used_sources: list[str] = []

        def _add_source_error(detail: str) -> None:
            nonlocal source_error_overflow
            if len(source_errors) < ROUND_SLOT_STATS_ERROR_DETAIL_LIMIT:
                source_errors.append(detail)
            else:
                source_error_overflow += 1

        for conn in connected:
            source_used = False
            async with conn.op_lock:
                for slot in slots:
                    requested_slot = int(slot)
                    try:
                        reply = await conn.gun.request_round_slot_stats(
                            slot=requested_slot,
                            timeout=2.5,
                        )
                    except Exception as exc:
                        request_error_count += 1
                        _add_source_error(
                            f"{conn.address}/slot{requested_slot}: {exc}"
                        )
                        continue
                    if not isinstance(reply, RoundSlotStatsReply):
                        invalid_reply_count += 1
                        _add_source_error(
                            f"{conn.address}/slot{requested_slot}: invalid round slot stats reply"
                        )
                        continue
                    slot_key = int(reply.slot)
                    if slot_key != requested_slot:
                        slot_mismatch_count += 1
                        _add_source_error(
                            f"{conn.address}/slot{requested_slot}: slot mismatch reply={slot_key}"
                        )
                        continue
                    if slot_key not in slot_values:
                        unexpected_slot_count += 1
                        _add_source_error(
                            f"{conn.address}/slot{requested_slot}: unexpected slot {slot_key}"
                        )
                        continue
                    slot_values[slot_key].append(
                        (conn.address, int(reply.hits), int(reply.kills))
                    )
                    source_used = True
            if source_used:
                used_sources.append(conn.address)

        merged_stats: dict[int, dict[str, int]] = {}
        missing_slots: list[int] = []
        inconsistent_slots: list[str] = []
        for slot in slots:
            slot_key = int(slot)
            values = list(slot_values.get(slot_key, []))
            if not values:
                missing_slots.append(slot_key)
                continue
            # 54 replies are per-slot totals; multiple blasters typically report
            # the same numbers. Never sum across sources.
            pairs = [(hits, kills) for _addr, hits, kills in values]
            unique_pairs = sorted(set(pairs))
            if len(unique_pairs) > 1:
                inconsistent_slots.append(
                    f"{slot_key}:"
                    + ",".join(f"{h}/{k}" for h, k in unique_pairs)
                )
            # Prefer the most complete observed pair (max hits, then max kills).
            best_hits, best_kills = max(unique_pairs, key=lambda hk: (hk[0], hk[1]))
            merged_stats[slot_key] = {
                "hits": int(best_hits),
                "kills": int(best_kills),
            }

        if not merged_stats:
            if source_errors:
                return {}, None, "; ".join(source_errors)
            return {}, None, "round slot stats unavailable"

        stats_source_address = ",".join(used_sources) if used_sources else None
        stats_error: str | None = None
        if missing_slots:
            stats_error = "missing slots: " + ", ".join(str(slot) for slot in missing_slots)
        if inconsistent_slots:
            inconsistency = "inconsistent slot stats: " + "; ".join(inconsistent_slots)
            if stats_error:
                stats_error = f"{stats_error} | {inconsistency}"
            else:
                stats_error = inconsistency
        anomaly_counts: list[str] = []
        if slot_mismatch_count > 0:
            anomaly_counts.append(f"slot_mismatch={slot_mismatch_count}")
        if invalid_reply_count > 0:
            anomaly_counts.append(f"invalid_reply={invalid_reply_count}")
        if unexpected_slot_count > 0:
            anomaly_counts.append(f"unexpected_slot={unexpected_slot_count}")
        if request_error_count > 0:
            anomaly_counts.append(f"request_error={request_error_count}")
        if anomaly_counts:
            anomaly_summary = "round slot stats anomalies: " + ", ".join(anomaly_counts)
            if stats_error:
                stats_error = f"{stats_error} | {anomaly_summary}"
            else:
                stats_error = anomaly_summary
        if source_errors:
            extra = "; ".join(source_errors)
            if source_error_overflow > 0:
                extra = f"{extra}; +{source_error_overflow} more"
            if stats_error:
                stats_error = f"{stats_error} | {extra}"
            else:
                stats_error = extra

        return merged_stats, stats_source_address, stats_error

    def _empty_ranking_snapshot(self) -> dict[str, Any]:
        return {
            "running": False,
            "session_state": "idle",
            "session_id": None,
            "started_at": None,
            "planned_end_at": None,
            "duration_seconds": None,
            "remaining_seconds": None,
            "ended_at": None,
            "end_reason": None,
            "participants": [],
            "ranking": [],
            "slot_stats": [],
            "round_shots": [],
            "round_shot_wait": None,
            "totals": {
                "shots": 0,
                "reloads": 0,
                "hits": None,
                "kills": None,
                "assists": None,
            },
            "stats_source_address": None,
            "stats_error": None,
            "close_failures": [],
        }

    def _build_session_snapshot(
        self,
        *,
        session: GameSessionState,
        active_connections: dict[str, ConnectionState],
        running: bool,
        close_failures: list[dict[str, str]] | None = None,
        stats_source_address: str | None = None,
        stats_error: str | None = None,
    ) -> dict[str, Any]:
        ranking = self._build_ranking_entries(
            session=session,
            active_connections=active_connections,
        )
        totals_shots = sum(int(item.get("shots", 0) or 0) for item in ranking)
        totals_reloads = sum(int(item.get("reloads", 0) or 0) for item in ranking)
        has_hits = any(item.get("hits") is not None for item in ranking)
        has_kills = any(item.get("kills") is not None for item in ranking)
        has_assists = any(item.get("assists") is not None for item in ranking)
        totals_hits = (
            sum(int(item.get("hits", 0) or 0) for item in ranking) if has_hits else None
        )
        totals_kills = (
            sum(int(item.get("kills", 0) or 0) for item in ranking)
            if has_kills
            else None
        )
        totals_assists = (
            sum(int(item.get("assists", 0) or 0) for item in ranking)
            if has_assists
            else None
        )
        life_slot_stats = self._session_life_slot_stats(
            session=session,
            active_connections=active_connections,
        )
        slot_stats_rows: list[dict[str, Any]] = []
        row_slots = set(session.final_slot_stats)
        for key in session.participant_keys:
            conn = active_connections.get(key)
            if conn is not None and conn.assigned_slot is not None:
                row_slots.add(int(conn.assigned_slot))
        if row_slots:
            for slot in sorted(row_slots):
                stats = session.final_slot_stats.get(
                    slot, {"hits": 0, "kills": 0, "assists": 0}
                )
                life_stats = life_slot_stats.get(slot)
                source = "round_slot_stats"
                if life_stats is not None and (
                    int(life_stats.get("hits", 0) or 0) > 0
                    or int(life_stats.get("kills", 0) or 0) > 0
                    or int(life_stats.get("assists", 0) or 0) > 0
                ):
                    stats = life_stats
                    source = "life_delta"
                mapped_conn: ConnectionState | None = None
                for key in session.participant_keys:
                    conn = active_connections.get(key)
                    if conn is None:
                        continue
                    if conn.assigned_slot == slot:
                        mapped_conn = conn
                        break
                slot_stats_rows.append(
                    {
                        "slot": int(slot),
                        "hits": int(stats.get("hits", 0)),
                        "kills": int(stats.get("kills", 0)),
                        "assists": int(stats.get("assists", 0)),
                        "address": mapped_conn.address if mapped_conn else None,
                        "display_name": self._display_name(mapped_conn)
                        if mapped_conn is not None
                        else None,
                        "team": mapped_conn.assigned_team if mapped_conn else None,
                        "source": source,
                    }
                )
        participants = [
            active_connections[key].address
            for key in session.participant_keys
            if key in active_connections
        ]
        round_shot_rows: list[dict[str, Any]] = []
        for key in session.participant_keys:
            report = session.final_round_shots.get(key)
            if not report:
                continue
            round_shot_rows.append(copy.deepcopy(report))
        remaining_seconds = None
        if running:
            remaining_seconds = max(
                0,
                int(round(float(session.planned_end_at - time.time()))),
            )
        return {
            "running": bool(running),
            "session_state": "running" if running else "ended",
            "session_id": session.session_id,
            "started_at": session.started_at,
            "planned_end_at": session.planned_end_at,
            "duration_seconds": session.duration_seconds,
            "remaining_seconds": remaining_seconds,
            "ended_at": session.ended_at,
            "end_reason": session.end_reason,
            "participants": participants,
            "ranking": ranking,
            "slot_stats": slot_stats_rows,
            "round_shots": round_shot_rows,
            "round_shot_wait": copy.deepcopy(session.round_shot_wait),
            "totals": {
                "shots": totals_shots,
                "reloads": totals_reloads,
                "hits": totals_hits,
                "kills": totals_kills,
                "assists": totals_assists,
            },
            "stats_source_address": stats_source_address,
            "stats_error": stats_error,
            "close_failures": close_failures or [],
        }

    @staticmethod
    def _live_shot_count(conn: ConnectionState) -> int:
        state = conn.live_state
        try:
            shot_count = int(state.get("shot_count", 0) or 0)
        except (TypeError, ValueError):
            shot_count = 0
        if state.get("shot_count_source") == "ammo_delta" or shot_count > 0:
            return max(0, shot_count)
        try:
            return max(0, int(state.get("trigger_count", 0) or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _coerce_slot_counter_map(raw: Any) -> dict[int, int]:
        if not isinstance(raw, dict):
            return {}
        out: dict[int, int] = {}
        for key, value in raw.items():
            try:
                slot = int(key)
                count = int(value)
            except (TypeError, ValueError):
                continue
            if slot > 0 and count > 0:
                out[slot] = out.get(slot, 0) + count
        return out

    def _aggregate_life_slot_stats(
        self,
        connections: list[ConnectionState],
    ) -> dict[int, dict[str, int]]:
        stats: dict[int, dict[str, int]] = {}
        for conn in connections:
            hits_by_slot = self._coerce_slot_counter_map(
                conn.live_state.get("received_hits_by_attacker_slot")
            )
            kills_by_slot = self._coerce_slot_counter_map(
                conn.live_state.get("received_kills_by_attacker_slot")
            )
            assists_by_slot = self._coerce_slot_counter_map(
                conn.live_state.get("received_assists_by_attacker_slot")
            )
            for slot in set(hits_by_slot) | set(kills_by_slot) | set(assists_by_slot):
                bucket = stats.setdefault(slot, {"hits": 0, "kills": 0, "assists": 0})
                bucket["hits"] += int(hits_by_slot.get(slot, 0) or 0)
                bucket["kills"] += int(kills_by_slot.get(slot, 0) or 0)
                bucket["assists"] += int(assists_by_slot.get(slot, 0) or 0)
        return stats

    def _session_life_slot_stats(
        self,
        *,
        session: GameSessionState,
        active_connections: dict[str, ConnectionState],
    ) -> dict[int, dict[str, int]]:
        conns = [
            active_connections[key]
            for key in session.participant_keys
            if key in active_connections
        ]
        current = self._aggregate_life_slot_stats(conns)
        out: dict[int, dict[str, int]] = {}
        for slot, stats in current.items():
            baseline = session.baseline_life_slot_stats.get(
                int(slot), {"hits": 0, "kills": 0, "assists": 0}
            )
            hits = max(
                0,
                int(stats.get("hits", 0) or 0)
                - int(baseline.get("hits", 0) or 0),
            )
            kills = max(
                0,
                int(stats.get("kills", 0) or 0)
                - int(baseline.get("kills", 0) or 0),
            )
            assists = max(
                0,
                int(stats.get("assists", 0) or 0)
                - int(baseline.get("assists", 0) or 0),
            )
            if hits > 0 or kills > 0 or assists > 0:
                out[int(slot)] = {"hits": hits, "kills": kills, "assists": assists}
        return out

    def _build_ranking_entries(
        self,
        *,
        session: GameSessionState,
        active_connections: dict[str, ConnectionState],
    ) -> list[dict[str, Any]]:
        ranking: list[dict[str, Any]] = []
        slot_stats = session.final_slot_stats or {}
        life_slot_stats = self._session_life_slot_stats(
            session=session,
            active_connections=active_connections,
        )

        for key in session.participant_keys:
            conn = active_connections.get(key)
            if conn is None:
                continue
            shot_now = self._live_shot_count(conn)
            trigger_now = int(conn.live_state.get("trigger_count", 0) or 0)
            reload_now = int(conn.live_state.get("reload_count", 0) or 0)
            shots = max(0, shot_now - int(session.baseline_shots.get(key, 0)))
            reloads = max(0, reload_now - int(session.baseline_reloads.get(key, 0)))
            slot = conn.assigned_slot
            slot_stat = (
                slot_stats.get(int(slot)) if slot is not None else None
            )
            hits = None
            kills = None
            assists = None
            accuracy = None
            hit_stats_source = None
            life_stat = life_slot_stats.get(int(slot)) if slot is not None else None
            if life_stat is not None:
                hits = int(life_stat.get("hits", 0))
                kills = int(life_stat.get("kills", 0))
                assists = int(life_stat.get("assists", 0))
                hit_stats_source = "life_delta"
                if shots > 0:
                    accuracy = round((hits / shots) * 100.0, 1)
            elif slot_stat is not None:
                hits = int(slot_stat.get("hits", 0))
                kills = int(slot_stat.get("kills", 0))
                if "assists" in slot_stat:
                    assists = int(slot_stat.get("assists", 0))
                hit_stats_source = "round_slot_stats"
                if shots > 0:
                    accuracy = round((hits / shots) * 100.0, 1)
            ranking.append(
                {
                    "address": conn.address,
                    "name": conn.name,
                    "local_name": conn.local_name,
                    "display_name": self._display_name(conn),
                    "slot": conn.assigned_slot,
                    "team": conn.assigned_team,
                    "shots": shots,
                    "reloads": reloads,
                    "hits": hits,
                    "kills": kills,
                    "assists": assists,
                    "accuracy_percent": accuracy,
                    "hit_stats_source": hit_stats_source,
                    "connection_state": conn.connection_state,
                    "last_disconnect_reason": conn.last_disconnect_reason,
                    "last_error": conn.last_error,
                }
            )

        use_slot_stats = any(item.get("kills") is not None for item in ranking)
        if use_slot_stats:
            ranking.sort(
                key=lambda item: (
                    -int(item["kills"] if item["kills"] is not None else -1),
                    -int(item["assists"] if item["assists"] is not None else -1),
                    -int(item["hits"] if item["hits"] is not None else -1),
                    -int(item["shots"]),
                    int(item["reloads"]),
                    str(item["display_name"]).lower(),
                    str(item["address"]).lower(),
                )
            )
        else:
            ranking.sort(
                key=lambda item: (
                    -int(item["shots"]),
                    int(item["reloads"]),
                    str(item["display_name"]).lower(),
                    str(item["address"]).lower(),
                )
            )
        for idx, item in enumerate(ranking, start=1):
            item["rank"] = idx
        return ranking

    def _display_name(self, conn: ConnectionState) -> str:
        if conn.local_name:
            return conn.local_name
        if conn.name:
            return conn.name
        return conn.address

    def _normalize_local_name(self, local_name: str | None) -> str | None:
        if local_name is None:
            return None
        value = str(local_name).strip()
        if not value:
            return None
        if "\n" in value or "\r" in value or "\t" in value:
            raise ValueError("local name contains invalid control characters")
        if len(value) > LOCAL_NAME_MAX_LENGTH:
            raise ValueError(
                f"local name too long ({len(value)} > {LOCAL_NAME_MAX_LENGTH})"
            )
        return value

    def _load_local_name_store(
        self,
    ) -> tuple[dict[str, str], dict[str, tuple[int, int]]]:
        if not self._local_name_store_path.exists():
            return {}, {}
        try:
            payload = json.loads(self._local_name_store_path.read_text(encoding="utf-8"))
        except Exception:
            return {}, {}
        if not isinstance(payload, dict):
            return {}, {}

        names: dict[str, str] = {}
        team_profiles: dict[str, tuple[int, int]] = {}
        for raw_address, raw_value in payload.items():
            if not isinstance(raw_address, str):
                continue
            try:
                key = self._normalize_address(raw_address).lower()
            except Exception:
                continue

            if isinstance(raw_value, str):
                safe_name = self._normalize_local_name(raw_value)
                if safe_name is not None:
                    names[key] = safe_name
                continue

            if not isinstance(raw_value, dict):
                continue

            safe_name = self._normalize_local_name(raw_value.get("local_name"))
            if safe_name is not None:
                names[key] = safe_name

            try:
                safe_slot, safe_team = self._validate_safe_team(
                    slot=int(raw_value.get("slot")),
                    team=int(raw_value.get("team")),
                )
            except Exception:
                continue
            team_profiles[key] = (safe_slot, safe_team)
        return names, team_profiles

    def _save_local_name_store(self) -> None:
        path = self._local_name_store_path
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        payload_map: dict[str, dict[str, Any]] = {}
        for address in sorted(
            set(self._local_names.keys()) | set(self._local_team_profiles.keys())
        ):
            item: dict[str, Any] = {}
            name = self._local_names.get(address)
            if name:
                item["local_name"] = str(name)
            team_profile = self._local_team_profiles.get(address)
            if team_profile is not None:
                slot, team = team_profile
                item["slot"] = int(slot)
                item["team"] = int(team)
            if item:
                payload_map[str(address)] = item
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                payload_map,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n"
            tmp_path.write_text(payload, encoding="utf-8")
            tmp_path.replace(path)
        except Exception as exc:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise RuntimeError(f"failed to persist local names: {exc}") from exc

    def _validate_safe_config(
        self,
        level: int,
        name_a: int,
        name_b: int,
        *,
        ammo_profile: int | None = None,
        damage_profile: int | None = None,
        health_profile: int | None = None,
    ) -> None:
        if level < SAFE_LEVEL_MIN or level > SAFE_LEVEL_MAX:
            raise ValueError(
                f"unsafe level: {level}. allowed range is "
                f"{SAFE_LEVEL_MIN}..{SAFE_LEVEL_MAX}"
            )
        if name_a < SAFE_NAME_INDEX_MIN or name_a > SAFE_NAME_INDEX_MAX:
            raise ValueError(
                f"unsafe name_a index: {name_a}. allowed range is "
                f"{SAFE_NAME_INDEX_MIN}..{SAFE_NAME_INDEX_MAX}"
            )
        if name_b < SAFE_NAME_INDEX_MIN or name_b > SAFE_NAME_INDEX_MAX:
            raise ValueError(
                f"unsafe name_b index: {name_b}. allowed range is "
                f"{SAFE_NAME_INDEX_MIN}..{SAFE_NAME_INDEX_MAX}"
            )
        self._validate_optional_profile_byte(
            label="ammo_profile",
            value=ammo_profile,
        )
        self._validate_optional_profile_byte(
            label="damage_profile",
            value=damage_profile,
        )
        self._validate_optional_profile_byte(
            label="health_profile",
            value=health_profile,
        )

    def _validate_optional_profile_byte(
        self, *, label: str, value: int | None
    ) -> None:
        if value is None:
            return
        if value < SAFE_PROFILE_BYTE_MIN or value > SAFE_PROFILE_BYTE_MAX:
            raise ValueError(
                f"unsafe {label}: {value}. allowed range is "
                f"{SAFE_PROFILE_BYTE_MIN}..{SAFE_PROFILE_BYTE_MAX}"
            )

    def _validate_safe_team(self, *, slot: int, team: int) -> tuple[int, int]:
        if slot < SAFE_SLOT_MIN or slot > SAFE_SLOT_MAX:
            raise ValueError(
                f"unsafe slot: {slot}. allowed range is "
                f"{SAFE_SLOT_MIN}..{SAFE_SLOT_MAX}"
            )
        if team < SAFE_TEAM_MIN or team > SAFE_TEAM_MAX:
            raise ValueError(
                f"unsafe team: {team}. allowed range is "
                f"{SAFE_TEAM_MIN}..{SAFE_TEAM_MAX}"
            )
        return int(slot), int(team)

    def _resolve_team_for_start(
        self,
        *,
        conn: ConnectionState,
        slot: int | None,
        team: int | None,
    ) -> tuple[int, int]:
        selected_slot = conn.assigned_slot if slot is None else slot
        selected_team = conn.assigned_team if team is None else team
        if (
            selected_slot is None
            or int(selected_slot) < SAFE_SLOT_MIN
            or int(selected_slot) > SAFE_SLOT_MAX
        ):
            selected_slot = SAFE_SLOT_MIN
        if selected_team is None:
            selected_team = self._default_team_for_slot(selected_slot)
        elif selected_team < SAFE_TEAM_MIN or selected_team > SAFE_TEAM_MAX:
            # Migrate stale assignments from older UI versions.
            selected_team = self._default_team_for_slot(selected_slot)
        safe_slot, safe_team = self._validate_safe_team(
            slot=selected_slot, team=selected_team
        )
        conn.assigned_slot = safe_slot
        conn.assigned_team = safe_team
        return safe_slot, safe_team

    def _resolve_multi_team_profiles(
        self, conns: list[ConnectionState]
    ) -> dict[str, tuple[int, int]]:
        used: set[int] = set()
        profiles: dict[str, tuple[int, int]] = {}

        for conn in conns:
            if conn.assigned_slot is None or conn.assigned_team is None:
                continue
            raw_slot = int(conn.assigned_slot)
            raw_team = int(conn.assigned_team)
            if raw_slot < SAFE_SLOT_MIN or raw_slot > SAFE_SLOT_MAX:
                continue
            if raw_team < SAFE_TEAM_MIN or raw_team > SAFE_TEAM_MAX:
                safe_slot = raw_slot
                safe_team = SAFE_MULTIPLAYER_FFA_TEAM
            else:
                safe_slot = raw_slot
                safe_team = raw_team
            if safe_slot in used:
                continue
            profiles[conn.address.lower()] = (safe_slot, safe_team)
            used.add(safe_slot)

        next_slot = SAFE_SLOT_MIN
        for conn in conns:
            key = conn.address.lower()
            if key in profiles:
                continue
            while next_slot in used and next_slot <= SAFE_SLOT_MAX:
                next_slot += 1
            if next_slot > SAFE_SLOT_MAX:
                next_slot = SAFE_SLOT_MIN
                while next_slot in used and next_slot <= SAFE_SLOT_MAX:
                    next_slot += 1
            if next_slot > SAFE_SLOT_MAX:
                next_slot = SAFE_SLOT_MIN
            slot = next_slot
            existing_teams = {team for _slot, team in profiles.values()}
            if existing_teams == {SAFE_MULTIPLAYER_FFA_TEAM}:
                team = SAFE_MULTIPLAYER_FFA_TEAM
            elif existing_teams and existing_teams.isdisjoint(
                {SAFE_MULTIPLAYER_FFA_TEAM}
            ):
                team = self._default_team_for_slot(slot)
            else:
                team = SAFE_MULTIPLAYER_FFA_TEAM
            profiles[key] = (slot, team)
            conn.assigned_slot = slot
            conn.assigned_team = team
            used.add(slot)
            next_slot += 1
        return profiles

    def _validate_multi_team_mode(
        self,
        profiles: dict[str, tuple[int, int]],
    ) -> None:
        teams = {int(team) for _slot, team in profiles.values()}
        if not teams:
            return
        if teams == {SAFE_MULTIPLAYER_FFA_TEAM}:
            return
        if teams.issubset({0, 1}):
            return
        raise ValueError(
            "unsupported multiplayer team mix: use either red-vs-blue only "
            "(teams 0/1) or all violet (team 2). mixed violet with red/blue "
            "causes hit counting to fail"
        )

    def _default_team_for_slot(self, slot: int) -> int:
        # Keep a stable 0/1 rotation independent of absolute slot numbers.
        return int((int(slot) - SAFE_SLOT_MIN) % 2)

    def _validate_safe_game_delay(self, delay: float) -> float:
        if delay < SAFE_GAME_DELAY_MIN or delay > SAFE_GAME_DELAY_MAX:
            raise ValueError(
                f"unsafe game delay: {delay}. allowed range is "
                f"{SAFE_GAME_DELAY_MIN:.2f}..{SAFE_GAME_DELAY_MAX:.2f} s"
            )
        return float(delay)

    def _validate_game_duration(self, duration_seconds: int) -> int:
        value = int(duration_seconds)
        if (
            value < SAFE_GAME_DURATION_SECONDS_MIN
            or value > SAFE_GAME_DURATION_SECONDS_MAX
        ):
            raise ValueError(
                f"unsafe game duration: {value}. allowed range is "
                f"{SAFE_GAME_DURATION_SECONDS_MIN}..{SAFE_GAME_DURATION_SECONDS_MAX} s"
            )
        return value

    def _sanitize_end_reason(self, reason: str) -> str:
        value = str(reason or "").strip().lower().replace(" ", "_")
        if not value:
            return "manual_stop"
        safe = "".join(ch for ch in value if ch.isalnum() or ch in ("_", "-", "."))
        if not safe:
            return "manual_stop"
        return safe[:64]

    def _format_error(self, exc: Exception) -> str:
        name = exc.__class__.__name__
        text = str(exc).strip()
        if not text:
            text = repr(exc).strip()
        if not text:
            return name
        if text.startswith(name):
            return text
        return f"{name}: {text}"

    async def _start_one_multi_connection(
        self,
        *,
        conn: ConnectionState,
        slot: int,
        team: int,
        startup_volume: int,
        delay: float,
        duration_seconds: int,
        force_startup: bool,
    ) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        try:
            snapshot, recovery_used = await self._run_multiplayer_start_sequence(
                conn=conn,
                slot=slot,
                team=team,
                startup_volume=startup_volume,
                delay=delay,
                duration_seconds=duration_seconds,
                allow_reconnect=False,
                reconnect_timeout=SAFE_MULTI_RECONNECT_TIMEOUT,
            )
            conn.last_snapshot = _snapshot_to_dict(snapshot)
            conn.last_game_start_at = time.time()
        except Exception as exc:
            return None, {
                "address": conn.address,
                "error": self._format_error(exc),
            }

        self._schedule_event(
            "game_start",
            {
                "address": conn.address,
                "name": conn.name,
                "startup_performed": True,
                "force_startup": force_startup,
                "delay": delay,
                "duration_seconds": duration_seconds,
                "slot": slot,
                "team": team,
                "game_start_at": conn.last_game_start_at,
                "multi": True,
                "recovery_used": recovery_used,
            },
        )
        return {
            "address": conn.address,
            "startup_performed": True,
            "last_snapshot": conn.last_snapshot,
            "slot": slot,
            "team": team,
            "duration_seconds": duration_seconds,
            "game_start_at": conn.last_game_start_at,
            "recovery_used": recovery_used,
        }, None

    async def _run_multiplayer_start_sequence(
        self,
        *,
        conn: ConnectionState,
        slot: int,
        team: int,
        startup_volume: int,
        delay: float,
        duration_seconds: int,
        allow_reconnect: bool = True,
        reconnect_timeout: float = RECONNECT_SCAN_TIMEOUT,
    ) -> tuple[Any, bool]:
        """
        Run multiplayer start sequence with one guarded recovery retry.

        Recovery path:
          1) ensure connection
          2) run confirmed startup exchange (35/5b)
          3) retry multiplayer setup sequence
        """
        last_error: Exception | None = None
        for attempt in (1, 2):
            try:
                if allow_reconnect:
                    await self._ensure_connected(conn, timeout=reconnect_timeout)
                elif not conn.gun.is_connected:
                    raise ConnectionError(
                        f"device not connected for multiplayer start: {conn.address}"
                    )
                async with conn.op_lock:
                    if attempt == 2:
                        await conn.gun.startup(volume=startup_volume)
                    snapshot = await conn.gun.send_multiplayer_game_setup(
                        delay=delay,
                        slot=slot,
                        team=team,
                        volume=startup_volume,
                        duration_seconds=duration_seconds,
                    )
                    # Some blasters return an all-zero startup profile during
                    # game setup and then stay in blinking pre-start state.
                    # Bootstrap a minimal profile and retry once in-session.
                    if (
                        int(getattr(snapshot, "level", 0) or 0) == 0
                        and int(getattr(snapshot, "name_a", 0) or 0) == 0
                        and int(getattr(snapshot, "name_b", 0) or 0) == 0
                    ):
                        await conn.gun.write_config(level=1, name_a=1, name_b=1)
                        snapshot = await conn.gun.send_multiplayer_game_setup(
                            delay=delay,
                            slot=slot,
                            team=team,
                            volume=startup_volume,
                            duration_seconds=duration_seconds,
                        )
                return snapshot, (attempt == 2)
            except Exception as exc:
                last_error = exc
                if attempt == 1:
                    continue
                break
        assert last_error is not None
        raise last_error

    async def subscribe_events(
        self, *, max_queue_size: int = LIVE_SUBSCRIBER_QUEUE_SIZE
    ) -> tuple[int, asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=max_queue_size)
        async with self._subscribers_lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = queue
        return subscriber_id, queue

    async def unsubscribe_events(self, subscriber_id: int) -> None:
        async with self._subscribers_lock:
            self._subscribers.pop(subscriber_id, None)

    async def live_snapshot(self) -> dict[str, Any]:
        return {
            "generated_at": time.time(),
            "connections": await self.list_connections(),
        }

    def _spawn_background(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    def _schedule_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._spawn_background(
            self._publish_event(event_type=event_type, payload=payload)
        )

    async def _publish_event(self, *, event_type: str, payload: dict[str, Any]) -> None:
        event = self._build_event(event_type=event_type, payload=payload)
        await self._append_event_to_debug_log(event)
        async with self._subscribers_lock:
            queues = list(self._subscribers.values())
        for queue in queues:
            self._queue_put_drop_oldest(queue, event)

    async def _append_event_to_debug_log(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=True, separators=(",", ":"))
        async with self._debug_event_log_lock:
            await asyncio.to_thread(self._append_line_to_file, line)

    def _append_line_to_file(self, line: str) -> None:
        path = self._debug_event_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")

    def _build_event(self, *, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._event_seq += 1
        return {
            "seq": self._event_seq,
            "ts": time.time(),
            "type": event_type,
            "payload": payload,
        }

    def _queue_put_drop_oldest(
        self, queue: asyncio.Queue[dict[str, Any]], event: dict[str, Any]
    ) -> None:
        try:
            queue.put_nowait(event)
            return
        except asyncio.QueueFull:
            pass
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
