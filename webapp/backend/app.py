from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from webapp.backend.ble_service import BleHub


class ScanRequest(BaseModel):
    timeout: float = Field(default=10.0, ge=1.0, le=60.0)
    by_name: bool = False
    name: str = "NerfV"
    expected_count: int = Field(default=0, ge=0, le=16)


class ConnectRequest(BaseModel):
    address: str
    timeout: float = Field(default=15.0, ge=1.0, le=60.0)


class StartupRequest(BaseModel):
    volume: int = Field(default=0, ge=0, le=31)


class VolumeRequest(BaseModel):
    volume: int = Field(ge=0, le=31)


class ConfigRequest(BaseModel):
    level: int = Field(default=1, ge=1, le=5)
    name_a: int = Field(default=0, ge=0, le=49)
    name_b: int = Field(default=0, ge=0, le=49)
    ammo_profile: int | None = Field(default=None, ge=0, le=255)
    damage_profile: int | None = Field(default=None, ge=0, le=255)
    health_profile: int | None = Field(default=None, ge=0, le=255)


class GameStartRequest(BaseModel):
    delay: float = Field(default=0.0, ge=0.0, le=0.30)
    startup_volume: int = Field(default=0, ge=0, le=31)
    force_startup: bool = True
    duration_seconds: int = Field(default=300, ge=30, le=3600)
    slot: int | None = Field(default=None, ge=1, le=10)
    team: int | None = Field(default=None, ge=0, le=2)


class MultiGameStartRequest(BaseModel):
    addresses: list[str] = Field(default_factory=list)
    delay: float = Field(default=0.0, ge=0.0, le=0.30)
    startup_volume: int = Field(default=1, ge=0, le=31)
    force_startup: bool = True
    duration_seconds: int = Field(default=300, ge=30, le=3600)
    auto_recycle_followup: bool = True


class GameEndRequest(BaseModel):
    reason: str = Field(default="manual_stop", min_length=1, max_length=64)


class TeamProfileRequest(BaseModel):
    slot: int = Field(ge=1, le=10)
    team: int = Field(ge=0, le=2)


class LocalNameRequest(BaseModel):
    local_name: str | None = Field(default=None, max_length=32)


class ReconnectRequest(BaseModel):
    timeout: float = Field(default=15.0, ge=3.0, le=60.0)


class AutoReconnectRequest(BaseModel):
    enabled: bool = True


def _as_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ble_hub = BleHub()
    yield
    await app.state.ble_hub.shutdown()


app = FastAPI(title="LaserOps Control", lifespan=lifespan)

frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
app.mount("/assets", StaticFiles(directory=frontend_dir), name="assets")


def _hub(request: Request) -> BleHub:
    return request.app.state.ble_hub


async def _run_host_command(
    command: tuple[str, ...], timeout: float = 8.0
) -> dict[str, object]:
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return {
            "command": list(command),
            "ok": False,
            "timed_out": True,
            "returncode": None,
            "stdout": "",
            "stderr": "command timed out",
        }
    return {
        "command": list(command),
        "ok": proc.returncode == 0,
        "timed_out": False,
        "returncode": proc.returncode,
        "stdout": stdout.decode(errors="replace").strip(),
        "stderr": stderr.decode(errors="replace").strip(),
    }


async def _enable_bluetooth_host() -> dict[str, object]:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("bluetooth enable is only supported on linux hosts")

    commands: tuple[tuple[str, ...], ...] = (
        ("rfkill", "unblock", "bluetooth"),
        ("bluetoothctl", "power", "on"),
        ("hciconfig", "hci0", "up"),
    )
    attempts: list[dict[str, object]] = []
    ran_any = False
    any_ok = False

    for cmd in commands:
        if shutil.which(cmd[0]) is None:
            attempts.append(
                {
                    "command": list(cmd),
                    "ok": False,
                    "skipped": "tool not installed",
                }
            )
            continue
        ran_any = True
        result = await _run_host_command(cmd)
        attempts.append(result)
        if result.get("ok"):
            any_ok = True

    if not ran_any:
        raise RuntimeError(
            "no supported bluetooth control utility found (rfkill/bluetoothctl/hciconfig)"
        )
    if not any_ok:
        last_error = ""
        for attempt in reversed(attempts):
            stderr = str(attempt.get("stderr") or "").strip()
            if stderr:
                last_error = stderr
                break
        detail = f" ({last_error})" if last_error else ""
        raise RuntimeError(f"failed to enable bluetooth{detail}")

    return {"status": "ok", "attempts": attempts}


