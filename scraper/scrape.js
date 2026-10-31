const { chromium } = require('playwright-extra');
const stealth = require('puppeteer-extra-plugin-stealth')();
const fs = require('fs');

chromium.use(stealth);

const cookiesFlag = process.argv.indexOf('--cookies');
const cookiesPath = cookiesFlag === -1 ? '' : process.argv[cookiesFlag + 1];
const expectedFlag = process.argv.indexOf('--expected');
const expectedCount = expectedFlag === -1 ? 0 : Number(process.argv[expectedFlag + 1]) || 0;
// The API can hand over the whole library at once. Most of it counts as a full capture.
const fullPayloadCount = expectedCount > 0 ? Math.floor(expectedCount * 0.8) : Infinity;

// The app reads one JSON object per line from stdout.
function emit(event, fields = {}) {
  console.log(JSON.stringify({ event, ...fields }));
}

const headless = process.argv.includes('--headless');
const SAME_SITE = { strict: 'Strict', lax: 'Lax', none: 'None', no_restriction: 'None' };

function stop(message) {
  emit('error', { message });
  process.exitCode = 1;
}

async function isLoginPage(page) {
  return /\/(login|signin)\b|auth\.myminifactory\.com/i.test(page.url())
    || (await page.$('input[type="password"]')) !== null;
}

