# Autonomy: what agents may do on their own

Each workspace sets a **level** for four kinds of action. People are never held back by it: they follow roles
and the approval workflow. The dial applies to agents (API keys, OAuth/MCP sessions) and to automations
(evergreen recycling, repeating posts).

| Level | Meaning |
| --- | --- |
| Off | The action is refused. Nothing is saved. |
| Draft only | Drafts are written, but anything that would go out waits for a person. |
| Auto with guardrails | It goes out after the undo window if every guardrail passes; otherwise it is held as a draft. |
| Full auto | It goes out after the undo window. Only blocked phrases and crisis pause still apply. |

Actions: draft content, schedule and publish posts, reply to comments and DMs, recycle evergreen posts
(which also has "each item's own setting", the default). New workspaces start at Draft only. A level can be
overridden per social account. The dial never skips an approval the workspace requires.

## Guardrails (Auto with guardrails)

Blocked words and phrases (refused outright), links, @mentions, prices and discounts, health, financial
and legal claims, a daily cap per account, quiet hours, and two optional AI checks (risk and brand-voice
drift) that count against the plan's AI allowance and hold the action if the AI can't be reached.

## What an agent is told

A write that the policy held or refused says why, so the agent can behave:

```json
"autonomy": {
  "decision": "held",
  "level": "draft_only",
  "reasons": ["its autonomy level is Draft only, so a person has to approve it"],
  "message": "Held for review because its autonomy level is Draft only, so a person has to approve it. It was saved as a draft; a person can approve it in Activity.",
  "action_id": "…",
  "undo_until": null
}
```

A refusal is an error (REST 403, MCP error) beginning "Blocked because". Read the policy first with
`GET /autonomy` / `get_autonomy_policy`; a key can lower levels, switch crisis pause on and add blocked
phrases, but only a person can loosen the policy.

## Undo window, crisis pause, audit

Autonomous posts and replies wait (5 minutes by default) in "Going out soon", where a person can undo
or hold them. After publishing, "Delete from platform" appears where the platform allows it. Crisis pause
stops every autonomous action and holds queued ones until it is turned off. Every action, by a person,
agent or automation, is in the workspace's Activity page and `GET /autonomy/actions`, with the decision,
the guardrail results and a diff against the source text. Held and blocked actions notify owners and
managers and send `agent.action.held` / `agent.action.blocked` webhooks.
