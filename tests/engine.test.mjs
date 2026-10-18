// Engine tests: run the exact <script id="fpc-engine"> code shipped in index.html.
//   node --test tests/engine.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const m = /<script id="fpc-engine">([\s\S]*?)<\/script>/.exec(html);
assert.ok(m, 'engine script block not found');
vm.runInThisContext(m[1], { filename: 'index.html#fpc-engine' });
const E = globalThis.FPCEngine;

const NOW = Date.parse('2026-10-07T12:00:00Z');
const T = (h = 1) => new Date(NOW - h * 3600e3).toISOString();
let sid = 0;
function obs(o = {}) {
  sid++;
  return Object.assign({
    source: 'live', platform: 'doordash', store_id: 'S' + sid, store_name: "McDonald's",
    store_url: 'https://www.doordash.com/store/mcdonalds-x-' + sid + '/',
    address: { street: sid + ' Main St', city: 'Springfield', state: 'IL', zip: '627' + String(sid).padStart(2, '0'), country: 'US' },
    observed_at: T(1), order_mode: 'delivery', currency: 'USD', items: [],
    fees: { delivery_fee_cents: null, service_fee_cents: null, small_order_fee_cents: null, tax_cents: null, destination_zip: null }
  }, o);
}
const item = (name, cents, extra = {}) => Object.assign({ name, price_cents: cents, currency: 'USD', availability: 'available', addons: [], required_options: 'unknown' }, extra);
const order = (items, extra = {}) => Object.assign({ chain: "McDonald's", chainAliases: '', mode: 'delivery', items: items.map(i => Object.assign({ name: '', size: '', flavor: '', count: '', quantity: '1', ghSubtotal: '', modifiers: [], aliases: '' }, i)) }, extra);
const run = (o, observations, opts = {}) => E.evaluate(o, observations, Object.assign({ now: NOW, platforms: ['doordash', 'ubereats'] }, opts));
const match1 = (orderItem, cand) => E.matchItem(E.prepareItem(Object.assign({ name: '', size: '', flavor: '', count: '', quantity: '1', ghSubtotal: '', modifiers: [], aliases: '' }, orderItem), 0), cand);

test('integer-cent money parsing and formatting', () => {
  assert.equal(E.parseMoneyToCents('$5.49'), 549);
  assert.equal(E.parseMoneyToCents('5.5'), 550);
  assert.equal(E.parseMoneyToCents('1,299.00'), 129900);
  assert.equal(E.parseMoneyToCents('+$1.50'), 150);
  assert.equal(E.parseMoneyToCents('12,99'), null);
  assert.equal(E.parseMoneyToCents('9.999'), null);
  assert.equal(E.parseMoneyToCents('9.990'), 999);
  assert.equal(E.parseMoneyToCents(0.1 + 0.2), null, 'binary-float noise is rejected, not rounded');
  assert.equal(E.parseMoneyToCents(-1), null);
  assert.equal(E.formatCents(-123456), '-$1,234.56');
  assert.equal(E.formatCents(5), '$0.05');
  assert.equal(E.mulCents(333, 3), 999);
  assert.throws(() => E.mulCents(1.5, 2));
  assert.equal(E.sumCents([10, 10, 10]), 30);
  assert.equal(E.sumCents([10, null]), null, 'unknown component makes the sum unknown, never zero');
});

test('harmless normalization only', () => {
  assert.equal(E.matchChain("McDonald's®", 'McDonalds (Main St)').status, 'exact');
  assert.equal(E.matchChain('Chick-fil-A', 'Chick fil A').status, 'exact');
  assert.equal(E.matchChain('Chipotle', 'Chipotle Mexican Grill').status, 'uncertain');
  assert.equal(E.matchChain('Chipotle', 'Chipotle Mexican Grill', ['Chipotle Mexican Grill']).status, 'exact');
  assert.equal(E.matchChain("McDonald's", 'Burger King').status, 'mismatch');
  assert.equal(match1({ name: 'Mac & Cheese' }, item('Mac and Cheese', 500)).status !== 'rejected', true);
  assert.equal(match1({ name: "Sweet 'N Sour Sauce" }, item('Sweet and Sour Sauce', 0)).status !== 'rejected', true);
  assert.equal(match1({ name: 'Spicy Chicken Sandwich' }, item('Chicken Sandwich', 500)).status, 'rejected', 'fuzzy names are not equivalence');
});

test('size mismatches, matches and missing sizes', () => {
  assert.equal(match1({ name: 'Large Fries' }, item('Medium Fries', 389)).status, 'rejected');
  const ok = match1({ name: 'Fries', size: 'Large' }, item('Fries', 419, { variant: 'Large' }));
  assert.equal(ok.status, 'exact');
  assert.equal(ok.lineCents, 419);
  assert.equal(match1({ name: 'Fries', size: 'Large' }, item('Fries', 419)).status, 'uncertain');
  assert.equal(match1({ name: 'Fries' }, item('Fries', 419, { variant: 'Large' })).status, 'uncertain');
  assert.equal(match1({ name: 'Iced Coffee', size: '16 oz' }, item('Iced Coffee 24 oz', 300)).status, 'rejected');
  assert.equal(match1({ name: 'Iced Coffee', size: '16 oz' }, item('Iced Coffee (16oz)', 300)).status, 'exact');
  assert.equal(match1({ name: 'Large Iced Coffee' }, item('Iced Coffee', 300, { variant: 'Large 32 oz' })).status, 'exact');
  assert.equal(match1({ name: 'Iced Coffee', size: '32 oz' }, item('Large Iced Coffee', 300)).status, 'uncertain');
  assert.equal(match1({ name: 'Turkey Sub', size: '6"' }, item('Turkey Sub Footlong', 999)).status, 'rejected');   // a footlong is 12"
  assert.equal(match1({ name: 'Turkey Sub', size: '12 inch' }, item('Turkey Sub Footlong', 999)).status, 'exact');
  assert.equal(match1({ name: 'Turkey Sub 6 inch' }, item('6" Turkey Sub', 699)).status, 'exact');
});

test('piece-count mismatches', () => {
  assert.equal(match1({ name: '10 pc Chicken McNuggets' }, item('20 pc. Chicken McNuggets®', 999)).status, 'rejected');
  assert.equal(match1({ name: '10 pc Chicken McNuggets' }, item('10 piece Chicken McNuggets', 549)).status, 'exact');
  assert.equal(match1({ name: 'Chicken McNuggets', count: '10' }, item('Chicken McNuggets', 549, { variant: '10 pc' })).status, 'exact');
  assert.equal(match1({ name: 'Chicken McNuggets', count: '10' }, item('Chicken McNuggets', 549)).status, 'uncertain');
  assert.equal(match1({ name: 'Donuts', count: '12' }, item('Dozen Donuts', 1299)).status, 'exact');
  const conflict = match1({ name: '10 pc Chicken McNuggets', count: '20' }, item('20 pc Chicken McNuggets', 999));
  assert.equal(conflict.status, 'uncertain', 'order conflict needs confirmation');
});

test('flavor handling', () => {
  assert.equal(match1({ name: 'Wings', flavor: 'Buffalo', count: '10' }, item('10 pc Buffalo Wings', 1299)).status, 'exact');
  assert.equal(match1({ name: 'Wings', flavor: 'Buffalo', count: '10' }, item('10 pc Lemon Pepper Wings', 1299)).status, 'rejected');
  const viaAddon = match1({ name: 'Wings', flavor: 'Buffalo', count: '10' }, item('10 pc Wings', 1299, { addons: [{ name: 'Buffalo', group: 'Flavor', price_cents: 50 }] }));
  assert.equal(viaAddon.status, 'exact');
  assert.equal(viaAddon.unitCents, 1349);
  assert.equal(match1({ name: 'Wings', flavor: 'Buffalo', count: '10' }, item('10 pc Wings', 1299)).status, 'uncertain');
  // No flavor in the order: "Buffalo Wings" may be the same item named with its
  // sauce, so it needs confirmation; it is never verified on its own.
  const noFlavor = match1({ name: 'Wings' }, item('Buffalo Wings', 1299));
  assert.equal(noFlavor.status, 'uncertain');
  assert.match(noFlavor.reasons.map(r => r.text).join(' '), /Name differs only by "buffalo"/);
});

test('modifiers, removals, quantities and missing charges', () => {
  const cand = item('Quarter Pounder with Cheese', 649, { addons: [{ name: 'Bacon', group: 'Extras', price_cents: 150 }, { name: 'No Onions', group: 'Remove', price_cents: 0 }] });
  const r = match1({ name: 'Quarter Pounder w/ Cheese', quantity: '2', modifiers: [{ name: 'Add Bacon', price: '1.50' }, { name: 'No Onions', price: '' }] }, cand);
  assert.equal(r.status, 'exact');
  assert.equal(r.modifierCents, 150);
  assert.equal(r.unitCents, 799);
  assert.equal(r.lineCents, 1598, 'quantity × (base + modifiers)');
  const missingMod = match1({ name: 'Quarter Pounder with Cheese', modifiers: [{ name: 'Extra Pickles', price: '0.30' }] }, cand);
  assert.equal(missingMod.status, 'uncertain');
  assert.equal(missingMod.lineCents, null, 'unknown modifier charge → no verified total');
  const removal = match1({ name: 'Quarter Pounder with Cheese', modifiers: [{ name: 'No Pickles', price: '' }] }, cand);
  assert.equal(removal.status, 'uncertain');
  const unpricedAddon = match1({ name: 'Quarter Pounder with Cheese', modifiers: [{ name: 'Bacon' }] }, item('Quarter Pounder with Cheese', 649, { addons: [{ name: 'Bacon', price_cents: null }] }));
  assert.equal(unpricedAddon.status, 'uncertain');
  const reqd = match1({ name: 'Burrito' }, item('Burrito', 899, { required_options: [{ name: 'Choose protein', required: true }] }));
  assert.equal(reqd.status, 'uncertain', 'mandatory option without a priced selection');
  const reqdOk = match1({ name: 'Burrito', modifiers: [{ name: 'Steak' }] }, item('Burrito', 899, { required_options: [{ name: 'Choose protein' }], addons: [{ name: 'Steak', group: 'Choose protein', price_cents: 200 }] }));
  assert.equal(reqdOk.status, 'exact');
  assert.equal(reqdOk.lineCents, 1099);
  assert.equal(match1({ name: 'Burrito' }, item('Burrito', null)).status, 'uncertain', 'no price published');
  assert.equal(match1({ name: 'Burrito' }, item('Burrito', null, { price_range_cents: [800, 1200] })).status, 'uncertain');
  assert.equal(match1({ name: 'Burrito', quantity: '1.5' }, item('Burrito', 899)).status, 'uncertain', 'invalid quantity');
  const q3 = match1({ name: 'Cookie', quantity: '3' }, item('Cookie', 333));
  assert.equal(q3.lineCents, 999);
});

