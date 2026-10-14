// Browser-capture bookmarklet: run the exact <script id="fpc-capture"> from
// index.html on store-page URLs (served locally via request interception),
// then feed its JSON through the app's real importer and ranking engine.
//   node --test tests/capture.test.mjs
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';
import path from 'node:path';
import vm from 'node:vm';

const require = createRequire(import.meta.url);
let pw;
try { pw = require('playwright'); } catch { pw = require(path.join(execSync('npm root -g').toString().trim(), 'playwright')); }
const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const CAPTURE = /<script type="text\/plain" id="fpc-capture">([\s\S]*?)<\/script>/.exec(html)[1];
vm.runInThisContext(/<script id="fpc-engine">([\s\S]*?)<\/script>/.exec(html)[1]);
const E = globalThis.FPCEngine;

// SYNTHETIC page following schema.org (shape matches what real Uber Eats pages published in the 2026-10-08 live runs).
const ld = {
  '@context': 'https://schema.org', '@type': 'Restaurant', name: 'McDonald&#39;s® (Main &amp; 1st)',
  address: { '@type': 'PostalAddress', streetAddress: '1 Main St', addressLocality: 'Columbus', addressRegion: 'OH', postalCode: '43215', addressCountry: 'US' },
  hasMenu: { '@type': 'Menu', hasMenuSection: [
    { '@type': 'MenuSection', name: 'Featured items', hasMenuItem: [
      { '@type': 'MenuItem', name: '10 pc. Chicken McNuggets®', offers: { '@type': 'Offer', price: '6.49', priceCurrency: 'USD' } },
      { '@type': 'MenuItem', name: 'Iced Coffee', offers: { '@type': 'Offer', price: '0.00', priceCurrency: 'USD' } }] },
    { '@type': 'MenuSection', name: 'Most Popular', hasMenuItem: [
      { '@type': 'MenuItem', name: '10 pc. Chicken McNuggets®', offers: { '@type': 'Offer', price: '6.49', priceCurrency: 'USD' } }] }] },
};
const page = body => `<!doctype html><html><head><meta charset="utf-8"><title>t</title><script type="application/ld+json">${JSON.stringify(body)}</script></head><body><h1>store</h1></body></html>`;

let browser, ctx;
before(async () => { browser = await pw.chromium.launch(); ctx = await browser.newContext(); });
after(async () => { await browser.close(); });

async function runOn(url, body) {
  const p = await ctx.newPage();
  await p.route('**/*', r => r.request().url() === url ? r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body }) : r.abort());
  await p.goto(url);
  const res = await p.evaluate(src => { window.__fpcNoOverlay = true; return (0, eval)(src); }, CAPTURE);
  await p.close();
  return res;
}

test('captures an Uber Eats store page into a valid import record', async () => {
  const url = 'https://www.ubereats.com/store/mcdonalds-main-1st/AbCdEf123456?diningMode=DELIVERY';
  const res = await runOn(url, page(ld));
  assert.equal(res.ok, true, JSON.stringify(res.notes));
  const o = res.observation;
  assert.equal(o.platform, 'ubereats');
  assert.equal(o.store_id, 'AbCdEf123456');
  assert.equal(o.store_url, 'https://www.ubereats.com/store/mcdonalds-main-1st/AbCdEf123456');
  assert.equal(o.store_name, "McDonald's® (Main & 1st)");             // entities decoded
  assert.equal(o.items.length, 2);                                     // repeated listing collapsed
  const coffee = o.items.find(i => i.name === 'Iced Coffee');
  assert.equal(coffee.price_cents, null);                              // $0.00 is not a price
  const v = E.validateImport(JSON.stringify({ observations: [o] }), { now: Date.now() + 1000 });
  assert.deepEqual(v.errors, []);
  assert.equal(v.observations[0].source, 'import');
  assert.match(v.observations[0].items.find(i => i.name === 'Iced Coffee').price_note, /\$0\.00/);
  const ev = E.evaluate({ chain: "McDonald's", items: [{ name: '10 pc Chicken McNuggets', quantity: '2' }] }, v.observations, { platforms: ['ubereats'] });
  assert.equal(ev.winner.row.lineCents, 1298);
  assert.match(ev.winnerLabel, /among 1 checked location/);
});

test('DoorDash store page: pickup taken from the page address', async () => {
  const res = await runOn('https://www.doordash.com/store/mcdonalds-columbus-123456/?pickup=true', page(ld));
  assert.equal(res.ok, true);
  assert.equal(res.observation.platform, 'doordash');
  assert.equal(res.observation.store_id, '123456');
  assert.equal(res.observation.order_mode, 'pickup');
  assert.deepEqual(E.validateImport(JSON.stringify({ observations: [res.observation] }), { now: Date.now() + 1000 }).errors, []);
});

// SYNTHETIC page in the layout of real DoorDash store pages (archived captures,
// scripts/archive_probe.py): nested section list, offers without priceCurrency,
// no postalCode, and the currency stated only in the page's own app data.
const ddLd = {
  '@context': 'https://schema.org', '@type': 'Restaurant', name: 'Taco Bell',
  address: { '@type': 'PostalAddress', streetAddress: '1170 Northwest 185th Avenue', addressLocality: 'Beaverton', addressRegion: 'OR', addressCountry: 'US' },
  hasMenu: { '@type': 'Menu', hasMenuSection: [[{ '@type': 'MenuSection', name: 'Most Ordered', hasMenuItem: [
    { '@type': 'MenuItem', name: 'Crunchwrap Supreme®', offers: { '@type': 'Offer', price: '8.53' } }] }]] },
};
const ddPage = (cur) => page(ddLd).replace('</body>', cur == null ? '</body>'
  : `<script>self.__next_f.push([1,"{\\"price\\":{\\"currency\\":\\"${cur}\\",\\"decimalPlaces\\":2},\\"displayPrice\\":\\"$$8.53\\"}"])</script></body>`);

