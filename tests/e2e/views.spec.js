// Behavior of each view, with mocked API data so tests can use hostile names.
const { test, expect } = require('@playwright/test');

const HOSTILE = `Evil<img src=x onerror="window.xss=1">'"<b>`;

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
});

test.afterEach(async ({ page }) => {
  expect(await page.evaluate(() => window.xss)).toBeUndefined();
  expect(page.pageErrors).toEqual([]);
});

function mock(page, path, json) {
  return page.route(`**${path}*`, (route) => route.fulfill({ json }));
}

test.describe('creators', () => {
  const creators = [
    { designer_name: 'Plain Maker', total_models: 12, bonus_models: 1, supported_models: 5, active_months: 3, first_seen: '2024-01', latest_seen: '2026-01' },
    { designer_name: HOSTILE, total_models: 2, bonus_models: 0, supported_models: 0, active_months: 1, first_seen: null, latest_seen: null },
  ];

  test.beforeEach(async ({ page }) => {
    await mock(page, '/api/creators', creators);
    await page.goto('/#creators');
  });

  test('rows show creator stats', async ({ page }) => {
    const row = page.locator('#creators-table-body tr').first();
    await expect(row).toContainText('Plain Maker');
    await expect(row).toContainText('12');
    await expect(row).toContainText('2024-01');
  });

  test('a hostile name renders as text', async ({ page }) => {
    await expect(page.locator('#creators-table-body tr').nth(1).locator('td').first()).toHaveText(HOSTILE);
    await expect(page.locator('#creators-table-body img')).toHaveCount(0);
  });

  test('creator filters list each creator', async ({ page }) => {
    const options = page.locator('#model-creator-filter option');
    await expect(options).toHaveCount(3);
    await expect(options.first()).toHaveText('All Creators');
    await expect(options.nth(1)).toHaveAttribute('value', 'Plain Maker');
    await expect(options.nth(2)).toHaveAttribute('value', HOSTILE);
  });

  test('inspect goes to releases filtered by that creator', async ({ page }) => {
    await page.locator('#creators-table-body tr').nth(1).locator('.btn-link').click();
    await expect(page).toHaveURL(/#releases$/);
    await expect(page.locator('#release-search')).toHaveValue(HOSTILE);
  });
});

test.describe('releases', () => {
  const release = (overrides) => ({
    id: '1_2026-05', designer_name: 'Plain Maker', release_month: '2026-05', model_count: 4, total_files: 9,
    total_size_bytes: 1048576, is_likely_bundle: 1, download_status: 'not_downloaded', sample_model_id: 7,
    sample_image_url: '', ...overrides,
  });
  const releases = [
    release({}),
    release({ id: '2_2026-05', designer_name: HOSTILE, download_status: 'queued', is_likely_bundle: 0, sample_model_id: null }),
    release({ id: '3_2026-04', release_month: '2026-04', download_status: 'completed' }),
  ];

  test.beforeEach(async ({ page }) => {
    await mock(page, '/api/creators', [{ designer_name: HOSTILE, total_models: 2 }, { designer_name: 'Plain Maker', total_models: 4 }]);
    await mock(page, '/api/releases', { releases, total: 3 });
    await mock(page, '/api/audit', {
      has_data: true, oldest_month: '2024-01', latest_month: '2026-05', actual_months_count: 10, expected_months_count: 12,
      coverage_pct: 83, avg_models: 4, std_dev: 1, total_bonus_models: 2, missing_count: 1, missing_months: ['2025-02'],
      low_outliers: [{ month: '2025-03', count: 1 }], high_outliers: [{ month: '2025-04', count: 9 }],
    });
    await page.goto('/#releases');
    await expect(page.locator('.release-month-section').first()).toBeVisible();
  });

  test('releases group by month with totals', async ({ page }) => {
    await expect(page.locator('.release-month-section')).toHaveCount(2);
    const may = page.locator('.release-month-section').first();
    await expect(may.locator('.release-month-title')).toHaveText('May 2026');
    await expect(may).toContainText('2 drops');
    await expect(may.locator('.card')).toHaveCount(2);
  });

  test('buttons follow download status', async ({ page }) => {
    await expect(page.getByRole('button', { name: 'Queue Entire Pack' })).toHaveCount(1);
    await expect(page.getByRole('button', { name: 'Cancel Pack Queue' })).toHaveCount(1);
    await expect(page.getByRole('button', { name: 'Archived' })).toBeDisabled();
    await expect(page.locator('.badge-bundle').first()).toHaveText('Patreon / Tribe');
  });

  test('a hostile name renders as text', async ({ page }) => {
    await expect(page.locator('.card-title').nth(1)).toHaveText(HOSTILE);
    await expect(page.locator('#releases-grid img[src="x"]')).toHaveCount(0);
  });

  test('queueing a pack sends its release id without opening the card', async ({ page }) => {
    let body;
    await page.route('**/api/downloads/queue', async (route) => {
      body = route.request().postDataJSON();
      await route.fulfill({ json: { success: true } });
    });
    await page.getByRole('button', { name: 'Queue Entire Pack' }).click();
    await expect.poll(() => body).toEqual({ release_id: '1_2026-05' });
    await expect(page.locator('#release-modal')).toBeHidden();
  });

  test('a card opens its release, even with a hostile name', async ({ page }) => {
    await mock(page, '/api/models', { models: [], total: 0 });
    await page.locator('.card').nth(1).click();
    await expect(page.locator('#release-modal')).toBeVisible();
    await expect(page.locator('#modal-title')).toContainText(HOSTILE);
  });

  test('a creator chip filters releases', async ({ page }) => {
    await page.locator('.release-creator-chip').nth(1).click();
    await expect(page.locator('#release-creator-filter')).toHaveValue(HOSTILE);
  });

  test('a missing preview falls back to the remote image', async ({ page }) => {
    await page.unroute('**/api/releases*');
    await mock(page, '/api/releases', { releases: [release({ sample_image_url: 'http://127.0.0.1:8099/static/none.jpg' })], total: 1 });
    await page.route('**/previews/7/cover.jpg', (route) => route.fulfill({ status: 404, body: '' }));
    await page.reload();
    await expect(page.locator('#releases-grid img').first()).toHaveAttribute('src', /static\/none\.jpg/);
  });

  test('audit lists gaps for a creator', async ({ page }) => {
    await page.locator('.release-creator-chip').first().click();
    await expect(page.locator('#audit-anomalies')).toContainText('2025-02');
    await expect(page.locator('#audit-anomalies')).toContainText('2025-03 (1 models)');
    await expect(page.locator('#audit-anomalies')).toContainText('2025-04 (+9 models)');
    await expect(page.locator('#audit-title')).toContainText('Plain Maker');
  });

  test('audit stays compact for the whole library', async ({ page }) => {
    await expect(page.locator('#audit-anomalies')).toContainText('1 low volume, 1 high volume drops');
  });

  test('errors show a message', async ({ page }) => {
    await page.unroute('**/api/releases*');
    await page.route('**/api/releases*', (route) => route.fulfill({ status: 500, json: { error: `boom ${HOSTILE}` } }));
    await page.reload();
    await expect(page.locator('#releases-grid')).toContainText('Failed to load releases');
    await expect(page.locator('#releases-grid img')).toHaveCount(0);
  });
});

test.describe('tasks', () => {
  const task = (overrides) => ({
    id: 5, task_type: 'library_scrape', status: 'completed', progress_pct: 100, progress_label: 'Done',
    started_at: '2026-05-01 12:00:00', completed_at: '2026-05-01 12:01:00', ...overrides,
  });

  async function open(page, status) {
    await mock(page, '/api/tasks/status', status);
    await page.goto('/#settings');
  }

  test('the scrape dialog shows a scrape that failed quickly', async ({ page }) => {
    const label = 'MyMiniFactory asked for a login';
    let started = false;
    await page.route('**/api/scraper/start*', (route) => { started = true; return route.fulfill({ json: { task_id: 9 } }); });
    await page.route('**/api/tasks/status*', (route) => route.fulfill({
      json: {
        active_task: null,
        recent_tasks: started ? [task({ id: 9, status: 'failed', progress_pct: 5, progress_label: label })] : [],
      },
    }));
    await mock(page, '/api/tasks/9', { id: 9, log_output: 'login page' });
    await page.goto('/#downloads');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.openModal('modal-scraper'));
    await page.locator('#btn-trigger-scrape').click();

    await expect(page.locator('#scrape-modal-status')).toHaveText('FAILED');
    await expect(page.locator('#scrape-modal-summary')).toHaveText(label);
    await expect(page.locator('#scrape-modal-log')).toHaveText('login page');
    await expect(page.locator('#btn-trigger-scrape')).toBeEnabled();
    await expect(page.locator('#btn-trigger-scrape')).toHaveText('Start Library Scrape');
  });

  test('history lists tasks, with hostile labels as text', async ({ page }) => {
    await open(page, { active_task: null, recent_tasks: [task({}), task({ id: 6, task_type: 'metadata_ingest', status: 'failed', progress_label: HOSTILE })] });
    const rows = page.locator('#task-history-tbody tr');
    await expect(rows).toHaveCount(2);
    await expect(rows.first()).toContainText('Library Scrape');
    await expect(rows.first()).toContainText('COMPLETED');
    await expect(rows.nth(1)).toContainText('MMF Metadata Ingest');
    await expect(rows.nth(1).locator('.cell-label-truncate')).toHaveText(HOSTILE);
    await expect(rows.nth(1).locator('.status-failed')).toHaveCount(1);
    await expect(page.locator('#task-history-tbody img')).toHaveCount(0);
  });

  test('an empty history says so, and later fills in', async ({ page }) => {
    await open(page, { active_task: null, recent_tasks: [] });
    await expect(page.locator('#task-history-tbody')).toContainText('No recent tasks recorded.');
    await page.unroute('**/api/tasks/status*');
    await mock(page, '/api/tasks/status', { active_task: null, recent_tasks: [task({})] });
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.renderTasksView());
    await expect(page.locator('#task-history-tbody tr')).toHaveCount(1);
    await expect(page.locator('#task-history-tbody')).not.toContainText('No recent tasks recorded.');
  });

  test('scrape folders list in the ingest dialog, with hostile names as text', async ({ page }) => {
    await mock(page, '/api/scrapes/list', { scrapes: [{ folder: HOSTILE, model_count: 3 }, { folder: 'scrape_2', model_count: 0 }] });
    await open(page, { active_task: null, recent_tasks: [] });
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.openIngestModal());
    const options = page.locator('#ingest-scrape-folder-select option');
    await expect(options).toHaveCount(2);
    await expect(options.first()).toHaveAttribute('value', HOSTILE);
    await expect(options.nth(1)).toHaveText('scrape_2 (empty)');
  });

  test('no scrape folders says so', async ({ page }) => {
    await mock(page, '/api/scrapes/list', { scrapes: [] });
    await open(page, { active_task: null, recent_tasks: [] });
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.openIngestModal());
    await expect(page.locator('#ingest-scrape-folder-select')).toContainText('No scrape runs found');
  });

  test('the ingest report breaks down creators', async ({ page }) => {
    await open(page, { active_task: null, recent_tasks: [] });
    await page.evaluate(async (name) => (await import('/static/js/actions.js')).actions.renderIngestReport({
      submitted: 3, ingested: 2, creators: { [name]: { count: 2, pre_supported: 1 } },
    }), HOSTILE);
    await expect(page.locator('#report-ingested')).toHaveText('2');
    await expect(page.locator('.cell-creator-accent')).toHaveText(HOSTILE);
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.renderIngestReport({ creators: {} }));
    await expect(page.locator('#report-creators-container')).toContainText('No new creators');
  });
});

