"""MMF sign-in routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import auth as handlers
from desync_archival.api.routing import add_route

router = APIRouter()

add_route(router, "GET", "/api/auth/status", handlers.handle_get_auth_status)
add_route(router, "POST", "/api/auth/config", handlers.handle_post_auth_config, schemas.AuthConfigBody)
add_route(router, "POST", ("/api/auth/login", "/api/auth/login-url"), handlers.handle_post_auth_login)
add_route(router, "GET", "/api/auth/callback", handlers.handle_get_auth_callback)
add_route(router, "POST", "/api/auth/token_callback", handlers.handle_post_auth_token_callback,
          schemas.TokenCallbackBody)
add_route(router, "POST", "/api/auth/disconnect", handlers.handle_post_auth_disconnect)
