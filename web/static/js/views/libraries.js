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
  fetchArchiveQueue,
  fetchCatalogReview,
  saveCatalogReview,
  importFile,
  browseImportFolder,
  fetchArchiveReview,
  saveArchiveReview,
  fetchImageQueue,
  postImageQueue
} from '../api.js';
import { showToast } from './settings.js';

const PREVIEW_WATCH_MS = 30 * 60 * 1000;
const PREVIEW_WATCH_INTERVAL_MS = 5000;

let previewWatch = null;
const previews = {};
const PREVIEW_PAGE_SIZE = 7;
let review = null;
let archiveQueue = [];
const importPicked = new Set();

const peekTime = (stamp) => parseServerTime(stamp)?.toLocaleString() || stamp;
const flagsIn = (release) => release.new.filter(m => (m.flags || []).length).length;

function flaggedUnreviewed(report) {
  return (report?.releases || []).filter(r => !r.reviewed).reduce((sum, r) => sum + flagsIn(r), 0);
}

// Unreviewed trouble first, so the job shrinks from the top as releases get done.
function reviewQueue(report) {
  const worth = (report?.releases || []).filter(r => r.new.length || r.added || r.known || r.overlaps.length || r.empty.length || r.low_confidence.length);
  return [...worth].sort((a, b) =>
    Number(a.reviewed) - Number(b.reviewed)
    || (flagsIn(b) + b.low_confidence.length) - (flagsIn(a) + a.low_confidence.length)
    || a.name.localeCompare(b.name));
}

function libraryCard(lib) {
  const lastScan = lib.last_scanned_at
    ? parseServerTime(lib.last_scanned_at)?.toLocaleString() || 'Unknown'
    : '';
  return html`
    <section class="tool-card" data-library=${lib.id}>
      <div class="reconcile-card-action">
        <button type="button" class="btn-pill btn-action-primary" title="Finds new folders and shows them for approval. Adds nothing."
          @click=${() => startLibraryPass(lib, 'preview')}>Preview New Folders</button>
        <button type="button" class="btn-pill" title="Checks known models against storage. Never adds new folders."
          @click=${() => startLibraryPass(lib, 'full')}>Full Rescan</button>
        <button type="button" class="btn-pill btn-action-cancel"
          @click=${() => window.handleDeleteExternalLibrary(lib.id, lib.name)}>Remove</button>
      </div>
      <div class="reconcile-card-body">
        <h3 class="reconcile-card-title">${lib.name}${lib.exists ? nothing : html` <span class="status-badge status-danger font-xs" title="Directory path not currently mounted">Unmounted</span>`}</h3>
        <p>${lib.default_creator || lib.name}</p>
        <p class="font-mono font-xs location-path">${lib.path}</p>
        <div class="reconcile-last-run">
          <span>${lib.disk_formatted || '0 B'}</span>
          <span>${(lib.total_models || 0).toLocaleString()} models</span>
          <span>${(lib.total_files || 0).toLocaleString()} files</span>
          <span>${lastScan ? `Last scanned ${lastScan}` : 'Never scanned'}</span>
        </div>
      </div>
    </section>
  `;
}

