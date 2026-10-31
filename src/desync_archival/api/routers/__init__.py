"""One APIRouter per domain. The order here is the order routes are matched."""

from desync_archival.api.routers import catalog, archives, downloads, stashes

ALL = [catalog.router, archives.router, downloads.router, stashes.router]
