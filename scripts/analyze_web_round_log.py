from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


RELEVANT_TX_PREFIXES = ("49", "4a", "5b", "35", "58", "42", "54")
RELEVANT_RX_PREFIXES = ("35", "47", "54", "49", "52", "31", "32", "30", "3e", "3f")
RELEVANT_EVENT_TYPES = {
    "tx_packet",
    "notification",
    "connection",
    "game_start",
    "game_session",
    "ranking",
}


@dataclass
class StartSequence:
    address: str
    number: int
    first_ts: float
    slot: int | None = None
    team: int | None = None
    duration_seconds: int | None = None
    volume: int | None = None
    startup_query_count: int = 0
    startup_snapshot_count: int = 0
    armed: bool = False
    tx_raw: list[str] = field(default_factory=list)
    rx_raw: list[str] = field(default_factory=list)


def default_log_path() -> Path:
    return Path(__file__).resolve().parents[1] / "webapp" / "backend" / "state" / "live_event_log.ndjson"


def default_marker_path() -> Path:
    return Path(__file__).resolve().parents[1] / "webapp" / "backend" / "state" / "debug_round_marker.json"


def format_ts(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def load_marker(path: Path) -> float | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    marker_ts = payload.get("ts")
    return float(marker_ts) if marker_ts is not None else None


def write_marker(path: Path) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    marker_ts = time.time()
    payload = {
        "ts": marker_ts,
        "local_time": dt.datetime.fromtimestamp(marker_ts).isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return marker_ts


def parse_ndjson(path: Path, since_ts: float | None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                events.append(
                    {
                        "ts": 0.0,
                        "seq": None,
                        "type": "parse_error",
                        "payload": {"line": line_no, "error": str(exc)},
                    }
                )
                continue
            ts = float(event.get("ts") or 0.0)
            if since_ts is not None and ts < since_ts:
                continue
            events.append(event)
    return events


def packet_payload(event: dict[str, Any]) -> tuple[str, str, str, dict[str, Any]]:
    payload = event.get("payload") or {}
    packet = payload.get("packet") or {}
    address = str(payload.get("address") or "")
    raw = str(packet.get("raw") or "").lower()
    decoded = str(packet.get("decoded") or "")
    derived = packet.get("derived") if isinstance(packet.get("derived"), dict) else {}
    return address, raw, decoded, derived


def is_relevant_packet(event: dict[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type == "tx_packet":
        _address, raw, _decoded, _derived = packet_payload(event)
        return raw.startswith(RELEVANT_TX_PREFIXES)
    if event_type == "notification":
        _address, raw, _decoded, _derived = packet_payload(event)
        return raw.startswith(RELEVANT_RX_PREFIXES)
    return event_type in {"connection", "game_start", "game_session"}


def short_address(address: str) -> str:
    if len(address) >= 5:
        return address[-5:]
    return address or "?"


def decode_duration_from_4a(raw: str) -> int | None:
    try:
        payload = bytes.fromhex(raw)
    except ValueError:
        return None
    if len(payload) != 12 or payload[0] != 0x4A:
        return None
    return (payload[4] << 8) | payload[5]


def render_event_line(event: dict[str, Any], base_ts: float | None) -> str:
    ts = float(event.get("ts") or 0.0)
    delta = ""
    if base_ts is not None and ts > 0:
        delta = f"+{ts - base_ts:8.3f}s "
    prefix = f"{delta}{format_ts(ts) if ts else 'no-ts'}"
    seq = event.get("seq")
    seq_text = f" #{seq}" if seq is not None else ""
    event_type = str(event.get("type") or "?")

    if event_type in {"tx_packet", "notification"}:
        address, raw, decoded, derived = packet_payload(event)
        direction = "TX" if event_type == "tx_packet" else "RX"
        slot = derived.get("slot")
        team = derived.get("team")
        extra: list[str] = []
        if slot is not None:
            extra.append(f"slot={slot}")
        if team is not None:
            extra.append(f"team={team}")
        if "duration_seconds" in derived:
            extra.append(f"duration={derived['duration_seconds']}s")
        if "volume" in derived:
            extra.append(f"volume={derived['volume']}")
        if "round_shots" in derived:
            extra.append(f"shots={derived['round_shots']}")
        if "hits" in derived:
            extra.append(f"hits={derived.get('hits')}")
        if "kills" in derived:
            extra.append(f"kills={derived.get('kills')}")
        extra_text = f" ({', '.join(extra)})" if extra else ""
        return f"{prefix}{seq_text} {direction:<2} {short_address(address):>5} {raw:<26} {decoded}{extra_text}"

    payload = event.get("payload") or {}
    if event_type == "game_session":
        action = payload.get("action")
        session_id = payload.get("session_id")
        participants = payload.get("participants") or []
        return f"{prefix}{seq_text} SESSION action={action} id={session_id} participants={len(participants)}"
    if event_type == "game_start":
        return (
            f"{prefix}{seq_text} GAME_START {short_address(str(payload.get('address') or ''))} "
            f"slot={payload.get('slot')} team={payload.get('team')} recovery={payload.get('recovery_used')}"
        )
    if event_type == "connection":
        return (
            f"{prefix}{seq_text} CONNECTION {short_address(str(payload.get('address') or ''))} "
            f"action={payload.get('action')} state={payload.get('connection_state')}"
        )
    return f"{prefix}{seq_text} {event_type} {json.dumps(payload, ensure_ascii=True, sort_keys=True)}"


def summarize(events: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    if not events:
        return "No matching events found."

    first_ts = min(float(event.get("ts") or 0.0) for event in events if float(event.get("ts") or 0.0) > 0)
    last_ts = max(float(event.get("ts") or 0.0) for event in events if float(event.get("ts") or 0.0) > 0)

    tx_counts: dict[str, Counter[str]] = defaultdict(Counter)
    rx_counts: dict[str, Counter[str]] = defaultdict(Counter)
    start_sequences: dict[str, list[StartSequence]] = defaultdict(list)
    active_sequence: dict[str, StartSequence] = {}
    warnings: list[str] = []

    game_sessions = [event for event in events if event.get("type") == "game_session"]
    connections = [event for event in events if event.get("type") == "connection"]

    for event in events:
        event_type = event.get("type")
        if event_type not in {"tx_packet", "notification"}:
            continue
        address, raw, _decoded, derived = packet_payload(event)
        if not address or not raw:
            continue
        key = address.lower()
        ts = float(event.get("ts") or 0.0)
        family = raw[:2]

        if event_type == "tx_packet":
            tx_counts[key][family] += 1
            if family == "49" and len(raw) >= 6:
                current = active_sequence.get(key)
                if current is not None and not current.armed:
                    warnings.append(
                        f"{address}: new 49 before previous start sequence was armed with 58"
                    )
                seq = StartSequence(
                    address=address,
                    number=len(start_sequences[key]) + 1,
                    first_ts=ts,
                    slot=derived.get("slot"),
                    team=derived.get("team"),
                )
                seq.tx_raw.append(raw)
                start_sequences[key].append(seq)
                active_sequence[key] = seq
                continue

            seq = active_sequence.get(key)
            if seq is not None:
                seq.tx_raw.append(raw)
                if family == "4a":
                    seq.duration_seconds = derived.get("duration_seconds") or decode_duration_from_4a(raw)
                elif family == "5b":
                    seq.volume = derived.get("volume")
                elif raw == "35":
                    seq.startup_query_count += 1
                elif raw == "58":
                    seq.armed = True
            continue

        rx_counts[key][family] += 1
        seq = active_sequence.get(key)
        if seq is not None:
            seq.rx_raw.append(raw)
            if family == "35":
                seq.startup_snapshot_count += 1

    for key, sequences in start_sequences.items():
        for seq in sequences:
            if not seq.armed:
                warnings.append(
                    f"{seq.address}: start sequence {seq.number} missing TX 58 arm command"
                )
            if seq.startup_query_count == 0:
                warnings.append(
                    f"{seq.address}: start sequence {seq.number} missing TX 35 startup query"
                )
            if seq.startup_snapshot_count == 0:
                warnings.append(
                    f"{seq.address}: start sequence {seq.number} missing RX 35 startup snapshot"
                )

    sequence_counts = {key: len(value) for key, value in start_sequences.items()}
    if sequence_counts and len(set(sequence_counts.values())) > 1:
        warnings.append(f"uneven start sequence counts per blaster: {sequence_counts}")
    ended_sessions = [
        event
        for event in game_sessions
        if (event.get("payload") or {}).get("action") == "ended"
    ]
    if ended_sessions:
        for key, sequences in start_sequences.items():
            if sequences and rx_counts[key].get("47", 0) == 0:
                warnings.append(
                    f"{sequences[0].address}: no RX 47 round-shot report observed after ended session"
                )

    lines.append("Window")
    lines.append(f"  events: {len(events)}")
    lines.append(f"  first:  {format_ts(first_ts)}")
    lines.append(f"  last:   {format_ts(last_ts)}")
    lines.append(f"  span:   {last_ts - first_ts:.3f}s")
    lines.append("")

    lines.append("Start Sequences")
    if start_sequences:
        for key in sorted(start_sequences):
            for seq in start_sequences[key]:
                status = "armed" if seq.armed else "not-armed"
                lines.append(
                    "  "
                    f"{short_address(seq.address)} #{seq.number}: "
                    f"slot={seq.slot} team={seq.team} duration={seq.duration_seconds} "
                    f"volume={seq.volume} tx35={seq.startup_query_count} "
                    f"rx35={seq.startup_snapshot_count} {status}"
                )
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Packet Counts")
    addresses = sorted(set(tx_counts) | set(rx_counts))
    if addresses:
        for key in addresses:
            display = short_address(key)
            tx = " ".join(f"{family}={count}" for family, count in sorted(tx_counts[key].items()))
            rx = " ".join(f"{family}={count}" for family, count in sorted(rx_counts[key].items()))
            lines.append(f"  {display} TX: {tx or '-'}")
            lines.append(f"  {display} RX: {rx or '-'}")
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Lifecycle Events")
    if game_sessions or connections:
        for event in sorted(game_sessions + connections, key=lambda item: float(item.get("ts") or 0.0)):
            lines.append("  " + render_event_line(event, first_ts))
    else:
        lines.append("  none")
    lines.append("")

    lines.append("Relevant Timeline")
    for event in sorted((event for event in events if is_relevant_packet(event)), key=lambda item: float(item.get("ts") or 0.0)):
        lines.append("  " + render_event_line(event, first_ts))
    lines.append("")

    lines.append("Warnings")
    if warnings:
        for warning in warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("  none")

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize relevant BLE round events from the LaserOps webapp NDJSON log."
    )
    parser.add_argument("--log", type=Path, default=default_log_path())
    parser.add_argument("--marker", type=Path, default=default_marker_path())
    parser.add_argument("--write-marker", action="store_true")
    parser.add_argument("--since-marker", action="store_true")
    parser.add_argument("--since-ts", type=float, default=None)
    parser.add_argument("--all", action="store_true", help="Include non-relevant event types in the parsed window.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.write_marker:
        marker_ts = write_marker(args.marker)
        print(f"Wrote marker {args.marker}")
        print(f"Marker timestamp: {marker_ts:.6f} ({format_ts(marker_ts)})")
        return 0

    since_ts = args.since_ts
    if args.since_marker:
        marker_ts = load_marker(args.marker)
        if marker_ts is None:
            raise SystemExit(f"Marker not found or invalid: {args.marker}")
        since_ts = marker_ts

    events = parse_ndjson(args.log, since_ts)
    if not args.all:
        events = [
            event
            for event in events
            if event.get("type") in RELEVANT_EVENT_TYPES and is_relevant_packet(event)
        ]
    print(summarize(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
