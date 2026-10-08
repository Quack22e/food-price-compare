// End-to-end UI test: real index.html in Chromium, real backend, synthetic
// local platform fixtures (tests/e2e_env.py). Run: node --test tests/ui.e2e.mjs
import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import readline from 'node:readline';

const require = createRequire(import.meta.url);
let pw;
try { pw = require('playwright'); } catch { pw = require(path.join(execSync('npm root -g').toString().trim(), 'playwright')); }
const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);

let proc, env, browser, ctx, page;
const consoleErrors = [];
const dialogs = [];

async function text(sel) { return (await page.locator(sel).innerText()).replace(/\s+/g, ' '); }
async function waitText(sel, re, timeout = 60000) {
  await page.waitForFunction(([s, src, flags]) => new RegExp(src, flags).test((document.querySelector(s) || {}).innerText || ''), [sel, re.source, re.flags], { timeout });
}
async function waitStatus(re, timeout = 90000) { await waitText('#progressArea', new RegExp('status: ' + re.source), timeout); }
async function selectRTab(name) { await page.click(`#resultTabs button[data-rtab="${name}"]`); }

describe('index.html end-to-end', { timeout: 300000 }, () => {
  before(async () => {
    proc = spawn('python3', [path.join(here, 'e2e_env.py')], { cwd: root, stdio: ['ignore', 'pipe', 'inherit'] });
    const rl = readline.createInterface({ input: proc.stdout });
    env = JSON.parse(await new Promise((res, rej) => { rl.once('line', res); proc.once('exit', c => rej(new Error('env exited ' + c))); }));
    browser = await pw.chromium.launch();
    ctx = await browser.newContext({ acceptDownloads: true });
    page = await ctx.newPage();
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text()); });
    page.on('pageerror', e => consoleErrors.push('pageerror: ' + e.message));
    page.on('dialog', d => { dialogs.push(d.message()); d.accept(); });
    await page.goto(env.app + '/');
  });
  after(async () => {
    if (browser) await browser.close();
    if (proc) proc.kill();
  });

  it('connects to the backend (served same-origin)', async () => {
    await waitText('#backendStatus', /Backend connected \(TEST MODE\)/, 10000);
    assert.equal(await page.isDisabled('#searchBtn'), false);
  });

  it('parses a pasted receipt into editable fields (client-side)', async () => {
    await page.click('#inputTabs button[data-tab="paste"]');
    await page.fill('#receiptText', "Your order from McDonald's\n2 x 10 pc. Chicken McNuggets  $12.98\n   Sweet 'N Sour Sauce\nSubtotal $12.98\nDelivery fee $1.99\nTotal $15.20");
    await page.click('#receiptParse');
    assert.equal(await page.inputValue('#chain'), "McDonald's");
    assert.equal(await page.inputValue('#it0_name'), '10 pc. Chicken McNuggets');
    assert.equal(await page.inputValue('#it0_qty'), '2');
    assert.equal(await page.inputValue('#it0_gh'), '12.98');
    assert.match(await text('#receiptResult'), /Parsed 1 item/);
    assert.match(await text('#it0_hint'), /Detected in name: 10 pc/);
  });

  it('extracts a Grubhub link through the backend', async () => {
    await page.click('#resetOrder');
    await page.click('#inputTabs button[data-tab="link"]');
    await page.fill('#ghUrl', 'https://www.grubhub.com/restaurant/mcdonalds-1-main-st-chicago/555?menuItemId=777');
    await page.click('#ghExtract');
    await waitText('#ghResult', /Fields extracted/, 20000);
    assert.equal(await page.inputValue('#chain'), "McDonald's");
    assert.equal(await page.inputValue('#it0_name'), 'Chicken McNuggets');
    assert.match(await text('#it0_hint'), /Grubhub base price \$6\.29/);
  });

  it('rejects a non-Grubhub link with a manual-entry hint', async () => {
    await page.fill('#ghUrl', 'https://example.com/foo');
    await page.click('#ghExtract');
    assert.match(await text('#ghResult'), /Not a grubhub\.com URL.*Manual entry/);
  });

  it('runs a live search: progress, coverage, winner, sorting, explanations', async () => {
    await page.fill('#it0_count', '10');
    await page.fill('#it0_qty', '2');
    await page.fill('#it0_gh', '14.00');
    await page.click('#searchBtn');
    await waitStatus(/completed/);
    const prog = await text('#progressArea');
    assert.match(prog, /DoorDash finished/);
    assert.match(prog, /Uber Eats finished/);
    assert.match(prog, /6 Discovered/);
    await waitText('#winner', /Lowest verified price among 7 checked locations/);
    const w = await text('#winner');
    assert.match(w, /\$10\.58/);            // Uber Eats 10 pc @ $5.29 × 2
    assert.match(w, /Saves \$3\.42 vs your Grubhub item subtotal \$14\.00/);
    assert.match(w, /may not deliver to you/);
    assert.match(await text('#coverage'), /not proof of the cheapest price nationwide/);
    assert.match(await text('#progressArea'), /Matched \(exact\)/);
    const rows = page.locator('#resultBody tbody tr');
    assert.equal(await rows.count(), 7);
    assert.match(await rows.first().innerText(), /\$10\.58/);
    await page.click('#resultBody th button:has-text("Item subtotal")');
    assert.match(await rows.first().innerText(), /\$11\.98/);  // sorted descending now
    await page.click('#resultBody th button:has-text("Item subtotal")');
    await page.locator('#resultBody tbody tr').first().locator('button:has-text("Why?")').click();
    const det = await page.locator('#resultBody tr.details').innerText();
    assert.match(det, /Product name matches/);
    assert.match(det, /Piece count matches \(10\)/);
    assert.match(det, /diningMode=DELIVERY/);
    await selectRTab('rejected');
    assert.match(await text('#resultBody'), /Piece count differs: order 10, candidate 20/);
    await selectRTab('uncertain');
    assert.match(await text('#resultTabs'), /Needs confirmation \(\d+\)/);
    await selectRTab('barriers');
    assert.match(await text('#resultBody'), /job → completed/);
    await selectRTab('verified');
  });

  it('cancels a running job and resumes it from the checkpoint', async () => {
    await page.click('#searchBtn');
    await waitText('#progressArea', /status: running/, 20000);
    await page.waitForFunction(() => /[1-9]\d*\s+Checked/.test(document.querySelector('#progressArea').innerText), null, { timeout: 30000 });
    await page.click('#cancelBtn');
    await waitStatus(/cancelled/);
    assert.match(await text('#progressArea'), /cancelled/);
    await page.click('#refreshJobs');
    await page.waitForFunction(() => document.querySelectorAll('#resumeSelect option').length >= 3);
    const opt = await page.locator('#resumeSelect option', { hasText: 'cancelled' }).first().getAttribute('value');
    await page.selectOption('#resumeSelect', opt);
    await page.click('#resumeBtn');
    await waitStatus(/completed/);
    await waitText('#winner', /\$10\.58/);
    await waitText('#winner', /among 7 checked locations/);  // nothing lost across cancel/resume
  });

  it('exports CSV and JSON', async () => {
    let [dl] = await Promise.all([page.waitForEvent('download'), page.click('#exportCsv')]);
    const csv = readFileSync(await dl.path(), 'utf8');
    assert.match(csv, /^match_status,platform,data_source/);
    assert.match(csv, /verified,ubereats,live,.*10\.58/);
    [dl] = await Promise.all([page.waitForEvent('download'), page.click('#exportJson')]);
    const js = JSON.parse(readFileSync(await dl.path(), 'utf8'));
    assert.match(js.winner_label, /Lowest verified price among 7 checked locations/);
    assert.equal(js.winner.row.lineCents, 1058);
    assert.equal(js.coverage.exhaustive, false);
  });

  it('imports collected data, reports invalid records, renders untrusted text safely', async () => {
    const good = { platform: 'doordash', store_id: '424242', store_name: "McDonald's (<img src=x onerror=alert(1)>)", store_url: 'https://www.doordash.com/store/mcdonalds-test-424242/',
      address: { street: '<script>alert(2)</script>9 Import Way', city: 'Reno', state: 'NV', zip: '89501' }, observed_at: new Date(Date.now() - 3600e3).toISOString(), order_mode: 'delivery',
      currency: 'USD', items: [{ name: '10 pc Chicken McNuggets', price: '4.00', availability: 'available' }] };
    const bad = Object.assign({}, good, { store_id: '1', currency: 'CAD' });
    await page.fill('#importText', JSON.stringify([good, bad]));
    await page.click('#importBtn');
    assert.match(await text('#importReport'), /Imported 1 store observation.*1 error/);
    assert.match(await text('#importReport'), /currency CAD is not USD/);
    // Store name and street contain markup: rendered as text, never executed.
    await waitText('#winner', /\$8\.00/);
    const w = await text('#winner');
    assert.match(w, /McDonald's \(<img src=x onerror=alert\(1\)>\)/);
    assert.match(w, /<script>alert\(2\)<\/script>9 Import Way/);
    assert.equal(await page.locator('#winner img, #winner script, #resultBody img').count(), 0);
    assert.equal(dialogs.filter(d => d === '1' || d === '2').length, 0);
    const link = page.locator('#winner a');
    assert.equal(await link.getAttribute('href'), 'https://www.doordash.com/store/mcdonalds-test-424242/');
    assert.match(await link.getAttribute('rel'), /noopener/);
    assert.match(await text('#resultBody'), /Imported/);
    await page.uncheck('#includeImported');
    await waitText('#winner', /\$10\.58/);
    await page.check('#includeImported');
    await page.click('#importClear');
    await waitText('#winner', /\$10\.58/);
    await page.fill('#importText', '{"broken": ');
    await page.click('#importBtn');
    assert.match(await text('#importReport'), /Invalid JSON/);
    await page.fill('#importText', '');
  });

  it('keeps pickup and delivery separate', async () => {
    await page.check('#modePickup');
    await waitText('#winner', /No verified winner yet/);
    assert.match(await text('#coverage'), /7 in the other order mode/);
    await page.check('#modeDelivery');
    await waitText('#winner', /\$10\.58/);
  });

  it('filters by location', async () => {
    await page.check('#scopeLocal');
    await page.selectOption('#fState', 'TX');
    await waitText('#winner', /\$11\.18/);       // Austin branch: $5.59 × 2
    assert.match(await text('#winner'), /Austin, TX 78701/);
    assert.match(await text('#coverage'), /outside the location filter/);
    await page.selectOption('#fState', '');
    await page.fill('#fZip', '43215');
    await waitText('#winner', /Columbus, OH 43215/);
    await page.fill('#fZip', '');
    await page.check('#scopeNational');
    await waitText('#winner', /\$10\.58/);
  });

  it('checkout-total ranking refuses unknown fees', async () => {
    await page.selectOption('#rankBy', 'checkout');
    await waitText('#winner', /Unknown charges are never treated as zero/);
    await page.selectOption('#rankBy', 'subtotal');
    await waitText('#winner', /\$10\.58/);
  });

  it('requires all basket items at one branch', async () => {
    await page.click('#addItem');
    await page.fill('#it1_name', 'Fries');
    await page.fill('#it1_size', 'Large');
    await page.click('#it0_hint');
    await page.click('#searchBtn');
    await waitStatus(/completed/);
    await waitText('#winner', /All 2 items at this one branch/);
    assert.match(await text('#winner'), /\$15\.17/);   // 2 × $5.49 + $4.19 at the first DoorDash branch
    await selectRTab('baskets');
    const b = await text('#resultBody');
    assert.match(b, /all items exact/);
    assert.match(b, /missing: Fries/);               // Uber Eats branch has no fries → not combined
    await selectRTab('verified');
  });

  it('adds/removes modifiers and items, and resets the order', async () => {
    await page.click('.item-card >> nth=0 >> button:has-text("+ Add modifier")');
    assert.equal(await page.locator('.item-card >> nth=0 >> .mod').count(), 1);
    await page.fill('.item-card >> nth=0 >> .mod input >> nth=0', 'Extra Sauce');
    await waitText('#winner', /No verified winner yet|Candidates exist/);
    await page.click('.item-card >> nth=0 >> .mod button');
    assert.equal(await page.locator('.mod').count(), 0);
    await page.click('.item-card >> nth=1 >> button:has-text("Remove item")');
    assert.equal(await page.locator('.item-card').count(), 1);
    await waitText('#winner', /\$10\.58/);
  });

  it('isolates demo data behind an explicit, labeled toggle', async () => {
    assert.equal(await page.isVisible('#demoBanner'), false);
    await page.check('#demoToggle');
    assert.equal(await page.isVisible('#demoBanner'), true);
    await page.click('#demoOrderBtn');
    await waitText('#winner', /DEMO DATA/);
    assert.match(await text('#winner'), /\$19\.98/);
    assert.match(await text('#resultBody'), /DEMO/);
    await page.uncheck('#demoToggle');
    assert.equal(await page.isVisible('#demoBanner'), false);
    await waitText('#winner', /No verified winner yet/);  // demo order vs real data: nothing mixes
    assert.doesNotMatch(await text('#resultBody'), /DEMO/);
    await page.click('#resetOrder');
  });

  it('probes platforms', async () => {
    await page.click('#probeBtn');
    await waitText('#progressArea', /Live probe results/, 30000);
    assert.match(await text('#progressArea'), /DoorDash: reachable; robots\.txt permits crawling/);
  });

  it('deletes a checkpoint and purges server data', async () => {
    await page.click('#refreshJobs');
    await page.waitForFunction(() => document.querySelectorAll('#resumeSelect option').length >= 2);
    const val = await page.locator('#resumeSelect option').nth(1).getAttribute('value');
    await page.selectOption('#resumeSelect', val);
    await page.click('#deleteJob');
    await waitText('#searchMsg', /Checkpoint deleted/);
    await page.click('#purgeBtn');
    await waitText('#purgeMsg', /Deleted \d+ checkpoint file/);
    assert.ok(dialogs.length >= 2);
  });

  it('handles a disconnected backend (file:// page)', async () => {
    const p2 = await ctx.newPage();
    const errs = [];
    p2.on('pageerror', e => errs.push(e.message));
    await p2.goto('file://' + path.join(root, 'index.html'));
    await p2.fill('#backendUrl', 'http://127.0.0.1:9');
    await p2.click('#backendConnect');
    await p2.waitForFunction(() => /Backend disconnected/.test(document.querySelector('#backendStatus').innerText));
    assert.equal(await p2.isDisabled('#searchBtn'), true);
    assert.match(await p2.locator('#progressArea').innerText(), /Backend disconnected.*Manual entry, receipt parsing, imports, demo mode and export still work/s);
    await p2.fill('#ghUrl', 'https://www.grubhub.com/restaurant/chipotle-mexican-grill-1-a-st/42');
    await p2.click('#ghExtract');
    assert.match(await p2.locator('#ghResult').innerText(), /Backend not connected/);
    assert.equal(await p2.inputValue('#chain'), 'Chipotle Mexican Grill');
    await p2.close();
    assert.deepEqual(errs, []);
  });

  it('produced no uncaught page errors', () => {
    assert.deepEqual(consoleErrors.filter(e => !/Failed to load resource|ERR_CONNECTION_REFUSED/.test(e)), []);
  });
});
