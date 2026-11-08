/**
 * LLM Grouping (E54): send collections to an LLM to group, check its answer, and apply it.
 *
 * Nothing changes until the dry run is confirmed. Anything the checks doubt waits in the review
 * editor for a person, and every applied import can be undone.
 */
import { registerActions } from '../actions.js';
import {
  applyGroupingImport,
  approveGroupingTags,
  checkGroupingAnswer,
  discardGroupingImport,
  downloadGroupingExport,
  fetchGrouping,
  fetchGroupingPreview,
  reviewGroupingUnit,
  undoGroupingImport,
} from '../api.js';
import { html, nothing, render } from '../template.js';
import { formatLocalDateTime as formatDate } from '../utils.js';
import { openSuggestionReview } from './libraries.js';
import { showToast } from './settings.js';

const OUTCOMES = {
  apply: ['status-completed', 'Applies on confirm'],
  suggest: ['status-issues', 'Waits for you'],
  refuse: ['status-failed', 'Refused'],
};
const IMPORT_STATES = {
  checked: 'Waiting for you to confirm',
  applied: 'Applied',
  undone: 'Undone',
  discarded: 'Discarded',
};
// What a person may pick per unit before confirming. A unit the checks sent to review can't jump
// straight in; the server refuses that too. A refused unit has no choice: it's left as it is.
const CHOICES = {
  apply: [['apply', 'Apply now'], ['wait', 'Wait for me'], ['skip', "Skip, keep today's"]],
  suggest: [['wait', 'Wait for me'], ['skip', "Skip, keep today's"]],
};
const DEFAULT_CHOICE = { apply: 'apply', suggest: 'wait' };
const PREVIEW_DELAY_MS = 300;

let overview = null;
let loadError = '';
const form = { mode: 'delta', archives: true, libraries: true, includeReviewed: false, batchLines: 1000 };
let preview = null;
let previewError = '';
let previewTimer = null;
let report = null;
let outcome = null;
let undoOutcome = null;
let choices = new Map();
let pasted = '';
let busy = false;
const tagPicks = new Map();

const plural = (count, word, many = `${word}s`) => `${count.toLocaleString()} ${count === 1 ? word : many}`;
// Reasons arrive as separate phrases, some without a full stop.
const sentences = (lines) => lines.map(line => (/[.!?]$/.test(line) ? line : `${line}.`)).join(' ');

function scope() {
  if (form.archives && form.libraries) return 'both';
  return form.archives ? 'archives' : form.libraries ? 'libraries' : '';
}

function badge([css, label]) {
  return html`<span class="status-badge ${css}">${label}</span>`;
}

// Export ---------------------------------------------------------------------------------------

function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(loadPreview, PREVIEW_DELAY_MS);
}

async function loadPreview() {
  if (!scope()) {
    preview = null;
    previewError = 'Pick archives, linked libraries, or both.';
    paint();
    return;
  }
  try {
    preview = await fetchGroupingPreview({ ...form, scope: scope() });
    previewError = '';
  } catch (err) {
    preview = null;
    previewError = err.message;
  }
  paint();
}

function setForm(field, value) {
  form[field] = value;
  schedulePreview();
  paint();
}

function previewLine() {
  if (previewError) return html`<p class="font-sm text-alert" id="grouping-preview" role="status">${previewError}</p>`;
  if (!preview) return html`<p class="font-sm text-dim" id="grouping-preview">Counting...</p>`;
  const reviewed = preview.left_out.filter(u => u.why.startsWith('Reviewed')).length;
  const unchanged = preview.left_out.filter(u => u.why.startsWith('No change')).length;
  const leftOut = [reviewed && `${reviewed} reviewed by a person`, unchanged && `${unchanged} unchanged since their last answer`].filter(Boolean);
  return html`
    <p class="font-sm" id="grouping-preview" role="status">
      ${preview.units ? html`<strong>${plural(preview.units, 'unit')}</strong> in ${plural(preview.batches, 'batch', 'batches')}, ${plural(preview.files, 'file')}.`
        : html`<strong>Nothing to send.</strong>`}
      ${leftOut.length ? html`<span class="text-dim">Left out: ${leftOut.join(', ')}.</span>` : nothing}
    </p>
    ${preview.oversized.length ? html`
      <p class="font-xs text-dim">${preview.oversized.length === 1 ? '1 unit is' : `${preview.oversized.length} units are`} larger than one batch, so each goes in a batch of its own.</p>
    ` : nothing}
  `;
}