test.describe('unlinked library items', () => {
  const stash = { id: 4, name: 'Print soon', item_count: 2, is_active: 0, description: '' };
  const item = (id, isInactive) => ({
    id, item_type: 'model', model_id: id, title: `Model ${id}`, creator_name: 'Forge', model_name: `Model ${id}`,
    thumbnail_url: '', size_bytes: 10, is_inactive: isInactive,
  });

  test('a stash flags the inactive model and keeps the others plain', async ({ page }) => {
    await mock(page, '/api/stashes', { stashes: [stash] });
    await mock(page, '/api/stashes/active', { active_stash: null });
    await mock(page, '/api/stashes/4', { stash: { ...stash, items: [item(1, true), item(2, false)] } });
    await page.goto('/#stashes');
    await page.getByText('Print soon').first().click();

    const cards = page.locator('.stash-item-card');
    await expect(cards).toHaveCount(2);
    await expect(cards.nth(0).locator('.status-failed')).toHaveText('UNAVAILABLE');
    await expect(cards.nth(0).locator('.status-failed')).toHaveAttribute('title', /download, slice or print/);
    await expect(cards.nth(0)).toHaveClass(/stash-item-inactive/);
    await expect(cards.nth(1).locator('.status-failed')).toHaveCount(0);
    expect(page.pageErrors).toEqual([]);
  });

  test('the detail view explains it and hides download and stash actions', async ({ page }) => {
    await mock(page, '/api/models/detail', {
      id: 1, name: 'Golem', designer_name: 'Forge', source: 'external', is_active: 0, tags: [], files: [],
    });
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.openModelDetailModal(1));
    await expect(page.locator('#detail-unavailable-note')).toContainText('Not available for local download');
    await expect(page.getByRole('button', { name: '+ Add Model to Stash' })).toBeHidden();
    expect(page.pageErrors).toEqual([]);
  });
});

test.describe('duplicate report', () => {
  test('lists matches with hostile names as text', async ({ page }) => {
    const side = (fileId, filename, source = 'mmf') => ({
      file_id: fileId, filename, size_bytes: 1024, model_id: fileId, model_name: `Model ${fileId}`, source,
      path: source === 'mmf' ? `/vault/${fileId}/x.stl` : '/loot/x.stl', delete_blocked: source === 'mmf' ? '' : 'Linked libraries are read-only.',
    });
    const results = {
      success: true,
      summary: { exact_groups: 1, candidate_groups: 1, duplicate_models: 1, overlapping_models: 0, ignored_names: ['base'], pairs: 2 },
      pairs: [
        { kind: 'exact', first: side(1, HOSTILE), second: side(2, 'two.stl') },
        { kind: 'candidate', first: side(3, 'three.stl'), second: side(4, 'loot.stl', 'external') },
      ],
      models: [{ kind: 'duplicate', shared_files: 2, models: [{ model_id: 1, model_name: 'One' }, { model_id: 2, model_name: 'Two' }] }],
    };
    let decided = null;
    let deleted = null;
    await mock(page, '/api/reconcile/duplicates', results);
    await page.route('**/api/reconcile/duplicates/latest', (route) => route.fulfill({ json: { ...results, has_results: true } }));
    await page.route('**/api/reconcile/duplicates/decision', (route) => {
      decided = route.request().postDataJSON();
      return route.fulfill({ json: { success: true } });
    });
    await page.route('**/api/reconcile/duplicates/delete', (route) => {
      deleted = route.request().postDataJSON();
      return route.fulfill({ json: { success: true } });
    });
    page.on('dialog', (dialog) => dialog.accept());
    await page.goto('/#downloads');
    await page.locator('#group-reconcile').click();
    const card = page.locator('[data-card="duplicate_scan"]');
    await expect(card.locator('#duplicate-results-panel summary')).toContainText('2 pairs to review');
    await expect(page.locator('.duplicate-row')).toHaveCount(3);
    await expect(page.locator('.duplicate-row').nth(0)).toContainText('DUPLICATE MODEL');
    await expect(page.locator('.duplicate-row').nth(1)).toContainText('EXACT');
    await expect(page.locator('.duplicate-row').nth(1)).toContainText('/vault/1/x.stl');
    await expect(page.locator('.duplicate-row').nth(2)).toContainText('POSSIBLE');
    const blocked = page.locator('.duplicate-side[data-file="4"]');
    await expect(blocked.locator('.duplicate-delete')).toBeDisabled();
    await expect(blocked.locator('.duplicate-delete')).toHaveAttribute('title', 'Linked libraries are read-only.');
    await expect(blocked.locator('.duplicate-blocked')).toHaveText('Linked libraries are read-only.');
    await expect(page.locator('.duplicate-side[data-file="3"] .duplicate-delete')).toBeEnabled();
    await expect(page.locator('.duplicate-side[data-file="3"] .duplicate-blocked')).toHaveCount(0);
    const whole = await card.boundingBox();
    const panel = await card.locator('#duplicate-results-panel').boundingBox();
    expect(panel.width).toBeGreaterThan(whole.width - 4);
    await page.locator('.duplicate-keep').first().click();
    await expect.poll(() => decided).toEqual({ first_file_id: 1, second_file_id: 2, decision: 'keep' });
    await page.locator('.duplicate-delete').first().click();
    await expect.poll(() => deleted).toEqual({ file_id: 1, confirm: true });
    expect(await page.evaluate(() => window.xss)).toBeUndefined();
    expect(page.pageErrors).toEqual([]);
  });

  test('pending deletion restores, and purges after asking twice', async ({ page }) => {
    const bodies = [];
    let restored = null;
    let items = [
      { id: 5, filename: 'old.stl', collection: 'Pack', original_path: '/vault/old.stl', size_bytes: 1024, held: true, age_days: 20, can_purge: true },
      { id: 6, filename: 'new.stl', collection: 'Pack', original_path: '/vault/new.stl', size_bytes: 1024, held: true, age_days: 2, can_purge: false },
    ];
    await page.route('**/api/pending-deletion', (route) => route.fulfill({ json: { count: 2, size_bytes: 2048, wait_days: 14 } }));
    await page.route('**/api/pending-deletion/items', (route) => route.fulfill({ json: { success: true, online: true, wait_days: 14, items } }));
    await page.route('**/api/pending-deletion/restore', (route) => {
      restored = route.request().postDataJSON();
      items = items.filter((item) => item.id !== restored.id);
      return route.fulfill({ json: { success: true, artifacts_lost: 0, partial: false } });
    });
    await page.route('**/api/pending-deletion/purge', (route) => {
      bodies.push(route.request().postDataJSON());
      return route.fulfill({ json: { success: true, purged: 1 } });
    });
    const dialogs = [];
    page.on('dialog', (dialog) => { dialogs.push(dialog.message()); dialog.accept(); });
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('settings'));
    await expect(page.locator('#pending-deletion-status')).toContainText('2 files');
    await page.locator('#btn-purge-pending').click();
    await expect(page.locator('#pending-purge-list tbody tr')).toHaveCount(2);
    await expect(page.locator('#btn-confirm-pending-purge')).toHaveText('Delete 1 Files');
    await page.locator('[data-pending="6"] .pending-restore').click();
    await expect.poll(() => restored).toEqual({ id: 6 });
    await expect(page.locator('#pending-purge-list tbody tr')).toHaveCount(1);
    expect(dialogs).toHaveLength(0);
    await page.locator('#btn-confirm-pending-purge').click();
    await expect.poll(() => bodies.length).toBe(1);
    expect(dialogs).toHaveLength(1);
    expect(bodies[0]).toEqual({ confirm: true, ids: [5] });
  });
});