test('DoorDash layout: a missing currency counts as USD only when the page data states USD alone', async () => {
  const url = 'https://www.doordash.com/store/taco-bell-1194579/';
  const ok = await runOn(url, ddPage('USD'));
  assert.equal(ok.ok, true, JSON.stringify(ok.notes));
  assert.equal(ok.observation.currency, 'USD');
  assert.deepEqual(ok.observation.items.map(i => [i.name, i.price_cents, i.currency]), [['Crunchwrap Supreme®', 853, 'USD']]);
  assert.match(ok.notes.join(' '), /page's own data states USD and no other/);
  const v = E.validateImport(JSON.stringify({ observations: [ok.observation] }), { now: Date.now() + 1000 });
  assert.deepEqual(v.errors, []);
  assert.equal(v.observations[0].address.zip, '');                     // DoorDash publishes no ZIP
  for (const cur of ['CAD', null]) {
    const bad = await runOn(url, ddPage(cur));
    assert.notEqual(bad.observation.currency, 'USD');
    assert.match(bad.notes.join(' '), /not taken as USD/);
    assert.equal(E.validateImport(JSON.stringify({ observations: [bad.observation] }), { now: Date.now() + 1000 }).observations.length, 0);
  }
});

test('DoorDash price strings: "US$12.75" is a price, "$13.59+" a starting price', async () => {
  const ld = JSON.parse(JSON.stringify(ddLd));
  ld.hasMenu.hasMenuSection[0][0].hasMenuItem = [
    { '@type': 'MenuItem', name: 'Burrito Bowl', offers: { '@type': 'Offer', price: 'US$12.75' } },
    { '@type': 'MenuItem', name: 'Deep Dish Cheese', offers: { '@type': 'Offer', price: '$13.59+' } }];
  const res = await runOn('https://www.doordash.com/store/chipotle-mexican-grill-26003435/', ddPage('USD').replace(JSON.stringify(ddLd), JSON.stringify(ld)));
  assert.equal(res.ok, true, JSON.stringify(res.notes));
  const [bowl, pie] = res.observation.items;
  assert.deepEqual([bowl.name, bowl.price_cents, bowl.currency], ['Burrito Bowl', 1275, 'USD']);
  assert.equal(pie.price_cents, null);
  assert.match(pie.price_note, /starting price \(\$13\.59\+\)/);
  assert.equal(pie.required_options[0].required, true);
});

test('refuses to guess: no structured data, not a store page, Grubhub, other sites', async () => {
  const bare = '<!doctype html><html><body><div>$6.49 Big Mac</div></body></html>';
  let r = await runOn('https://www.doordash.com/store/x-1/', bare);
  assert.equal(r.ok, false);
  assert.match(r.notes.join(' '), /no schema\.org restaurant data/);
  r = await runOn('https://www.ubereats.com/brand/mcdonalds', page(ld));
  assert.equal(r.ok, false);
  assert.match(r.notes.join(' '), /single store page/);
  r = await runOn('https://www.grubhub.com/restaurant/x/123', page(ld));
  assert.equal(r.ok, false);
  assert.match(r.notes.join(' '), /Grubhub/);
  r = await runOn('https://example.com/store/x/AbCdEf123456', page(ld));
  assert.equal(r.ok, false);
});

test('overlay renders untrusted names as text and offers copy', async () => {
  const evil = JSON.parse(JSON.stringify(ld));
  evil.name = '<img src=x onerror="window.__pwned=1">Burger';
  const p = await ctx.newPage();
  const url = 'https://www.ubereats.com/store/evil/AbCdEf123456';
  await p.route('**/*', r => r.request().url() === url ? r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: page(evil) }) : r.abort());
  await p.goto(url);
  await p.evaluate(src => (0, eval)(src), CAPTURE);
  const panel = p.locator('#fpc-capture-panel');
  assert.match(await panel.innerText(), /<img src=x/);
  assert.equal(await p.evaluate(() => window.__pwned === 1), false);
  assert.equal(await panel.locator('img').count(), 0);
  assert.equal(await panel.locator('button', { hasText: 'Copy for import' }).count(), 1);
  await panel.locator('button', { hasText: 'Switch to pickup' }).click();
  assert.match(await p.locator('#fpc-capture-panel textarea').inputValue(), /"order_mode": "pickup"/);
  await p.close();
});

test('app builds a working bookmarklet link from the same code', async () => {
  const p = await ctx.newPage();
  await p.goto('file://' + path.resolve(new URL('../index.html', import.meta.url).pathname));
  const href = await p.getAttribute('#captureBookmarklet', 'href');
  assert.match(href, /^javascript:/);
  const code = decodeURIComponent(href.slice('javascript:'.length));
  assert.doesNotThrow(() => new Function(code));
  await p.close();
  const url = 'https://www.ubereats.com/store/mcdonalds-main-1st/AbCdEf123456';
  const q = await ctx.newPage();
  await q.route('**/*', r => r.request().url() === url ? r.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: page(ld) }) : r.abort());
  await q.goto(url);
  await q.evaluate(c => (0, eval)(c), code);                         // the minified bookmarklet itself
  assert.match(await q.locator('#fpc-capture-panel').innerText(), /2 menu listings \(1 with a price\)/);
  await q.close();
});
