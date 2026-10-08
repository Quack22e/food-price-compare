// Rank the REAL observations from a live-verification report with the exact
// engine shipped in index.html.
//   node scripts/rank_report.mjs live-verification/report.json "McDonald's" "10 pc. Chicken McNuggets" [qty] [grubhubSubtotal]
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
vm.runInThisContext(/<script id="fpc-engine">([\s\S]*?)<\/script>/.exec(html)[1]);
const E = globalThis.FPCEngine;
const [reportPath, chain, itemName, qty = '1', gh = ''] = process.argv.slice(2);
const report = JSON.parse(readFileSync(reportPath, 'utf8'));
const observations = report.jobs.flatMap(j => j.observations || []);
const order = { chain, chainAliases: '', mode: 'delivery', items: [{ name: itemName, size: '', flavor: '', count: '', quantity: qty, ghSubtotal: gh, modifiers: [], aliases: '' }] };
const ev = E.evaluate(order, observations, { mode: 'delivery', platforms: ['doordash', 'ubereats'], staleHours: 24 * 7, now: Date.parse(report.finished_at) });
const out = {
  observations: observations.length,
  winner_label: ev.winner ? ev.winnerLabel : null,
  winner: ev.winner && { store: ev.winner.row.storeName, address: ev.winner.row.address, listing: ev.winner.row.candidate,
    unit: E.formatCents(ev.winner.row.unitCents), subtotal: E.formatCents(ev.winner.row.lineCents), observed_at: ev.winner.row.observedAt,
    source: ev.winner.row.storeUrl, mode: ev.winner.row.orderMode, mode_basis: ev.winner.row.modeBasis,
    savings_vs_grubhub: ev.winner.row.savingsCents == null ? null : E.formatCents(ev.winner.row.savingsCents) },
  verified: ev.rows.verified.map(r => `${E.formatCents(r.lineCents)}  ${r.candidate} @ ${r.storeName}, ${r.city} ${r.state} ${r.zip}`),
  needs_confirmation: ev.rows.uncertain.map(r => `${r.candidate} @ ${r.storeName}: ${r.reasons.filter(x => x.level === 'warn').map(x => x.text).join('; ')}`),
  not_equivalent: ev.rows.rejected.slice(0, 8).map(r => `${r.candidate} @ ${r.storeName}: ${r.reasons.filter(x => x.level === 'fail').map(x => x.text).join('; ')}`),
  coverage: ev.coverage.statement,
};
console.log(JSON.stringify(out, null, 1));
