import { loadArchiveQueue, loadExternalLibraries } from './libraries.js';
import {
  fetchModels,
  fetchCreators,
  fetchDownloadsStatus,
  fetchDownloadHistory,
  queueDownloads,
  dequeueDownloads,
  controlDownloads,
  runReconcileScan,
  fetchReconcileStatus,
  fetchReconcileHistory,
  fetchLatestDuplicates,
  postDuplicateDecision,
  deleteDuplicateFile,
  cancelReconcileScan,
  resetReconcileModels,
  deleteOrphanDirectories,
  ingestUntrackedFiles,
  fetchStagingGarbage,
  purgeStagingGarbage,
  fetchPathDrift,
  fetchDuplicates,
  realignModelPaths,
  triggerArchivePeekBackfill,
  triggerCatalogPreviewBackfill
} from '../api.js';
import { formatBytes, formatLocalDate, formatLocalDateTime, statusBadge } from '../utils.js';
import { appendToBody, html, imageFallback, nothing, render, safeUrl, stopPropagation } from '../template.js';

let currentPendingPage = 1;
const pendingPageSize = 25;
let totalPendingCount = 0;
const selectedPendingIds = new Set();
let pendingCreatorsLoaded = false;

let currentHistoryPage = 1;
const historyPageSize = 25;
let totalHistoryCount = 0;

let activeTransfersTimer = null;
let currentDiscrepancies = [];
let currentOrphanDirectories = [];
let currentUntrackedFiles = [];
let currentStagingGarbage = [];
let currentPathDrifts = [];
let selectedScanMode = 'fast';

const formatDate = formatLocalDate;

function runLabel() {
  return `Run ${selectedScanMode === 'deep' ? 'Deep' : 'Fast'} Reconciliation`;
}

