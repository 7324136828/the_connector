"""ZIP installation, lifecycle management, and plugin UI hosting."""
import json
import mimetypes
import re
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from starlette.concurrency import run_in_threadpool

from ..services.plugin_manager import MAX_ZIP_BYTES, PluginError, PluginConflict, PluginNotFound

router = APIRouter(prefix="/api/plugins", tags=["plugins"])
CORS = {"Access-Control-Allow-Origin": "*"}


class PluginFrameCORSMiddleware:
    """Allow opaque sandbox origins only on plugin assets and their proxy."""
    def __init__(self, app):
        self.app = app
        self.frame_cors = CORSMiddleware(app, allow_origins=["null"],
                                        allow_methods=["GET", "POST"], allow_headers=["Content-Type"])

    async def __call__(self, scope, receive, send):
        origin = next((value for key, value in scope.get("headers", []) if key == b"origin"), None)
        is_plugin_resource = re.match(r"^/api/plugins/[a-z][a-z0-9_]{0,63}/(?:assets|proxy)/", scope.get("path", ""))
        if scope["type"] == "http" and origin == b"null" and is_plugin_resource:
            await self.frame_cors(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def manager():
    from ..main import plugin_manager
    return plugin_manager


def translate(exc):
    return HTTPException(404 if isinstance(exc, PluginNotFound) else 409 if isinstance(exc, PluginConflict) else 400, str(exc))


@router.get("")
def list_plugins():
    return manager().list()


@router.post("/install", status_code=201)
async def install_plugin(request: Request):
    payload = bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload) > MAX_ZIP_BYTES:
            raise HTTPException(413, "Plugin ZIP exceeds 10 MiB")
    try:
        return await run_in_threadpool(manager().install, bytes(payload))
    except PluginError as exc:
        raise translate(exc) from exc


@router.post("/{plugin_id}/enable")
def enable_plugin(plugin_id: str):
    try:
        return manager().enable(plugin_id)
    except PluginError as exc:
        raise translate(exc) from exc


@router.post("/{plugin_id}/disable")
def disable_plugin(plugin_id: str):
    try:
        return manager().disable(plugin_id)
    except PluginError as exc:
        raise translate(exc) from exc


@router.delete("/{plugin_id}")
def uninstall_plugin(plugin_id: str):
    try:
        manager().uninstall(plugin_id)
        return {"status": "uninstalled", "id": plugin_id}
    except PluginError as exc:
        raise translate(exc) from exc


@router.get("/{plugin_id}/assets/{asset_path:path}")
def plugin_asset(plugin_id: str, asset_path: str):
    try:
        path = manager().asset(plugin_id, asset_path)
        mime = "text/javascript" if path.suffix == ".js" else mimetypes.guess_type(path.name)[0]
        return FileResponse(path, media_type=mime, headers={**CORS, "Cache-Control": "no-store"})
    except PluginError as exc:
        raise translate(exc) from exc


@router.get("/{plugin_id}/frame", response_class=HTMLResponse)
def plugin_frame(plugin_id: str, session_id: str | None = Query(None)):
    try:
        record = manager().get(plugin_id)
        manager().backend_url(plugin_id)
        entry = record["manifest"]["ui"]["entry"]
        options = json.dumps({"apiBaseUrl": f"/api/plugins/{plugin_id}/proxy", "sessionId": session_id}).replace("<", "\\u003c")
        module_url = json.dumps(f"/api/plugins/{plugin_id}/assets/{quote(entry, safe='/')}").replace("<", "\\u003c")
        return HTMLResponse(f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{margin:0;padding:12px;background:#121217;}}</style></head><body><main id="plugin"></main><p id="error" role="alert"></p>
<script type="module">
try {{
  const {{ mount }} = await import({module_url});
  const instance = mount(document.getElementById('plugin'), {options});
  window.addEventListener('pagehide', () => instance.unmount());
}} catch (error) {{ document.getElementById('error').textContent = error.message; }}
</script></body></html>''', headers=CORS)
    except PluginError as exc:
        raise translate(exc) from exc


@router.options("/{plugin_id}/proxy/{path:path}")
def plugin_options(plugin_id: str, path: str):
    return Response(headers={**CORS, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})


@router.get("/{plugin_id}/proxy/{path:path}")
@router.post("/{plugin_id}/proxy/{path:path}")
async def plugin_proxy(plugin_id: str, path: str, request: Request):
    try:
        # Only the declared webhook and health endpoint can be reached.
        record = manager().get(plugin_id)
        if "/" + path not in (record["manifest"]["webhook"]["path"], "/health"):
            raise PluginNotFound("Undeclared plugin endpoint")
        url = manager().backend_url(plugin_id) + "/" + path
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_ZIP_BYTES:
                raise HTTPException(413, "Plugin request exceeds 10 MiB")
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            response = await client.request(request.method, url, params=list(request.query_params.multi_items()),
                                            content=bytes(body), headers={"Content-Type": request.headers.get("Content-Type", "application/json")})
        return Response(response.content, status_code=response.status_code,
                        headers={**CORS, "Cache-Control": "no-store"},
                        media_type=response.headers.get("content-type", "application/json"))
    except PluginError as exc:
        raise translate(exc) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "Plugin backend is unavailable") from exc