async function downloadExport() {
  busy = true;
  paint();
  try {
    const { blob, filename } = await downloadGroupingExport({
      mode: form.mode, scope: scope(), include_reviewed: form.includeReviewed, batch_lines: Number(form.batchLines),
    });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = filename;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 60000);
    showToast(`Saved ${filename}. Give the whole zip to the LLM.`);
    await loadGrouping();
  } catch (err) {
    alert(`Export failed: ${err.message}`);
  } finally {
    busy = false;
    paint();
  }
}

function exportPanel() {
  const segment = (mode, label) => html`
    <button type="button" class=${`segmented-btn ${form.mode === mode ? 'active' : ''}`} id=${`grouping-mode-${mode}`}
      @click=${() => setForm('mode', mode)}>${label}</button>`;
  const recent = overview?.exports || [];
  return html`
    <section class="card grouping-panel" id="grouping-export">
      <h3 class="grouping-step">1. Export</h3>
      <p class="font-sm text-dim">A zip of file lists for the LLM to group, with its instructions inside. It holds names and sizes only, never the files.</p>
      <div class="grouping-row">
        <div class="segmented-control" role="group" aria-label="What to send">
          ${segment('delta', 'Changed since the last answer')}
          ${segment('full', 'Everything')}
        </div>
      </div>
      <div class="grouping-row font-sm">
        <label><input type="checkbox" id="grouping-archives" .checked=${form.archives} @change=${e => setForm('archives', e.target.checked)}> Archives (MMF and imported)</label>
        <label><input type="checkbox" id="grouping-libraries" .checked=${form.libraries} @change=${e => setForm('libraries', e.target.checked)}> Linked libraries</label>
        <label><input type="checkbox" id="grouping-reviewed" .checked=${form.includeReviewed} @change=${e => setForm('includeReviewed', e.target.checked)}> Include ones a person reviewed</label>
      </div>
      <div class="grouping-row font-sm">
        <label>Batch size
          <input type="number" class="grouping-lines" id="grouping-lines" min=${overview?.batch_lines.min || 200} max=${overview?.batch_lines.max || 20000}
            step="100" .value=${String(form.batchLines)} @change=${e => setForm('batchLines', e.target.value)}> lines
        </label>
        <span class="text-dim font-xs">About 40,000 tokens a batch at 1,000. Go smaller for an LLM with less room.</span>
      </div>
      ${previewLine()}
      <button type="button" class="btn-pill btn-action-primary" id="btn-grouping-export"
        ?disabled=${busy || !preview?.units} @click=${downloadExport}>Download Export</button>
      ${form.includeReviewed ? html`<p class="font-xs text-dim">Answers for reviewed units only ever wait for you in review.</p>` : nothing}
      ${recent.length ? html`
        <details class="grouping-recent">
          <summary class="font-xs text-dim">Recent exports</summary>
          <ul class="font-xs">
            ${recent.map(e => html`<li><span class="font-mono">${e.export_key}</span> ${e.mode}, ${e.scope}: ${plural(e.unit_count, 'unit')} in ${plural(e.batch_count, 'batch', 'batches')}, ${formatDate(e.created_at)}</li>`)}
          </ul>
        </details>
      ` : nothing}
    </section>
  `;
}

// Answer ---------------------------------------------------------------------------------------