test('currency must be USD', () => {
  assert.equal(match1({ name: 'Burrito' }, item('Burrito', 899, { currency: 'CAD' })).status, 'rejected');
  assert.equal(match1({ name: 'Burrito' }, item('Burrito', 899, { currency: null })).status, 'uncertain');
});

test('pickup and delivery are never mixed', () => {
  const o = order([{ name: 'Burrito' }]);
  const d = obs({ items: [item('Burrito', 900)] });
  const p = obs({ order_mode: 'pickup', items: [item('Burrito', 700)] });
  const ev = run(o, [d, p], { mode: 'delivery' });
  assert.equal(ev.winner.row.lineCents, 900);
  assert.equal(ev.coverage.excluded.otherMode, 1);
  const evp = run(o, [d, p], { mode: 'pickup' });
  assert.equal(evp.winner.row.lineCents, 700);
  assert.equal(evp.winner.row.orderMode, 'pickup');
});

test('winner label, savings vs Grubhub item subtotal, unavailable and stale handling', () => {
  const o = order([{ name: 'Burrito', quantity: '2', ghSubtotal: '21.98' }]);
  const a = obs({ items: [item('Burrito', 899)] });
  const b = obs({ platform: 'ubereats', store_url: 'https://www.ubereats.com/store/x/abcdefgh', items: [item('Burrito', 799, { availability: 'unavailable' })] });
  const c = obs({ items: [item('Burrito', 699)], observed_at: T(48) });
  const ev = run(o, [a, b, c], { staleHours: 24 });
  assert.equal(ev.winnerLabel, 'Lowest verified price among 3 checked locations');
  assert.ok(!/cheapest in the us/i.test(ev.winnerLabel));
  assert.equal(ev.winner.row.storeId, a.store_id, 'unavailable and stale results cannot win');
  assert.equal(ev.winner.row.lineCents, 1798);
  assert.equal(ev.winner.row.savingsCents, 2198 - 1798);
  assert.equal(ev.coverage.staleVerified, 1);
  assert.equal(ev.rows.verified.find(r => r.storeId === c.store_id).stale, true);
  assert.equal(ev.rows.verified.find(r => r.storeId === b.store_id).availability, 'unavailable');
  const ex = run(o, [a], { exhaustiveEvidence: true });
  assert.match(ex.winnerLabel, /exhaustive/);
  assert.equal(run(o, [c], { staleHours: 24 }).winner, null, 'only stale data → no winner');
});

test('non-US, other chains and location filter', () => {
  const o = order([{ name: 'Burrito' }]);
  const on = obs({ address: { street: '1 King', city: 'Toronto', state: 'ON', zip: 'M5H', country: 'CA' }, items: [item('Burrito', 100)] });
  const pr = obs({ address: { street: '1 Calle', city: 'San Juan', state: 'PR', zip: '00901', country: 'US' }, items: [item('Burrito', 100)] });
  const bk = obs({ store_name: 'Burger King', items: [item('Burrito', 100)] });
  const tx = obs({ address: { street: '9 Elm', city: 'Austin', state: 'TX', zip: '78701', country: 'US' }, items: [item('Burrito', 950)] });
  const ev = run(o, [on, pr, bk, tx]);
  assert.equal(ev.coverage.excluded.nonUS, 2);
  assert.equal(ev.coverage.excluded.otherChain, 1);
  assert.equal(ev.winner.row.lineCents, 950);
  const il = obs({ items: [item('Burrito', 990)] });
  const loc = run(o, [tx, il], { filters: { state: 'IL', city: '', zip: '' } });
  assert.equal(loc.winner.row.state, 'IL');
  assert.equal(loc.coverage.excluded.location, 1);
});

test('duplicate branches', () => {
  const o = order([{ name: 'Burrito' }]);
  const old = obs({ store_id: 'D1', observed_at: T(5), items: [item('Burrito', 500)] });
  const fresh = Object.assign({}, old, { observed_at: T(1), items: [item('Burrito', 650)] });
  const ev = run(o, [old, fresh]);
  assert.equal(ev.rows.verified.length, 1);
  assert.equal(ev.rows.verified[0].lineCents, 650, 'newest observation of a store wins');
  assert.equal(ev.coverage.duplicates, 1);
  const twin = obs({ store_id: 'D2', address: Object.assign({}, old.address), items: [item('Burrito', 480)] });
  const ev2 = run(o, [fresh, twin]);
  assert.equal(ev2.coverage.checkedLocations, 1, 'same address counted once');
  assert.equal(ev2.rows.verified.length, 2, 'both listings shown');
  assert.equal(ev2.rows.verified.find(r => r.storeId === 'D2').duplicateOf, 'D1');
});

test('same-branch baskets never combine restaurants', () => {
  const o = order([{ name: 'Burrito', ghSubtotal: '10.00' }, { name: 'Chips', ghSubtotal: '3.00' }]);
  const both = obs({ items: [item('Burrito', 900), item('Chips', 300)] });
  const cheapBurritoOnly = obs({ items: [item('Burrito', 500)] });
  const cheapChipsOnly = obs({ platform: 'ubereats', store_url: 'https://www.ubereats.com/store/y/zzzzzzzz', items: [item('Chips', 100)] });
  const chipsUnavailable = obs({ items: [item('Burrito', 400), item('Chips', 100, { availability: 'unavailable' })] });
  const ev = run(o, [both, cheapBurritoOnly, cheapChipsOnly, chipsUnavailable]);
  assert.equal(ev.multi, true);
  assert.equal(ev.winner.kind, 'basket');
  assert.equal(ev.winner.basket.storeId, both.store_id);
  assert.equal(ev.winner.basket.subtotalCents, 1200);
  assert.equal(ev.winner.basket.savingsCents, 100);
  assert.equal(ev.baskets.find(b => b.storeId === cheapBurritoOnly.store_id).status, 'partial');
  const un = ev.baskets.find(b => b.storeId === chipsUnavailable.store_id);
  assert.equal(un.status, 'partial');
  assert.deepEqual(un.missing, ['Chips (unavailable)']);
});

test('checkout-total ranking only with all destination charges known', () => {
  const o = order([{ name: 'Burrito' }]);
  const nofees = obs({ items: [item('Burrito', 500)] });
  const fees = obs({ items: [item('Burrito', 700)], fees: { delivery_fee_cents: 199, service_fee_cents: 150, small_order_fee_cents: 0, tax_cents: 60, destination_zip: '62701' } });
  const partial = obs({ items: [item('Burrito', 600)], fees: { delivery_fee_cents: 0, service_fee_cents: null, small_order_fee_cents: 0, tax_cents: 50, destination_zip: '62701' } });
  const ev = run(o, [nofees, fees, partial], { rankBy: 'checkout' });
  assert.equal(ev.winner.row.storeId, fees.store_id);
  assert.equal(ev.winner.row.checkoutTotalCents, 700 + 199 + 150 + 0 + 60);
  assert.equal(ev.rows.verified.find(r => r.storeId === partial.store_id).checkoutTotalCents, null, 'unknown service fee is not zero');
  assert.equal(run(o, [nofees], { rankBy: 'checkout' }).winner, null);
  assert.equal(run(o, [nofees, fees], { rankBy: 'subtotal' }).winner.row.storeId, nofees.store_id);
});

test('imported vs demo isolation and include toggle', () => {
  const o = order([{ name: 'Burrito' }]);
  const imp = Object.assign(obs({ items: [item('Burrito', 300)] }), { source: 'import' });
  const live = obs({ items: [item('Burrito', 500)] });
  assert.equal(run(o, [imp, live]).winner.row.source, 'import');
  assert.equal(run(o, [imp, live], { includeImported: false }).winner.row.source, 'live');
  const demo = E.demoObservations(NOW);
  assert.ok(demo.every(d => d.source === 'demo' && d.store_url === null));
  const dev = run(E.demoOrder(), demo);
  assert.ok(dev.winner && dev.winner.row.source === 'demo');
  assert.equal(dev.winner.row.lineCents, 2 * (849 + 150));
});

test('partial data does not crash: missing items, addresses, timestamps', () => {
  const o = order([{ name: 'Burrito' }]);
  const ev = run(o, [obs({ items: undefined }), obs({ observed_at: 'not a date', items: [item('Burrito', 100)] }), obs({ address: null, items: [item('Burrito', 1)] })]);
  assert.equal(ev.winner, null, 'unparseable timestamp counts as stale');
  assert.equal(ev.coverage.excluded.nonUS, 1);
});

