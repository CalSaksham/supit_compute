export const TIER_POINTS = {'Platinum': 95, 'Gold': 85, 'Silver': 74, 'Bronze': 62, 'Participation Ribbon': 48, 'Underperforming': 30};
export const TIER_RANK = {'Platinum': 5, 'Gold': 4, 'Silver': 3, 'Bronze': 2, 'Participation Ribbon': 1, 'Underperforming': 0};
export const EVIDENCE_PRIOR_GPU_HOURS = 100000;
export const HOURS_PER_MONTH = 730;
export const DEMO_DEAL_KEY = 'supit-demo-deal';

const clamp = v => Math.max(0, Math.min(1, v));

export function supitScore(s) {
  const parts = {
    uptime: clamp((s.uptime - 98.5) / 1.4) * 100,
    onTime: s.onTime,
    handover: s.handoverFirstPass,
    speed: clamp((s.speedVsContract - 0.85) / 0.15) * 100,
    credits: s.creditsPaid
  };
  const score = .3 * parts.uptime + .2 * parts.onTime + .2 * parts.handover + .15 * parts.speed + .15 * parts.credits;
  return {score, parts};
}

// Uptime and speed are weighted by GPU-hours; per-deal outcomes (on time, first pass, credits paid) by deal count.
export function mergeDeal(base, deal) {
  if (!base) return {...deal};
  const h = base.gpuHours + deal.gpuHours, n = base.deals + deal.deals;
  const byHours = k => (base[k] * base.gpuHours + deal[k] * deal.gpuHours) / h;
  const byDeals = k => (base[k] * base.deals + deal[k] * deal.deals) / n;
  return {...base, deals: n, gpuHours: h, uptime: byHours('uptime'), speedVsContract: byHours('speedVsContract'),
    onTime: byDeals('onTime'), handoverFirstPass: byDeals('handoverFirstPass'), creditsPaid: byDeals('creditsPaid')};
}

export function scoreProvider(p, sample) {
  const cmPoints = TIER_POINTS[p.cm] ?? null;
  if (!sample) return {cmPoints, supit: null, weight: 0, combined: cmPoints};
  const {score, parts} = supitScore(sample);
  const weight = sample.gpuHours / (sample.gpuHours + EVIDENCE_PRIOR_GPU_HOURS);
  return {cmPoints, supit: {score, parts, ...sample}, weight, combined: cmPoints == null ? score : cmPoints * (1 - weight) + score * weight};
}

// The demo deal is one 730-hour month in which each second of the demo session stands for one hour.
export const DEMO_HOURS_PER_SECOND = 1;

export function dealRecord({gpus, price, holdbackShare, claim, handoverPassed, contractedTps, slowTps}) {
  if (!handoverPassed) return null;
  const monthGpuHours = gpus * HOURS_PER_MONTH;
  const toHours = gpuMinutes => gpuMinutes * 60 * DEMO_HOURS_PER_SECOND;
  const downShare = Math.min(1, toHours(claim.downtime_gpu_minutes) / monthGpuHours);
  const slowShare = Math.min(1, toHours(claim.slow_gpu_minutes) / monthGpuHours);
  const owed = claim.total_claim * 3600 * DEMO_HOURS_PER_SECOND;
  const holdback = monthGpuHours * price * holdbackShare;
  return {
    deals: 1,
    gpuHours: monthGpuHours,
    onTime: 100,
    handoverFirstPass: 100,
    uptime: 100 * (1 - downShare),
    speedVsContract: 1 - slowShare * (1 - slowTps / contractedTps),
    creditsPaid: owed > 0 ? 100 * Math.min(1, holdback / owed) : 100,
    downHours: toHours(claim.downtime_gpu_minutes) / gpus,
    slowHours: toHours(claim.slow_gpu_minutes) / gpus
  };
}