async function check(text, name) {
  if (!text.trim()) return;
  busy = true;
  outcome = null;
  choices = new Map();
  paint();
  try {
    report = await checkGroupingAnswer(text, name);
    await loadGrouping();
  } catch (err) {
    report = null;
    alert(`Couldn't check it: ${err.message.includes('413') || err.message.includes('too large') ? 'the file is too large. Upload one batch\'s answer at a time.' : err.message}`);
  } finally {
    busy = false;
    paint();
  }
}

async function pickAnswer(input) {
  const file = input.files?.[0];
  input.value = '';
  if (file) await check(await file.text(), file.name);
}

async function copyFixRequest() {
  const text = report?.fix_request || '';
  try {
    await navigator.clipboard.writeText(text);
    showToast('Fix request copied. Paste it to the LLM in the same chat.');
  } catch (_) {
    const pre = document.getElementById('grouping-fix-text');
    if (pre) window.getSelection().selectAllChildren(pre);
    showToast('Copy the selected text, then paste it to the LLM.');
  }
}

async function confirmImport(importId, picked = {}) {
  busy = true;
  paint();
  try {
    outcome = await applyGroupingImport(importId, picked);
    report = null;
    choices = new Map();
    showToast(outcomeLine(outcome));
    await loadGrouping();
  } catch (err) {
    alert(`Apply failed: ${err.message}`);
  } finally {
    busy = false;
    paint();
  }
}

async function discardImport(importId) {
  try {
    await discardGroupingImport(importId);
    if (report?.import_id === importId) report = null;
    await loadGrouping();
  } catch (err) {
    alert(err.message);
  }
}

const choiceOf = (unit) => choices.get(unit.unit) || DEFAULT_CHOICE[unit.outcome] || 'skip';

function choose(unit, value) {
  if (value === DEFAULT_CHOICE[unit.outcome]) choices.delete(unit.unit); else choices.set(unit.unit, value);
  paint();
}

function reviewAllMyself() {
  for (const unit of report.units) {
    if (unit.outcome === 'apply' && !unit.filed && choiceOf(unit) === 'apply') choices.set(unit.unit, 'wait');
  }
  paint();
}

function chosenCounts() {
  const counts = { apply: 0, wait: 0, skip: 0, review: 0 };
  for (const unit of report.units) {
    if (unit.filed) counts.review += 1;
    else if (CHOICES[unit.outcome]) counts[choiceOf(unit)] += 1;
  }
  return counts;
}

// Files just this unit as waiting and opens it, leaving the rest of the answer to confirm.
async function reviewNow(unit) {
  busy = true;
  paint();
  try {
    const result = await reviewGroupingUnit(report.import_id, unit.unit);
    unit.filed = result.suggestion_id;
    choices.delete(unit.unit);
    await loadGrouping();
    openSuggestionReview(result.suggestion_id, loadGrouping);
  } catch (err) {
    alert(`Couldn't open it for review: ${err.message}`);
  } finally {
    busy = false;
    paint();
  }
}

function choiceCell(unit) {
  const options = CHOICES[unit.outcome];
  if (!options) return html`<span class="font-xs text-dim">Left as it is</span>`;
  if (unit.filed) return html`<span class="font-xs grouping-filed">In review, under Waiting for you</span>`;
  const current = choiceOf(unit);
  return html`
    <select class="grouping-choice font-xs" aria-label=${`Your choice for ${unit.title}`} @change=${e => choose(unit, e.target.value)}>
      ${options.map(([value, label]) => html`<option value=${value} ?selected=${value === current}>${label}</option>`)}
    </select>
    ${report.import_id ? html`<button type="button" class="btn-pill font-xs grouping-review-now" ?disabled=${busy}
      @click=${() => reviewNow(unit)}>Review Now</button>` : nothing}
  `;
}