test('receipt parsing', () => {
  const r = E.parseReceipt([
    "Thanks! Your order from McDonald's is on the way",
    'Delivery to 123 Main St',
    '1  10 pc. Chicken McNuggets   $6.49',
    "     Sweet 'N Sour Sauce",
    '2 x Large Fries  $8.38',
    '   + Extra Salt (+$0.25)',
    'Tip Top Wings $9.99',
    'Subtotal $24.86',
    'Delivery fee $1.99',
    'Service fee $3.73',
    'Sales tax $2.10',
    'Driver tip $4.00',
    'Promo SAVE5 -$5.00',
    'Total $31.68'
  ].join('\n'));
  assert.equal(r.chain, "McDonald's");
  assert.equal(r.mode, 'delivery');
  assert.equal(r.items.length, 3);
  assert.deepEqual([r.items[0].name, r.items[0].quantity, r.items[0].ghSubtotal], ['10 pc. Chicken McNuggets', '1', '6.49']);
  assert.deepEqual(r.items[0].modifiers, [{ name: "Sweet 'N Sour Sauce", price: '' }]);
  assert.deepEqual([r.items[1].name, r.items[1].quantity, r.items[1].ghSubtotal], ['Large Fries', '2', '8.38']);
  assert.deepEqual(r.items[1].modifiers, [{ name: 'Extra Salt', price: '0.25' }]);
  assert.equal(r.items[2].name, 'Tip Top Wings', 'item names starting with fee words are kept');
  assert.equal(r.ignored.length, 8, 'seven fee/total lines plus the delivery-address line');
  assert.ok(['Subtotal $24.86', 'Delivery fee $1.99', 'Service fee $3.73', 'Sales tax $2.10', 'Driver tip $4.00', 'Promo SAVE5 -$5.00', 'Total $31.68'].every(l => r.ignored.includes(l)));
  assert.ok(r.items[2]._notes.some(n => /assumed 1/.test(n)));
  const pick = E.parseReceipt('Order from Chipotle\nPickup at 5:30pm\n1 Burrito Bowl $10.25');
  assert.equal(pick.mode, 'pickup');
  assert.equal(E.parseReceipt('hello').items.length, 0);
});

test('Grubhub URL parsing', () => {
  const g = E.parseGrubhubUrl('https://www.grubhub.com/restaurant/mcdonalds-1234-n-main-st-chicago/2345678?menuItemId=99887766');
  assert.equal(g.ok, true);
  assert.equal(g.chainGuess, 'Mcdonalds');
  assert.equal(g.restaurantId, '2345678');
  assert.equal(g.menuItemId, '99887766');
  assert.equal(E.parseGrubhubUrl('https://www.doordash.com/store/x-1/').ok, false);
  assert.equal(E.parseGrubhubUrl('http://www.grubhub.com/restaurant/x/1').ok, false);
  assert.equal(E.parseGrubhubUrl('nonsense').ok, false);
});

test('import validation (JSON and CSV) and safe URLs', () => {
  const good = { platform: 'doordash', store_id: '123', store_name: "McDonald's", store_url: 'https://www.doordash.com/store/mcdonalds-1-123/',
    address: { street: '1 Main', city: 'Austin', state: 'Texas', zip: '78701' }, observed_at: '2026-10-07T10:00:00Z', order_mode: 'delivery', currency: 'USD',
    items: [{ name: 'Big Mac', price: '6.39', availability: 'available', addons: [{ name: 'Bacon', price: '1.50' }] }, { name: 'Bad', price: '12,99' }] };
  const r = E.validateImport(JSON.stringify({ observations: [good,
    Object.assign({}, good, { store_url: 'https://evil.example/store' }),
    Object.assign({}, good, { currency: 'CAD' }),
    Object.assign({}, good, { observed_at: '2030-01-01T00:00:00Z' }),
    Object.assign({}, good, { address: { city: 'Toronto', state: 'ON', zip: 'M5H', country: 'CA' } }),
    Object.assign({}, good, { store_url: 'javascript:alert(1)' })] }), { now: NOW });
  assert.equal(r.observations.length, 1);
  assert.equal(r.observations[0].address.state, 'TX');
  assert.equal(r.observations[0].items.length, 1, 'invalid item dropped');
  assert.equal(r.observations[0].items[0].price_cents, 639);
  assert.equal(r.observations[0].items[0].addons[0].price_cents, 150);
  assert.equal(r.observations[0].source, 'import');
  assert.ok(r.errors.length >= 6, r.errors.join('\n'));
  assert.equal(E.validateImport('{oops', { now: NOW }).errors.length, 1);
  // ZIP is optional (DoorDash pages publish none) but never malformed.
  const noZip = E.validateImport([Object.assign({}, good, { address: { street: '1 Main', city: 'Austin', state: 'TX', country: 'US' } })], { now: NOW });
  assert.equal(noZip.observations.length, 1);
  assert.equal(noZip.observations[0].address.zip, '');
  assert.match(noZip.warnings.join(' '), /no ZIP code; a ZIP filter cannot match this store/);
  assert.match(E.validateImport([Object.assign({}, good, { address: { city: 'Austin', state: 'TX', zip: '7870' } })], { now: NOW }).errors.join(' '), /5-digit US ZIP/);
  const csv = [E.CSV_COLUMNS.join(','),
    'ubereats,abc123xyz,"McDonald\'s, Main St",https://www.ubereats.com/store/mcd/abc123xyz,1 Main,Columbus,OH,43215,US,2026-10-07T09:00:00Z,delivery,USD,"10 pc ""Chicken"" McNuggets",,5.29,available,Sauce: BBQ=0.00; Bacon=1.50,none,Chicken,,,,,,,,,,',
    'ubereats,abc123xyz,"McDonald\'s, Main St",https://www.ubereats.com/store/mcd/abc123xyz,1 Main,Columbus,OH,43215,US,2026-10-07T09:00:00Z,delivery,USD,Fries,Large,4.19,unknown,,,,DashPass 20% off,3.35,Members only,1.99,2.50,0,0.80,43215,,'].join('\r\n');
  const c = E.validateImport(csv, { now: NOW });
  assert.deepEqual(c.errors, []);
  assert.equal(c.observations.length, 1);
  const ob = c.observations[0];
  assert.equal(ob.items.length, 2);
  assert.equal(ob.items[0].name, '10 pc "Chicken" McNuggets');
  assert.deepEqual(ob.items[0].addons, [{ name: 'BBQ', group: 'Sauce', price_cents: 0 }, { name: 'Bacon', group: null, price_cents: 150 }]);
  assert.equal(ob.items[1].promotions[0].conditions, 'Members only');
  assert.equal(ob.fees.delivery_fee_cents, 199);
  assert.equal(ob.fees.destination_zip, '43215');
  assert.ok(E.validateImport('a,b\n1,2', { now: NOW }).errors[0].includes('missing required column'));
  const conflict = csv.replace(/\r\n$/, '') + '\r\n' + csv.split('\r\n')[2].replace('1.99,2.50', '2.99,2.50');
  assert.ok(E.validateImport(conflict, { now: NOW }).errors.some(e => /conflicts/.test(e)));
  assert.equal(E.safeUrl('https://www.ubereats.com/store/a/b', 'doordash'), null);
  assert.equal(E.safeUrl('https://www.doordash.com/x'), 'https://www.doordash.com/x');
});

test('CSV export escapes and blocks formula injection', () => {
  const out = E.toCSV(['a', 'b'], [['=HYPERLINK("x")', 'x,"y"'], ['@SUM(1)', 'ok']]);
  assert.equal(out, 'a,b\r\n"\'=HYPERLINK(""x"")","x,""y"""\r\n\'@SUM(1),ok\r\n');
  assert.deepEqual(E.parseCSV(out)[1], ['\'=HYPERLINK("x")', 'x,"y"']);
});

test('shipped example import files validate against the documented schema', () => {
  const later = Date.parse('2026-12-31T00:00:00Z');
  const j = E.validateImport(readFileSync(new URL('../examples/import-example.json', import.meta.url), 'utf8'), { now: later });
  assert.deepEqual(j.errors, []);
  assert.equal(j.observations[0].items[0].promotions[0].price_cents, 719);
  assert.equal(j.observations[0].fees.tax_cents, 102);
  const c = E.validateImport(readFileSync(new URL('../examples/import-example.csv', import.meta.url), 'utf8'), { now: later });
  assert.deepEqual(c.errors, []);
  assert.equal(c.observations[0].items.length, 2);
  assert.equal(c.observations[0].order_mode, 'pickup');
  // The fictional example chain never matches a real order.
  assert.equal(E.matchChain("McDonald's", c.observations[0].store_name).status, 'mismatch');
  // Exact match against the example's required option + add-on, with quantity.
  const ev = E.evaluate({ chain: 'Example Schema Grill', items: [{ name: 'Chicken Tenders', count: '6', quantity: '2', modifiers: [{ name: 'Ranch' }, { name: 'Extra Sauce' }] }] },
    j.observations, { now: later, staleHours: 24 * 365 });
  assert.equal(ev.winner.row.lineCents, 2 * (899 + 0 + 75));
  assert.equal(ev.winner.row.checkoutTotalCents, 1948 + 299 + 180 + 0 + 102, 'all destination charges known → checkout total');
});

test('a $0.00 listing flagged by the backend is never a verified price', () => {
  const r = match1({ name: 'Iced Latte' }, item('Iced Latte', null, { price_note: 'published price is $0.00, which on this platform means the price is set by required options', required_options: [{ name: 'price-setting options (not published)', required: true }] }));
  assert.equal(r.status, 'uncertain');
  assert.equal(r.lineCents, null);
  assert.ok(r.reasons.some(x => /Price not verifiable: published price is \$0\.00/.test(x.text)));
});

