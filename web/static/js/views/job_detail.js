/**
 * The full story of one download job: its error in full, and every attempt. The History table
 * cuts errors short, and these are what explain a failure.
 */
import { fetchDownloadJob } from '../api.js';
import { formatLocalDateTime as formatDate } from '../utils.js';
import { html, nothing, render } from '../template.js';

let open = null;

/** Closes the window, if one is open. */
export function closeJobDetail() {
  open?.remove();
  open = null;
}

function copyText(text, button) {
  navigator.clipboard?.writeText(text).then(() => {
    button.textContent = 'Copied';
  }).catch(() => {
    button.textContent = 'Copy failed';
  });
}

function errorBlock(text) {
  return html`
    <div class="job-detail-error-head">
      <span class="font-xs text-dim">Error</span>
      <button type="button" class="btn-pill font-xs" @click=${(event) => copyText(text, event.target)}>Copy</button>
    </div>
    <pre class="job-detail-error">${text}</pre>
  `;
}

function attemptRow(attempt) {
  return html`
    <li class="job-detail-attempt">
      <div class="font-xs text-dim">
        ${formatDate(attempt.logged_at)} · attempt ${attempt.attempt_number} · ${attempt.status}
        ${attempt.http_status ? html` · HTTP ${attempt.http_status}` : nothing}
        ${attempt.filename ? html` · <span class="font-mono">${attempt.filename}</span>` : nothing}
      </div>
      ${attempt.error_detail ? html`<pre class="job-detail-error">${attempt.error_detail}</pre>` : nothing}
    </li>
  `;
}

function body(data) {
  const { job, attempts } = data;
  return html`
    <dl class="job-detail-facts">
      <dt>Creator</dt><dd>${job.creator_name || 'Unknown'}</dd>
      <dt>Status</dt><dd>${job.status}</dd>
      <dt>Queued</dt><dd>${formatDate(job.enqueued_at)}</dd>
      <dt>Finished</dt><dd>${formatDate(job.completed_at)}</dd>
      <dt>Retries</dt><dd>${job.retry_count || 0}</dd>
    </dl>
    ${job.error_message ? errorBlock(job.error_message) : html`<p class="text-dim">No error recorded.</p>`}
    <h3 class="job-detail-subhead">Attempts for this collection</h3>
    ${attempts.length
      ? html`<ul class="job-detail-attempts">${attempts.map(attemptRow)}</ul>`
      : html`<p class="text-dim">None recorded.</p>`}
  `;
}

/** Opens the window for a job, replacing any that's open. */
export async function openJobDetail(jobId, title) {
  closeJobDetail();
  const root = document.createElement('div');
  root.className = 'job-detail-backdrop';
  root.id = 'job-detail';
  open = root;

  const paint = (content) => render(html`
    <div class="job-detail-window" role="dialog" aria-label="Download job details">
      <div class="job-detail-header">
        <h2 class="job-detail-title">${title || `Job #${jobId}`}</h2>
        <button type="button" class="stl-preview-close" aria-label="Close" @click=${closeJobDetail}>&times;</button>
      </div>
      <div class="job-detail-body">${content}</div>
    </div>
  `, root);

  // Checks where the press began, so selecting error text and letting go outside doesn't close it.
  let pressedOutside = false;
  root.addEventListener('pointerdown', (event) => { pressedOutside = event.target === root; });
  root.addEventListener('click', (event) => {
    if (event.target === root && pressedOutside) closeJobDetail();
  });

  paint(html`<p class="text-dim">Loading</p>`);
  document.body.append(root);
  try {
    const data = await fetchDownloadJob(jobId);
    if (open === root) paint(body(data));
  } catch (err) {
    if (open === root) paint(html`<p class="text-alert">${err.message}</p>`);
  }
}