// The lead reason shows; the rest, often one per model, fold away so a row stays short.
function reasonsView(unit) {
  const [lead, ...more] = unit.reasons;
  const folded = [...more, ...unit.warnings];
  const label = unit.outcome === 'suggest' ? 'Why it waits' : 'Notes';
  return html`
    ${unit.problems.length ? html`<ul class="grouping-reasons font-xs">${unit.problems.map(p => html`<li class="text-alert">${p}</li>`)}</ul>` : nothing}
    ${lead ? html`<p class="grouping-lead font-xs">${lead}</p>` : nothing}
    ${folded.length ? html`
      <details class="grouping-why"><summary class="font-xs text-dim">${label}: ${folded.length} more</summary>
        <ul class="grouping-reasons font-xs">
          ${more.map(r => html`<li>${r}</li>`)}
          ${unit.warnings.map(w => html`<li class="text-dim">${w}</li>`)}
        </ul>
      </details>
    ` : nothing}
  `;
}

function unitRow(unit) {
  return html`
    <tr class="grouping-unit" data-unit=${unit.unit}>
      <td>${unit.title}<div class="font-xs text-dim">${unit.creator}</div></td>
      <td>${badge(OUTCOMES[unit.outcome] || OUTCOMES.refuse)}</td>
      <td>${choiceCell(unit)}</td>
      <td class="font-sm">
        ${unit.summary}
        ${reasonsView(unit)}
        ${unit.models?.length ? html`
          <details><summary class="font-xs text-dim">Models after</summary>
            <ul class="font-xs">${unit.models.map(m => html`<li>${m.name}${m.group ? html` <span class="text-dim">in ${m.group}</span>` : nothing}: ${plural(m.files, 'file')}</li>`)}</ul>
          </details>
        ` : nothing}
      </td>
    </tr>
  `;
}

function choicesBar() {
  if (!report.import_id) return nothing;
  const chosen = chosenCounts();
  const changed = choices.size > 0;
  const holdable = report.units.some(u => u.outcome === 'apply' && !u.filed && choiceOf(u) === 'apply');
  return html`
    <div class="grouping-choices font-sm" id="grouping-choices">
      <span id="grouping-chosen" role="status">
        ${changed ? 'With your choices: ' : 'On confirm: '}${chosen.apply} ${chosen.apply === 1 ? 'applies' : 'apply'} now,
        ${chosen.wait} ${chosen.wait === 1 ? 'waits' : 'wait'} for you, ${chosen.skip} skipped.${chosen.review ? ` ${chosen.review} already in review.` : ''}
      </span>
      <button type="button" class="btn-pill font-xs" id="btn-grouping-review-all" ?disabled=${!holdable || busy} @click=${reviewAllMyself}>Review All Myself</button>
      ${changed ? html`<button type="button" class="btn-pill font-xs" id="btn-grouping-reset-choices" ?disabled=${busy}
        @click=${() => { choices = new Map(); paint(); }}>Use the Checks' Choices</button>` : nothing}
    </div>
  `;
}

function reportView() {
  if (!report) return nothing;
  const { counts } = report;
  const appliesNow = report.import_id ? chosenCounts().apply : 0;
  return html`
    <div class="grouping-report" id="grouping-report">
      ${report.error ? html`<p class="text-alert font-sm" role="alert">${report.error}</p>` : html`
        <p class="font-sm" role="status">
          <strong>Dry run, nothing has changed yet.</strong>
          ${counts.apply} ${counts.apply === 1 ? 'applies' : 'apply'} on confirm,
          ${counts.suggest} will wait for you in review, ${counts.refuse} refused.
        </p>
      `}
      ${(report.notes || []).length ? html`<p class="font-xs text-dim">${report.notes.join(' ')}</p>` : nothing}
      ${report.units.length ? html`
        ${choicesBar()}
        <table class="table-compact grouping-units">
          <thead><tr><th>Unit</th><th>Result</th><th>Your choice</th><th>What happens</th></tr></thead>
          <tbody>${report.units.map(unitRow)}</tbody>
        </table>
      ` : nothing}
      ${report.fix_request ? html`
        <div class="grouping-fix" id="grouping-fix">
          <div class="job-detail-error-head">
            <span class="font-sm">Some of this needs fixing by the LLM. Paste this to it in the same chat, then check its corrected answer here.</span>
            <button type="button" class="btn-pill font-xs" id="btn-grouping-copy-fix" @click=${copyFixRequest}>Copy Fix Request</button>
          </div>
          <pre class="job-detail-error" id="grouping-fix-text">${report.fix_request}</pre>
        </div>
      ` : nothing}
      ${report.import_id ? html`
        <div class="flex gap-2 mt-2">
          <button type="button" class="btn-pill btn-action-primary" id="btn-grouping-confirm" ?disabled=${busy}
            @click=${() => confirmImport(report.import_id, Object.fromEntries(choices))}>${appliesNow ? 'Confirm and Apply' : 'Confirm'}</button>
          <button type="button" class="btn-pill btn-action-cancel" id="btn-grouping-discard" ?disabled=${busy}
            @click=${() => discardImport(report.import_id)}>Discard</button>
        </div>
        ${counts.refuse ? html`<p class="font-xs text-dim">Refused units are left as they are. Fix them with the LLM and check its answer again.</p>` : nothing}
      ` : nothing}
    </div>
  `;
}

function outcomeLine(result) {
  const waits = (count, what) => `${plural(count, what)} ${count === 1 ? 'waits' : 'wait'} for you below.`;
  return [
    result.applied.length ? `Applied ${plural(result.applied.length, 'unit')}.` : 'Nothing was applied.',
    result.suggested.length ? waits(result.suggested.length, 'unit') : '',
    result.pictures_held_back.length ? waits(result.pictures_held_back.length, 'picture') : '',
    result.filed?.length ? `${plural(result.filed.length, 'unit')} ${result.filed.length === 1 ? 'was' : 'were'} already in review.` : '',
    result.skipped?.length ? `Skipped ${plural(result.skipped.length, 'unit')}, which ${result.skipped.length === 1 ? 'keeps its' : 'keep their'} grouping and ${result.skipped.length === 1 ? 'goes' : 'go'} out in the next export.` : '',
    result.refused.length ? `${plural(result.refused.length, 'unit')} couldn't apply.` : '',
  ].filter(Boolean).join(' ');
}