test('prices you saw yourself: store ID from the link, "item $price" lines, same validation as imports', () => {
  assert.equal(E.storeIdFromUrl('https://www.doordash.com/store/taco-bell-fulton-24633898/?pickup=false', 'doordash'), '24633898');
  assert.equal(E.storeIdFromUrl('https://www.doordash.com/store/24633898/', 'doordash'), '24633898');
  assert.equal(E.storeIdFromUrl('https://www.ubereats.com/store/taco-bell-fulton/AbCdEf123456', 'ubereats'), 'AbCdEf123456');
  assert.equal(E.storeIdFromUrl('https://drd.sh/abc123', 'doordash'), null);                 // short link: not a store page
  assert.equal(E.storeIdFromUrl('https://www.doordash.com/business/taco-bell-1/', 'doordash'), null);
  assert.equal(E.storeIdFromUrl('https://www.ubereats.com/store/x/AbCdEf123456', 'doordash'), null);   // wrong platform
  assert.equal(E.storeIdFromUrl('javascript:alert(1)//www.doordash.com/store/1/', 'doordash'), null);

  const lines = E.parsePriceLines('Crunchwrap Supreme $7.31\n\n10 pc. Chicken McNuggets — $5.19\nChalupa Supreme: US$ 6.70\nno price here\n$4.00');
  assert.deepEqual(lines.items, [{ name: 'Crunchwrap Supreme', price: '7.31' }, { name: '10 pc. Chicken McNuggets', price: '5.19' },
    { name: 'Chalupa Supreme', price: '6.70' }]);
  assert.equal(lines.problems.length, 2);                                                     // no price; no name

  const f = { platform: 'doordash', order_mode: 'pickup', seen_at: '2026-10-07T11:00:00Z', store_url: 'https://www.doordash.com/store/taco-bell-fulton-24633898/',
    store_name: 'Taco Bell', street: '1 Main St', city: 'Fulton', state: 'MS', zip: '38843', lines: 'Crunchwrap Supreme $6.99' };
  const r = E.enteredObservation(f, { now: NOW });
  assert.deepEqual(r.errors, []);
  const o = r.observations[0];
  assert.equal(o.source, 'import');
  assert.equal(o.store_id, '24633898');
  assert.equal(o.order_mode, 'pickup');
  assert.equal(o.observed_at, '2026-10-07T11:00:00.000Z');
  assert.match(o.extraction, /entered by hand from the DoorDash app/);
  assert.match(o.price_context, /entered by you/);
  assert.equal(o.items[0].price_cents, 699);
  // Ranked by the engine like any other record, in its own order mode only.
  const ev = run(order([{ name: 'Crunchwrap Supreme' }], { chain: 'Taco Bell', mode: 'pickup' }), r.observations, { mode: 'pickup' });
  assert.equal(ev.winner.row.lineCents, 699);
  assert.equal(run(order([{ name: 'Crunchwrap Supreme' }], { chain: 'Taco Bell' }), r.observations).winner, null);

  const bad = (over) => E.enteredObservation(Object.assign({}, f, over), { now: NOW }).errors.join(' | ');
  assert.match(bad({ store_url: 'https://drd.sh/x' }), /Store link must be a www\.doordash\.com store page/);
  assert.match(bad({ state: 'ON' }), /50 US states or DC/);
  assert.match(bad({ zip: 'M5V' }), /5-digit US ZIP/);
  assert.match(bad({ lines: '' }), /at least one line/);
  assert.match(bad({ seen_at: '2026-10-08T12:00:00Z' }), /in the future/);
  assert.match(bad({ seen_at: 'yesterday' }), /not a valid date/);
  assert.match(bad({ platform: 'grubhub' }), /DoorDash or Uber Eats/);
  assert.doesNotMatch(bad({ state: 'ON' }), /^record 1/);                                      // messages read as form errors
});

test('the committed real snapshot validates as snapshot observations, scoped per search', () => {
  const snap = JSON.parse(readFileSync(new URL('../live-verification/snapshot.json', import.meta.url), 'utf8'));
  const r = E.snapshotObservations(snap, { now: Date.parse(snap.finished_at || snap.generated_at) + 60000 });
  const total = snap.searches.reduce((n, s) => n + (s.observations || []).length, 0);
  assert.ok(total > 0, 'the committed snapshot has observations');
  assert.deepEqual(r.rejected, []);
  assert.equal(r.obs.length, total);
  for (const o of r.obs) {
    assert.equal(o.source, 'snapshot');
    assert.ok(o.scope.length > 0 && o.search);
    assert.equal(o.address.country, 'US');
  }
  // A store read for McNuggets is not a checked location for a Big Mac.
  const nug = r.obs.find((o) => /McNuggets/.test(o.search));
  if (nug) {
    assert.equal(E.inSnapshotScope(nug, [{ name: '10 pc. Chicken McNuggets' }]), true);
    assert.equal(E.inSnapshotScope(nug, [{ name: 'Big Mac' }]), false);
    // Every name variant must carry a searched product word: an alias alone does
    // not make the store checked for the item's own name, and attribute words
    // ("pcs", "piece") never count.
    assert.equal(E.inSnapshotScope(nug, [{ name: 'Big Mac', aliases: 'McNuggets' }]), false);
    assert.equal(E.inSnapshotScope(nug, [{ name: 'Chicken McNuggets', aliases: 'McNuggets 10 piece' }]), true);
    assert.equal(E.inSnapshotScope(nug, [{ name: '6 pcs Wings' }]), false);
    assert.equal(E.inSnapshotScope(Object.assign({}, nug, { scope: ['pcs', 'chicken', 'mcnuggets'] }), [{ name: '6 pcs Wings' }]), false);
  }
  assert.deepEqual(E.snapshotObservations({ searches: [{ label: 'x', observations: [{ platform: 'ubereats' }] }] }).rejected.length, 1);
});

test('name cleanup: calorie notes are not part of the product name', () => {
  for (const [a, b] of [['Chalupa Supreme', 'Chalupa Supreme (350 cal.)'], ['Big Mac', 'Big Mac [560.0 Cals]'],
    ['Crunchwrap Supreme', 'Crunchwrap Supreme 530 Calories'], ['Fries', 'Fries (230-490 kcal)'], ['Cookie', 'Cookie - 160 Cal']]) {
    assert.equal(match1({ name: a }, item(b, 500)).status, 'exact', b);
  }
  // Words that only start like "cal" stay part of the name.
  assert.equal(E.stripNotes('12 Calzone'), '12 Calzone');
  assert.equal(match1({ name: 'Calamari' }, item('Calamari', 900)).status, 'exact');
  assert.notEqual(match1({ name: 'Calzone' }, item('Calamari', 900)).status, 'exact');
});

test('a bare leading number is the piece count only when it equals the stated count', () => {
  // Archived DoorDash pages: "10 Chicken McNuggets [490-630 Cals]".
  const r = match1({ name: '10 pc. Chicken McNuggets' }, item('10 Chicken McNuggets [490-630 Cals]', 549));
  assert.equal(r.status, 'exact');
  assert.match(r.reasons.map(x => x.text).join(' '), /Piece count matches \(10\); the listing states it as a bare number/);
  assert.equal(match1({ name: '10 Chicken McNuggets' }, item('10 pc. Chicken McNuggets®', 549)).status, 'exact');
  assert.equal(match1({ name: '10 pc. Chicken McNuggets' }, item('20 Chicken McNuggets', 949)).status, 'rejected');
  assert.equal(match1({ name: '10 Chicken McNuggets' }, item('20 pc. Chicken McNuggets', 949)).status, 'rejected');
  assert.equal(match1({ name: 'Burrito' }, item('7 Layer Burrito', 449)).status, 'rejected');
  assert.equal(match1({ name: '7 Layer Burrito' }, item('7 Layer Burrito', 449)).status, 'exact');
});

test('name differences: variant words reject, other small differences only need confirmation', () => {
  const st = (o, c) => match1(typeof o === 'string' ? { name: o } : o, item(c, 700));
  // A word or number that makes another item: never the same.
  for (const [o, c, w] of [['Crunchwrap Supreme', 'Crunchwrap Supreme Combo', 'combo'], ['10 pc. Chicken McNuggets', '10 pc. Chicken McNuggets® Meal', 'meal'],
    ['Chalupa', 'Chalupa Supreme', 'supreme'], ['McChicken', 'Spicy McChicken', 'spicy'], ['Big Mac', 'Double Big Mac', 'double'],
    ['Wings', 'Boneless Wings', 'boneless'], ['Burrito', 'Burrito Bowl', 'bowl'], ['Small - Deep Dish Margherita', 'Small Deep Dish Margherita - GF', 'gf'],
    ['Taco', '2 Tacos', '2']]) {
    const r = st(o, c);
    assert.equal(r.status, 'rejected', c);
    if (/\d/.test(w)) continue;
    assert.match(r.reasons.map(x => x.text).join(' '), new RegExp('"' + w + '" makes it a different item'), c);
  }
  // One or two other words: needs confirmation, never verified on its own.
  for (const [o, c, w] of [['Small - Deep Dish Margherita', 'Small Deep Dish Margherita Pizza', 'pizza'], ['Large Fries', 'World Famous Fries® - Large', 'world famous'],
    ['Burrito Bowl', 'Chicken Burrito Bowl', 'chicken'], ['Small Deep Dish Margherita Pizza', 'Small - Deep Dish Margherita', 'pizza']]) {
    const r = st(o, c);
    assert.equal(r.status, 'uncertain', c);
    assert.equal(r.lineCents, 700);
    assert.match(r.reasons.map(x => x.text).join(' '), new RegExp('Name differs only by "' + w + '"'), c);
  }
  // Still rejected: different words on both sides, more than two extra words,
  // a soft name difference plus a real attribute conflict, another flavor.
  assert.equal(st('Cheese Pizza', 'Pepperoni Pizza').status, 'rejected');
  assert.equal(st('Fries', 'Chili Cheese Bacon Fries').status, 'rejected');
  assert.equal(st('Small - Deep Dish Margherita', 'Large Deep Dish Margherita Pizza').status, 'rejected');
  assert.equal(st({ name: 'Wings', flavor: 'Buffalo', count: '10' }, '10 pc Lemon Pepper Wings').status, 'rejected');
  // In a ranking, a soft difference is never the verified winner.
  const ev = run(order([{ name: 'Large Fries' }]), [obs({ items: [item('World Famous Fries® - Large', 449)] })]);
  assert.equal(ev.winner, null);
  assert.equal(ev.rows.uncertain.length, 1);
});

