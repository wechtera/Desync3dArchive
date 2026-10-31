/**
 * ui/static/js/views/settings.js - Settings workspace controller.
 * Manages storage configuration, directory usage metrics, in-place external libraries,
 * worker tuning, diagnostic logging, log pruning, and MyMiniFactory authentication.
 */

import { formatBytes, parseServerTime } from '../utils.js';
import { appendToBody, html, imageFallback, nothing, render, safeUrl, stopPropagation } from '../template.js';
import {
  fetchExternalLibraries,
  addExternalLibrary,
  deleteExternalLibrary,
  scanExternalLibrary,
  fetchLibraryPreview,
  approveLibraryPreview,
  fetchImageQueue,
  postImageQueue,
  fetchInactive
} from '../api.js';

let currentBrowsePath = '';
let targetInputId = '';

function showToast(message) {
  const toast = document.getElementById('settings-toast');
  if (toast) {
    toast.textContent = message;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 3500);
  } else {
    console.log('[Toast]', message);
  }
}

/**
 * Parses a Cookie-Editor array or key-value object and extracts cf_clearance and PHPSESSID.
 */
function extractTokensFromCookiesJson(jsonStr) {
  if (!jsonStr || typeof jsonStr !== 'string') return { cf: '', php: '' };
  try {
    const parsed = JSON.parse(jsonStr.trim());
    let cf = '';
    let php = '';
    if (Array.isArray(parsed)) {
      for (const item of parsed) {
        if (!item || typeof item !== 'object') continue;
        const name = (item.name || '').trim();
        const value = (item.value || '').trim();
        if (name === 'cf_clearance') cf = value;
        if (name === 'PHPSESSID' || name.toLowerCase() === 'phpsessid') php = value;
      }
    } else if (typeof parsed === 'object') {
      for (const [k, v] of Object.entries(parsed)) {
        if (k === 'cf_clearance') cf = String(v).trim();
        if (k === 'PHPSESSID' || k.toLowerCase() === 'phpsessid') php = String(v).trim();
      }
    }
    return { cf, php };
  } catch (e) {
    return { cf: '', php: '' };
  }
}

function bindCookieJsonSync() {
  const rawTextarea = document.getElementById('setting-cookies-raw');
  if (rawTextarea && !rawTextarea.dataset.syncBound) {
    rawTextarea.dataset.syncBound = 'true';
    const syncHandler = () => {
      const { cf, php } = extractTokensFromCookiesJson(rawTextarea.value);
      const cfInput = document.getElementById('setting-cookie-cf');
      const phpInput = document.getElementById('setting-cookie-php');
      if (cf && cfInput) cfInput.value = cf;
      if (php && phpInput) phpInput.value = php;
    };
    rawTextarea.addEventListener('input', syncHandler);
    rawTextarea.addEventListener('paste', () => setTimeout(syncHandler, 50));
  }
}

export async function loadSettingsView() {
  await Promise.all([
    loadSettings(),
    loadDirectoryUsage(),
    loadExternalLibraries(),
    loadAuthStatus(),
    loadSessionStatus(),
    loadLoggingSettings()
  ]);
  bindCookieJsonSync();
}

const EXTRA_COOKIE_FIELDS = [
  ['setting-cookie-rememberme', 'cookie_rememberme'],
  ['setting-cookie-cf-bm', 'cookie_cf_bm'],
  ['setting-cookie-cfuvid', 'cookie_cfuvid'],
  ['setting-cookie-cflb', 'cookie_cflb'],
];