def _parse_bluetoothctl_power(stdout: str) -> bool | None:
    match = re.search(
        r"^\s*Powered:\s*(yes|no)\s*$",
        stdout,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if match is None:
        return None
    return match.group(1).strip().lower() == "yes"


def _parse_rfkill_blocked(stdout: str) -> bool | None:
    soft = re.findall(r"Soft blocked:\s*(yes|no)", stdout, flags=re.IGNORECASE)
    hard = re.findall(r"Hard blocked:\s*(yes|no)", stdout, flags=re.IGNORECASE)
    values = [*soft, *hard]
    if not values:
        return None
    return any(str(value).strip().lower() == "yes" for value in values)


async def _bluetooth_status_host() -> dict[str, object]:
    # Bluetooth power control endpoints are Linux-specific in this app.
    if not sys.platform.startswith("linux"):
        return {"supported": False, "active": True}

    if shutil.which("bluetoothctl"):
        result = await _run_host_command(("bluetoothctl", "show"), timeout=4.0)
        if result.get("ok"):
            power = _parse_bluetoothctl_power(str(result.get("stdout") or ""))
            if power is not None:
                return {"supported": True, "active": power}

    if shutil.which("hciconfig"):
        result = await _run_host_command(("hciconfig", "hci0"), timeout=4.0)
        if result.get("ok"):
            text = str(result.get("stdout") or "")
            if re.search(r"\bUP\b", text, flags=re.IGNORECASE):
                return {"supported": True, "active": True}
            if re.search(r"\bDOWN\b", text, flags=re.IGNORECASE):
                return {"supported": True, "active": False}

    if shutil.which("rfkill"):
        result = await _run_host_command(("rfkill", "list", "bluetooth"), timeout=4.0)
        if result.get("ok"):
            blocked = _parse_rfkill_blocked(str(result.get("stdout") or ""))
            if blocked is not None:
                return {"supported": True, "active": not blocked}

    # Unknown Linux adapter state: keep the button visible so operator can trigger enable.
    return {"supported": True, "active": False}


@app.get("/api/health")
async def health() -> dict[str, object]:
    bluetooth_status = await _bluetooth_status_host()
    return {
        "status": "ok",
        "bluetooth_supported": bool(bluetooth_status["supported"]),
        "bluetooth_active": bool(bluetooth_status["active"]),
    }


@app.post("/api/server/restart")
async def restart_server() -> dict[str, object]:
    restart_argv = [sys.executable, *sys.argv]

    async def _restart_soon() -> None:
        await asyncio.sleep(0.75)
        try:
            os.execv(sys.executable, restart_argv)
        except Exception:
            os._exit(1)

    asyncio.create_task(_restart_soon())
    return {"status": "restarting", "pid": os.getpid()}


@app.get("/api/meta/routes")
async def meta_routes() -> dict[str, object]:
    routes: list[dict[str, object]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = sorted(list(getattr(route, "methods", []) or []))
        if not path:
            continue
        routes.append({"path": path, "methods": methods})
    return {"routes": routes}


@app.post("/api/scan")
async def scan(request: Request, body: ScanRequest) -> dict[str, object]:
    hub = _hub(request)
    try:
        devices = await hub.scan(
            timeout=body.timeout,
            by_name=body.by_name,
            name=body.name,
            expected_count=body.expected_count,
        )
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"devices": devices}


@app.post("/api/bluetooth/enable")
async def enable_bluetooth() -> dict[str, object]:
    try:
        return await _enable_bluetooth_host()
    except Exception as exc:
        raise _as_http_error(exc) from exc


@app.get("/api/connections")
async def list_connections(request: Request) -> dict[str, object]:
    hub = _hub(request)
    return {"connections": await hub.list_connections()}


@app.post("/api/connect")
async def connect(request: Request, body: ConnectRequest) -> dict[str, object]:
    hub = _hub(request)
    try:
        connection = await hub.connect(address=body.address, timeout=body.timeout)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"connection": connection}


@app.post("/api/disconnect/{address}")
async def disconnect(request: Request, address: str) -> dict[str, str]:
    hub = _hub(request)
    try:
        await hub.disconnect(address=address)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "disconnected"}


@app.post("/api/reconnect/{address}")
async def reconnect(
    request: Request, address: str, body: ReconnectRequest
) -> dict[str, object]:
    hub = _hub(request)
    try:
        connection = await hub.reconnect(address=address, timeout=body.timeout)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "reconnected", "connection": connection}


@app.post("/api/reconnect/auto/{address}")
async def set_auto_reconnect(
    request: Request, address: str, body: AutoReconnectRequest
) -> dict[str, object]:
    hub = _hub(request)
    try:
        connection = await hub.set_auto_reconnect(address=address, enabled=body.enabled)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok", "connection": connection}