(async () => {
  if (!cookiesPath) {
    console.error('Usage: node scrape.js --cookies <path>');
    process.exitCode = 1;
    return;
  }
  if (!fs.existsSync(cookiesPath)) {
    console.error('cookies.json not found.');
    return;
  }

  const rawCookies = JSON.parse(fs.readFileSync(cookiesPath, 'utf8'));
  const cleanCookies = (Array.isArray(rawCookies) ? rawCookies : []).map(c => ({
    name: c.name,
    value: c.value,
    domain: c.domain || '.myminifactory.com',
    path: c.path || '/',
    // MMF is HTTPS only, and Chrome drops SameSite=None cookies that aren't secure.
    secure: true,
    httpOnly: Boolean(c.httpOnly),
    expires: c.expirationDate ? Number(c.expirationDate) : undefined,
    sameSite: SAME_SITE[String(c.sameSite).toLowerCase()] || 'Lax'
  }));

  // Launch installed Chrome if available; fallback to standard Chromium
  let browser;
  try {
    browser = await chromium.launch({
      channel: 'chrome',
      headless,
      args: [
        '--disable-blink-features=AutomationControlled',
        '--disable-infobars'
      ]
    });
  } catch (err) {
    emit('status', { message: 'Chrome launch failed, falling back to Chromium' });
    browser = await chromium.launch({
      headless,
      args: ['--disable-blink-features=AutomationControlled']
    });
  }

  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
  });

  if (cleanCookies.length > 0) {
    await context.addCookies(cleanCookies);
  }

  const page = await context.newPage();
  const models = new Map();
  let fullPayloadCaptured = false;

  // Intercept background API payloads
  page.on('response', async (response) => {
    const contentType = response.headers()['content-type'] || '';
    if (contentType.includes('application/json')) {
      try {
        const data = await response.json();
        let items = [];

        if (Array.isArray(data)) {
          items = data;
        } else if (data && typeof data === 'object') {
          items = data.items || data.objects || data.data || [];
        }

        if (Array.isArray(items) && items.length > 50) {
          // Only the path: query strings can carry tokens.
          emit('api_batch', { path: new URL(response.url()).pathname, count: items.length });

          for (const item of items) {
            const modelId = String(
              item.object_id || 
              (item.object && item.object.id) || 
              item.id || 
              ''
            );
            const modelName = item.name || item.title || (item.object && item.object.name) || `Model ${modelId}`;

            if (modelId) {
              models.set(modelId, modelName);
            }
          }

          if (models.size >= fullPayloadCount) {
            fullPayloadCaptured = true;
          }
        }
      } catch (err) {}
    }
  });

  emit('status', { message: 'Navigating to MyMiniFactory library' });
  await page.goto('https://www.myminifactory.com/library', { waitUntil: 'domcontentloaded' });

  // Cloudflare Challenge Detection and Grace Period
  try {
    const title = await page.title();
    const hasChallenge = title.includes('Just a moment') || 
                         (await page.$('#challenge-running')) !== null || 
                         (await page.$('#cf-turnstile')) !== null;

    if (hasChallenge && headless) {
      await browser.close();
      return stop('Cloudflare asked for a check, which needs a visible window. Turn off headless scraping in Settings, or refresh your cookies.');
    }

    if (hasChallenge) {
      emit('status', { message: 'Cloudflare check detected. Click the box in the browser window if prompted.' });

      await page.waitForFunction(() => {
        return !document.title.includes('Just a moment') && !document.querySelector('#challenge-running');
      }, { timeout: 90000 });

      emit('status', { message: 'Cloudflare check passed' });
      
      // Update cookies.json with any newly minted Cloudflare session cookies
      const updatedCookies = await context.cookies();
      fs.writeFileSync(cookiesPath, JSON.stringify(updatedCookies, null, 2));
    }
  } catch (cfErr) {
    emit('status', { message: `Cloudflare wait ended: ${cfErr.message}` });
  }

  if (await isLoginPage(page)) {
    await browser.close();
    return stop('MyMiniFactory asked for a login, so the session cookies were not accepted. Export fresh cookies, including REMEMBERME, into Settings.');
  }

  // Wait for initial JSON payloads to stream in
  await page.waitForTimeout(6000);

  if (fullPayloadCaptured) {
    emit('finalizing', { count: models.size });
  } else {
    emit('status', { message: `API provided ${models.size} models. Scrolling for the rest.` });

    for (let cycle = 1; cycle <= 250; cycle++) {
      const prevCount = models.size;

      const extracted = await page.evaluate(() => {
        function queryShadow(root) {
          let matches = [];
          const nodes = root.querySelectorAll('*');
          for (const node of nodes) {
            if (node.tagName === 'A' && node.href) {
              matches.push({
                href: node.href,
                title: node.innerText || node.getAttribute('title') || ''
              });
            }
            if (node.shadowRoot) {
              matches = matches.concat(queryShadow(node.shadowRoot));
            }
          }
          return matches;
        }
        return queryShadow(document.body);
      });

      for (const item of extracted) {
        const directMatch = item.href.match(/3d-print-.*-(\d+)/);
        if (directMatch) {
          const id = directMatch[1];
          if (!models.has(id) || models.get(id).startsWith('Model ')) {
            models.set(id, item.title.trim() || `Model ${id}`);
          }
        }
      }

      emit('cycle', { cycle, count: models.size });

      await page.evaluate(() => {
        window.scrollTo(0, document.body.scrollHeight);
        const scrollables = Array.from(document.querySelectorAll('*')).filter(el => {
          const style = window.getComputedStyle(el);
          return (style.overflowY === 'auto' || style.overflowY === 'scroll') && el.scrollHeight > el.clientHeight;
        });
        scrollables.forEach(s => s.scrollTop = s.scrollHeight);
      });

      await page.keyboard.press('End');
      await page.mouse.wheel(0, 4000);
      await page.waitForTimeout(3000);

      // There is no fixed target: a library can hold more than the catalog knows about.
      if (models.size === prevCount && cycle > 5) {
        emit('finalizing', { count: models.size });
        break;
      }
    }
  }

  // Save outputs
  const sortedIds = Array.from(models.keys()).sort((a, b) => Number(b) - Number(a));
  fs.writeFileSync('model_ids.txt', sortedIds.join('\n') + '\n');
  fs.writeFileSync('models.json', JSON.stringify(Object.fromEntries(models), null, 2));

  emit('done', { count: sortedIds.length });
  await browser.close();
})().catch((err) => {
  const closed = /has been closed/.test(String(err));
  stop(closed ? 'The browser window was closed before the scrape finished.' : `Scrape failed: ${err.message}`);
});