// Results sit above the tools, so a long list would otherwise appear out of sight.
function showAuditState() {
  document.getElementById('reconcile-audit-state')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

export async function loadRunHistory() {
  const slots = document.querySelectorAll('[data-run]');
  if (!slots.length) return;
  try {
    const { runs } = await fetchReconcileHistory();
    slots.forEach((slot) => {
      const run = runs[slot.dataset.run];
      if (!run) {
        render(html`Not run yet`, slot);
        return;
      }
      const mode = slot.dataset.run === 'forward' && run.scan_mode ? ` (${run.scan_mode})` : '';
      render(html`
        <span>Last run ${formatLocalDateTime(run.completed_at || run.started_at)}${mode}</span>
        <span>Job #${run.task_id}</span>
        <span class="reconcile-run-status reconcile-run-${run.status}">${run.status}</span>
        ${run.progress_label ? html`<span>${run.progress_label}</span>` : nothing}
      `, slot);
    });
  } catch (err) {
    console.warn('Run history unavailable:', err);
  }
}

document.addEventListener('click', (event) => {
  const run = event.target.closest?.('.reconcile-run');
  if (!run) return;
  if (run.id !== 'btn-scan-duplicates') showAuditState();
  setTimeout(loadRunHistory, 1500);
});

export function setReconcileMode(mode) {
  selectedScanMode = mode;
  const fastBtn = document.getElementById('btn-mode-fast');
  const deepBtn = document.getElementById('btn-mode-deep');
  if (fastBtn && deepBtn) {
    fastBtn.classList.toggle('active', mode === 'fast');
    deepBtn.classList.toggle('active', mode === 'deep');
  }
  const btnRun = document.getElementById('btn-run-reconcile');
  if (btnRun) {
    btnRun.textContent = runLabel();
  }
}

/** Polls transfers while the Active view shows. Stops itself when the view is hidden. */
export function startActiveTransfersPolling() {
  stopActiveTransfersPolling();

  async function poll() {
    const viewDl = document.getElementById('view-downloads');
    const subActive = document.getElementById('sub-dl-active');

    if (!viewDl || viewDl.style.display === 'none' || !subActive || subActive.style.display === 'none') {
      stopActiveTransfersPolling();
      return;
    }

    const data = await renderActiveTransfers();
    const hasActive = Boolean(
      (data?.active_downloads && data.active_downloads.length > 0) ||
      (data?.active_offloads && data.active_offloads.length > 0) ||
      (data?.counts?.pending > 0) ||
      (data?.counts?.downloading > 0) ||
      (data?.counts?.staging_complete > 0) ||
      (data?.counts?.offloading > 0)
    );

    activeTransfersTimer = setTimeout(poll, hasActive ? 1000 : 2500);
  }

  activeTransfersTimer = setTimeout(poll, 1000);
}

export function stopActiveTransfersPolling() {
  if (activeTransfersTimer) {
    clearTimeout(activeTransfersTimer);
    activeTransfersTimer = null;
  }
}

const DOWNLOAD_GROUPS = {
  sync: ['sync'],
  download: ['pending', 'active', 'history'],
  external: ['libraries', 'archives', 'import'],
  reconcile: ['reconcile'],
};

export function switchDownloadGroup(group) {
  switchDownloadSubView(DOWNLOAD_GROUPS[group][0]);
}

export function switchDownloadSubView(subview) {
  Object.values(DOWNLOAD_GROUPS).flat().forEach(sv => {
    const el = document.getElementById(`sub-dl-${sv}`);
    if (el) el.style.display = (sv === subview) ? 'block' : 'none';
  });

  const group = Object.keys(DOWNLOAD_GROUPS).find(g => DOWNLOAD_GROUPS[g].includes(subview));
  ['download', 'external'].forEach(name => {
    const row = document.getElementById(`minor-${name}`);
    if (row) row.style.display = name === group ? 'flex' : 'none';
  });
  document.querySelectorAll('#view-downloads .sub-tabs .sub-btn').forEach(btn => {
    const active = btn.dataset.group ? btn.dataset.group === group : (btn.getAttribute('data-args') || '').includes(`"${subview}"`);
    btn.classList.toggle('active', active);
  });

  if (subview === 'pending') {
    stopActiveTransfersPolling();
    loadPendingModels();
  } else if (subview === 'active') {
    renderActiveTransfers();
    startActiveTransfersPolling();
  } else if (subview === 'history') {
    stopActiveTransfersPolling();
    loadDownloadHistory();
  } else if (subview === 'reconcile') {
    stopActiveTransfersPolling();
    loadReconcileView();
  } else if (subview === 'sync') {
    stopActiveTransfersPolling();
  } else if (subview === 'libraries') {
    stopActiveTransfersPolling();
    loadExternalLibraries();
  } else if (subview === 'archives') {
    stopActiveTransfersPolling();
    loadArchiveQueue();
  }
}

window.switchDownloadSubView = switchDownloadSubView;
window.switchDownloadGroup = switchDownloadGroup;

export async function populatePendingCreatorsFilter() {
  const select = document.getElementById('pending-creator-filter');
  if (!select) return;

  const currentSelection = select.value;
  try {
    const creators = await fetchCreators();
    const optionsHtmlParts = [html`<option value="">All Creators</option>`];
    optionsHtmlParts.push(creators.map(c => {
      const name = c.designer_name || 'Unknown';
      const count = c.total_models || 0;
      return html`<option value="${name}">${name} (${count})</option>`;
    }));

    render(optionsHtmlParts, select);
    if (currentSelection) select.value = currentSelection;
    pendingCreatorsLoaded = true;
  } catch (err) {
    console.error('Failed to populate pending creators:', err);
  }
}

export async function loadPendingModels(page = currentPendingPage) {
  if (!pendingCreatorsLoaded) {
    await populatePendingCreatorsFilter();
  }

  currentPendingPage = Math.max(1, page);

  const searchEl = document.getElementById('pending-search');
  const creatorEl = document.getElementById('pending-creator-filter');
  const sortEl = document.getElementById('pending-sort');
  const hideCompletedEl = document.getElementById('pending-hide-completed');

  const search = searchEl ? searchEl.value.trim() : '';
  const designer = creatorEl ? creatorEl.value : '';
  const sort = sortEl ? sortEl.value : 'date_desc';
  const hideCompleted = hideCompletedEl ? hideCompletedEl.checked : true;

  const offset = (currentPendingPage - 1) * pendingPageSize;
  const tbody = document.getElementById('pending-table-body');
  if (tbody) {
    render(html`<tr><td colspan="7" class="table-msg">Loading catalog items...</td></tr>`, tbody);
  }

  try {
    const data = await fetchModels({
      search,
      designer,
      sort,
      hideCompleted,
      limit: pendingPageSize,
      offset
    });

    totalPendingCount = data.total || 0;
    const models = data.models || [];

    if (!tbody) return;

    if (models.length === 0) {
      render(html`<tr><td colspan="7" class="table-msg">No matching models found.</td></tr>`, tbody);
      updatePendingPagination(offset, 0);
      return;
    }

    render(models.map(m => {
      const isChecked = selectedPendingIds.has(m.id);
      const isCompleted = m.download_status === 'completed';
      const isQueued = m.download_status === 'queued';

      let actionBtn = '';
      if (isCompleted) {
        actionBtn = html`<button type="button" class="btn-pill" disabled>Archived</button>`;
      } else if (isQueued) {
        actionBtn = html`<button type="button" class="btn-pill btn-action-cancel" @click=${() => window.dequeueSingleModel(m.id)}>Cancel</button>`;
      } else {
        actionBtn = html`<button type="button" class="btn-pill btn-action-queue" @click=${() => window.queueSingleModel(m.id)}>Queue</button>`;
      }

      const creator = m.designer_name || m.creator_name || m.creator || 'Unknown';
      const fileCount = m.file_count ?? m.total_files ?? m.files ?? 0;
      const dateStr = formatDate(m.published_at || m.date_added);

      return html`
        <tr>
          <td class="col-checkbox">
            <input type="checkbox" class="pending-row-chk" value="${m.id}" ?checked=${isChecked} @change=${(event) => window.toggleSelectPending(m.id, event.target.checked)}>
          </td>
          <td>
            <div class="cell-clickable-title" @click=${() => window.openModelDetailModal(m.id)} title="View Metadata">
              ${m.name}
            </div>
            <span class="font-mono text-dim font-xs">#${m.id}</span>
          </td>
          <td>
            <span class="cell-creator-accent">${creator}</span>
          </td>
          <td>
            <span class="font-semibold font-mono">${fileCount}</span>
          </td>
          <td>
            <span class="font-mono text-dim font-sm">${dateStr}</span>
          </td>
          <td>${statusBadge(m.download_status || 'not_downloaded')}</td>
          <td>${actionBtn}</td>
        </tr>
      `;
    }), tbody);

    updatePendingPagination(offset, models.length);
    updateSelectAllCheckbox(models);
  } catch (err) {
    if (tbody) {
      render(html`<tr><td colspan="7" class="table-msg text-alert">Failed loading models: ${err.message}</td></tr>`, tbody);
    }
  }
}

export function resetPendingPageAndLoad() {
  loadPendingModels(1);
}

export function changePendingPage(delta) {
  const totalPages = Math.ceil(totalPendingCount / pendingPageSize) || 1;
  const target = currentPendingPage + delta;
  if (target >= 1 && target <= totalPages) {
    loadPendingModels(target);
  }
}

function updatePendingPagination(offset, count) {
  const pageInfo = document.getElementById('pending-page-info');
  const pageNum = document.getElementById('pending-page-num');
  const prevBtn = document.getElementById('pending-prev-btn');
  const nextBtn = document.getElementById('pending-next-btn');

  const totalPages = Math.ceil(totalPendingCount / pendingPageSize) || 1;
  const start = totalPendingCount === 0 ? 0 : offset + 1;
  const end = offset + count;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalPendingCount.toLocaleString()} models`;
  if (pageNum) pageNum.textContent = `Page ${currentPendingPage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentPendingPage <= 1;
  if (nextBtn) nextBtn.disabled = currentPendingPage >= totalPages;
}

/** Adds or removes one model from the selection, which survives paging. */
export function toggleSelectPending(modelId, isChecked) {
  if (isChecked) {
    selectedPendingIds.add(modelId);
  } else {
    selectedPendingIds.delete(modelId);
  }
  updateSelectedCount();
}

export function toggleSelectAllPending(isChecked) {
  const checkboxes = document.querySelectorAll('.pending-row-chk');
  checkboxes.forEach(chk => {
    const id = parseInt(chk.value, 10);
    chk.checked = isChecked;
    if (isChecked) {
      selectedPendingIds.add(id);
    } else {
      selectedPendingIds.delete(id);
    }
  });
  updateSelectedCount();
}

function updateSelectAllCheckbox(currentModels) {
  const selectAll = document.getElementById('select-all-pending');
  if (!selectAll || currentModels.length === 0) return;
  const allChecked = currentModels.every(m => selectedPendingIds.has(m.id));
  selectAll.checked = allChecked;
}

function updateSelectedCount() {
  const counter = document.getElementById('selected-count');
  if (counter) counter.textContent = selectedPendingIds.size;
}

export async function queueSelectedModels() {
  if (selectedPendingIds.size === 0) {
    alert('Please select at least one model to queue.');
    return;
  }
  const ids = Array.from(selectedPendingIds);
  try {
    await queueDownloads({ model_ids: ids });
    selectedPendingIds.clear();
    updateSelectedCount();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Failed to queue models: ${err.message}`);
  }
}

export async function dequeueSelectedModels() {
  if (selectedPendingIds.size === 0) {
    alert('Please select at least one model to remove.');
    return;
  }
  const ids = Array.from(selectedPendingIds);
  try {
    reportKeptJobs(await dequeueDownloads({ model_ids: ids }));
    selectedPendingIds.clear();
    updateSelectedCount();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Failed to remove models: ${err.message}`);
  }
}

