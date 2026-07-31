from __future__ import annotations
import hmac
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from apple_calendar_server import store
from apple_calendar_server.config import Config

_config: Config | None = None


def init(config: Config) -> None:
    global _config
    _config = config


def _authorized(request: Request) -> bool:
    if not _config or not _config.bearer_tokens:
        return False
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        return False
    token = header[len("Bearer "):]
    return any(hmac.compare_digest(token, t) for t in _config.bearer_tokens)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


async def _json_body(request: Request) -> tuple[dict, JSONResponse | None]:
    try:
        return (await request.json()), None
    except Exception:
        return {}, _err(400, "invalid_json", "body must be valid JSON")


async def get_events(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _err(401, "unauthorized", "missing or invalid bearer token")
    start = request.query_params.get("start") or None
    end = request.query_params.get("end") or None
    calendar_name = request.query_params.get("calendar") or None
    since = request.query_params.get("since") or None
    server_time = store.now_utc()
    try:
        events = store.list_events(start=start, end=end, calendar_name=calendar_name, since=since, include_deleted=True)
    except ValueError as e:
        return _err(400, "validation_error", str(e))
    return JSONResponse({"server_time": server_time, "events": events})


async def get_event(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _err(401, "unauthorized", "missing or invalid bearer token")
    id = request.path_params["id"]
    event = store.get(id)
    if event is None:
        return _err(404, "not_found", f"no event with id {id!r}")
    return JSONResponse(event)


async def put_event(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _err(401, "unauthorized", "missing or invalid bearer token")
    body, error = await _json_body(request)
    if error:
        return error
    body["id"] = request.path_params["id"]
    try:
        stored = store.upsert(body)
    except store.Stale as e:
        return JSONResponse({"current": e.current}, status_code=409)
    except (ValueError, KeyError) as e:
        return _err(400, "validation_error", str(e))
    return JSONResponse(stored)


async def delete_event(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _err(401, "unauthorized", "missing or invalid bearer token")
    body = {}
    if await request.body():
        body, error = await _json_body(request)
        if error:
            return error

    id = request.path_params["id"]
    try:
        tombstone = store.soft_delete(id, updated_at=body.get("updated_at"))
    except KeyError:
        return _err(404, "not_found", f"no event with id {id!r}")
    except store.Stale as e:
        return JSONResponse({"current": e.current}, status_code=409)
    return JSONResponse(tombstone)


async def get_calendars(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _err(401, "unauthorized", "missing or invalid bearer token")
    return JSONResponse({"calendars": store.list_calendars()})


async def gc_tombstones(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _err(401, "unauthorized", "missing or invalid bearer token")
    body, _ = await _json_body(request) if await request.body() else ({}, None)
    older_than_days = int(body.get("older_than_days", 30))
    removed = store.gc_tombstones(older_than_days=older_than_days)
    return JSONResponse({"removed": removed})


routes: list[Route] = [
    Route("/events", get_events, methods=["GET"]),
    Route("/events/{id}", get_event, methods=["GET"]),
    Route("/events/{id}", put_event, methods=["PUT"]),
    Route("/events/{id}", delete_event, methods=["DELETE"]),
    Route("/calendars", get_calendars, methods=["GET"]),
    Route("/admin/gc_tombstones", gc_tombstones, methods=["POST"]),
]


def create_app() -> Starlette:
    return Starlette(routes=routes)