export async function loadSettings() {
  try {
    const res = await fetch('/api/settings');
    const data = await res.json();

    const setVal = (id, val) => {
      const el = document.getElementById(id);
      if (el && val !== undefined && val !== null) el.value = val;
    };

    setVal('setting-stash-export-path', data.stash_download_path || '~/Downloads/DESYNC_Print_Queue');
    setVal('setting-root-path', data.download_root_path || '');
    setVal('setting-work-path', data.download_work_path || '');
    setVal('setting-concurrency', data.worker_concurrency || 2);
    setVal('setting-offloader-concurrency', data.offloader_concurrency || 1);
    setVal('setting-batch-size', data.batch_size || 5);
    setVal('setting-cooldown', data.batch_cooldown_seconds || 3.0);
    setVal('setting-stream-chunk', data.stream_chunk_size_mb || 1.0);
    setVal('setting-offload-chunk', data.offload_chunk_size_mb || 4.0);

    // The server never sends secrets, so these fields only show whether a value is set.
    const secrets = data.secrets_configured || {};
    const setSecret = (id, configured) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (el.dataset.emptyPlaceholder === undefined) el.dataset.emptyPlaceholder = el.placeholder;
      el.value = '';
      el.placeholder = configured ? 'Configured. Leave blank to keep it.' : el.dataset.emptyPlaceholder;
    };

    setSecret('setting-cookies-raw', secrets.cookies_json);
    setSecret('setting-cookie-cf', secrets.cookie_cf_clearance || secrets.cf_clearance);
    setSecret('setting-cookie-php', secrets.cookie_phpsessid || secrets.PHPSESSID);
    for (const [id, key] of EXTRA_COOKIE_FIELDS) setSecret(id, secrets[key]);

    setVal('setting-auth-client-id', data.mmf_client_id || '');

    const headlessEl = document.getElementById('setting-scraper-headless');
    if (headlessEl) headlessEl.checked = data.scraper_headless !== '0';

    const backupEl = document.getElementById('backup-status');
    if (backupEl) {
      backupEl.textContent = data.last_backup_at
        ? `Last database backup: ${data.last_backup_at} (${data.last_backup_result})`
        : 'No database backup has run yet.';
    }
  } catch (err) {
    console.error('Failed to load settings:', err);
  }
}

