// Static price board (board/index.html) in Chromium. Uses the committed real
// snapshot, so assertions check behaviour and wording, not specific prices.
// Run: node --test tests/board.e2e.mjs   (rebuild first: python3 scripts/build_board.py)
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
let pw;
try { pw = require('playwright'); } catch { pw = require(path.join(execSync('npm root -g').toString().trim(), 'playwright')); }
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const boardPath = path.join(root, 'board', 'index.html');
const engineOf = (html) => /<script id="fpc-engine">([\s\S]*?)<\/script>/.exec(html)[1];

let browser, page;
const errors = [], hosts = new Set();
before(async () => {
  browser = await pw.chromium.launch();
  page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('request', (r) => { const u = new URL(r.url()); if (u.protocol.startsWith('http')) hosts.add(u.hostname); });
  // Fonts are optional; keep the test offline.
  await page.route(/^https?:\/\//, (r) => r.abort());
  await page.goto('file://' + boardPath);
  await page.waitForSelector('#winner .label-tag, #winner .label-empty');
});
after(async () => { await browser?.close(); });
const text = (sel) => page.locator(sel).innerText();

test('ships the same matching engine as index.html (rebuild the board after engine changes)', () => {
  assert.equal(engineOf(readFileSync(boardPath, 'utf8')), engineOf(readFileSync(path.join(root, 'index.html'), 'utf8')));
});

test('opens on a real comparison and never claims the cheapest price in the US', async () => {
  const w = await text('#winner');
  assert.match(w, /Lowest verified price among \d+ checked locations?/i);
  assert.doesNotMatch(w + await text('#coverage'), /cheapest (price )?in the US/i);
  assert.match(await text('#coverage'), /not proof of the cheapest price nationwide/);
  assert.match(w, /May not deliver to you/);
});

test('a Grubhub menu item from MenuPages fills the order and scales with quantity', async () => {
  await page.click('#resetOrder');
  await page.fill('#ghUrl', 'https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510');
  await page.click('#ghLookup');
  assert.match(await text('#ghResult'), /in the snapshot/);
  const opt = page.locator('#ghMenus select option', { hasText: 'Small - Deep Dish Margherita ·' }).first();
  const label = await opt.innerText();
  const unit = Math.round(parseFloat(/\$(\d+\.\d{2})/.exec(label)[1]) * 100);
  await page.selectOption('#ghMenus select', await opt.getAttribute('value'));
  assert.equal(await page.inputValue('#chain'), "Lou Malnati's Pizzeria");
  assert.equal(await page.inputValue('#it0_name'), 'Small - Deep Dish Margherita');
  assert.equal(await page.inputValue('#it0_gh'), (unit / 100).toFixed(2));
  await page.fill('#it0_qty', '2');
  assert.equal(await page.inputValue('#it0_gh'), (unit * 2 / 100).toFixed(2));
  assert.match(await text('#it0_hint'), /from MenuPages \(Grubhub-owned\)/);
  await page.waitForFunction(() => /Grubhub/.test(document.querySelector('#winner').innerText));
});

test('an unknown Grubhub listing asks for manual entry', async () => {
  await page.fill('#ghUrl', 'https://www.grubhub.com/restaurant/some-place-1-main-st-springfield/999999999');
  await page.click('#ghLookup');
  assert.match(await text('#ghResult'), /not in this snapshot.*enter the item and your Grubhub subtotal by hand/s);
  await page.fill('#ghUrl', 'https://example.com/x');
  await page.click('#ghLookup');
  assert.match(await text('#ghResult'), /Not a grubhub\.com URL/);
});

test('imports render untrusted text as text, are labelled, and invalid records are reported', async () => {
  await page.click('#resetOrder');
  await page.fill('#chain', 'Test Chain');
  await page.fill('#it0_name', 'Test Wrap');
  const rec = (over) => Object.assign({ platform: 'ubereats', store_id: 'x1', store_name: 'Test Chain (<img src=x onerror="window.__pwned=1">)',
    store_url: 'https://www.ubereats.com/store/test/x1', address: { street: '1 Main St', city: 'Springfield', state: 'IL', zip: '62701', country: 'US' },
    observed_at: new Date(Date.now() - 60000).toISOString(), order_mode: 'delivery', items: [{ name: 'Test Wrap', price: '5.25' }] }, over);
  await page.fill('#importText', JSON.stringify({ observations: [rec({}), rec({ store_id: 'x2', address: { city: 'Toronto', state: 'ON', zip: 'M5V', country: 'CA' } })] }));
  await page.click('#importBtn');
  assert.match(await text('#importMsg'), /Added 1 store menu/);
  assert.match(await text('#importMsg'), /outside the US|50 US states/);
  await page.waitForFunction(() => /\$5\.25/.test(document.querySelector('#winner').innerText));
  assert.match(await text('#winner'), /Imported by you/);
  assert.match(await text('#winner'), /<img src=x/);
  assert.equal(await page.evaluate(() => window.__pwned), undefined);
  assert.equal(await page.locator('#winner img').count(), 0);
  await page.click('#clearImport');
});

test('switching snapshot runs keeps each run separate', async () => {
  // Every price shown for a run must have been observed during that run.
  const n = await page.locator('#runSel option').count();
  assert.ok(n >= 2, 'the committed board should hold several runs');
  await page.locator('#presets button', { hasText: "McDonald's" }).first().click();
  const when = (t) => { const m = /(\d{1,2}) (\w{3}) (\d{4}), (\d\d):(\d\d) UTC/.exec(t); return m && Date.parse(`${m[1]} ${m[2]} ${m[3]} ${m[4]}:${m[5]} UTC`); };
  let withWinner = 0;
  for (let i = 0; i < n; i++) {
    await page.selectOption('#runSel', String(i));
    await page.waitForTimeout(150);
    const runAt = when(await page.locator('#runSel option').nth(i).innerText());
    const w = await text('#winner');
    const seen = /Observed ([^(]+)\(/.exec(w);
    if (!seen) continue;
    withWinner++;
    const at = when(seen[1]);
    assert.ok(at <= runAt + 5 * 60000 && at >= runAt - 30 * 60000, `run ${i}: price observed ${seen[1]} outside the run at ${new Date(runAt).toISOString()}`);
    const rows = page.locator('#tabBody tbody tr:not(.details) td:nth-child(5)');
    assert.ok(await rows.count() >= 1);
  }
  assert.ok(withWinner >= 1 && withWinner < n || withWinner === n, 'at least one run shows a verified price');
  await page.selectOption('#runSel', '0');
  await page.click('#resetOrder');
});

test('coverage tab lists what each search checked and what blocked it', async () => {
  await page.click('#tabs button[data-tab="coverage"]');
  const c = await text('#tabBody');
  assert.match(c, /Store pages read/);
  assert.match(c, /Blocks: captcha|No blocks reported/);
  await page.click('#tabs button[data-tab="verified"]');
});

test('an item the snapshot never searched for is not reported as missing from checked stores', async () => {
  await page.click('#resetOrder');
  await page.fill('#chain', "McDonald's");
  await page.fill('#it0_name', 'Big Mac');
  await page.waitForFunction(() => /did not search for this item/.test(document.querySelector('#winner').innerText));
  assert.doesNotMatch(await text('#winner'), /No checked store lists/);
  assert.match(await text('#coverage'), /read only for a different item/);
  assert.match(await text('#coverage'), /Results cover only the 0 locations/);
});

test('picking a menu item starts a fresh item, keeps the quantity, and can be picked again', async () => {
  await page.click('#resetOrder');
  await page.fill('#ghUrl', 'https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510');
  await page.click('#ghLookup');
  const pick = async (label) => {
    const v = await page.locator('#ghMenus select option', { hasText: label }).first().getAttribute('value');
    await page.selectOption('#ghMenus select', v);
  };
  await pick('Small - Deep Dish Margherita ·');
  await page.fill('#it0_qty', '2');
  await page.fill('#it0_flavor', 'Pesto');
  await page.fill('#it0_count', '8');
  await pick('Small - Deep Dish Chicago Classic ·');
  assert.equal(await page.inputValue('#it0_name'), 'Small - Deep Dish Chicago Classic');
  assert.equal(await page.inputValue('#it0_flavor'), '');
  assert.equal(await page.inputValue('#it0_count'), '');
  assert.equal(await page.inputValue('#it0_qty'), '2');
  assert.equal(await page.inputValue('#ghMenus select'), '');          // picker reset
  await page.click('#resetOrder');
  await pick('Small - Deep Dish Chicago Classic ·');                   // same listing again
  assert.equal(await page.inputValue('#it0_name'), 'Small - Deep Dish Chicago Classic');
  assert.match(await page.locator('#ghMenus select option').first().innerText(), /\((\d+) priced listings\)/);
});

test('the run note follows the current order and filters', async () => {
  await page.selectOption('#stateFilter', 'AZ');
  await page.locator('#presets button', { hasText: 'Taco Bell' }).click();
  await page.selectOption('#stateFilter', '');
  await page.waitForTimeout(200);
  const cov = await text('#coverage');
  const newest = await page.locator('#runSel option').first().innerText();
  // With all states again, the newest run's own result is shown unless it truly had no match.
  if (/found no exact listing|checked no stores/.test(cov)) assert.notEqual(await page.inputValue('#runSel'), '0');
  else assert.equal(await page.inputValue('#runSel'), '0', newest);
});

test('no horizontal scroll at phone widths', async () => {
  for (const w of [400, 375, 360]) {
    const p = await browser.newPage({ viewport: { width: w, height: 800 } });
    await p.route(/^https?:\/\//, (r) => r.abort());
    await p.goto('file://' + boardPath);
    await p.waitForSelector('#winner .label-tag, #winner .label-empty');
    const [sw, iw] = await p.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
    assert.ok(sw <= iw, `scrollWidth ${sw} > ${iw} at ${w}px`);
    await p.close();
  }
});

test('no page errors, and no requests except optional fonts', () => {
  assert.deepEqual(errors, []);
  for (const h of hosts) assert.ok(['fonts.googleapis.com', 'fonts.gstatic.com'].includes(h), 'unexpected request to ' + h);
});