const TOTAL_LABELS = [
  ['files', 'file'], ['artifacts', 'artifact'], ['archive_members', 'archive member'],
  ['downloaded_files', 'downloaded file'], ['collections', 'collection'],
];

// Catalog-wide, so a download finishing meanwhile can raise a count. Only a fall means trouble.
function totalsView(totals) {
  if (!totals) return nothing;
  const { before, after } = totals;
  const fell = TOTAL_LABELS.filter(([key]) => after[key] < before[key]);
  if (fell.length) {
    return html`<p class="text-alert font-sm grouping-totals" role="alert">
      ${fell.map(([key, word]) => `${plural(before[key], word)} became ${after[key].toLocaleString()}`).join(', ')}.
      Something was lost. Undo this answer and stop until it's understood.</p>`;
  }
  const listed = TOTAL_LABELS.map(([key, word]) => plural(after[key], word));
  return html`<p class="font-xs text-dim grouping-totals">Every file is still listed: ${listed.slice(0, -1).join(', ')} and ${listed.at(-1)}, none fewer than before.</p>`;
}

function outcomeView() {
  if (!outcome) return nothing;
  return html`
    <div class="grouping-outcome font-sm" id="grouping-outcome" role="status">
      <p>${outcomeLine(outcome)}</p>
      ${outcome.refused.length ? html`
        <ul class="font-xs">${outcome.refused.map(r => html`<li class="text-alert">${r.unit}: ${r.why}</li>`)}</ul>
      ` : nothing}
      ${totalsView(outcome.totals)}
    </div>
  `;
}

function undoOutcomeView() {
  if (!undoOutcome) return nothing;
  return html`
    <div class="grouping-outcome font-sm" id="grouping-undo-outcome" role="status">
      <p>Restored ${plural(undoOutcome.restored.length, 'unit')}.</p>
      ${undoOutcome.left.length ? html`
        <ul class="font-xs">${undoOutcome.left.map(u => html`<li><strong>${u.title || u.unit}</strong>: ${u.why}</li>`)}</ul>
      ` : nothing}
      ${totalsView(undoOutcome.totals)}
    </div>
  `;
}

