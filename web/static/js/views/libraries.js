/**
 * views/libraries.js - Linked libraries: registering, previewing, reviewing and adding.
 * Disk figures stay in Settings. Everything that changes a library lives here.
 */

import { parseServerTime } from '../utils.js';
import { html, nothing, render } from '../template.js';
import {
  fetchExternalLibraries,
  addExternalLibrary,
  deleteExternalLibrary,
  scanExternalLibrary,
  fetchLibraryPreview,
  approveLibraryPreview,
  fetchLibraryRelease,
  saveLibraryReview,
  fetchImageQueue,
  postImageQueue
} from '../api.js';
import { showToast } from './settings.js';

const PREVIEW_WATCH_MS = 30 * 60 * 1000;
const PREVIEW_WATCH_INTERVAL_MS = 5000;

let previewWatch = null;
let currentLibrary = null;
let currentReport = null;
let review = null;

const flagsIn = (release) => release.new.filter(m => (m.flags || []).length).length;

function flaggedUnreviewed(report) {
  return (report?.releases || []).filter(r => !r.reviewed).reduce((sum, r) => sum + flagsIn(r), 0);
}

// Unreviewed trouble first, so the job shrinks from the top as releases get done.
function reviewQueue(report) {
  const worth = (report?.releases || []).filter(r => r.new.length || r.overlaps.length || r.empty.length || r.low_confidence.length);
  return [...worth].sort((a, b) =>
    Number(a.reviewed) - Number(b.reviewed)
    || (flagsIn(b) + b.low_confidence.length) - (flagsIn(a) + a.low_confidence.length)
    || a.name.localeCompare(b.name));
}

