"""Stash routes. Every path also works with a trailing slash."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import stashes as handlers
from desync_archival.api.routing import add_route, plain, with_path_id

router = APIRouter()


def both(path: str) -> tuple[str, str]:
    return path, f"{path}/"


add_route(router, "GET", both("/api/stashes"), plain(handlers.handle_get_stashes))
add_route(router, "POST", both("/api/stashes"), plain(handlers.handle_create_stash), schemas.StashCreateBody)
add_route(router, "GET", both("/api/stashes/active"), plain(handlers.handle_get_active_stash))
add_route(router, "DELETE", both("/api/stashes/items/{item_id:int}"),
          with_path_id(handlers.handle_remove_stash_item, "item_id"))
add_route(router, "POST", both("/api/stashes/{stash_id:int}/active"),
          with_path_id(handlers.handle_set_active_stash, "stash_id"))
add_route(router, "POST", both("/api/stashes/{stash_id:int}/items"),
          with_path_id(handlers.handle_add_stash_item, "stash_id"), schemas.StashItemBody)
add_route(router, "POST", both("/api/stashes/{stash_id:int}/bulk-models"),
          with_path_id(handlers.handle_bulk_add_models, "stash_id"), schemas.ModelIdsBody)
add_route(router, "POST", both("/api/stashes/{stash_id:int}/export"),
          with_path_id(handlers.handle_post_export_stash, "stash_id", tasks=True), schemas.StashExportBody)
add_route(router, "GET", both("/api/stashes/{stash_id:int}"),
          with_path_id(handlers.handle_get_stash_detail, "stash_id"))
add_route(router, "DELETE", both("/api/stashes/{stash_id:int}"),
          with_path_id(handlers.handle_delete_stash, "stash_id"))
