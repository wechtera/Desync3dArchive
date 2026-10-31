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
        recent_tasks: started ? [task({ id: 9, status: 'failed', progress_pct: 5, progress_label: label, log_output: 'login page' })] : [],
      },
    }));
    await page.goto('/#downloads');
    await page.evaluate(() => window.openModal('modal-scraper'));
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
    await expect(rows.nth(1)).toContainText('Metadata Ingest');
    await expect(rows.nth(1).locator('.cell-label-truncate')).toHaveText(HOSTILE);
    await expect(rows.nth(1).locator('.status-failed')).toHaveCount(1);
    await expect(page.locator('#task-history-tbody img')).toHaveCount(0);
  });

  test('an empty history says so, and later fills in', async ({ page }) => {
    await open(page, { active_task: null, recent_tasks: [] });
    await expect(page.locator('#task-history-tbody')).toContainText('No recent tasks recorded.');
    await page.unroute('**/api/tasks/status*');
    await mock(page, '/api/tasks/status', { active_task: null, recent_tasks: [task({})] });
    await page.evaluate(() => window.renderTasksView());
    await expect(page.locator('#task-history-tbody tr')).toHaveCount(1);
    await expect(page.locator('#task-history-tbody')).not.toContainText('No recent tasks recorded.');
  });

  test('scrape folders list in the ingest dialog, with hostile names as text', async ({ page }) => {
    await mock(page, '/api/scrapes/list', { scrapes: [{ folder: HOSTILE, model_count: 3 }, { folder: 'scrape_2', model_count: 0 }] });
    await open(page, { active_task: null, recent_tasks: [] });
    await page.evaluate(() => window.openIngestModal());
    const options = page.locator('#ingest-scrape-folder-select option');
    await expect(options).toHaveCount(2);
    await expect(options.first()).toHaveAttribute('value', HOSTILE);
    await expect(options.nth(1)).toHaveText('scrape_2 (empty)');
  });

  test('no scrape folders says so', async ({ page }) => {
    await mock(page, '/api/scrapes/list', { scrapes: [] });
    await open(page, { active_task: null, recent_tasks: [] });
    await page.evaluate(() => window.openIngestModal());
    await expect(page.locator('#ingest-scrape-folder-select')).toContainText('No scrape runs found');
  });

  test('the ingest report breaks down creators', async ({ page }) => {
    await open(page, { active_task: null, recent_tasks: [] });
    await page.evaluate((name) => window.renderIngestReport({
      submitted: 3, ingested: 2, creators: { [name]: { count: 2, pre_supported: 1 } },
    }), HOSTILE);
    await expect(page.locator('#report-ingested')).toHaveText('2');
    await expect(page.locator('.cell-creator-accent')).toHaveText(HOSTILE);
    await page.evaluate(() => window.renderIngestReport({ creators: {} }));
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
    await page.evaluate(() => window.openModelDetailModal(1));
    await expect(page.locator('#detail-unavailable-note')).toContainText('Not available for local download');
    await expect(page.getByRole('button', { name: '+ Add Model to Stash' })).toBeHidden();
    expect(page.pageErrors).toEqual([]);
  });
});

test.describe('duplicate report', () => {
  test('lists matches with hostile names as text', async ({ page }) => {
    await mock(page, '/api/reconcile/duplicates', {
      success: true,
      summary: { exact_groups: 1, candidate_groups: 1, duplicate_models: 1, overlapping_models: 0, ignored_names: ['base'] },
      files: [
        { kind: 'exact', filename: HOSTILE, size_bytes: 1024, models: [{ model_id: 1, model_name: 'One' }, { model_id: 2, model_name: 'Two' }] },
        { kind: 'candidate', filename: 'b.stl', size_bytes: 10, models: [{ model_id: 3, model_name: 'Three' }, { model_id: 4, model_name: 'Four' }] },
      ],
      models: [{ kind: 'duplicate', shared_files: 2, models: [{ model_id: 1, model_name: 'One' }, { model_id: 2, model_name: 'Two' }] }],
    });
    await page.goto('/');
    await page.evaluate(() => window.triggerDuplicateScan());
    await expect(page.locator('.duplicate-row')).toHaveCount(3);
    await expect(page.locator('.duplicate-row').nth(0)).toContainText('DUPLICATE MODEL');
    await expect(page.locator('.duplicate-row').nth(1)).toContainText('EXACT');
    await expect(page.locator('.duplicate-row').nth(2)).toContainText('POSSIBLE');
    expect(await page.evaluate(() => window.xss)).toBeUndefined();
    expect(page.pageErrors).toEqual([]);
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
    await page.evaluate((context) => window.openAddToStashModal(context), item);
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
    await expect(page.getByText('Never Scanned')).toBeVisible();
  });

  test('a library can be scanned', async ({ page }) => {
    await mock(page, '/api/settings/libraries', { libraries: [{ id: 2, name: 'Lib', path: '/l', exists: true, total_models: 1, total_files: 2, disk_bytes: 1 }] });
    let scanned = false;
    await page.route('**/api/settings/libraries/2/scan', (route) => { scanned = true; return route.fulfill({ json: { success: true, task_id: 1 } }); });
    page.on('dialog', (dialog) => dialog.accept());
    await page.goto('/#settings');
    await page.locator('[data-settings-tab-btn="libraries"]').click();
    await page.getByRole('button', { name: /Scan/ }).first().click();
    await expect.poll(() => scanned).toBe(true);
  });

  test('the folder browser lists and navigates, with hostile names as text', async ({ page }) => {
    const paths = [];
    await page.route('**/api/system/browse*', (route) => {
      paths.push(new URL(route.request().url()).searchParams.get('path') || new URL(route.request().url()).searchParams.get('folder'));
      return route.fulfill({ json: { success: true, current_path: '/a', parent_path: '/', directories: [{ name: HOSTILE, path: `/a/${HOSTILE}` }], dirs: [HOSTILE] } });
    });
    await page.goto('/#settings');
    await page.evaluate(() => window.openDirectoryPicker('setting-work-path'));
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
    await page.evaluate(() => window.refreshCurrentView());
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
