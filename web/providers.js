import {TIER_RANK, HOURS_PER_MONTH, DEMO_DEAL_KEY, mergeDeal, scoreProvider} from '/scoring.js?v=2';

const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money = (v, d = 2) => '$' + Number(v).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
const num = v => Number(v).toLocaleString('en-US', {maximumFractionDigits: 0});
const pct = v => Number(v).toLocaleString('en-US', {maximumFractionDigits: 2});

let data, providers = [];
const shortlist = new Set();
const readDemoDeal = () => { try { return JSON.parse(localStorage.getItem(DEMO_DEAL_KEY) || 'null'); } catch { return null; } };
let demoDeal = readDemoDeal();

function scored(p, useSample) {
  const base = useSample && p.sample ? p.sample : null;
  const deal = demoDeal?.providerId === p.id ? demoDeal.record : null;
  const s = deal ? mergeDeal(base, deal) : base;
  return {...p, ...scoreProvider(p, s), demo: deal ? {buyer: demoDeal.buyer, before: scoreProvider(p, base), record: deal} : null};
}

const filters = () => ({
  search: $('#f-search').value.trim().toLowerCase(),
  workload: $('#f-workload').value,
  gpu: $('#f-gpu').value,
  term: $('#f-term').value,
  region: $('#f-region').value,
  software: $('#f-software').value,
  price: Number($('#f-price').value),
  deposit: Number($('#f-deposit').value),
  tier: Number($('#f-tier').value),
  count: Math.max(1, Number($('#f-count').value) || 0),
  supit: $('#f-supit').checked,
  sort: $('#f-sort').value,
  sample: $('#sample-toggle').checked
});

function matches(p, f) {
  if (f.search && !p.name.toLowerCase().includes(f.search)) return false;
  if (f.workload === 'training' && !p.fabric) return false;
  if (f.gpu && !p.gpus.includes(f.gpu)) return false;
  if (f.term && !p.terms[f.term]) return false;
  if (f.region && !p.regions.includes(f.region)) return false;
  if (f.software && !p.software.includes(f.software)) return false;
  if (p.price > f.price) return false;
  if (p.depositPct > f.deposit) return false;
  if ((TIER_RANK[p.cm] ?? -1) < f.tier) return false;
  if (f.supit && !(f.sample && p.sample?.supitTerms)) return false;
  return true;
}

const sorters = {
  combined: (a, b) => (b.combined ?? -1) - (a.combined ?? -1),
  cm: (a, b) => (b.cmPoints ?? -1) - (a.cmPoints ?? -1) || (b.combined ?? -1) - (a.combined ?? -1),
  supit: (a, b) => (b.supit?.score ?? -1) - (a.supit?.score ?? -1),
  price: (a, b) => a.price - b.price
};

const tierClass = t => 'tier-' + String(t).toLowerCase().replace(/\s+/g, '-');
const tierPill = t => `<span class="tier ${tierClass(t)}">${esc(t)}</span>`;
const termText = p => [p.terms.od && 'On-demand', p.terms.short && '1–6 mo', p.terms.long && '12+ mo'].filter(Boolean).join(' · ');
const scoreBar = v => `<span class="bar"><i style="width:${Math.round(v)}%"></i></span>`;