export async function queueSingleModel(modelId) {
  try {
    await queueDownloads({ model_ids: [modelId] });
    window.refreshCurrentView();
  } catch (err) {
    alert(`Queue failed: ${err.message}`);
  }
}

function reportKeptJobs(result) {
  if (result?.kept) {
    alert(`${result.kept} already started or finished, so they stayed. A running job can't be removed until it ends.`);
  }
}

export async function dequeueSingleModel(modelId) {
  try {
    reportKeptJobs(await dequeueDownloads({ model_ids: [modelId] }));
    window.refreshCurrentView();
  } catch (err) {
    alert(`Cancel failed: ${err.message}`);
  }
}

export async function clearQueueStatus(status = 'pending') {
  if (!confirm(`Are you sure you want to clear all ${status} jobs from the queue?`)) return;
  try {
    await dequeueDownloads({ clear_status: status });
    selectedPendingIds.clear();
    updateSelectedCount();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Clear queue failed: ${err.message}`);
  }
}

/** Purges pending, downloading and failed jobs, after asking. */
export async function confirmPurgeIncomplete() {
  if (!confirm('Purge all pending and downloading jobs? Completed files will remain intact.')) return;
  try {
    await dequeueDownloads({ purge_incomplete: true });
    renderActiveTransfers();
    window.refreshCurrentView();
  } catch (err) {
    alert(`Purge failed: ${err.message}`);
  }
}

/** Draws workers and the queue. Returns the status, or null on failure, so the poll can pace itself. */
export async function renderActiveTransfers() {
  try {
    const data = await fetchDownloadsStatus();

    const stateEl = document.getElementById('active-queue-state');
    const pendEl = document.getElementById('active-queue-pending');
    const stageEl = document.getElementById('active-queue-staged');

    if (stateEl) {
      stateEl.textContent = data.is_paused ? 'PAUSED' : (data.is_running ? 'RUNNING' : 'STOPPED');
      stateEl.className = data.is_paused ? 'text-warn font-bold' : (data.is_running ? 'text-accent font-bold' : 'text-dim font-bold');
    }
    if (pendEl) pendEl.textContent = data.counts?.pending || 0;
    if (stageEl) stageEl.textContent = data.counts?.staging_complete || 0;

    const dlBox = document.getElementById('active-download-workers-container');
    if (dlBox) {
      const activeDownloads = data.active_downloads || [];
      if (activeDownloads.length === 0) {
        render(html`<div class="empty-state-notice">No active downloads in flight.</div>`, dlBox);
      } else {
        render(activeDownloads.map(w => {
          const pct = w.total_bytes > 0 ? Math.min(100, Math.round((w.downloaded_bytes / w.total_bytes) * 100)) : 0;
          return html`
            <div class="progress-box">
              <div class="progress-box-top">
                <span><strong>${w.model_name}</strong> &bull; ${w.current_file || ''}</span>
                <span class="font-mono">${formatBytes(w.downloaded_bytes)} / ${formatBytes(w.total_bytes)} (${pct}%)</span>
              </div>
              <div class="progress-bar-bg progress-bar-flush">
                <div class="progress-bar-fill" style="width:${pct}%"></div>
              </div>
              <div class="progress-box-meta font-mono">
                Speed: ${formatBytes(w.speed_bps || 0)}/s &bull; Creator: ${w.creator || 'Unknown'}
              </div>
            </div>
          `;
        }), dlBox);
      }
    }

    const offBox = document.getElementById('active-offload-workers-container');
    if (offBox) {
      const activeOffloads = data.active_offloads || [];
      if (activeOffloads.length === 0) {
        render(html`<div class="empty-state-notice">No offload migrations in progress.</div>`, offBox);
      } else {
        render(activeOffloads.map(w => {
          const pct = w.total_bytes > 0 ? Math.min(100, Math.round((w.transferred_bytes / w.total_bytes) * 100)) : 0;
          return html`
            <div class="progress-box">
              <div class="progress-box-top">
                <span><strong>${w.model_name}</strong> &bull; ${w.current_file || ''}</span>
                <span class="font-mono">${formatBytes(w.transferred_bytes)} / ${formatBytes(w.total_bytes)} (${pct}%)</span>
              </div>
              <div class="progress-bar-bg progress-bar-flush">
                <div class="progress-bar-fill" style="width:${pct}%"></div>
              </div>
              <div class="progress-box-meta font-mono">
                LAN Transfer Speed: ${formatBytes(w.speed_bps || 0)}/s
              </div>
            </div>
          `;
        }), offBox);
      }
    }

    const activeTbody = document.getElementById('active-pending-table-body');
    if (activeTbody) {
      const jobs = data.queued_jobs || [];
      if (jobs.length === 0) {
        render(html`<tr><td colspan="8" class="td-empty">No jobs currently queued.</td></tr>`, activeTbody);
      } else {
        render(jobs.map(j => html`
          <tr>
            <td class="font-mono">${j.model_id}</td>
            <td><strong>${j.model_name || `Model #${j.model_id}`}</strong></td>
            <td>${j.creator_name || 'Unknown'}</td>
            <td class="font-mono">${j.total_files || 0}</td>
            <td class="font-mono">${formatBytes(j.total_bytes || 0)}</td>
            <td class="font-mono text-dim font-sm">${formatDate(j.enqueued_at)}</td>
            <td>${statusBadge(j.status)}</td>
            <td>
              <button type="button" class="btn-pill btn-action-cancel" @click=${() => window.dequeueSingleModel(j.model_id)}>Cancel</button>
            </td>
          </tr>
        `), activeTbody);
      }
    }

    return data;
  } catch (err) {
    console.error('Failed active transfers poll:', err);
    return null;
  }
}

export async function controlDownload(action) {
  try {
    await controlDownloads(action);
    renderActiveTransfers();
  } catch (err) {
    alert(`Control action failed: ${err.message}`);
  }
}

export async function loadDownloadHistory(page = currentHistoryPage) {
  currentHistoryPage = Math.max(1, page);

  const filterEl = document.getElementById('history-filter');
  const sortEl = document.getElementById('history-sort');

  const status = filterEl ? filterEl.value : '';
  const sort = sortEl ? sortEl.value : 'enqueued_desc';

  const offset = (currentHistoryPage - 1) * historyPageSize;
  const tbody = document.getElementById('history-table-body');

  if (tbody) {
    render(html`<tr><td colspan="8" class="table-msg">Loading audit logs...</td></tr>`, tbody);
  }

  try {
    const data = await fetchDownloadHistory({
      status,
      sort,
      limit: historyPageSize,
      offset
    });

    totalHistoryCount = data.total || 0;
    const jobs = data.history || [];

    if (!tbody) return;

    if (jobs.length === 0) {
      render(html`<tr><td colspan="8" class="table-msg">No historical jobs recorded.</td></tr>`, tbody);
      updateHistoryPagination(offset, 0);
      return;
    }

    render(jobs.map(j => {
      const creator = j.creator_name || j.designer_name || 'Unknown';
      return html`
        <tr>
          <td class="font-mono text-dim font-sm">${formatDate(j.enqueued_at)}</td>
          <td class="font-mono text-dim font-sm">${formatDate(j.completed_at)}</td>
          <td>
            <div class="cell-clickable-title" @click=${() => window.openModelDetailModal(j.model_id)}>
              ${j.model_name || `Model #${j.model_id}`}
            </div>
          </td>
          <td>${creator}</td>
          <td>${statusBadge(j.status)}</td>
          <td class="font-mono">${j.retry_count || 0}</td>
          <td class="cell-label-truncate font-mono font-xs" title="${j.error_message || ''}">
            ${j.error_message || '-'}
          </td>
          <td>
            ${j.status === 'failed'
              ? html`<button type="button" class="btn-pill btn-action-queue" @click=${() => window.queueSingleModel(j.model_id)}>Retry</button>`
              : html`<span class="text-dim font-sm">None</span>`
            }
          </td>
        </tr>
      `;
    }), tbody);

    updateHistoryPagination(offset, jobs.length);
  } catch (err) {
    if (tbody) {
      render(html`<tr><td colspan="8" class="table-msg text-alert">Failed to load history: ${err.message}</td></tr>`, tbody);
    }
  }
}

export function resetHistoryPageAndLoad() {
  loadDownloadHistory(1);
}

export function changeHistoryPage(delta) {
  const totalPages = Math.ceil(totalHistoryCount / historyPageSize) || 1;
  const target = currentHistoryPage + delta;
  if (target >= 1 && target <= totalPages) {
    loadDownloadHistory(target);
  }
}

function updateHistoryPagination(offset, count) {
  const pageInfo = document.getElementById('history-page-info');
  const pageNum = document.getElementById('history-page-num');
  const prevBtn = document.getElementById('history-prev-btn');
  const nextBtn = document.getElementById('history-next-btn');

  const totalPages = Math.ceil(totalHistoryCount / historyPageSize) || 1;
  const start = totalHistoryCount === 0 ? 0 : offset + 1;
  const end = offset + count;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalHistoryCount.toLocaleString()} jobs`;
  if (pageNum) pageNum.textContent = `Page ${currentHistoryPage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentHistoryPage <= 1;
  if (nextBtn) nextBtn.disabled = currentHistoryPage >= totalPages;
}

// -----------------------------------------------------------------------------
// Reconciliation Sub-View & Telemetry Consumer
// -----------------------------------------------------------------------------

export function updateReconcileFromHealth(task) {
  if (!task || task.task_type !== 'vault_reconcile') return;

  const progressBox = document.getElementById('reconcile-progress-container');
  const progressBar = document.getElementById('reconcile-progress-bar');
  const progressPct = document.getElementById('reconcile-progress-pct');
  const currentItemEl = document.getElementById('reconcile-current-item');
  const statusBadge = document.getElementById('reconcile-status-badge');
  const logsEl = document.getElementById('reconcile-task-logs');
  const btnRun = document.getElementById('btn-run-reconcile');

  const scanMode = (task.params?.scan_mode || selectedScanMode).toUpperCase();

  if (task.status === 'running') {
    if (progressBox) progressBox.style.display = 'block';
    if (statusBadge) {
      statusBadge.textContent = `${scanMode} SCAN`;
      statusBadge.className = 'status-badge status-queued';
    }
    if (btnRun) {
      btnRun.disabled = true;
      btnRun.textContent = `${scanMode} Audit Running...`;
    }

    const pct = Math.min(100, Math.max(0, task.progress_pct || 0));
    if (progressBar) progressBar.style.width = `${pct}%`;
    if (progressPct) progressPct.textContent = `${pct}%`;
    if (currentItemEl) currentItemEl.textContent = task.progress_label || 'Auditing storage...';

    if (logsEl && task.log_output) {
      logsEl.textContent = task.log_output;
      logsEl.scrollTop = logsEl.scrollHeight;
    }
  } else if (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
    if (btnRun) {
      btnRun.disabled = false;
      btnRun.textContent = runLabel();
    }
    loadReconcileView();
  }
}

export async function loadReconcileView() {
  loadRunHistory();
  loadDuplicateResults();
  const progressBox = document.getElementById('reconcile-progress-container');
  const btnRun = document.getElementById('btn-run-reconcile');
  const lastScanEl = document.getElementById('reconcile-last-scan');
  const fixAllBtn = document.getElementById('btn-fix-all-reconcile');
  const orphanSection = document.getElementById('reconcile-orphan-section');

  try {
    const data = await fetchReconcileStatus();

    if (data.status === 'running') {
      updateReconcileFromHealth({
        task_type: 'vault_reconcile',
        status: 'running',
        progress_pct: data.progress_pct,
        progress_label: data.current_item,
        log_output: data.log_output,
        params: { scan_mode: data.scan_mode }
      });
      return data;
    }

    if (progressBox) progressBox.style.display = 'none';
    if (btnRun) {
      btnRun.disabled = false;
      btnRun.textContent = runLabel();
    }

    const summary = data.summary || {};
    currentDiscrepancies = data.discrepancies || [];
    currentOrphanDirectories = data.orphan_directories || [];
    currentUntrackedFiles = data.untracked_files || [];

    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el) el.textContent = val;
    };

    setVal('reconcile-stat-audited', (summary.total_audited || 0).toLocaleString());
    setVal('reconcile-stat-healthy', (summary.healthy || 0).toLocaleString());
    setVal('reconcile-stat-discrepancies', (summary.discrepancies || 0).toLocaleString());

    const countSpan = document.getElementById('reconcile-defect-count');
    if (countSpan) countSpan.textContent = currentDiscrepancies.length;

    if (fixAllBtn) {
      fixAllBtn.style.display = currentDiscrepancies.length > 0 ? 'inline-flex' : 'none';
    }

    if (lastScanEl) {
      if (data.timestamp) {
        const timeStr = formatLocalDateTime(data.timestamp);
        let msg = `Last ${(data.scan_mode || 'fast').toUpperCase()} scan finished at ${timeStr}.`;
        if (summary.hashes_baselined > 0) {
          msg += ` Established & stored ${summary.hashes_baselined} new SHA-256 checksums.`;
        }
        lastScanEl.textContent = msg;
      } else {
        lastScanEl.textContent = 'Audit not run yet. Choose a tool below.';
      }
    }

    if (data.scan_mode === 'reverse' || currentOrphanDirectories.length > 0 || currentUntrackedFiles.length > 0) {
      if (orphanSection) {
        orphanSection.style.display = 'block';
        renderReverseOrphanSection(orphanSection, currentOrphanDirectories, currentUntrackedFiles);
      }
    } else if (orphanSection) {
      orphanSection.style.display = 'none';
    }

    renderReconcileDiscrepancies(currentDiscrepancies);
    return data;
  } catch (err) {
    console.error('Failed to load reconcile view:', err);
    return null;
  }
}

export async function triggerReconcileScan() {
  const btn = document.getElementById('btn-run-reconcile');
  const progressBox = document.getElementById('reconcile-progress-container');
  const logsEl = document.getElementById('reconcile-task-logs');

  if (btn) {
    btn.disabled = true;
    btn.textContent = `Dispatching ${selectedScanMode.toUpperCase()} Task...`;
  }
  if (progressBox) progressBox.style.display = 'block';
  if (logsEl) logsEl.textContent = `Dispatching ${selectedScanMode.toUpperCase()} task to background worker...`;

  try {
    const res = await runReconcileScan(selectedScanMode);
    if (!res.success) {
      throw new Error(res.error || 'Failed to dispatch task');
    }
  } catch (err) {
    alert(`Failed launching reconciliation: ${err.message}`);
    if (progressBox) progressBox.style.display = 'none';
    if (btn) {
      btn.disabled = false;
      btn.textContent = runLabel();
    }
  }
}

export async function triggerOrphanAudit() {
  const btn = document.getElementById('btn-run-orphan-audit');
  const progressBox = document.getElementById('reconcile-progress-container');
  const logsEl = document.getElementById('reconcile-task-logs');

  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Dispatching Reverse Audit...';
  }
  if (progressBox) progressBox.style.display = 'block';
  if (logsEl) logsEl.textContent = 'Dispatching reverse orphan audit to background pipeline worker...';

  try {
    const res = await runReconcileScan('reverse');
    if (!res.success) throw new Error(res.error || 'Failed to dispatch orphan audit');
  } catch (err) {
    alert(`Failed launching orphan audit: ${err.message}`);
    if (progressBox) progressBox.style.display = 'none';
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Run Reverse Orphan Audit';
    }
  }
}

export async function cancelReconcile() {
  try {
    await cancelReconcileScan();
  } catch (err) {
    alert(`Cancel request failed: ${err.message}`);
  }
}

function renderReverseOrphanSection(container, orphans, untracked) {
  if (orphans.length === 0 && untracked.length === 0) {
    render('', container);
    return;
  }

  const htmlParts = [];
  if (orphans.length > 0) {
    htmlParts.push(html`
      <div class="orphan-header-row">
        <div class="orphan-header-title">
          Reverse Audit: Orphan Directories on Disk (${orphans.length})
        </div>
        <button type="button" class="btn-pill btn-action-cancel" @click=${() => window.deleteAllOrphans()}>
          Delete All Orphan Folders (${orphans.length})
        </button>
      </div>
    `);
    htmlParts.push(orphans.map(o => html`
      <div class="card reconcile-card card-orphan">
        <div class="reconcile-card-header">
          <div>
            <div class="model-title">${o.folder_name}</div>
            <div class="font-mono text-dim path-text">${o.directory_path}</div>
          </div>
          <div class="footer-actions">
            <span class="font-mono text-dim font-sm">${formatBytes(o.size_bytes)}</span>
            <button type="button" class="btn-pill btn-action-cancel" @click=${() => window.deleteSingleOrphan(o.directory_path)}>
              Delete Directory
            </button>
          </div>
        </div>
        <div class="defect-detail text-alert">${o.detail}</div>
      </div>
    `));
  }

  if (untracked.length > 0) {
    htmlParts.push(html`
      <div class="orphan-header-row">
        <div class="untracked-header-title">
          Reverse Audit: Untracked Files Inside Valid Models (${untracked.length})
        </div>
        <button type="button" class="btn-pill btn-action-primary" @click=${() => window.ingestAllUntracked()}>
          Auto-Ingest All Untracked Files (${untracked.length})
        </button>
      </div>
    `);
    htmlParts.push(untracked.map(u => html`
      <div class="card reconcile-card card-untracked">
        <div class="reconcile-card-header">
          <div>
            <div class="cell-clickable-title" @click=${() => window.openModelDetailModal(u.model_id)}>
              ${u.model_name} <span class="font-mono text-dim font-xs">(#${u.model_id})</span>
            </div>
            <div class="model-subtitle">
              File: <strong class="text-main">${u.filename}</strong> (${formatBytes(u.size_bytes)})
            </div>
          </div>
          <button type="button" class="btn-pill btn-action-primary" @click=${() => window.ingestSingleUntracked(u.model_id, u.file_path)}>
            Auto-Ingest to DB
          </button>
        </div>
        <div class="defect-detail text-accent">${u.detail}</div>
      </div>
    `));
  }

  render(htmlParts, container);
}

function renderReconcileDiscrepancies(items) {
  const container = document.getElementById('reconcile-results-container');
  if (!container) return;

  if (!items || items.length === 0) {
    render(html`
      <div class="results-placeholder text-accent">
        100% Integrity Confirmed: All audited models have valid directories, expected files, matching sizes, and uncorrupted archive checksums.
      </div>
    `, container);
    return;
  }

  render(items.map(m => {
    const issuesHtml = m.issues.map(iss => html`
      <div class="reconcile-issue-row">
        <div class="issue-top">
          <span class="reconcile-defect-pill">${iss.defect}</span>
          <span class="font-mono text-dim font-xs">${iss.filename || 'Directory Defect'}</span>
        </div>
        <div class="defect-detail text-alert font-sm">
          ${iss.detail}
        </div>
        <div class="font-mono text-dim font-xs">
          Expected: ${iss.expected_path}
        </div>
      </div>
    `);

    return html`
      <div class="card reconcile-card">
        <div class="reconcile-card-header">
          <div>
            <div class="cell-clickable-title" @click=${() => window.openModelDetailModal(m.model_id)}>
              ${m.model_name} <span class="font-mono text-dim font-xs">(#${m.model_id})</span>
            </div>
            <div class="model-subtitle">
              Creator: ${m.creator_name}
            </div>
          </div>
          <button type="button" class="btn-pill btn-action-cancel" @click=${() => window.resetSingleReconcileModel(m.model_id)}>
            Reset for Retry
          </button>
        </div>
        <div class="reconcile-card-body">
          ${issuesHtml}
        </div>
      </div>
    `;
  }), container);
}

export async function resetSingleReconcileModel(modelId) {
  if (!confirm(`Reset Model #${modelId} in database? Its vault record will be cleared and the job set to 'failed' so it can be re-downloaded.`)) return;
  try {
    const res = await resetReconcileModels([modelId]);
    if (res.success) {
      currentDiscrepancies = currentDiscrepancies.filter(d => d.model_id !== modelId);
      renderReconcileDiscrepancies(currentDiscrepancies);
      const countSpan = document.getElementById('reconcile-defect-count');
      if (countSpan) countSpan.textContent = currentDiscrepancies.length;
      const fixAllBtn = document.getElementById('btn-fix-all-reconcile');
      if (fixAllBtn) fixAllBtn.style.display = currentDiscrepancies.length > 0 ? 'inline-flex' : 'none';
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Reset failed: ${err.message}`);
  }
}

export async function resetAllReconcileDiscrepancies() {
  if (!currentDiscrepancies || currentDiscrepancies.length === 0) return;
  const count = currentDiscrepancies.length;
  if (!confirm(`Reset all ${count} discrepant models in the database? Their vault records will be cleared and jobs placed in 'failed' status for batch retry.`)) return;

  const ids = currentDiscrepancies.map(d => d.model_id);
  try {
    const res = await resetReconcileModels(ids);
    if (res.success) {
      currentDiscrepancies = [];
      renderReconcileDiscrepancies([]);
      const countSpan = document.getElementById('reconcile-defect-count');
      if (countSpan) countSpan.textContent = '0';
      const fixAllBtn = document.getElementById('btn-fix-all-reconcile');
      if (fixAllBtn) fixAllBtn.style.display = 'none';
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Reset all failed: ${err.message}`);
  }
}

export async function deleteSingleOrphan(path) {
  if (!confirm(`Move this orphan folder to Pending Deletion? It can be restored for 14 days.\n\n${path}`)) return;
  try {
    const res = await deleteOrphanDirectories([path]);
    if (res.errors?.length) {
      alert(res.errors.join('\n'));
    } else if (res.success) {
      currentOrphanDirectories = currentOrphanDirectories.filter(o => o.directory_path !== path);
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), currentOrphanDirectories, currentUntrackedFiles);
    }
  } catch (err) {
    alert(`Failed deleting directory: ${err.message}`);
  }
}

export async function deleteAllOrphans() {
  if (currentOrphanDirectories.length === 0) return;
  const count = currentOrphanDirectories.length;
  if (!confirm(`Move all ${count} orphan folders to Pending Deletion? They can be restored for 14 days.`)) return;

  const paths = currentOrphanDirectories.map(o => o.directory_path);
  try {
    const res = await deleteOrphanDirectories(paths);
    if (res.errors?.length) {
      alert(`${res.deleted_count} moved. These were refused, so run the audit again:\n\n${res.errors.join('\n')}`);
    } else if (res.success) {
      currentOrphanDirectories = [];
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), [], currentUntrackedFiles);
    }
  } catch (err) {
    alert(`Failed deleting orphan directories: ${err.message}`);
  }
}

