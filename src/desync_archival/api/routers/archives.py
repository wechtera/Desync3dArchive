"""Archive routes: submodels, raw member trees and the peek backfill."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import archives as handlers
from desync_archival.api.routing import add_route, path_to_param, with_tasks

router = APIRouter()

add_route(router, "GET", "/api/archives/{file_id:int}/submodels",
          path_to_param(handlers.handle_get_archive_submodels, "file_id", "file_id", tasks=True))
add_route(router, "GET", "/api/models/archives", with_tasks(handlers.handle_get_archive_submodels))
add_route(router, "GET", "/api/archives/{file_id:int}/raw-tree",
          path_to_param(handlers.handle_get_archive_raw, "file_id", "file_id", tasks=True))
add_route(router, "GET", "/api/models/archives/raw", with_tasks(handlers.handle_get_archive_raw))
add_route(router, "POST", "/api/reconcile/archive-peek",
          with_tasks(handlers.handle_post_archive_peek_backfill), schemas.ModelIdsBody)