test.describe('add to stash', () => {
  const active = { id: 3, name: HOSTILE, item_count: 2, is_active: 1 };
  const stashes = [active, { id: 4, name: 'Second', item_count: 1, is_active: 0 }];
  const item = { item_type: 'model', model_id: 10, title: HOSTILE };

  async function open(page, list = stashes, current = active) {
    await mock(page, '/api/stashes', { stashes: list });
    await mock(page, '/api/stashes/active', { active_stash: current });
    await page.goto('/');
    await page.evaluate(async (context) => (await import('/static/js/actions.js')).actions.openAddToStashModal(context), item);
  }

  test('with no stashes it points to the stash manager', async ({ page }) => {
    await open(page, [], null);
    await expect(page.locator('.stash-modal-zero-gate')).toBeVisible();
    await page.getByRole('button', { name: /Go to Stash Manager/ }).click();
    await expect(page).toHaveURL(/#stashes$/);
    await expect(page.locator('#modal-add-to-stash-backdrop')).toBeHidden();
  });

  test('hostile stash names render as text, with the active one selected', async ({ page }) => {
    await open(page);
    await expect(page.locator('#stash-modal-item-title')).toHaveText(HOSTILE);
    await expect(page.locator('#btn-quick-add-stash')).toContainText(HOSTILE);
    await expect(page.locator('#stash-modal-select')).toHaveValue('3');
    await expect(page.locator('#modal-add-to-stash-backdrop img')).toHaveCount(0);
  });

  test('quick add posts the item and confirms', async ({ page }) => {
    let body;
    await page.route('**/api/stashes/3/items', async (route) => {
      body = route.request().postDataJSON();
      await route.fulfill({ status: 201, json: { success: true } });
    });
    await open(page);
    await page.locator('#btn-quick-add-stash').click();
    await expect(page.locator('#stash-modal-feedback')).toContainText('Added to');
    await expect(page.locator('#stash-modal-feedback')).toContainText(HOSTILE);
    expect(body).toMatchObject({ item_type: 'model', model_id: 10 });
  });

  test('a duplicate gives a friendly notice', async ({ page }) => {
    await page.route('**/api/stashes/4/items', (route) => route.fulfill({ status: 201, json: { already_exists: true, stash_name: 'Second' } }));
    await open(page);
    await page.locator('#stash-modal-select').selectOption('4');
    await page.getByRole('button', { name: 'Add to Stash' }).click();
    await expect(page.locator('#stash-modal-feedback')).toContainText('Already pending paint');
    await expect(page.locator('#stash-modal-feedback')).toHaveClass(/feedback-warn/);
  });

  test('creating a stash needs a name', async ({ page }) => {
    await open(page);
    await page.getByRole('button', { name: 'Create & Add', exact: true }).click();
    await expect(page.locator('#stash-modal-feedback')).toContainText('Please enter a name');
  });

  test('create and add makes the stash, then adds to it', async ({ page }) => {
    let created;
    await open(page);
    await page.route('**/api/stashes', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      created = route.request().postDataJSON();
      return route.fulfill({ status: 201, json: { stash: { id: 9, name: 'New' } } });
    });
    await page.route('**/api/stashes/9/items', (route) => route.fulfill({ status: 201, json: {} }));
    await page.locator('#stash-modal-new-name').fill('New');
    await page.getByRole('button', { name: /Make Current/ }).click();
    await expect(page.locator('#stash-modal-feedback')).toContainText('Added to');
    expect(created).toMatchObject({ name: 'New', make_active: true });
  });
});

test.describe('stashes', () => {
  const stash = (overrides) => ({ id: 3, name: HOSTILE, description: HOSTILE, item_count: 1, total_bytes: 2048, is_active: 1, ...overrides });
  const detail = {
    ...stash({}), total_items: 1, models_count: 1, files_count: 0, submodels_count: 2, archive_members_count: 3,
    items: [{ id: 21, item_type: 'file', model_id: 10, title: HOSTILE, creator_name: HOSTILE, relative_path: HOSTILE,
      file_extension: '.stl', is_supported: 1, size_bytes: 1024, thumbnail_url: '' }],
  };

  async function open(page, list = [stash({}), stash({ id: 4, name: 'Second', is_active: 0 })], item = detail) {
    await mock(page, '/api/stashes', { stashes: list });
    await mock(page, '/api/stashes/active', { active_stash: list[0] || null });
    await mock(page, '/api/stashes/3', { stash: item });
    await mock(page, '/api/stashes/4', { stash: { ...item, id: 4, name: 'Second', is_active: 0, items: [] } });
    await page.goto('/#stashes');
  }

  test('sidebar and workspace show names as text', async ({ page }) => {
    await open(page);
    await expect(page.locator('.stash-nav-card')).toHaveCount(2);
    await expect(page.locator('.stash-nav-title').first()).toHaveText(HOSTILE);
    await expect(page.locator('.stash-title-accent')).toHaveText(HOSTILE);
    await expect(page.locator('.stash-item-card .card-title')).toHaveText(HOSTILE);
    await expect(page.locator('#stash-detail-workspace img[src="x"]')).toHaveCount(0);
  });

  test('metrics and items show', async ({ page }) => {
    await open(page);
    await expect(page.locator('#stash-detail-workspace')).toContainText('Staged Manifest Items (1)');
    await expect(page.locator('.stash-type-pill')).toHaveText('ARCHIVE FILE');
    await expect(page.locator('.stash-item-card')).toContainText('PRE-SUPPORTED');
  });

  test('selecting another stash shows its empty state and an active button', async ({ page }) => {
    await open(page);
    await page.locator('.stash-nav-card').nth(1).click();
    await expect(page.locator('.stash-title-accent')).toHaveText('Second');
    await expect(page.locator('#stash-detail-workspace')).toContainText('This stash is empty.');
    await expect(page.getByRole('button', { name: 'Set as Active Stash' })).toBeVisible();
  });

  test('no stashes shows the empty sidebar and workspace', async ({ page }) => {
    await open(page, []);
    await expect(page.locator('.stash-sidebar-empty')).toBeVisible();
    await expect(page.locator('#stash-detail-workspace')).toContainText('No Stash Selected');
  });

  test('deleting asks with the name, then calls the API', async ({ page }) => {
    let method;
    await open(page);
    await page.route('**/api/stashes/3', async (route) => {
      method = route.request().method();
      if (method !== 'DELETE') return route.fallback();
      return route.fulfill({ json: { success: true } });
    });
    const messages = [];
    page.on('dialog', (dialog) => { messages.push(dialog.message()); dialog.accept(); });
    await page.getByRole('button', { name: 'Delete Stash' }).click();
    await expect.poll(() => messages.length).toBe(1);
    expect(messages[0]).toContain(HOSTILE);
    await expect.poll(() => method).toBe('DELETE');
  });

  test('removing an item calls the API', async ({ page }) => {
    let removed = false;
    await open(page);
    await page.route('**/api/stashes/items/21', (route) => { removed = true; return route.fulfill({ json: { success: true } }); });
    await page.getByRole('button', { name: 'Remove' }).click();
    await expect.poll(() => removed).toBe(true);
  });

  test('a synchronous export shows the result, and Done closes it', async ({ page }) => {
    await open(page);
    await page.route('**/api/stashes/3/export', (route) => route.fulfill({
      json: { export_path: HOSTILE, stash_name: HOSTILE, total_files: 2, total_bytes: 1024 },
    }));
    await page.locator('#btn-export-stash').click();
    await expect(page.locator('#export-path-text')).toHaveText(HOSTILE);
    await expect(page.locator('#stash-export-success-modal')).toContainText('Slicer Print Queue Ready');
    await page.getByRole('button', { name: 'Done' }).click();
    await expect(page.locator('#stash-export-success-modal')).toHaveCount(0);
    await expect(page.locator('#btn-export-stash')).toHaveText('Prepare for Slicer');
  });

  test('a finished background export fetches its report, because the poll leaves it out, P-01', async ({ page }) => {
    await open(page);
    await page.route('**/api/stashes/3/export', (route) => route.fulfill({ json: { task_id: 8, stash_name: HOSTILE } }));
    await mock(page, '/api/tasks/status', { active_task: null, recent_tasks: [{ id: 8, status: 'completed', progress_pct: 100 }] });
    await mock(page, '/api/tasks/8', { id: 8, status: 'completed', result_summary: { export_path: HOSTILE, total_files: 2, total_bytes: 1024 } });
    await page.locator('#btn-export-stash').click();
    await expect(page.locator('#export-path-text')).toHaveText(HOSTILE);
  });

  test('a failed report fetch keeps trying, and never shows an empty success', async ({ page }) => {
    await open(page);
    await page.route('**/api/stashes/3/export', (route) => route.fulfill({ json: { task_id: 8, stash_name: HOSTILE } }));
    await mock(page, '/api/tasks/status', { active_task: null, recent_tasks: [{ id: 8, status: 'completed', progress_pct: 100 }] });
    let replies = 0;
    await page.route('**/api/tasks/8', (route) => {
      replies += 1;
      return replies === 1
        ? route.fulfill({ status: 500, json: { error: 'busy' } })
        : route.fulfill({ json: { id: 8, result_summary: { export_path: HOSTILE, total_files: 2, total_bytes: 1024 } } });
    });
    await page.locator('#btn-export-stash').click();
    await expect(page.locator('#export-path-text')).toHaveText(HOSTILE);
    expect(replies).toBeGreaterThan(1);
  });

  test('a background export shows progress and can be cancelled', async ({ page }) => {
    await open(page);
    await page.route('**/api/stashes/3/export', (route) => route.fulfill({ json: { task_id: 8, stash_name: HOSTILE } }));
    await mock(page, '/api/tasks/status', { active_task: { id: 8, status: 'running', progress_pct: 40, progress_label: HOSTILE }, recent_tasks: [] });
    await page.route('**/api/tasks/cancel', (route) => route.fulfill({ json: { success: true } }));
    await page.locator('#btn-export-stash').click();
    await expect(page.locator('#export-task-pct')).toHaveText('40%');
    await expect(page.locator('#export-task-status-label')).toHaveText(HOSTILE);
    await page.getByRole('button', { name: 'Cancel Export' }).click();
    await expect(page.locator('#stash-export-progress-modal')).toHaveCount(0);
  });
});

