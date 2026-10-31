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
