/**
 * PDFs from every source. Filters run on the server against the catalog, so this works with the NAS offline.
 */

import { html, imageFallback, nothing, render, safeUrl } from '../template.js';
import { formatBytes } from '../utils.js';
import { fetchDocuments, stashDocument, updateDocuments as startUpdate, queueDownloads } from '../api.js';
import { showToast } from './settings.js';

const STATE_LABELS = {
  on_disk: 'On disk',
  in_archive: 'Inside an archive',
  not_downloaded: 'Not downloaded',
  unread: 'Archive not read yet',
};
const FILTERS = { kind: 'document-kind', creator: 'document-creator', source: 'document-source', state: 'document-state' };
let page = 1;
let searchTimer = null;

function fillSelect(id, values, labelFor = value => value) {
  const select = document.getElementById(id);
  if (!select || select.options.length > 1) return;
  values.forEach(value => select.add(new Option(labelFor(value), value)));
}

async function addToStash(doc) {
  try {
    const res = await stashDocument(doc.key);
    showToast(`Added to ${res.stash_name || 'the stash'}.`);
    window.checkHealth?.();
  } catch (err) {
    alert(err.message);
  }
}

async function queueCollection(doc) {
  try {
    await queueDownloads({ model_ids: [doc.collection_id] });
    showToast(`${doc.collection} queued for download.`);
  } catch (err) {
    alert(err.message);
  }
}

// safeUrl only passes full web links, so it would blank our own /previews/ paths. A cover from a
// collection is the part that came from outside.
function coverOf(doc) {
  const url = doc.preview_url || '';
  return url.startsWith('/previews/') ? url : safeUrl(url) || '/previews/none';
}

function card(doc) {
  const stored = doc.state === 'on_disk' || doc.state === 'in_archive';
  return html`
    <article class="document-card" data-key=${doc.key}>
      <img class="document-cover" src=${coverOf(doc)} alt="" loading="lazy" @error=${imageFallback('/previews/none')}>
      <div class="document-body">
        <strong class="document-title">${doc.title}</strong>
        <span class="font-xs text-dim">${doc.kind} · ${doc.creator || doc.source}</span>
        <span class="font-xs text-dim document-origin">${doc.state === 'unread' ? doc.filename : doc.collection}</span>
        <span class="font-xs text-dim">${STATE_LABELS[doc.state]}${doc.size_bytes ? ` · ${formatBytes(doc.size_bytes)}` : ''}</span>
        <div class="document-actions">
          ${stored ? html`
            <a class="btn-pill font-xs" href=${`/api/documents/${encodeURIComponent(doc.key)}/file`} target="_blank" rel="noopener">Open</a>
            <button type="button" class="btn-pill font-xs document-stash" @click=${() => addToStash(doc)}>Add to Stash</button>
          ` : html`
            <button type="button" class="btn-pill font-xs document-queue" @click=${() => queueCollection(doc)}>Queue Download</button>
          `}
        </div>
      </div>
    </article>
  `;
}

export async function loadDocuments(nextPage = 1) {
  page = nextPage;
  const results = document.getElementById('document-results');
  if (!results) return;
  const filters = { q: document.getElementById('document-search')?.value.trim() || '', page };
  Object.entries(FILTERS).forEach(([name, id]) => { filters[name] = document.getElementById(id)?.value || ''; });
  try {
    const data = await fetchDocuments(filters);
    fillSelect('document-kind', Object.keys(data.kinds));
    fillSelect('document-creator', data.creators);
    fillSelect('document-source', data.sources);
    const pages = Math.max(Math.ceil(data.total / data.page_size), 1);
    render(html`
      <p class="text-dim font-sm" id="document-summary">
        ${data.total.toLocaleString()} PDFs${data.previews_waiting ? ` · ${data.previews_waiting.toLocaleString()} previews waiting` : ''}
      </p>
      ${data.documents.length
        ? html`<div class="document-grid">${data.documents.map(card)}</div>`
        : html`<div class="table-msg text-dim">No PDFs match. Update PDFs, under Vault Reconcile, records the ones in linked libraries.</div>`}
      ${pages > 1 ? html`
        <div class="results-pager">
          <button type="button" class="btn-pill font-xs" ?disabled=${page <= 1} @click=${() => loadDocuments(page - 1)}>Previous</button>
          <span class="font-sm text-dim">Page ${page} of ${pages}</span>
          <button type="button" class="btn-pill font-xs" ?disabled=${page >= pages} @click=${() => loadDocuments(page + 1)}>Next</button>
        </div>
      ` : nothing}
    `, results);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed loading PDFs: ${err.message}</div>`, results);
  }
}

export async function updateDocuments() {
  try {
    const res = await startUpdate();
    showToast(`PDF update started (Task #${res.task_id}). Follow it on the Tasks tab.`);
    window.checkHealth?.();
  } catch (err) {
    alert(err.message);
  }
}

export function bindDocumentFilters() {
  document.getElementById('document-search')?.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadDocuments(1), 250);
  });
  Object.values(FILTERS).forEach(id => document.getElementById(id)?.addEventListener('change', () => loadDocuments(1)));
}

Object.assign(window, { loadDocuments, updateDocuments });
bindDocumentFilters();
