/**
 * ui/static/js/utils.js - Shared UI formatting and sanitization utilities
 */

/**
 * Parses a time from the server. SQLite's CURRENT_TIMESTAMP gives UTC with a space and no zone.
 * Python's isoformat() gives local time with a T, which the browser already reads as local.
 */
export function parseServerTime(val) {
  if (!val) return null;
  const text = String(val);
  const isSqliteUtc = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d+)?$/.test(text);
  const date = new Date(isSqliteUtc ? `${text.replace(' ', 'T')}Z` : text);
  return Number.isNaN(date.getTime()) ? null : date;
}

const pad = (n) => String(n).padStart(2, '0');

export function formatLocalDate(val) {
  const d = parseServerTime(val);
  if (!d) return val || '-';
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function formatLocalDateTime(val) {
  const d = parseServerTime(val);
  if (!d) return val || '-';
  return `${formatLocalDate(val)} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

export function formatBytes(bytes, decimals = 1) {
  if (!bytes || bytes <= 0) return '0 B';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${parseFloat((bytes / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

export function getFileExtension(filename) {
  if (!filename) return 'FILE';
  const parts = filename.split('.');
  return parts.length > 1 ? parts.pop().toUpperCase() : 'FILE';
}

export function renderStatusBadge(status) {
  const s = (status || 'not_downloaded').toLowerCase();
  switch (s) {
    case 'completed':
      return '<span class="status-badge status-completed">Archived</span>';
    case 'downloading':
    case 'staging_complete':
    case 'offloading':
      return `<span class="status-badge status-queued">${s.replace('_', ' ')}</span>`;
    case 'queued':
      return '<span class="status-badge status-queued">Queued</span>';
    case 'partial':
      return '<span class="status-badge status-partial">Partial</span>';
    case 'failed':
      return '<span class="status-badge status-failed">Failed</span>';
    default:
      return '<span class="status-badge status-not_downloaded">Not Downloaded</span>';
  }
}
