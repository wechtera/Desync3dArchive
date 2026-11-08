/**
 * Download Issues: files a download kept with a size MMF lists wrong, or couldn't get at all.
 * Each is settled by Keep, by uploading a file downloaded by hand, or by a clean download later.
 */
import { actions, registerActions } from '../actions.js';
import { fetchDownloadIssues, keepAllDownloadIssues, keepDownloadIssue, uploadDownloadIssueFile } from '../api.js';
import { html, nothing, render } from '../template.js';
import { formatBytes, formatLocalDateTime as formatDate } from '../utils.js';

const KINDS = {
  size_differs: ['status-issues', 'Kept, size differs'],
  not_downloaded: ['status-failed', 'Not downloaded'],
};
const RESOLUTIONS = { kept: 'Kept as is', uploaded: 'Replaced by an upload', downloaded: 'Downloaded cleanly later' };
// A browser saves a second download of one name as "name (1).ext". The server allows the same.
const BROWSER_COPY = / \(\d+\)$/;

let shown = 'open';
let banner = null;
const working = new Set();
const notes = new Map();

export function issueBadge(kind) {
  const [css, label] = KINDS[kind] || ['status-failed', kind];
  return html`<span class="status-badge ${css}">${label}</span>`;
}

function splitName(name) {
  const dot = name.lastIndexOf('.');
  return dot > 0 ? [name.slice(0, dot), name.slice(dot).toLowerCase()] : [name, ''];
}

export function sameName(expected, picked) {
  const [stem, ext] = splitName(picked);
  return `${stem.replace(BROWSER_COPY, '')}${ext}`.toLowerCase() === expected.toLowerCase();
}

/** Shows the open count on the Reconcile tab and the Download Issues tab, or hides it at zero. */
export function showIssueCount(count) {
  document.querySelectorAll('[data-issue-count]').forEach((el) => {
    el.textContent = count ? String(count) : '';
    el.hidden = !count;
  });
}

function bytes(count) {
  return count == null ? 'an unknown size' : `${formatBytes(count)} (${count.toLocaleString()} bytes)`;
}

function sizes(issue) {
  if (issue.kind === 'size_differs') {
    return html`MMF lists ${bytes(issue.listed_bytes)}, and both tries delivered ${bytes(issue.received_bytes)}.`;
  }
  return issue.listed_bytes ? html`MMF lists ${bytes(issue.listed_bytes)}.` : nothing;
}

function actionButtons(issue) {
  const busy = working.has(issue.id);
  const [, ext] = splitName(issue.filename);
  return html`
    <div class="issue-actions">
      ${issue.kind === 'size_differs' && issue.stored
        ? html`<button type="button" class="btn-pill" ?disabled=${busy} @click=${() => keepIssue(issue)}>Keep as is</button>`
        : nothing}
      <button type="button" class="btn-pill btn-action-queue" ?disabled=${busy}
        @click=${(event) => event.currentTarget.nextElementSibling.click()}>Upload file</button>
      <input type="file" class="issue-file-input" hidden accept=${ext || nothing} @change=${(event) => pickFile(issue, event.target)}>
    </div>
  `;
}

