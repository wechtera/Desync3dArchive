/**
 * views/browse.js - Browsing models. Opening a model shows its groups, a group opens all its
 * models, and clicking a variant there goes back to the model view for that variant.
 */

import { formatBytes } from '../utils.js';
import { html, imageFallback, nothing, render, safeUrl } from '../template.js';
import {
  fetchCatalogModel, fetchCatalogModels, fetchModelGroup, fetchTags, postGroupTag, postModelTag, stashModelSelection,
} from '../api.js';
import { ensureRenderStyles, renderSTLViewDropdown } from './models.js';
import { showToast } from './settings.js';

const SEARCH_DELAY_MS = 250;

let page = 1;
let searchTimer = null;
let shownModel = null;
let activeTag = '';

function card(model, onClick) {
  return html`
    <button type="button" class="browse-card" data-model=${model.id} @click=${onClick}>
      <img src=${model.image_path ? `/${model.image_path}` : `/previews/${model.cover_id}/cover.jpg`} @error=${imageFallback(model.image_url)} loading="lazy" alt=${model.name}>
      <div class="browse-card-title">${model.name}</div>
      <div class="browse-card-meta">
        ${[model.designer_name, model.artifacts ? `${model.artifacts} artifacts` : ''].filter(Boolean).join(' · ')}
      </div>
    </button>
  `;
}

function stashArtifact(artifact) {
  const inArchive = artifact.archive_member_id != null;
  window.openAddToStashModal({
    item_type: inArchive ? 'archive_member' : 'file',
    model_id: artifact.collection_id,
    file_id: artifact.file_id,
    submodel_id: artifact.submodel_id || null,
    archive_member_id: artifact.archive_member_id,
    title: artifact.filename,
  });
}

function artifactLine(artifact) {
  const traits = [artifact.scale, artifact.supported ? 'Supported' : 'Unsupported', artifact.hollow ? 'Hollow' : ''].filter(Boolean);
  return html`
    <tr>
      <td class="font-mono font-xs">${artifact.filename}</td>
      <td class="font-xs">${traits.join(', ')}</td>
      <td class="font-mono font-xs">${formatBytes(artifact.size_bytes || 0)}</td>
      <td>${artifact.filename.toLowerCase().endsWith('.stl')
        ? renderSTLViewDropdown(artifact.archive_member_id != null ? 'archive_member' : 'file', artifact.archive_member_id ?? artifact.file_id, artifact.filename)
        : html`<span class="text-dim font-xs">-</span>`}</td>
      <td><button type="button" class="btn-pill font-xs artifact-stash" @click=${() => stashArtifact(artifact)}>+ Stash</button></td>
    </tr>
  `;
}

// Hints only, and the server refuses anything else: a new tag starts on the Tags page.
function tagPicker(add, done) {
  const hint = async (event) => {
    const { tags } = await fetchTags(event.target.value.trim(), true);
    render(tags.map(t => html`<option value=${t.name}></option>`), document.getElementById('tag-hints'));
  };
  const submit = async () => {
    const box = document.getElementById('tag-add');
    if (!box.value.trim()) return;
    try {
      await add(box.value.trim());
      await done();
    } catch (err) {
      alert(err.message);
    }
  };
  return html`
    <div class="tag-picker">
      <input type="text" id="tag-add" list="tag-hints" placeholder="Add a tag" aria-label="Add a tag" spellcheck="false" @input=${hint}>
      <datalist id="tag-hints"></datalist>
      <button type="button" class="btn-pill font-xs" id="tag-add-button" @click=${submit}>Add</button>
    </div>
  `;
}

async function removeTag(modelId, name) {
  await postModelTag(modelId, { name, action: 'remove' });
  await openBrowseModel(modelId);
}

function selection() {
  return {
    scale: document.getElementById('model-stash-scale')?.value || '',
    support: document.getElementById('model-stash-support')?.value || '',
  };
}

function selectedArtifacts() {
  const { scale, support } = selection();
  return shownModel.artifacts.filter(a => (!scale || a.scale === scale)
    && (!support || Boolean(a.supported) === (support === 'supported')));
}

// The table follows the dropdowns, so what you see is exactly what the button will stash.
function showSelection() {
  const picked = selectedArtifacts();
  render(picked.map(artifactLine), document.getElementById('model-artifact-rows'));
  const button = document.getElementById('model-stash-selection');
  button.textContent = `Stash ${picked.length} Artifacts`;
  button.disabled = !picked.length;
}

