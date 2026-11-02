// The STL preview window. The Python half is StlPreviewE53Tests in tests/test_remaining_items.py.
const { test, expect } = require('@playwright/test');

// A binary STL holding one triangle standing on the ground.
function oneTriangle() {
  const body = Buffer.alloc(134);
  body.writeUInt32LE(1, 80);
  [[0, 0, 0], [1, 0, 0], [0, 0, 1]].forEach((point, index) => {
    point.forEach((value, axis) => body.writeFloatLE(value, 96 + (index * 12) + (axis * 4)));
  });
  return body;
}

test.beforeEach(async ({ page }) => {
  page.pageErrors = [];
  page.on('pageerror', (error) => page.pageErrors.push(error.message));
  await page.route('**/*', (route) => {
    const { hostname } = new URL(route.request().url());
    return hostname === '127.0.0.1' ? route.continue() : route.abort();
  });
  await page.route('**/api/preview/stl', (route) => route.fulfill({
    status: 200, contentType: 'model/stl', body: oneTriangle(),
  }));
  await page.goto('/#browse');
});

test.afterEach(async ({ page }) => {
  expect(page.pageErrors).toEqual([]);
});

function open(page, title = 'hero.stl') {
  return page.evaluate(async (name) => {
    const { openStlPreview } = await import('/static/js/views/stl_preview.js');
    await openStlPreview('file', 1, name);
  }, title);
}

async function menuHasPreview(page, name) {
  return page.evaluate(async (title) => {
    const { renderSTLViewDropdown } = await import('/static/js/views/models.js');
    const { render } = await import('/static/js/template.js');
    const host = document.createElement('div');
    render(renderSTLViewDropdown('file', 1, title), host);
    return [...host.querySelectorAll('.render-dropdown-item')].map((item) => item.textContent.trim());
  }, name);
}

test('the Render menu offers Preview for an STL', async ({ page }) => {
  expect((await menuHasPreview(page, 'Hero.STL'))[0]).toBe('Preview');
});

test('other file types get no Preview', async ({ page }) => {
  expect(await menuHasPreview(page, 'pack.zip')).not.toContain('Preview');
});

test('a mesh loads into the window, with shadows on', async ({ page }) => {
  await open(page);
  await expect(page.locator('#stl-preview')).toHaveAttribute('data-ready', 'true');
  await expect(page.locator('.stl-preview-title')).toHaveText('hero.stl');
  await expect(page.locator('.stl-preview-status')).toBeHidden();
  const hasPicture = await page.locator('.stl-preview-canvas').evaluate((canvas) => canvas.width > 0 && canvas.height > 0);
  expect(hasPicture).toBe(true);
});

test('the X closes it', async ({ page }) => {
  await open(page);
  await page.locator('.stl-preview-close').click();
  await expect(page.locator('#stl-preview')).toHaveCount(0);
});

test('a click outside closes it', async ({ page }) => {
  await open(page);
  await page.mouse.click(4, 4);
  await expect(page.locator('#stl-preview')).toHaveCount(0);
});

test('a click inside leaves it open', async ({ page }) => {
  await open(page);
  await page.locator('.stl-preview-canvas').click();
  await expect(page.locator('#stl-preview')).toHaveCount(1);
});

test('turning the model and letting go outside leaves it open', async ({ page }) => {
  await open(page);
  const box = await page.locator('.stl-preview-canvas').boundingBox();
  await page.mouse.move(box.x + 50, box.y + 50);
  await page.mouse.down();
  await page.mouse.move(2, 2);
  await page.mouse.up();
  await expect(page.locator('#stl-preview')).toHaveCount(1);
});

test('only one preview exists at a time', async ({ page }) => {
  await open(page, 'first.stl');
  await open(page, 'second.stl');
  await expect(page.locator('#stl-preview')).toHaveCount(1);
  await expect(page.locator('.stl-preview-title')).toHaveText('second.stl');
});

test('the title bar moves the window', async ({ page }) => {
  await open(page);
  const before = await page.locator('.stl-preview-window').boundingBox();
  const header = await page.locator('.stl-preview-title').boundingBox();
  await page.mouse.move(header.x + 5, header.y + 5);
  await page.mouse.down();
  await page.mouse.move(header.x - 95, header.y + 65);
  await page.mouse.up();
  const after = await page.locator('.stl-preview-window').boundingBox();
  expect(Math.round(after.x - before.x)).toBe(-100);
  expect(Math.round(after.y - before.y)).toBe(60);
  await expect(page.locator('#stl-preview')).toHaveCount(1);
});

test('the up axis is read from the shape', async ({ page }) => {
  const guesses = await page.evaluate(async () => {
    const { guessUpAxis } = await import('/static/js/views/stl_preview.js');
    return [
      [76, 6, 76], [76, 76, 5], [5, 76, 76], // Plates lie on their thin side.
      [109, 127, 111], [30, 20, 60], [42, 40, 20], // Sizes from real packs and slicer files.
    ].map(([x, y, z]) => guessUpAxis({ x, y, z }));
  });
  expect(guesses).toEqual(['y', 'z', 'x', 'y', 'z', 'y']);
});

test('Reorient cycles the up axis and leaves the window open', async ({ page }) => {
  await open(page);
  const root = page.locator('#stl-preview');
  await expect(root).toHaveAttribute('data-up', 'y');
  const button = page.locator('.stl-preview-turn');
  await button.click();
  await expect(root).toHaveAttribute('data-up', 'z');
  await button.click();
  await expect(root).toHaveAttribute('data-up', 'x');
  await button.click();
  await expect(root).toHaveAttribute('data-up', 'y');
});

test('a refused file says why', async ({ page }) => {
  await page.unroute('**/api/preview/stl');
  await page.route('**/api/preview/stl', (route) => route.fulfill({
    status: 404, json: { error: "This file isn't reachable. The NAS may be offline." },
  }));
  await open(page);
  await expect(page.locator('.stl-preview-status')).toContainText('offline');
});
