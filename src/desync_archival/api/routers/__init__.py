"""One APIRouter per domain. The order here is the order routes are matched."""

from desync_archival.api.routers import (
    archives,
    auth,
    catalog,
    downloads,
    reconcile,
    settings,
    stashes,
    tasks,
)

ALL = [catalog.router, archives.router, downloads.router, stashes.router, settings.router, tasks.router, reconcile.router, auth.router]
