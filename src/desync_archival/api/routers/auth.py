"""MMF sign-in routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import auth as handlers
from desync_archival.api.routing import add_route, plain

router = APIRouter()

add_route(router, "GET", "/api/auth/status", plain(handlers.handle_get_auth_status))
add_route(router, "POST", "/api/auth/config", plain(handlers.handle_post_auth_config), schemas.AuthConfigBody)
add_route(router, "POST", ("/api/auth/login", "/api/auth/login-url"), plain(handlers.handle_post_auth_login))
add_route(router, "GET", "/api/auth/callback", plain(handlers.handle_get_auth_callback))
add_route(router, "POST", "/api/auth/token_callback", plain(handlers.handle_post_auth_token_callback),
          schemas.TokenCallbackBody)
add_route(router, "POST", "/api/auth/disconnect", plain(handlers.handle_post_auth_disconnect))