test.describe('settings', () => {
  test('directories and libraries show hostile names as text', async ({ page }) => {
    await mock(page, '/api/settings/directories', { directories: [{ key: 'logs', name: HOSTILE, path: HOSTILE, size_bytes: 10, formatted_size: '10 B', file_count: 2 }] });
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: HOSTILE, path: HOSTILE, default_creator: HOSTILE, exists: false, disk_bytes: 5, total_models: 1, total_files: 2, last_scanned_at: null }] });
    await page.goto('/#settings');
    await expect(page.locator('#critical-directories-table-container')).toContainText('10 B');
    await expect(page.getByText(HOSTILE).first()).toBeVisible();
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
    await page.locator('[data-settings-tab-btn="libraries"]').click();
    await expect(page.locator('#library-usage-table')).toContainText('Never Scanned');
  });

  const previewReport = {
    finished_at: '2026-05-01 12:00:00',
    candidates: { proposed: 2 },
    report: {
      totals: { new: 2, known: 1, overlaps: 1, empty: 1, low_confidence: 1 },
      releases: [
        { name: HOSTILE, new: [{ rel_path: 'a/b', category: 'Heroes' }], known: 1, overlaps: ['a/c'], empty: ['a/d'], low_confidence: [] },
        { name: 'Odd Release', new: [{ rel_path: 'Odd Release', category: 'Miniatures' }], known: 0, overlaps: [], empty: [], low_confidence: ['Odd Release: no folder in its path has a usable name'] },
      ],
    },
  };

  test('a preview lists what would be added, and an unticked release is left out', async ({ page }) => {
    await mock(page, '/api/settings/libraries/2/preview', previewReport);
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true, total_models: 1, total_files: 1 }], total: 1 });
    let approved = null;
    let mode = null;
    await page.route('**/api/settings/libraries/2/preview/approve', (route) => {
      approved = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, approved: 1, skipped: 1 } });
    });
    await page.route('**/api/settings/libraries/2/scan', (route) => {
      mode = route.request().postDataJSON().mode;
      return route.fulfill({ json: { success: true, task_id: 4 } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();

    await expect(page.locator('#library-preview-summary')).toContainText('2 new, 1 already known, 1 overlapping');
    await expect(page.getByText(HOSTILE)).toBeVisible();
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
    await page.locator('.preview-release-check[data-release="Odd Release"]').uncheck();
    page.on('dialog', (dialog) => dialog.accept());
    await page.locator('#btn-approve-preview').click();
    await expect.poll(() => mode).toBe('add');
    expect(approved).toEqual({ skip_releases: ['Odd Release'] });
  });

  const reviewRelease = {
    release: 'Drop',
    folders: ['Drop/Busts', 'Drop/Busts/Lidda', 'Drop/Voxshax', 'Drop/Voxshax/Tomb'],
    files: ['Drop/Busts/Bases.zip', 'Drop/Busts/Lidda/a.stl', 'Drop/Voxshax/b.stl', 'Drop/Voxshax/Tomb/c.stl'],
    cards: [
      { key: 'voxshax', name: 'Voxshax', category: 'Heroes', paths: ['Drop/Voxshax'], skip: false, flags: ['Holds other named items: Tomb'] },
      { key: 'lidda', name: 'Lidda', category: 'Busts', paths: ['Drop/Busts/Lidda'], skip: false, flags: [] },
    ],
    reviewed: false, changed: false, locked: [], settled: [],
  };
  const reviewReport = {
    finished_at: '2026-05-01 12:00:00',
    candidates: { proposed: 2 },
    report: {
      totals: { new: 2, known: 0, overlaps: 0, empty: 0, low_confidence: 0 },
      releases: [
        { name: 'Calm', new: [{ rel_path: 'Calm/x', name: 'Quiet', folders: ['Calm/x'], flags: [] }], known: 0, overlaps: [], empty: [], low_confidence: [] },
        { name: 'Drop', new: [
          { rel_path: 'Drop/voxshax', name: 'Voxshax', folders: ['Drop/Voxshax'], flags: ['Holds other named items: Tomb'] },
          { rel_path: 'Drop/lidda', name: 'Lidda', folders: ['Drop/Busts/Lidda'], flags: [] },
        ], known: 0, overlaps: [], empty: [], low_confidence: [] },
      ],
    },
  };

  async function openReviewScreen(page) {
    await mock(page, '/api/settings/libraries/2/preview', reviewReport);
    await page.route('**/api/settings/libraries/2/preview/release*', (route) => route.fulfill({ json: reviewRelease }));
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true }], total: 1 });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();
  }

  test('approve and add reviewed sends only held releases and starts the add', async ({ page }) => {
    const report = structuredClone(reviewReport);
    report.report.totals.ready = 2;
    await mock(page, '/api/settings/libraries/2/preview', report);
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true }], total: 1 });
    let approved = null;
    let mode = null;
    await page.route('**/api/settings/libraries/2/preview/approve', (route) => {
      approved = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, approved: 1, waiting: 1 } });
    });
    await page.route('**/api/settings/libraries/2/scan', (route) => {
      mode = route.request().postDataJSON().mode;
      return route.fulfill({ json: { success: true, task_id: 4 } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();
    await page.locator('.preview-release-check[data-release="Calm"]').uncheck();
    await page.locator('#btn-approve-ready').click();
    await expect.poll(() => mode).toBe('add');
    expect(approved).toEqual({ skip_releases: ['Calm'], ready_only: true });
  });

  test('flagged releases come first and models can be read in the list', async ({ page }) => {
    await openReviewScreen(page);
    await expect(page.locator('.preview-release-open').first()).toHaveText('Drop');
    await page.locator('.preview-models summary').first().click();
    await expect(page.locator('.preview-model-flagged')).toHaveText('Voxshax');
  });

  test('a flagged release says what to check and lights up the cause', async ({ page }) => {
    await openReviewScreen(page);
    await page.locator('.preview-release-open').first().click();
    await expect(page.locator('#review-concerns')).toContainText('What to check (1)');
    await expect(page.locator('#review-concerns')).toContainText('Item inside a model');
    await expect(page.locator('#review-concerns')).toContainText('New Card');

    // It opens on the concern: the folder that caused it stands out from the rest of the card.
    await expect(page.locator('.review-row[data-path="Drop/Voxshax/Tomb"]')).toHaveClass(/review-row-suspect/);
    await expect(page.locator('.review-row[data-path="Drop/Voxshax"]')).toHaveClass(/review-row-flagged/);
    await expect(page.locator('.review-row[data-path="Drop/Voxshax"]')).not.toHaveClass(/review-row-suspect/);
    await expect(page.locator('.review-card[data-card="Voxshax"]')).toHaveClass(/review-card-focus/);
    await expect(page.locator('.review-row[data-path="Drop/Busts/Lidda"]')).toHaveCount(0);
  });

  test('repeated warnings collapse into one entry with one merge', async ({ page }) => {
    const release = structuredClone(reviewRelease);
    release.cards = [
      { key: '1', name: 'Mask', category: '', paths: ['Drop/Voxshax/b.stl'], skip: false, flags: [], group: '', tags: [] },
      { key: '2', name: 'Mask L', category: '', paths: ['Drop/Voxshax/Tomb/c.stl'], skip: false, flags: ['Looks like a part of Mask'], group: '', tags: [] },
      { key: '3', name: 'Mask R', category: '', paths: ['Drop/Busts/Lidda/a.stl'], skip: false, flags: ['Looks like a part of Mask'], group: '', tags: [] },
    ];
    await mock(page, '/api/settings/libraries/2/preview', reviewReport);
    await page.route('**/api/settings/libraries/2/preview/release*', (route) => route.fulfill({ json: release }));
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true }], total: 1 });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();
    await page.locator('.preview-release-open').first().click();
    await expect(page.locator('.review-concern')).toHaveCount(1);
    await expect(page.locator('.review-concern-chip')).toHaveCount(2);
    await page.locator('.review-merge').click();
    await expect(page.locator('.review-card')).toHaveCount(1);
    await expect(page.locator('.review-row[data-path="Drop/Busts/Lidda/a.stl"] .review-label')).toHaveText('Mask');
  });

  test('a possible duplicate lights up both cards', async ({ page }) => {
    const release = structuredClone(reviewRelease);
    release.cards[0].flags = ['Very close to Lidda, possibly the same thing'];
    await mock(page, '/api/settings/libraries/2/preview', reviewReport);
    await page.route('**/api/settings/libraries/2/preview/release*', (route) => route.fulfill({ json: release }));
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true }], total: 1 });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();
    await page.locator('.preview-release-open').first().click();
    await expect(page.locator('#review-concerns')).toContainText('Possible duplicate');
    await expect(page.locator('.review-row[data-path="Drop/Voxshax"]')).toHaveClass(/review-row-focus/);
    await expect(page.locator('.review-row[data-path="Drop/Busts/Lidda"]')).toHaveClass(/review-row-focus/);
  });

  test('a card keeps the code guess until a human moves something', async ({ page }) => {
    await openReviewScreen(page);
    let saved = null;
    await page.route('**/api/settings/libraries/2/preview/review', (route) => {
      saved = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, cards: 2 } });
    });
    await page.locator('.preview-release-open').first().click();
    await expect(page.locator('#library-review-modal')).toBeVisible();

    // The flagged card's folder starts open, so its contents show with the code's label.
    await expect(page.locator('.review-row[data-path="Drop/Voxshax"] .review-label')).toHaveText('Voxshax');
    await page.locator('.review-row[data-path="Drop/Voxshax/Tomb"] .review-select').check();
    await page.locator('#btn-review-new').click();
    await expect(page.locator('.review-row[data-path="Drop/Voxshax/Tomb"] .review-label')).toHaveText('Tomb');

    await page.locator('#btn-review-save').click();
    await expect.poll(() => saved).not.toBeNull();
    expect(saved.release).toBe('Drop');
    expect(saved.cards.find((c) => c.name === 'Tomb').paths).toEqual(['Drop/Voxshax/Tomb']);
    expect(saved.cards.find((c) => c.name === 'Voxshax').paths).toEqual(['Drop/Voxshax']);
  });

  test('a single file can become a card, and a card can be skipped', async ({ page }) => {
    await openReviewScreen(page);
    let saved = null;
    await page.route('**/api/settings/libraries/2/preview/review', (route) => {
      saved = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, cards: 2 } });
    });
    await page.locator('.preview-release-open').first().click();
    await page.locator('.review-row[data-path="Drop/Busts"] .review-twisty').click();
    await page.locator('.review-row[data-path="Drop/Busts/Bases.zip"] .review-select').check();
    await page.locator('#btn-review-new').click();
    await page.locator('.review-card[data-card="Bases"] .review-skip').check();
    await page.locator('#btn-review-save').click();
    await expect.poll(() => saved).not.toBeNull();
    const card = saved.cards.find((c) => c.name === 'Bases');
    expect(card.paths).toEqual(['Drop/Busts/Bases.zip']);
    expect(card.skip).toBe(true);
  });

  const browseModel = {
    id: 7, name: 'Ghul A', category: 'Tabletop', state: 'proposed', artifacts: 1, cover_id: 3, image_url: null, designer_name: 'Maker',
    groups: [{ id: 4, name: 'Ghul' }], collections: [{ id: 3, name: 'Vampire Court' }],
  };

  test('a model leads to its group, and a variant leads back to a model', async ({ page }) => {
    const variant = { ...browseModel, id: 8, name: 'Ghul B' };
    await page.route('**/api/catalog-models?*', (route) => route.fulfill({ json: { models: [browseModel], total: 1, page: 1, page_size: 48 } }));
    await page.route('**/api/catalog-models/7', (route) => route.fulfill({ json: { ...browseModel, tags: ['undead'], scales: ['32mm'], artifacts: [{ id: 1, filename: '32mm_Ghul_A.stl', scale: '32mm', supported: 0, hollow: 0, size_bytes: 10, file_id: 5, archive_member_id: 6, collection_id: 3 }] } }));
    await page.route('**/api/catalog-models/8', (route) => route.fulfill({ json: { ...variant, tags: [], scales: [], artifacts: [] } }));
    await page.route('**/api/model-groups/4', (route) => route.fulfill({ json: { id: 4, name: 'Ghul', models: [browseModel, variant] } }));
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('browse'));
    await page.locator('.browse-card[data-model="7"]').click();
    await expect(page.locator('#browse-model-view')).toContainText('32mm_Ghul_A.stl');

    await expect(page.locator('#model-view-facts')).toContainText('Maker');
    await expect(page.locator('#browse-model-view')).toContainText('undead');
    await expect(page.locator('#model-stash-selection')).toHaveText('Stash 1 Artifacts');
    await expect(page.locator('#model-artifact-rows tr')).toHaveCount(1);
    await expect(page.locator('#model-artifact-rows .btn-render-toggle')).toHaveCount(1);
    await page.locator('#model-stash-support').selectOption('supported');
    await expect(page.locator('#model-stash-selection')).toHaveText('Stash 0 Artifacts');
    await expect(page.locator('#model-artifact-rows tr')).toHaveCount(0);
    await page.locator('#model-stash-support').selectOption('');
    await page.locator('.artifact-stash').click();
    await expect(page.locator('#modal-add-to-stash-backdrop')).toBeVisible();
    await page.evaluate(() => { document.getElementById('modal-add-to-stash-backdrop').style.display = 'none'; });

    await page.locator('.browse-group-link').click();
    await expect(page.locator('#browse-group-view .browse-card')).toHaveCount(2);

    await page.locator('#browse-group-view .browse-card[data-model="8"]').click();
    await expect(page.locator('#model-view-title')).toHaveText('Ghul B');
    await expect(page.locator('#browse-model-view')).toContainText('Contents unknown');
  });

  test('an archive can be reviewed and a group set on a card', async ({ page }) => {
    await page.route('**/api/archive-review', (route) => route.fulfill({ json: { collections: [{ id: 3, name: 'Vampire Court', models: 2, model_groups: 1, artifacts: 2, reviewed: false, suspect: true }] } }));
    let saved = null;
    await page.route('**/api/archive-review/3', (route) => {
      if (route.request().method() === 'POST') {
        saved = route.request().postDataJSON();
        return route.fulfill({ json: { success: true, cards: 2 } });
      }
      return route.fulfill({ json: {
        release: '3', title: 'Vampire Court', peeked_at: '2026-10-31 12:00:00', folders: ['3/a.zip', '3/a.zip/Ghuls'], files: ['3/a.zip/Ghuls/A.stl', '3/a.zip/Ghuls/B.stl'],
        cards: [
          { key: '7', name: 'Ghul A', category: '', paths: ['3/a.zip/Ghuls/A.stl'], skip: false, flags: [], group: 'Ghul', tags: [] },
          { key: '8', name: 'Ghul B', category: '', paths: ['3/a.zip/Ghuls/B.stl'], skip: false, flags: ['Looks like a part of Ghul A'], group: 'Ghul', tags: [] },
        ],
        reviewed: false, changed: false, locked: [], settled: [],
      } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-archives').click();
    await expect(page.locator('#archive-queue-summary')).toContainText('0 of 1');
    await page.locator('.archive-review-open').click();
    await expect(page.locator('#library-review-title')).toContainText('Vampire Court - peeked');
    await expect(page.locator('#btn-review-reset')).toHaveCount(0);
    await expect(page.locator('#review-concerns')).toContainText('Possible part');
    await expect(page.locator('.review-merge')).toContainText('into Ghul A');
    await expect(page.locator('.review-row[data-path="3/a.zip/Ghuls/B.stl"]').or(page.locator('.review-row[data-path="3/a.zip/Ghuls"]')).first()).toBeVisible();
    await page.locator('.review-card[data-card="Ghul A"] .review-card-name').fill('Zombie');
    await page.locator('.review-row[data-path="3/a.zip"] .review-select').check();
    await expect(page.locator('#review-target option')).toHaveText(['Ghul B', 'Zombie']);
    await page.locator('.review-row[data-path="3/a.zip"] .review-select').uncheck();
    await page.locator('.review-card[data-card="Ghul B"] .review-card-group').fill('Undead');
    await page.locator('#btn-review-save').click();
    await expect.poll(() => saved).not.toBeNull();
    expect(saved.cards.find((c) => c.name === 'Ghul B').group).toBe('Undead');
  });

  test('a rendered sub-model preview shows as an image', async ({ page }) => {
    await page.route('**/api/render/stl', (route) => route.fulfill({ contentType: 'image/jpeg', body: Buffer.from('jpeg') }));
    await page.goto('/');
    await page.evaluate(async () => {
      const box = document.createElement('div');
      box.innerHTML = '<div id="submodel-thumb-container-1"><button id="render-it">Render</button></div>';
      document.body.append(box);
      await (await import('/static/js/actions.js')).actions.triggerSubmodelRender(1, 2, document.getElementById('render-it'));
      window.renderedBox = box;
    });
    expect(await page.evaluate(() => window.renderedBox.textContent)).not.toContain('[object');
    expect(await page.evaluate(() => window.renderedBox.querySelector('img.submodel-thumb').src)).toContain('blob:');
  });

  test('the backups tab shows state, runs a backup and checks one', async ({ page }) => {
    await mock(page, '/api/backups', {
      backup_dir: '/b', mirror_dir: '/nas/b', mirror_online: true, last_backup_at: '2026-10-31', last_backup_result: 'OK',
      backups: [{ name: 'library-20261031-000000-000000.db', size_bytes: 100, mirrored: true, has_metadata: true }],
    });
    const sent = [];
    await page.route('**/api/backups', (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      sent.push(route.request().postDataJSON());
      return route.fulfill({ json: { success: true, rows: 10, tables: 3 } });
    });
    await page.goto('/#settings');
    await page.locator('[data-settings-tab-btn="backups"]').click();
    await expect(page.locator('#backup-facts')).toContainText('Online');
    await page.locator('#btn-backup-now').click();
    await page.getByRole('button', { name: 'Check' }).click();
    await expect.poll(() => sent.map((s) => s.action)).toEqual(['run', 'verify']);
  });

  test('the audit tab lists destructive actions as text', async ({ page }) => {
    await page.route('**/api/audit-history?*', (route) => route.fulfill({ json: { actions: ['delete_orphans'], entries: [{ id: 1, action: 'delete_orphans', dry_run: 1, outcome: 'ok', detail: HOSTILE, created_at: '2026-10-31 12:00:00' }] } }));
    await page.goto('/#settings');
    await page.locator('[data-settings-tab-btn="audit"]').click();
    await expect(page.locator('#audit-history-panel #audit-table')).toContainText('Dry run');
    await expect(page.locator('#audit-panel')).not.toContainText('Dry run');
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
  });

  test('files and folders are picked in a browser and imported together', async ({ page }) => {
    let sent = null;
    await page.route('**/api/system/browse*', (route) => route.fulfill({ json: {
      success: true, current_path: '/d', parent_path: null, roots: ['/d'],
      directories: [{ name: 'Pack', path: '/d/Pack' }], files: [{ name: 'dragon.stl', path: '/d/dragon.stl', size_bytes: 5 }],
    } }));
    await page.route('**/api/import-file', (route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, task_id: 9 } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-import').click();
    await expect(page.locator('#btn-import-file')).toBeDisabled();
    await page.locator('#btn-import-browse').click();
    await page.locator('#import-browser-list input').nth(0).check();
    await page.locator('#import-browser-list input').nth(1).check();
    await page.getByRole('button', { name: 'Done' }).click();
    await expect(page.locator('#import-picked')).toContainText('2 selected');
    await page.locator('#btn-import-file').click();
    await expect.poll(() => sent?.paths).toEqual(['/d/Pack', '/d/dragon.stl']);
  });

  test('directories lists every location and explains missing ones', async ({ page }) => {
    await page.route('**/api/settings/locations', (route) => route.fulfill({ json: { locations: [
      { group: 'Configuration', name: 'Settings file', path: '/c/production.env', purpose: 'Server settings', change: 'Edit the file', state: 'found' },
      { group: 'Files and libraries', name: 'Vault', path: '/Volumes/V', purpose: 'Permanent home', change: 'Settings', state: 'not found' },
      { group: 'Backups', name: 'Backup mirror', path: '', purpose: 'Second copy', change: 'Settings', state: 'not set' },
    ] } }));
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('settings'));
    await page.locator('[data-settings-tab-btn="directories"]').click();
    const panel = page.locator('#locations-panel');
    await expect(panel).toContainText('/c/production.env');
    await expect(panel).toContainText('offline, or not created yet');
    await expect(panel).toContainText('Not set');
  });

  test('health shows checks and saves schedules', async ({ page }) => {
    let sent = null;
    await page.route('**/api/health', (route) => route.fulfill({ json: {
      status: 'warning', jobs: [],
      checks: [{ key: 'sign_in', label: 'MMF sign-in', state: 'warning', detail: 'Expires in 5 hours.' }],
      schedules: { integrity_enabled: '0', integrity_interval_days: '30', integrity_mode: 'fast' },
    } }));
    await page.route('**/api/schedules', (route) => {
      sent = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, schedules: {} } });
    });
    await page.goto('/');
    await expect(page.locator('#nav-tab-settings')).toHaveClass(/nav-attention/);
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('settings'));
    await page.locator('[data-settings-tab-btn="health"]').click();
    await expect(page.locator('#health-overall')).toHaveText('Needs attention');
    await expect(page.locator('#health-panel')).toContainText('Expires in 5 hours.');
    await expect(page.locator('input[name="sync_enabled"]')).toHaveCount(0);
    await page.locator('input[name="integrity_enabled"]').check();
    await page.locator('input[name="integrity_interval_days"]').fill('3');
    await page.getByRole('button', { name: 'Save Schedules' }).click();
    await expect.poll(() => sent).toMatchObject({ integrity_enabled: true, integrity_interval_days: 3 });
  });

  test('pdfs filter, stash and queue from one view', async ({ page }) => {
    const urls = [];
    let stashed = null;
    const documents = [
      { key: 'file-1', title: 'Guide', filename: 'guide.pdf', kind: 'Painting guide', creator: 'Maker', collection: 'Pack', collection_id: 4, source: 'MyMiniFactory', size_bytes: 5, state: 'on_disk', preview_url: '/previews/documents/file-1.jpg' },
      { key: 'file-2', title: 'Dragonseekers (PDF)', filename: 'Dragonseekers.zip', kind: 'Other', creator: 'Maker', collection: 'Dragonseekers (PDF)', collection_id: 5, source: 'MyMiniFactory', size_bytes: 5, state: 'unread', preview_url: '' },
    ];
    await page.route('**/api/documents?*', (route) => {
      urls.push(route.request().url());
      return route.fulfill({ json: { documents, total: 2, page: 1, page_size: 48, kinds: { Other: 1, 'Painting guide': 1 }, creators: ['Maker'], sources: ['MyMiniFactory'], previews_waiting: 1 } });
    });
    await page.route('**/api/documents/stash', (route) => {
      stashed = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, stash_id: 1, stash_name: 'Reading' } });
    });
    await page.goto('/');
    await page.locator('#btn-library-dropdown').click();
    await page.locator('#nav-documents').click();
    await expect(page.locator('#document-summary')).toContainText('2 PDFs');
    await expect(page.locator('[data-key="file-2"]')).toContainText('Archive not read yet');
    await expect(page.locator('[data-key="file-2"] .document-stash')).toHaveCount(0);
    await page.locator('[data-key="file-1"] .document-stash').click();
    await expect.poll(() => stashed).toEqual({ key: 'file-1' });
    await expect(page.locator('[data-key="file-1"] img')).toHaveAttribute('src', '/previews/documents/file-1.jpg');
    await page.route('**/api/documents/file-1/file', (route) => route.fulfill({ contentType: 'application/pdf', body: '%PDF-1.4' }));
    const [opened] = await Promise.all([page.waitForEvent('popup'), page.locator('[data-key="file-1"] a').click()]);
    expect(opened.url()).toContain('/api/documents/file-1/file');
    await opened.close();
    await page.locator('#document-kind').selectOption('Painting guide');
    await expect.poll(() => urls.at(-1)).toContain('kind=Painting+guide');
  });

  test('models filter by creator', async ({ page }) => {
    const urls = [];
    await page.route('**/api/catalog-models?*', (route) => {
      urls.push(route.request().url());
      return route.fulfill({ json: { creators: ['Loot Studios', 'Maker'], models: [], total: 0, page: 1, page_size: 48 } });
    });
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('browse'));
    await expect(page.locator('#browse-creator option')).toHaveCount(3);
    await page.locator('#browse-creator').selectOption('Loot Studios');
    await expect.poll(() => urls.at(-1)).toContain('creator=Loot%20Studios');
    await expect(page.locator('#browse-creator option')).toHaveCount(3);
  });

  test('vault reconcile shows state first, then a card for each tool', async ({ page }) => {
    await page.route('**/api/reconcile/history', (route) => route.fulfill({ json: { runs: {
      forward: { task_id: 12, status: 'completed', progress_label: 'Scan finished', started_at: '2026-11-01 10:00:00', completed_at: '2026-11-01 10:05:00', scan_mode: 'deep' },
      staging_scan: { task_id: 14, status: 'failed', progress_label: 'boom', started_at: '2026-11-01 11:00:00', completed_at: '2026-11-01 11:00:00', scan_mode: null },
    } } }));
    await page.goto('/#downloads');
    await page.locator('#group-reconcile').click();
    const cards = page.locator('#reconcile-cards .tool-card');
    await expect(cards).toHaveCount(8);
    await expect(page.locator('#sub-dl-reconcile .reconcile-guide-grid')).toHaveCount(0);
    for (let index = 0; index < 8; index += 1) {
      await expect(cards.nth(index).locator('.reconcile-card-action button').last()).toBeVisible();
      await expect(cards.nth(index).locator('.reconcile-card-body')).toContainText('To validate:');
    }
    const forward = page.locator('[data-card="forward"]');
    await expect(forward.locator('[data-run]')).toContainText('Job #12');
    await expect(forward.locator('[data-run]')).toContainText('(deep)');
    await expect(page.locator('[data-card="staging_scan"] [data-run]')).toContainText('failed');
    await expect(page.locator('[data-card="duplicate_scan"] [data-run]')).toHaveText('Not run yet');

    await expect(page.locator('#reconcile-results-container')).toHaveCSS('overflow-y', 'auto');
    const stateBox = await page.locator('#reconcile-metrics-bar').boundingBox();
    const toolsBox = await page.locator('#reconcile-cards').boundingBox();
    expect(stateBox.y).toBeLessThan(toolsBox.y);

    const switchBox = await forward.locator('.segmented-control').boundingBox();
    const runBox = await page.locator('#btn-run-reconcile').boundingBox();
    expect(switchBox.y + switchBox.height).toBeLessThanOrEqual(runBox.y);
    await expect(page.locator('#btn-run-reconcile')).toHaveText('Run Fast Reconciliation');
    await page.locator('#btn-mode-deep').click();
    await expect(page.locator('#btn-run-reconcile')).toHaveText('Run Deep Reconciliation');

    const bodyBox = await forward.locator('.reconcile-card-body').boundingBox();
    const whole = await forward.boundingBox();
    expect(bodyBox.width / whole.width).toBeGreaterThan(0.6);
  });

  test('every library gets a preview, seven releases to a page', async ({ page }) => {
    const release = (index) => ({ name: `Release ${index}`, new: [{ name: 'M', rel_path: `Release ${index}/M`, flags: ['Odd'] }], known: 0, added: 0, overlaps: [], empty: [], low_confidence: [], reviewed: false, edited: false });
    const report = (count) => ({ releases: Array.from({ length: count }, (_, i) => release(i + 1)), totals: { new: count, known: 0, overlaps: 0, empty: 0, low_confidence: 0, ready: 0 } });
    let approved = null;
    await page.route('**/api/settings/libraries', (route) => route.fulfill({ json: { libraries: [
      { id: 1, name: 'Loot', path: '/loot', exists: true }, { id: 2, name: 'Second', path: '/second', exists: true },
    ] } }));
    await page.route('**/api/settings/libraries/1/preview', (route) => route.fulfill({ json: { candidates: { proposed: 9 }, report: report(9) } }));
    await page.route('**/api/settings/libraries/2/preview', (route) => route.fulfill({ json: { candidates: { proposed: 2 }, report: report(2) } }));
    await page.route('**/api/settings/libraries/1/preview/approve', (route) => {
      approved = route.request().postDataJSON();
      return route.fulfill({ json: { success: true, approved: 8, waiting: 1 } });
    });
    await page.route('**/api/settings/libraries/1/scan', (route) => route.fulfill({ json: { success: true, task_id: 4 } }));
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();
    const first = page.locator('#library-preview-1');
    await expect(first).toContainText('Preview for Loot');
    await expect(page.locator('#library-preview-2')).toContainText('Preview for Second');
    await expect(first.locator('tbody tr')).toHaveCount(7);
    await first.locator('.preview-release-check').first().uncheck();
    const name = await first.locator('.preview-release-check').first().getAttribute('data-release');
    await first.getByRole('button', { name: 'Next' }).click();
    await expect(first.locator('tbody tr')).toHaveCount(2);
    await expect(first).toContainText('Page 2 of 2');
    await first.getByRole('button', { name: 'Previous' }).click();
    await expect(first.locator('.preview-release-check').first()).not.toBeChecked();
    await first.getByRole('button', { name: 'Next' }).click();
    page.on('dialog', (dialog) => dialog.accept());
    await first.locator('#btn-approve-preview').click();
    await expect.poll(() => approved?.skip || approved?.skipped || approved?.held || approved).toBeTruthy();
    expect(JSON.stringify(approved)).toContain(name);

    const order = await page.locator('#external-libraries-panel').evaluate((panel) => ['external-libraries-table-container', 'new-lib-path', 'library-image-queue', 'library-preview-container']
      .map((id) => panel.querySelector(`#${id}`).getBoundingClientRect().top));
    expect([...order].sort((x, y) => x - y)).toEqual(order);
  });

  test('every dropdown leaves room for its arrow', async ({ page }) => {
    await page.goto('/#downloads');
    const padding = await page.locator('#history-sort').evaluate((el) => getComputedStyle(el).paddingRight);
    expect(parseInt(padding, 10)).toBeGreaterThanOrEqual(30);
    const arrow = await page.locator('#history-sort').evaluate((el) => getComputedStyle(el).appearance);
    expect(arrow).toBe('none');
  });

  test('help cards and tables stay inside the page', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('help'));
    await page.setViewportSize({ width: 1300, height: 800 });
    const overflow = await page.locator('.help-card').evaluateAll((cards) => cards.filter((card) => card.scrollWidth > card.clientWidth + 1).length);
    expect(overflow).toBe(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(0);
  });

  test('update pdfs runs from vault reconcile', async ({ page }) => {
    let started = false;
    await page.route('**/api/documents/update', (route) => {
      started = true;
      return route.fulfill({ json: { success: true, task_id: 3 } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-reconcile').click();
    await expect(page.locator('#view-documents #btn-document-update')).toHaveCount(0);
    await page.locator('#sub-dl-reconcile #btn-document-update').click();
    await expect.poll(() => started).toBe(true);
  });

  test('help explains the server settings file', async ({ page }) => {
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('help'));
    await expect(page.locator('#help-configuration')).toContainText('DESYNC_ALLOWED_ROOTS');
  });

  test('clicking a tag shows the models that have it', async ({ page }) => {
    const urls = [];
    await page.route('**/api/tags*', (route) => route.fulfill({ json: { tags: [{ name: 'undead', uses: 3 }], total: 1 } }));
    await page.route('**/api/catalog-models?*', (route) => {
      urls.push(route.request().url());
      return route.fulfill({ json: { models: [], total: 0, page: 1, page_size: 48 } });
    });
    await page.goto('/');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.switchTab('stashes'));
    await page.locator('#stash-tab-tags').click();
    await page.locator('.tag-open-page').click();
    await expect(page.locator('#browse-tag-filter')).toContainText('undead');
    expect(urls.at(-1)).toContain('tag=undead');
    await page.locator('#browse-tag-filter').click();
    await expect(page.locator('#browse-tag-filter')).toHaveCount(0);
  });

  test('manage tabs group downloads and external sources', async ({ page }) => {
    await page.goto('/#downloads');
    await expect(page.locator('#view-downloads > .sub-tabs').first().locator('.sub-btn')).toHaveText(['MMF Sync', 'Download MMF', 'External Vault', 'Vault Reconcile']);
    await expect(page.locator('#sub-dl-sync')).toBeVisible();
    await page.locator('#group-download').click();
    await expect(page.locator('#sub-dl-pending')).toBeVisible();
    await expect(page.locator('#minor-external')).toBeHidden();
    await page.locator('#group-external').click();
    await expect(page.locator('#sub-dl-libraries')).toBeVisible();
    await expect(page.locator('#group-external')).toHaveClass(/active/);
  });

  test('settings shows library disk figures but no library actions', async ({ page }) => {
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true, disk_formatted: '4 GB' }], total: 1 });
    await page.goto('/#settings');
    await page.locator('[data-settings-tab-btn="libraries"]').click();
    await expect(page.locator('#library-usage-table')).toContainText('4 GB');
    await expect(page.getByRole('button', { name: 'Preview New Folders' })).toHaveCount(0);
  });

  test('the image queue reports its state and can be paused', async ({ page }) => {
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true }], total: 1 });
    let action = null;
    await page.route('**/api/library-images', (route) => {
      if (route.request().method() === 'POST') {
        action = route.request().postDataJSON().action;
        return route.fulfill({ json: { success: true } });
      }
      return route.fulfill({ json: { paused: false, state: 'storage_offline', counts: { pending: 3, done: 1, failed: 2, no_source: 4, skipped: 0 } } });
    });
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();

    await expect(page.locator('#image-queue-summary')).toContainText('Waiting for storage to come back. 3 waiting, 1 done');
    await page.locator('#btn-image-queue-toggle').click();
    await expect.poll(() => action).toBe('pause');
  });

  test('the inactive tab shows items, with hostile text as text', async ({ page }) => {
    await page.route('**/api/inactive*', (route) => route.fulfill({
      json: {
        total: 1,
        open_counts: [{ process: 'library_scan', reason: 'folder_missing', count: 1 }],
        items: [{ id: 1, item_type: 'model', item_id: 9, item_name: HOSTILE, process: 'library_scan', reason: 'folder_missing', detail: HOSTILE, created_at: '2026-05-01 12:00:00', resolved_at: null }],
      },
    }));
    await page.goto('/#settings');
    await page.locator('[data-settings-tab-btn="inactive"]').click();

    await expect(page.locator('#inactive-table-container')).toContainText('folder_missing');
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
    await expect(page.locator('#inactive-filter option').nth(1)).toHaveText('library_scan: folder_missing (1)');
  });

  test('a library can be scanned', async ({ page }) => {
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true, total_models: 1, total_files: 2, disk_bytes: 1 }] });
    let scanned = false;
    await page.route('**/api/settings/libraries/2/scan', (route) => { scanned = true; return route.fulfill({ json: { success: true, task_id: 1 } }); });
    page.on('dialog', (dialog) => dialog.accept());
    await page.goto('/#downloads');
    await page.locator('#group-external').click();
    await page.locator('#sub-btn-libraries').click();
    await page.getByRole('button', { name: 'Full Rescan' }).first().click();
    await expect.poll(() => scanned).toBe(true);
  });

  test('the folder browser lists and navigates, with hostile names as text', async ({ page }) => {
    const paths = [];
    await page.route('**/api/system/browse*', (route) => {
      paths.push(new URL(route.request().url()).searchParams.get('path') || new URL(route.request().url()).searchParams.get('folder'));
      return route.fulfill({ json: { success: true, current_path: '/a', parent_path: '/', directories: [{ name: HOSTILE, path: `/a/${HOSTILE}` }], dirs: [HOSTILE] } });
    });
    await page.goto('/#settings');
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.openDirectoryPicker('setting-work-path'));
    const items = page.locator('.browse-item');
    await expect(items).toHaveCount(2);
    await expect(items.nth(1)).toContainText(HOSTILE);
    await items.nth(1).click();
    await expect.poll(() => paths.at(-1)).toBe(`/a/${HOSTILE}`);
  });
});

