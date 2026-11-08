// E59: dismissing a Vault Audit finding, and the note when a saved scan came from another version.
// The Python half is tests/test_audit_findings.py.
const { test, expect } = require('@playwright/test');

const ISSUE = { defect: 'METADATA_MISSING', filename: 'metadata.json', detail: 'Embedded metadata.json is missing.', expected_path: '/v/a' };
const MODEL = { model_id: 2000002530, model_name: 'September 2025 PDFs', creator_name: 'Loot Studios', source: 'linked', issues: [ISSUE] };

function status(dismissed, versions = {}) {
  return {
    status: 'completed', scan_mode: 'deep', timestamp: '2026-11-01 21:56:52',
    summary: { total_audited: 2613, healthy: 2612, discrepancies: dismissed ? 0 : 1 },
    discrepancies: dismissed ? [] : [MODEL],
    dismissed: dismissed ? [{ ...ISSUE, model_id: MODEL.model_id, model_name: MODEL.model_name, creator_name: MODEL.creator_name }] : [],
    saved_version: '1.0.3', current_version: '1.0.3', ...versions,
  };
}

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.calls = [];
  page.dismissed = false;
  page.versions = {};
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  await page.route('**/api/reconcile/status*', (route) => route.fulfill({ json: status(page.dismissed, page.versions) }));
  await page.route('**/api/reconcile/findings/*', (route) => {
    const step = new URL(route.request().url()).pathname.split('/').pop();
    page.calls.push({ step, body: route.request().postDataJSON() });
    page.dismissed = step === 'dismiss';
    return route.fulfill({ json: { success: true } });
  });
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

test('a dismissed finding moves to its own list and can be restored', async ({ page }) => {
  await page.goto('/#downloads');
  await page.locator('#group-reconcile').click();
  await expect(page.locator('.reconcile-card')).toHaveCount(1);
  await page.locator('.reconcile-card .reconcile-dismiss').click();
  await expect(page.locator('.reconcile-card')).toHaveCount(0);
  expect(page.calls[0]).toEqual({ step: 'dismiss', body: {
    model_id: 2000002530, defect: 'METADATA_MISSING', filename: 'metadata.json', detail: 'Embedded metadata.json is missing.',
    model_name: 'September 2025 PDFs',
  } });
  const dismissed = page.locator('#reconcile-dismissed');
  await expect(dismissed.locator('summary')).toHaveText('Dismissed findings (1)');
  await dismissed.locator('summary').click();
  await dismissed.locator('.reconcile-restore').click();
  await expect(page.locator('.reconcile-card')).toHaveCount(1);
  expect(page.calls[1].step).toBe('restore');
});

test('a scan saved by another version says so', async ({ page }) => {
  page.versions = { saved_version: '1.0.0' };
  await page.goto('/#downloads');
  await page.locator('#group-reconcile').click();
  await expect(page.locator('#reconcile-last-scan')).toContainText('It was saved by version 1.0.0, and this is 1.0.3. Run the audit again for current results.');
});

test('a scan from before versions were recorded says it came from an earlier version', async ({ page }) => {
  page.versions = { saved_version: null };
  await page.goto('/#downloads');
  await page.locator('#group-reconcile').click();
  await expect(page.locator('#reconcile-last-scan')).toContainText('It was saved by an earlier version of the app.');
});
