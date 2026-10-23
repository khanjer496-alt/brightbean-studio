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
and legal claims (English and Arabic), a daily cap per account, quiet hours, and two optional AI checks
(risk and brand-voice drift) that count against the plan's AI allowance and hold the action if the AI
can't be reached or the text is too long for it to read in full.

The checks read everything the post would publish: caption, title, first comment, every thread part and
each account's overrides. Text is folded before matching, so case, accents, zero-width characters,
full-width or styled letters and look-alike letters from other scripts don't change the result.

Every write path an agent has goes through the policy: creating, scheduling, editing, duplicating and
queueing posts, resuming a stopped thread (refused rather than held when publishing isn't allowed),
creating, editing and sending replies. Editing a reply already waiting out its undo window is a new send
decision with a fresh window. A schedule that is held keeps its repeat setting; the series starts when a
person approves the post.

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
`GET /autonomy` / `get_autonomy_policy`; a key (or an OAuth/MCP session) can only make the policy
stricter: lower levels, switch crisis pause on, add blocked phrases, switch a review check on, lengthen the
undo window or lower a daily cap. Sending back an unchanged value is fine; anything looser needs a person.

## Undo window, crisis pause, audit

Autonomous posts and replies wait (5 minutes by default) in "Going out soon", where a person can undo
or hold them. After publishing, "Delete from platform" appears where the platform allows it. Undo and
hold take effect against the publisher's claim, so either they land before the send starts or they say it
already went out. Crisis pause stops every autonomous action and holds queued ones until it is turned
off; the publisher re-checks it at the moment it sends.

The policy is asked again at the moment of sending, not only when the agent asked. If crisis pause is on
or the level has been lowered below Auto with guardrails since, a post an agent queued (or a copy in a
repeating series an agent started) is held as a draft for a person to approve in Activity, and a thread
an automation started stops between parts (the live parts stay; approving it in Activity resumes it).
Posts a person scheduled or approved last, and series a person set up, aren't stopped by the level.
Unattended replies to reviews follow the reply level the same way.

At Draft only a blocked phrase doesn't refuse a draft: the draft is saved and flagged, and the warning
shows beside it in Activity. Every action, by a person,
agent or automation, is in the workspace's Activity page and `GET /autonomy/actions`, with the decision,
the guardrail results and a diff against the source text. Held and blocked actions notify owners and
managers and send `agent.action.held` / `agent.action.blocked` webhooks. The trail can't be edited or
deleted from the app, the admin or the API; entries are kept for `AUTONOMY_AUDIT_RETENTION_DAYS`
(400 by default), except ones still waiting for a person.