export async function ingestSingleUntracked(modelId, filePath) {
  try {
    const res = await ingestUntrackedFiles([{ model_id: modelId, file_path: filePath }]);
    if (res.success) {
      currentUntrackedFiles = currentUntrackedFiles.filter(u => u.file_path !== filePath);
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), currentOrphanDirectories, currentUntrackedFiles);
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Failed ingesting file: ${err.message}`);
  }
}

export async function ingestAllUntracked() {
  if (currentUntrackedFiles.length === 0) return;
  const count = currentUntrackedFiles.length;
  if (!confirm(`Auto-ingest all ${count} untracked files into the database?`)) return;

  const items = currentUntrackedFiles.map(u => ({ model_id: u.model_id, file_path: u.file_path }));
  try {
    const res = await ingestUntrackedFiles(items);
    if (res.success) {
      currentUntrackedFiles = [];
      renderReverseOrphanSection(document.getElementById('reconcile-orphan-section'), currentOrphanDirectories, []);
      window.refreshCurrentView();
    }
  } catch (err) {
    alert(`Failed ingesting untracked files: ${err.message}`);
  }
}

export async function triggerStagingSweep() {
  const container = document.getElementById('reconcile-results-container');
  if (container) {
    render(html`<div class="table-msg">Scanning local scratch staging directory (.work/in_flight & .work/staged)...</div>`, container);
  }
  try {
    const res = await fetchStagingGarbage();
    if (!res.success) throw new Error(res.error || 'Failed scanning staging.');
    currentStagingGarbage = res.items || [];
    renderStagingGarbageResults(res.total_bytes, currentStagingGarbage);
  } catch (err) {
    alert(`Staging scan failed: ${err.message}`);
  }
}

function renderStagingGarbageResults(totalBytes, items) {
  const container = document.getElementById('reconcile-results-container');
  if (!container) return;

  if (items.length === 0) {
    render(html`
      <div class="results-placeholder text-accent">
        Staging Clean: Zero abandoned .part files or orphaned staged model folders found.
      </div>
    `, container);
    return;
  }

  const htmlParts = [html`
    <div class="orphan-header-row">
      <div>
        <span class="findings-title-alert">Staging Garbage Discovered (${items.length} items)</span>
        <span class="font-sm text-dim ml-2">Total Reclaimable: <strong>${formatBytes(totalBytes)}</strong></span>
      </div>
      <button type="button" class="btn-pill btn-action-cancel" @click=${() => window.purgeAllStagingGarbage()}>
        Purge All Staging Garbage (${formatBytes(totalBytes)})
      </button>
    </div>
  `];

  htmlParts.push(items.map(it => html`
    <div class="card reconcile-card card-orphan">
      <div class="reconcile-card-header">
        <div>
          <div class="model-title">
            ${it.type === 'in_flight_part' ? 'In-Flight .part File' : 'Staged Model Folder'}: ${it.name}
          </div>
          <div class="font-mono text-dim path-text">${it.path}</div>
        </div>
        <div class="footer-actions">
          <span class="font-mono text-dim font-sm">${formatBytes(it.size_bytes)}</span>
          <button type="button" class="btn-pill btn-action-cancel" @click=${() => window.purgeSingleStagingGarbage(it.path)}>Purge</button>
        </div>
      </div>
      <div class="defect-detail text-alert">${it.reason}</div>
    </div>
  `));

  render(htmlParts, container);
}

export async function purgeSingleStagingGarbage(path) {
  if (!confirm(`Purge this staging file/directory?\n\n${path}`)) return;
  try {
    const res = await purgeStagingGarbage([path]);
    if (res.success) {
      currentStagingGarbage = currentStagingGarbage.filter(it => it.path !== path);
      const remainingBytes = currentStagingGarbage.reduce((acc, it) => acc + (it.size_bytes || 0), 0);
      renderStagingGarbageResults(remainingBytes, currentStagingGarbage);
    }
  } catch (err) {
    alert(`Failed purging: ${err.message}`);
  }
}

export async function purgeAllStagingGarbage() {
  if (currentStagingGarbage.length === 0) return;
  if (!confirm(`Permanently purge all ${currentStagingGarbage.length} stale staging files/folders?`)) return;
  try {
    const res = await purgeStagingGarbage();
    if (res.success) {
      currentStagingGarbage = [];
      renderStagingGarbageResults(0, []);
    }
  } catch (err) {
    alert(`Failed purging staging: ${err.message}`);
  }
}

function revealDuplicates() {
  const panel = document.getElementById('duplicate-results-panel');
  if (!panel) return;
  panel.hidden = false;
  panel.open = true;
  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

export async function triggerDuplicateScan(includeShared = false) {
  const panel = document.getElementById('duplicate-results-panel');
  if (!panel) return;
  render(html`<summary>Comparing catalog files...</summary>`, panel);
  revealDuplicates();
  try {
    renderDuplicateResults(await fetchDuplicates(includeShared === true));
    loadRunHistory();
  } catch (err) {
    alert(`Duplicate scan failed: ${err.message}`);
  }
}

// Results stay until each pair is acted on, so returning to the page finds them where they were.
/** Shows the saved results, so returning to the page finds them where they were. */
export async function loadDuplicateResults() {
  try {
    const data = await fetchLatestDuplicates();
    if (data.has_results) renderDuplicateResults(data);
  } catch (err) {
    console.warn('Duplicate results unavailable:', err);
  }
}

async function decidePair(pair, decision) {
  try {
    await postDuplicateDecision(pair.first.file_id, pair.second.file_id, decision);
    await loadDuplicateResults();
  } catch (err) {
    alert(`Could not save that: ${err.message}`);
  }
}

async function deleteSide(side) {
  const ask = side.path
    ? `Move ${side.filename} to .Pending-Deletion and take it out of the catalog? It can be purged after 14 days.`
    : `Take ${side.filename} out of the catalog? It isn't downloaded, so no file moves.`;
  if (!confirm(ask)) return;
  try {
    await deleteDuplicateFile(side.file_id);
    await loadDuplicateResults();
  } catch (err) {
    alert(`Could not delete it: ${err.message}`);
  }
}