function issueCard(issue) {
  const note = notes.get(issue.id);
  return html`
    <article class="card issue-card" data-issue-id=${issue.id}>
      <div class="issue-head">
        ${issueBadge(issue.kind)}
        <span class="text-dim font-xs">Found ${formatDate(issue.found_at)}</span>
      </div>
      <div class="issue-path">
        <span class="text-dim">${issue.creator_name}</span>
        <span class="text-dim" aria-hidden="true">›</span>
        <button type="button" class="link-button" @click=${() => actions.openModelDetailModal(issue.model_id)}>${issue.model_name}</button>
        <span class="text-dim" aria-hidden="true">›</span>
        <span class="font-mono issue-filename">${issue.filename}</span>
      </div>
      <p class="font-sm issue-sizes">${sizes(issue)}</p>
      ${issue.stored ? html`<p class="font-xs text-dim issue-stored">Stored at <span class="font-mono">${issue.vault_path}</span></p>` : nothing}
      ${issue.kind === 'size_differs' && !issue.stored && !issue.resolved_at
        ? html`<p class="font-xs text-dim issue-moving">Moving to the NAS. Keep as is appears once it's stored.</p>` : nothing}
      <div class="issue-links font-sm">
        ${issue.model_url ? html`<a href=${issue.model_url} target="_blank" rel="noopener noreferrer">Model page on MMF</a>` : nothing}
        ${issue.download_url ? html`<a href=${issue.download_url} target="_blank" rel="noopener noreferrer">Download this file from MMF</a>` : nothing}
      </div>
      <details class="issue-detail">
        <summary class="font-xs text-dim">What happened</summary>
        <pre class="job-detail-error">${issue.detail}</pre>
      </details>
      ${issue.resolved_at
        ? html`<p class="text-dim font-xs">${RESOLUTIONS[issue.resolution] || issue.resolution} · ${formatDate(issue.resolved_at)}</p>`
        : actionButtons(issue)}
      ${note ? html`<p class="font-sm ${note.ok ? 'text-muted' : 'text-alert'}" role="status">${note.text}</p>` : nothing}
    </article>
  `;
}

let items = [];
let models = [];
const workingModels = new Set();

// A model with several open issues, with Keep All for the files already on the NAS.
function modelSummary(model) {
  const busy = workingModels.has(model.model_id);
  return html`
    <article class="card issue-model" data-model-id=${model.model_id}>
      <div class="issue-path">
        <span class="text-dim">${model.creator_name}</span>
        <span class="text-dim" aria-hidden="true">›</span>
        <button type="button" class="link-button" @click=${() => actions.openModelDetailModal(model.model_id)}>${model.model_name}</button>
        <span class="text-dim">: ${model.open} open</span>
      </div>
      ${model.scrambled ? html`<p class="font-sm issue-scrambled">MMF's size list for this model is out of order: every size it
        lists is another file's real size. The files themselves are right, so keeping them is safe.</p>` : nothing}
      <div class="issue-actions">
        ${model.keepable ? html`<button type="button" class="btn-pill issue-keep-all" ?disabled=${busy}
          @click=${() => keepAll(model)}>Keep All ${model.keepable}</button>` : nothing}
        ${model.waiting ? html`<span class="font-xs text-dim">${model.waiting} still moving to the NAS</span>` : nothing}
      </div>
    </article>
  `;
}

async function keepAll(model) {
  const files = items.filter(i => i.model_id === model.model_id && i.kind === 'size_differs' && i.stored);
  const listed = files.map(i => `${i.filename}: ${formatBytes(i.stored_bytes)}`).join('\n');
  const these = files.length === 1 ? 'this file' : `these ${files.length} files`;
  if (!confirm(`Keep ${these} of ${model.model_name} as they are? Each file's real size replaces the size MMF lists.\n\n${listed}`)) return;
  workingModels.add(model.model_id);
  paint();
  try {
    const result = await keepAllDownloadIssues(model.model_id);
    const kept = result.kept.length === 1 ? '1 file' : `${result.kept.length} files`;
    banner = `Kept ${kept} of ${model.model_name}.${result.left ? ` ${result.left} still open.` : ''}`;
  } catch (err) {
    banner = `Couldn't keep them: ${err.message}`;
  } finally {
    workingModels.delete(model.model_id);
  }
  await loadDownloadIssues();
}

function paint() {
  const list = document.getElementById('issues-list');
  if (!list) return;
  const empty = shown === 'open'
    ? 'Nothing needs attention. Files a download couldn\'t settle will show here.'
    : 'Nothing settled yet.';
  render(html`
    ${banner ? html`<p class="issues-banner font-sm" role="status">${banner}</p>` : nothing}
    ${shown === 'open' && models.length ? html`<div class="issue-models" id="issue-models">${models.map(modelSummary)}</div>` : nothing}
    ${items.length ? items.map(issueCard) : html`<div class="table-msg">${empty}</div>`}
  `, list);
}

export async function loadDownloadIssues(state = shown) {
  shown = state;
  document.getElementById('btn-issues-open')?.classList.toggle('active', state === 'open');
  document.getElementById('btn-issues-resolved')?.classList.toggle('active', state === 'resolved');
  try {
    const data = await fetchDownloadIssues(state);
    items = data.items;
    models = data.models || [];
    showIssueCount(data.open);
  } catch (err) {
    items = [];
    models = [];
    banner = `Couldn't load Download Issues: ${err.message}`;
  }
  paint();
}

function showDownloadIssues(state) {
  banner = null;
  notes.clear();
  loadDownloadIssues(state);
}

async function settle(issue, work, doneMessage) {
  working.add(issue.id);
  paint();
  try {
    await work();
    notes.delete(issue.id);
    banner = doneMessage;
  } catch (err) {
    notes.set(issue.id, { ok: false, text: err.message });
  } finally {
    working.delete(issue.id);
  }
  await loadDownloadIssues();
}

function keepIssue(issue) {
  const question = `Keep ${issue.filename} as it is? Its real size, ${bytes(issue.stored_bytes)}, replaces the size MMF lists.`;
  if (!confirm(question)) return;
  settle(issue, () => keepDownloadIssue(issue.id), `Kept ${issue.filename}.`);
}

function pickFile(issue, input) {
  const file = input.files?.[0];
  input.value = '';
  if (!file) return;
  const [, wanted] = splitName(issue.filename);
  const [, given] = splitName(file.name);
  if (given !== wanted) {
    notes.set(issue.id, { ok: false, text: `That's a ${given || 'file with no extension'}, and this entry needs a ${wanted} file.` });
    paint();
    return;
  }
  const confirmName = !sameName(issue.filename, file.name);
  if (confirmName && !confirm(`The file you picked is ${file.name}, but this entry is ${issue.filename}. Use it anyway?`)) return;
  notes.set(issue.id, { ok: true, text: `Uploading ${file.name}, ${formatBytes(file.size)}. A large file takes a minute.` });
  const done = issue.stored
    ? `Uploaded ${issue.filename}. The copy it replaced is in Pending Deletion.`
    : `Uploaded ${issue.filename} to the vault.`;
  settle(issue, () => uploadDownloadIssueFile(issue.id, file, confirmName), done);
}

registerActions({ showDownloadIssues });