export async function loadExternalLibraries() {
  const panel = document.getElementById('external-libraries-panel');
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

// The panel only draws when the list loads, so a finished preview used to stay invisible.
function watchForFinishedPreview(lib, seen) {
  clearInterval(previewWatch);
  const deadline = Date.now() + PREVIEW_WATCH_MS;
  previewWatch = setInterval(async () => {
    const data = await fetchLibraryPreview(lib.id).catch(() => null);
    if (Date.now() > deadline || (data && data.finished_at !== seen)) {
      clearInterval(previewWatch);
      if (data && data.finished_at !== seen) await loadExternalLibraries();
    }
  }, PREVIEW_WATCH_INTERVAL_MS);
}

async function startLibraryPass(lib, mode) {
  try {
    const before = mode === 'preview' ? await fetchLibraryPreview(lib.id) : null;
    const res = await scanExternalLibrary(lib.id, mode);
    showToast(`Started for '${lib.name}' (Task #${res.task_id}). Follow it on the Tasks tab.`);
    window.checkHealth?.();
    if (before) watchForFinishedPreview(lib, before.finished_at);
  } catch (err) {
    alert(`Could not start: ${err.message}`);
  }
}

async function approveAndAdd(lib) {
  const skipped = [...document.querySelectorAll('.preview-release-check')]
    .filter(box => !box.checked)
    .map(box => box.dataset.release);
  const unread = flaggedUnreviewed(currentReport);
  if (unread && !confirm(`${unread} flagged models are in releases you haven't reviewed. Add them anyway?`)) return;
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

  currentLibrary = lib;
  currentReport = report;
  const releases = reviewQueue(report);
  const flagged = (report?.releases || []).reduce((sum, r) => sum + flagsIn(r), 0);
  const unread = flaggedUnreviewed(report);
  render(html`
    <div class="mt-4 pt-3" style="border-top: 1px solid #262635;">
      <h3 class="font-bold font-md mb-2 text-accent">Preview for ${lib.name}</h3>
      ${report ? html`
        <p class="text-dim font-sm" id="library-preview-summary">
          ${report.totals.new} new, ${report.totals.known} already known, ${report.totals.overlaps} overlapping,
          ${report.totals.empty} empty, ${report.totals.low_confidence} to check.
          Overlapping and empty folders are never added.
        </p>
        <p class="text-dim font-sm">
          ${flagged} models flagged. Most suspicious releases come first. Open any release to change its cards.
        </p>
        <table class="table-compact" id="library-preview-table">
          <thead><tr><th style="width: 60px;">Add</th><th>Release</th><th>New</th><th>Known</th><th>Flagged</th><th>Status</th><th>Needs a look</th></tr></thead>
          <tbody>
            ${releases.map(r => html`
              <tr class=${flagsIn(r) ? 'preview-row-flagged' : ''}>
                <td>${r.new.length ? html`<input type="checkbox" class="preview-release-check" data-release=${r.name} ?checked=${!r.low_confidence.length}>` : nothing}</td>
                <td>
                  ${r.new.length ? html`<button type="button" class="link-btn preview-release-open" data-release=${r.name} @click=${() => openReview(lib, r.name)}>${r.name}</button>` : r.name}
                  ${r.new.length ? html`
                    <details class="preview-models">
                      <summary>${r.new.length} models</summary>
                      ${r.new.map(m => html`
                        <button type="button" class=${`preview-model ${(m.flags || []).length ? 'preview-model-flagged' : ''}`}
                          title=${(m.flags || []).join('. ')} @click=${() => openReview(lib, r.name, m.rel_path)}>${m.name || m.rel_path}</button>
                      `)}
                    </details>
                  ` : nothing}
                </td>
                <td class="font-mono">${r.new.length}</td>
                <td class="font-mono">${r.known}</td>
                <td class="font-mono">${flagsIn(r) || ''}</td>
                <td>${r.reviewed ? 'Reviewed' : r.edited ? 'Edited' : ''}</td>
                <td>
                  ${previewFolderList('Check', r.low_confidence)}
                  ${previewFolderList('Overlaps', r.overlaps)}
                  ${previewFolderList('Empty', r.empty)}
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      ` : nothing}
      ${unread ? html`<p class="text-dim font-sm mt-2" id="preview-unread">${unread} flagged models are in releases you haven't reviewed.</p>` : nothing}
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
// Review modal: a release's tree on the left, its cards on the right
// -----------------------------------------------------------------------------

const leaf = (path) => path.slice(path.lastIndexOf('/') + 1);
const isInside = (path, parent) => path === parent || path.startsWith(`${parent}/`);

function ownerOf(path) {
  // The deepest claim wins, the same rule the server carves by.
  for (let at = path; at.includes('/') || at; at = at.includes('/') ? at.slice(0, at.lastIndexOf('/')) : '') {
    const card = review.cards.find(c => c.members.includes(at));
    if (card) return card;
  }
  return null;
}

function isLocked(path) {
  return review.data.locked.some(held => isInside(path, held));
}

function childrenOf(path) {
  const prefix = `${path}/`;
  const direct = (paths) => paths.filter(p => p.startsWith(prefix) && !p.slice(prefix.length).includes('/'));
  return { folders: direct(review.data.folders), files: direct(review.data.files) };
}

function openPathsTo(card) {
  // The member itself opens too: what's wrong with a flagged card is usually inside it.
  for (const member of card.members) {
    for (let at = member; at.includes('/'); at = at.slice(0, at.lastIndexOf('/'))) review.open.add(at);
  }
}

function drawTreeRow(path, isFile, depth) {
  const expanded = review.open.has(path);
  const { folders, files } = isFile ? { folders: [], files: [] } : childrenOf(path);
  const owner = ownerOf(path);
  const locked = isLocked(path);
  const toggleOpen = () => {
    if (expanded) review.open.delete(path); else review.open.add(path);
    drawReview();
  };
  const toggleSelected = (event) => {
    if (event.target.checked) review.selected.add(path); else review.selected.delete(path);
    drawReview();
  };
  return html`
    <div class="review-row" data-path=${path} style=${`padding-left: ${depth * 16}px`}>
      <input type="checkbox" class="review-select" aria-label=${`Select ${leaf(path)}`} ?disabled=${locked} .checked=${review.selected.has(path)} @change=${toggleSelected}>
      ${isFile ? html`<span class="review-twisty"></span>` : html`
        <button type="button" class="review-twisty" aria-label=${expanded ? 'Collapse' : 'Expand'} @click=${toggleOpen}>${expanded ? '-' : '+'}</button>
      `}
      <span class=${isFile ? 'review-name font-mono' : 'review-name'}>${leaf(path)}</span>
      ${locked ? html`<span class="review-label">In library</span>`
        : owner ? html`<span class=${`review-label ${owner.skip ? 'review-label-skip' : ''} ${owner.key === review.focus ? 'review-label-focus' : ''}`}>${owner.name}</span>` : nothing}
    </div>
    ${expanded ? [...folders.map(f => drawTreeRow(f, false, depth + 1)), ...files.map(f => drawTreeRow(f, true, depth + 1))] : nothing}
  `;
}

function moveSelected(target) {
  // A ticked folder carries its contents, so its ticked children add nothing.
  const picked = [...review.selected].filter(p => ![...review.selected].some(o => o !== p && isInside(p, o)));
  for (const card of review.cards) card.members = card.members.filter(m => !picked.some(p => isInside(m, p)));
  target.members.push(...picked);
  review.cards = review.cards.filter(c => c.members.length);
  review.selected.clear();
  review.focus = target.key;
  review.dirty = true;
  drawReview();
}

function moveToChosen() {
  const chosen = document.getElementById('review-target')?.value;
  const target = review.cards.find(c => c.key === chosen);
  if (target) moveSelected(target);
}

function newCard() {
  const first = [...review.selected][0];
  if (!first) return;
  const card = { key: '', name: leaf(first).replace(/\.[^.]+$/, ''), category: '', members: [], skip: false, flags: [] };
  card.key = `new-${Date.now()}`;
  review.cards = [card, ...review.cards];
  moveSelected(card);
}

function drawCard(card) {
  const edit = (field) => (event) => {
    card[field] = field === 'skip' ? event.target.checked : event.target.value;
    review.dirty = true;
    if (field === 'skip') drawReview();
  };
  const show = () => {
    review.focus = card.key;
    openPathsTo(card);
    drawReview();
  };
  return html`
    <div class=${`review-card ${card.key === review.focus ? 'review-card-focus' : ''} ${card.skip ? 'review-card-skip' : ''}`} data-card=${card.name}>
      <div class="flex gap-2">
        <input type="text" class="review-card-name" aria-label="Card name" .value=${card.name} @input=${edit('name')} spellcheck="false">
        <input type="text" class="review-card-category" aria-label="Category" .value=${card.category} @input=${edit('category')} spellcheck="false">
      </div>
      <div class="review-card-actions font-xs text-dim">
        <button type="button" class="link-btn" @click=${show}>${card.members.length} ${card.members.length === 1 ? 'path' : 'paths'}</button>
        <label><input type="checkbox" class="review-skip" .checked=${!!card.skip} @change=${edit('skip')}> Skip</label>
      </div>
      ${(card.flags || []).map(flag => html`<div class="font-xs review-flag">${flag}</div>`)}
    </div>
  `;
}

function drawReview() {
  const body = document.getElementById('library-review-body');
  if (!body || !review) return;
  const { data } = review;
  const top = data.folders.filter(f => !f.slice(data.release.length + 1).includes('/'));
  const selected = review.selected.size;
  document.getElementById('library-review-title').textContent = data.release;
  // Flagged cards first. The key tiebreak stops a card jumping while its name is being edited.
  const cards = [...review.cards].sort((a, b) => (b.flags || []).length - (a.flags || []).length || a.key.localeCompare(b.key));
  render(html`
    ${data.settled.length ? html`<p class="text-dim font-sm">This release is approved, so its cards are read-only.</p>` : nothing}
    <div class="review-layout">
      <div class="review-tree" id="review-tree">
        ${top.map(f => drawTreeRow(f, false, 0))}
      </div>
      <div class="review-cards">
        <div class="review-actions">
          <span class="font-sm" id="review-selected">${selected} selected</span>
          <button type="button" class="btn-pill font-xs" id="btn-review-new" ?disabled=${!selected} @click=${newCard}>New Card</button>
          <select id="review-target" aria-label="Card to move to">
            ${cards.map(c => html`<option value=${c.key}>${c.name}</option>`)}
          </select>
          <button type="button" class="btn-pill font-xs" id="btn-review-move" ?disabled=${!selected} @click=${moveToChosen}>Move to Card</button>
        </div>
        <div class="review-card-list">${cards.map(drawCard)}</div>
      </div>
    </div>
    <div class="flex gap-2 mt-2">
      <button type="button" class="btn-pill btn-action-primary" id="btn-review-next" ?disabled=${!!data.settled.length} @click=${() => saveReview(true, true)}>Mark Reviewed and Next</button>
      <button type="button" class="btn-pill" id="btn-review-save" ?disabled=${!!data.settled.length} @click=${() => saveReview(data.reviewed, false)}>Save</button>
      <button type="button" class="btn-pill btn-action-cancel" id="btn-review-reset" ?disabled=${!!data.settled.length || !data.changed} @click=${resetReview}>Reset to Code's Guess</button>
    </div>
  `, body);
}

async function openReview(lib, release, focusPath = '') {
  if (review?.dirty && !confirm('Discard your unsaved changes to this release?')) return;
  try {
    const data = await fetchLibraryRelease(lib.id, release);
    const key = focusPath.slice(release.length + 1);
    review = {
      lib, data, cards: structuredClone(data.cards), selected: new Set(), open: new Set(), dirty: false,
      focus: data.cards.some(c => c.key === key) ? key : '',
    };
    for (const card of review.cards) if ((card.flags || []).length || card.key === review.focus) openPathsTo(card);
    drawReview();
    window.openModal('library-review-modal');
  } catch (err) {
    alert(`Could not open ${release}: ${err.message}`);
  }
}

function nextRelease(current) {
  const queue = reviewQueue(currentReport).filter(r => r.new.length && r.name !== current && !r.reviewed);
  return queue[0]?.name;
}

async function saveReview(reviewed, advance) {
  try {
    await saveLibraryReview(review.lib.id, { release: review.data.release, cards: review.cards, reviewed });
    const { lib, data } = review;
    review.dirty = false;
    showToast(`Saved ${data.release}.`);
    await loadLibraryPreview(lib);
    const next = advance ? nextRelease(data.release) : null;
    if (next) await openReview(lib, next);
    else if (advance) closeReview();
    else await openReview(lib, data.release);
  } catch (err) {
    alert(`Save failed: ${err.message}`);
  }
}

async function resetReview() {
  if (!confirm(`Put ${review.data.release} back to the code's guess?`)) return;
  try {
    const { lib, data } = review;
    await saveLibraryReview(lib.id, { release: data.release, reset: true });
    review.dirty = false;
    await loadLibraryPreview(lib);
    await openReview(lib, data.release);
  } catch (err) {
    alert(`Reset failed: ${err.message}`);
  }
}

export function closeReview() {
  if (review?.dirty && !confirm('Discard your unsaved changes to this release?')) return;
  review = null;
  window.closeModal('library-review-modal');
}

Object.assign(window, {
  loadExternalLibraries,
  handleAddExternalLibrary,
  handleScanExternalLibrary,
  handleDeleteExternalLibrary,
  closeReview,
});
