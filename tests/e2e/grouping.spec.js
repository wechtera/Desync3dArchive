// LLM Grouping (E54) as a person uses it. The Python half is tests/test_grouping_routes.py and the
// other test_grouping files; here the API is stubbed so each screen state can be reached on purpose.
const { test, expect } = require('@playwright/test');

const SUGGESTION_ITEM = {
  id: 5, import_id: 3, unit: 'archive:1', title: 'Vampire Court', creator: 'Titan Forge', bucket: 'medium',
  reasons: ['The LLM rated the unit medium: Not sure the Banshee is one figure'], created_at: '2026-11-08 12:56:36',
  kind: 'grouping', models: 3, pictures: 0,
};
const APPLIED = {
  id: 3, name: 'answer-01.json', status: 'applied', export_id: 'e-20261108-120000-ab12', created_at: '2026-11-08 12:00:00',
  applied_at: '2026-11-08 12:01:00', undone_at: null, counts: { apply: 4, suggest: 1, refuse: 0 }, units: 5, tags: ['banshee'],
};
const OVERVIEW = {
  waiting: [SUGGESTION_ITEM],
  imports: [APPLIED],
  exports: [{ export_key: 'e-20261108-120000-ab12', mode: 'delta', scope: 'both', unit_count: 5, batch_count: 1, created_at: '2026-11-08 11:59:00' }],
  pictures: { pending: 2, done: 7, failed: 0, skipped: 0 },
  batch_lines: { default: 2000, min: 200, max: 20000 },
  max_answer_chars: 2031616,
};
const PREVIEW = { units: 12, batches: 2, files: 340, oversized: [], left_out: [{ unit: 'archive:9', why: 'Reviewed by a person' }] };
const UNIT_OK = {
  unit: 'archive:1', title: 'Vampire Court', creator: 'Titan Forge', outcome: 'apply', bucket: 'high', stale: false,
  problems: [], reasons: [], warnings: [], before: { models: 7, groups: 0 }, after: { models: 3, groups: 1 },
  models: [{ name: 'Ghul A', group: 'Ghuls', files: 2 }, { name: 'Ghul B', group: 'Ghuls', files: 1 }, { name: 'Banshee', group: '', files: 2 }],
  summary: 'Applies on confirm: 7 models become 3.',
};
const UNIT_REFUSED = {
  ...UNIT_OK, unit: 'archive:2', title: 'Frog Riders', outcome: 'refuse', bucket: '', models: [], after: {},
  problems: ['It changes creator. Only the app sets that: leave creator out of the answer.'],
  summary: 'Refused: 1 problem to fix with the LLM.',
};
const FIX = 'Your answer to export e-20261108-120000-ab12 needs these fixes.\nUnit archive:2 (Frog Riders):\n- It changes creator.\n';
const GOOD_REPORT = { export_id: 'e-20261108-120000-ab12', notes: [], error: '', units: [UNIT_OK], fix_request: '', import_id: 7, counts: { apply: 1, suggest: 0, refuse: 0 } };
const MIXED_REPORT = { ...GOOD_REPORT, units: [UNIT_OK, UNIT_REFUSED], fix_request: FIX, counts: { apply: 1, suggest: 0, refuse: 1 } };
const UNREADABLE = {
  export_id: '', notes: [], error: "It isn't JSON.", units: [], import_id: null, counts: { apply: 0, suggest: 0, refuse: 0 },
  fix_request: "The app couldn't read your answer. It isn't JSON.\n",
};
const CARD = (key, name, paths, picture, alternates = []) => ({
  key, name, category: 'Tabletop', paths, skip: false, group: name.startsWith('Ghul') ? 'Ghuls' : '', tags: [],
  flags: [name === 'Banshee' ? 'Suggested: Body and head are one figure' : 'Suggested'], picture, picture_alternates: alternates, picture_confidence: 'high',
});
const VIEW = {
  release: '1', title: 'Vampire Court', peeked_at: null, reviewed: false, changed: true, locked: [], settled: [],
  folders: ['1/a.zip', '1/a.zip/Ghuls', '1/a.zip/Banshee'],
  files: ['1/a.zip/Ghuls/Ghul_A.stl', '1/a.zip/Ghuls/Ghul_B.stl', '1/a.zip/Banshee/Banshee_Body.stl', '1/a.zip/Banshee/Banshee_Head.stl'],
  cards: [
    CARD('1', 'Ghul A', ['1/a.zip/Ghuls/Ghul_A.stl'], 'i1', ['i3']),
    CARD('1000000000001', 'Ghul B', ['1/a.zip/Ghuls/Ghul_B.stl'], 'i2'),
    CARD('1000000000002', 'Banshee', ['1/a.zip/Banshee/Banshee_Body.stl', '1/a.zip/Banshee/Banshee_Head.stl'], ''),
  ],
  suggestion: {
    id: 5, bucket: 'medium', kind: 'grouping', pictures: [],
    reasons: ['The LLM rated the unit medium: Not sure the Banshee is one figure'],
    images: [
      { id: 'i1', name: 'Ghul_A.png', path: 'a.zip/Renders/Ghul_A.png', url: '/static/missing-a.png' },
      { id: 'i2', name: 'Ghul_B.png', path: 'a.zip/Renders/Ghul_B.png', url: '/static/missing-b.png' },
      { id: 'i3', name: 'Ghuls_Group.png', path: 'a.zip/Renders/Ghuls_Group.png', url: '' },
    ],
    unplaced_note: 'Unplaced files sit on a skipped card. Move them to a model, or leave them skipped.',
  },
};

