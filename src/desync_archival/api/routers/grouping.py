"""LLM Grouping routes (E54)."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import grouping as handlers
from desync_archival.api.routing import add_route

router = APIRouter()

add_route(router, "GET", "/api/grouping", handlers.handle_get_grouping)
add_route(router, "GET", "/api/grouping/preview", handlers.handle_get_grouping_preview)
add_route(router, "POST", "/api/grouping/exports", handlers.handle_post_grouping_export, schemas.GroupingExportBody)
add_route(router, "POST", "/api/grouping/check", handlers.handle_post_grouping_check, schemas.GroupingCheckBody)
add_route(router, "POST", "/api/grouping/imports/{import_id:int}/apply", handlers.handle_post_grouping_apply, schemas.GroupingApplyBody)
add_route(router, "POST", "/api/grouping/imports/{import_id:int}/review", handlers.handle_post_grouping_review, schemas.GroupingReviewBody)
add_route(router, "POST", "/api/grouping/imports/{import_id:int}/discard", handlers.handle_post_grouping_discard)
add_route(router, "POST", "/api/grouping/imports/{import_id:int}/undo", handlers.handle_post_grouping_undo)
add_route(router, "POST", "/api/grouping/imports/{import_id:int}/tags", handlers.handle_post_grouping_tags, schemas.GroupingTagsBody)
add_route(router, "GET", "/api/grouping/suggestions/{suggestion_id:int}", handlers.handle_get_grouping_suggestion)
add_route(router, "POST", "/api/grouping/suggestions/{suggestion_id:int}/accept",
          handlers.handle_post_grouping_accept, schemas.GroupingAcceptBody)
add_route(router, "POST", "/api/grouping/suggestions/{suggestion_id:int}/reject", handlers.handle_post_grouping_reject)
