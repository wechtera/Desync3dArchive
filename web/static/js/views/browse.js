/**
 * views/browse.js - Browsing models. Opening a model shows its groups, a group opens all its
 * models, and clicking a variant there goes back to the model view for that variant.
 */

import { formatBytes } from '../utils.js';
import { html, imageFallback, nothing, render } from '../template.js';
import { fetchCatalogModel, fetchCatalogModels, fetchModelGroup } from '../api.js';

const SEARCH_DELAY_MS = 250;

let page = 1;
let searchTimer = null;

function card(model, onClick) {
  return html`
    <button type="button" class="browse-card" data-model=${model.id} @click=${onClick}>
      <img src=${`/previews/${model.cover_id}/cover.jpg`} @error=${imageFallback(model.image_url)} loading="lazy" alt=${model.name}>
      <div class="browse-card-title">${model.name}</div>
      <div class="browse-card-meta">
        ${model.designer_name || ''}${model.artifacts ? ` · ${model.artifacts} artifacts` : ' · Contents unknown'}
      </div>
    </button>
  `;
}

function artifactLine(artifact) {
  const traits = [artifact.scale, artifact.supported ? 'Supported' : 'Unsupported', artifact.hollow ? 'Hollow' : ''].filter(Boolean);
  return html`
    <tr>
      <td class="font-mono font-xs">${artifact.filename}</td>
      <td class="font-xs">${traits.join(', ')}</td>
      <td class="font-mono font-xs">${formatBytes(artifact.size_bytes || 0)}</td>
    </tr>
  `;
}

export async function openBrowseModel(modelId) {
  const model = await fetchCatalogModel(modelId);
  document.getElementById('model-view-title').textContent = model.name;
  render(html`
    <div id="browse-model-view">
      ${model.groups.length ? html`
        <p class="font-sm">Part of ${model.groups.map(g => html`
          <button type="button" class="browse-link browse-group-link" @click=${() => openBrowseGroup(g.id)}>${g.name}</button>
        `)}</p>
      ` : nothing}
      <p class="font-sm text-dim">In ${model.collections.map(c => c.name).join(', ')}</p>
      ${model.tags.length ? html`<p class="font-sm text-dim">Tags: ${model.tags.join(', ')}</p>` : nothing}
      ${model.artifacts.length ? html`
        <table class="table-compact">
          <thead><tr><th>Artifact</th><th>Print</th><th>Size</th></tr></thead>
          <tbody>${model.artifacts.map(artifactLine)}</tbody>
        </table>
      ` : html`<p class="text-dim font-sm">Contents unknown until this collection is peeked.</p>`}
    </div>
  `, document.getElementById('model-view-body'));
  window.openModal('model-view-modal');
}

export async function openBrowseGroup(groupId) {
  const group = await fetchModelGroup(groupId);
  document.getElementById('model-view-title').textContent = group.name;
  render(html`
    <div class="browse-grid" id="browse-group-view">
      ${group.models.map(m => card(m, () => openBrowseModel(m.id)))}
    </div>
  `, document.getElementById('model-view-body'));
  window.openModal('model-view-modal');
}

export async function loadBrowse(nextPage = 1) {
  const container = document.getElementById('browse-results');
  const box = document.getElementById('browse-search');
  if (!container) return;
  page = nextPage;
  try {
    const data = await fetchCatalogModels(box?.value.trim() || '', page);
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    render(html`
      <p class="text-dim font-sm" id="browse-summary">${data.total.toLocaleString()} models</p>
      <div class="browse-grid">${data.models.map(m => card(m, () => openBrowseModel(m.id)))}</div>
      <div class="flex gap-2 mt-2">
        <button type="button" class="btn-pill" ?disabled=${page <= 1} @click=${() => loadBrowse(page - 1)}>Previous</button>
        <span class="font-sm">Page ${page} of ${pages}</span>
        <button type="button" class="btn-pill" ?disabled=${page >= pages} @click=${() => loadBrowse(page + 1)}>Next</button>
      </div>
    `, container);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed loading models: ${err.message}</div>`, container);
  }
}

document.addEventListener('input', (event) => {
  if (event.target?.id !== 'browse-search') return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadBrowse(1), SEARCH_DELAY_MS);
});

Object.assign(window, { loadBrowse, openBrowseModel, openBrowseGroup });