function answerPanel() {
  return html`
    <section class="card grouping-panel" id="grouping-answer">
      <h3 class="grouping-step">2. Check the answer</h3>
      <p class="font-sm text-dim">Upload the LLM's answer for one batch, or paste it. The app checks every unit first and shows what would change.</p>
      <div class="grouping-row">
        <button type="button" class="btn-pill btn-action-queue" ?disabled=${busy}
          @click=${e => e.currentTarget.nextElementSibling.click()}>Choose Answer File</button>
        <input type="file" id="grouping-answer-file" hidden accept=".json,.txt,application/json,text/plain" @change=${e => pickAnswer(e.target)}>
      </div>
      <details class="grouping-paste" ?open=${!!pasted}>
        <summary class="font-sm">Or paste it</summary>
        <textarea id="grouping-paste" class="grouping-paste-box" rows="6" spellcheck="false"
          placeholder="Paste the LLM's reply. Text around the JSON is fine." .value=${pasted} @input=${e => { pasted = e.target.value; }}></textarea>
        <button type="button" class="btn-pill" id="btn-grouping-check-paste" ?disabled=${busy}
          @click=${() => check(pasted, 'pasted answer')}>Check Pasted Answer</button>
      </details>
      ${busy ? html`<p class="font-sm text-dim" role="status">Working...</p>` : nothing}
      ${reportView()}
      ${outcomeView()}
    </section>
  `;
}

// Waiting and history --------------------------------------------------------------------------

function waitingPanel() {
  const items = overview?.waiting || [];
  return html`
    <section class="card grouping-panel" id="grouping-waiting">
      <h3 class="grouping-step">3. Waiting for you <span class="text-dim font-sm">(${items.length})</span></h3>
      ${items.length ? html`
        <p class="font-sm text-dim">Each opens in the review editor with the LLM's cards filled in. Accept it as is, change it first, or reject it to keep today's grouping.</p>
        <table class="table-compact">
          <thead><tr><th>Unit</th><th>Kind</th><th>LLM's confidence</th><th>Why it waits</th><th></th></tr></thead>
          <tbody>
            ${items.map(s => html`
              <tr class="grouping-suggestion" data-suggestion=${s.id}>
                <td>${s.title}<div class="font-xs text-dim">${s.creator}</div></td>
                <td class="font-sm">${s.kind === 'pictures' ? plural(s.pictures, 'picture') : plural(s.models, 'model')}</td>
                <td class="font-sm">${s.bucket}</td>
                <td class="font-xs">${sentences(s.reasons)}</td>
                <td><button type="button" class="btn-pill font-xs grouping-open" @click=${() => openSuggestionReview(s.id, loadGrouping)}>Open</button></td>
              </tr>
            `)}
          </tbody>
        </table>
      ` : html`<p class="font-sm text-dim">Nothing is waiting.</p>`}
    </section>
  `;
}

async function undoImport(item) {
  if (!confirm(`Undo ${item.name}? Each unit it applied goes back to how it was, unless someone has changed it since.`)) return;
  try {
    const result = await undoGroupingImport(item.id);
    undoOutcome = result;
    const left = result.left.length
      ? ` ${result.left.length === 1 ? '1 unit was changed since, so it was' : `${result.left.length} units were changed since, so they were`} left alone.` : '';
    showToast(`Restored ${plural(result.restored.length, 'unit')}.${left}`);
    await loadGrouping();
  } catch (err) {
    alert(`Undo failed: ${err.message}`);
  }
}

async function approveTags(item) {
  const picked = [...(tagPicks.get(item.id) || [])];
  if (!picked.length) return;
  try {
    const result = await approveGroupingTags(item.id, picked);
    showToast(`Created ${plural(result.created.length, 'tag')} and added them ${plural(result.added, 'time')}.`);
    tagPicks.delete(item.id);
    await loadGrouping();
  } catch (err) {
    alert(err.message);
  }
}

