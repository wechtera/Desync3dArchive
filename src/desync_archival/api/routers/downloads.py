"""Download queue, history and worker control routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import downloads as handlers
from desync_archival.api.routing import add_route, plain

router = APIRouter()

add_route(router, "GET", ("/api/downloads/status", "/api/downloads/queue"), plain(handlers.handle_get_downloads_status))
add_route(router, "GET", "/api/downloads/history", plain(handlers.handle_get_downloads_history))
add_route(router, "POST", "/api/downloads/queue", plain(handlers.handle_post_downloads_queue), schemas.DownloadQueueBody)
add_route(router, "DELETE", "/api/downloads/queue", plain(handlers.handle_delete_downloads_queue),
          schemas.DownloadDequeueBody)
add_route(router, "POST", "/api/downloads/dequeue", plain(handlers.handle_delete_downloads_queue),
          schemas.DownloadDequeueBody)
add_route(router, "POST", "/api/downloads/control", plain(handlers.handle_post_downloads_control),
          schemas.DownloadControlBody)