export async function loadDirectoryUsage() {
  const container = document.getElementById('critical-directories-table-container');
  if (!container) return;

  render(html`<div class="table-msg">Scanning directory footprints...</div>`, container);

  try {
    const res = await fetch('/api/settings/directories');
    const data = await res.json();
    const directories = data.directories || data.items || [];

    if (!directories.length) {
      render(html`<div class="table-msg">No directory metrics available.</div>`, container);
      return;
    }

    render(html`
      <table class="table-compact">
        <thead>
          <tr>
            <th>Directory Scope</th>
            <th>Resolved Server Path</th>
            <th>Item Count</th>
            <th>Disk Footprint</th>
          </tr>
        </thead>
        <tbody>
          ${directories.map(d => html`
            <tr>
              <td><strong>${d.name}</strong></td>
              <td class="font-mono font-xs text-dim">${d.path || '-'}</td>
              <td>${d.file_count !== undefined ? d.file_count.toLocaleString() : '-'}</td>
              <td class="font-mono">${d.size_bytes !== null && d.size_bytes !== undefined ? formatBytes(d.size_bytes) : html`<span class="text-dim">N/A (Database)</span>`}</td>
            </tr>
          `)}
        </tbody>
      </table>
    `, container);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed scanning directories: ${err.message}</div>`, container);
  }
}

// -----------------------------------------------------------------------------
// In-Place External Libraries Management (Phase 9)
// -----------------------------------------------------------------------------

export function switchSettingsTab(tab) {
  if (tab === 'inactive') loadInactive();
  if (tab === 'libraries') loadImageQueue().catch(() => {});
  // Panels stay in the page while hidden, so a save still reads every field.
  document.querySelectorAll('[data-settings-tab]').forEach(panel => {
    panel.hidden = panel.dataset.settingsTab !== tab;
  });
  document.querySelectorAll('[data-settings-tab-btn]').forEach(btn => {
    const selected = btn.dataset.settingsTabBtn === tab;
    btn.classList.toggle('active', selected);
    btn.setAttribute('aria-selected', selected ? 'true' : 'false');
  });
}

export async function loadExternalLibraries() {
  let panel = document.getElementById('external-libraries-panel');

  // Auto-scaffold panel into settings-grid if not already in index.html
  if (!panel) {
    const grid = document.querySelector('.settings-grid');
    if (grid) {
      panel = document.createElement('div');
      panel.id = 'external-libraries-panel';
      panel.className = 'settings-panel';
      panel.dataset.settingsTab = 'libraries';
      panel.hidden = !document.querySelector('[data-settings-tab-btn="libraries"].active');
      if (grid.children.length > 1) {
        grid.insertBefore(panel, grid.children[1]);
      } else {
        grid.appendChild(panel);
      }
    }
  }

  if (!panel) return;

  render(html`
    <div class="settings-panel-header">
      <div>
        <h2 class="settings-panel-title">In-Place External Libraries</h2>
        <div class="settings-panel-subtitle">
          Manage local &amp; network storage collections (e.g., Loot Studios) indexed zero-copy in strictly read-only mode.
        </div>
      </div>
      <div class="flex gap-2">
        <button type="button" class="btn-pill" @click=${() => window.loadExternalLibraries()}>Refresh Libraries</button>
      </div>
    </div>
    <div id="external-libraries-table-container">
      <div class="table-msg">Loading registered libraries...</div>
    </div>
    <div id="library-preview-container"></div>
    <div id="library-image-queue" class="mt-3"></div>
    <div class="mt-4 pt-3" style="border-top: 1px solid #262635;">
      <h3 class="font-bold font-md mb-2 text-accent">Register New In-Place Library</h3>
      <div class="settings-form-row two-col">
        <div class="settings-field-group">
          <label for="new-lib-path">Top-Level Storage Directory Path *</label>
          <div class="settings-input-with-button">
            <input type="text" id="new-lib-path" placeholder="/Volumes/STL/Loot Studios" spellcheck="false">
            <button type="button" class="btn-pill" @click=${() => window.openDirectoryPicker('new-lib-path')}>Browse</button>
          </div>
          <span class="form-help-text">Direct path to library root on NAS or local drive (strictly read-only).</span>
        </div>
        <div class="settings-field-group">
          <label for="new-lib-name">Display Name (Optional)</label>
          <input type="text" id="new-lib-name" placeholder="Defaults to folder name" spellcheck="false">
          <span class="form-help-text">Label shown in catalog and monthly releases.</span>
        </div>
      </div>
      <div class="settings-form-row two-col mt-2">
        <div class="settings-field-group">
          <label for="new-lib-creator">Default Creator / Studio (Optional)</label>
          <input type="text" id="new-lib-creator" placeholder="e.g. Loot Studios" spellcheck="false">
          <span class="form-help-text">Assigned to indexed models if not specified in release subfolders.</span>
        </div>
        <div class="settings-field-group flex items-end">
          <button type="button" class="btn-pill btn-action-primary" @click=${() => window.handleAddExternalLibrary()}>
            Register In-Place Library
          </button>
        </div>
      </div>
    </div>
  `, panel);

  const tableContainer = document.getElementById('external-libraries-table-container');
  try {
    const res = await fetchExternalLibraries();
    const libs = res.libraries || [];

    if (!libs.length) {
      render(html`
        <div class="table-msg text-dim py-4">
          No external libraries registered. Register a directory above to index external collections without moving files.
        </div>
      `, tableContainer);
      return;
    }

    render(html`
      <table class="table-compact">
        <thead>
          <tr>
            <th>Name / Studio</th>
            <th>Storage Path</th>
            <th>Disk Footprint</th>
            <th>Models</th>
            <th>Total Files</th>
            <th>Last Scanned</th>
            <th style="width: 300px;">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${libs.map(lib => {
            const lastScan = lib.last_scanned_at
              ? parseServerTime(lib.last_scanned_at)?.toLocaleString() || '-'
              : html`<span class="text-dim">Never Scanned</span>`;
            const existsBadge = lib.exists
              ? ''
              : html`<span class="status-badge status-danger font-xs ml-1" title="Directory path not currently mounted">Unmounted</span>`;

            return html`
              <tr>
                <td>
                  <strong>${lib.name}</strong>
                  <div class="text-dim font-xs">${lib.default_creator || lib.name}</div>
                </td>
                <td class="font-mono font-xs text-dim">
                  ${lib.path} ${existsBadge}
                </td>
                <td class="font-mono">${lib.disk_formatted || '0 B'}</td>
                <td class="font-mono">${(lib.total_models || 0).toLocaleString()}</td>
                <td class="font-mono">${(lib.total_files || 0).toLocaleString()}</td>
                <td class="font-sm">${lastScan}</td>
                <td>
                  <div class="flex gap-1">
                    <button type="button" class="btn-pill font-xs btn-action-primary" @click=${() => startLibraryPass(lib, 'preview')}
                      title="Finds new folders and shows them for approval. Adds nothing.">
                      Preview New Folders
                    </button>
                    <button type="button" class="btn-pill font-xs" @click=${() => startLibraryPass(lib, 'full')}
                      title="Checks known models against storage. Never adds new folders.">
                      Full Rescan
                    </button>
                    <button type="button" class="btn-pill font-xs btn-action-cancel" @click=${() => window.handleDeleteExternalLibrary(lib.id, lib.name)} title="Unlink library from database (preserves source NAS files)">
                      Remove
                    </button>
                  </div>
                </td>
              </tr>
            `;
          })}
        </tbody>
      </table>
    `, tableContainer);
    await Promise.all([loadLibraryPreview(libs[0]), loadImageQueue()]);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed loading external libraries: ${err.message}</div>`, tableContainer);
  }
}

