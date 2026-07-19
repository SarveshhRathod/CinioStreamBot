from aiohttp import web
from .stream_routes import routes
from .api_routes import api_routes

def web_server():
    web_app = web.Application(client_max_size=30000000)
    web_app.add_routes(routes)
    web_app.add_routes(api_routes)
    return web_app
