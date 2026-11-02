"""Settings, storage, external library and logging routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import settings as handlers
from desync_archival.api.routes import storage as storage_handlers
from desync_archival.api.routing import add_route

router = APIRouter()


add_route(router, "POST", ("/api/settings/libraries/{library_id:int}/scan", "/api/settings/libraries/{library_id:int}/scan/"),
          handlers.handle_post_external_library_scan, schemas.LibraryScanBody)
add_route(router, "DELETE", ("/api/settings/libraries/{library_id:int}", "/api/settings/libraries/{library_id:int}/"),
          handlers.handle_delete_external_library)
add_route(router, "GET", ("/api/settings/libraries", "/api/settings/libraries/"),
          handlers.handle_get_external_libraries)
add_route(router, "POST", ("/api/settings/libraries", "/api/settings/libraries/"),
          handlers.handle_post_external_library, schemas.LibraryBody)
add_route(router, "GET", "/api/settings/libraries/{library_id:int}/preview",
          handlers.handle_get_library_preview)
add_route(router, "POST", "/api/settings/libraries/{library_id:int}/preview/approve",
          handlers.handle_post_library_approve, schemas.LibraryApproveBody)
add_route(router, "GET", "/api/settings/libraries/{library_id:int}/preview/release",
          handlers.handle_get_library_release)
add_route(router, "POST", "/api/settings/libraries/{library_id:int}/preview/review",
          handlers.handle_post_library_review, schemas.LibraryReviewBody)
add_route(router, "GET", "/api/catalog-models", handlers.handle_get_models)
add_route(router, "GET", "/api/catalog-models/{model_id:int}", handlers.handle_get_catalog_model)
add_route(router, "POST", "/api/catalog-models/{model_id:int}/stash",
          handlers.handle_post_catalog_model_stash, schemas.ModelStashBody)
add_route(router, "GET", "/api/model-groups/{group_id:int}", handlers.handle_get_model_group)
add_route(router, "GET", "/api/tags", handlers.handle_get_tags)
add_route(router, "POST", "/api/tags", handlers.handle_post_tags, schemas.TagBody)
add_route(router, "POST", "/api/catalog-models/{model_id:int}/tags",
          handlers.handle_post_model_tags, schemas.TagBody)
add_route(router, "POST", "/api/model-groups/{group_id:int}/tags",
          handlers.handle_post_group_tags, schemas.TagBody)
add_route(router, "GET", "/api/settings/libraries/{library_id:int}/catalog-review",
          handlers.handle_get_catalog_review)
add_route(router, "POST", "/api/settings/libraries/{library_id:int}/catalog-review",
          handlers.handle_post_catalog_review, schemas.LibraryReviewBody)
add_route(router, "POST", "/api/import-file", handlers.handle_post_import_file, schemas.ImportFileBody)
add_route(router, "GET", "/api/audit-history", handlers.handle_get_audit_history)
add_route(router, "GET", "/api/backups", handlers.handle_get_backups)
add_route(router, "POST", "/api/backups", handlers.handle_post_backups, schemas.BackupBody)
add_route(router, "GET", "/api/archive-review", handlers.handle_get_archive_queue)
add_route(router, "GET", "/api/archive-review/{collection_id:int}",
          handlers.handle_get_archive_review)
add_route(router, "POST", "/api/archive-review/{collection_id:int}",
          handlers.handle_post_archive_review, schemas.ArchiveReviewBody)
add_route(router, "GET", "/api/library-images", handlers.handle_get_image_queue)
add_route(router, "POST", "/api/library-images", handlers.handle_post_image_queue, schemas.ImageQueueBody)
add_route(router, "GET", "/api/inactive", handlers.handle_get_inactive)
add_route(router, "GET", "/api/settings", handlers.handle_get_settings)
add_route(router, "POST", "/api/settings", handlers.handle_post_settings, schemas.SettingsBody)
add_route(router, "GET", "/api/settings/directories", handlers.handle_get_directory_usage)
add_route(router, "GET", "/api/settings/locations", handlers.handle_get_locations)
add_route(router, "GET", "/api/health", handlers.handle_get_health)
add_route(router, "GET", "/api/documents", handlers.handle_get_documents)
add_route(router, "POST", "/api/documents/stash", handlers.handle_post_document_stash, schemas.DocumentStashBody)
add_route(router, "POST", "/api/documents/update", handlers.handle_post_document_update)
add_route(router, "POST", "/api/schedules", handlers.handle_post_schedules, schemas.SchedulesBody)
add_route(router, "GET", "/api/system/browse", handlers.handle_get_system_browse)
add_route(router, "GET", "/api/storage/status", storage_handlers.handle_get_storage_status)
add_route(router, "POST", "/api/storage/config", storage_handlers.handle_post_storage_config,
          schemas.StorageConfigBody)
add_route(router, "GET", "/api/settings/logging", handlers.handle_get_logging_config)
add_route(router, "POST", "/api/settings/logging", handlers.handle_post_logging_config, schemas.LoggingBody)
add_route(router, "POST", "/api/settings/logs/purge", handlers.handle_post_logs_purge, schemas.LogsPurgeBody)
add_route(router, "POST", ("/api/settings/stashes/purge", "/api/stashes/purge"),
          handlers.handle_post_stashes_purge, schemas.StashPurgeBody)
