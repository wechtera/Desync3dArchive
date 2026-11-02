"""Task, scrape and ingest routes."""

from fastapi import APIRouter

from desync_archival.api import schemas
from desync_archival.api.routes import tasks as handlers
from desync_archival.api.routing import add_route

router = APIRouter()

add_route(router, "GET", ("/api/tasks", "/api/tasks/status"), handlers.handle_get_tasks_status)
add_route(router, "GET", "/api/tasks/{task_id:int}", handlers.handle_get_task)
add_route(router, "GET", "/api/scrapes/list", handlers.handle_get_scrapes_list)
add_route(router, "GET", "/api/scrapes/download", handlers.handle_get_scrapes_download)
add_route(router, "POST", ("/api/scraper/start", "/api/tasks/scrape"), handlers.handle_post_scraper_start)
add_route(router, "POST", "/api/tasks/cancel", handlers.handle_post_task_cancel)
add_route(router, "POST", ("/api/ingest/start", "/api/tasks/ingest"), handlers.handle_post_ingest_start,
          schemas.IngestBody)
add_route(router, "POST", "/api/tasks/kill", handlers.handle_post_task_kill)
add_route(router, "POST", "/api/tasks/clear", handlers.handle_post_tasks_clear, schemas.ConfirmBody)