export async function loadExternalLibraries() {
  const panel = document.getElementById('external-libraries-panel');
  if (!panel) return;

  render(html`
    <div class="reconcile-cards">
      <div class="library-section-heading">
        <div>
          <h2 class="settings-panel-title">Existing In-Place Libraries</h2>
          <div class="settings-panel-subtitle">Local and network collections indexed where they sit, strictly read-only.</div>
        </div>
        <button type="button" class="btn-pill" @click=${() => window.loadExternalLibraries()}>Refresh Libraries</button>
      </div>
      <div class="reconcile-cards-inner" id="external-libraries-table-container">
        <div class="table-msg">Loading registered libraries...</div>
      </div>

      <h2 class="settings-panel-title library-section-title">Register New External Library</h2>
      <section class="tool-card">
        <div class="reconcile-card-action">
          <button type="button" class="btn-pill btn-action-primary" @click=${() => window.handleAddExternalLibrary()}>Register In-Place Library</button>
        </div>
        <div class="reconcile-card-body">
          <div class="settings-form-row two-col">
            <div class="settings-field-group">
              <label for="new-lib-path">Top-Level Storage Directory Path *</label>
              <div class="settings-input-with-button">
                <input type="text" id="new-lib-path" placeholder="/Volumes/STL/Loot Studios" spellcheck="false">
                <button type="button" class="btn-pill" @click=${() => window.openDirectoryPicker('new-lib-path')}>Browse</button>
              </div>
              <span class="form-help-text">Library root on the NAS or a local drive. It stays read-only.</span>
            </div>
            <div class="settings-field-group">
              <label for="new-lib-name">Display Name (Optional)</label>
              <input type="text" id="new-lib-name" placeholder="Defaults to folder name" spellcheck="false">
              <span class="form-help-text">Label shown in the catalog and monthly releases.</span>
            </div>
          </div>
          <div class="settings-form-row two-col mt-2">
            <div class="settings-field-group">
              <label for="new-lib-creator">Default Creator / Studio (Optional)</label>
              <input type="text" id="new-lib-creator" placeholder="e.g. Loot Studios" spellcheck="false">
              <span class="form-help-text">Used for models when a release folder doesn't name one.</span>
            </div>
          </div>
        </div>
      </section>

      <h2 class="settings-panel-title library-section-title">Background Images</h2>
      <div id="library-image-queue"></div>

      <div id="library-preview-container"></div>
    </div>
  `, panel);

  const tableContainer = document.getElementById('external-libraries-table-container');
  try {
    const res = await fetchExternalLibraries();
    const libs = res.libraries || [];

    if (!libs.length) {
      render(html`
        <div class="table-msg text-dim py-4">
          No external libraries registered. Register a directory below to index external collections without moving files.
        </div>
      `, tableContainer);
      await loadImageQueue();
      return;
    }

    render(html`${libs.map(libraryCard)}`, tableContainer);
    // One slot per library: each has its own preview, so a second library sits beside the first.
    render(html`${libs.map(lib => html`<div id=${`library-preview-${lib.id}`}></div>`)}`, document.getElementById('library-preview-container'));
    await Promise.all([...libs.map(loadLibraryPreview), loadImageQueue()]);
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

// Waiting costs nothing here: flagged cards and unticked releases just stay proposed.
async function approveReviewed(lib) {
  const held = [...previews[lib.id].held];
  try {
    const res = await approveLibraryPreview(lib.id, held, true);
    showToast(`Approved ${res.approved} folders. ${res.waiting} stay for review.`);
    await startLibraryPass(lib, 'add');
    await loadLibraryPreview(lib);
  } catch (err) {
    alert(`Approval failed: ${err.message}`);
  }
}

async function approveAndAdd(lib) {
  const skipped = [...previews[lib.id].held];
  const unread = flaggedUnreviewed(previews[lib.id].report);
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

function toggleRelease(state, name, ticked) {
  if (ticked) state.held.delete(name); else state.held.add(name);
}

function turnPreviewPage(lib, step) {
  previews[lib.id].page += step;
  drawLibraryPreview(lib);
}

function drawLibraryPreview(lib) {
  const container = document.getElementById(`library-preview-${lib.id}`);
  const state = previews[lib.id];
  if (!container || !state) return;
  const { report, waiting, approved } = state;
  if (!report && !approved) {
    render(nothing, container);
    return;
  }
  const releases = reviewQueue(report);
  const pages = Math.max(Math.ceil(releases.length / PREVIEW_PAGE_SIZE), 1);
  state.page = Math.min(state.page, pages);
  const shown = releases.slice((state.page - 1) * PREVIEW_PAGE_SIZE, state.page * PREVIEW_PAGE_SIZE);
  const flagged = (report?.releases || []).reduce((sum, r) => sum + flagsIn(r), 0);
  const unread = flaggedUnreviewed(report);
  render(html`
    <section class="library-preview-card">
      <h2 class="settings-panel-title">Preview for ${lib.name}</h2>
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
            ${shown.map(r => html`
              <tr class=${flagsIn(r) ? 'preview-row-flagged' : ''}>
                <td>${r.new.length ? html`<input type="checkbox" class="preview-release-check" data-release=${r.name}
                  .checked=${!state.held.has(r.name)} @change=${(event) => toggleRelease(state, r.name, event.target.checked)}>` : nothing}</td>
                <td>
                  ${r.new.length ? html`<button type="button" class="link-btn preview-release-open" data-release=${r.name} @click=${() => openReview(lib, r.name)}>${r.name}</button>` : r.name}
                  ${r.added || r.known ? html`<button type="button" class="btn-pill font-xs preview-edit-added" @click=${() => openAddedReview(lib, r.name)}>Edit Added</button>` : nothing}
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
        ${pages > 1 ? html`
          <div class="results-pager">
            <button type="button" class="btn-pill font-xs" ?disabled=${state.page <= 1} @click=${() => turnPreviewPage(lib, -1)}>Previous</button>
            <span class="font-sm text-dim">Page ${state.page} of ${pages}</span>
            <button type="button" class="btn-pill font-xs" ?disabled=${state.page >= pages} @click=${() => turnPreviewPage(lib, 1)}>Next</button>
          </div>
        ` : nothing}
      ` : nothing}
      ${unread ? html`<p class="text-dim font-sm mt-2" id="preview-unread">${unread} flagged models are in releases you haven't reviewed.</p>` : nothing}
      ${report && waiting ? html`
        <p class="text-dim font-sm mt-2" id="preview-ready">
          ${report.totals.ready} ready: reviewed by you, or unflagged. Unticked releases and flagged models stay for review.
        </p>
      ` : nothing}
      <div class="flex gap-2 mt-2">
        ${report?.totals.ready ? html`<button type="button" class="btn-pill btn-action-primary" id="btn-approve-ready" @click=${() => approveReviewed(lib)}>Approve and Add Reviewed</button>` : nothing}
        ${waiting ? html`<button type="button" class="btn-pill" id="btn-approve-preview" @click=${() => approveAndAdd(lib)}>Approve and Add All ${waiting} Folders</button>` : nothing}
        ${approved ? html`<button type="button" class="btn-pill" @click=${() => startLibraryPass(lib, 'add')}>Resume Adding ${approved} Folders</button>` : nothing}
      </div>
    </section>
  `, container);
}

async function loadLibraryPreview(lib) {
  if (!lib) return;
  const data = await fetchLibraryPreview(lib.id);
  const candidates = data.candidates || {};
  const earlier = previews[lib.id];
  // Ticks live here, because a release on another page has no checkbox to read.
  previews[lib.id] = {
    report: data.report, waiting: candidates.proposed || 0, approved: candidates.approved || 0,
    page: earlier?.page || 1, held: earlier?.held || new Set(),
  };
  drawLibraryPreview(lib);
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
    <section class="tool-card">
      <div class="reconcile-card-action">
        <button type="button" class="btn-pill" id="btn-image-queue-toggle" @click=${() => changeImageQueue(data.paused ? 'resume' : 'pause')}>${data.paused ? 'Resume' : 'Pause'}</button>
        ${counts.failed + counts.no_source ? html`<button type="button" class="btn-pill" @click=${() => changeImageQueue('retry')}>Retry ${counts.failed + counts.no_source} Failed</button>` : nothing}
      </div>
      <div class="reconcile-card-body">
        <p>Renders and caches a cover for each linked library model in the background, so the library shows pictures without reaching the NAS. It pauses by itself during a scan or while storage is offline.</p>
        <div class="reconcile-last-run" id="image-queue-summary">
          <span>${IMAGE_STATES[data.state] || data.state}. ${counts.pending} waiting, ${counts.done} done,
          ${counts.failed} failed, ${counts.no_source} with nothing to render.</span>
        </div>
      </div>
    </section>
  `, container);
}

/** Unlinks a library, after asking. Files are untouched. */
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
    const card = review.cards.find(c => c.paths.includes(at));
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

// Keeps letters and digits in any script, so names compare across punctuation.
const squash = (text) => text.toLowerCase().replace(/[^\p{L}\p{N}]/gu, '');

// Flags arrive as sentences from the resolver. This turns each into what's wrong, what to do about
// it, and which rows or cards to light up. A flag that matches nothing is still shown as written.
function concernOf(flag) {
  const close = flag.match(/^Very close to (.+), possibly the same thing$/);
  if (close) {
    return {
      label: 'Possible duplicate', other: close[1], terms: [],
      ask: `Compare with ${close[1]}. If they're one design, tick the rows and Move to Card.`,
    };
  }
  const part = flag.match(/^Looks like a part of (.+)$/);
  if (part) {
    return {
      label: 'Possible part', other: part[1], terms: [],
      ask: `May be a piece of ${part[1]}, not its own model. If so, merge it. If it's its own design, leave it.`,
    };
  }
  const rename = flag.match(/^Looks like a rename of (.+) \(#(\d+)\)$/);
  if (rename) {
    return {
      label: 'Possible rename', relink: Number(rename[2]), terms: [],
      ask: `Its files match ${rename[1]}, which went inactive. Link it to keep that model's ID, cover and history.`,
    };
  }
  const parent = flag.match(/^Named from a parent folder, but its folder says (.+)$/);
  if (parent) {
    const names = parent[1].split(', ');
    return { label: 'Wrong name?', terms: names, ask: `The name came from a parent folder, but the highlighted folder says ${parent[1]}. Rename the card if so.` };
  }
  const holds = flag.match(/^Holds other named items: (.+)$/);
  if (holds) {
    const names = holds[1].split(', ');
    return { label: 'Item inside a model', terms: names, ask: 'Is a highlighted folder its own thing? Tick it and click New Card. If it\'s a piece of this model, leave it.' };
  }
  if (flag.endsWith('is also a folder grouping other models')) {
    return { label: 'Named after its folder', terms: [], ask: 'This card took the name of the folder around it. The blue rows show what it really is, so rename the card to match.' };
  }
  if (/^Spans \d+ folders/.test(flag)) {
    return { label: 'Unusually large', terms: [], ask: 'It covers far more folders than others here. Check its paths: if different designs were merged, split them with New Card.' };
  }
  if (flag.startsWith('New since your review')) return { label: 'New', terms: [], ask: 'This appeared after you reviewed the release.' };
  return { label: 'Check', terms: [], ask: flag };
}

const isNote = (flag) => flag.startsWith('Shared with');
const concernsOf = (card) => (card.flags || []).filter(f => !isNote(f)).map(concernOf);

function isSuspectRow(path, card) {
  if (!card) return false;
  const name = squash(leaf(path));
  return concernsOf(card).some(c => c.terms.some(term => squash(term) && name.includes(squash(term))));
}

function focusCard(card) {
  review.focus = card.key;
  openPathsTo(card);
  for (const concern of concernsOf(card)) {
    const twin = review.cards.find(c => c.name === concern.other);
    if (twin) openPathsTo(twin);
  }
  drawReview();
  const row = document.querySelector(`.review-row[data-path="${CSS.escape(card.paths[0] || '')}"]`);
  row?.scrollIntoView({ block: 'center' });
  document.querySelector('.review-card-focus')?.scrollIntoView({ block: 'nearest' });
  document.querySelector('.review-concern-focus')?.scrollIntoView({ block: 'nearest' });
}

// Twenty-six "part of Mask" warnings say one thing, so they read as one entry with one fix.
function concernGroups() {
  const groups = new Map();
  for (const card of review.cards) {
    for (const concern of concernsOf(card)) {
      const key = `${concern.label}|${concern.other || concern.ask}`;
      if (!groups.has(key)) groups.set(key, { ...concern, cards: [] });
      groups.get(key).cards.push(card);
    }
  }
  return [...groups.values()];
}

function linkRename(group) {
  group.cards[0].relink = group.relink;
  review.dirty = true;
  drawReview();
}

function mergeInto(group) {
  const target = review.cards.find(c => c.name === group.other);
  if (!target) return;
  for (const card of group.cards) {
    target.paths.push(...card.paths);
    card.paths = [];
  }
  review.cards = review.cards.filter(c => c.paths.length);
  review.dirty = true;
  focusCard(target);
}

function drawConcerns() {
  const groups = concernGroups();
  if (!groups.length) return html`<p class="font-sm text-dim" id="review-concerns-none">Nothing is flagged here.</p>`;
  return html`
    <div class="review-concerns" id="review-concerns">
      <div class="review-concerns-title">What to check (${review.cards.filter(c => concernsOf(c).length).length})</div>
      ${groups.map(group => html`
        <div class="review-concern">
          <div class="review-concern-line"><span class="review-concern-label">${group.label}</span> ${group.ask}</div>
          <div class="review-concern-cards">
            ${group.cards.map(card => html`
              <button type="button" class=${`review-concern-chip ${card.key === review.focus ? 'review-concern-focus' : ''}`} @click=${() => focusCard(card)}>${card.name}</button>
            `)}
            ${group.relink ? html`
              <button type="button" class="btn-pill font-xs review-link" ?disabled=${group.cards[0].relink === group.relink}
                @click=${() => linkRename(group)}>${group.cards[0].relink === group.relink ? 'Linked' : 'Link to Old Model'}</button>
            ` : nothing}
            ${group.label === 'Possible part' ? html`
              <button type="button" class="btn-pill font-xs review-merge" @click=${() => mergeInto(group)}>Merge ${group.cards.length > 1 ? `all ${group.cards.length}` : ''} into ${group.other}</button>
            ` : nothing}
          </div>
        </div>
      `)}
      <div class="font-xs text-dim review-legend">
        <span class="review-key review-key-focus"></span> card you're checking
        <span class="review-key review-key-flagged"></span> rows on a flagged card
        <span class="review-key review-key-suspect"></span> the row causing the concern
      </div>
    </div>
  `;
}

function openPathsTo(card) {
  // The member itself opens too: what's wrong with a flagged card is usually inside it.
  for (const member of card.paths) {
    for (let at = member; at.includes('/'); at = at.slice(0, at.lastIndexOf('/'))) review.open.add(at);
  }
}

function rowClass(path, owner) {
  const twins = review.cards.find(c => c.key === review.focus);
  const inFocus = owner && (owner.key === review.focus
    || (twins && concernsOf(twins).some(c => c.other === owner.name)));
  return [
    'review-row',
    owner && concernsOf(owner).length ? 'review-row-flagged' : '',
    inFocus ? 'review-row-focus' : '',
    isSuspectRow(path, owner) ? 'review-row-suspect' : '',
  ].filter(Boolean).join(' ');
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
    <div class=${rowClass(path, owner)} data-path=${path} style=${`padding-left: ${depth * 16}px`}>
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
  for (const card of review.cards) card.paths = card.paths.filter(m => !picked.some(p => isInside(m, p)));
  target.paths.push(...picked);
  review.cards = review.cards.filter(c => c.paths.length);
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
  const card = { key: '', name: leaf(first).replace(/\.[^.]+$/, ''), category: '', paths: [], skip: false, flags: [], group: '', tags: [] };
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
  const show = () => focusCard(card);
  return html`
    <div class=${`review-card ${card.key === review.focus ? 'review-card-focus' : ''} ${card.skip ? 'review-card-skip' : ''} ${concernsOf(card).length ? 'review-card-flagged' : ''}`} data-card=${card.name}>
      <div class="flex gap-2">
        <input type="text" class="review-card-name" aria-label="Card name" .value=${card.name} @input=${edit('name')} spellcheck="false">
        <input type="text" class="review-card-category" aria-label="Category" .value=${card.category} @input=${edit('category')} spellcheck="false">
      </div>
      <input type="text" class="review-card-group mt-1" aria-label="Model group" placeholder="Model group" list="review-groups" .value=${card.group || ''} @input=${edit('group')} spellcheck="false">
      <div class="review-card-actions font-xs text-dim">
        <button type="button" class="link-btn" @click=${show}>${card.paths.length} ${card.paths.length === 1 ? 'path' : 'paths'}</button>
        <label><input type="checkbox" class="review-skip" .checked=${!!card.skip} @change=${edit('skip')}> Skip</label>
      </div>
      ${(card.flags || []).map(flag => html`<div class=${`font-xs ${isNote(flag) ? 'text-dim' : 'review-flag'}`}>${isNote(flag) ? flag : concernOf(flag).label}</div>`)}
    </div>
  `;
}

function drawReview() {
  const body = document.getElementById('library-review-body');
  if (!body || !review) return;
  const { data } = review;
  const top = data.folders.filter(f => !f.slice(data.release.length + 1).includes('/'));
  const selected = review.selected.size;
  const peeked = data.peeked_at ? ` - peeked ${peekTime(data.peeked_at)}` : '';
  document.getElementById('library-review-title').textContent = `${data.title || data.release}${peeked}`;
  const groupNames = [...new Set(review.cards.map(c => c.group).filter(Boolean))].sort();
  // Flagged cards first. The key tiebreak stops a card jumping while its name is being edited.
  const cards = [...review.cards].sort((a, b) => (b.flags || []).length - (a.flags || []).length || a.key.localeCompare(b.key));
  render(html`
    ${data.settled.length ? html`<p class="text-dim font-sm">${data.settled.length} cards here are already approved and hidden. Their paths show as In library.</p>` : nothing}
    ${drawConcerns()}
    <div class="review-layout">
      <div class="review-tree" id="review-tree">
        ${top.map(f => drawTreeRow(f, false, 0))}
      </div>
      <div class="review-cards">
        <div class="review-actions">
          <span class="font-sm" id="review-selected">${selected} selected</span>
          <button type="button" class="btn-pill font-xs" id="btn-review-new" ?disabled=${!selected} @click=${newCard}>New Card</button>
          <select id="review-target" aria-label="Card to move to">
            ${[...review.cards].sort((x, y) => x.name.localeCompare(y.name, undefined, { numeric: true, sensitivity: 'base' })).map(c => html`<option value=${c.key}>${c.name}</option>`)}
          </select>
          <button type="button" class="btn-pill font-xs" id="btn-review-move" ?disabled=${!selected} @click=${moveToChosen}>Move to Card</button>
        </div>
        <datalist id="review-groups">${groupNames.map(g => html`<option value=${g}></option>`)}</datalist>
        <div class="review-card-list">${cards.map(drawCard)}</div>
      </div>
    </div>
    <div class="flex gap-2 mt-2">
      <button type="button" class="btn-pill btn-action-primary" id="btn-review-next" ?disabled=${!data.cards.length} @click=${() => saveReview(true, true)}>Mark Reviewed and Next</button>
      <button type="button" class="btn-pill" id="btn-review-save" ?disabled=${!data.cards.length} @click=${() => saveReview(data.reviewed, false)}>Save</button>
      ${review.source.reset ? html`
        <button type="button" class="btn-pill btn-action-cancel" id="btn-review-reset" ?disabled=${!data.changed} @click=${resetReview}>Reset to Code's Guess</button>
      ` : nothing}
    </div>
  `, body);
}

async function showReview(source, focusKey = '') {
  if (review?.dirty && !confirm('Discard your unsaved changes to this release?')) return;
  try {
    const data = await source.load();
    review = {
      source, data, cards: structuredClone(data.cards), selected: new Set(), open: new Set(), dirty: false,
      focus: data.cards.some(c => c.key === focusKey) ? focusKey : '',
    };
    for (const card of review.cards) if ((card.flags || []).length || card.key === review.focus) openPathsTo(card);
    drawReview();
    window.openModal('library-review-modal');
    // Land on the first concern, so the release opens on the thing that needs a decision.
    const first = review.cards.find(c => c.key === review.focus) || review.cards.find(c => concernsOf(c).length);
    if (first) focusCard(first);
  } catch (err) {
    alert(`Could not open it: ${err.message}`);
  }
}

// Linked releases and archives share the screen. Only where cards load and save differs.
function openReview(lib, release, focusPath = '') {
  return showReview({
    load: () => fetchLibraryRelease(lib.id, release),
    save: (cards, reviewed) => saveLibraryReview(lib.id, { release, cards, reviewed }),
    reset: () => saveLibraryReview(lib.id, { release, reset: true }),
    refresh: () => loadLibraryPreview(lib),
    next: () => {
      const queue = reviewQueue(previews[lib.id]?.report).filter(r => r.new.length && r.name !== release && !r.reviewed);
      return queue[0] ? () => openReview(lib, queue[0].name) : null;
    },
    again: () => openReview(lib, release),
  }, focusPath.slice(release.length + 1));
}

function openAddedReview(lib, release) {
  return showReview({
    load: () => fetchCatalogReview(lib.id, release),
    save: (cards) => saveCatalogReview(lib.id, { release, cards }),
    refresh: () => loadLibraryPreview(lib),
    next: () => null,
    again: () => openAddedReview(lib, release),
  });
}

function openArchiveReview(collectionId) {
  return showReview({
    load: () => fetchArchiveReview(collectionId),
    save: (cards, reviewed) => saveArchiveReview(collectionId, { cards, reviewed }),
    refresh: loadArchiveQueue,
    next: () => {
      const next = archiveQueue.find(c => c.id !== collectionId && !c.reviewed);
      return next ? () => openArchiveReview(next.id) : null;
    },
    again: () => openArchiveReview(collectionId),
  });
}

function drawImportPicked() {
  const list = document.getElementById('import-picked');
  if (!list) return;
  render(importPicked.size ? html`
    <p class="font-sm">${importPicked.size} selected</p>
    <ul class="import-list">
      ${[...importPicked].map(path => html`
        <li class="font-mono font-xs">${path}
          <button type="button" class="tag-remove" aria-label=${`Remove ${path}`} @click=${() => { importPicked.delete(path); drawImportPicked(); }}>×</button>
        </li>
      `)}
    </ul>
  ` : nothing, list);
  document.getElementById('btn-import-file').disabled = !importPicked.size;
}

async function browseImport(folder = '') {
  const body = document.getElementById('import-browser-body');
  try {
    const data = await browseImportFolder(folder);
    const toggle = (path) => (event) => {
      if (event.target.checked) importPicked.add(path); else importPicked.delete(path);
      drawImportPicked();
    };
    const row = (path, name, isFolder) => html`
      <li class="import-row">
        <input type="checkbox" aria-label=${`Select ${name}`} .checked=${importPicked.has(path)} @change=${toggle(path)}>
        ${isFolder ? html`<button type="button" class="link-btn" @click=${() => browseImport(path)}>${name}/</button>` : html`<span class="font-mono font-xs">${name}</span>`}
      </li>
    `;
    render(html`
      <div class="import-roots">
        ${(data.roots || []).map(root => html`<button type="button" class="btn-pill font-xs" @click=${() => browseImport(root)}>${root.split('/').pop()}</button>`)}
      </div>
      <p class="font-mono font-xs text-dim">${data.current_path}</p>
      ${data.parent_path ? html`<button type="button" class="link-btn font-sm" @click=${() => browseImport(data.parent_path)}>Up one folder</button>` : nothing}
      <ul class="import-browser-list" id="import-browser-list">
        ${data.directories.map(d => row(d.path, d.name, true))}
        ${(data.files || []).map(f => row(f.path, f.name, false))}
      </ul>
      <div class="flex gap-2 mt-2">
        <button type="button" class="btn-pill btn-action-primary" @click=${() => window.closeModal('import-browser-modal')}>Done</button>
      </div>
    `, body);
  } catch (err) {
    alert(err.message);
  }
}

export async function openImportBrowser() {
  await browseImport();
  window.openModal('import-browser-modal');
}

export async function handleImportFile() {
  try {
    const res = await importFile({ paths: [...importPicked], creator: document.getElementById('import-creator')?.value.trim() || '' });
    showToast(`Import started (Task #${res.task_id}). Follow it on the Tasks tab.`);
    importPicked.clear();
    drawImportPicked();
    window.checkHealth?.();
  } catch (err) {
    alert(err.message);
  }
}

export async function loadArchiveQueue() {
  const container = document.getElementById('archive-review-queue');
  if (!container) return;
  try {
    archiveQueue = (await fetchArchiveQueue()).collections || [];
    const reviewed = archiveQueue.filter(c => c.reviewed).length;
    render(archiveQueue.length ? html`
      <p class="text-dim font-sm" id="archive-queue-summary">
        ${reviewed} of ${archiveQueue.length} collections reviewed. Most suspicious first.
      </p>
      <table class="table-compact">
        <thead><tr><th>Collection</th><th>Last Peeked</th><th>Models</th><th>Groups</th><th>Artifacts</th><th>Status</th></tr></thead>
        <tbody>
          ${archiveQueue.map(c => html`
            <tr class=${c.suspect && !c.reviewed ? 'preview-row-flagged' : ''}>
              <td><button type="button" class="link-btn archive-review-open" @click=${() => openArchiveReview(c.id)}>${c.name}</button></td>
              <td class="font-sm">${c.peeked_at ? peekTime(c.peeked_at) : '-'}</td>
              <td class="font-mono">${c.models}</td>
              <td class="font-mono">${c.model_groups}</td>
              <td class="font-mono">${c.artifacts}</td>
              <td>${c.reviewed ? 'Reviewed' : c.suspect ? 'Likely over-split' : ''}</td>
            </tr>
          `)}
        </tbody>
      </table>
    ` : html`<div class="table-msg text-dim">No peeked archives yet.</div>`, container);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed loading archives: ${err.message}</div>`, container);
  }
}

async function saveReview(reviewed, advance) {
  try {
    const { source, data } = review;
    await source.save(review.cards, reviewed);
    review.dirty = false;
    showToast(`Saved ${data.title || data.release}.`);
    await source.refresh();
    const next = advance ? source.next() : null;
    if (next) await next();
    else if (advance) closeReview();
    else await source.again();
  } catch (err) {
    alert(`Save failed: ${err.message}`);
  }
}

async function resetReview() {
  if (!confirm(`Put ${review.data.release} back to the code's guess?`)) return;
  try {
    const { source } = review;
    await source.reset();
    review.dirty = false;
    await source.refresh();
    await source.again();
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
  handleImportFile,
  openImportBrowser,
  loadArchiveQueue,
});