@app.post("/api/startup/{address}")
async def startup(request: Request, address: str, body: StartupRequest) -> dict[str, object]:
    hub = _hub(request)
    try:
        snapshot = await hub.startup(address=address, volume=body.volume)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"snapshot": snapshot}


@app.post("/api/volume/{address}")
async def set_volume(request: Request, address: str, body: VolumeRequest) -> dict[str, str]:
    hub = _hub(request)
    try:
        await hub.set_volume(address=address, volume=body.volume)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok"}


@app.post("/api/config/{address}")
async def write_config(request: Request, address: str, body: ConfigRequest) -> dict[str, str]:
    hub = _hub(request)
    try:
        await hub.write_config(
            address=address,
            level=body.level,
            name_a=body.name_a,
            name_b=body.name_b,
            ammo_profile=body.ammo_profile,
            damage_profile=body.damage_profile,
            health_profile=body.health_profile,
        )
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok"}


@app.post("/api/team/{address}")
async def set_team_profile(
    request: Request, address: str, body: TeamProfileRequest
) -> dict[str, object]:
    hub = _hub(request)
    try:
        connection = await hub.set_team_profile(
            address=address,
            slot=body.slot,
            team=body.team,
        )
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok", "connection": connection}


@app.post("/api/local-name/{address}")
async def set_local_name(
    request: Request, address: str, body: LocalNameRequest
) -> dict[str, object]:
    hub = _hub(request)
    try:
        connection = await hub.set_local_name(
            address=address,
            local_name=body.local_name,
        )
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok", "connection": connection}


@app.post("/api/game/start/{address}")
async def start_game(
    request: Request, address: str, body: GameStartRequest
) -> dict[str, object]:
    hub = _hub(request)
    try:
        result = await hub.start_game(
            address=address,
            delay=body.delay,
            startup_volume=body.startup_volume,
            force_startup=body.force_startup,
            duration_seconds=body.duration_seconds,
            slot=body.slot,
            team=body.team,
        )
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok", "result": result}


@app.post("/api/game/start-multi")
async def start_game_multi(
    request: Request, body: MultiGameStartRequest
) -> dict[str, object]:
    hub = _hub(request)
    try:
        result = await hub.start_game_multi(
            addresses=body.addresses or None,
            startup_volume=body.startup_volume,
            delay=body.delay,
            force_startup=body.force_startup,
            duration_seconds=body.duration_seconds,
            auto_recycle_followup=body.auto_recycle_followup,
        )
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok", "result": result}


@app.post("/api/game/end")
async def end_game(request: Request, body: GameEndRequest) -> dict[str, object]:
    hub = _hub(request)
    try:
        result = await hub.end_game(reason=body.reason)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok", "result": result}


@app.post("/api/game/end/")
async def end_game_slash(request: Request, body: GameEndRequest) -> dict[str, object]:
    return await end_game(request, body)


@app.get("/api/game/ranking")
async def game_ranking(request: Request) -> dict[str, object]:
    hub = _hub(request)
    try:
        return await hub.game_ranking()
    except Exception as exc:
        raise _as_http_error(exc) from exc


@app.get("/api/game/ranking/")
async def game_ranking_slash(request: Request) -> dict[str, object]:
    return await game_ranking(request)


@app.post("/api/status/poll/{address}")
async def poll_status(request: Request, address: str) -> dict[str, str]:
    hub = _hub(request)
    try:
        payload = await hub.poll_status(address=address)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"payload": payload}


@app.post("/api/stats/{address}")
async def collect_stats(request: Request, address: str) -> dict[str, object]:
    hub = _hub(request)
    try:
        stats = await hub.collect_stats(address=address)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"stats": stats}


@app.post("/api/session/close/{address}")
async def close_session(request: Request, address: str) -> dict[str, str]:
    hub = _hub(request)
    try:
        await hub.close_session(address=address)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"status": "ok"}


@app.get("/api/notifications/{address}")
async def notifications(request: Request, address: str) -> dict[str, object]:
    hub = _hub(request)
    try:
        items = await hub.notifications(address=address)
    except Exception as exc:
        raise _as_http_error(exc) from exc
    return {"items": items}


@app.get("/api/live/stream")
async def live_stream(request: Request) -> StreamingResponse:
    hub = _hub(request)
    subscriber_id, queue = await hub.subscribe_events()
    snapshot = await hub.live_snapshot()

    async def _gen():
        yield f"event: snapshot\ndata: {json.dumps(snapshot, separators=(',', ':'))}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                payload = json.dumps(event, separators=(",", ":"))
                yield f"event: {event['type']}\ndata: {payload}\n\n"
        finally:
            await hub.unsubscribe_events(subscriber_id)

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(frontend_dir / "index.html")
