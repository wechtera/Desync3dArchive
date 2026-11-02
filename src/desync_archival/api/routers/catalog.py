"""Catalog routes: metrics, models, creators, releases, audit and renders."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import catalog as handlers
from desync_archival.api.routing import add_route

router = APIRouter()


add_route(router, "GET", "/api/metrics", handlers.handle_get_metrics)
add_route(router, "GET", "/api/models", handlers.handle_get_models)
add_route(router, "POST", ("/api/models/{model_id:int}/render-view", "/api/models/{model_id:int}/render-view/"),
          handlers.handle_post_model_render_view, schemas.ModelViewBody)
add_route(router, ("GET", "POST"), ("/api/render/stl", "/api/render/stl/"), handlers.handle_post_render_stl, schemas.RenderStlBody)
add_route(router, "GET", "/api/models/detail", handlers.handle_get_model_detail)
add_route(router, "GET", "/api/models/item", handlers.handle_get_model_detail)
add_route(router, "GET", "/api/models/files", handlers.handle_get_model_files)
add_route(router, "GET", "/api/models/{id:int}", handlers.handle_get_model_detail)
add_route(router, "GET", "/api/models/{model_id:int}/files",
          handlers.handle_get_model_files)
add_route(router, "GET", "/api/creators", handlers.handle_get_creators)
add_route(router, "GET", "/api/releases", handlers.handle_get_releases)
add_route(router, "GET", "/api/audit", handlers.handle_get_audit)
