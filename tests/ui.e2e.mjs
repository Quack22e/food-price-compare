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

  it('adds prices you saw in the DoorDash app by hand, labelled and validated', async () => {
    const seen = async (over) => {
      const f = Object.assign({ seenPlatform: 'doordash', seenMode: 'delivery', seenUrl: 'https://www.doordash.com/store/mcdonalds-reno-777001/',
        seenName: "McDonald's", seenStreet: '1 Seen St', seenCity: 'Reno', seenState: 'NV', seenZip: '89501', seenLines: '10 pc Chicken McNuggets $3.50' }, over);
      for (const [id, v] of Object.entries(f)) {
        if (id === 'seenPlatform' || id === 'seenMode') await page.selectOption('#' + id, v); else await page.fill('#' + id, v);
      }
      await page.click('#seenAdd');
      return text('#seenMsg');
    };
    try {
      assert.match(await seen({ seenUrl: 'https://drd.sh/abc' }), /Not added.*Store link must be a www\.doordash\.com store page/);
      assert.match(await seen({ seenLines: 'McNuggets about 3.50' }), /Not added.*"item name \$price"/);
      assert.match(await seen({ seenZip: 'M5V 2T6' }), /Not added.*5-digit US ZIP/);
      assert.match(await seen({}), /Added 1 price\(s\) at McDonald's, Reno NV \(DoorDash, delivery\), labelled "Imported"/);
      assert.equal(await page.inputValue('#seenLines'), '');
      await waitText('#winner', /\$7\.00/);                                   // quantity 2 x $3.50
      assert.match(await text('#winner'), /DoorDash · McDonald's — 1 Seen St, Reno, NV 89501/);
      assert.match(await text('#resultBody'), /Imported/);
      const link = page.locator('#winner a');
      assert.equal(await link.getAttribute('href'), 'https://www.doordash.com/store/mcdonalds-reno-777001/');
      await page.locator('#resultBody tbody tr').first().locator('button:has-text("Why?")').click();
      const det = await page.locator('#resultBody tr.details').first().innerText();
      assert.match(det, /entered by you from what DoorDash showed you/);
      assert.match(det, /order mode you selected in the DoorDash app/);
    } finally {
      await page.click('#importClear');                                    // never leak into later tests
    }
    await waitText('#winner', /\$10\.58/);
  });

  it('loads a live-run snapshot: one run, labelled, scoped to what each search looked for', async () => {
    const at = new Date(Date.now() - 2 * 3600e3).toISOString();
    const ob = (id, name, price) => ({ platform: 'ubereats', store_id: id, store_name: "McDonald's", store_url: 'https://www.ubereats.com/store/mcdonalds-test/' + id,
      address: { street: id + ' Snap Rd', city: 'Boise', state: 'ID', zip: '83702', country: 'US' }, observed_at: at, order_mode: 'delivery', currency: 'USD',
      extraction: 'schema.org JSON-LD', items: [{ name, price_cents: price, currency: 'USD' }] });
    const snap = { generated_at: at, github_run_id: '999', searches: [
      { label: 'McNuggets search', request: { order: { chain: "McDonald's", items: [{ name: '10 pc. Chicken McNuggets' }] } }, observations: [ob('SnapA1', '10 pc Chicken McNuggets', 425)] },
      { label: 'Big Mac search', request: { order: { chain: "McDonald's", items: [{ name: 'Big Mac' }] } }, observations: [ob('SnapB2', 'Big Mac', 599)] }] };
    try {
      await page.setInputFiles('#snapFile', { name: 'snapshot.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify(snap)) });
      await waitText('#snapMsg', /Snapshot run 999/);
      const m = await text('#snapMsg');
      assert.match(m, /2 checked store page\(s\)\. 1 searched for this order's items; 1 were read only for a different item and are not counted/);
      assert.match(m, /Search: McNuggets search/);
      await waitText('#winner', /\$8\.50/);                                  // 2 x $4.25 from the snapshot
      assert.match(await text('#winner'), /Boise, ID 83702/);
      assert.match(await text('#resultBody'), /Snapshot/);
      assert.doesNotMatch(await text('#resultBody'), /SnapB2 Snap Rd/);      // the Big Mac store is not a checked location here
      await page.uncheck('#includeSnapshot');
      await waitText('#winner', /\$10\.58/);
      await page.check('#includeSnapshot');
      await waitText('#winner', /\$8\.50/);
      // The server's own snapshot file (the committed real one) loads the same way.
      await page.click('#snapServer');
      await waitText('#snapMsg', /from the server/);
      assert.match(await text('#snapMsg'), /Snapshot run \d+ from 20\d\d-.*: [1-9]\d* checked store page\(s\)/);
      await page.setInputFiles('#snapFile', { name: 'bad.json', mimeType: 'application/json', buffer: Buffer.from('{"observations": []}') });
      await waitText('#snapMsg', /Not a snapshot/);
    } finally {
      await page.click('#snapClear');
    }
    await waitText('#winner', /\$10\.58/);
  });

  it('suggests the closest listings when nothing matches exactly, and "Same item" confirms one', async () => {
    const at = new Date(Date.now() - 3600e3).toISOString();
    const rec = { platform: 'ubereats', store_id: 'Fry01', store_name: "McDonald's", store_url: 'https://www.ubereats.com/store/mcd/Fry01',
      address: { street: '5 Fry Rd', city: 'Boise', state: 'ID', zip: '83702', country: 'US' }, observed_at: at, order_mode: 'delivery',
      items: [{ name: 'World Famous Fries® - Large [320-490 Cals]', price: '4.49' }, { name: 'Fries Meal', price: '8.99' }] };
    const before = { name: await page.inputValue('#it0_name'), count: await page.inputValue('#it0_count'), qty: await page.inputValue('#it0_qty') };
    try {
      await page.fill('#importText', JSON.stringify([rec]));
      await page.click('#importBtn');
      await page.fill('#it0_name', 'Large Fries');
      await page.fill('#it0_count', '');
      await page.fill('#it0_qty', '1');
      await waitText('#closest', /No checked store lists "Large Fries" exactly/);
      assert.match(await text('#closest'), /World Famous Fries® - Large \[320-490 Cals\] · 1 store · \$4\.49 · name differs only by "world famous"/);
      assert.doesNotMatch(await text('#closest'), /Fries Meal/);
      assert.doesNotMatch(await text('#winner'), /\$4\.49/);
      await page.click('#closest button:has-text("Same item")');
      await waitText('#winner', /\$4\.49/);
      assert.equal(await page.inputValue('#it0_alias'), 'World Famous Fries® - Large');   // calorie note dropped from the saved name
      assert.equal(await page.locator('#closest .closest').count(), 0);
    } finally {
      await page.click('#importClear');
      await page.fill('#it0_alias', '');
      await page.fill('#it0_name', before.name); await page.fill('#it0_count', before.count); await page.fill('#it0_qty', before.qty);
    }
    await waitText('#winner', /\$10\.58/);
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

  it('fills the Grubhub item and price from the Grubhub-owned MenuPages menu', async () => {
    await page.click('#resetOrder');
    await page.click('#inputTabs button[data-tab="link"]');
    await page.fill('#ghUrl', 'https://www.grubhub.com/restaurant/lou-malnatis-pizzeria-6456-s-cicero-ave-chicago/434510');
    await page.click('#ghExtract');
    await page.waitForSelector('#ghMenuSelect', { timeout: 30000 });
    assert.equal(await page.inputValue('#chain'), "Lou Malnati's Pizzeria");
    assert.match(await text('#ghMenu'), /Grubhub menu for Lou Malnati's Pizzeria, 6456 S Cicero Ave, Chicago, IL/);
    await page.fill('#it0_qty', '2');
    const opt = await page.locator('#ghMenuSelect option', { hasText: 'Small - Deep Dish Margherita' }).getAttribute('value');
    await page.selectOption('#ghMenuSelect', opt);
    assert.equal(await page.inputValue('#it0_name'), 'Small - Deep Dish Margherita');
    assert.equal(await page.inputValue('#it0_gh'), '43.10');
    assert.match(await text('#it0_hint'), /Grubhub price \$21\.55 × 2 from MenuPages/);
    assert.match(await text('#it0_hint'), /Detected in name: size small/);
    // Quantity edits keep a picked Grubhub price in step; a new pick starts a fresh item.
    await page.fill('#it0_qty', '3');
    assert.equal(await page.inputValue('#it0_gh'), '64.65');
    assert.match(await text('#it0_hint'), /Grubhub price \$21\.55 × 3 from MenuPages/);
    await page.fill('#it0_flavor', 'Pesto');
    const opt2 = await page.locator('#ghMenuSelect option', { hasText: 'Personal - Deep Dish Margherita' }).getAttribute('value');
    await page.selectOption('#ghMenuSelect', opt2);
    assert.equal(await page.inputValue('#it0_name'), 'Personal - Deep Dish Margherita');
    assert.equal(await page.inputValue('#it0_flavor'), '');
    assert.equal(await page.inputValue('#it0_gh'), '42.75');            // $14.25 × 3, quantity kept
    assert.equal(await page.inputValue('#ghMenuSelect'), '');           // placeholder again; re-picking it changes nothing
    await page.click('#addItem');
    assert.equal(await page.locator('#ghMenuTarget option').count(), 2); // the target list follows the order
    await page.locator('#items .item-card').nth(1).locator('button:has-text("Remove item")').click();
    assert.equal(await page.locator('#ghMenuTarget option').count(), 1);
    // The fixture source is http://127.0.0.1, which the URL allowlist refuses to link.
    assert.match(await text('#ghMenu'), /Source page \(no link\)/);
    assert.equal(await page.locator('#ghMenu a').count(), 0);
    // A rejected URL clears the previous restaurant's menu.
    await page.fill('#ghUrl', 'https://example.com/foo');
    await page.click('#ghExtract');
    assert.equal(await page.locator('#ghMenuSelect').count(), 0);
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
    assert.equal(await p2.isDisabled('#snapServer'), true);              // a snapshot file still loads without a backend
    assert.match(await p2.locator('#progressArea').innerText(), /Backend disconnected.*Manual entry, receipt parsing, imports, demo mode and export still work/s);
    assert.match(await p2.locator('#progressArea').innerText(), /FPC_ALLOW_NULL_ORIGIN=1/);  // why a file:// page can't reach a running service
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
