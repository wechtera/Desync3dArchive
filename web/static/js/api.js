/**
 * web/static/js/api.js - Client API transport layer for DESYNC 3D Vault.
 * Handles REST requests for catalog, tasks, downloads, settings,
 * in-place external libraries, stash manifests, and ephemeral rendering.
 */

const RENDER_RETRIES = 3;

async function request(url, options = {}) {
  const res = await fetch(url, options);
  if (!res.ok) {
    let errorDetail = `HTTP ${res.status}`;
    try {
      const errJson = await res.json();
      if (errJson.error) errorDetail = errJson.error;
    } catch (_) {}
    throw new Error(errorDetail);
  }
  return res.json();
}

export async function fetchMetrics() {
  return request('/api/metrics');
}

export async function fetchStorageStatus() {
  return request('/api/storage/status');
}

export async function saveStorageConfig(payload) {
  const body = typeof payload === 'string' ? { path: payload } : payload;
  return request('/api/storage/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
}

export async function fetchDownloadsStatus() {
  return request('/api/downloads/status');
}

export async function fetchDownloadHistory({ status = '', sort = '', limit = 50, offset = 0 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (sort) params.set('sort', sort);
  params.set('limit', limit);
  params.set('offset', offset);
  return request(`/api/downloads/history?${params.toString()}`);
}

export async function queueDownloads(payload) {
  return request('/api/downloads/queue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function dequeueDownloads(payload) {
  return request('/api/downloads/queue', {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function controlDownloads(action) {
  return request('/api/downloads/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  });
}

export async function fetchAudit(designer = '') {
  return request(`/api/audit?designer=${encodeURIComponent(designer)}`);
}

export async function fetchReleases({ designer = '', bundlesOnly = false, limit = 24, offset = 0 } = {}) {
  const params = new URLSearchParams();
  if (designer) params.set('designer', designer);
  if (bundlesOnly) params.set('bundles_only', '1');
  params.set('limit', limit);
  params.set('offset', offset);
  return request(`/api/releases?${params.toString()}`);
}

export async function fetchCreators() {
  return request('/api/creators');
}

export async function fetchModels({
  search = '',
  designer = '',
  tag = '',
  month = '',
  bonus = false,
  hideCompleted = false,
  sort = '',
  limit = 24,
  offset = 0
} = {}) {
  const params = new URLSearchParams();
  if (search) params.set('search', search);
  if (designer) params.set('designer', designer);
  if (tag) params.set('tag', tag);
  if (month) params.set('month', month);
  if (bonus) params.set('bonus', '1');
  if (hideCompleted) params.set('hide_completed', '1');
  if (sort) params.set('sort', sort);
  params.set('limit', limit);
  params.set('offset', offset);
  return request(`/api/models?${params.toString()}`);
}

export async function fetchModelFiles(modelId) {
  return request(`/api/models/files?model_id=${encodeURIComponent(modelId)}`);
}

export async function fetchModelDetail(modelId) {
  return request(`/api/models/detail?id=${encodeURIComponent(modelId)}`);
}

// -----------------------------------------------------------------------------
// Archive Peeker & Submodels Inspection
// -----------------------------------------------------------------------------

export async function fetchArchiveSubmodels(fileId) {
  return request(`/api/models/archives?file_id=${encodeURIComponent(fileId)}`);
}

export async function fetchRawArchiveTree(fileId) {
  return request(`/api/models/archives/raw?file_id=${encodeURIComponent(fileId)}`);
}

export async function triggerArchivePeekBackfill(modelIds = null) {
  return request('/api/reconcile/archive-peek', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds }),
  });
}

// -----------------------------------------------------------------------------
// Settings & System Configuration
// -----------------------------------------------------------------------------

export async function fetchSettings() {
  return request('/api/settings');
}

export async function updateSettings(payload) {
  return request('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function browseDirectory(path = '') {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  return request(`/api/system/browse?${params.toString()}`);
}

export async function fetchDirectoryUsage() {
  const resp = await fetch('/api/settings/directories');
  if (!resp.ok) {
    throw new Error(`Failed to fetch directory usage: ${resp.statusText}`);
  }
  return await resp.json();
}

// -----------------------------------------------------------------------------
// In-Place External Libraries
// -----------------------------------------------------------------------------

export async function fetchExternalLibraries() {
  return request('/api/settings/libraries');
}

export async function addExternalLibrary(payload) {
  return request('/api/settings/libraries', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function deleteExternalLibrary(libraryId) {
  return request(`/api/settings/libraries/${libraryId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function scanExternalLibrary(libraryId, mode = 'full') {
  return request(`/api/settings/libraries/${libraryId}/scan`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode })
  });
}

// -----------------------------------------------------------------------------
// 3D Rendering & Ephemeral View Streaming
// -----------------------------------------------------------------------------

/**
 * Caches a persistent 3D model perspective to previews/<model_id>/view_<view>.jpg
 */
export async function renderModelView(modelId, view = 'front_left', force = false) {
  return request(`/api/models/${modelId}/render-view`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ view, force })
  });
}

// The server runs two renders at a time and answers 429 past that, saying when to come back.
export async function requestRender(payload) {
  for (let attempt = 0; ; attempt += 1) {
    const res = await fetch('/api/render/stl', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res.status !== 429 || attempt === RENDER_RETRIES) return res;
    const seconds = Number(res.headers.get('Retry-After')) || 5;
    await new Promise((resolve) => setTimeout(resolve, seconds * 1000));
  }
}

/**
 * Builds a direct streaming URL for on-demand ephemeral JPEG rendering.
 * Does not write any persistent file to previews/.
 */
export function getEphemeralRenderUrl(sourceType, id, view = 'front_left') {
  const params = new URLSearchParams({
    source_type: sourceType,
    id: String(id),
    view: view,
    _t: String(Date.now())
  });
  return `/api/render/stl?${params.toString()}`;
}

// -----------------------------------------------------------------------------
// Native MMF Authentication
// -----------------------------------------------------------------------------

export async function fetchAuthStatus() {
  return request('/api/auth/status');
}

export async function saveAuthConfig(payload) {
  return request('/api/auth/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function getAuthLoginUrl() {
  return request('/api/auth/login?redirect=0', { method: 'POST' });
}

export async function disconnectAuth() {
  return request('/api/auth/disconnect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  });
}

// -----------------------------------------------------------------------------
// Background Pipeline Tasks
// -----------------------------------------------------------------------------

export async function fetchTasksStatus() {
  return request('/api/tasks/status');
}

// The status poll leaves out logs and reports, so a finished task's come from here.
export async function fetchTask(taskId) {
  return request(`/api/tasks/${taskId}`);
}

export async function cancelTask() {
  return request('/api/tasks/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function killTask() {
  return request('/api/tasks/kill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function previewClearTasksHistory() {
  return request('/api/tasks/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({}),
  });
}

export async function clearTasksHistory() {
  return request('/api/tasks/clear', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: true }),
  });
}

export async function startScraper() {
  return request('/api/scraper/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function fetchScrapesList() {
  return request('/api/scrapes/list');
}

export async function startIngest(payload = {}) {
  return request('/api/ingest/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

// -----------------------------------------------------------------------------
// Vault Reconciliation
// -----------------------------------------------------------------------------

export async function runReconcileScan(scanMode = 'fast') {
  return request('/api/reconcile/scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scan_mode: scanMode }),
  });
}

export async function fetchReconcileStatus() {
  return request('/api/reconcile/status');
}

export async function cancelReconcileScan() {
  return request('/api/reconcile/cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
  });
}

export async function resetReconcileModels(modelIds) {
  return request('/api/reconcile/reset', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds, confirm: true }),
  });
}

export async function deleteOrphanDirectories(paths) {
  return request('/api/reconcile/delete-orphans', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths, confirm: true }),
  });
}

export async function ingestUntrackedFiles(items) {
  return request('/api/reconcile/ingest-untracked', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  });
}

export async function fetchStagingGarbage() {
  return request('/api/reconcile/staging', { method: 'POST' });
}

export async function purgeStagingGarbage(paths = null) {
  return request('/api/reconcile/staging/purge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ paths, confirm: true }),
  });
}

export async function fetchDuplicates(includeShared = false) {
  return request(`/api/reconcile/duplicates?include_shared=${includeShared ? 1 : 0}`, { method: 'POST' });
}

export async function fetchPathDrift() {
  return request('/api/reconcile/drift', { method: 'POST' });
}

export async function realignModelPaths(modelIds = null, confirm = false) {
  return request('/api/reconcile/drift/realign', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds, confirm }),
  });
}

export async function triggerCatalogPreviewBackfill(modelIds = null) {
  return request('/api/reconcile/catalog-previews', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds }),
  });
}

// -----------------------------------------------------------------------------
// Stash System ("A pile of gray") API Client
// -----------------------------------------------------------------------------

export async function fetchStashes() {
  return request('/api/stashes');
}

export async function createStash({ name, description = 'A pile of gray', make_active = false }) {
  return request('/api/stashes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description, make_active })
  });
}

export async function fetchActiveStash() {
  return request('/api/stashes/active');
}

export async function setActiveStash(stashId) {
  return request(`/api/stashes/${stashId}/active`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function fetchStashDetail(stashId) {
  return request(`/api/stashes/${stashId}`);
}

export async function deleteStash(stashId) {
  return request(`/api/stashes/${stashId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function addStashItem(stashId, { item_type, model_id, file_id = null, submodel_id = null, archive_member_id = null, notes = '' }) {
  const res = await fetch(`/api/stashes/${stashId}/items`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_type, model_id, file_id, submodel_id, archive_member_id, notes })
  });
  const data = await res.json();
  if (!res.ok && res.status !== 409) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export async function removeStashItem(stashItemId) {
  return request(`/api/stashes/items/${stashItemId}`, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' }
  });
}

export async function bulkAddStashModels(stashId, modelIds) {
  return request(`/api/stashes/${stashId}/bulk-models`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_ids: modelIds })
  });
}

export async function exportStash(stashId, options = {}) {
  const res = await fetch(`/api/stashes/${stashId}/export`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(options)
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(data.error || `HTTP ${res.status}`);
  }
  return data;
}

export async function fetchLibraryPreview(libraryId) {
  return request(`/api/settings/libraries/${libraryId}/preview`);
}

export async function fetchLibraryRelease(libraryId, release) {
  return request(`/api/settings/libraries/${libraryId}/preview/release?name=${encodeURIComponent(release)}`);
}

export async function saveLibraryReview(libraryId, payload) {
  return request(`/api/settings/libraries/${libraryId}/preview/review`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

const postJson = (url, payload) => request(url, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(payload)
});

export const fetchTags = (search, hints = false) => request(`/api/tags?q=${encodeURIComponent(search)}${hints ? '&hints=1' : ''}`);
export const postTag = (payload) => postJson('/api/tags', payload);
export const postModelTag = (modelId, payload) => postJson(`/api/catalog-models/${modelId}/tags`, payload);
export const postGroupTag = (groupId, payload) => postJson(`/api/model-groups/${groupId}/tags`, payload);
export const fetchCatalogReview = (libraryId, release) => request(`/api/settings/libraries/${libraryId}/catalog-review?name=${encodeURIComponent(release)}`);
export const saveCatalogReview = (libraryId, payload) => postJson(`/api/settings/libraries/${libraryId}/catalog-review`, payload);

export async function importFile(payload) {
  return request('/api/import-file', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function fetchAuditHistory(action) {
  return request(`/api/audit-history?action=${encodeURIComponent(action)}`);
}

export async function fetchBackups() {
  return request('/api/backups');
}

export async function postBackups(payload) {
  return request('/api/backups', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function fetchArchiveQueue() {
  return request('/api/archive-review');
}

export async function fetchArchiveReview(collectionId) {
  return request(`/api/archive-review/${collectionId}`);
}

export async function saveArchiveReview(collectionId, payload) {
  return request(`/api/archive-review/${collectionId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function fetchCatalogModels(search, page, tag = '', creator = '') {
  return request(`/api/catalog-models?q=${encodeURIComponent(search)}&page=${page}&tag=${encodeURIComponent(tag)}&creator=${encodeURIComponent(creator)}`);
}

export async function fetchDocuments(filters) {
  return request(`/api/documents?${new URLSearchParams(filters)}`);
}

export async function stashDocument(key) {
  return postJson('/api/documents/stash', { key });
}

export async function updateDocuments() {
  return postJson('/api/documents/update', {});
}

export async function fetchLatestDuplicates() {
  return request('/api/reconcile/duplicates/latest');
}

export async function postDuplicateDecision(firstFileId, secondFileId, decision) {
  return postJson('/api/reconcile/duplicates/decision', { first_file_id: firstFileId, second_file_id: secondFileId, decision });
}

export async function deleteDuplicateFile(fileId) {
  return postJson('/api/reconcile/duplicates/delete', { file_id: fileId, confirm: true });
}

export async function fetchPendingDeletion() {
  return request('/api/pending-deletion');
}

export async function fetchPendingItems() {
  return request('/api/pending-deletion/items');
}

export async function restorePendingItem(id) {
  return postJson('/api/pending-deletion/restore', { id });
}

export async function purgePendingDeletion(body) {
  return postJson('/api/pending-deletion/purge', body);
}

export async function fetchReconcileHistory() {
  return request('/api/reconcile/history');
}

export async function fetchHealth() {
  return request('/api/health');
}

export async function postSchedules(body) {
  return postJson('/api/schedules', body);
}

export async function fetchLocations() {
  return request('/api/settings/locations');
}

export async function browseImportFolder(folder) {
  return request(`/api/system/browse?files=1${folder ? `&path=${encodeURIComponent(folder)}` : ''}`);
}

export async function fetchCatalogModel(modelId) {
  return request(`/api/catalog-models/${modelId}`);
}

export async function stashModelSelection(modelId, payload) {
  return request(`/api/catalog-models/${modelId}/stash`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
}

export async function fetchModelGroup(groupId) {
  return request(`/api/model-groups/${groupId}`);
}

export async function approveLibraryPreview(libraryId, skipReleases, readyOnly = false) {
  return request(`/api/settings/libraries/${libraryId}/preview/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(readyOnly ? { skip_releases: skipReleases, ready_only: true } : { skip_releases: skipReleases })
  });
}

export async function fetchImageQueue() {
  return request('/api/library-images');
}

export async function postImageQueue(action) {
  return request('/api/library-images', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action })
  });
}

export async function fetchInactive(filters = {}) {
  const params = new URLSearchParams(Object.entries(filters).filter(([, value]) => value));
  return request(`/api/inactive?${params}`);
}
