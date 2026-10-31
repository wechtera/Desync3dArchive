"""Catalog routes: metrics, models, creators, releases, audit and renders."""

from fastapi import APIRouter

from desync_archival.api import routes, schemas
from desync_archival.api.routes.catalog import handle_get_audit
from desync_archival.api.routing import RouteContext, add_route, path_to_param, plain

router = APIRouter()


def render_view(ctx: RouteContext):
    return routes.handle_post_model_render_view(
        ctx.conn, ctx.path["model_id"], ctx.download_manager, ctx.body, ctx.params, task_manager=ctx.task_manager
    )


def render_stl(ctx: RouteContext):
    return routes.handle_post_render_stl(
        ctx.conn, ctx.download_manager, ctx.body, ctx.params, task_manager=ctx.task_manager
    )


add_route(router, "GET", "/api/metrics", plain(routes.handle_get_metrics))
add_route(router, "GET", "/api/models", plain(routes.handle_get_models))
add_route(router, "POST", ("/api/models/{model_id:int}/render-view", "/api/models/{model_id:int}/render-view/"),
          render_view, schemas.ModelViewBody)
add_route(router, ("GET", "POST"), ("/api/render/stl", "/api/render/stl/"), render_stl, schemas.RenderStlBody)
add_route(router, "GET", "/api/models/detail", plain(routes.handle_get_model_detail))
add_route(router, "GET", "/api/models/item", plain(routes.handle_get_model_detail))
add_route(router, "GET", "/api/models/files", plain(routes.handle_get_model_files))
add_route(router, "GET", "/api/models/{model_id:int}", path_to_param(routes.handle_get_model_detail, "model_id", "id"))
add_route(router, "GET", "/api/models/{model_id:int}/files",
          path_to_param(routes.handle_get_model_files, "model_id", "model_id"))
add_route(router, "GET", "/api/creators", plain(routes.handle_get_creators))
add_route(router, "GET", "/api/releases", plain(routes.handle_get_releases))
add_route(router, "GET", "/api/audit", plain(handle_get_audit))