function duplicateSide(side) {
  return html`
    <div class="duplicate-side" data-file=${side.file_id}>
      <strong class="font-sm">${side.filename}</strong>
      <span class="font-xs text-dim">${side.model_name}</span>
      <span class="font-mono font-xs text-dim location-path">${side.path || 'Not downloaded'}</span>
      <span class="font-xs text-dim">${formatBytes(side.size_bytes || 0)}</span>
      <button type="button" class="btn-pill btn-action-cancel font-xs duplicate-delete" ?disabled=${Boolean(side.delete_blocked)}
        title=${side.delete_blocked || ''} @click=${() => deleteSide(side)}>Delete This One</button>
      ${side.delete_blocked ? html`<span class="font-xs text-dim duplicate-blocked">${side.delete_blocked}</span>` : nothing}
    </div>
  `;
}

function renderDuplicateResults(data) {
  const panel = document.getElementById('duplicate-results-panel');
  if (!panel) return;
  const pairs = data.pairs || [];
  const includeShared = Boolean(data.include_shared);
  const models = (data.models || []).filter((group) => group.kind === 'duplicate');
  const hidden = (data.summary?.pairs || 0) - pairs.length;
  panel.hidden = false;
  // Open the first time only, so an action doesn't reopen a section someone collapsed.
  if (!panel.dataset.shown) {
    panel.dataset.shown = '1';
    panel.open = pairs.length > 0;
  }
  render(html`
    <summary>${pairs.length ? `${pairs.length} pairs to review` : 'No pairs left to review'}</summary>
    <div class="reconcile-results-body" id="duplicate-results">
      <div class="reconcile-results-bar">
        <span class="font-sm text-dim">Keep and Ignore both stop a pair coming back. Delete moves one file to .Pending-Deletion.</span>
        <button type="button" class="btn-pill font-xs" @click=${() => triggerDuplicateScan(!includeShared)}>
          ${includeShared ? 'Hide shared bases and supports' : 'Include shared bases and supports'}
        </button>
      </div>
      ${models.map((group) => html`
        <div class="font-sm duplicate-row"><span class="status-badge status-failed font-xs">DUPLICATE MODEL</span>
          ${group.models.map((model) => model.model_name).join(' / ')}, ${group.shared_files} files</div>
      `)}
      ${pairs.map((pair) => html`
        <div class="duplicate-pair duplicate-row">
          <div class="duplicate-pair-head">
            <span class="status-badge ${pair.kind === 'exact' ? 'status-completed' : 'status-queued'} font-xs">${pair.kind === 'exact' ? 'EXACT' : 'POSSIBLE'}</span>
            <div class="flex gap-2">
              <button type="button" class="btn-pill font-xs duplicate-keep" @click=${() => decidePair(pair, 'keep')}>Keep Both</button>
              <button type="button" class="btn-pill font-xs duplicate-ignore" @click=${() => decidePair(pair, 'ignore')}>Ignore</button>
            </div>
          </div>
          <div class="duplicate-sides">${duplicateSide(pair.first)}${duplicateSide(pair.second)}</div>
        </div>
      `)}
      ${hidden > 0 ? html`<p class="font-sm text-dim">${hidden} more after these. Run the check again once these are done.</p>` : nothing}
    </div>
  `, panel);
}