function tagChooser(item) {
  if (!item.tags.length) return nothing;
  const picks = tagPicks.get(item.id) || new Set();
  const toggle = (tag) => (event) => {
    if (event.target.checked) picks.add(tag); else picks.delete(tag);
    tagPicks.set(item.id, picks);
    paint();
  };
  return html`
    <div class="grouping-tags font-xs">
      New tags it suggested:
      ${item.tags.map(tag => html`<label><input type="checkbox" .checked=${picks.has(tag)} @change=${toggle(tag)}> ${tag}</label>`)}
      <button type="button" class="btn-pill font-xs" ?disabled=${!picks.size} @click=${() => approveTags(item)}>Create and Add</button>
    </div>
  `;
}

function importsPanel() {
  const items = overview?.imports || [];
  const pictures = overview?.pictures || {};
  return html`
    <section class="card grouping-panel" id="grouping-imports">
      <h3 class="grouping-step">Recent answers</h3>
      ${undoOutcomeView()}
      ${items.length ? html`
        <table class="table-compact">
          <thead><tr><th>Checked</th><th>Answer</th><th>State</th><th>Units</th><th></th></tr></thead>
          <tbody>
            ${items.map(item => html`
              <tr class="grouping-import" data-import=${item.id}>
                <td class="font-sm">${formatDate(item.created_at)}</td>
                <td class="font-sm">${item.name}<div class="font-xs text-dim font-mono">${item.export_id}</div></td>
                <td class="font-sm">${IMPORT_STATES[item.status] || item.status}</td>
                <td class="font-xs">${item.counts.apply} apply, ${item.counts.suggest} wait, ${item.counts.refuse} refused${tagChooser(item)}</td>
                <td>
                  ${item.status === 'checked' ? html`
                    <button type="button" class="btn-pill font-xs" @click=${() => confirmImport(item.id)}>Apply</button>
                    <button type="button" class="btn-pill font-xs btn-action-cancel" @click=${() => discardImport(item.id)}>Discard</button>
                  ` : nothing}
                  ${item.status === 'applied' ? html`
                    <button type="button" class="btn-pill font-xs btn-action-cancel grouping-undo" @click=${() => undoImport(item)}>Undo</button>
                  ` : nothing}
                </td>
              </tr>
            `)}
          </tbody>
        </table>
      ` : html`<p class="font-sm text-dim">No answers checked yet.</p>`}
      ${pictures.pending || pictures.failed ? html`
        <p class="font-xs text-dim" id="grouping-pictures">
          Pictures to fetch from the NAS or render: ${pictures.pending} waiting${pictures.failed ? `, ${pictures.failed} failed` : ''}.
          They run while the image queue is idle.
        </p>
      ` : nothing}
    </section>
  `;
}

// Screen ---------------------------------------------------------------------------------------

function paint() {
  const root = document.getElementById('grouping-root');
  if (!root) return;
  render(html`
    ${loadError ? html`<p class="text-alert font-sm" role="alert">Couldn't load LLM Grouping: ${loadError}</p>` : nothing}
    <p class="font-sm text-dim grouping-intro">
      Export file lists, give the zip to an LLM such as Claude or ChatGPT, then check its answer here. Nothing changes until you confirm,
      anything it isn't sure of waits for you, every file stays listed, and each answer can be undone.
    </p>
    <div class="grouping-grid">
      ${exportPanel()}
      ${answerPanel()}
    </div>
    ${waitingPanel()}
    ${importsPanel()}
  `, root);
}

export async function loadGrouping() {
  try {
    overview = await fetchGrouping();
    loadError = '';
  } catch (err) {
    loadError = err.message;
  }
  paint();
  // An export or an apply changes what a delta would hold, so the count is always fresh.
  loadPreview();
}

registerActions({ loadGrouping });
