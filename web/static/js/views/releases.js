import { fetchReleases, fetchAudit, fetchModels, fetchCreators, queueDownloads, dequeueDownloads } from '../api.js';
import { formatBytes, statusBadge } from '../utils.js';
import { html, imageFallback, nothing, render, stopPropagation } from '../template.js';
import { renderModelCard } from './models.js';

let currentReleasePage = 1;
const releasePageSize = 36;
let totalReleaseCount = 0;
let releaseCreatorsLoaded = false;

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'
];

function formatMonthTitle(monthStr) {
  if (!monthStr || monthStr === 'Unassigned') return 'Unassigned Drops';
  const parts = monthStr.split('-');
  if (parts.length === 2) {
    const year = parseInt(parts[0], 10);
    const monthNum = parseInt(parts[1], 10);
    if (monthNum >= 1 && monthNum <= 12) {
      return `${MONTH_NAMES[monthNum - 1]} ${year}`;
    }
  }
  return monthStr;
}

export async function populateReleaseCreatorsFilter() {
  const select = document.getElementById('release-creator-filter');
  if (!select) return;

  const currentSelection = select.value;
  try {
    const creators = await fetchCreators();
    const named = creators.filter(c => c.designer_name && c.designer_name !== 'Unknown Designer' && c.designer_name !== 'Unknown');
    render(html`
      <option value="">All Creators</option>
      ${named.map(c => html`<option value=${c.designer_name}>${c.designer_name} (${c.total_models || 0})</option>`)}
    `, select);
    if (currentSelection) select.value = currentSelection;
    releaseCreatorsLoaded = true;
  } catch (err) {
    console.error('Failed to populate release creators filter:', err);
  }
}

export function filterReleaseByCreator(creatorName) {
  const select = document.getElementById('release-creator-filter');
  const searchInput = document.getElementById('release-search');
  if (select) select.value = creatorName;
  if (searchInput) searchInput.value = '';
  resetReleasePageAndLoad();
}

export async function loadAudit(designer = '') {
  const auditPanel = document.getElementById('audit-panel');
  if (!auditPanel) return;

  const data = await fetchAudit(designer);
  if (!data.has_data) {
    auditPanel.style.display = 'none';
    return;
  }

  auditPanel.style.display = 'block';
  render(designer
    ? html`Catalog Continuity &amp; Statistical Audit &bull; <span class="text-accent">${designer}</span>`
    : html`Entire Library Continuity &amp; Statistical Audit`, document.getElementById('audit-title'));

  document.getElementById('audit-span').textContent = `${data.oldest_month} \u2192 ${data.latest_month}`;
  document.getElementById('audit-coverage').textContent = `${data.actual_months_count} / ${data.expected_months_count} Mo (${data.coverage_pct}%)`;
  document.getElementById('audit-avg').textContent = `${data.avg_models} \u00B1 ${data.std_dev}`;
  document.getElementById('audit-bonuses').textContent = `${data.total_bonus_models} items`;

  const anomaliesDiv = document.getElementById('audit-anomalies');
  
  // Compact summary for whole-library view to preserve vertical real estate
  if (!designer) {
    render(html`
      <div class="anomaly-group">
        <span class="anomaly-label text-dim">Outliers:</span>
        <span class="font-xs text-dim">${data.low_outliers.length} low volume, ${data.high_outliers.length} high volume drops. Select a creator to inspect specific gaps.</span>
      </div>
    `, anomaliesDiv);
    return;
  }

  render(html`
    ${data.missing_count > 0 ? html`
      <div class="anomaly-group">
        <span class="anomaly-label text-alert">Missing Months (${data.missing_count}):</span>
        ${data.missing_months.map(m => html`<span class="gap-pill" title="Check MMF library or studio hiatus">${m}</span>`)}
      </div>
    ` : html`
      <div class="anomaly-group">
        <span class="anomaly-label text-accent">Chronology:</span>
        <span class="ok-pill">Continuous release history: 0 calendar months missing</span>
      </div>
    `}
    ${data.low_outliers?.length ? html`
      <div class="anomaly-group">
        <span class="anomaly-label text-warn">Low Volume:</span>
        ${data.low_outliers.map(o => html`<span class="low-pill">${o.month} (${o.count} models)</span>`)}
      </div>
    ` : nothing}
    ${data.high_outliers?.length ? html`
      <div class="anomaly-group">
        <span class="anomaly-label text-info">High Volume:</span>
        ${data.high_outliers.map(o => html`<span class="high-pill">${o.month} (+${o.count} models)</span>`)}
      </div>
    ` : nothing}
  `, anomaliesDiv);
}