export async function triggerPathDriftScan() {
  const container = document.getElementById('reconcile-results-container');
  if (container) {
    render(html`<div class="table-msg">Scanning for legacy naming schemes and path drift across vault...</div>`, container);
  }
  try {
    const res = await fetchPathDrift();
    if (!res.success) throw new Error(res.error || 'Failed scanning path drift.');
    currentPathDrifts = res.items || [];
    renderPathDriftResults(currentPathDrifts);
  } catch (err) {
    alert(`Path drift scan failed: ${err.message}`);
  }
}

function renderPathDriftResults(items) {
  const container = document.getElementById('reconcile-results-container');
  if (!container) return;

  if (items.length === 0) {
    render(html`
      <div class="results-placeholder text-accent">
        Zero Path Drift: All archived models conform to canonical naming standards.
      </div>
    `, container);
    return;
  }

  const htmlParts = [html`
    <div class="orphan-header-row">
      <div>
        <span class="findings-title-drift">Legacy Path Drift Detected (${items.length} models)</span>
        <div class="font-sm text-dim">Folders with brackets, unescaped whitespace, or obsolete creator paths.</div>
      </div>
      <button type="button" class="btn-pill btn-action-primary" @click=${() => window.realignAllPaths()}>
        Realign All Models (${items.length})
      </button>
    </div>
  `];

  htmlParts.push(items.map(it => html`
    <div class="card reconcile-card card-drift">
      <div class="reconcile-card-header">
        <div>
          <div class="cell-clickable-title" @click=${() => window.openModelDetailModal(it.model_id)}>
            ${it.model_name} <span class="font-mono text-dim font-xs">(#${it.model_id})</span>
          </div>
          <div class="model-subtitle font-mono">${it.reasons.join(' \u2022 ')}</div>
        </div>
        <button type="button" class="btn-pill btn-action-primary" @click=${() => window.realignSinglePath(it.model_id)}>Realign Path</button>
      </div>
      <div>
        <div class="path-text-current font-mono"><strong>Current:</strong> ${it.current_path}</div>
        <div class="path-text-canonical font-mono"><strong>Canonical:</strong> ${it.canonical_path}</div>
      </div>
    </div>
  `));

  render(htmlParts, container);
}

