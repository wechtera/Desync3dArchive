// Download Issues and History's "Completed with issues". The Python half is tests/test_download_issues.py.
const { test, expect } = require('@playwright/test');

const KEPT = {
  id: 11, file_id: 545058, model_id: 136138, kind: 'size_differs', listed_bytes: 197131215, received_bytes: 17192220,
  detail: 'MMF lists 197,131,215 bytes for ogre-butcher-pot.chitubox, and both tries delivered 17,192,220 bytes.',
  found_at: '2026-11-08 03:01:19', resolved_at: null, resolution: null, filename: 'ogre-butcher-pot.chitubox',
  download_url: 'https://www.myminifactory.com/download/136138?downloadfile=ogre-butcher-pot.chitubox',
  model_name: 'Ogre Butchers (presupported)', model_url: 'https://www.myminifactory.com/object/3d-print-ogre-butchers-presupported-136138',
  creator_name: 'Duncan Shadow', stored_bytes: 17192220, stored: true,
};
const MISSING = {
  ...KEPT, id: 12, file_id: 527246, model_id: 133303, kind: 'not_downloaded', received_bytes: null, stored: false,
  stored_bytes: null, filename: 'chickenbear-arm-left.chitubox', model_name: 'Chickenbear', detail: 'HTTP 500 downloading file 527246',
};
const SETTLED = { ...KEPT, id: 9, resolved_at: '2026-11-08 04:00:00', resolution: 'uploaded' };

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.calls = [];
  page.open = [KEPT, MISSING];
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
  // The real poll answers from an empty test database, so it has to agree with the issues here.
  await page.route('**/api/downloads/status', async (route) => {
    const response = await route.fetch();
    return route.fulfill({ response, json: { ...(await response.json()), open_issues: page.open.length } });
  });
  await page.route((url) => url.pathname === '/api/download-issues', (route) => {
    const resolved = new URL(route.request().url()).searchParams.get('state') === 'resolved';
    return route.fulfill({ json: { items: resolved ? [SETTLED] : page.open, open: page.open.length } });
  });
  await page.route('**/api/download-issues/*/keep', (route) => {
    page.calls.push({ method: route.request().method(), url: route.request().url() });
    page.open = page.open.filter((issue) => issue.id !== KEPT.id);
    return route.fulfill({ json: { success: true } });
  });
  await page.route((url) => /\/api\/download-issues\/\d+\/file$/.test(url.pathname), (route) => {
    const request = route.request();
    page.calls.push({ method: request.method(), url: request.url(), type: request.headers()['content-type'], size: request.postDataBuffer()?.length });
    if (page.uploadFails) return route.fulfill({ status: 503, json: { error: 'The vault is offline, so nothing can be placed.' } });
    page.open = page.open.filter((issue) => !request.url().includes(`/${issue.id}/`));
    return route.fulfill({ json: { success: true } });
  });
  await page.goto('/#downloads');
  await page.evaluate(async () => {
    const { actions } = await import('/static/js/actions.js');
    actions.switchDownloadSubView('issues');
  });
  await expect(page.locator('.issue-card')).toHaveCount(2);
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

const card = (page, issue) => page.locator(`.issue-card[data-issue-id="${issue.id}"]`);

test('each issue names the model and file, with both MMF links', async ({ page }) => {
  const kept = card(page, KEPT);
  await expect(kept.locator('.issue-path')).toContainText('Duncan Shadow');
  await expect(kept.locator('.issue-path')).toContainText('Ogre Butchers (presupported)');
  await expect(kept.locator('.issue-filename')).toHaveText('ogre-butcher-pot.chitubox');
  await expect(kept).toContainText('197,131,215 bytes');
  await expect(kept).toContainText('17,192,220 bytes');
  await expect(kept.getByRole('link', { name: 'Model page on MMF' })).toHaveAttribute('href', KEPT.model_url);
  await expect(kept.getByRole('link', { name: 'Download this file from MMF' })).toHaveAttribute('href', KEPT.download_url);
  await expect(kept.locator('.status-badge')).toHaveText('Kept, size differs');
  await expect(card(page, MISSING).locator('.status-badge')).toHaveText('Not downloaded');
});

test('the count shows on the Reconcile tab and the Download Issues tab', async ({ page }) => {
  await expect(page.locator('#group-reconcile [data-issue-count]')).toHaveText('2');
  await expect(page.locator('#sub-btn-issues [data-issue-count]')).toHaveText('2');
});

test('Keep is offered only for a kept file, and settles it', async ({ page }) => {
  await expect(card(page, MISSING).getByRole('button', { name: 'Keep as is' })).toHaveCount(0);
  page.once('dialog', (dialog) => dialog.accept());
  await card(page, KEPT).getByRole('button', { name: 'Keep as is' }).click();
  await expect(page.locator('.issues-banner')).toHaveText('Kept ogre-butcher-pot.chitubox.');
  await expect(page.locator('.issue-card')).toHaveCount(1);
  expect(page.calls).toEqual([{ method: 'POST', url: expect.stringContaining(`/api/download-issues/${KEPT.id}/keep`) }]);
  await expect(page.locator('#group-reconcile [data-issue-count]')).toHaveText('1');
});