function queueButton(r) {
  if (r.download_status === 'completed') {
    return html`<button type="button" class="btn-pill" disabled>Archived</button>`;
  }
  if (r.download_status === 'queued') {
    return html`<button type="button" class="btn-pill btn-action-cancel" @click=${() => cancelReleaseQueue(r.id)}>Cancel Pack Queue</button>`;
  }
  return html`<button type="button" class="btn-pill btn-action-queue" @click=${() => queueReleasePack(r.id)}>Queue Entire Pack</button>`;
}

function releaseCard(r) {
  const localSrc = r.sample_model_id ? `/previews/${r.sample_model_id}/cover.jpg` : (r.sample_image_url || '');
  return html`
    <div class="card cursor-pointer" title="Click to view models in this release"
         @click=${() => openReleaseModal(r.designer_name, r.release_month)}>
      <div class="thumb-wrap">
        ${localSrc
          ? html`<img src=${localSrc} @error=${imageFallback(r.sample_image_url)} loading="lazy" alt=${r.designer_name}>`
          : html`<span class="text-dim font-xs">No Preview</span>`}
      </div>
      <div class="card-body">
        <div class="card-header-row">
          <span class="badge badge-month font-mono font-xs">${r.release_month}</span>
          <div class="card-badge-row">
            ${statusBadge(r.download_status)}
            ${r.is_likely_bundle ? html`<span class="badge badge-bundle">Patreon / Tribe</span>` : nothing}
          </div>
        </div>
        <div class="card-title">${r.designer_name}</div>
        <div class="release-stats-row font-mono">
          ${r.model_count} core models &bull; ${r.total_files} files (${formatBytes(r.total_size_bytes)})
        </div>
        <div class="footer" style="justify-content: flex-end;">
          <div class="footer-actions" @click=${stopPropagation}>${queueButton(r)}</div>
        </div>
      </div>
    </div>
  `;
}

function monthSection(monthKey, monthReleases) {
  // Exclude Unknown Designer from the monthly chips
  const creatorsInMonth = Array.from(new Set(
    monthReleases
      .map(r => r.designer_name)
      .filter(name => name && name !== 'Unknown Designer' && name !== 'Unknown')
  ));
  const totalModels = monthReleases.reduce((acc, r) => acc + (r.model_count || 0), 0);
  const totalBytes = monthReleases.reduce((acc, r) => acc + (r.total_size_bytes || 0), 0);

  return html`
    <section class="release-month-section">
      <div class="release-month-header">
        <div class="release-month-top">
          <div class="release-month-title-wrap">
            <h3 class="release-month-title">${formatMonthTitle(monthKey)}</h3>
            <span class="badge badge-month font-mono font-xs">${monthKey}</span>
          </div>
          <div class="release-month-stats font-mono">
            <span><strong>${monthReleases.length}</strong> drop${monthReleases.length === 1 ? '' : 's'}</span>
            <span>&bull;</span>
            <span><strong>${totalModels}</strong> models</span>
            <span>&bull;</span>
            <span><strong>${formatBytes(totalBytes)}</strong></span>
          </div>
        </div>

        ${creatorsInMonth.length > 0 ? html`
          <div class="release-month-creators-bar">
            <span class="release-month-creators-label">Creators in this month (${creatorsInMonth.length}):</span>
            ${creatorsInMonth.map(c => html`
              <button type="button" class="release-creator-chip" title="Filter by ${c}" @click=${() => filterReleaseByCreator(c)}>
                ${c}
              </button>
            `)}
          </div>
        ` : nothing}
      </div>

      <div class="grid releases-month-grid">
        ${monthReleases.map(releaseCard)}
      </div>
    </section>
  `;
}

