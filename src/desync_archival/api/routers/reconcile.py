"""Vault reconciliation routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import reconcile as handlers
from desync_archival.api.routing import add_route

router = APIRouter()

add_route(router, "GET", "/api/reconcile/duplicates/latest", handlers.handle_get_reconcile_duplicates_latest)
add_route(router, "POST", "/api/reconcile/duplicates/decision", handlers.handle_post_reconcile_duplicate_decision,
          schemas.DuplicateDecisionBody)
add_route(router, "POST", "/api/reconcile/duplicates/delete", handlers.handle_post_reconcile_duplicate_delete,
          schemas.DuplicateDeleteBody)
add_route(router, "GET", "/api/pending-deletion", handlers.handle_get_pending_deletion)
add_route(router, "GET", "/api/pending-deletion/items", handlers.handle_get_pending_deletion_items)
add_route(router, "POST", "/api/pending-deletion/restore", handlers.handle_post_pending_deletion_restore,
          schemas.PendingRestoreBody)
add_route(router, "POST", "/api/pending-deletion/purge", handlers.handle_post_pending_deletion_purge,
          schemas.PendingPurgeBody)
add_route(router, "GET", "/api/reconcile/history", handlers.handle_get_reconcile_history)
add_route(router, "GET", "/api/reconcile/status", handlers.handle_get_reconcile_status)
add_route(router, "POST", "/api/reconcile/scan", handlers.handle_post_reconcile_scan, schemas.ReconcileScanBody)
add_route(router, "POST", "/api/reconcile/cancel", handlers.handle_post_reconcile_cancel)
add_route(router, "POST", "/api/reconcile/reset", handlers.handle_post_reconcile_reset, schemas.ResetBody)
add_route(router, "POST", ("/api/reconcile/delete-orphans", "/api/reconcile/orphan-audit"),
          handlers.handle_post_reconcile_delete_orphans, schemas.PathsBody)
add_route(router, "POST", "/api/reconcile/ingest-untracked", handlers.handle_post_reconcile_ingest_untracked,
          schemas.UntrackedBody)
add_route(router, "GET", ("/api/reconcile/staging", "/api/reconcile/staging-garbage"),
          handlers.handle_get_reconcile_staging_garbage)
add_route(router, "POST", ("/api/reconcile/staging", "/api/reconcile/staging-garbage"),
          handlers.handle_post_reconcile_staging_check)
add_route(router, "POST", ("/api/reconcile/staging/purge", "/api/reconcile/staging-purge", "/api/reconcile/staging-sweep"), handlers.handle_post_reconcile_staging_purge, schemas.PathsBody)
add_route(router, "GET", "/api/reconcile/duplicates", handlers.handle_get_reconcile_duplicates)
add_route(router, "POST", "/api/reconcile/duplicates", handlers.handle_post_reconcile_duplicates)
add_route(router, "GET", ("/api/reconcile/drift", "/api/reconcile/path-drift", "/api/reconcile/drift-scan"),
          handlers.handle_get_reconcile_path_drift)
add_route(router, "POST", ("/api/reconcile/drift", "/api/reconcile/path-drift", "/api/reconcile/drift-scan"),
          handlers.handle_post_reconcile_path_drift)
add_route(router, "POST", "/api/reconcile/catalog-previews", handlers.handle_post_reconcile_catalog_previews,
          schemas.ModelIdsBody)
add_route(router, "POST", ("/api/reconcile/drift/realign", "/api/reconcile/path-realign"),
          handlers.handle_post_reconcile_path_realign, schemas.ModelIdsBody)