test.describe('downloads', () => {
  const pending = { models: [{ id: 1, name: HOSTILE, designer_name: HOSTILE, file_count: 2, download_status: 'not_downloaded', published_at: '2026-05-01' }], total: 1 };

  async function open(page, status = {}) {
    await mock(page, '/api/downloads/status', { active_downloads: [], active_offloads: [], jobs: [], is_running: true, reconnect_required: false, ...status });
    await mock(page, '/api/downloads/history', { history: [{ id: 1, model_id: 1, model_name: HOSTILE, creator_name: HOSTILE, status: 'failed', error_message: HOSTILE, enqueued_at: '2026-05-01 12:00:00', completed_at: null, total_files: 1, total_bytes: 1 }], total: 1 });
    await mock(page, '/api/models', pending);
    await mock(page, '/api/creators', [{ designer_name: HOSTILE, total_models: 1 }]);
    await page.goto('/#downloads');
    await page.locator('#group-download').click();
  }

  test('pending models show as text, and queue by id', async ({ page }) => {
    let body;
    await open(page);
    await page.route('**/api/downloads/queue', async (route) => {
      if (route.request().method() !== 'POST') return route.fallback();
      body = route.request().postDataJSON();
      return route.fulfill({ json: { success: true } });
    });
    await expect(page.locator('.pending-row-chk').first()).toBeAttached();
    await expect(page.locator('.cell-clickable-title').first()).toContainText(HOSTILE);
    await page.locator('tbody .btn-action-queue').first().click();
    await expect.poll(() => body?.model_ids).toEqual([1]);
  });

  test('a ticked row stays ticked after a refresh', async ({ page }) => {
    await open(page);
    await page.locator('.pending-row-chk').first().check();
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.refreshCurrentView());
    await expect(page.locator('.pending-row-chk').first()).toBeChecked();
  });

  test('active downloads and jobs render', async ({ page }) => {
    await open(page, {
      active_downloads: [{ model_id: 1, model_name: HOSTILE, creator: HOSTILE, current_file: HOSTILE, status: 'downloading', speed_bps: 1, downloaded_bytes: 1, total_bytes: 2 }],
      jobs: [{ id: 9, model_id: 1, model_name: HOSTILE, creator_name: HOSTILE, status: 'pending', total_files: 1, total_bytes: 1, enqueued_at: '2026-05-01 12:00:00' }],
    });
    await expect(page.getByText(HOSTILE).first()).toBeAttached();
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
  });

});