test('closest listings: suggestions only, confirmed once as a same-item name', () => {
  const stores = [obs({ items: [item('World Famous Fries® - Large', 449), item('World Famous Fries® - Medium', 389), item('Fries Meal', 899), item('Large Hash Browns', 299)] }),
                  obs({ items: [item('World Famous Fries® - Large', 479)] })];
  const o = order([{ name: 'Large Fries' }]);
  let ev = run(o, stores);
  const ls = ev.closest[0].listings;
  assert.deepEqual(ls.map(l => [l.label, l.stores, l.minCents, l.maxCents]), [['World Famous Fries® - Large', 2, 449, 479]]);
  assert.match(ls[0].why, /name differs only by "world famous"/);
  assert.ok(ls[0].score > 0 && ls[0].score <= 1);
  assert.equal(ev.winner, null);
  // Medium (size conflict), Meal (variant word) and Hash Browns (no shared product word) are not suggested.
  o.items[0].aliases = 'World Famous Fries® - Large';
  ev = run(o, stores);
  assert.equal(ev.winner.row.lineCents, 449);
  assert.match(ev.winner.row.reasons.map(x => x.text).join(' '), /via your confirmed alias "World Famous Fries® - Large"/);
  assert.match(ev.winner.row.reasons.map(x => x.text).join(' '), /Size matches \(large\)/);
  assert.deepEqual(ev.closest[0].listings, []);                  // an exact listing exists now
  // Suggestions are per order item, and only for items without an exact listing.
  const two = run(order([{ name: 'Large Fries' }, { name: 'Fries Meal' }]), stores);
  assert.equal(two.closest[0].listings.length, 1);
  assert.deepEqual(two.closest[1].listings, []);
});

test('review fixes: ZIP-less duplicates by city/state, canonical typed links, warning text', () => {
  const noZip = (id, city) => obs({ store_id: id, address: { street: '100 Main St', city, state: 'OR', zip: '', country: 'US' }, items: [item('Burrito', 500)] });
  // Same street in two cities: two branches. Same street, same city: one branch listed twice.
  assert.equal(E.dedupeObservations([noZip('a', 'Beaverton'), noZip('b', 'Aloha')]).duplicates.length, 0);
  const d = E.dedupeObservations([noZip('a', 'Beaverton'), noZip('b', 'Beaverton')]);
  assert.equal(Object.keys(d.addressDuplicateOf).length, 1);
  const f = { platform: 'doordash', order_mode: 'delivery', seen_at: '2026-10-07T11:00:00Z', store_name: 'Taco Bell', city: 'Fulton', state: 'MS', zip: '',
    store_url: 'https://www.doordash.com/store/taco-bell-fulton-24633898/?lat=34.27&lng=-88.41&utm_source=share#menu', lines: 'Crunchwrap Supreme $6.99' };
  const r = E.enteredObservation(f, { now: NOW });
  assert.equal(r.observations[0].store_url, 'https://www.doordash.com/store/taco-bell-fulton-24633898/');
  assert.deepEqual(r.warnings, ['no ZIP code; a ZIP filter cannot match this store']);
});

test('typed entries for one store and mode are merged; each item keeps its own time', () => {
  const f = (lines, at) => E.enteredObservation({ platform: 'doordash', order_mode: 'delivery', seen_at: at, store_name: 'Taco Bell', city: 'Fulton', state: 'MS', zip: '38843',
    store_url: 'https://www.doordash.com/store/taco-bell-fulton-24633898/', lines }, { now: NOW }).observations[0];
  let list = E.mergeEntered([], f('Crunchwrap Supreme $6.99\n10 pc Nuggets $4.00', '2026-10-05T09:00:00Z')).list;
  // "Crunchwrap Supreme®" and "10 Piece Nuggets" are the same items to the matcher, so they replace the old prices.
  const mg = E.mergeEntered(list, f('Chalupa Supreme $5.49\nCrunchwrap Supreme® $7.09\n10 Piece Nuggets $4.20', '2026-10-07T11:00:00Z'));
  assert.equal(mg.merged, true);
  list = mg.list;
  assert.equal(list.length, 1);
  assert.deepEqual(list[0].items.map(i => [i.name, i.price_cents, i.observed_at]), [
    ['Chalupa Supreme', 549, '2026-10-07T11:00:00.000Z'], ['Crunchwrap Supreme®', 709, '2026-10-07T11:00:00.000Z'], ['10 Piece Nuggets', 420, '2026-10-07T11:00:00.000Z']]);
  assert.equal(list[0].observed_at, '2026-10-07T11:00:00.000Z');             // the newest entry
  // An item kept from an older entry keeps its older time and can go stale on its own.
  const older = E.mergeEntered([f('Taco $1.99', '2026-10-05T09:00:00Z')], f('Chalupa Supreme $5.49', '2026-10-07T11:00:00Z')).list;
  assert.deepEqual(older[0].items.map(i => [i.name, i.observed_at]), [['Taco', '2026-10-05T09:00:00.000Z'], ['Chalupa Supreme', '2026-10-07T11:00:00.000Z']]);
  const taco = run(order([{ name: 'Taco' }], { chain: 'Taco Bell' }), older, { staleHours: 24 });
  assert.equal(taco.winner, null);
  assert.equal(taco.rows.verified[0].stale, true);
  assert.equal(taco.rows.verified[0].observedAt, '2026-10-05T09:00:00.000Z');
  const chalupa = run(order([{ name: 'Chalupa Supreme' }], { chain: 'Taco Bell' }), older, { staleHours: 24 });
  assert.equal(chalupa.winner.row.lineCents, 549);                              // the fresh item is not aged by the old one
  // A basket is as old as its oldest part.
  const basket = run(order([{ name: 'Taco' }, { name: 'Chalupa Supreme' }], { chain: 'Taco Bell' }), older, { staleHours: 24 });
  assert.equal(basket.baskets[0].stale, true);
  assert.equal(basket.winner, null);
  // Another mode, or a plain import, is never merged.
  assert.equal(E.mergeEntered(list, Object.assign(f('Taco $2.00', '2026-10-07T10:00:00Z'), { order_mode: 'pickup' })).merged, false);
  const imported = Object.assign({}, list[0], { extraction: 'imported record' });
  assert.equal(E.mergeEntered([imported], f('Taco $2.00', '2026-10-07T10:00:00Z')).merged, false);
  // Item times are validated like record times.
  const bad = E.validateImport([Object.assign({}, list[0], { items: [{ name: 'Taco', price: '1.00', observed_at: '2030-01-01T00:00:00Z' }] })], { now: NOW });
  assert.match(bad.errors.join(' '), /observed_at is not a valid past time/);
});

test('review fixes: hand-entry lines, non-US links, US evidence, malformed snapshots, excluded imports', () => {
  const lines = E.parsePriceLines('Nachos $12,99\nDeep Dish Cheese $13.59+\nCrunchwrap Supreme $5.00');
  assert.deepEqual(lines.items, [{ name: 'Crunchwrap Supreme', price: '5.00' }]);
  assert.match(lines.problems[0], /"\$12,99" is not a valid US price/);
  assert.match(lines.problems[1], /starting price/);
  const f = { platform: 'ubereats', order_mode: 'delivery', seen_at: '2026-10-07T11:00:00Z', store_name: 'Hungry Jacks', city: 'Perth', state: 'WA', zip: '',
    store_url: 'https://www.ubereats.com/au/store/hj-perth/AbCdEf123456', lines: 'Whopper $12.50' };
  assert.match(E.enteredObservation(f, { now: NOW }).errors.join(' '), /non-US section/);
  assert.equal(E.enteredObservation(Object.assign({}, f, { lines: 'Nachos $12,99\nWhopper $12.50' }), { now: NOW }).observations.length, 0);
  const imp = (over) => E.validateImport([Object.assign({ platform: 'ubereats', store_id: 'x', store_name: 'X', store_url: 'https://www.ubereats.com/store/x/AbCdEf123456',
    address: { city: 'Perth', state: 'WA' }, observed_at: '2026-10-07T10:00:00Z', order_mode: 'delivery', items: [{ name: 'A', price: '1.00' }] }, over)], { now: NOW });
  assert.match(imp({}).errors.join(' '), /needs country "US" or a 5-digit ZIP/);
  assert.equal(imp({ address: { city: 'Seattle', state: 'WA', country: 'US' } }).observations.length, 1);
  assert.equal(imp({ address: { city: 'Seattle', state: 'WA', zip: '98101' } }).observations.length, 1);
  assert.match(imp({ store_url: 'https://www.doordash.com/en-CA/store/x-1/', platform: 'doordash', address: { city: 'Seattle', state: 'WA', zip: '98101' } }).errors.join(' '), /non-US section/);
  assert.equal(imp({ store_url: 'https://www.doordash.com/en-US/store/x-1/', platform: 'doordash', address: { city: 'Seattle', state: 'WA', zip: '98101' } }).observations.length, 1);
  // Malformed snapshots are reported, never thrown.
  const bad = { toString: 1 };
  for (const s of [null, {}, { searches: [null] }, { searches: [{ observations: {} }] }, { searches: [{ observations: [null] }] },
    { searches: [{ label: bad, request: { order: { items: [null, { name: 'x', aliases: 'a, b' }] } }, observations: [] }], github_run_id: bad }]) {
    const r = E.snapshotObservations(s);
    assert.ok(Array.isArray(r.obs) && Array.isArray(r.rejected));
  }
  assert.match(E.snapshotObservations({ searches: [{ label: 'Tacos', observations: [{ store_name: 'Taco Bell', platform: 'ubereats' }] }] }).rejected[0], /^Tacos, Taco Bell: store_id is required/);
  // An excluded import never hides another reading of the same store.
  const live = obs({ source: 'snapshot', store_id: 'S9', items: [item('Burrito', 599)] });
  const typed = Object.assign({}, live, { source: 'import', observed_at: T(0.5), items: [item('Burrito', 649)] });
  assert.equal(run(order([{ name: 'Burrito' }]), [live, typed], { includeImported: false }).winner.row.lineCents, 599);
  assert.equal(run(order([{ name: 'Burrito' }]), [live, typed], { includeImported: false }).coverage.excluded.imported, 1);
});