export async function loadReleases(page = currentReleasePage) {
  if (!releaseCreatorsLoaded) {
    await populateReleaseCreatorsFilter();
  }

  currentReleasePage = Math.max(1, page);

  const searchInput = document.getElementById('release-search');
  const creatorSelect = document.getElementById('release-creator-filter');
  const bundleCheck = document.getElementById('bundles-only');

  const searchVal = searchInput ? searchInput.value.trim() : '';
  const selectedCreator = creatorSelect ? creatorSelect.value : '';
  const designer = selectedCreator || searchVal;
  const bundlesOnly = bundleCheck ? bundleCheck.checked : false;

  loadAudit(designer);

  const offset = (currentReleasePage - 1) * releasePageSize;
  const container = document.getElementById('releases-grid');

  if (container) {
    render(html`<div class="grid-empty-msg">Loading release drops...</div>`, container);
  }

  try {
    const data = await fetchReleases({
      designer,
      bundlesOnly,
      limit: releasePageSize,
      offset
    });

    const releases = Array.isArray(data) ? data : (data.releases || []);
    totalReleaseCount = Array.isArray(data) ? releases.length : (data.total || 0);

    if (container) {
      if (releases.length === 0) {
        render(html`<div class="grid-empty-msg">No matching monthly drops found.</div>`, container);
        updateReleasesPagination(offset, 0);
        return;
      }

      // Group into ordered months
      const monthsMap = new Map();
      for (const r of releases) {
        const monthKey = r.release_month || 'Unassigned';
        if (!monthsMap.has(monthKey)) {
          monthsMap.set(monthKey, []);
        }
        monthsMap.get(monthKey).push(r);
      }

      render(Array.from(monthsMap, ([monthKey, monthReleases]) => monthSection(monthKey, monthReleases)), container);
    }

    updateReleasesPagination(offset, releases.length);
  } catch (err) {
    if (container) {
      render(html`<div class="grid-empty-msg text-alert">Failed to load releases: ${err.message}</div>`, container);
    }
  }
}

export function resetReleasePageAndLoad() {
  loadReleases(1);
}

export function changeReleasePage(delta) {
  const totalPages = Math.ceil(totalReleaseCount / releasePageSize) || 1;
  const targetPage = currentReleasePage + delta;
  if (targetPage >= 1 && targetPage <= totalPages) {
    loadReleases(targetPage);
    const view = document.getElementById('view-releases');
    if (view) view.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function updateReleasesPagination(offset, currentCount) {
  const pageInfo = document.getElementById('releases-page-info');
  const pageNum = document.getElementById('releases-page-num');
  const prevBtn = document.getElementById('releases-prev-btn');
  const nextBtn = document.getElementById('releases-next-btn');

  const totalPages = Math.ceil(totalReleaseCount / releasePageSize) || 1;
  const start = totalReleaseCount === 0 ? 0 : offset + 1;
  const end = offset + currentCount;

  if (pageInfo) pageInfo.textContent = `Showing ${start}-${end} of ${totalReleaseCount.toLocaleString()} drops`;
  if (pageNum) pageNum.textContent = `Page ${currentReleasePage} of ${totalPages}`;
  if (prevBtn) prevBtn.disabled = currentReleasePage <= 1;
  if (nextBtn) nextBtn.disabled = currentReleasePage >= totalPages;
}

export async function openReleaseModal(designer, month) {
  document.getElementById('modal-title').textContent = `${designer} \u2014 Release: ${month}`;
  document.getElementById('release-modal').style.display = 'flex';

  const data = await fetchModels({ designer, month, limit: 150 });
  const container = document.getElementById('modal-models-grid');
  container.innerHTML = data.models.map(m => renderModelCard(m)).join('');
}

export async function queueReleasePack(releaseId) {
  await queueDownloads({ release_id: releaseId });
  window.refreshCurrentView();
}

export async function cancelReleaseQueue(releaseId) {
  const parts = releaseId.split('_');
  const data = await fetchModels({ designer: parts[0], month: parts[1], limit: 200 });
  const ids = data.models.map(m => m.id);
  if (ids.length > 0) {
    await dequeueDownloads({ model_ids: ids });
  }
  window.refreshCurrentView();
}

window.filterReleaseByCreator = filterReleaseByCreator;
window.resetReleasePageAndLoad = resetReleasePageAndLoad;