function row(p, f) {
  const monthly = p.price * f.count * HOURS_PER_MONTH;
  const chips = (f.sample && p.sample ? '<span class="sample-chip">SAMPLE</span>' : '') + (p.demo ? `<span class="sample-chip demo-chip">+ ${esc(p.demo.buyer).toUpperCase()} DEMO</span>` : '');
  const was = p.demo && p.demo.before.supit ? ` · ${p.demo.before.supit.score.toFixed(1)} → ${p.supit.score.toFixed(1)}` : '';
  const supitCell = p.supit
    ? `<strong class="score">${Math.round(p.supit.score)}</strong>${chips}<small>${p.supit.deals} deals · ${num(p.supit.gpuHours / 1000)}k GPU-hrs${was}</small>`
    : `<span class="muted">No deal data yet</span>`;
  const combinedCell = p.combined == null ? '<span class="muted">—</span>'
    : `<strong class="score">${Math.round(p.combined)}</strong>${scoreBar(p.combined)}<small>${p.supit ? Math.round(p.weight * 100) + '% weight on SUPIT data' : 'ClusterMAX only'}</small>`;
  const terms = f.sample && p.sample ? (p.sample.supitTerms ? '<span class="yes">✓ Yes</span>' : '<span class="muted">Not yet</span>') : '<span class="muted">—</span>';
  return `<tr data-id="${p.id}"><td><button class="provider-name" data-open="${p.id}">${esc(p.name)}</button><small>${esc(p.gpus.map(g => g === 'GB200' ? 'GB200/GB300' : g === 'AMD' ? 'AMD MI300X/MI355X' : g).join(', '))}</small><small>${esc(p.regions.join(' · '))}${p.fabric ? ' · High-speed fabric' : ''}</small></td>
<td>${tierPill(p.cm)}</td><td>${supitCell}</td><td>${combinedCell}</td>
<td><strong>${money(p.price)}</strong><small>≈ ${money(monthly, 0)} / mo for ${num(f.count)} GPUs</small></td>
<td>${esc(termText(p))}<small>${p.depositPct ? p.depositPct + '% deposit' : 'No deposit'}</small></td>
<td class="center">${terms}</td>
<td class="center"><input type="checkbox" class="shortlist-box" data-shortlist="${p.id}" aria-label="Shortlist ${esc(p.name)}" ${shortlist.has(p.id) ? 'checked' : ''} ${!shortlist.has(p.id) && shortlist.size >= 3 ? 'disabled' : ''}></td></tr>`;
}

function render() {
  const f = filters();
  $('#f-price-out').textContent = money(f.price);
  const list = providers.map(p => scored(p, f.sample)).filter(p => matches(p, f)).sort(sorters[f.sort]);
  $('#provider-rows').innerHTML = list.map(p => row(p, f)).join('');
  $('#empty').hidden = list.length > 0;
  $('#result-count').textContent = `${list.length} of ${providers.length} providers · ClusterMAX ${data.clustermax.version}, ${new Date(data.clustermax.published).toLocaleDateString('en-US', {month: 'short', year: 'numeric'})}`;
  $('#f-supit').disabled = !f.sample;
  const dp = demoDeal && providers.find(p => p.id === demoDeal.providerId);
  $('#demo-deal-note').hidden = !dp;
  if (dp) $('#demo-deal-text').textContent = `Includes a live demo deal: ${demoDeal.buyer} × ${dp.name}.`;
  renderShortlist();
}

function renderShortlist() {
  const names = providers.filter(p => shortlist.has(p.id)).map(p => p.name);
  $('#shortlist-bar').hidden = !names.length;
  $('#shortlist-count').textContent = `${names.length} of 3 shortlisted`;
  $('#shortlist-names').textContent = names.join(' · ');
}

