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
  assert.equal(match1({ name: 'Turkey Sub', size: '6"' }, item('Turkey Sub Footlong', 999)).status, 'uncertain');
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
    assert.equal(E.inSnapshotScope(nug, [{ name: 'Big Mac', aliases: 'McNuggets' }]), true);
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
  assert.match(r.reasons.map(x => x.text).join(' '), /Piece count matches \(10\); the listing states it as a leading number/);
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

test('typed entries for one store and mode are merged, keeping the older time', () => {
  const f = (lines, at) => E.enteredObservation({ platform: 'doordash', order_mode: 'delivery', seen_at: at, store_name: 'Taco Bell', city: 'Fulton', state: 'MS', zip: '38843',
    store_url: 'https://www.doordash.com/store/taco-bell-fulton-24633898/', lines }, { now: NOW }).observations[0];
  let list = E.mergeEntered([], f('Crunchwrap Supreme $6.99', '2026-10-07T09:00:00Z')).list;
  const mg = E.mergeEntered(list, f('Chalupa Supreme $5.49\nCrunchwrap Supreme $7.09', '2026-10-07T11:00:00Z'));
  assert.equal(mg.merged, true);
  list = mg.list;
  assert.equal(list.length, 1);
  assert.deepEqual(list[0].items.map(i => [i.name, i.price_cents]), [['Chalupa Supreme', 549], ['Crunchwrap Supreme', 709]]);
  assert.equal(list[0].observed_at, '2026-10-07T09:00:00.000Z');
  // Both items rank from the one record.
  const basket = run(order([{ name: 'Crunchwrap Supreme' }, { name: 'Chalupa Supreme' }], { chain: 'Taco Bell' }), list);
  assert.equal(basket.winner.basket.subtotalCents, 709 + 549);
  // Another mode, another store, or a plain import is never merged.
  assert.equal(E.mergeEntered(list, Object.assign(f('Taco $2.00', '2026-10-07T10:00:00Z'), { order_mode: 'pickup' })).merged, false);
  const imported = Object.assign({}, list[0], { extraction: 'imported record' });
  assert.equal(E.mergeEntered([imported], f('Taco $2.00', '2026-10-07T10:00:00Z')).merged, false);
});
