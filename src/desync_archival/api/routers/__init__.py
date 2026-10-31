"""One APIRouter per domain. The order here is the order routes are matched."""

from desync_archival.api.routers import catalog

ALL = [catalog.router]