export async function handleAddExternalLibrary(event) {
  if (event) event.preventDefault();
  const pathInput = document.getElementById('new-lib-path');
  const nameInput = document.getElementById('new-lib-name');
  const creatorInput = document.getElementById('new-lib-creator');

  const path = pathInput ? pathInput.value.trim() : '';
  const name = nameInput ? nameInput.value.trim() : '';
  const default_creator = creatorInput ? creatorInput.value.trim() : '';

  if (!path) {
    alert('Please specify a directory path for the in-place library.');
    return;
  }

  try {
    const res = await addExternalLibrary({ path, name, default_creator });
    if (res.success) {
      showToast(res.message || `In-place library '${res.library.name}' registered.`);
      if (pathInput) pathInput.value = '';
      if (nameInput) nameInput.value = '';
      if (creatorInput) creatorInput.value = '';
      await loadExternalLibraries();
    } else {
      alert(`Failed to add library: ${res.error}`);
    }
  } catch (err) {
    alert(`Registration error: ${err.message}`);
  }
}

export async function handleScanExternalLibrary(libraryId, libraryName) {
  try {
    const res = await scanExternalLibrary(libraryId);
    if (res.success) {
      showToast(`Scan started for '${libraryName}' (Task #${res.task_id}).`);
      if (window.refreshTasksStatus) {
        window.refreshTasksStatus();
      }
    } else {
      alert(`Failed to start scan: ${res.error}`);
    }
  } catch (err) {
    alert(`Scan error: ${err.message}`);
  }
}

async function startLibraryPass(lib, mode) {
  try {
    const res = await scanExternalLibrary(lib.id, mode);
    showToast(`Started for '${lib.name}' (Task #${res.task_id}). Follow it on the Tasks tab.`);
    window.checkHealth?.();
  } catch (err) {
    alert(`Could not start: ${err.message}`);
  }
}

async function approveAndAdd(lib) {
  const skipped = [...document.querySelectorAll('.preview-release-check')]
    .filter(box => !box.checked)
    .map(box => box.dataset.release);
  try {
    const res = await approveLibraryPreview(lib.id, skipped);
    showToast(`Approved ${res.approved} folders.`);
    await startLibraryPass(lib, 'add');
    await loadLibraryPreview(lib);
  } catch (err) {
    alert(`Approval failed: ${err.message}`);
  }
}

function previewFolderList(label, folders) {
  if (!folders.length) return nothing;
  return html`<div class="text-dim font-xs mt-1">${label}: ${folders.join(', ')}</div>`;
}

