"""Stash routes. Every path also works with a trailing slash."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import stashes as handlers
from desync_archival.api.routing import add_route

router = APIRouter()


def both(path: str) -> tuple[str, str]:
    return path, f"{path}/"


add_route(router, "GET", both("/api/stashes"), handlers.handle_get_stashes)
add_route(router, "POST", both("/api/stashes"), handlers.handle_create_stash, schemas.StashCreateBody)
add_route(router, "GET", both("/api/stashes/active"), handlers.handle_get_active_stash)
add_route(router, "DELETE", both("/api/stashes/items/{stash_item_id:int}"),
          handlers.handle_remove_stash_item)
add_route(router, "POST", both("/api/stashes/{stash_id:int}/active"),
          handlers.handle_set_active_stash)
add_route(router, "POST", both("/api/stashes/{stash_id:int}/items"),
          handlers.handle_add_stash_item, schemas.StashItemBody)
add_route(router, "POST", both("/api/stashes/{stash_id:int}/bulk-models"),
          handlers.handle_bulk_add_models, schemas.ModelIdsBody)
add_route(router, "POST", both("/api/stashes/{stash_id:int}/export"),
          handlers.handle_post_export_stash, schemas.StashExportBody)
add_route(router, "GET", both("/api/stashes/{stash_id:int}"),
          handlers.handle_get_stash_detail)
add_route(router, "DELETE", both("/api/stashes/{stash_id:int}"),
          handlers.handle_delete_stash)