test.describe('models', () => {
  const model = (overrides) => ({
    id: 1, name: HOSTILE, designer_name: HOSTILE, download_status: 'not_downloaded', tags: [HOSTILE, 'plain', 'c', 'd', 'e', 'f'],
    pre_supported: 1, is_bonus: 1, url: 'javascript:window.xss=1', primary_image_url: 'http://127.0.0.1:8099/static/none.jpg', file_count: 2, ...overrides,
  });

  async function open(page, models = [model({})]) {
    await mock(page, '/api/models', { models, total: models.length });
    await mock(page, '/api/creators', []);
    await page.goto('/#models');
    await expect(page.locator('#view-models .card').first()).toBeVisible();
  }

  test('a card shows its data as text', async ({ page }) => {
    await open(page);
    const card = page.locator('#view-models .card').first();
    await expect(card.locator('.card-title')).toHaveText(HOSTILE);
    await expect(card.locator('.badge-bonus')).toBeVisible();
    await expect(card).toContainText('Pre-Supported');
    await expect(card.locator('.tag-pill')).toHaveCount(6);
    await expect(card.locator('.tag-more')).toHaveText('+1');
    await expect(card.locator('img[src="x"]')).toHaveCount(0);
  });

  test('a javascript URL is never a link', async ({ page }) => {
    await open(page);
    await expect(page.locator('a[href^="javascript"]')).toHaveCount(0);
  });

  test('buttons follow download status', async ({ page }) => {
    await open(page, [model({ id: 1, download_status: 'completed', url: '' }), model({ id: 2, download_status: 'queued', url: '' }), model({ id: 3, url: 'https://www.myminifactory.com/o/3' })]);
    await expect(page.getByRole('button', { name: 'Archived' })).toBeDisabled();
    await expect(page.locator('#view-models .btn-action-cancel')).toHaveCount(1);
    await expect(page.locator('a.btn-mmf-fullwidth')).toHaveAttribute('href', 'https://www.myminifactory.com/o/3');
  });

  test('clicking a tag filters without opening the model', async ({ page }) => {
    await open(page);
    await page.locator('.tag-pill').first().click();
    await expect(page.locator('#model-tag-filter')).toHaveValue(HOSTILE);
  });

  test('add to stash opens from a card with the hostile title', async ({ page }) => {
    await mock(page, '/api/stashes', { stashes: [] });
    await mock(page, '/api/stashes/active', { active_stash: null });
    await open(page);
    await page.locator('#view-models .model-card-actions button').last().click();
    await expect(page.locator('#stash-modal-item-title')).toHaveText(HOSTILE);
  });

  test('the detail modal shows model data as text', async ({ page }) => {
    await open(page);
    await mock(page, '/api/models/1', model({ description: HOSTILE, files: [], release_month: '2026-05', views: 1, likes: 2, price_value: 0 }));
    await page.locator('#view-models .card').first().click();
    await expect(page.getByText(HOSTILE).first()).toBeVisible();
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
    await expect(page.locator('a[href^="javascript"]')).toHaveCount(0);
  });

  test('the files modal lists files as text', async ({ page }) => {
    await open(page);
    await mock(page, '/api/models/1/files', { id: 1, name: HOSTILE, designer_name: HOSTILE, total_files: 1, files: [{ file_id: 7, filename: HOSTILE, size_bytes: 1024, download_status: 'completed', downloaded: true }] });
    await page.locator('#view-models .model-card-actions button').first().click();
    await expect(page.getByText(HOSTILE).first()).toBeVisible();
    await expect(page.locator('body img[src="x"]')).toHaveCount(0);
  });
});
