/**
 * views/tags.js - The one place tags are managed. Elsewhere you can only choose an existing tag.
 */

import { html, render } from '../template.js';
import { fetchTags, postTag } from '../api.js';
import { showToast } from './settings.js';

const SEARCH_DELAY_MS = 250;
let searchTimer = null;

async function act(payload, done) {
  try {
    await postTag(payload);
    showToast(done);
    await loadTagsPage();
  } catch (err) {
    alert(err.message);
  }
}

function renameTag(name) {
  const next = prompt(`Rename ${name}. Using an existing tag merges them.`, name);
  if (next && next.trim() !== name) act({ action: 'rename', name, new_name: next }, `Renamed ${name}.`);
}

function deleteTag(name, uses) {
  if (confirm(`Delete ${name} from ${uses} items? Imports won't add it back.`)) act({ action: 'delete', name }, `Deleted ${name}.`);
}

function createTag() {
  const box = document.getElementById('tag-new');
  if (box.value.trim()) act({ action: 'create', name: box.value }, 'Tag created.');
}

function search(event) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadTagsPage(event.target.value), SEARCH_DELAY_MS);
}

export async function loadTagsPage(query = document.getElementById('tag-search')?.value || '') {
  const page = document.getElementById('tags-page');
  if (!page) return;
  try {
    const { tags, total } = await fetchTags(query);
    render(html`
      <div class="settings-panel">
        <div class="settings-panel-header">
          <div>
            <h2 class="settings-panel-title">Tags</h2>
            <div class="settings-panel-subtitle" id="tags-summary">${total.toLocaleString()} tags. Spaces become dashes, and a deleted tag stays deleted.</div>
          </div>
        </div>
        <div class="tag-toolbar">
          <input type="text" id="tag-search" placeholder="Search tags" aria-label="Search tags" spellcheck="false" .value=${query} @input=${search}>
          <input type="text" id="tag-new" placeholder="New tag" aria-label="New tag" spellcheck="false">
          <button type="button" class="btn-pill" id="btn-tag-create" @click=${createTag}>Create</button>
        </div>
        ${tags.length < total ? html`<p class="text-dim font-sm" id="tags-capped">Showing ${tags.length} of ${total.toLocaleString()}. Search to narrow it down.</p>` : ''}
        <table class="table-compact" id="tags-table">
          <thead><tr><th>Tag</th><th>Used</th><th></th></tr></thead>
          <tbody>
            ${tags.map(t => html`
              <tr>
                <td>${t.name}</td>
                <td class="font-mono font-xs">${t.uses.toLocaleString()}</td>
                <td class="tag-actions">
                  <button type="button" class="btn-pill font-xs" @click=${() => renameTag(t.name)}>Rename</button>
                  <button type="button" class="btn-pill font-xs btn-action-cancel" @click=${() => deleteTag(t.name, t.uses)}>Delete</button>
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      </div>
    `, page);
  } catch (err) {
    render(html`<div class="table-msg text-alert">Failed loading tags: ${err.message}</div>`, page);
  }
}

export function switchStashSection(section) {
  const showTags = section === 'tags';
  document.getElementById('tags-page').style.display = showTags ? 'block' : 'none';
  document.getElementById('stash-section').style.display = showTags ? 'none' : '';
  document.getElementById('stash-tab-tags').classList.toggle('active', showTags);
  document.getElementById('stash-tab-stashes').classList.toggle('active', !showTags);
  if (showTags) loadTagsPage();
}

Object.assign(window, { switchStashSection, loadTagsPage });
