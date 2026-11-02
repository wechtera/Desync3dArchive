"""Archive routes: submodels, raw member trees and the peek backfill."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import archives as handlers
from desync_archival.api.routing import add_route

router = APIRouter()

add_route(router, "GET", "/api/archives/{file_id:int}/submodels",
          handlers.handle_get_archive_submodels)
add_route(router, "GET", "/api/models/archives", handlers.handle_get_archive_submodels)
add_route(router, "GET", "/api/archives/{file_id:int}/raw-tree",
          handlers.handle_get_archive_raw)
add_route(router, "GET", "/api/models/archives/raw", handlers.handle_get_archive_raw)
add_route(router, "POST", "/api/reconcile/archive-peek",
          handlers.handle_post_archive_peek_backfill, schemas.ModelIdsBody)
