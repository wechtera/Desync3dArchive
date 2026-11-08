"""Download Issues routes."""

from fastapi import APIRouter

from desync_archival.api.routes import download_issues as handlers
from desync_archival.api.routing import add_route

router = APIRouter()

add_route(router, "GET", "/api/download-issues", handlers.handle_get_download_issues)
add_route(router, "POST", "/api/download-issues/{issue_id:int}/keep", handlers.handle_post_download_issue_keep)
# Not add_route: that reads the whole body as JSON, and this one streams a file to disk.
router.add_api_route(
    "/api/download-issues/{issue_id:int}/file", handlers.put_download_issue_file, methods=["PUT"], response_model=None,
)
