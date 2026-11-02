"""Pydantic request bodies. Each one lists the fields its handler reads."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HtmlPage(str):
    """A handler's marker for a real HTML page. Any other string is sent as text."""


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ModelViewBody(RequestBody):
    view: str | None = None
    force: bool | None = None


class RenderStlBody(RequestBody):
    """Body of POST /api/render/stl. id, file_id and member_id are aliases the old UI sent."""
    source_type: str | None = None
    id: int | str | None = None
    file_id: int | str | None = None
    member_id: int | str | None = None
    view: str | None = None
    force: bool | None = None


class ModelIdsBody(RequestBody):
    model_ids: list[int] | None = None
    confirm: bool = False


class DownloadQueueBody(RequestBody):
    model_ids: list[int] = Field(default_factory=list)
    release_id: str | None = None


class DownloadDequeueBody(RequestBody):
    release_id: str | None = None
    model_ids: list[int] = Field(default_factory=list)
    clear_status: str | None = None
    purge_incomplete: bool = False
    confirm: bool = False


class DownloadControlBody(RequestBody):
    action: str = ""
    confirm: bool = False


class StashCreateBody(RequestBody):
    name: str = ""
    description: str | None = None
    make_active: bool = False


class StashItemBody(RequestBody):
    item_type: str = ""
    model_id: int | None = None
    file_id: int | None = None
    submodel_id: int | None = None
    archive_member_id: int | None = None
    notes: str | None = None


class StashExportBody(RequestBody):
    """Body of POST /api/stashes/{id}/export. sync exists for the contract harness."""
    filter: str | None = None
    filter_mode: str | None = None
    sync: bool = False


class StorageConfigBody(RequestBody):
    """Body of POST /api/storage/config. path and work_path are old aliases."""
    download_root_path: str | None = None
    path: str | None = None
    download_work_path: str | None = None
    work_path: str | None = None


# Settings the app keeps for itself. A client overwriting these could fake a recent backup or clear
# the circuit breaker.
APP_ONLY_SETTINGS = frozenset({
    "circuit_breaker_reason", "circuit_breaker_tripped", "cookies_saved_at", "mmf_oauth_state",
    "mmf_token_expires_at", "mmf_user_id", "mmf_username",
})
APP_ONLY_PREFIXES = ("last_", "sync_")


class SettingsBody(BaseModel):
    """Settings are open key-value pairs, with the path fields typed."""

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def refuse_app_only_keys(self) -> "SettingsBody":
        refused = [key for key in self.model_extra or {} if key in APP_ONLY_SETTINGS or key.startswith(APP_ONLY_PREFIXES)]
        if refused:
            raise ValueError(f"Only the app can change {', '.join(sorted(refused))}")
        return self

    download_root_path: str | None = None
    download_work_path: str | None = None
    stash_download_path: str | None = None
    metadata_backup_path: str | None = None
    # Same limits _load_settings clamps to. A bad value used to save and then stop the next startup.
    worker_concurrency: int | None = Field(default=None, ge=1, le=16)
    offloader_concurrency: int | None = Field(default=None, ge=1, le=8)
    batch_size: int | None = Field(default=None, ge=1, le=100)
    batch_cooldown_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    stream_chunk_size_mb: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    offload_chunk_size_mb: float | None = Field(default=None, gt=0, allow_inf_nan=False)


class LibraryBody(RequestBody):
    path: str = ""
    name: str | None = None
    default_creator: str | None = None


class LibraryScanBody(RequestBody):
    mode: str | None = None


class LibraryApproveBody(RequestBody):
    skip_releases: list[str] | None = None
    ready_only: bool | None = None


class TagBody(RequestBody):
    name: str
    action: str | None = None
    new_name: str | None = None


class DuplicateDecisionBody(RequestBody):
    first_file_id: int
    second_file_id: int
    decision: str


class DuplicateDeleteBody(RequestBody):
    file_id: int
    confirm: bool = False


class PendingRestoreBody(RequestBody):
    id: int


class PendingPurgeBody(RequestBody):
    confirm: bool = False
    ids: list[int] | None = None


class DocumentStashBody(RequestBody):
    key: str


class SchedulesBody(RequestBody):
    integrity_enabled: bool = False
    integrity_interval_days: int = 30
    integrity_mode: str = "fast"


class ImportFileBody(RequestBody):
    paths: list[str]
    creator: str | None = None


class BackupBody(RequestBody):
    action: str
    name: str | None = None
    backup_dir: str | None = None
    mirror_dir: str | None = None


class ModelStashBody(RequestBody):
    scale: str | None = None
    support: str | None = None


class ArchiveReviewBody(RequestBody):
    cards: list[dict[str, Any]] | None = None
    reviewed: bool | None = None


class LibraryReviewBody(RequestBody):
    release: str
    cards: list[dict[str, Any]] | None = None
    reviewed: bool | None = None
    reset: bool | None = None


class ImageQueueBody(RequestBody):
    action: str = ""


class LoggingBody(RequestBody):
    level: str | None = None
    layers: list[str] | None = None


class LogsPurgeBody(RequestBody):
    days: int | str | None = None
    confirm: bool = False


class LoginBody(RequestBody):
    password: str


class ConfirmBody(RequestBody):
    confirm: bool = False


class StashPurgeBody(ConfirmBody):
    days: int | str | None = None


class IngestBody(RequestBody):
    scrape_folder: str | None = None
    folder: str | None = None
    raw_ids: str | None = None
    model_ids: list[Any] | None = None


class ReconcileScanBody(RequestBody):
    scan_mode: str = "fast"


class ResetBody(ConfirmBody):
    model_ids: list[int] = Field(default_factory=list)


class PathsBody(ConfirmBody):
    paths: list[str] | None = None


class UntrackedItem(RequestBody):
    model_id: int | None = None
    file_path: str | None = None


class UntrackedBody(RequestBody):
    items: list[UntrackedItem] = Field(default_factory=list)


class AuthConfigBody(RequestBody):
    client_id: str | None = None
    redirect_uri: str | None = None


class TokenCallbackBody(RequestBody):
    access_token: str = ""
    expires_in: int = 21600
    state: str | None = None