// Moves run as a job, so a folder list stays on screen until a fresh scan shows the result.
async function realignPaths(ids) {
  const preview = await realignModelPaths(ids);
  const moves = preview.would_move.map(item => `${item.current_path}\n  to ${item.canonical_path}`);
  if (preview.errors.length) alert(preview.errors.join('\n'));
  if (moves.length === 0) return;
  const shown = moves.slice(0, 5).join('\n\n');
  const more = moves.length > 5 ? `\n\nand ${moves.length - 5} more` : '';
  if (!confirm(`Move ${moves.length} folders on the vault and update the catalog?\n\n${shown}${more}`)) return;
  await realignModelPaths(ids, true);
  alert('Folder moves started. Follow them in Pipeline Tasks, then run the scan again.');
  if (typeof window.checkHealth === 'function') window.checkHealth();
}

export async function realignSinglePath(modelId) {
  try {
    await realignPaths([modelId]);
  } catch (err) {
    alert(`Realign failed: ${err.message}`);
  }
}

export async function realignAllPaths() {
  if (currentPathDrifts.length === 0) return;
  try {
    await realignPaths(currentPathDrifts.map(it => it.model_id));
  } catch (err) {
    alert(`Realign all failed: ${err.message}`);
  }
}

export async function triggerArchiveInspectionSweep() {
  const btn = document.getElementById('btn-backfill-archives');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Launching Archive Peek...';
  }

  try {
    const res = await triggerArchivePeekBackfill();
    if (!res.success) throw new Error(res.error || 'Failed to dispatch archive peek backfill');
    alert('Archive peek backfill dispatched to background pipeline worker. You can monitor progress in Pipeline Tasks.');
    if (typeof window.checkHealth === 'function') {
      window.checkHealth();
    }
  } catch (err) {
    alert(`Archive peek backfill failed: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Backfill Archive Inspection';
    }
  }
}

export { resetAllReconcileDiscrepancies as resetAllReconcileDiscrepencies };


export async function addSelectedToStash() {
  if (selectedPendingIds.size === 0) {
    alert('Please select at least one model to add to stash.');
    return;
  }
  const ids = Array.from(selectedPendingIds);

  if (typeof window.openAddToStashModal === 'function') {
    window.openAddToStashModal({
      item_type: 'bulk_models',
      model_ids: ids,
      title: `${ids.length} Selected Model${ids.length === 1 ? '' : 's'}`
    }, () => {
      // Clear selection upon successful stash addition
      selectedPendingIds.clear();
      updateSelectedCount();
      const selectAll = document.getElementById('select-all-pending');
      if (selectAll) selectAll.checked = false;
      document.querySelectorAll('.pending-row-chk').forEach(chk => { chk.checked = false; });
    });
  }
}

// Bind to window for HTML onclick handler
window.addSelectedToStash = addSelectedToStash;


export async function triggerCatalogPreviewSweep() {
  const btn = document.getElementById('btn-backfill-previews');
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Caching Previews...';
  }

  try {
    const res = await triggerCatalogPreviewBackfill();
    if (!res.success) throw new Error(res.error || 'Failed to dispatch catalog preview backfill');
    alert('Catalog preview cache sweep dispatched to background pipeline worker. Monitor progress in Pipeline Tasks.');
    if (typeof window.checkHealth === 'function') {
      window.checkHealth();
    }
  } catch (err) {
    alert(`Preview cache failed: ${err.message}`);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = 'Backfill Model Previews';
    }
  }
}

window.triggerCatalogPreviewSweep = triggerCatalogPreviewSweep;

window.loadRunHistory = loadRunHistory;
window.loadDuplicateResults = loadDuplicateResults;