test('red-team cases: never a false exact, and confirmed aliases work', () => {
  const st = (o, c, extra = {}) => match1(typeof o === 'string' ? { name: o } : o, item(c, 900, extra)).status;
  // Repeated words and bundles.
  assert.equal(st('Double-Double', 'Double'), 'rejected');
  assert.equal(st('Double', 'Double-Double'), 'rejected');
  assert.equal(st({ name: 'Large 2-Topping Pizzas', quantity: '2' }, '2 Large 2-Topping Pizzas'), 'rejected');
  // Menu numbers are not counts, and not product words.
  assert.equal(st({ name: 'Chicken McNuggets', count: '6' }, '#6 Chicken McNuggets'), 'uncertain');
  assert.equal(st('Big Mac Meal', '#1 Big Mac® Meal'), 'exact');
  // Size-field words must be in the listing; a size the order did not state is not assumed.
  assert.equal(st({ name: 'Deep Dish Cheese', size: 'Small Gluten Free' }, 'Small - Deep Dish Cheese'), 'uncertain');
  assert.equal(st({ name: 'Pepperoni', size: 'Deep Dish' }, 'Personal - Deep Dish Pepperoni'), 'uncertain');
  assert.equal(st({ name: '10 pc. Chicken McNuggets', size: 'Large Meal' }, '10 pc. Chicken McNuggets® Meal'), 'uncertain');
  assert.equal(st({ name: 'Deep Dish Cheese', size: 'Small Deep Dish' }, 'Small - Deep Dish Cheese'), 'exact');
  // A count typed in the size field is a piece count.
  assert.equal(st({ name: 'Chick-fil-A Nuggets', size: '12 ct' }, 'Chick-fil-A® Nuggets'), 'uncertain');
  assert.equal(st({ name: 'Chick-fil-A Nuggets', size: '12 ct' }, 'Chick-fil-A® Nuggets 12 ct'), 'exact');
  // Normalization: ™ before NFKD, w/o, gluten free, Jr., inch marks, X-Lg, serving notes, trailing counts.
  assert.equal(st({ name: 'Chick-n-Minis', count: '4' }, 'Chick-n-Minis™', { variant: '4 ct' }), 'exact');
  assert.equal(st('The Malnati Chicago Classic Deep Dish - GF', 'The Malnati Chicago Classic™ Deep Dish - GF'), 'exact');
  assert.equal(st('Quarter Pounder w/ Cheese', 'Quarter Pounder w/o Cheese'), 'rejected');
  assert.equal(st('Cheese Thin Crust - GF', 'Gluten Free Thin Crust Cheese'), 'exact');
  assert.equal(st('Junior Bacon Cheeseburger', 'Jr. Bacon Cheeseburger'), 'exact');
  assert.equal(st("Large 14'' Pepperoni Pizza", 'Large 14" Pepperoni Pizza'), 'exact');
  assert.equal(st('Large 16 in. Cheese Pizza', 'Large 16" Cheese Pizza'), 'exact');
  assert.equal(st('X-Lg Cheese Pizza', 'XLarge Cheese Pizza'), 'exact');
  assert.equal(st('Large Cheese Pizza', 'X-Lg Cheese Pizza'), 'rejected');
  assert.equal(st('40 pc. Chicken McNuggets', '40-piece Chicken McNuggets (Serves 4) [1860-2210 Cals]'), 'exact');
  assert.equal(st({ name: 'Fried Chicken Wings', count: '7' }, 'Fried Chicken Wings (7)'), 'exact');
  assert.equal(st({ name: '10 Chicken McNuggets', count: '10' }, '10 Chicken McNuggets [490-630 Cals]'), 'exact');
  assert.equal(st('Large Pepperoni Pizza', 'Large Pepperoni Pizza (290 Cal/Slice)'), 'exact');
  assert.equal(st('Cheeseburger', 'Cheese Burger'), 'exact');
  // Variant words added by the red team.
  for (const [o, c] of [['Large Cheese Pizza', 'Large Three Cheese Pizza'], ['Small - Deep Dish Pepperoni', 'Small Frozen Deep Dish Pepperoni'],
    ['Margherita', 'Thin Crust Margherita'], ['McChicken', 'McChicken® Biscuit'], ['Crispy Chicken Crunchwrap', 'Crispy Chicken Crunchwrap Slider'],
    ['Crunchwrap Supreme', 'Black Bean Crunchwrap Supreme']]) assert.equal(st(o, c), 'rejected', c);
  assert.equal(st({ name: 'Italian B.M.T.', size: '6 inch' }, 'Italian B.M.T.® Footlong'), 'rejected');
  // An option in several groups at different charges: the order's size decides, or it is not proven.
  const A = [{ name: 'Pepperoni', price_cents: 150, group: 'Small Pizza Toppings' }, { name: 'Pepperoni', price_cents: 250, group: 'Large Pizza Toppings' }];
  const big = match1({ name: 'Create Your Own Pizza', size: 'Large', modifiers: [{ name: 'Pepperoni', price: '2.50' }] }, item('Create Your Own Pizza', 1699, { variant: 'Large', addons: A }));
  assert.equal(big.status, 'exact'); assert.equal(big.unitCents, 1699 + 250);
  const H = [{ name: 'Pepperoni', price_cents: 100, group: 'Left Half Toppings' }, { name: 'Pepperoni', price_cents: 200, group: 'Whole Pizza Toppings' }];
  assert.equal(match1({ name: 'Large Create Your Own Pizza', modifiers: [{ name: 'Pepperoni' }] }, item('Large Create Your Own Pizza', 1499, { addons: H })).unitCents, 1699);
  const amb = match1({ name: 'Create Your Own Pizza', modifiers: [{ name: 'Pepperoni' }] }, item('Create Your Own Pizza', 999, { addons: A }));
  assert.equal(amb.status, 'uncertain');
  assert.match(amb.reasons.map(r => r.text).join(' '), /listed in 2 option groups with different charges/);
  // Confirming a suggested listing (as the UI stores it) makes it match.
  for (const [name, listing, extra] of [['10 Piece McNuggets', '10 Chicken McNuggets [490-630 Cals]', {}], ['Large Pepperoni', 'Large Pepperoni Pizza (2,320 Cal)', {}],
    ['Deep Dish Cheese', 'Small - Deep Dish Cheese', { size: 'Small Deep Dish' }]]) {
    const alias = E.stripNotes(listing).replace(/[,;\n]+/g, ' ').replace(/\s+/g, ' ').trim();
    assert.equal(match1(Object.assign({ name, aliases: alias }, extra), item(listing, 519)).status, 'exact', listing);
  }
  // Closest listings never suggest a different flavor or topping.
  const ev = run(order([{ name: 'Large - Thin Crust Pepperoni' }], { chain: "Lou Malnati's" }),
    [obs({ store_name: "Lou Malnati's", items: [item('Large - Thin Crust Margherita', 2826), item('Large - Thin Crust Pepperonis', 2899)] })]);
  assert.deepEqual(ev.closest[0].listings.map(l => l.label), ['Large - Thin Crust Pepperonis']);
});

