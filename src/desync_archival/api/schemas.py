"""Pydantic request bodies. Each one lists the fields its handler reads."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ModelViewBody(RequestBody):
    view: str | None = None
    force: bool | None = None


class RenderStlBody(RequestBody):
    source_type: str | None = None
    id: int | str | None = None
    file_id: int | str | None = None
    member_id: int | str | None = None
    view: str | None = None
    force: bool | None = None


class ModelIdsBody(RequestBody):
    model_ids: list[int] | None = None


class DownloadQueueBody(RequestBody):
    model_ids: list[int] = Field(default_factory=list)
    release_id: str | None = None


class DownloadDequeueBody(RequestBody):
    model_ids: list[int] = Field(default_factory=list)
    clear_status: str | None = None
    purge_incomplete: bool = False


class DownloadControlBody(RequestBody):
    action: str = ""


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
    filter: str | None = None
    filter_mode: str | None = None
    sync: bool = False


class StorageConfigBody(RequestBody):
    download_root_path: str | None = None
    path: str | None = None
    download_work_path: str | None = None
    work_path: str | None = None


class SettingsBody(BaseModel):
    """Settings are open key-value pairs, with the path fields typed."""

    model_config = ConfigDict(extra="allow")

    download_root_path: str | None = None
    download_work_path: str | None = None
    stash_download_path: str | None = None
    metadata_backup_path: str | None = None


class LibraryBody(RequestBody):
    path: str = ""
    name: str | None = None
    default_creator: str | None = None


class LibraryScanBody(RequestBody):
    mode: str | None = None


class LibraryApproveBody(RequestBody):
    skip_releases: list[str] | None = None
    ready_only: bool | None = None


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
