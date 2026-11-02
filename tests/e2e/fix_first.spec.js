// Browser side of the 2026-11-01 review's first fixes. The Python half is tests/test_fix_first.py.
const { test, expect } = require('@playwright/test');

const release = (overrides) => ({
  id: '362414_2021-05', designer_name: 'Maker', release_month: '2021-05', model_count: 4, total_files: 9,
  total_size_bytes: 1048576, is_likely_bundle: 0, download_status: 'not_downloaded', sample_model_id: null,
  sample_image_url: '', ...overrides,
});

function mock(page, path, json) {
  return page.route(`**${path}*`, (route) => route.fulfill({ json }));
}

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.dialogs = [];
  page.answer = true;
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  page.on('dialog', async (dialog) => {
    page.dialogs.push(dialog.message());
    await (page.answer ? dialog.accept() : dialog.dismiss());
  });
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

test.describe('releases, R-12', () => {
  test.beforeEach(async ({ page }) => {
    await mock(page, '/api/creators', []);
    await mock(page, '/api/audit', { has_data: false });
    await mock(page, '/api/releases', {
      releases: [
        release({}),
        release({ id: '362414_2021-06', release_month: '2021-06', download_status: 'queued' }),
        release({ id: 'external_Loot Studios_2021-04', designer_name: 'Loot Studios', release_month: '2021-04' }),
      ],
      total: 3,
    });
    await page.goto('/#releases');
    await expect(page.locator('.release-month-section').first()).toBeVisible();
  });

  test('cancel sends the release key, not a guessed designer and month', async ({ page }) => {
    let request;
    await page.route('**/api/downloads/queue', async (route) => {
      request = { method: route.request().method(), body: route.request().postDataJSON() };
      await route.fulfill({ json: { success: true } });
    });
    await page.getByRole('button', { name: 'Cancel Pack Queue' }).click();
    await expect.poll(() => request).toEqual({ method: 'DELETE', body: { release_id: '362414_2021-06' } });
  });

  test('a linked library release offers no download button', async ({ page }) => {
    await expect(page.getByRole('button', { name: 'Queue Entire Pack' })).toHaveCount(1);
  });

  test('a refused queue says so', async ({ page }) => {
    await page.route('**/api/downloads/queue', (route) => route.fulfill({ status: 400, json: { error: 'No model IDs provided' } }));
    await page.getByRole('button', { name: 'Queue Entire Pack' }).click();
    await expect.poll(() => page.dialogs.join()).toContain("Couldn't queue this pack");
  });
});

test.describe('vault reconcile, S-02, S-03 and R-15', () => {
  const realign = (page) => page.evaluate(async () => (await import('/static/js/actions.js')).actions.realignSinglePath(5));

  test.beforeEach(async ({ page }) => {
    await page.goto('/#downloads');
    await expect.poll(() => page.evaluate(async () => typeof (await import('/static/js/actions.js')).actions.realignSinglePath)).toBe('function');
    page.bodies = [];
    await page.route('**/api/reconcile/drift/realign', async (route) => {
      const body = route.request().postDataJSON();
      page.bodies.push(body);
      await route.fulfill({
        json: body.confirm
          ? { success: true, task_id: 12 }
          : {
            success: true, dry_run: true, errors: [],
            would_move: [{ model_id: 5, current_path: '/vault/old', canonical_path: '/vault/Maker/5_Model' }],
          },
      });
    });
  });

  test('realign shows the move and asks before it sends confirm', async ({ page }) => {
    await realign(page);
    expect(page.bodies).toEqual([{ model_ids: [5], confirm: false }, { model_ids: [5], confirm: true }]);
    expect(page.dialogs[0]).toContain('/vault/old');
    expect(page.dialogs[0]).toContain('/vault/Maker/5_Model');
  });

  test('saying no sends nothing more', async ({ page }) => {
    page.answer = false;
    await realign(page);
    expect(page.bodies).toEqual([{ model_ids: [5], confirm: false }]);
  });

  test('a refused orphan is reported and nothing claims success', async ({ page }) => {
    await page.route('**/api/reconcile/delete-orphans', (route) => route.fulfill({
      json: { success: true, deleted_count: 0, errors: ['Refused 123_Kept: the catalog has files there.'] },
    }));
    await page.evaluate(async () => (await import('/static/js/actions.js')).actions.deleteSingleOrphan('/vault/123_Kept'));
    expect(page.dialogs[0]).toContain('Pending Deletion');
    expect(page.dialogs[1]).toContain('Refused 123_Kept');
  });
});
