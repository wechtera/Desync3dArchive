"""One APIRouter per domain. The order here is the order routes are matched."""

from desync_archival.api.routers import (
    archives,
    auth,
    catalog,
    downloads,
    reconcile,
    session,
    settings,
    stashes,
    tasks,
)

ALL = [session.router, catalog.router, archives.router, downloads.router, stashes.router, settings.router, tasks.router, reconcile.router, auth.router]