async function stashSelection() {
  try {
    const res = await stashModelSelection(shownModel.id, selection());
    const left = res.matched - res.added;
    showToast(`Added ${res.added} to ${res.stash}.${left ? ` ${left} couldn't be added.` : ''}`);
    window.checkHealth?.();
  } catch (err) {
    alert(`Could not stash: ${err.message}`);
  }
}

export async function openBrowseModel(modelId) {
  ensureRenderStyles();
  const model = await fetchCatalogModel(modelId);
  shownModel = model;
  document.getElementById('model-view-title').textContent = model.name;
  const facts = [
    ['Creator', model.designer_name],
    ['Release', model.release_month],
    ['Category', model.category],
    ['Size', model.total_bytes ? formatBytes(model.total_bytes) : ''],
    ['Collections', model.collections.map(c => c.name).join(', ')],
  ].filter(([, value]) => value);
  render(html`
    <div id="browse-model-view">
      <div class="model-view-head">
        <img src=${model.image_path ? `/${model.image_path}` : `/previews/${model.cover_id}/cover.jpg`} @error=${imageFallback(model.image_url)} alt=${model.name}>
        <div>
          <dl class="model-view-facts" id="model-view-facts">
            ${facts.map(([label, value]) => html`<dt>${label}</dt><dd>${value}</dd>`)}
          </dl>
          ${model.groups.length ? html`
            <p class="model-view-groups font-sm">Part of ${model.groups.map(g => html`
              <button type="button" class="btn-pill font-xs browse-group-link" @click=${() => openBrowseGroup(g.id)}>${g.name}</button>
            `)}</p>
          ` : nothing}
          ${model.url ? html`<p><a class="btn-pill font-xs model-source-link" href=${safeUrl(model.url)} target="_blank" rel="noopener noreferrer">View Source Page</a></p>` : nothing}
          <div class="model-view-tags" id="model-tags">
            ${model.tags.map(t => html`
              <span class="badge font-xs tag-chip"><button type="button" class="tag-open" @click=${() => showTag(t)}>${t}</button><button type="button" class="tag-remove" aria-label=${`Remove ${t}`} @click=${() => removeTag(model.id, t)}>×</button></span>
            `)}
          </div>
          ${tagPicker((name) => postModelTag(model.id, { name, action: 'add' }), () => openBrowseModel(model.id))}
        </div>
      </div>
      ${model.artifacts.length ? html`
        <div class="model-print-choice">
          <select id="model-stash-scale" aria-label="Scale" @change=${showSelection}>
            <option value="">All sizes</option>
            ${model.scales.map(s => html`<option value=${s}>${s}</option>`)}
          </select>
          <select id="model-stash-support" aria-label="Support" @change=${showSelection}>
            <option value="">Supported and unsupported</option>
            <option value="supported">Supported</option>
            <option value="unsupported">Unsupported</option>
          </select>
          <button type="button" class="btn-pill btn-action-primary font-xs" id="model-stash-selection" @click=${stashSelection}>Stash ${model.artifacts.length} Artifacts</button>
        </div>
        <table class="table-compact">
          <thead><tr><th>Artifact</th><th>Print</th><th>Size</th><th>Render</th><th>Stash</th></tr></thead>
          <tbody id="model-artifact-rows"></tbody>
        </table>
      ` : html`<p class="text-dim font-sm">Contents unknown until this collection is peeked.</p>`}
    </div>
  `, document.getElementById('model-view-body'));
  if (model.artifacts.length) showSelection();
  window.openModal('model-view-modal');
}

export async function openBrowseGroup(groupId) {
  const group = await fetchModelGroup(groupId);
  document.getElementById('model-view-title').textContent = group.name;
  render(html`
    ${tagPicker((name) => postGroupTag(group.id, { name }), () => showToast(`Added to ${group.models.length} models.`))}
    <div class="browse-grid" id="browse-group-view">
      ${group.models.map(m => card(m, () => openBrowseModel(m.id)))}
    </div>
  `, document.getElementById('model-view-body'));
  window.openModal('model-view-modal');
}

// Reached from the Tags page and from a model's tag chips.
export function showTag(tag) {
  activeTag = tag;
  window.closeModal('model-view-modal');
  if (tag) {
    window.switchTab('browse');
  } else {
    loadBrowse(1);
  }
}

export async function loadBrowse(nextPage = 1) {
  const container = document.getElementById('browse-results');
  const box = document.getElementById('browse-search');
  if (!container) return;
  page = nextPage;
  try {
    const creator = document.getElementById('browse-creator');
    const data = await fetchCatalogModels(box?.value.trim() || '', page, activeTag, creator?.value || '');
    if (creator && creator.options.length === 1) {
      (data.creators || []).forEach(name => creator.add(new Option(name, name)));
    }
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    render(html`
      <div class="browse-summary-row">
        <p class="text-dim font-sm" id="browse-summary">${data.total.toLocaleString()} models</p>
        ${activeTag ? html`<button type="button" class="btn-pill font-xs" id="browse-tag-filter" @click=${() => showTag('')}>Tag: ${activeTag} ×</button>` : nothing}
      </div>
      <div class="browse-grid">${data.models.map(m => card(m, () => openBrowseModel(m.id)))}</div>
      <div class="results-pager" id="browse-pager">
        <button type="button" class="btn-pill" ?disabled=${page <= 1} @click=${() => loadBrowse(page - 1)}>Previous</button>
        <span class="font-sm text-dim">Page ${page} of ${pages}</span>
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

document.getElementById('browse-creator')?.addEventListener('change', () => loadBrowse(1));

Object.assign(window, { loadBrowse, openBrowseModel, openBrowseGroup, showTag });
