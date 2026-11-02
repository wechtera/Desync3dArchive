import { actions } from '../actions.js';
import { fetchCreators } from '../api.js';
import { html, render } from '../template.js';

/** Fills the creator table and both creator filters from one request. */
export async function loadCreators() {
  const creators = await fetchCreators();

  const tbody = document.getElementById('creators-table-body');
  if (tbody) {
    render(creators.map(c => html`
      <tr>
        <td class="cell-name">${c.designer_name}</td>
        <td class="font-mono">${c.total_models}</td>
        <td><span class="text-bonus font-mono">${c.bonus_models}</span></td>
        <td class="font-mono">${c.supported_models}</td>
        <td class="font-mono">${c.active_months}</td>
        <td class="cell-date-range font-mono">${c.first_seen || ''} &rarr; ${c.latest_seen || ''}</td>
        <td>
          <span class="btn-link" @click=${() => filterByCreator(c.designer_name)}>
            Inspect Drops &rarr;
          </span>
        </td>
      </tr>
    `), tbody);
  }

  const options = html`
    <option value="">All Creators</option>
    ${creators.map(c => html`<option value=${c.designer_name}>${c.designer_name} (${c.total_models})</option>`)}
  `;
  for (const id of ['model-creator-filter', 'pending-creator-filter']) {
    const select = document.getElementById(id);
    if (select) render(options, select);
  }
}

/** Jumps to Releases, filtered to one creator. */
export function filterByCreator(name) {
  actions.switchTab('releases');
  const releaseSearch = document.getElementById('release-search');
  if (releaseSearch) releaseSearch.value = name;
  actions.loadReleases();
}