test('red-team round 2: part sizes, menu numbers, servings, scripts, aliases, suggestions', () => {
  const st = (o, c, extra = {}) => match1(typeof o === 'string' ? { name: o } : o, item(c, 900, extra)).status;
  const chainSt = (chain, o, c, extra = {}) => {
    const ev = run(order([typeof o === 'string' ? { name: o } : o], { chain }), [obs({ store_name: chain, items: [item(c, 900, extra)] })]);
    return ev.rows.verified.length ? 'exact' : ev.rows.uncertain.length ? 'uncertain' : 'rejected';
  };
  // Which part a size belongs to.
  assert.equal(st('Large Chips & Guacamole', 'Chips & Large Guacamole'), 'rejected');
  assert.equal(st('Chips & Large Queso Blanco', 'Large Chips & Queso Blanco'), 'rejected');
  assert.equal(st('Chips & Large Guacamole', 'Large Chips & Large Guacamole'), 'rejected');
  assert.equal(st('Small Fries & Large Drink', 'Large Fries & Small Drink'), 'rejected');
  assert.equal(st('Large Chips & Guacamole', 'Large Chips & Large Guacamole'), 'uncertain');
  assert.equal(st('Small Fries & Large Drink', 'Small Fries & Large Drink'), 'exact');
  assert.equal(st('Large Mac & Cheese', 'Mac & Cheese', { variant: 'Large' }), 'exact');
  // Menu numbers are compared when both sides have one.
  assert.equal(st('#3 Combo', '#9 Combo'), 'rejected');
  assert.equal(st('No. 1 Special', '#2 Special'), 'rejected');
  assert.equal(st('#1 - Big Mac Meal', '#1-Big Mac Meal'), 'exact');
  assert.equal(st('#3 Combo', 'Combo #3'), 'exact');
  assert.equal(st('Big Mac Meal', '# 1 Big Mac Meal'), 'exact');
  // Serving sizes are compared.
  assert.equal(st('Bagel Pack (Serves 13)', 'Bagel Pack (Serves 6)'), 'rejected');
  assert.equal(st('Party Pack (Serves 4-6)', 'Party Pack (Serves 10-12)'), 'rejected');
  assert.equal(st('Family Meal (Serves 4)', 'Family Meal - Serves 4'), 'exact');
  assert.equal(st('Family Meal (Serves 4)', 'Family Meal (Feeds 4)'), 'exact');
  assert.equal(st('Party Tray (Serves 10-12)', 'Party Tray (serves 10 to 12 people)'), 'exact');
  assert.equal(st('Burrito Bar', 'Burrito Bar (Serves 20)'), 'uncertain');
  // Calorie notes, fractions, decimals, liters, letter sizes, baker's dozen.
  assert.equal(st({ name: 'Chicken Tenders', count: '3' }, 'Chicken Tenders 5 - 600 Cal'), 'rejected');
  assert.equal(st({ name: 'Chicken Tenders', count: '3' }, 'Chicken Tenders 3 - 360 Cal'), 'exact');
  for (const c of ['Big Mac (250 Cal Each)', 'Big Mac (250 Cals/ea)', 'Big Mac (540 & 590 Cal)']) assert.equal(st('Big Mac', c), 'exact', c);
  assert.equal(st('Cookie', 'Cookie (170 Cal per Cookie)'), 'exact');
  assert.equal(st('1/2 Chicken', '12 Chicken'), 'rejected');
  assert.equal(st('1/2 Chicken', 'Half Chicken'), 'exact');
  assert.equal(st('1/4 Pounder', 'Quarter Pounder'), 'exact');
  assert.equal(st({ name: 'Donuts', count: '6' }, '1/2 Dozen Donuts'), 'exact');
  assert.equal(st('½ Dozen Oysters', 'Half Dozen Oysters'), 'exact');
  assert.equal(st('Sauce .5 oz', 'Sauce 5 oz'), 'rejected');
  assert.equal(st('Sauce .5 oz', 'Sauce 0.5 oz'), 'exact');
  assert.equal(st('Coke 2 Liter', 'Coke 2L'), 'exact');
  assert.equal(st('Coke 2 Liter', 'Coke 20 oz'), 'rejected');           // volumes are compared across units
  assert.equal(st('Md Fries', 'Medium Fries'), 'exact');
  assert.equal(st('Fries (M)', 'Medium Fries'), 'exact');
  assert.equal(st('Md Fries', 'Large Fries'), 'rejected');
  assert.equal(st({ name: 'Bagels', count: '13' }, "Baker's Dozen Bagels"), 'exact');
  assert.equal(st('Dozen Bagels', "Baker's Dozen Bagels"), 'rejected');
  assert.equal(st('Large 14" Pepperoni Pizza', 'Large 14″ Pepperoni Pizza'), 'exact');
  // Names in other scripts, and emoji, are compared too.
  assert.equal(st('麻婆豆腐', '宫保鸡丁'), 'rejected');
  assert.equal(st('麻婆豆腐', '麻婆豆腐'), 'exact');
  assert.equal(st('Dim Sum 虾饺', 'Dim Sum 烧卖'), 'rejected');
  assert.equal(st('Шаурма', 'Плов'), 'rejected');
  assert.notEqual(st('Chicken Sandwich', 'Chicken Sandwich 🌶️'), 'exact');
  assert.equal(st('Dumplings 🌱', 'Dumplings 🐷'), 'rejected');
  // Variant words from this round.
  for (const [o, c] of [['Caffè Latte', 'Iced Caffè Latte'], ['Plate', 'Bigger Plate'], ['Orange Chicken', 'Orange Chicken Plate'], ['Hamburger', 'Little Hamburger'],
    ['Mocha', 'Mocha Frappe'], ['Baja Blast', 'Baja Blast Freeze'], ['Baconator', 'Baconator Fries'], ['Honey', 'Honey Mustard Sauce'], ['Cheeseburger', 'Bacon Cheeseburger'],
    ['Grilled Stuft Burrito', 'XXL Grilled Stuft Burrito'], ['Burger with Bacon', 'Burger w/out Bacon'], ['Broccoli Cheddar Soup', 'Broccoli Cheddar Soup Cup'],
    ['Freshly-Brewed Iced Tea Sweetened', 'Freshly-Brewed Iced Tea Unsweetened']]) assert.equal(st(o, c), 'rejected', c);
  // Numbers as words, connectors, brand prefixes, flavor as an option.
  assert.equal(st('You Pick Two', 'You Pick 2'), 'exact');
  assert.equal(st('Chicken Caesar Salad', 'Caesar Salad with Chicken'), 'uncertain');
  assert.equal(st('Bacon Gouda Egg Sandwich', 'Bacon, Gouda & Egg Sandwich'), 'uncertain');
  assert.equal(chainSt('Chick-fil-A', { name: 'Nuggets', count: '8' }, 'Chick-fil-A® Nuggets', { variant: '8 ct' }), 'exact');
  assert.equal(chainSt('Five Guys', 'Regular Fries', 'Regular Five Guys Style Fries'), 'uncertain');
  assert.equal(st({ name: 'Wings', flavor: 'Buffalo' }, 'Traditional Wings', { addons: [{ name: 'Buffalo', group: 'Sauce', price_cents: 0 }] }), 'uncertain');
  assert.equal(st({ name: 'Wings', flavor: 'Buffalo' }, 'Lemon Pepper Wings'), 'rejected');
  // A confirmed alias carries its count and size, and prices a dropped word as an option.
  assert.equal(st({ name: '10 Wings', aliases: '10 pc Classic Wings' }, 'Classic Wings'), 'uncertain');
  assert.equal(st({ name: '10 Wings', aliases: '10 pc Classic Wings' }, '10 pc Classic Wings'), 'exact');
  assert.equal(st({ name: 'Fries', aliases: 'World Famous Fries® - Small' }, 'World Famous Fries® - Small'), 'exact');
  assert.equal(st({ name: 'Fries', aliases: 'World Famous Fries® - Small' }, 'World Famous Fries® - Large'), 'rejected');
  const bowl = match1({ name: 'Steak Burrito Bowl', aliases: 'Burrito Bowl' }, item('Burrito Bowl', 1070, { addons: [{ name: 'Chicken', group: 'Protein', price_cents: 0 }, { name: 'Steak', group: 'Protein', price_cents: 200 }] }));
  assert.equal(bowl.status, 'exact'); assert.equal(bowl.unitCents, 1270);
  // Without such an option, your confirmation decides; the row notes the word.
  const plain = match1({ name: 'Steak Burrito Bowl', aliases: 'Burrito Bowl' }, item('Burrito Bowl', 1070));
  assert.equal(plain.status, 'exact');
  assert.match(plain.reasons.map(r => r.text).join(' '), /Your item name also says "steak"; you confirmed this listing as the same item/);
  assert.equal(st({ name: 'Margherita Pizza', size: 'Small Deep Dish', aliases: 'Small - Deep Dish Margherita' }, 'Small - Deep Dish Margherita'), 'exact');
  assert.equal(st({ name: 'Broccoli Cheddar Soup', size: 'Bread Bowl', aliases: 'Broccoli Cheddar Soup in a Bread Bowl' }, 'Broccoli Cheddar Soup in a Bread Bowl'), 'exact');
  // Suggestions: never another word, another size or count, a stale or duplicate listing.
  const sug = (o, items, more = []) => run(order([typeof o === 'string' ? { name: o } : o]), [obs({ items })].concat(more)).closest[0].listings.map(l => l.label);
  assert.deepEqual(sug('Blueberry Muffin', [item('Blackberry Muffin', 300)]), []);
  assert.deepEqual(sug('Onion Rings', [item('Onion Wings', 300)]), []);
  assert.deepEqual(sug('Small Deep Dish Buffalo Chicken', [item('Large - Deep Dish BuffaLou Chicken', 4470), item('Small - Deep Dish Buffalo Chickens', 2100)]), ['Small - Deep Dish Buffalo Chickens']);
  assert.deepEqual(sug('Chalupa Supreme', [item('Chalupa Supreme', 549, { availability: 'unknown' })]), []);
  const staleStore = obs({ observed_at: T(24 * 30), items: [item('Large Fries', 449)] });
  assert.deepEqual(sug('Large Fries', [item('World Famous Fries® - Large', 459)], [staleStore]), ['World Famous Fries® - Large']);
});

