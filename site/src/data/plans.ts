// Source of truth: apps/billing/plans.py and docs/agents/capabilities.md in the app repo.
export interface Plan {
  slug: string; name: string; tagline: string;
  monthly: number; yearly: number; // yearly = total per year billed annually
  channels: number; workspaces: string; members: string; apiCalls: string;
  popular?: boolean;
}
export const PLANS: Plan[] = [
  { slug: 'free', name: 'Free', tagline: 'For trying PostDelegate on a few channels.', monthly: 0, yearly: 0, channels: 3, workspaces: '1', members: '2', apiCalls: '100' },
  { slug: 'starter', name: 'Starter', tagline: 'For one brand and a client to approve.', monthly: 19, yearly: 180, channels: 5, workspaces: '2', members: '2', apiCalls: '2,000' },
  { slug: 'team', name: 'Team', tagline: 'For small teams working across brands.', monthly: 29, yearly: 276, channels: 10, workspaces: '5', members: 'Unlimited', apiCalls: '4,000', popular: true },
  { slug: 'pro', name: 'Pro', tagline: 'For agencies growing their client list.', monthly: 39, yearly: 372, channels: 30, workspaces: '15', members: 'Unlimited', apiCalls: '10,000' },
  { slug: 'ultimate', name: 'Ultimate', tagline: 'For large agencies and heavy agent use.', monthly: 79, yearly: 756, channels: 100, workspaces: 'Unlimited', members: 'Unlimited', apiCalls: '20,000' },
];
export const yearlyPerMonth = (p: Plan) => Math.floor(p.yearly / 12);