const json = (route, body, status = 200) => route.fulfill({ status, json: body });

test.beforeEach(async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  page.pageErrors = [];
  page.calls = [];
  page.report = GOOD_REPORT;
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  page.on('dialog', (dialog) => { page.calls.push({ dialog: dialog.message() }); dialog.accept(); });
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
  const record = (route) => {
    const request = route.request();
    page.calls.push({ method: request.method(), path: new URL(request.url()).pathname, body: request.postDataJSON?.() ?? null });
  };
  await page.route((url) => url.pathname === '/api/grouping', (route) => json(route, OVERVIEW));
  await page.route('**/api/grouping/preview?*', (route) => {
    const params = new URL(route.request().url()).searchParams;
    page.calls.push({ path: '/api/grouping/preview', query: Object.fromEntries(params) });
    return json(route, PREVIEW);
  });
  await page.route('**/api/grouping/exports', (route) => {
    record(route);
    return route.fulfill({
      status: 200, body: Buffer.from('PK\u0005\u0006' + '\u0000'.repeat(18)),
      headers: { 'Content-Type': 'application/zip', 'X-Export-Id': 'e-20261108-130000-cd34', 'Content-Disposition': 'attachment; filename="e-20261108-130000-cd34.zip"' },
    });
  });
  await page.route('**/api/grouping/check', (route) => { record(route); return json(route, page.report); });
  await page.route((url) => /^\/api\/grouping\/imports\/\d+\/(apply|discard|undo|tags)$/.test(url.pathname), (route) => {
    record(route);
    const step = new URL(route.request().url()).pathname.split('/').pop();
    const bodies = {
      apply: { import_id: 7, applied: [{ unit: 'archive:1' }], suggested: [], refused: [], pictures_held_back: [] },
      discard: { success: true },
      undo: { import_id: 3, restored: ['archive:1', 'archive:4'], left: [{ unit: 'archive:6', why: 'Changed since' }] },
      tags: { success: true, created: ['banshee'], added: 1 },
    };
    return json(route, bodies[step]);
  });
  await page.route('**/api/grouping/suggestions/5', (route) => json(route, structuredClone(VIEW)));
  await page.route('**/api/grouping/suggestions/5/*', (route) => {
    record(route);
    return json(route, { success: true, pictures: 2, pictures_waiting: 1 });
  });
  await page.goto('/#downloads');
  await page.evaluate(async () => {
    const { actions } = await import('/static/js/actions.js');
    actions.switchDownloadGroup('grouping');
  });
  await expect(page.locator('#grouping-preview')).toContainText('12 units');
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

test('the tab says what an export would hold and what was left out', async ({ page }) => {
  await expect(page.locator('#group-grouping')).toHaveClass(/active/);
  await expect(page.locator('#grouping-preview')).toContainText('12 units in 2 batches, 340 files.');
  await expect(page.locator('#grouping-preview')).toContainText('Left out: 1 reviewed by a person.');
  await page.locator('#grouping-mode-full').click();
  await page.locator('#grouping-reviewed').check();
  await expect.poll(() => page.calls.filter(c => c.path === '/api/grouping/preview').at(-1)?.query)
    .toMatchObject({ mode: 'full', scope: 'both', include_reviewed: '1' });
});

test('with nothing ticked there is nothing to export, and it says why', async ({ page }) => {
  await page.locator('#grouping-archives').uncheck();
  await page.locator('#grouping-libraries').uncheck();
  await expect(page.locator('#grouping-preview')).toHaveText('Pick archives, linked libraries, or both.');
  await expect(page.locator('#btn-grouping-export')).toBeDisabled();
});

test('the export saves as a zip named after the export', async ({ page }) => {
  await page.locator('#grouping-libraries').uncheck();
  await expect.poll(() => page.calls.filter(c => c.path === '/api/grouping/preview').at(-1)?.query?.scope).toBe('archives');
  const download = page.waitForEvent('download');
  await page.locator('#btn-grouping-export').click();
  expect((await download).suggestedFilename()).toBe('e-20261108-130000-cd34.zip');
  expect(page.calls.find(c => c.path === '/api/grouping/exports').body).toMatchObject({ mode: 'delta', scope: 'archives', batch_lines: 2000 });
});

test('an answer file gets a dry run, and confirming applies it', async ({ page }) => {
  await page.locator('#grouping-answer-file').setInputFiles({ name: 'answer-01.json', mimeType: 'application/json', buffer: Buffer.from('{"export_id": "x"}') });
  await expect(page.locator('#grouping-report')).toContainText('Dry run, nothing has changed yet.');
  await expect(page.locator('.grouping-unit')).toContainText('Applies on confirm: 7 models become 3.');
  expect(page.calls.find(c => c.path === '/api/grouping/check').body).toEqual({ text: '{"export_id": "x"}', name: 'answer-01.json' });
  await expect(page.locator('#grouping-fix')).toHaveCount(0);
  await page.locator('#btn-grouping-confirm').click();
  await expect(page.locator('#grouping-outcome')).toContainText('Applied 1 unit.');
  expect(page.calls.some(c => c.path === '/api/grouping/imports/7/apply')).toBe(true);
});

test('a refused unit gives a fix request to copy back to the LLM', async ({ page }) => {
  page.report = MIXED_REPORT;
  await page.locator('#grouping-answer-file').setInputFiles({ name: 'answer.json', mimeType: 'application/json', buffer: Buffer.from('{}') });
  await expect(page.locator('.grouping-unit[data-unit="archive:2"]')).toContainText('It changes creator.');
  await expect(page.locator('#grouping-fix-text')).toHaveText(FIX);
  await page.locator('#btn-grouping-copy-fix').click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(FIX);
  await expect(page.locator('#grouping-answer')).toContainText('Refused units are left as they are.');
});

test('a pasted reply that cannot be read is explained, with nothing to confirm', async ({ page }) => {
  page.report = UNREADABLE;
  await page.locator('.grouping-paste summary').click();
  await page.locator('#grouping-paste').fill('Sure! Here are your groups: Ghul A, Ghul B.');
  await page.locator('#btn-grouping-check-paste').click();
  await expect(page.locator('#grouping-report [role="alert"]')).toHaveText("It isn't JSON.");
  await expect(page.locator('#grouping-fix-text')).toContainText("couldn't read your answer");
  await expect(page.locator('#btn-grouping-confirm')).toHaveCount(0);
  expect(page.calls.find(c => c.path === '/api/grouping/check').body.name).toBe('pasted answer');
});

test('a waiting suggestion opens in the review editor and accepting sends the cards and pictures', async ({ page }) => {
  await page.locator('.grouping-suggestion .grouping-open').click();
  const modal = page.locator('#library-review-modal');
  await expect(modal.locator('#review-suggestion')).toContainText('Suggested by an LLM');
  await expect(modal.locator('#review-suggestion')).toContainText('Not sure the Banshee is one figure');
  await expect(modal.locator('#review-concerns')).toContainText('The LLM says: Body and head are one figure');
  const ghulA = modal.locator('.review-card[data-card="Ghul A"]');
  await expect(ghulA.locator('.pick-chosen')).toHaveAttribute('title', 'Ghul_A.png');
  await ghulA.getByRole('button', { name: 'Use Ghuls_Group.png' }).click();
  await expect(ghulA).toContainText('Fetched from the NAS once you accept.');
  await modal.locator('#btn-suggestion-accept').click();
  await expect(modal).not.toBeVisible();
  const accept = page.calls.find(c => c.path === '/api/grouping/suggestions/5/accept');
  expect(accept.body.cards.map(c => c.name)).toEqual(['Ghul A', 'Ghul B', 'Banshee']);
  expect(accept.body.pictures).toEqual({ '1/a.zip/Ghuls/Ghul_A.stl': 'i3', '1/a.zip/Ghuls/Ghul_B.stl': 'i2' });
});

test('rejecting asks first, then keeps today\'s grouping', async ({ page }) => {
  await page.locator('.grouping-suggestion .grouping-open').click();
  await page.locator('#btn-suggestion-reject').click();
  await expect(page.locator('#library-review-modal')).not.toBeVisible();
  expect(page.calls.some(c => c.dialog?.startsWith('Reject the suggestion'))).toBe(true);
  expect(page.calls.some(c => c.path === '/api/grouping/suggestions/5/reject')).toBe(true);
});

test('undo asks first and says what it left alone', async ({ page }) => {
  await page.locator('.grouping-import .grouping-undo').click();
  expect(page.calls.some(c => c.dialog?.startsWith('Undo answer-01.json?'))).toBe(true);
  await expect(page.locator('#settings-toast')).toContainText('Restored 2 units. 1 unit was changed since, so it was left alone.');
});

test('suggested tags are only created once ticked and approved', async ({ page }) => {
  const row = page.locator('.grouping-import[data-import="3"]');
  const create = row.getByRole('button', { name: 'Create and Add' });
  await expect(create).toBeDisabled();
  await row.getByLabel('banshee').check();
  await create.click();
  await expect.poll(() => page.calls.find(c => c.path === '/api/grouping/imports/3/tags')?.body).toEqual({ approved: ['banshee'] });
});

test('pictures waiting for the NAS are counted', async ({ page }) => {
  await expect(page.locator('#grouping-pictures')).toContainText('2 waiting');
});

test('a pictures-only suggestion keeps the grouping and sends just the pictures', async ({ page }) => {
  const view = structuredClone(VIEW);
  view.cards = view.cards.map(card => ({ ...card, flags: [], picture: undefined }));
  view.suggestion = {
    ...view.suggestion, kind: 'pictures', reasons: ["Pictures the LLM wasn't sure of"],
    pictures: [{ model_id: 1000000000002, name: 'Banshee', image: 'i3', alternates: [{ short: 'i1' }], confidence: 'medium', reason: 'Group shot only', current: '' }],
  };
  await page.route('**/api/grouping/suggestions/5', (route) => json(route, view));
  await page.locator('.grouping-suggestion .grouping-open').click();
  const panel = page.locator('#review-suggestion');
  await expect(panel).toContainText("The grouping is today's.");
  await expect(page.locator('.review-card .pick-row')).toHaveCount(0);
  const item = panel.locator('.review-suggestion-picture[data-model="1000000000002"]');
  await expect(item.locator('.pick-chosen')).toHaveAttribute('title', 'Ghuls_Group.png');
  await item.getByRole('button', { name: 'Use Ghul_A.png' }).click();
  await page.locator('#btn-suggestion-accept').click();
  const accept = page.calls.find(c => c.path === '/api/grouping/suggestions/5/accept');
  expect(accept.body).toEqual({ cards: null, pictures: { 1000000000002: 'i1' } });
});