async function loadLibraryPreview(lib) {
  const container = document.getElementById('library-preview-container');
  if (!container || !lib) return;
  const data = await fetchLibraryPreview(lib.id);
  const candidates = data.candidates || {};
  const waiting = candidates.proposed || 0;
  const approved = candidates.approved || 0;
  const report = data.report;
  if (!report && !approved) {
    render(nothing, container);
    return;
  }

  const releases = (report?.releases || []).filter(r => r.new.length || r.overlaps.length || r.empty.length || r.outside_rules);
  render(html`
    <div class="mt-4 pt-3" style="border-top: 1px solid #262635;">
      <h3 class="font-bold font-md mb-2 text-accent">Preview for ${lib.name}</h3>
      ${report ? html`
        <p class="text-dim font-sm" id="library-preview-summary">
          ${report.totals.new} new, ${report.totals.known} already known, ${report.totals.overlaps} overlapping,
          ${report.totals.empty} empty, ${report.totals.outside_rules} treated as one model.
          Overlapping and empty folders are never added.
        </p>
        <table class="table-compact">
          <thead><tr><th style="width: 60px;">Add</th><th>Release</th><th>New</th><th>Known</th><th>Needs a look</th></tr></thead>
          <tbody>
            ${releases.map(r => html`
              <tr>
                <td>${r.new.length ? html`<input type="checkbox" class="preview-release-check" data-release=${r.name} ?checked=${!r.outside_rules}>` : nothing}</td>
                <td>${r.name}</td>
                <td class="font-mono">${r.new.length}</td>
                <td class="font-mono">${r.known}</td>
                <td>
                  ${r.outside_rules ? html`<span class="status-badge status-failed font-xs">One model</span>` : nothing}
                  ${previewFolderList('Overlaps', r.overlaps)}
                  ${previewFolderList('Empty', r.empty)}
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      ` : nothing}
      <div class="flex gap-2 mt-2">
        ${waiting ? html`<button type="button" class="btn-pill btn-action-primary" id="btn-approve-preview" @click=${() => approveAndAdd(lib)}>Approve and Add ${waiting} Folders</button>` : nothing}
        ${approved ? html`<button type="button" class="btn-pill" @click=${() => startLibraryPass(lib, 'add')}>Resume Adding ${approved} Folders</button>` : nothing}
      </div>
    </div>
  `, container);
}

const IMAGE_STATES = {
  paused: 'Paused',
  waiting_for_inventory: 'Waiting for the library scan to finish',
  storage_offline: 'Waiting for storage to come back',
  blender_unavailable: 'Waiting for Blender',
  idle: 'Nothing to do',
  working: 'Working',
  stopped: 'Not running',
};

async function changeImageQueue(action) {
  try {
    await postImageQueue(action);
    await loadImageQueue();
  } catch (err) {
    alert(`Image queue: ${err.message}`);
  }
}

export async function loadImageQueue() {
  const container = document.getElementById('library-image-queue');
  if (!container) return;
  const data = await fetchImageQueue();
  const counts = data.counts;
  render(html`
    <div class="mt-4 pt-3" style="border-top: 1px solid #262635;">
      <h3 class="font-bold font-md mb-2 text-accent">Background Images</h3>
      <p class="text-dim font-sm" id="image-queue-summary">
        ${IMAGE_STATES[data.state] || data.state}. ${counts.pending} waiting, ${counts.done} done,
        ${counts.failed} failed, ${counts.no_source} with nothing to render.
      </p>
      <div class="flex gap-2 mt-2">
        <button type="button" class="btn-pill" id="btn-image-queue-toggle" @click=${() => changeImageQueue(data.paused ? 'resume' : 'pause')}>${data.paused ? 'Resume' : 'Pause'}</button>
        ${counts.failed ? html`<button type="button" class="btn-pill" @click=${() => changeImageQueue('retry')}>Retry ${counts.failed} Failed</button>` : nothing}
      </div>
    </div>
  `, container);
}

export async function loadInactive() {
  const container = document.getElementById('inactive-table-container');
  if (!container) return;
  const filterEl = document.getElementById('inactive-filter');
  const history = document.getElementById('inactive-history')?.checked;
  const [process, reason] = (filterEl?.value || '|').split('|');
  try {
    const data = await fetchInactive({ process, reason, include_resolved: history ? '1' : '' });
    if (filterEl) {
      const selected = filterEl.value;
      render(html`
        <option value="">Everything open</option>
        ${data.open_counts.map(c => html`<option value="${c.process}|${c.reason}">${c.process}: ${c.reason} (${c.count})</option>`)}
      `, filterEl);
      filterEl.value = selected;
    }
    render(data.items.length === 0
      ? html`<div class="table-msg text-dim">Nothing here.</div>`
      : html`
        <table class="table-compact">
          <thead><tr><th>Item</th><th>Type</th><th>Process</th><th>Reason</th><th>Detail</th><th>When</th><th>Resolved</th></tr></thead>
          <tbody>
            ${data.items.map(item => html`
              <tr>
                <td>${item.item_name || `#${item.item_id}`}</td>
                <td>${item.item_type}</td>
                <td class="font-mono font-xs">${item.process}</td>
                <td class="font-mono font-xs">${item.reason}</td>
                <td class="font-xs text-dim">${item.detail || '-'}</td>
                <td class="font-sm">${parseServerTime(item.created_at)?.toLocaleString() || '-'}</td>
                <td class="font-sm">${item.resolved_at ? parseServerTime(item.resolved_at)?.toLocaleString() : '-'}</td>
              </tr>
            `)}
          </tbody>
        </table>
      `, container);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed loading inactive items: ${err.message}</div>`, container);
  }
}

export async function handleDeleteExternalLibrary(libraryId, libraryName) {
  if (!confirm(`Are you sure you want to unlink '${libraryName}'?\n\nNOTE: No physical files or directories on your storage will be touched or deleted.`)) {
    return;
  }
  try {
    const res = await deleteExternalLibrary(libraryId);
    if (res.success) {
      showToast(res.message || `Library '${libraryName}' unlinked.`);
      await loadExternalLibraries();
    } else {
      alert(`Failed to remove library: ${res.error}`);
    }
  } catch (err) {
    alert(`Remove error: ${err.message}`);
  }
}

// -----------------------------------------------------------------------------
// Native MMF Authentication & Diagnostics
// -----------------------------------------------------------------------------

export async function loadAuthStatus() {
  try {
    const res = await fetch('/api/auth/status');
    const data = await res.json();

    const badge = document.getElementById('auth-status-badge');
    const diagBox = document.getElementById('auth-diagnostics');
    const btnConnect = document.getElementById('btn-connect-auth');
    const btnTest = document.getElementById('btn-test-auth');
    const btnDisconnect = document.getElementById('btn-disconnect-auth');

    if (!badge) return;

    if (data.connected) {
      badge.textContent = `CONNECTED AS @${(data.username || 'USER').toUpperCase()}`;
      badge.className = 'status-badge status-completed font-xs';

      if (diagBox) diagBox.style.display = 'grid';
      const diagUser = document.getElementById('diag-auth-user');
      const diagExpiry = document.getElementById('diag-auth-expiry');
      const diagStatus = document.getElementById('diag-auth-status');

      if (diagUser) diagUser.textContent = data.username ? `@${data.username} (ID: ${data.user_id})` : '-';
      if (diagExpiry) {
        if (data.expires_at) {
          const expDate = new Date(data.expires_at * 1000);
          diagExpiry.textContent = expDate.toLocaleString();
        } else {
          diagExpiry.textContent = 'Indefinite / Permanent';
        }
      }
      if (diagStatus) diagStatus.textContent = 'OAuth 2.0 Bearer Authenticated';

      if (btnConnect) btnConnect.style.display = 'none';
      if (btnTest) btnTest.style.display = 'inline-block';
      if (btnDisconnect) btnDisconnect.style.display = 'inline-block';
    } else {
      badge.textContent = data.configured ? 'DISCONNECTED / EXPIRED' : 'NOT CONFIGURED';
      badge.className = 'status-badge status-not_downloaded font-xs';

      if (diagBox) diagBox.style.display = 'none';
      if (btnConnect) btnConnect.style.display = 'inline-block';
      if (btnTest) btnTest.style.display = 'none';
      if (btnDisconnect) btnDisconnect.style.display = 'none';
    }
  } catch (err) {
    console.error('Failed to load auth status:', err);
  }
}

export async function loadLoggingSettings() {
  try {
    const res = await fetch('/api/settings/logging');
    const data = await res.json();
    if (!data.success) return;

    const config = data.config || {};
    const levelSelect = document.getElementById('setting-log-level');
    if (levelSelect && config.level) {
      levelSelect.value = config.level;
    }

    const activeLayers = new Set(config.layers || []);
    ['http', 'storage', 'archive', 'sql'].forEach(layer => {
      const chk = document.getElementById(`layer-${layer}`);
      if (chk) {
        chk.checked = activeLayers.has(layer);
      }
    });
  } catch (err) {
    console.error('Failed to load logging settings:', err);
  }
}

export async function saveLoggingSettings() {
  const levelSelect = document.getElementById('setting-log-level');
  const level = levelSelect ? levelSelect.value : 'INFO';

  const layers = [];
  ['http', 'storage', 'archive', 'sql'].forEach(layer => {
    const chk = document.getElementById(`layer-${layer}`);
    if (chk && chk.checked) {
      layers.push(layer);
    }
  });

  try {
    const res = await fetch('/api/settings/logging', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level, layers })
    });
    const data = await res.json();
    if (data.success) {
      showToast('Logging configuration applied.');
    } else {
      alert(`Failed to save logging configuration: ${data.error}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

export async function purgeLogs(days = 14) {
  if (!confirm(`Are you sure you want to delete rotated log files and audit records older than ${days} days?`)) {
    return;
  }
  try {
    const res = await fetch('/api/settings/logs/purge', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ days })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message);
      loadDirectoryUsage();
    } else {
      alert(`Log purge failed: ${data.error}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

export async function saveSettingsForm() {
  const getVal = id => { const el = document.getElementById(id); return el ? el.value.trim() : ''; };

  let cfVal = getVal('setting-cookie-cf');
  let phpVal = getVal('setting-cookie-php');
  const rawJson = getVal('setting-cookies-raw');

  if ((!cfVal || !phpVal) && rawJson) {
    const extracted = extractTokensFromCookiesJson(rawJson);
    if (!cfVal) cfVal = extracted.cf;
    if (!phpVal) phpVal = extracted.php;
  }

  let cleanJson = rawJson;
  if (rawJson) {
    try {
      const parsed = JSON.parse(rawJson);
      cleanJson = JSON.stringify(parsed);
    } catch (e) {
      return alert('Raw Browser Cookies JSON is not valid JSON format.');
    }
  }

  const cfEl = document.getElementById('setting-cookie-cf');
  if (cfEl && cfVal) cfEl.value = cfVal;
  const phpEl = document.getElementById('setting-cookie-php');
  if (phpEl && phpVal) phpEl.value = phpVal;

  const payload = {
    download_root_path: getVal('setting-root-path'),
    download_work_path: getVal('setting-work-path'),
    stash_download_path: getVal('setting-stash-export-path') || '~/Downloads/DESYNC_Print_Queue',
    worker_concurrency: getVal('setting-concurrency') || 2,
    offloader_concurrency: getVal('setting-offloader-concurrency') || 1,
    batch_size: getVal('setting-batch-size') || 5,
    batch_cooldown_seconds: getVal('setting-cooldown') || 3.0,
    stream_chunk_size_mb: getVal('setting-stream-chunk') || 1.0,
    offload_chunk_size_mb: getVal('setting-offload-chunk') || 4.0,
    scraper_headless: document.getElementById('setting-scraper-headless')?.checked === false ? '0' : '1',
    cookie_cf_clearance: cfVal,
    cookie_phpsessid: phpVal,
    ...Object.fromEntries(EXTRA_COOKIE_FIELDS.map(([id, key]) => [key, getVal(id)])),
    cookies_json: cleanJson,
  };

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.success || res.ok) {
      showToast('Settings saved successfully.');
      loadSettings();
      loadDirectoryUsage();
    } else {
      alert(`Failed saving settings: ${data.error || 'Server error'}`);
    }
  } catch (err) {
    alert(`Failed saving settings: ${err.message}`);
  }
}

export async function saveAuthCredentials() {
  const clientId = (document.getElementById('setting-auth-client-id')?.value || '').trim();

  try {
    const res = await fetch('/api/auth/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: clientId })
    });
    const data = await res.json();
    if (data.success) {
      showToast('OAuth credentials saved.');
      loadAuthStatus();
    } else {
      alert(`Failed to save credentials: ${data.error}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

export function saveAuthSlug() {
  return saveAuthCredentials();
}

export async function connectAccount() {
  try {
    const res = await fetch('/api/auth/login?redirect=0');
    const data = await res.json();
    if (data.auth_url) {
      window.location.href = data.auth_url;
    } else {
      alert(data.error || 'Failed to generate MyMiniFactory login authorization URL.');
    }
  } catch (err) {
    alert(`Connection error: ${err.message}`);
  }
}

async function loadSessionStatus() {
  try {
    const res = await fetch('/api/session');
    const data = await res.json();
    document.getElementById('btn-logout').style.display = data.login_required ? '' : 'none';
  } catch (err) {
    console.error('Error loading session status:', err);
  }
}

export async function logOut() {
  try {
    await fetch('/api/logout', { method: 'POST' });
    window.location.href = '/login';
  } catch (err) {
    alert(`Log out error: ${err.message}`);
  }
}

export async function disconnectAccount() {
  if (!confirm('Disconnect this MyMiniFactory account?')) return;
  try {
    const res = await fetch('/api/auth/disconnect', { method: 'POST' });
    const data = await res.json();
    if (data.success) {
      showToast('Account disconnected.');
      loadAuthStatus();
    }
  } catch (err) {
    alert(`Disconnect error: ${err.message}`);
  }
}

export async function testAccountConnection() {
  try {
    const res = await fetch('/api/auth/status');
    const data = await res.json();
    if (data.connected) {
      alert(`Connection active! Authenticated as @${data.username} (ID: ${data.user_id}).`);
    } else {
      alert('Connection expired or not connected.');
    }
  } catch (err) {
    alert(`Test error: ${err.message}`);
  }
}

export async function openDirectoryPicker(inputId) {
  targetInputId = inputId;
  const initialPath = document.getElementById(inputId)?.value.trim() || '';
  currentBrowsePath = initialPath;
  await navigateBrowseFolder(initialPath);
  const modal = document.getElementById('browse-modal');
  if (modal) modal.style.display = 'flex';
}

export async function navigateBrowseFolder(folderPath) {
  try {
    const url = `/api/system/browse${folderPath ? `?path=${encodeURIComponent(folderPath)}` : ''}`;
    const res = await fetch(url);
    const data = await res.json();

    currentBrowsePath = data.current_path || '';
    const pathBox = document.getElementById('browse-current-path');
    if (pathBox) pathBox.textContent = currentBrowsePath;

    const list = document.getElementById('browse-dir-list');
    if (!list) return;

    const htmlParts = [];
    if (data.parent_path) {
      htmlParts.push(html`
        <li class="browse-item cursor-pointer" @click=${() => window.navigateBrowseFolder(data.parent_path)}>
          <span class="text-dim font-mono">[..]</span> <span class="font-semibold text-accent">Parent Folder</span>
        </li>
      `);
    }

    const dirs = data.directories || [];
    if (!dirs.length) {
      htmlParts.push(html`<li class="browse-item text-dim">No subdirectories found.</li>`);
    } else {
      dirs.forEach(d => {
        htmlParts.push(html`
          <li class="browse-item cursor-pointer" @click=${() => window.navigateBrowseFolder(d.path)}>
            <span class="text-accent font-mono">[DIR]</span> ${d.name}
          </li>
        `);
      });
    }
    render(htmlParts, list);
  } catch (err) {
    console.error('Directory browser navigation error:', err);
  }
}

export function selectCurrentBrowseFolder() {
  if (targetInputId && currentBrowsePath) {
    const input = document.getElementById(targetInputId);
    if (input) input.value = currentBrowsePath;
  }
  const modal = document.getElementById('browse-modal');
  if (modal) modal.style.display = 'none';
}

export const initSettingsView = loadSettingsView;
export const refreshDirectoryUsage = loadDirectoryUsage;

export async function purgeStashExports(days = 14) {
  const purge = (body) => fetch('/api/settings/stashes/purge', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body)
  });
  try {
    const preview = await (await purge({ days })).json();
    const size = formatBytes(preview.reclaimable_bytes || 0);
    if (!confirm(`Delete ${preview.would_purge} print queue stash folders older than ${days} days (${size})?`)) {
      return;
    }
    const res = await purge({ days, confirm: true });
    const data = await res.json();
    if (data.success) {
      showToast(data.message);
      loadDirectoryUsage();
    } else {
      alert(`Stash purge failed: ${data.error || 'Server error'}`);
    }
  } catch (err) {
    alert(`Request error: ${err.message}`);
  }
}

// -----------------------------------------------------------------------------
// Window Bindings for Global DOM Event Handlers
// -----------------------------------------------------------------------------
Object.assign(window, {
  loadSettingsView,
  initSettingsView,
  loadSettings,
  refreshDirectoryUsage,
  loadDirectoryUsage,
  loadExternalLibraries,
  handleAddExternalLibrary,
  handleScanExternalLibrary,
  handleDeleteExternalLibrary,
  loadInactive,
  loadAuthStatus,
  loadLoggingSettings,
  saveLoggingSettings,
  purgeLogs,
  saveSettingsForm,
  saveAuthSlug,
  saveAuthCredentials,
  connectAccount,
  disconnectAccount,
  testAccountConnection,
  openDirectoryPicker,
  navigateBrowseFolder,
  selectCurrentBrowseFolder,
  purgeStashExports,
});