let lastFocus;
function openProfile(id) {
  const f = filters();
  const p = scored(providers.find(x => x.id === id), f.sample);
  const s = p.supit;
  const part = (label, weight, value, detail) => `<div class="part"><div class="part-top"><span>${label} <em>${weight}</em></span><strong>${Math.round(value)}</strong></div>${scoreBar(value)}<small>${detail}</small></div>`;
  const d = p.demo, r = d?.record;
  const demoBlock = d ? `<div class="demo-pass"><h3>Includes ${esc(d.buyer)}’s demo deal <span class="sample-chip demo-chip">LIVE FROM DEMO</span></h3>
<p class="muted-p">One month of ${num(r.gpuHours)} GPU-hours, where each demo second stands for one hour: ${pct(r.uptime)}% uptime, ${Math.round(r.speedVsContract * 100)}% of contracted speed, ${Math.round(r.creditsPaid)}% of credits paid.</p>
<div class="kv"><span>SUPIT score</span><strong>${d.before.supit ? d.before.supit.score.toFixed(1) : '—'} → ${s.score.toFixed(1)}</strong></div>
<div class="kv"><span>Combined score</span><strong>${d.before.combined.toFixed(1)} → ${p.combined.toFixed(1)}</strong></div>
<a class="text-link" href="/demo.html#claim">Back to the demo ↗</a></div>` : '';
  const supitBlock = s ? `${demoBlock}<h3>SUPIT score: ${Math.round(s.score)} ${f.sample && p.sample ? '<span class="sample-chip">SAMPLE</span>' : ''}</h3><p class="muted-p">${s.deals} deals, ${num(s.gpuHours)} GPU-hours measured.</p>
${part('Uptime', '30%', s.parts.uptime, `${pct(s.uptime)}% of contracted GPU-hours`)}
${part('On-time delivery', '20%', s.parts.onTime, `${Math.round(s.onTime)}% of clusters passed handover by the contract date`)}
${part('Handover first-pass rate', '20%', s.parts.handover, `${Math.round(s.handoverFirstPass)}% passed on the first attempt`)}
${part('Speed vs contract', '15%', s.parts.speed, `${Math.round(s.speedVsContract * 100)}% of the contracted tokens/sec on average`)}
${part('Credits paid', '15%', s.parts.credits, `${Math.round(s.creditsPaid)}% of evidence-backed claims paid`)}`
    : `<h3>SUPIT score</h3><p class="muted-p">No deal data yet. The combined score equals the ClusterMAX rating until SUPIT measures a deal with ${esc(p.name)}.</p>`;
  const combined = s ? `${p.cmPoints} × ${(1 - p.weight).toFixed(2)} + ${Math.round(s.score)} × ${p.weight.toFixed(2)} = <strong>${Math.round(p.combined)}</strong>` : `<strong>${p.cmPoints}</strong> (ClusterMAX only)`;
  lastFocus = document.activeElement;
  $('#drawer').innerHTML = `<div class="drawer-top"><span class="eyebrow">PROVIDER PROFILE</span><button class="close" data-close aria-label="Close">×</button></div>
<h2 id="drawer-title">${esc(p.name)}</h2>
<div class="profile-scores"><div><span>ClusterMAX ${esc(data.clustermax.version)}</span>${tierPill(p.cm)}<small>${p.cmPoints} points</small></div><div><span>SUPIT</span><strong>${s ? Math.round(s.score) : '—'}</strong><small>${!s ? 'no deals yet' : d ? 'incl. demo deal' : 'sample data'}</small></div><div><span>Combined</span><strong>${Math.round(p.combined)}</strong><small>${s ? Math.round(p.weight * 100) + '% SUPIT weight' : 'ClusterMAX only'}</small></div></div>
<p class="formula">${combined}</p>
<h3>ClusterMAX rating</h3><p class="muted-p">SemiAnalysis rated ${esc(p.name)} <b>${esc(p.cm)}</b> in ClusterMAX ${esc(data.clustermax.version)}. <a href="${esc(data.clustermax.url)}" target="_blank" rel="noopener">Read their review ↗</a></p>
${supitBlock}
<h3>Commercial terms <span class="sample-chip">PLACEHOLDER</span></h3>
<div class="kv"><span>Indicative price</span><strong>${money(p.price)} / GPU-hr</strong></div>
<div class="kv"><span>For ${num(f.count)} GPUs</span><strong>≈ ${money(p.price * f.count * HOURS_PER_MONTH, 0)} / month</strong></div>
<div class="kv"><span>Terms</span><strong>${esc(termText(p))}</strong></div>
<div class="kv"><span>Typical deposit</span><strong>${p.depositPct ? p.depositPct + '%' : 'None'}</strong></div>
<div class="kv"><span>GPUs</span><strong>${esc(p.gpus.join(', '))}</strong></div>
<div class="kv"><span>Regions</span><strong>${esc(p.regions.join(', '))}</strong></div>
<div class="kv"><span>Software</span><strong>${esc(p.software.join(', ') || 'Bare machines')}</strong></div>
<button class="button button-dark drawer-cta" data-shortlist-toggle="${p.id}">${shortlist.has(p.id) ? 'Remove from shortlist' : 'Add to shortlist'} <span>↗</span></button>
<p class="drawer-note">Prices and terms are placeholders. Always confirm with the provider. SUPIT never takes payment from providers for placement.</p>`;
  $('#drawer').hidden = false; $('#drawer-overlay').hidden = false; document.body.style.overflow = 'hidden';
  $('#drawer .close').focus();
}
function closeProfile() { $('#drawer').hidden = true; $('#drawer-overlay').hidden = true; document.body.style.overflow = ''; if (lastFocus?.isConnected) lastFocus.focus(); }

