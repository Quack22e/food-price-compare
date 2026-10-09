// Rank every search in a live snapshot with the exact engine shipped in
// index.html, and compare pickup with delivery prices per branch.
//   node scripts/rank_snapshot.mjs [live-verification/snapshot.json]
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
vm.runInThisContext(/<script id="fpc-engine">([\s\S]*?)<\/script>/.exec(html)[1]);
const E = globalThis.FPCEngine;
const snap = JSON.parse(readFileSync(process.argv[2] || new URL('../live-verification/snapshot.json', import.meta.url), 'utf8'));
const now = Date.parse(snap.finished_at || snap.generated_at) || Date.now();
const money = (c) => (Number.isInteger(c) ? E.formatCents(c) : '—');
const where = (a) => [a.city, a.state].filter(Boolean).join(' ');

const out = { run: snap.github_run_id, generated_at: snap.generated_at, searches: [], pickup_vs_delivery: [], checks: snap.checks || {} };
for (const s of snap.searches || []) {
  const o = s.request.order;
  const mode = s.request.order_mode || 'delivery';
  const order = { chain: o.chain, chainAliases: '', mode, items: o.items.map((it) => ({ name: it.name, size: '', flavor: '', count: '', quantity: '1', ghSubtotal: '', modifiers: [], aliases: '' })) };
  const ev = E.evaluate(order, s.observations || [], { mode, platforms: ['doordash', 'ubereats'], staleHours: 24 * 7, now });
  const r = { label: s.label, mode, checked: ev.coverage.checkedLocations, label_text: ev.winner ? ev.winnerLabel : null };
  if (ev.multi) {
    r.baskets = ev.baskets.map((b) => ({ status: b.status, store: b.storeName, where: where(b), subtotal: money(b.subtotalCents),
      parts: b.parts.map((p) => `${p.candidate} ${money(p.lineCents)}`), missing: b.missing, uncertain: b.uncertain }));
    r.winner = ev.winner && { store: ev.winner.basket.storeName, where: where(ev.winner.basket), subtotal: money(ev.winner.basket.subtotalCents) };
  } else {
    r.verified = ev.rows.verified.map((x) => `${money(x.lineCents)} ${x.candidate} @ ${x.storeName} (${where(x)})`);
    r.winner = ev.winner && { store: ev.winner.row.storeName, where: where(ev.winner.row), price: money(ev.winner.row.lineCents), observed_at: ev.winner.row.observedAt };
  }
  out.searches.push(r);
}

// Same item, same store, both modes requested in this run.
const byKey = {};
for (const s of snap.searches || []) {
  const items = s.request.order.items;
  if (items.length !== 1) continue;
  for (const ob of s.observations || []) {
    // Exact matches only, by the engine's own item rule.
    const want = E.prepareItem({ name: items[0].name, quantity: '1' }, 0);
    const exact = (ob.items || []).filter((it) => E.matchItem(want, it, { obsCurrency: ob.currency }).status === 'exact');
    const best = exact.sort((a, b) => a.price_cents - b.price_cents)[0];
    if (!best || !Number.isInteger(best.price_cents)) continue;
    const k = `${s.request.order.chain}|${items[0].name}|${ob.store_id}`;
    byKey[k] = byKey[k] || { item: items[0].name, store: ob.store_name, where: where(ob.address || {}) };
    byKey[k][ob.order_mode] = best.price_cents;
  }
}
for (const v of Object.values(byKey)) {
  if (v.delivery != null && v.pickup != null) {
    out.pickup_vs_delivery.push({ item: v.item, store: v.store, where: v.where, delivery: money(v.delivery), pickup: money(v.pickup), same: v.delivery === v.pickup });
  }
}
console.log(JSON.stringify(out, null, 1));
