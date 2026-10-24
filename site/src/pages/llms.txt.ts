import type { APIRoute } from 'astro';
import { SITE } from '../data/site';
import { NETWORKS } from '../data/networks';
import { PLANS } from '../data/plans';
export const GET: APIRoute = () => {
  const body = `# PostDelegate

> ${SITE.tagline} PostDelegate is a social media scheduler for people and AI agents. Plan, review, schedule and publish to ${NETWORKS.length} networks from one calendar; connect Claude, ChatGPT or any MCP client, or use your own AI key.

PostDelegate is operated by ${SITE.operator}. The web app is at ${SITE.app}. The workspace is open source (AGPL-3.0): ${SITE.source}

## Key facts
- Free plan: $0, 3 channels. Paid plans: ${PLANS.filter((p) => p.monthly).map((p) => `${p.name} $${p.monthly}/month`).join(', ')}. Yearly billing is 20% cheaper. Every paid plan includes every feature.
- Every new workspace gets a 14-day trial with Ultimate allowances, no card required.
- AI is bring-your-own: use your own API key (Anthropic, OpenAI, Google Gemini or any OpenAI-compatible service) or drive PostDelegate from Claude or ChatGPT over MCP. No AI is included in or billed by any plan.
- Agent safety: a per-action autonomy dial (Off, Draft only, Auto with guardrails, Full auto; new workspaces start at Draft only), an undo window (5 minutes by default), crisis pause, guardrails and a tamper-resistant audit trail. API keys can only tighten the policy.

## Docs
- [Full product facts](${SITE.url}/llms-full.txt)
- [For AI agents](${SITE.url}/agents)
- [MCP server](${SITE.url}/mcp)
- [REST API](${SITE.url}/api)
- [Pricing](${SITE.url}/pricing)
- [Supported platforms](${SITE.url}/platforms)
- [Compare with Postiz](${SITE.url}/compare/postiz)
- [Compare with Post Bridge](${SITE.url}/compare/post-bridge)
- [Caption checker](${SITE.url}/tools/caption-checker)
- [Support](${SITE.url}/support)
`;
  return new Response(body, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } });
};