test('an upload goes as raw bytes with its name', async ({ page }) => {
  await card(page, KEPT).locator('.issue-file-input').setInputFiles({
    name: 'ogre-butcher-pot.chitubox', mimeType: 'application/octet-stream', buffer: Buffer.alloc(4096, 1),
  });
  await expect(page.locator('.issues-banner')).toContainText('The copy it replaced is in Pending Deletion');
  const [call] = page.calls;
  expect(call.method).toBe('PUT');
  expect(call.type).toBe('application/octet-stream');
  expect(call.size).toBe(4096);
  expect(new URL(call.url).searchParams.get('name')).toBe('ogre-butcher-pot.chitubox');
  expect(new URL(call.url).searchParams.get('confirm')).toBeNull();
});

test('a browser copy of the name needs no confirm', async ({ page }) => {
  await card(page, KEPT).locator('.issue-file-input').setInputFiles({
    name: 'ogre-butcher-pot (1).chitubox', mimeType: 'application/octet-stream', buffer: Buffer.alloc(10, 1),
  });
  await expect(page.locator('.issues-banner')).toBeVisible();
  expect(new URL(page.calls[0].url).searchParams.get('confirm')).toBeNull();
});

test('another kind of file is refused before anything is sent', async ({ page }) => {
  await card(page, KEPT).locator('.issue-file-input').setInputFiles({
    name: 'ogre-butcher-pot.stl', mimeType: 'application/octet-stream', buffer: Buffer.alloc(10, 1),
  });
  await expect(card(page, KEPT)).toContainText("That's a .stl, and this entry needs a .chitubox file.");
  expect(page.calls).toEqual([]);
});

test('another name asks first, and a no sends nothing', async ({ page }) => {
  const input = card(page, KEPT).locator('.issue-file-input');
  page.once('dialog', (dialog) => dialog.dismiss());
  await input.setInputFiles({ name: 'pot.chitubox', mimeType: 'application/octet-stream', buffer: Buffer.alloc(10, 1) });
  await expect(page.locator('.issue-card')).toHaveCount(2);
  expect(page.calls).toEqual([]);

  page.once('dialog', (dialog) => dialog.accept());
  await input.setInputFiles({ name: 'pot.chitubox', mimeType: 'application/octet-stream', buffer: Buffer.alloc(10, 1) });
  await expect(page.locator('.issue-card')).toHaveCount(1);
  expect(new URL(page.calls[0].url).searchParams.get('confirm')).toBe('1');
});

test("a refused upload says why and leaves the issue open", async ({ page }) => {
  page.uploadFails = true;
  await card(page, MISSING).locator('.issue-file-input').setInputFiles({
    name: 'chickenbear-arm-left.chitubox', mimeType: 'application/octet-stream', buffer: Buffer.alloc(10, 1),
  });
  await expect(card(page, MISSING)).toContainText('The vault is offline, so nothing can be placed.');
  await expect(page.locator('.issue-card')).toHaveCount(2);
});

test('settled issues say how, and offer nothing to do', async ({ page }) => {
  await page.locator('#btn-issues-resolved').click();
  const settled = card(page, SETTLED);
  await expect(settled).toContainText('Replaced by an upload');
  await expect(settled.getByRole('button', { name: /Upload|Keep/ })).toHaveCount(0);
});

test('History shows a finished job with issues, and its window leads to them', async ({ page }) => {
  await page.route('**/api/downloads/history*', (route) => route.fulfill({ json: {
    total: 1, limit: 50, offset: 0, history: [
      { id: 3, model_id: 136138, model_name: 'Ogre Butchers', creator_name: 'Duncan Shadow', status: 'completed', error_message: null, open_issues: 1 },
    ],
  } }));
  await page.route('**/api/downloads/jobs/3', (route) => route.fulfill({ json: {
    job: { id: 3, model_id: 136138, model_name: 'Ogre Butchers', creator_name: 'Duncan Shadow', status: 'completed', error_message: null, retry_count: 0 },
    attempts: [], issues: [KEPT],
  } }));
  await page.evaluate(async () => {
    const { actions } = await import('/static/js/actions.js');
    actions.switchDownloadSubView('history');
  });
  await expect(page.locator('#group-download')).toHaveClass(/active/);
  await expect(page.locator('#group-reconcile')).not.toHaveClass(/active/);
  const row = page.locator('#history-table-body tr').first();
  await expect(row.locator('.status-badge')).toHaveText('Completed with issues');
  await row.getByRole('button', { name: '1 file needs attention' }).click();
  const window = page.locator('#job-detail');
  await expect(window.locator('.job-detail-facts .status-badge')).toHaveText('Completed with issues');
  await expect(window.locator('.job-detail-issues')).toContainText('ogre-butcher-pot.chitubox');
  await window.getByRole('button', { name: 'Open Download Issues' }).click();
  await expect(page.locator('#job-detail')).toHaveCount(0);
  await expect(page.locator('#sub-dl-issues')).toBeVisible();
});
