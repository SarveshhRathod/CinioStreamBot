from aiohttp import web
from FileStream.config import Telegram, Server
from FileStream.utils.database import db

api_routes = web.RouteTableDef()


def _check_auth(request: web.Request):
    if not Telegram.API_SECRET:
        raise web.HTTPForbidden(text="API_SECRET is not configured on the bot.")
    if request.headers.get("X-API-Key") != Telegram.API_SECRET:
        raise web.HTTPUnauthorized(text="Invalid or missing X-API-Key header.")


@api_routes.get("/api/unlinked-files")
async def unlinked_files(request: web.Request):
    """Files uploaded to the bot via Telegram that no movie/episode uses yet."""
    _check_auth(request)
    files = await db.get_unlinked_files()
    for f in files:
        f["stream_link"] = f'{Server.URL}dl/{f["id"]}'
        f["watch_link"] = f'{Server.URL}watch/{f["id"]}'
    return web.json_response({"count": len(files), "files": files})


@api_routes.post("/api/link-file")
async def link_file(request: web.Request):
    """Called by Laravel after a file is attached to a Content/Episode source,
    so it stops showing up in the 'unlinked' list."""
    _check_auth(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    file_id = data.get("file_id")
    if not file_id:
        return web.json_response({"error": "file_id is required"}, status=400)

    ok = await db.mark_file_linked(file_id)
    if not ok:
        return web.json_response({"error": "file_id not found"}, status=404)
    return web.json_response({"success": True, "file_id": file_id})


@api_routes.post("/api/unlink-file")
async def unlink_file(request: web.Request):
    """Called by Laravel after a Content/Episode source built from a bot
    file is deleted, so that file becomes available again in the picker."""
    _check_auth(request)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    file_id = data.get("file_id")
    if not file_id:
        return web.json_response({"error": "file_id is required"}, status=400)

    ok = await db.mark_file_unlinked(file_id)
    if not ok:
        return web.json_response({"error": "file_id not found"}, status=404)
    return web.json_response({"success": True, "file_id": file_id})
