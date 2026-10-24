import type { APIRoute } from 'astro';
import { SITE } from '../data/site';
import { NETWORKS } from '../data/networks';
import { PLANS, yearlyPerMonth } from '../data/plans';
import { FAQ } from '../data/faq';
export const GET: APIRoute = () => {
  const plans = PLANS.map((p) => `- ${p.name}: ${p.monthly ? `$${p.monthly}/month or $${p.yearly}/year ($${yearlyPerMonth(p)}/month)` : '$0'}; ${p.channels} channels, ${p.workspaces} workspaces, ${p.members} team members, ${p.apiCalls} API and MCP calls per day.`).join('\n');
  const nets = NETWORKS.map((n) => `- ${n.name}: caption limit ${n.limit.toLocaleString('en-US')}${n.unit === 'utf8_bytes' ? ' bytes' : ' characters'}`).join('\n');
  const faq = FAQ.map((f) => `### ${f.q}\n${f.a.replace(/<[^>]+>/g, '')}`).join('\n\n');
  const body = `# PostDelegate: full product facts

${SITE.tagline}

PostDelegate is a social media scheduler for people and AI agents, operated by ${SITE.operator}. App: ${SITE.app}. Contact: ${SITE.email}. Source code (AGPL-3.0): ${SITE.source}

## What it does
Calendar (day, week, month, list), composer with per-channel customisation and live character counts, queues and posting slots with best-time suggestions, approvals with internal review and a separate client sign-off, client portal, social inbox, analytics, scheduled white-label reports, competitor benchmarks, evergreen recycling and recurring posts, link-in-bio pages, short links, media library, REST API, MCP server and webhooks.

## Plans
${plans}
Free has no approvals, inbox, analytics, client portal, reports, competitors or AI visibility report. Paid plans include all of them. Every new workspace gets a 14-day trial with Ultimate allowances, no card required; without a subscription it moves to Free and nothing is deleted.

## AI
AI is never part of a plan. Use your own API key (Anthropic, OpenAI, Google Gemini or any OpenAI-compatible service), or use PostDelegate from Claude or ChatGPT through the MCP server at ${SITE.app}/api/v1/mcp. Agents connect with OAuth or scoped API keys.

## Agent safety
Each workspace sets an autonomy level per action (draft, schedule and publish, reply, recycle): Off, Draft only, Auto with guardrails, Full auto. New workspaces start at Draft only. Guardrails cover blocked phrases, links, mentions, prices, health, financial and legal claims, a daily cap per account and quiet hours. Autonomous posts wait in an undo window (5 minutes by default). Crisis pause stops all autonomous actions. Every action is written to an audit trail. An API key can only tighten the policy. The dial never skips an approval the workspace requires.

## Networks (${NETWORKS.length})
Each platform controls its own API access, account types and permissions, so availability of features varies by channel.
${nets}

## FAQ
${faq}

## Pages
${SITE.url}/ , /pricing , /platforms , /agents , /mcp , /api , /claude-cowork , /codex , /compare , /tools , /about , /support , /status
`;
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
