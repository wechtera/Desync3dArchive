"""Vault reconciliation routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import reconcile as handlers
from desync_archival.api.routing import add_route, plain

router = APIRouter()

add_route(router, "GET", "/api/reconcile/history", plain(handlers.handle_get_reconcile_history))
add_route(router, "GET", "/api/reconcile/status", plain(handlers.handle_get_reconcile_status))
add_route(router, "POST", "/api/reconcile/scan", plain(handlers.handle_post_reconcile_scan), schemas.ReconcileScanBody)
add_route(router, "POST", "/api/reconcile/cancel", plain(handlers.handle_post_reconcile_cancel))
add_route(router, "POST", "/api/reconcile/reset", plain(handlers.handle_post_reconcile_reset), schemas.ResetBody)
add_route(router, "POST", ("/api/reconcile/delete-orphans", "/api/reconcile/orphan-audit"),
          plain(handlers.handle_post_reconcile_delete_orphans), schemas.PathsBody)
add_route(router, "POST", "/api/reconcile/ingest-untracked", plain(handlers.handle_post_reconcile_ingest_untracked),
          schemas.UntrackedBody)
add_route(router, "GET", ("/api/reconcile/staging", "/api/reconcile/staging-garbage"),
          plain(handlers.handle_get_reconcile_staging_garbage))
add_route(router, "POST", ("/api/reconcile/staging/purge", "/api/reconcile/staging-purge", "/api/reconcile/staging-sweep"), plain(handlers.handle_post_reconcile_staging_purge), schemas.PathsBody)
add_route(router, "GET", "/api/reconcile/duplicates", plain(handlers.handle_get_reconcile_duplicates))
add_route(router, ("GET", "POST"), ("/api/reconcile/drift", "/api/reconcile/path-drift", "/api/reconcile/drift-scan"), plain(handlers.handle_get_reconcile_path_drift))
add_route(router, "POST", "/api/reconcile/catalog-previews", plain(handlers.handle_post_reconcile_catalog_previews),
          schemas.ModelIdsBody)
add_route(router, "POST", ("/api/reconcile/drift/realign", "/api/reconcile/path-realign"),
          plain(handlers.handle_post_reconcile_path_realign), schemas.ModelIdsBody)
