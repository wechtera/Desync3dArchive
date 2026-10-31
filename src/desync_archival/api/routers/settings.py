"""Settings, storage, external library and logging routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import settings as handlers
from desync_archival.api.routes import storage as storage_handlers
from desync_archival.api.routing import RouteContext, add_route, plain, with_path_id

router = APIRouter()


def scan_library(ctx: RouteContext):
    return handlers.handle_post_external_library_scan(
        ctx.conn, ctx.path["library_id"], ctx.download_manager, ctx.body, ctx.params, task_manager=ctx.task_manager
    )


def delete_library(ctx: RouteContext):
    return handlers.handle_delete_external_library(
        ctx.conn, ctx.path["library_id"], ctx.download_manager, ctx.body, ctx.params
    )


add_route(router, "POST", ("/api/settings/libraries/{library_id:int}/scan", "/api/settings/libraries/{library_id:int}/scan/"),
          scan_library, schemas.LibraryScanBody)
add_route(router, "DELETE", ("/api/settings/libraries/{library_id:int}", "/api/settings/libraries/{library_id:int}/"),
          delete_library)
add_route(router, "GET", ("/api/settings/libraries", "/api/settings/libraries/"),
          plain(handlers.handle_get_external_libraries))
add_route(router, "POST", ("/api/settings/libraries", "/api/settings/libraries/"),
          plain(handlers.handle_post_external_library), schemas.LibraryBody)
add_route(router, "GET", "/api/settings/libraries/{library_id:int}/preview",
          with_path_id(handlers.handle_get_library_preview, "library_id"))
add_route(router, "POST", "/api/settings/libraries/{library_id:int}/preview/approve",
          with_path_id(handlers.handle_post_library_approve, "library_id"), schemas.LibraryApproveBody)
add_route(router, "GET", "/api/settings/libraries/{library_id:int}/preview/release",
          with_path_id(handlers.handle_get_library_release, "library_id"))
add_route(router, "POST", "/api/settings/libraries/{library_id:int}/preview/review",
          with_path_id(handlers.handle_post_library_review, "library_id"), schemas.LibraryReviewBody)
add_route(router, "GET", "/api/catalog-models", plain(handlers.handle_get_catalog_models))
add_route(router, "GET", "/api/catalog-models/{model_id:int}", with_path_id(handlers.handle_get_catalog_model, "model_id"))
add_route(router, "GET", "/api/model-groups/{group_id:int}", with_path_id(handlers.handle_get_model_group, "group_id"))
add_route(router, "GET", "/api/archive-review", plain(handlers.handle_get_archive_queue))
add_route(router, "GET", "/api/archive-review/{collection_id:int}",
          with_path_id(handlers.handle_get_archive_review, "collection_id"))
add_route(router, "POST", "/api/archive-review/{collection_id:int}",
          with_path_id(handlers.handle_post_archive_review, "collection_id"), schemas.ArchiveReviewBody)
add_route(router, "GET", "/api/library-images", plain(handlers.handle_get_image_queue))
add_route(router, "POST", "/api/library-images", plain(handlers.handle_post_image_queue), schemas.ImageQueueBody)
add_route(router, "GET", "/api/inactive", plain(handlers.handle_get_inactive))
add_route(router, "GET", "/api/settings", plain(handlers.handle_get_settings))
add_route(router, "POST", "/api/settings", plain(handlers.handle_post_settings), schemas.SettingsBody)
add_route(router, "GET", "/api/settings/directories", plain(handlers.handle_get_directory_usage))
add_route(router, "GET", "/api/system/browse", plain(handlers.handle_get_system_browse))
add_route(router, "GET", "/api/storage/status", plain(storage_handlers.handle_get_storage_status))
add_route(router, "POST", "/api/storage/config", plain(storage_handlers.handle_post_storage_config),
          schemas.StorageConfigBody)
add_route(router, "GET", "/api/settings/logging", plain(handlers.handle_get_logging_config))
add_route(router, "POST", "/api/settings/logging", plain(handlers.handle_post_logging_config), schemas.LoggingBody)
add_route(router, "POST", "/api/settings/logs/purge", plain(handlers.handle_post_logs_purge), schemas.LogsPurgeBody)
add_route(router, "POST", ("/api/settings/stashes/purge", "/api/stashes/purge"),
          plain(handlers.handle_post_stashes_purge), schemas.StashPurgeBody)