test('red-team round 3: calorie brackets, parts, aliases, option pricing, suggestions, volumes, scripts', () => {
  const st = (o, c, extra = {}) => match1(typeof o === 'string' ? { name: o } : o, item(c, 900, extra)).status;
  // A calorie figure removes only its own part of a bracket; the rest is compared.
  for (const [o, c] of [["Dave's Single", "Dave's Single (Double, 850 Cal)"], ['Broccoli Cheddar Soup', 'Broccoli Cheddar Soup (Bowl, 360 Cal)'],
    ['Cheeseburger', 'Cheeseburger (No Cheese, 250 Cal)'], ['Caffe Latte', 'Caffè Latte (Iced, Grande, 190 Cal)'],
    ['Wings (6 pc, 540 Cal)', 'Wings (10 pc, 900 Cal)'], ['Family Meal (Serves 4)', 'Family Meal (Serves 8, 4800-7200 Cal)'],
    ['Latte (12 oz, 150 Cal)', 'Latte (16 oz, 190 Cal)'], ['#3 Combo', 'Combo (#5, 900 Cal)']]) assert.equal(st(o, c), 'rejected', c);
  for (const [o, c] of [['Fries', 'Fries (Large, 490 Cal)'], ['Fries', 'Fries [Large | 490 Cal]'], ['Classic Wings', 'Classic Wings (10 pc, 900-1,100 Cal)'],
    ['Family Meal', 'Family Meal (Serves 8, 4800-7200 Cal)'], ['Coca-Cola', 'Coca-Cola (2 Liter, 800 Cal)'], ['Cheese Pizza', 'Cheese Pizza (14", 2,240 Cal)']]) assert.equal(st(o, c), 'uncertain', c);
  assert.equal(st('Spicy Wings', 'Wings (Spicy, 900 Cal)'), 'exact');
  assert.equal(st('#5 Combo', 'Combo (#5, 900 Cal)'), 'exact');
  assert.equal(st({ name: 'Wings', count: '10' }, 'Wings (10 pc, 900 Cal)'), 'exact');
  assert.match(E.stripNotes("Dave's Single (Double, 850 Cal)"), /\(Double\)/);
  assert.doesNotMatch(E.stripNotes('Big Mac (250 Cals/ea) [490-630 Cals]'), /\(|\[|cal/i);
  // A name confirmed from such a listing keeps the bracket's words.
  assert.equal(st({ name: 'Chicken Nuggets', aliases: 'Nuggets (Grilled 8 ct)' }, 'Nuggets (12 ct, 380 Cal)'), 'rejected');
  // Words and numbers stay with their part of a multi-part name.
  for (const [o, c] of [['3 Tacos & 2 Enchiladas', '2 Tacos & 3 Enchiladas'], ['10 Wings & 5 Tenders', '5 Wings & 10 Tenders'],
    ['Sweet & Sour Chicken with Pork Fried Rice', 'Sweet & Sour Pork with Chicken Fried Rice'], ['Chicken Lo Mein & Pork Egg Roll', 'Pork Lo Mein & Chicken Egg Roll'],
    ['Pepperoni Pizza with Extra Cheese', 'Cheese Pizza with Extra Pepperoni'], ['Enchilada Dinner (2 Cheese & 1 Beef)', 'Enchilada Dinner (1 Cheese & 2 Beef)']]) assert.equal(st(o, c), 'rejected', c);
  assert.equal(st('2 Large 3-Topping Pizzas', '3 Large 2-Topping Pizzas'), 'uncertain');
  assert.equal(st('Wings & Things (5 Wings, 3 Tenders)', 'Wings & Things (3 Wings, 5 Tenders)'), 'uncertain');
  assert.equal(st('Chips & Guacamole', 'Guacamole & Chips'), 'exact');
  assert.equal(st('Sweet & Sour Chicken', 'Sweet and Sour Chicken'), 'exact');
  assert.equal(st('Combo 2 Tacos 1 Burrito', '1 Burrito 2 Tacos Combo'), 'exact');
  // A comma, "'N", "plus" or "w" also starts a part that a size can belong to.
  for (const [o, c] of [['Chips, Large Guacamole', 'Large Chips, Guacamole'], ["Chips 'N Large Queso", "Large Chips 'N Queso"],
    ['Chips w Large Salsa', 'Large Chips w Salsa'], ['Sandwich plus Large Drink', 'Large Sandwich plus Drink']]) assert.equal(st(o, c), 'rejected', c);
  assert.equal(st('Fries, Large', 'Large Fries'), 'exact');
  // A size field that names a part sizes that part.
  assert.equal(st({ name: 'Chips & Guacamole', size: 'Large Guacamole' }, 'Large Chips & Guacamole'), 'rejected');
  assert.equal(st({ name: 'Chips & Guacamole', size: 'Large Guacamole' }, 'Chips & Large Guacamole'), 'exact');
  // A confirmed alias brings its menu number, count, size and serving size, also when your own name matched.
  assert.equal(st({ name: 'Lunch Special', aliases: 'Lunch Combo #3' }, 'Lunch Combo #9'), 'rejected');
  assert.equal(st({ name: 'Lunch Special', aliases: 'Lunch Combo #3' }, '#3 Lunch Combo'), 'exact');
  assert.equal(st({ name: 'Spicy Chicken Combo', aliases: '#3 Spicy Chicken Sandwich Combo' }, '#5 Spicy Chicken Sandwich Combo'), 'rejected');
  assert.equal(st({ name: 'Classic Wings', aliases: '10 pc Classic Wings' }, 'Classic Wings'), 'uncertain');
  assert.equal(st({ name: 'Classic Wings', aliases: '10 pc Classic Wings' }, 'Classic Wings', { variant: '20 pc' }), 'rejected');
  assert.equal(st({ name: 'Classic Wings', aliases: '10 pc Classic Wings' }, '10 pc Classic Wings'), 'exact');
  assert.equal(st({ name: 'Party Tray', aliases: 'Party Tray (Serves 10)' }, 'Party Tray (Serves 20)'), 'rejected');
  assert.equal(st({ name: 'Italian BMT', aliases: 'Italian B.M.T. 6 inch' }, 'Italian B.M.T. Footlong'), 'rejected');
  assert.equal(st({ name: 'Chips & Guac', aliases: 'Large Chips & Guacamole' }, 'Chips & Large Guacamole'), 'rejected');
  assert.equal(st({ name: 'McNuggets', aliases: '10 pc. Chicken McNuggets®' }, '10 Chicken McNuggets [490-630 Cals]'), 'exact');
  assert.equal(st({ name: 'Chicken McNuggets', aliases: '10 Chicken McNuggets' }, '10 pc. Chicken McNuggets'), 'exact');
  // Each word your name adds over the confirmed name is priced as its own option, and no option is charged twice.
  const pizza = match1({ name: 'Pepperoni & Mushroom Pizza', aliases: 'Create Your Own Pizza' },
    item('Create Your Own Pizza', 1299, { addons: [{ name: 'Pepperoni', group: 'Toppings', price_cents: 200 }, { name: 'Mushroom', group: 'Toppings', price_cents: 200 }] }));
  assert.equal(pizza.status, 'exact'); assert.equal(pizza.unitCents, 1699);
  const wings = match1({ name: 'Buffalo Wings', flavor: 'Buffalo', aliases: 'Classic Wings' }, item('Classic Wings', 900, { addons: [{ name: 'Buffalo', group: 'Flavor', price_cents: 50 }] }));
  assert.equal(wings.unitCents, 950);
  const steak = match1({ name: 'Steak Burrito Bowl', aliases: 'Burrito Bowl', modifiers: [{ name: 'Steak', price: '2.00' }] }, item('Burrito Bowl', 900, { addons: [{ name: 'Steak', group: 'Protein', price_cents: 200 }] }));
  assert.equal(steak.unitCents, 1100);
  // A store that lists your item under its own name: that listing is the match there, not the confirmed shorter name.
  const both = run(order([{ name: 'Steak Burrito Bowl', aliases: 'Burrito Bowl' }]), [obs({ items: [item('Burrito Bowl', 1070), item('Steak Burrito Bowl', 1270)] })]);
  assert.equal(both.winner.row.candidate, 'Steak Burrito Bowl'); assert.equal(both.winner.row.lineCents, 1270);
  assert.match(both.rows.uncertain.map(r => r.reasons.map(x => x.text).join(' ')).join(' '), /also lists "Steak Burrito Bowl"/);
  const only = run(order([{ name: 'Steak Burrito Bowl', aliases: 'Burrito Bowl' }]), [obs({ items: [item('Burrito Bowl', 1070)] })]);
  assert.equal(only.winner.row.lineCents, 1070);
  // Suggestions: repeated words and numbers count; plurals of variant words are spelling; a name-matched listing elsewhere does not hide them.
  const sug = (o, items, more = []) => run(order([typeof o === 'string' ? { name: o } : o]), [obs({ items })].concat(more)).closest[0].listings.map(l => l.label);
  assert.deepEqual(sug('Large 2-Topping Pizza', [item('2 Large 2-Topping Pizzas', 1999)]), []);
  assert.deepEqual(sug('Double Burger', [item('Double-Double Burgers', 599)]), []);
  assert.deepEqual(sug('Chicken Quesadilla', [item('Chicken Quesadillas', 599)]), ['Chicken Quesadillas']);
  assert.deepEqual(sug('Small Fries', [item('Fries', 199)], [obs({ platform: 'ubereats', items: [item('World Famous Fries® - Small', 229)] })]), ['World Famous Fries® - Small']);
  assert.deepEqual(sug('Coke 20 oz', [item('Coca-Cola Coke 2 Liter', 399)]), []);
  // Volumes: ounces, liters, gallons, quarts and pints are one dimension.
  assert.equal(st('Coke 1 Liter', 'Coke 33.8 oz'), 'exact');
  assert.equal(st('Sweet Tea Gallon', 'Sweet Tea Half Gallon'), 'rejected');
  assert.equal(st('Sweet Tea Gallon', 'Sweet Tea (1 Gallon)'), 'exact');
  assert.equal(st('Wonton Soup Quart', 'Wonton Soup Pint'), 'rejected');
  assert.equal(st('Wonton Soup (Qt)', 'Wonton Soup Quart'), 'exact');
  assert.notEqual(st({ name: 'Sweet Tea Gallon', aliases: 'Sweet Tea' }, 'Sweet Tea'), 'exact');
  // Vowel signs, voicing marks and flag emoji are part of the name; accents on Latin letters are not.
  for (const [o, c] of [['หมี่กรอบ', 'หมูกรอบ'], ['चना मसाला', 'चीनी मसाला'], ['ぎょうざ', 'きょうさ'], ['Tacos 🇺🇸', 'Tacos 🇲🇽']]) assert.equal(st(o, c), 'rejected', c);
  for (const n of ['หมี่กรอบ', 'चना मसाला', 'ぎょうざ', 'Tacos 🇲🇽']) assert.equal(st(n, n), 'exact', n);
  assert.equal(st('Café Latte', 'Cafe Latte'), 'exact');
  assert.equal(st('Jalapeño Poppers', 'Jalapeno Poppers'), 'exact');
  // Accents on Latin and Greek letters only; "Й" is its own letter, "ё" is commonly written "е".
  assert.equal(st('Σουβλάκι', 'Σουβλακι'), 'exact');
  assert.equal(st('Йогурт', 'Иогурт'), 'rejected');
  assert.equal(st('Мёд', 'Мед'), 'exact');
  // Equal piece counts settle an unstated serving size either way round.
  assert.equal(st('12 pc Nuggets (Serves 4)', '12 pc Nuggets'), 'exact');
  assert.equal(st('12 pc Nuggets', '12 pc Nuggets (Serves 4)'), 'exact');
  assert.equal(st('Nuggets (Serves 4)', 'Nuggets'), 'uncertain');
});