function toggleShortlist(id, on) {
  if (on && shortlist.size >= 3) return;
  on ? shortlist.add(id) : shortlist.delete(id);
  sessionStorage.setItem('supit-shortlist', JSON.stringify([...shortlist]));
  sessionStorage.setItem('supit-shortlist-names', JSON.stringify(providers.filter(p => shortlist.has(p.id)).map(p => p.name)));
  render();
}

function reset() {
  $('#f-search').value = ''; $('#f-workload').value = 'inference';
  ['#f-gpu', '#f-term', '#f-region', '#f-software'].forEach(s => $(s).value = '');
  $('#f-price').value = 4; $('#f-deposit').value = 100; $('#f-tier').value = 0; $('#f-supit').checked = false; $('#f-sort').value = 'combined';
  render();
}

document.addEventListener('input', e => { if (e.target.closest('.filters') || e.target.id === 'sample-toggle') render(); });
document.addEventListener('change', e => {
  if (e.target.matches('[data-shortlist]')) toggleShortlist(e.target.dataset.shortlist, e.target.checked);
  else if (e.target.closest('.filters') || e.target.id === 'sample-toggle') render();
});
document.addEventListener('click', e => {
  const open = e.target.closest('[data-open]'); if (open) return openProfile(open.dataset.open);
  const t = e.target.closest('[data-shortlist-toggle]'); if (t) { const id = t.dataset.shortlistToggle; toggleShortlist(id, !shortlist.has(id)); return openProfile(id); }
  if (e.target.closest('[data-close]') || e.target.id === 'drawer-overlay') closeProfile();
});
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#drawer').hidden) closeProfile(); });
$('#f-reset').addEventListener('click', reset);

const toggle = $('#menu-toggle'), menu = $('#mobile-nav');
toggle.addEventListener('click', () => { menu.hidden = !menu.hidden; toggle.setAttribute('aria-expanded', String(!menu.hidden)); });

$('#demo-deal-clear').addEventListener('click', () => { localStorage.removeItem(DEMO_DEAL_KEY); demoDeal = null; render(); });
window.addEventListener('storage', e => {
  if (e.key !== DEMO_DEAL_KEY || !providers.length) return;
  demoDeal = readDemoDeal(); render();
  const open = !$('#drawer').hidden && $('#drawer [data-shortlist-toggle]')?.dataset.shortlistToggle;
  if (open) openProfile(open);
});

try { JSON.parse(sessionStorage.getItem('supit-shortlist') || '[]').forEach(id => shortlist.add(id)); } catch {}
fetch('/providers.json').then(r => r.json()).then(d => {
  data = d; providers = d.providers; render();
  const id = new URLSearchParams(location.search).get('provider');
  if (providers.some(p => p.id === id)) {
    document.querySelector(`tr[data-id="${id}"]`)?.scrollIntoView({block: 'center'});
    openProfile(id);
  }
})
  .catch(() => { $('#provider-rows').innerHTML = '<tr><td colspan="8">Provider data could not be loaded. Serve this folder over HTTP.</td></tr>'; });
